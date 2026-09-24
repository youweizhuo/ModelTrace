import json
import time
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from modeltrace_status.web import create_app, snapshot
from modeltrace_status.worker import Worker


class FakeAdapter:
    version = "test-method-v1"
    models = {"gpt-6-astra", "gpt-6-sol"}
    provenance = {"policy": "test", "bank_sha256": "abc"}

    def plan(self):
        return [{"prompt": "fixture", "expected_count": 300} for _ in range(3)]

    def sample_valid(self, output):
        return output["text"] != "short"

    def assess(self, outputs, expected):
        outputs = [o for o in outputs if self.sample_valid(o)]
        mismatch = len(outputs) == 3 and all(o["text"] == "mismatch" for o in outputs)
        return {"identity": "mismatch_signal" if mismatch else "consistent" if len(outputs) == 3 else "inconclusive" if outputs else "unknown",
                "candidates": [{"model": "gpt-6-sol" if mismatch else expected, "weight": .9}] if outputs else [], "valid_samples": len(outputs),
                "expected_weight": (.05 if mismatch else .9) if outputs else None}


class FakeRunner:
    version = "fixture-codex"

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.count = 0

    def run(self, *_):
        self.count += 1
        item = next(self.outcomes)
        responded = item in ("match", "mismatch", "short")
        return {"outcome": "responded" if responded else item,
                "text": item if responded else "", "usage": {"output_tokens": 10}, "duration_ms": 25}


def run_monitor(store):
    return store.targets()[0][:2]


def test_mismatch_runs_no_extra_batches(settings, store):
    runner = FakeRunner(["mismatch"] * 3)
    Worker(settings, store, runner, FakeAdapter()).check(*run_monitor(store))
    history = store.history("one")
    assert runner.count == 3 and len(history) == 1
    assert history[0]["identity"] == "mismatch_signal" and "confirmation" not in history[0]


def test_budget_reservation_is_atomic_and_precedes_inference(settings, store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: store.reserve_attempt("one", "fixture", 3), range(20)))
    assert sum(i is not None for i in ids) == 3
    runner = FakeRunner([])
    Worker(replace(settings, daily_budget=3), store, runner, FakeAdapter()).check(*run_monitor(store))
    assert runner.count == 0
    assert store.history("one")[0]["state"] == "budget_exhausted"
    assert store.targets()[0][2]["next_due"] > time.time()


def test_recovery_marks_inflight_checks_unknown(settings, store):
    m, _ = run_monitor(store)
    run = store.create_run(m, "scheduled", "test", {})
    store.reserve_attempt(m.id, run["id"], 10)
    store.recover()
    assert store.run(run["id"])["state"] == "interrupted"
    assert store.run(run["id"])["identity"] == "unknown"


def test_public_api_never_returns_keys_or_private_probe_evidence(settings, store):
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    app = create_app(settings)
    client = app.test_client()
    for url in ("/api/status", "/api/monitors/one/history", "/api/runs/" + store.history("one")[0]["id"]):
        response = client.get(url)
        assert response.status_code == 200
        assert b"secret-sentinel" not in response.data
        assert b'"text"' not in response.data
        assert b'"prompt"' not in response.data
        assert b"127.0.0.1:9999" not in response.data
    assert client.get("/api/admin/providers").status_code == 401
    assert client.get("/api/status?window=invalid").status_code == 400



def test_percent_encoded_drawer_link_redirects_to_fragment(settings, store):
    client = create_app(settings).test_client()
    response = client.get("/%23monitor=one&run=abc")
    assert response.status_code == 302 and response.headers["Location"] == "/#monitor=one&run=abc"
    assert client.get("/missing").json == {"error": "Not Found"}

def login(client, settings):
    token = client.get("/api/admin/session").json["csrf"]
    password = (settings.data_dir / "admin-password").read_text().strip()
    result = client.post("/api/admin/login", json={"password": password}, headers={"X-CSRF-Token": token})
    assert result.status_code == 200
    return {"X-CSRF-Token": result.json["csrf"]}


