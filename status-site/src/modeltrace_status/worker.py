from __future__ import annotations

import fcntl
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .codex_runner import CodexRunner
from .storage import Store
from .upstream_adapter import UpstreamAdapter

log = logging.getLogger(__name__)
FAILURES = {"provider_error", "auth_error", "rate_limit", "timeout"}


def endpoint_group(base_url):
    url = urlsplit(base_url)
    return (url.scheme.lower(), url.hostname, url.port or (443 if url.scheme == "https" else 80))


def availability(attempts):
    outcomes = [a["outcome"] for a in attempts]
    good = outcomes.count("responded")
    bad = sum(o in FAILURES for o in outcomes)
    if good and not bad:
        return "available"
    if good:
        return "partial"
    return "unavailable" if bad else "unknown"


class Worker:
    def __init__(self, settings, store=None, runner=None, adapter=None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.stop = threading.Event()
        self.runner = runner or CodexRunner(settings)
        self.adapter_error = None
        try:
            self.adapter = adapter or UpstreamAdapter(settings.upstream)
            self.adapter.plan()  # Validate upstream boundary before spending requests.
        except Exception as error:
            self.adapter = None
            self.adapter_error = type(error).__name__
        self.store.set_meta("checker", {"ready": self.adapter is not None, "version": self.adapter.version if self.adapter else None,
                                      "provenance": self.adapter.provenance if self.adapter else {}, "models": sorted(self.adapter.models) if self.adapter else [],
                                      "codex_version": self.runner.version, "error": self.adapter_error})

    def current(self, monitor):
        return any(m.id == monitor.id and m.same_basis(monitor.public()) for m, _, _ in self.store.targets())

    def batch(self, monitor, credential, kind="scheduled"):
        adapter = self.adapter
        provenance = (adapter.provenance if adapter else {}) | {"codex_version": self.runner.version,
                                                                 "sample_retries": self.settings.sample_retries}
        run = self.store.create_run(monitor, kind, adapter.version if adapter else "unavailable", provenance)
        evidence, outputs = [], []
        try:
            if not adapter:
                run.update(state="checker_error", reason="upstream_incompatible")
                return run
            plan = adapter.plan()
            run["planned_samples"] = run["planned_probes"] = len(plan)
            queue = [(challenge, None) for challenge in plan]
            retries = self.settings.sample_retries
            while queue:
                challenge, replaces = queue.pop(0)
                if self.stop.is_set() or not self.current(monitor):
                    run.update(state="interrupted", reason="monitor_changed_or_paused")
                    break
                attempt_id = self.store.reserve_attempt(monitor.id, run["id"], self.settings.daily_budget)
                if not attempt_id:
                    if replaces is not None:
                        # Assess the planned samples rather than discarding them.
                        run["planned_probes"] -= 1
                        break
                    run.update(state="budget_exhausted", reason="daily_attempt_limit")
                    break
                started_at = time.time()
                try:
                    outcome = self.runner.run(monitor, credential, challenge["prompt"])
                except Exception:
                    outcome = {"outcome": "runner_error", "text": "", "usage": {}}
                self.store.finish_attempt(attempt_id, outcome["outcome"])
                attempt = {k: outcome.get(k) for k in ("outcome", "duration_ms", "usage", "ttft_ms", "output_tps", "http_status", "diagnostic")}
                attempt.update(started_at=started_at, finished_at=time.time())
                if replaces is not None:
                    attempt["replaces"] = replaces
                run["attempts"].append(attempt)
                evidence.append({"challenge": challenge, "text": outcome.get("text", ""), "outcome": outcome["outcome"]})
                if outcome["outcome"] == "responded":
                    output = {"text": outcome["text"], "expected_count": challenge["expected_count"]}
                    # Every response is still scored, so rejected samples stay visible in diagnostics.
                    outputs.append(output)
                    if retries and not adapter.sample_valid(output):
                        # A short or truncated answer says nothing about availability;
                        # replace it with a fresh challenge so the batch can still reach three samples.
                        retries -= 1
                        run["planned_probes"] += 1
                        queue.append((adapter.plan()[0], len(run["attempts"]) - 1))
                self.store.save_run(run, evidence)
            run.update(adapter.assess(outputs, monitor.expected_model))
            run["availability"] = availability(run["attempts"])
            if run["state"] == "running":
                run["state"] = "completed"
            else:
                run["identity"] = "unknown"
        except Exception as error:
            run.update(state="checker_error", identity="unknown", reason="upstream_incompatible")
            log.error("Check failed for %s (%s)", monitor.id, type(error).__name__)
        finally:
            run["finished_at"] = time.time()
            self.store.save_run(run, evidence)
            log.info("%s %s: identity=%s availability=%s", monitor.id, kind, run["identity"], run["availability"])
        return run

    def check(self, monitor, credential):
        run = self.batch(monitor, credential)
        if run["state"] == "budget_exhausted":
            now = time.time()
            self.store.due_at(monitor.id, now - now % 86400 + 86400 + 1)

    def serve(self, once=False):
        lock_path = self.settings.data_dir / "worker.lock"
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("A status worker is already running") from None
            self.store.recover()
            self.store.prune(self.settings.retention_days, self.settings.evidence_days)
            pending = {}
            seen = set()
            last_prune = time.time()
            with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
                while not self.stop.is_set():
                    now = time.time()
                    self.store.set_meta("heartbeat", {"at": now, "active": list(pending), "checker_ready": self.adapter is not None})
                    for mid, (future, _) in list(pending.items()):
                        if future.done():
                            del pending[mid]
                            try:
                                future.result()
                            except Exception as error:
                                log.error("Worker task failed for %s (%s)", mid, type(error).__name__)
                    # Providers and models sharing an API server share capacity.
                    # Serialize their entire batches.
                    # Oldest due first prevents a slow monitor starving others.
                    targets = sorted(self.store.targets(), key=lambda t: t[2]["next_due"])
                    busy_endpoints = {endpoint for _, endpoint in pending.values()}
                    for monitor, credential, runtime in targets:
                        if len(pending) >= self.settings.concurrency:
                            break
                        if monitor.id in pending or once and monitor.id in seen or not once and runtime["next_due"] > now:
                            continue
                        endpoint = endpoint_group(monitor.base_url)
                        if endpoint in busy_endpoints:
                            continue
                        self.store.due_at(monitor.id, now + monitor.interval)
                        pending[monitor.id] = (pool.submit(self.check, monitor, credential), endpoint)
                        busy_endpoints.add(endpoint)
                        seen.add(monitor.id)
                    if once and not pending and all(m.id in seen for m, _, _ in targets):
                        break
                    if now - last_prune > 3600:
                        self.store.prune(self.settings.retention_days, self.settings.evidence_days)
                        last_prune = now
                    self.stop.wait(1 if once else 3)
            self.store.set_meta("heartbeat", {"at": time.time(), "active": [], "stopped": True})
