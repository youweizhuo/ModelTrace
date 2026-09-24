"""Install this checkout as persistent systemd user services, without sudo."""
import argparse
import os
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--web-only", action="store_true", help="Start the website while an initial worker check is in progress")
args = parser.parse_args()
project = Path(__file__).resolve().parents[1]
venv = project / ".venv/bin"
config = Path(args.config).expanduser().resolve()
if not (venv / "gunicorn").exists():
    parser.error("Install this application's .venv first")
unit_dir = Path.home() / ".config/systemd/user"
unit_dir.mkdir(parents=True, exist_ok=True)
def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

# Derive binding from the same configuration the service will read.
import tomllib
service = tomllib.loads(config.read_text()).get("service", {})
bind = f"{service.get('host', '0.0.0.0')}:{service.get('port', 7861)}"
commands = {
    "web": f"{quote(venv / 'gunicorn')} --bind {quote(bind)} --workers 1 --threads 4 --timeout 60 modeltrace_status.web:create_app()",
    "worker": f"{quote(venv / 'modeltrace-status')} --config {quote(config)} worker",
}
for kind, command in commands.items():
    content = f'''[Unit]
Description=ModelTrace Status {kind}
After=network-online.target

[Service]
Type=simple
WorkingDirectory={str(project).replace('%', '%%')}
Environment={quote('MODELTRACE_STATUS_CONFIG=' + str(config))}
Environment={quote('PATH=' + str(venv) + ':' + str(Path.home() / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin')}
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=260
UMask=0077

[Install]
WantedBy=default.target
'''
    (unit_dir / f"modeltrace-status-{kind}.service").write_text(content)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
services = ["modeltrace-status-web"] if args.web_only else ["modeltrace-status-web", "modeltrace-status-worker"]
subprocess.run(["systemctl", "--user", "enable", "--now", *services], check=True)
print("Enabled " + ", ".join(services) + ".")
