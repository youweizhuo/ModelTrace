from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import Counter, defaultdict
from statistics import median
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from .config import load_settings
from .storage import Store
from .upstream_adapter import method_version
from .worker import FAILURES

WINDOWS = {"24h": (86400, 24), "7d": (604800, 28), "30d": (2592000, 30)}
ASSETS = ("common.js", "status.js", "manage.js", "style.css", "icon.svg")


def worker_online(heartbeat, now):
    return bool(heartbeat) and now - heartbeat.get("at", 0) < 45 and not heartbeat.get("stopped", False)


def run_method(run):
    return method_version(run.get("provenance", {})) or run["checker_version"]


def finished(runs):
    return next((run for run in runs if run["state"] != "running"), None)


def run_speed(run):
    """Median TTFT and decode rate over a check's responded probes (None before timing was recorded)."""
    values = lambda key: [a[key] for a in (run or {}).get("attempts", []) if a["outcome"] == "responded" and a.get(key) is not None]
    ttft, tps = values("ttft_ms"), values("output_tps")
    return {"ttft_ms": round(median(ttft)) if ttft else None, "output_tps": round(median(tps), 1) if tps else None}


def assessed(run):
    """Scheduled checks the scorer weighed, even on fewer than three samples; stored
    confirmation re-runs would double-count mismatches."""
    return run["kind"] == "scheduled" and run["state"] == "completed" and run.get("expected_weight") is not None


def speed_tone(value, typical, higher_is_better=False):
    if value is None or typical is None:
        return None
    ratio = typical / value if higher_is_better else value / typical
    return "good" if ratio <= 1.25 else "warn" if ratio <= 2 else "bad"


