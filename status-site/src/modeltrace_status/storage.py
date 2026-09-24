from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace

from .config import Monitor
from .diagnostics import with_explanations

# Keep connection open/close and short transactions serialized inside each
# process. This avoids the concurrent connection open/close hang observed while
# testing this host's runtime. Inference and HTTP rendering never hold this lock;
# SQLite WAL and BEGIN IMMEDIATE still coordinate the web and worker processes.
_DB_LOCK = threading.RLock()


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS providers(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    credential TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS monitors(
                    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers(id),
                    model TEXT NOT NULL, expected_model TEXT NOT NULL, channel TEXT NOT NULL,
                    effort TEXT NOT NULL, interval INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    next_due REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, kind TEXT NOT NULL,
                    parent_id TEXT, scheduled_at REAL NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, state TEXT NOT NULL, revision TEXT NOT NULL,
                    checker_version TEXT NOT NULL, public_json TEXT NOT NULL, evidence_json TEXT);
                CREATE INDEX IF NOT EXISTS runs_monitor_time ON runs(monitor_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, monitor_id TEXT NOT NULL,
                    started_at REAL NOT NULL, outcome TEXT NOT NULL DEFAULT 'running');
                CREATE INDEX IF NOT EXISTS attempts_budget ON attempts(monitor_id, started_at);
            ''')
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        with _DB_LOCK:
            db = sqlite3.connect(self.path, timeout=20)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=20000")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def seed(self, settings, credentials):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
                return
            providers = {}
            for m in settings.monitors:
                group = (m.provider, m.base_url, m.key_env)
                if group not in providers:
                    pid = uuid.uuid4().hex
                    providers[group] = pid
                    db.execute("INSERT INTO providers(id,name,base_url,credential) VALUES(?,?,?,?)", (pid, m.provider, m.base_url, credentials[m.id]))
                db.execute("INSERT INTO monitors(id,provider_id,model,expected_model,channel,effort,interval) VALUES(?,?,?,?,?,?,?)",
                           (m.id, providers[group], m.model, m.expected_model, m.channel, m.effort, m.interval))
            db.execute("INSERT INTO meta VALUES('seeded','true')")

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def targets(self, enabled=True):
        sql = "SELECT m.*,p.name,p.base_url,p.credential,p.revision AS provider_revision,p.enabled AS provider_enabled FROM monitors m JOIN providers p ON m.provider_id=p.id"
        if enabled:
            sql += " WHERE m.enabled=1 AND p.enabled=1"
        sql += " ORDER BY p.name,m.model,m.id"
        with self.connect() as db:
            rows = db.execute(sql).fetchall()
        return [(Monitor(id=r["id"], provider=r["name"], model=r["model"], expected_model=r["expected_model"],
                         base_url=r["base_url"], key_env=f"STORED_KEY_{r['provider_revision']}", channel=r["channel"], effort=r["effort"], interval=r["interval"]),
                 r["credential"], {"enabled": bool(r["enabled"] and r["provider_enabled"]), "next_due": r["next_due"], "provider_id": r["provider_id"]}) for r in rows]

    def due_at(self, monitor_id, when):
        with self.connect() as db:
            db.execute("UPDATE monitors SET next_due=? WHERE id=?", (when, monitor_id))

    def queue_check(self, monitor_id, worker_online=False, active=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT m.id FROM monitors m JOIN providers p ON m.provider_id=p.id
                WHERE m.id=? AND m.enabled=1 AND p.enabled=1""", (monitor_id,)).fetchone()
            if not row:
                return None
            running = worker_online and (active or db.execute(
                "SELECT 1 FROM runs WHERE monitor_id=? AND state='running' LIMIT 1", (monitor_id,)).fetchone())
            if running:
                return "running"
            db.execute("UPDATE monitors SET next_due=0 WHERE id=?", (monitor_id,))
            return "queued"

    def set_monitor_enabled(self, monitor_id, enabled):
        with self.connect() as db:
            result = db.execute("""UPDATE monitors
                SET next_due=CASE WHEN enabled=0 AND ?=1 THEN 0 ELSE next_due END,
                    enabled=? WHERE id=?""", (int(enabled), int(enabled), monitor_id))
            return bool(result.rowcount)

    def set_provider_enabled(self, provider_id, enabled):
        with self.connect() as db:
            result = db.execute("UPDATE providers SET enabled=? WHERE id=?", (int(enabled), provider_id))
            return bool(result.rowcount)

    def reserve_attempt(self, monitor_id, run_id, budget, now=None):
        now = now or time.time()
        day = now - now % 86400
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM attempts WHERE monitor_id=? AND started_at>=?", (monitor_id, day)).fetchone()[0]
            if count >= budget:
                return None
            aid = uuid.uuid4().hex
            db.execute("INSERT INTO attempts(id,run_id,monitor_id,started_at) VALUES(?,?,?,?)", (aid, run_id, monitor_id, now))
            return aid

    def finish_attempt(self, attempt, outcome):
        with self.connect() as db:
            db.execute("UPDATE attempts SET outcome=? WHERE id=?", (outcome, attempt))

    def create_run(self, monitor, kind, checker, provenance, parent=None):
        now = time.time()
        rid = uuid.uuid4().hex
        data = {"id": rid, "monitor_id": monitor.id, "monitor": monitor.public(), "kind": kind, "parent_id": parent,
                "started_at": now, "finished_at": None, "state": "running", "identity": "unknown", "availability": "unknown",
                "candidates": [], "valid_samples": 0, "attempts": [], "provenance": provenance, "checker_version": checker}
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (rid, monitor.id, kind, parent, now, now, None, "running", monitor.revision, checker, json.dumps(data), None))
        return data

    def save_run(self, data, evidence=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET state=?,finished_at=?,public_json=?,evidence_json=COALESCE(?,evidence_json) WHERE id=?",
                       (data["state"], data["finished_at"], json.dumps(data), json.dumps(evidence) if evidence is not None else None, data["id"]))

    def run(self, rid):
        with self.connect() as db:
            row = db.execute("SELECT public_json FROM runs WHERE id=?", (rid,)).fetchone()
        return with_explanations(json.loads(row[0])) if row else None

    def history(self, monitor_id, since=0, limit=100, before=None):
        with self.connect() as db:
            rows = db.execute("SELECT public_json FROM runs WHERE monitor_id=? AND started_at>=? AND started_at<? ORDER BY started_at DESC LIMIT ?",
                              (monitor_id, since, before or time.time() + 1, limit)).fetchall()
        return [with_explanations(json.loads(row[0])) for row in rows]

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT public_json FROM runs WHERE state='running'").fetchall()
            for row in rows:
                run = json.loads(row[0])
                run.update(state="interrupted", identity="unknown", availability="unknown", finished_at=time.time(), reason="worker_restarted")
                db.execute("UPDATE runs SET state=?,finished_at=?,public_json=? WHERE id=?", (run["state"], run["finished_at"], json.dumps(run), run["id"]))
            db.execute("UPDATE attempts SET outcome='interrupted' WHERE outcome='running'")

    def prune(self, retention_days, evidence_days):
        now = time.time()
        with self.connect() as db:
            db.execute("UPDATE runs SET evidence_json=NULL WHERE started_at<?", (now - evidence_days * 86400,))
            db.execute("DELETE FROM runs WHERE started_at<?", (now - retention_days * 86400,))
            db.execute("DELETE FROM attempts WHERE started_at<?", (now - max(retention_days, 2) * 86400,))

    def admin_providers(self):
        with self.connect() as db:
            # An explicit projection ensures API keys never cross the HTTP boundary.
            providers = [dict(r) for r in db.execute("SELECT id,name,base_url,enabled,credential!='' AS has_key FROM providers ORDER BY name")]
            monitors = [dict(r) for r in db.execute("SELECT id,provider_id,model,expected_model,channel,effort,interval,enabled FROM monitors ORDER BY model")]
        for p in providers:
            p["monitors"] = [m for m in monitors if m["provider_id"] == p["id"]]
        return providers

    def save_provider(self, payload, provider_id=None):
        from urllib.parse import urlparse
        name = str(payload.get("name", "")).strip()
        base = str(payload.get("base_url", "")).strip().rstrip("/")
        credential = str(payload.get("api_key", "")).strip()
        parsed = urlparse(base)
        if not name or len(name) > 80 or len(base) > 2048 or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port == 0:
            raise ValueError("Provide a name and an HTTP(S) API base URL without embedded credentials, query or fragment")
        if any(c in credential for c in "\r\n") or len(credential) > 4096:
            raise ValueError("Invalid API key")
        models = payload.get("monitors", [])
        if not isinstance(models, list) or not 1 <= len(models) <= 30:
            raise ValueError("Configure between 1 and 30 model monitors")
        normalized = []
        signatures = set()
        for model in models:
            requested = str(model.get("model", "")).strip()
            expected = str(model.get("expected_model", requested)).strip()
            channel = str(model.get("channel", "Standard")).strip()
            effort = model.get("effort", "high")
            try:
                interval = int(model.get("interval", 3600))
            except (TypeError, ValueError):
                raise ValueError("Interval must be seconds") from None
            if not all(0 < len(v) <= 160 for v in (requested, expected, channel)) or effort not in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra") or not 60 <= interval <= 604800:
                raise ValueError("Invalid model, reasoning effort or check interval")
            sig = (requested, channel, effort)
            if sig in signatures:
                raise ValueError("Duplicate model/channel/reasoning combination")
            signatures.add(sig)
            normalized.append((model.get("id"), requested, expected, channel, effort, interval, int(bool(model.get("enabled", True)))))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if provider_id:
                current = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
                if not current:
                    raise ValueError("Provider not found")
                key = credential or current["credential"]
                changed = base != current["base_url"] or key != current["credential"] or name != current["name"]
                db.execute("UPDATE providers SET name=?,base_url=?,credential=?,enabled=?,revision=revision+? WHERE id=?",
                           (name, base, key, int(bool(payload.get("enabled", True))), int(changed), provider_id))
            else:
                if not credential:
                    raise ValueError("An API key is required for a new provider")
                provider_id = uuid.uuid4().hex
                db.execute("INSERT INTO providers(id,name,base_url,credential,enabled) VALUES(?,?,?,?,?)", (provider_id, name, base, credential, int(bool(payload.get("enabled", True)))))
                changed = True
            old = {r["id"]: dict(r) for r in db.execute("SELECT * FROM monitors WHERE provider_id=?", (provider_id,))}
            kept = set()
            for mid, model, expected, channel, effort, interval, enabled in normalized:
                if mid and mid not in old:
                    raise ValueError("Invalid monitor ID")
                mid = mid or uuid.uuid4().hex
                if mid in kept:
                    raise ValueError("Duplicate monitor ID")
                kept.add(mid)
                previous = old.get(mid)
                same = previous and not changed and all(previous[k] == v for k, v in {"model": model, "expected_model": expected, "channel": channel, "effort": effort, "interval": interval, "enabled": enabled}.items())
                due = previous["next_due"] if same else 0
                db.execute("INSERT OR REPLACE INTO monitors VALUES(?,?,?,?,?,?,?,?,?)", (mid, provider_id, model, expected, channel, effort, interval, enabled, due))
            for mid in old.keys() - kept:
                db.execute("DELETE FROM monitors WHERE id=?", (mid,))
        return provider_id

    def delete_provider(self, pid):
        with self.connect() as db:
            db.execute("DELETE FROM monitors WHERE provider_id=?", (pid,))
            db.execute("DELETE FROM providers WHERE id=?", (pid,))