def test_provider_management_auth_csrf_rotation_and_pause(settings, store):
    client = create_app(settings).test_client()
    payload = {"name": "New provider", "base_url": "http://localhost:18082/v1", "api_key": "another-private-key",
               "monitors": [{"model": "gpt-6-sol", "interval": 3600}]}
    assert client.post("/api/admin/providers", json=payload).status_code == 403
    headers = login(client, settings)
    assert client.post("/api/admin/providers", json=payload, headers=headers | {"Origin": "https://attacker.test"}).status_code == 403
    response = client.post("/api/admin/providers", json=payload, headers=headers)
    assert response.status_code == 201
    pid = response.json["id"]
    listing = client.get("/api/admin/providers")
    assert b"another-private-key" not in listing.data and b"secret-sentinel" not in listing.data
    provider = next(p for p in listing.json["providers"] if p["id"] == pid)
    revision = next(m.revision for m, _, _ in store.targets() if m.provider == "New provider")
    update = provider | {"api_key": "replacement-private-key", "enabled": False}
    assert client.put("/api/admin/providers/" + pid, json=update, headers=headers).status_code == 200
    assert not any(m.provider == "New provider" for m, _, _ in store.targets())
    paused = next((m, key) for m, key, _ in store.targets(enabled=False) if m.provider == "New provider")
    assert paused[0].revision != revision
    assert paused[1] == "replacement-private-key"
    # Blank key keeps the existing secret; client-side provider data cannot expose it.
    update.update(api_key="", enabled=True)
    assert client.put("/api/admin/providers/" + pid, json=update, headers=headers).status_code == 200
    assert next(key for m, key, _ in store.targets() if m.provider == "New provider") == "replacement-private-key"
    assert client.delete("/api/admin/providers/" + pid, headers=headers).status_code == 200
    assert all(m.provider != "New provider" for m, _, _ in store.targets(enabled=False))


def test_provider_validation_rolls_back_secret_and_model_changes(settings, store):
    provider = store.admin_providers()[0]
    original = run_monitor(store)
    payload = provider | {"api_key": "should-rollback", "monitors": [provider["monitors"][0] | {"id": "foreign-id"}]}
    with pytest.raises(ValueError):
        store.save_provider(payload, provider["id"])
    assert run_monitor(store) == original


def test_configuration_change_and_worker_loss_invalidate_current_status(settings, store):
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    store.set_meta("heartbeat", {"at": time.time()})
    assert not snapshot(store, settings)["monitors"][0]["stale"]
    provider = store.admin_providers()[0]
    store.save_provider(provider | {"api_key": "new-private-key"}, provider["id"])
    assert snapshot(store, settings)["monitors"][0]["configuration_changed"]
    store.set_meta("heartbeat", {"at": time.time() - 100})
    assert snapshot(store, settings)["monitors"][0]["stale"]


def test_reasoning_effort_change_keeps_current_assessment(settings, store):
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    provider = store.admin_providers()[0]
    effort = "low" if provider["monitors"][0]["effort"] != "low" else "medium"
    store.save_provider(provider | {"monitors": [provider["monitors"][0] | {"effort": effort}]}, provider["id"])
    monitor = snapshot(store, settings)["monitors"][0]
    assert monitor["effort"] == effort and monitor["latest_completed"]["identity"] == "consistent"
    assert not monitor["configuration_changed"]


