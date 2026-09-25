import json
import sys
import pytest
from dataclasses import replace
from pathlib import Path

from modeltrace_status.codex_runner import CodexRunner
from modeltrace_status.diagnostics import diagnose
from modeltrace_status.upstream_adapter import UpstreamAdapter


def fake_cli(tmp_path, body):
    path = tmp_path / "fake-codex"
    path.write_text(f"#!{sys.executable}\nimport sys,json,time\nif '--version' in sys.argv:\n print('fixture 1.0');sys.exit()\nsys.stdin.read()\n" + body)
    path.chmod(0o700)
    return str(path)


def test_runner_only_accepts_completed_model_output(settings, tmp_path):
    cli = fake_cli(tmp_path, '''
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'1, 2, 3'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'output_tokens':3}}),flush=True)
''')
    result = CodexRunner(replace(settings, codex=cli)).run(settings.monitors[0], "secret", "prompt")
    assert result["outcome"] == "responded"
    assert result["text"] == "1, 2, 3"
    assert result["ttft_ms"] is None and result["output_tps"] is None
    assert result["usage"]["output_tokens"] == 3


def test_runner_detects_tool_events_and_does_not_store_provider_errors(settings, tmp_path):
    cli = fake_cli(tmp_path, '''
print(json.dumps({'type':'item.started','item':{'type':'command_execution'}}),flush=True)
time.sleep(20)
''')
    result = CodexRunner(replace(settings, codex=cli)).run(settings.monitors[0], "secret", "prompt")
    assert result["outcome"] == "tool_use"
    cli = fake_cli(tmp_path, '''
print(json.dumps({'type':'item.completed','item':{'type':'error','message':'unexpected status 503 Service Unavailable: secret'}}),flush=True)
print(json.dumps({'type':'turn.failed','error':{'message':'unexpected status 503 secret'}}),flush=True)
sys.exit(1)
''')
    result = CodexRunner(replace(settings, codex=cli)).run(settings.monitors[0], "secret", "prompt")
    assert result["outcome"] == "provider_error" and result["http_status"] == 503
    assert "secret" not in json.dumps(result)


def test_runner_deadline_kills_child(settings, tmp_path):
    cli = fake_cli(tmp_path, "time.sleep(20)\n")
    result = CodexRunner(replace(settings, codex=cli, timeout=.1)).run(settings.monitors[0], "secret", "prompt")
    assert result["outcome"] == "timeout"
    assert result["duration_ms"] < 3000


def test_current_upstream_contract_and_reference_sample(settings):
    adapter = UpstreamAdapter(settings.upstream)
    plan = adapter.plan()
    assert len(plan) == 3
    with (settings.upstream / "data/gpt_reference.jsonl").open() as source:
        row = json.loads(next(source))
    result = adapter.assess([{"text": row["text"], "expected_count": row.get("requested_count", 300)}] * 3, "gpt-6-astra")
    assert result["valid_samples"] == 3
    assert abs(sum(r["weight"] for r in result["candidates"]) - 1) < 1e-8
    assert adapter.assess([], "gpt-6-astra")["identity"] == "unknown"
    refused = adapter.assess([{"text": "I can't help with that.", "expected_count": 300}] * 3, "gpt-6-astra")
    assert refused["identity"] == "inconclusive" and [d["accepted"] for d in refused["diagnostics"]] == [False] * 3
    unknown = adapter.assess([{"text": row["text"], "expected_count": 300}] * 3, "not-a-reference-model")
    assert unknown["identity"] == "not_in_library"


@pytest.mark.parametrize("message,status,code,outcome", [
    ('unexpected status 429: {"error":{"code":"concurrency_queue_timeout"}}', 429, "concurrency_queue_timeout", "rate_limit"),
    ('unexpected status 503: {"error":{"code":"no_eligible_account"}}', 503, "no_eligible_account", "provider_error"),
    ('stream disconnected: server_is_overloaded', None, "server_is_overloaded", "provider_error"),
    ('HTTP 503: unrelated provider diagnostic', 503, "http_503", "provider_error"),
    ('HTTP 401: invalid_api_key', 401, "invalid_api_key", "auth_error"),
    ('HTTP 429: insufficient_quota', 429, "insufficient_quota", "rate_limit"),
    ('HTTP 422: unknown request validation error', 422, "provider_error", "provider_error"),
    ('unexpected status 503: No eligible accounts for requested model', 503, "no_eligible_account", "provider_error"),
    ('unexpected status 429: Concurrency queue timed out', 429, "concurrency_queue_timeout", "rate_limit"),
])
def test_diagnostics_explain_specific_failures_without_echoing_provider_text(message, status, code, outcome):
    result = diagnose(message + ' secret-sentinel-do-not-publish https://private-host.test/?api_key=secret', http_status=status)
    assert result["diagnostic"]["code"] == code
    assert result["outcome"] == outcome
    assert result["diagnostic"]["detail"] and result["diagnostic"]["action"]
    assert "secret" not in json.dumps(result) and "private-host" not in json.dumps(result)


def test_final_provider_error_takes_precedence_over_stderr_warnings(settings, tmp_path):
    cli = fake_cli(tmp_path, '''
print('unrelated startup warning: HTTP 401 secret-sentinel', file=sys.stderr)
print(json.dumps({'type':'turn.failed','error':{'message':'unexpected status 429: {"error":{"code":"concurrency_queue_timeout","message":"secret-sentinel"}}'}}),flush=True)
sys.exit(1)
''')
    result = CodexRunner(replace(settings, codex=cli)).run(settings.monitors[0], "secret-sentinel", "prompt")
    assert result["outcome"] == "rate_limit" and result["http_status"] == 429
    assert result["diagnostic"]["code"] == "concurrency_queue_timeout"
    assert "secret-sentinel" not in json.dumps(result)


def test_checker_version_ignores_upstream_revision():
    from modeltrace_status.upstream_adapter import method_version
    method = {"scorer_sha256": "a", "bank_sha256": "b", "policy": "p", "probe_profile": "q"}
    assert method_version(method | {"upstream_commit": "one"}) == method_version(method | {"upstream_commit": "two"})
    assert method_version(method) != method_version(method | {"bank_sha256": "c"})
    assert method_version({"upstream_commit": "one"}) is None