def run_summary(run):
    """Everything a history tooltip needs, so hovering never waits on the network."""
    top = next(iter(run.get("candidates") or []), None)
    failure = next((a for a in reversed(run["attempts"]) if a["outcome"] != "responded"), None)
    status = next((a["http_status"] for a in reversed(run["attempts"]) if a.get("http_status")), None)
    return {"id": run["id"], "state": run["state"], "kind": run["kind"], "identity": run["identity"],
            "availability": run["availability"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "top": {"model": top["model"], "weight": top["weight"]} if top else None,
            "expected_weight": run.get("expected_weight"), "valid_samples": run.get("valid_samples", 0),
            "planned_samples": run.get("planned_samples", 3), "http_status": status, **run_speed(run),
            "failure": (failure.get("diagnostic") or {}).get("title") if failure else None}


def management_activity(store, now=None):
    now = time.time() if now is None else now
    heartbeat = store.meta("heartbeat", {})
    online = worker_online(heartbeat, now)
    active = set(heartbeat.get("active", [])) if online else set()
    monitors = {}
    for monitor, _, runtime in store.targets(enabled=False):
        recent = store.history(monitor.id, limit=2)
        run = next(iter(recent), None)
        done = finished(recent)
        in_progress = bool(run and run["state"] == "running")
        running = online and (monitor.id in active or in_progress)
        state = ("running" if running else "paused" if not runtime["enabled"]
                 else "queued" if runtime["next_due"] <= now or in_progress else "idle")
        monitors[monitor.id] = {
            "state": state, "enabled": runtime["enabled"],
            "completed_samples": len(run["attempts"]) if run else 0,
            "planned_samples": run.get("planned_probes", run.get("planned_samples", 3)) if run else 3,
            "run_id": run["id"] if run else None, "kind": run["kind"] if run else None,
            "run_state": run["state"] if run else None,
            "availability": run["availability"] if run else None,
            "finished_at": run["finished_at"] if run else None,
            "next_due": runtime["next_due"],
            "identity": done["identity"] if done else None,
            "identity_run_id": done["id"] if done else None,
        }
    return {"at": now, "worker_online": bool(online), "monitors": monitors}


def secret_file(path, generate):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = generate()
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")
    return value


def snapshot(store, settings, window="24h", now=None):
    now = now or time.time()
    seconds, count = WINDOWS[window]
    since = now - seconds
    heartbeat = store.meta("heartbeat", {})
    online = worker_online(heartbeat, now)
    checker = store.meta("checker", {})
    monitors = []
    # Speed is compared with every check in the window that asked for the same model at the same effort.
    peers = defaultdict(lambda: {"ttft_ms": [], "output_tps": []})
    for monitor, _, runtime in store.targets(enabled=False):
        rows = store.history(monitor.id, since=since, limit=10000)
        latest = rows[0] if rows else next(iter(store.history(monitor.id, limit=1)), None)
        # A check in progress must not hide the last completed assessment.
        done = finished(rows) or finished(store.history(monitor.id, limit=2))
        active = latest if online and latest and latest["state"] == "running" else None
        stale = bool(latest and (not online or now - latest["started_at"] > monitor.interval * 1.5 + settings.timeout))
        basis = done or latest
        changed = bool(basis and (not monitor.same_basis(basis["monitor"]) or run_method(basis) != checker.get("version")))
        assessments = Counter()
        outcomes = Counter()
        scheduled = 0
        weights = []
        buckets = [{"start": since + i * seconds / count, "end": since + (i + 1) * seconds / count,
                    "counts": {}, "runs": 0, "scheduled": 0, "latest_id": None,
                    "latest_identity": None, "latest_availability": None, "summary": None, "versions": []} for i in range(count)]
        for run in reversed(rows):
            if run["state"] == "running":
                continue
            identity = run["identity"]
            assessments[identity] += 1
            outcomes.update(a["outcome"] for a in run["attempts"])
            scheduled += run["kind"] == "scheduled"
            if assessed(run):
                weights.append(run["expected_weight"])
            i = min(count - 1, max(0, int((run["started_at"] - since) / seconds * count)))
            bucket = buckets[i]
            bucket["counts"][identity] = bucket["counts"].get(identity, 0) + 1
            bucket["runs"] += 1
            bucket["scheduled"] += run["kind"] == "scheduled"
            bucket["latest_id"] = run["id"]
            bucket["latest_identity"] = identity
            bucket["latest_availability"] = run["availability"]
            bucket["summary"] = run_summary(run)
            for key, value in run_speed(run).items():
                if value is not None:
                    peers[(monitor.expected_model, monitor.effort)][key].append(value)
            if run_method(run) not in bucket["versions"]:
                bucket["versions"].append(run_method(run))
        good = outcomes["responded"]
        failed = sum(outcomes[key] for key in FAILURES)
        monitors.append(monitor.public() | {
            "enabled": runtime["enabled"], "next_due": runtime["next_due"], "latest": latest,
            "latest_completed": done,
            "active": {"id": active["id"], "kind": active["kind"], "started_at": active["started_at"],
                       "completed_samples": len(active["attempts"]),
                       "planned_samples": active.get("planned_probes", active.get("planned_samples", 3))} if active else None,
            "stale": stale, "configuration_changed": changed, "history": buckets,
            "score": {"value": sum(weights) / len(weights) if weights else None, "checks": len(weights)},
            "metrics": {"responded": good, "failed": failed, "unknown": sum(outcomes.values()) - good - failed,
                        "outcomes": dict(outcomes), "assessments": dict(assessments), "scheduled_checks": scheduled,
                        "success_rate": good / (good + failed) if good + failed else None},
        })
    for m in monitors:
        speed = run_speed(m["latest_completed"])
        group = peers[(m["expected_model"], m["effort"])]
        typical = {key: median(group[key]) if group[key] else None for key in speed}
        m["speed"] = speed | {"typical": typical, "checks": len(group["ttft_ms"]),
                              "ttft_tone": speed_tone(speed["ttft_ms"], typical["ttft_ms"]),
                              "tps_tone": speed_tone(speed["output_tps"], typical["output_tps"], higher_is_better=True)}
    return {"at": now, "window": window, "worker_online": online, "heartbeat": heartbeat, "checker": checker,
            "monitors": monitors, "settings": {"daily_budget": settings.daily_budget}}


def create_app(settings=None):
    settings = settings or load_settings()
    store = Store(settings.db_path)
    web_dir = Path(__file__).with_name("web")
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=65536, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=28800)
    app.secret_key = secret_file(settings.data_dir / "session-secret", lambda: secrets.token_hex(32))
    password = secret_file(settings.data_dir / "admin-password", lambda: secrets.token_urlsafe(20))
    if not store.meta("admin_hash"):
        store.set_meta("admin_hash", generate_password_hash(password))
    failures = {}
    failure_lock = threading.Lock()

    @app.before_request
    def protect_management():
        if request.path.startswith("/api/admin/"):
            if request.method != "GET":
                origin = request.headers.get("Origin")
                if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                    abort(403)
                supplied = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf", "")
                if not expected or not secrets.compare_digest(supplied, expected):
                    abort(403)
            if request.path not in ("/api/admin/session", "/api/admin/login") and not session.get("admin"):
                abort(401)

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def http_error(error):
        # A drawer link whose '#' was percent-encoded (/%23monitor=…) arrives as a path.
        if error.code == 404 and request.method == "GET" and request.path.startswith("/#"):
            return redirect(request.path)
        return jsonify(error=error.name), error.code

    @app.errorhandler(500)
    def server_error(error):
        # Keep the API contract (JSON) even for unexpected failures.
        return jsonify(error="Internal Server Error"), 500

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.get("/manage")
    def manage():
        return send_from_directory(web_dir, "manage.html")

    @app.get("/assets/<name>")
    def asset(name):
        if name not in ASSETS:
            abort(404)
        return send_from_directory(web_dir, name)

    @app.get("/api/health")
    def health():
        return jsonify(web="ok", worker_online=worker_online(store.meta("heartbeat", {}), time.time()))

    @app.get("/api/status")
    def status():
        window = request.args.get("window", "24h")
        if window not in WINDOWS:
            abort(400)
        return jsonify(snapshot(store, settings, window))

    @app.get("/api/runs/<rid>")
    def get_run(rid):
        run = store.run(rid)
        if not run:
            abort(404)
        return jsonify(run)

    @app.get("/api/monitors/<mid>/history")
    def get_history(mid):
        try:
            before = float(request.args["before"]) if "before" in request.args else None
        except ValueError:
            abort(400)
        rows = store.history(mid, limit=50, before=before)
        return jsonify(runs=[run | {"speed": run_speed(run)} for run in rows], next_before=rows[-1]["started_at"] if len(rows) == 50 else None)

    @app.get("/api/admin/session")
    def admin_session():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return jsonify(authenticated=bool(session.get("admin")), csrf=session["csrf"])

    @app.post("/api/admin/login")
    def login():
        ip = request.remote_addr
        now = time.time()
        with failure_lock:
            recent = [t for t in failures.get(ip, []) if now - t < 600]
            if len(recent) >= 5:
                abort(429)
            failures[ip] = recent + [now]
        payload = request.get_json(silent=True) or {}
        supplied = str(payload.get("password", ""))[:512]
        if not check_password_hash(store.meta("admin_hash"), supplied):
            abort(401)
        with failure_lock:
            failures.pop(ip, None)
        session.clear()
        session.permanent = True
        session.update(admin=True, csrf=secrets.token_urlsafe(24))
        return jsonify(authenticated=True, csrf=session["csrf"])

    @app.post("/api/admin/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/admin/providers")
    def providers():
        return jsonify(providers=store.admin_providers(), models=store.meta("checker", {}).get("models", []),
                       activity=management_activity(store))

    @app.get("/api/admin/activity")
    def activity():
        return jsonify(management_activity(store))

    @app.post("/api/admin/providers")
    @app.put("/api/admin/providers/<pid>")
    def save_provider(pid=None):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400)
        try:
            saved = store.save_provider(payload, pid)
        except (ValueError, TypeError, AttributeError) as error:
            return jsonify(error=str(error) if isinstance(error, ValueError) else "Invalid provider configuration"), 400
        return jsonify(id=saved), 200 if pid else 201

    @app.patch("/api/admin/providers/<pid>")
    def update_provider(pid):
        # Pausing must not resend (and possibly revert) the provider's full configuration.
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
            return jsonify(error="Provide enabled as true or false"), 400
        if not store.set_provider_enabled(pid, payload["enabled"]):
            abort(404)
        return jsonify(id=pid, enabled=payload["enabled"])

    @app.delete("/api/admin/providers/<pid>")
    def delete_provider(pid):
        store.delete_provider(pid)
        return jsonify(ok=True)

    @app.post("/api/admin/monitors/<mid>/check")
    def check_now(mid):
        heartbeat = store.meta("heartbeat", {})
        online = worker_online(heartbeat, time.time())
        state = store.queue_check(mid, worker_online=online, active=mid in heartbeat.get("active", []))
        if state is None:
            abort(404)
        return jsonify(queued=state == "queued", state=state, activity=management_activity(store))

    @app.patch("/api/admin/monitors/<mid>")
    def update_monitor(mid):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
            return jsonify(error="Provide enabled as true or false"), 400
        if not store.set_monitor_enabled(mid, payload["enabled"]):
            abort(404)
        return jsonify(id=mid, enabled=payload["enabled"])

    return app
