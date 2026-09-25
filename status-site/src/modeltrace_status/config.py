from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Monitor:
    id: str
    provider: str
    model: str
    expected_model: str
    base_url: str
    key_env: str
    channel: str = "Standard"
    effort: str = "medium"
    interval: int = 3600

    @property
    def revision(self):
        fields = {k: v for k, v in self.__dict__.items() if k != "interval"}
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]

    def same_basis(self, recorded):
        """Whether a run recorded under `recorded` (a public() dict) checked this configuration.
        Interval and reasoning effort do not change the identity being assessed."""
        return replace(self, effort=recorded.get("effort", self.effort)).revision == recorded.get("revision")

    def public(self):
        return {k: getattr(self, k) for k in ("id", "provider", "model", "expected_model", "channel", "effort", "interval")} | {"revision": self.revision}


@dataclass(frozen=True)
class Settings:
    upstream: Path
    data_dir: Path
    codex: str
    monitors: tuple[Monitor, ...]
    secrets_file: Path | None = None
    concurrency: int = 2
    timeout: int = 240
    daily_budget: int = 120
    retention_days: int = 90
    evidence_days: int = 30
    host: str = "0.0.0.0"
    port: int = 7861

    @property
    def db_path(self):
        return self.data_dir / "status.sqlite3"


def load_settings(path=None):
    path = Path(path or os.environ.get("MODELTRACE_STATUS_CONFIG", "config.local.toml")).expanduser().resolve()
    raw = tomllib.loads(path.read_text())
    general = raw.get("service", {})
    def resolve(value):
        p = Path(value).expanduser()
        return (path.parent / p).resolve() if not p.is_absolute() else p.resolve()
    monitors = []
    for item in raw.get("monitors", []):
        m = Monitor(**item)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", m.id):
            raise ValueError("Monitor IDs must contain lowercase letters, digits, underscores or hyphens")
        u = urlparse(m.base_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment or u.port == 0:
            raise ValueError(f"Invalid base URL for {m.id}")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", m.key_env):
            raise ValueError(f"Invalid credential environment name for {m.id}")
        if not all(isinstance(v, str) and 0 < len(v) <= 160 for v in (m.provider, m.model, m.expected_model, m.channel)):
            raise ValueError("Invalid monitor labels")
        if m.effort not in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra") or not 60 <= m.interval <= 604800:
            raise ValueError(f"Invalid reasoning effort or interval for {m.id}")
        monitors.append(m)
    if not monitors or len({m.id for m in monitors}) != len(monitors):
        raise ValueError("Configure at least one monitor with unique IDs")
    settings = Settings(
        upstream=resolve(general["upstream"]), data_dir=resolve(general["data_dir"]),
        codex=general.get("codex", "codex"), monitors=tuple(monitors),
        secrets_file=resolve(general["secrets_file"]) if general.get("secrets_file") else None,
        **{key: general[key] for key in ("concurrency", "timeout", "daily_budget", "retention_days", "evidence_days", "host", "port") if key in general},
    )
    for key, lo, hi in (("concurrency", 1, 16), ("timeout", 10, 900), ("daily_budget", 3, 10000), ("retention_days", 1, 3650), ("evidence_days", 0, 3650), ("port", 1024, 65535)):
        if not isinstance(getattr(settings, key), int) or not lo <= getattr(settings, key) <= hi:
            raise ValueError(f"Invalid {key}")
    return settings


def load_credentials(settings):
    values = dict(os.environ)
    if settings.secrets_file:
        if settings.secrets_file.stat().st_mode & 0o077:
            raise ValueError("Credentials file must have mode 0600")
        for line in settings.secrets_file.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                raise ValueError("Credentials file must contain NAME=value lines")
            values[key.strip()] = value.strip()
    missing = [m.id for m in settings.monitors if not values.get(m.key_env)]
    if missing:
        raise ValueError("Missing credentials for: " + ", ".join(missing))
    return {m.id: values[m.key_env] for m in settings.monitors}