def test_history_pagination_and_retention(settings, store):
    m, _ = run_monitor(store)
    run = store.create_run(m, "scheduled", "v1", {})
    run.update(state="completed", finished_at=time.time())
    store.save_run(run, [{"text": "private-numeric-evidence"}])
    store.prune(90, 0)
    assert store.run(run["id"]) is not None
    with store.connect() as db:
        assert db.execute("SELECT evidence_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0] is None
    assert store.history(m.id, before=run["started_at"]) == []


def test_unknown_runner_errors_are_gaps_not_provider_outages(settings, store):
    Worker(settings, store, FakeRunner(["runner_error"] * 3), FakeAdapter()).check(*run_monitor(store))
    m = snapshot(store, settings)["monitors"][0]
    assert m["latest"]["availability"] == "unknown"
    assert m["metrics"]["success_rate"] is None
    assert m["metrics"]["unknown"] == 3


def test_worker_serializes_shared_server_across_keys_models_and_paths(settings, store):
    for name, base, model in [
        ("Second key", "http://127.0.0.1:9999/other/v1", "gpt-6-sol"),
        ("Independent API", "http://127.0.0.1:10000/v1", "gpt-6-astra"),
    ]:
        store.save_provider({"name": name, "base_url": base, "api_key": "fixture-key",
                             "monitors": [{"model": model}]})

    class ObservedRunner:
        version = "fixture"

        def __init__(self):
            self.lock = threading.Lock()
            self.active = Counter()
            self.max_shared = self.max_total = self.count = 0

        def run(self, monitor, *_):
            shared = ":9999/" in monitor.base_url
            with self.lock:
                self.active[shared] += 1
                self.count += 1
                self.max_shared = max(self.max_shared, self.active[True])
                self.max_total = max(self.max_total, sum(self.active.values()))
            time.sleep(.1)
            with self.lock:
                self.active[shared] -= 1
            return {"outcome": "responded", "text": "match", "usage": {}}

    runner = ObservedRunner()
    Worker(replace(settings, concurrency=3), store, runner, FakeAdapter()).serve(once=True)
    assert runner.count == 9
    assert runner.max_shared == 1
    assert runner.max_total == 2  # Different API servers can still run concurrently.
    assert all(len(store.history(m.id)) == 1 for m, _, _ in store.targets())


def test_saved_provider_diagnostic_is_visible_without_private_evidence(settings, store):
    from modeltrace_status.diagnostics import diagnose
    class ErrorRunner:
        version = "fixture"
        def run(self, *_):
            return diagnose('HTTP 429 concurrency_queue_timeout secret-sentinel', http_status=429) | {"http_status": 429}
    Worker(settings, store, ErrorRunner(), FakeAdapter()).check(*run_monitor(store))
    response = create_app(settings).test_client().get('/api/status')
    attempt = response.json['monitors'][0]['latest']['attempts'][0]
    assert attempt['diagnostic']['code'] == 'concurrency_queue_timeout'
    assert 'free account slot' in attempt['diagnostic']['detail']
    assert b'secret-sentinel' not in response.data


def test_pause_one_model_preserves_siblings_credentials_and_history(settings, store):
    provider = store.admin_providers()[0]
    store.save_provider(provider | {"monitors": provider["monitors"] + [{"model": "gpt-6-sol"}]}, provider["id"])
    original = next((m, key) for m, key, _ in store.targets() if m.id == "one")
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*original)
    store.due_at("one", time.time() + 3600)
    client = create_app(settings).test_client()
    url = "/api/admin/monitors/one"
    assert client.patch(url, json={"enabled": False}).status_code == 403
    headers = login(client, settings)
    assert client.patch(url, json={"enabled": "false"}, headers=headers).status_code == 400
    assert client.patch(url, json={"enabled": False, "model": "changed"}, headers=headers).status_code == 400
    assert client.patch(url, json={"enabled": False}, headers=headers).json["enabled"] is False
    assert [m.model for m, _, _ in store.targets()] == ["gpt-6-sol"]
    assert len(store.history("one")) == 1
    paused = next((m, key) for m, key, _ in store.targets(enabled=False) if m.id == "one")
    assert paused == original
    assert client.post(url + "/check", json={}, headers=headers).status_code == 404
    assert client.patch(url, json={"enabled": True}, headers=headers).status_code == 200
    resumed = next(runtime for m, _, runtime in store.targets() if m.id == "one")
    # The queued check was dropped; the next one follows the regular interval.
    assert resumed["next_due"] > time.time() + 60
    assert len(store.targets()) == 2
    assert client.patch("/api/admin/monitors/missing", json={"enabled": False}, headers=headers).status_code == 404


def test_pausing_during_a_probe_stops_remaining_samples(settings, store):
    class PauseRunner(FakeRunner):
        def run(self, monitor, *args):
            result = super().run(monitor, *args)
            store.set_monitor_enabled(monitor.id, False)
            return result
    runner = PauseRunner(["match"] * 3)
    Worker(settings, store, runner, FakeAdapter()).check(*run_monitor(store))
    assert runner.count == 1
    latest = store.history("one")[0]
    assert latest["state"] == "interrupted" and latest["identity"] == "unknown"


