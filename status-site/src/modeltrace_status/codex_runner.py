from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .diagnostics import diagnose

# Keep inference in Codex, but make numeric probes tool-free and independent of
# personal plugins, skills, sessions and auth. Reject any unexpected tool event.
DISABLED = ("shell_tool", "unified_exec", "shell_snapshot", "multi_agent", "multi_agent_v2", "apps",
            "plugins", "hooks", "browser_use", "computer_use", "image_generation", "view_image",
            "goals", "memories", "skill_search", "sleep_tool", "code_mode_host", "unbounded_connection_retries")


def categorize(message):
    value = message.lower()
    if re.search(r"\b(401|403)\b|unauthorized|invalid.api.key|authentication|forbidden", value):
        return "auth_error"
    if re.search(r"\b429\b|rate.limit|quota|insufficient.credit", value):
        return "rate_limit"
    if re.search(r"\b(500|502|503|504)\b|stream disconnected|connection refused|error sending request|failed to connect", value):
        return "provider_error"
    return "runner_error"


class CodexRunner:
    def __init__(self, settings):
        self.settings = settings
        version = subprocess.run([settings.codex, "--version"], capture_output=True, text=True, timeout=15)
        if version.returncode:
            raise ValueError("Codex executable is unavailable")
        self.version = version.stdout.strip()

    def run(self, monitor, credential, prompt):
        started = time.monotonic()
        runtime = self.settings.data_dir / "probes"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = {"outcome": "runner_error", "text": "", "usage": {}, "ttft_ms": None, "tbt_ms": None}
        with tempfile.TemporaryDirectory(prefix="probe-", dir=runtime) as directory:
            task_dir = Path(directory)
            codex_home = task_dir / "codex"
            work_dir = task_dir / "work"
            codex_home.mkdir(mode=0o700)
            work_dir.mkdir(mode=0o700)
            # This child uses CODEX_HOME for its documented configuration purpose.
            # No parent environment or user's existing Codex home is modified.
            env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
            env.update({"CODEX_HOME": str(codex_home), "MODELTRACE_PROBE_API_KEY": credential})
            config = {
                "model_provider": "modeltrace_monitor", "model": monitor.model,
                "model_reasoning_effort": monitor.effort, "approval_policy": "never", "web_search": "disabled",
                "model_providers.modeltrace_monitor.name": "ModelTrace monitor",
                "model_providers.modeltrace_monitor.base_url": monitor.base_url,
                "model_providers.modeltrace_monitor.env_key": "MODELTRACE_PROBE_API_KEY",
                "model_providers.modeltrace_monitor.wire_api": "responses",
                "model_providers.modeltrace_monitor.request_max_retries": 0,
                "model_providers.modeltrace_monitor.stream_max_retries": 0,
                "model_providers.modeltrace_monitor.stream_idle_timeout_ms": self.settings.timeout * 1000,
                "features.skip_host_skill_discovery": True, "suppress_unstable_features_warning": True,
                "features.code_mode": False, "features.code_mode_only": False,
            }
            command = [self.settings.codex, "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never", "-C", str(work_dir)]
            for key, value in config.items():
                command.extend(["-c", f"{key}={json.dumps(value)}"])
            for feature in DISABLED:
                command.extend(["--disable", feature])
            command.append("-")
            try:
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=env, cwd=work_dir, start_new_session=True)
            except OSError:
                return result | diagnose(outcome="runner_error") | {"duration_ms": round((time.monotonic() - started) * 1000)}
            messages, errors, pending = [], [], b""
            stderr = bytearray()
            total_bytes = 0
            finished = False
            failed = False
            stop = False
            def kill():
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            def event(line):
                nonlocal finished, failed, stop
                try:
                    payload = json.loads(line)
                except (ValueError, UnicodeError):
                    result["outcome"] = "runner_error"
                    stop = True
                    return
                kind = payload.get("type")
                item = payload.get("item", {})
                if kind in ("item.started", "item.completed") and item.get("type") not in ("agent_message", "reasoning", "error"):
                    result["outcome"] = "tool_use"
                    result["unexpected_item_type"] = re.sub(r"[^a-zA-Z_]", "", str(item.get("type")))[:50]
                    stop = True
                if kind == "item.completed" and item.get("type") == "agent_message":
                    messages.append(item.get("text", ""))
                if item.get("type") == "error":
                    errors.append(str(item.get("message", "")))
                if kind == "turn.completed":
                    finished = True
                    usage = payload.get("usage", {})
                    result["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens") if isinstance(usage.get(k), int) and usage[k] >= 0}
                if kind in ("error", "turn.failed"):
                    errors.append(str(payload.get("message") or payload.get("error") or ""))
                    if kind == "turn.failed":
                        failed = True
            try:
                child.stdin.write(prompt.encode())
                child.stdin.close()
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ, "out")
                    selector.register(child.stderr, selectors.EVENT_READ, "err")
                    while selector.get_map() and not stop:
                        if time.monotonic() - started >= self.settings.timeout:
                            result["outcome"] = "timeout"
                            stop = True
                            break
                        for key, _ in selector.select(.25):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            total_bytes += len(data)
                            if total_bytes > 4_000_000:
                                stop = True
                                result["outcome"] = "output_limit"
                                break
                            if key.data == "err":
                                if len(stderr) < 100_000:
                                    stderr.extend(data[:100_000 - len(stderr)])
                            else:
                                pending += data
                                while b"\n" in pending:
                                    line, pending = pending.split(b"\n", 1)
                                    if line.strip():
                                        event(line)
                    if pending.strip() and not stop:
                        event(pending)
                if stop:
                    kill()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill()
                    child.wait()
                if not stop:
                    if finished and not failed and child.returncode == 0 and messages:
                        result.update(outcome="responded", text=messages[-1])
                    else:
                        # Turn errors take precedence over unrelated CLI warnings.
                        # Translate recognized codes into fixed, public-safe text.
                        diagnostic = errors[-1] if errors else stderr.decode(errors="replace")
                        result["outcome"] = categorize(diagnostic)
                        status = re.search(r"(?:status(?: code)?|HTTP)\s*[:=]?\s*([45]\d{2})\b", diagnostic, re.I)
                        if status:
                            result["http_status"] = int(status[1])
                        result.update(diagnose(diagnostic, result["outcome"], result.get("http_status")))
            finally:
                if child.poll() is None:
                    kill()
                    child.wait()
                child.stdout.close()
                child.stderr.close()
            result["duration_ms"] = round((time.monotonic() - started) * 1000)
            if result["outcome"] != "responded" and not result.get("diagnostic"):
                result.update(diagnose(outcome=result["outcome"]))
            return result
