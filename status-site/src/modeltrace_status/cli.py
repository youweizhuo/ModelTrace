from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
from pathlib import Path

from .config import load_credentials, load_settings
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="ModelTrace scheduled status service")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("doctor")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("web")
    backup = commands.add_parser("backup")
    backup.add_argument("destination")
    args = parser.parse_args()
    settings = load_settings(args.config)
    store = Store(settings.db_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "init":
        store.seed(settings, load_credentials(settings))
        from .web import create_app
        create_app(settings)
        print(f"Configured {len(store.targets(enabled=False))} monitors. Admin password file: {settings.data_dir / 'admin-password'}")
    elif args.command == "doctor":
        from .upstream_adapter import UpstreamAdapter
        from .codex_runner import CodexRunner
        adapter = UpstreamAdapter(settings.upstream)
        print(json.dumps({"checker": adapter.provenance, "challenges": len(adapter.plan()), "models": sorted(adapter.models), "codex": CodexRunner(settings).version}, indent=2))
    elif args.command == "worker":
        from .worker import Worker
        runner = Worker(settings, store)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: runner.stop.set())
        runner.serve(once=args.once)
    elif args.command == "web":
        from .web import create_app
        create_app(settings).run(host=settings.host, port=settings.port, debug=False)
    elif args.command == "backup":
        destination = Path(args.destination).expanduser()
        if destination.exists():
            parser.error("Backup destination already exists")
        import os
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with store.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        print("Private database backup complete")


if __name__ == "__main__":
    main()