def test_history_color_uses_latest_finished_result_and_keeps_earlier_checks(settings, store):
    monitor, _ = run_monitor(store)
    now = time.time()
    def record(age, identity, state="completed", availability="available"):
        run = store.create_run(monitor, "scheduled", "test", {})
        run.update(started_at=now-age, finished_at=None if state=="running" else now-age+1,
                   state=state, identity=identity, availability=availability)
        with store.connect() as db:
            db.execute("UPDATE runs SET started_at=? WHERE id=?", (run["started_at"], run["id"]))
        store.save_run(run)
        return run
    record(120, "consistent")
    mismatch = record(60, "mismatch_signal")
    record(10, "unknown", "running")
    bucket = snapshot(store, settings, now=now)["monitors"][0]["history"][-1]
    assert bucket["latest_id"] == mismatch["id"]
    assert bucket["latest_identity"] == "mismatch_signal"
    assert bucket["latest_availability"] == "available"
    assert bucket["counts"] == {"consistent": 1, "mismatch_signal": 1}
    failure = record(5, "unknown", availability="unavailable")
    bucket = snapshot(store, settings, now=now)["monitors"][0]["history"][-1]
    assert bucket["latest_id"] == failure["id"] and bucket["latest_identity"] == "unknown"
    assert bucket["latest_availability"] == "unavailable"
    assert bucket["runs"] == 3  # Running probes don't replace a finished result.


def test_manual_check_reports_queue_progress_and_completion_without_requeue(settings, store):
    client = create_app(settings).test_client()
    assert client.get('/api/admin/activity').status_code == 401
    assert client.post('/api/admin/monitors/one/check', json={}).status_code == 403
    headers = login(client, settings)
    store.set_meta('heartbeat', {'at': time.time(), 'active': []})
    next_due = time.time() + 3600
    store.due_at('one', next_due)
    assert client.get('/api/admin/activity').json['monitors']['one']['state'] == 'idle'
    queued = client.post('/api/admin/monitors/one/check', json={}, headers=headers)
    assert queued.json['queued'] and queued.json['activity']['monitors']['one']['state'] == 'queued'
    assert client.get('/api/admin/providers').json['activity']['monitors']['one']['state'] == 'queued'
    monitor, _ = run_monitor(store)
    run = store.create_run(monitor, 'scheduled', 'test', {})
    run.update(planned_samples=3, attempts=[{'outcome': 'responded'}])
    store.save_run(run)
    store.due_at('one', next_due)
    progress = client.get('/api/admin/activity').json['monitors']['one']
    assert progress['state'] == 'running' and progress['completed_samples'] == 1
    assert progress['planned_samples'] == 3
    duplicate = client.post('/api/admin/monitors/one/check', json={}, headers=headers)
    assert not duplicate.json['queued'] and duplicate.json['state'] == 'running'
    assert store.targets()[0][2]['next_due'] == next_due
    run.update(state='completed', finished_at=time.time(), availability='available', attempts=[{'outcome': 'responded'}]*3)
    store.save_run(run)
    done = client.get('/api/admin/activity')
    assert done.json['monitors']['one']['state'] == 'idle'
    assert done.json['monitors']['one']['availability'] == 'available'
    assert done.json['monitors']['one']['finished_at'] == run['finished_at']
    assert b'secret-sentinel' not in done.data and b'"text"' not in done.data


def test_manual_check_can_queue_when_worker_is_offline_and_rejects_paused(settings, store):
    client = create_app(settings).test_client()
    headers = login(client, settings)
    store.set_meta('heartbeat', {'at': time.time()-60, 'active': ['one']})
    monitor, _ = run_monitor(store)
    store.create_run(monitor, 'scheduled', 'test', {})  # Left behind by a stopped worker.
    store.due_at('one', time.time()+3600)
    queued = client.post('/api/admin/monitors/one/check', json={}, headers=headers).json
    assert queued['queued'] and not queued['activity']['worker_online']
    assert queued['activity']['monitors']['one']['state'] == 'queued'
    assert store.targets()[0][2]['next_due'] == 0
    store.set_monitor_enabled('one', False)
    assert client.get('/api/admin/activity').json['monitors']['one']['state'] == 'paused'
    assert client.post('/api/admin/monitors/one/check', json={}, headers=headers).status_code == 404


def test_status_keeps_last_completed_result_while_a_check_runs(settings, store):
    Worker(settings, store, FakeRunner(["mismatch"] * 3), FakeAdapter()).check(*run_monitor(store))
    completed = store.history("one")[0]
    monitor, _ = run_monitor(store)
    running = store.create_run(monitor, "scheduled", "test-method-v1", {})
    running.update(planned_samples=3, attempts=[{"outcome": "responded"}])
    store.save_run(running)
    store.set_meta("heartbeat", {"at": time.time(), "active": ["one"]})
    store.set_meta("checker", {"version": "test-method-v1"})
    m = snapshot(store, settings)["monitors"][0]
    assert m["latest"]["id"] == running["id"]
    assert m["latest_completed"]["id"] == completed["id"]
    assert m["latest_completed"]["identity"] == "mismatch_signal"
    assert m["active"] == {"id": running["id"], "kind": "scheduled", "started_at": running["started_at"],
                           "completed_samples": 1, "planned_samples": 3}
    assert not m["configuration_changed"]
    store.set_meta("heartbeat", {"at": time.time() - 100})
    assert snapshot(store, settings)["monitors"][0]["active"] is None


def test_history_buckets_carry_public_tooltip_summaries(settings, store):
    from modeltrace_status.diagnostics import diagnose
    class ErrorRunner:
        version = "fixture"
        def run(self, *_):
            return diagnose("connection refused secret-sentinel") | {"duration_ms": 5}
    Worker(settings, store, ErrorRunner(), FakeAdapter()).check(*run_monitor(store))
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    response = create_app(settings).test_client().get("/api/status")
    bucket = response.json["monitors"][0]["history"][-1]
    summary = bucket["summary"]
    assert summary["id"] == bucket["latest_id"]
    assert summary["identity"] == "consistent" and summary["top"] == {"model": "gpt-6-astra", "weight": .9}
    assert summary["valid_samples"] == 3 and summary["failure"] is None
    failed = next(r for r in store.history("one") if r["availability"] == "unavailable")
    from modeltrace_status.web import run_summary
    assert run_summary(failed)["failure"] == "API connection refused"
    assert b"secret-sentinel" not in response.data and b'"text"' not in response.data


def test_provider_pause_endpoint_preserves_configuration(settings, store):
    client = create_app(settings).test_client()
    provider = store.admin_providers()[0]
    url = "/api/admin/providers/" + provider["id"]
    assert client.patch(url, json={"enabled": False}).status_code == 403
    headers = login(client, settings)
    before = next((m, key) for m, key, _ in store.targets(enabled=False))
    assert client.patch(url, json={"enabled": "no"}, headers=headers).status_code == 400
    assert client.patch(url, json={"enabled": False, "name": "x"}, headers=headers).status_code == 400
    assert client.patch(url, json={"enabled": False}, headers=headers).json == {"id": provider["id"], "enabled": False}
    assert store.targets() == []
    assert next((m, key) for m, key, _ in store.targets(enabled=False)) == before
    assert client.get("/api/admin/activity").json["monitors"]["one"]["state"] == "paused"
    assert client.patch(url, json={"enabled": True}, headers=headers).status_code == 200
    assert len(store.targets()) == 1
    assert client.patch("/api/admin/providers/missing", json={"enabled": True}, headers=headers).status_code == 404


def test_activity_reports_latest_identity_and_schedule(settings, store):
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    client = create_app(settings).test_client()
    login(client, settings)
    store.due_at("one", 12345.0)
    state = client.get("/api/admin/activity").json["monitors"]["one"]
    assert state["identity"] == "consistent"
    assert state["identity_run_id"] == store.history("one")[0]["id"]
    assert state["next_due"] == 12345.0


def test_unexpected_errors_return_json(settings, store):
    app = create_app(settings)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.add_url_rule("/api/boom", "boom", lambda: 1 / 0)
    response = app.test_client().get("/api/boom")
    assert response.status_code == 500
    assert response.json == {"error": "Internal Server Error"}


def test_pages_only_reference_served_assets(settings, store):
    import re
    from pathlib import Path
    client = create_app(settings).test_client()
    web = Path(__file__).resolve().parents[1] / "src/modeltrace_status/web"
    types = {".js": "javascript", ".css": "text/css", ".svg": "image/svg+xml"}
    referenced = set()
    for page in ("/", "/manage"):
        body = client.get(page).get_data(as_text=True)
        referenced |= set(re.findall(r'/assets/([\w.-]+)', body))
    for script in web.glob("*.js"):
        referenced |= set(re.findall(r"from '\./([\w.-]+)'", script.read_text()))
    assert {"status.js", "manage.js", "common.js", "style.css", "icon.svg"} <= referenced
    for name in referenced:
        response = client.get("/assets/" + name)
        assert response.status_code == 200, name
        assert types[Path(name).suffix] in response.content_type
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/assets/app.js").status_code == 404
    assert client.get("/assets/../web.py").status_code == 404


def test_short_response_is_replaced_by_a_fresh_probe(settings, store):
    runner = FakeRunner(["short", "match", "match", "match"])
    Worker(settings, store, runner, FakeAdapter()).check(*run_monitor(store))
    run = store.history("one")[0]
    assert runner.count == 4
    assert run["identity"] == "consistent" and run["valid_samples"] == 3
    assert run["availability"] == "available"
    assert [a.get("replaces") for a in run["attempts"]] == [None, None, None, 0]
    assert run["planned_samples"] == 3 and run["planned_probes"] == 4
    assert run["provenance"]["sample_retries"] == 1


def test_sample_replacements_are_bounded_by_setting_and_budget(settings, store):
    runner = FakeRunner(["short", "short", "match", "match"])
    Worker(settings, store, runner, FakeAdapter()).check(*run_monitor(store))
    assert runner.count == 4  # only one replacement per batch
    assert store.history("one")[0]["identity"] == "inconclusive"
    runner = FakeRunner(["short", "match", "match"])
    Worker(replace(settings, sample_retries=0), store, runner, FakeAdapter()).check(*run_monitor(store))
    assert runner.count == 3
    budget = replace(settings, daily_budget=store_attempts(store) + 3)
    runner = FakeRunner(["short", "match", "match"])
    Worker(budget, store, runner, FakeAdapter()).check(*run_monitor(store))
    latest = store.history("one")[0]
    assert runner.count == 3 and latest["state"] == "completed" and latest["planned_probes"] == 3
    assert latest["identity"] == "inconclusive"


def store_attempts(store):
    with store.connect() as db:
        return db.execute("SELECT count(*) FROM attempts").fetchone()[0]


def test_failed_requests_are_not_replaced(settings, store):
    runner = FakeRunner(["timeout", "match", "match"])
    Worker(settings, store, runner, FakeAdapter()).check(*run_monitor(store))
    assert runner.count == 3
    assert store.history("one")[0]["availability"] == "partial"


def test_identity_score_averages_expected_weight_over_scheduled_checks(settings, store):
    worker = Worker(settings, store, FakeRunner(["match"] * 3 + ["mismatch"] * 3 + ["match", "short", "short"]), FakeAdapter())
    for _ in range(3):
        worker.check(*run_monitor(store))
    score = snapshot(store, settings)["monitors"][0]["score"]
    # A check with a single valid sample still counts.
    assert score["checks"] == 3 and abs(score["value"] - (.9 + .05 + .9) / 3) < 1e-9


def test_editing_a_checked_model_does_not_queue_a_recheck(settings, store):
    Worker(settings, store, FakeRunner(["match"] * 3), FakeAdapter()).check(*run_monitor(store))
    provider = store.admin_providers()[0]
    edited = [m | {"effort": "high"} for m in provider["monitors"]]
    store.save_provider(provider | {"monitors": edited + [{"model": "gpt-6-sol"}]}, provider["id"])
    due = {m.id: runtime["next_due"] for m, _, runtime in store.targets()}
    assert due.pop("one") > time.time() + 60
    assert list(due.values()) == [0]  # the new model still gets its first check
