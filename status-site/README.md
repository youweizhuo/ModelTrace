# ModelTrace Status

An independent application for scheduled model identity checks, using Codex CLI
with configured API providers. Availability is secondary; latency is diagnostic.
All application code, deployment files, dependencies, and tests live in this
directory. The upstream repository's app, plugin, scorer, and reference bank do
not need patches.

## Local installation

Requires Python 3.11+, a compatible Codex CLI (validated with 0.156.1), and Linux
for worker process isolation and locking.

```bash
cd status-site
uv venv .venv
uv pip install --python .venv/bin/python -e '.[test]'
cp config.example.toml config.local.toml
```

Edit `config.local.toml`. Paths are relative to that file. Create the configured
credentials file with `NAME=value` lines, then restrict it:

```bash
chmod 600 credentials.env
.venv/bin/modeltrace-status --config config.local.toml init
.venv/bin/modeltrace-status --config config.local.toml doctor
```

`init` imports the initial providers once and creates `admin-password` in the
configured data directory (mode 0600). It does not overwrite providers after
they have been edited through the website. After initialization, the database
is the source of truth for providers, keys, models, intervals, and pause state.
The TOML file controls the service port, worker limits, retention, and paths.

Install persistent services on this machine:

```bash
.venv/bin/python deploy/install-user-services.py --config /absolute/path/to/config.local.toml
systemctl --user status modeltrace-status-web modeltrace-status-worker
```

The service listens on the configured address (default `0.0.0.0:7861`). User
lingering must be enabled for boot and logout persistence. The installer does
not change system-wide settings. Service logs:

```bash
journalctl --user -u modeltrace-status-worker -f
```

For development, run `modeltrace-status --config ... web` and `... worker` in
separate terminals. Production uses Gunicorn, with one web worker and four
threads. A file lock allows only one scheduling worker per data directory.
`worker --once` runs each enabled monitor once, including bounded confirmation
checks, and exits.

## Managing providers

Open `/manage` and sign in using the server's `admin-password` file. You can:

- Add named providers with a base URL and API key.
- Add requested/expected model pairs, reasoning settings, channel labels, and
  intervals. Separate configurations have separate histories.
- Replace a key, or leave the key field blank to preserve it.
- Pause or resume providers and individual models, queue an immediate check,
  and remove providers. Removed monitors' existing history stays in the database.

Each model row has its own Pause/Resume control. Provider-level Pause provider/Resume
provider controls the parent provider; resuming a provider preserves the individual
models' pause settings. Resuming a model queues a fresh check when its provider
is enabled. A pause lets the current probe finish, then stops the remaining
samples and confirmation batches.

Click a provider's name to show its configuration: the base URL and each
model's expected model, reasoning, interval, and channel. Each model row shows
its latest identity result (linked to the evidence on the status page) and live
activity: Queued, then Checking with the completed probe count and a progress
bar, then when the last check finished and when the next is due. The last check's
outcome is named only when it wasn't a clean finish (Partial, Failed, Stopped,
Daily limit, Check error). Provider headers show a badge only when paused. Activity updates every two seconds. Queue and run state survive
a page reload; clicking while a check is already active does not schedule another
batch. **Check all** queues every enabled model of a provider. An offline worker
is shown explicitly in the header.

The editor validates fields before saving (URL scheme, credentials or query in the
URL, intervals, duplicate model/channel/reasoning combinations) and asks before
discarding unsaved changes. If the session expires, the page returns to sign-in
instead of failing silently.

Changes apply without restarting the service. In-flight probes may finish; the
worker stops starting further probes when their configuration changes or their
monitor is paused/removed. A changed credential, endpoint, requested/expected model
or channel invalidates the *current* assessment until a new check completes.
Changing the reasoning setting or interval keeps the current assessment.

Management requires an authenticated, expiring session and CSRF token. There is
no default password. Keys are never included in browser responses, query strings,
child-process arguments, or ordinary logs. The private SQLite database contains
provider keys and must remain private; backups contain those same secrets.
Direct HTTP access is intended for a trusted internal network. Any external
exposure should use HTTPS through your chosen reverse proxy or tunnel.

## Checks and interpretation

The `upstream_adapter.py` module is the only integration with upstream Python
code. It loads `fingerprint.py` and `data/unified_bank.json` from the configured
checkout. Challenge generation, numerical parsing, and mathematical scoring are
performed by upstream. The current adapter requests three fresh challenge
responses, each in an independent ephemeral Codex session.

Codex runs in an isolated configuration directory and clean working directory.
Work tools, plugins, hooks, external integrations and subagents are disabled;
unexpected tool events cause the probe to be rejected. The child has a deadline,
bounded output, and a separate process group. API keys are supplied only through
the child's provider environment. Transport retries are configured to zero.

The app's versioned assessment policy follows upstream Guard's current weight
thresholds: consistent requires the expected model to rank first with weight
at least 0.5; a mismatch signal requires another candidate at least 0.8,
expected-model weight at most 0.15, and a gap of at least 0.65. Three valid
samples are required. Other usable results are inconclusive. An expected model
outside the bank is explicitly marked as unsupported.

A response that arrives but is too short to score (upstream requires
`max(80, ⌈0.55 × requested count⌉)` parsed numbers; refusals and truncated answers
fail this) is not an availability failure. By default the worker replaces one such
response per batch with a fresh challenge (`sample_retries`, 0–3). The rejected
response is still recorded and shown in the check's evidence, and replacements
count against the daily budget. If the budget has no room, the batch is assessed
without the replacement. Failed requests are never replaced.

An initial mismatch can trigger two additional batches. Only complete batches
that all show the same mismatch produce a repeated signal. A continuing signal
does not start endless confirmation batches. These observations are correlated;
the site does not interpret repeated findings as independent authentication proof.
Thresholds are inherited heuristics, not newly calibrated accuracy guarantees.

Each check records the scorer and bank hashes, upstream Git revision, probe
profile, assessment policy, Codex version, and monitor configuration revision.
The checker version is derived from the scorer and bank hashes, probe profile and
policy only, so repository commits that leave those unchanged keep current results.
Monitors appear in a dense table, one card per provider (or per expected model,
or ungrouped): identity, closest reference model with its relative weight
(from the latest check, with a meter colored by the identity result),
history and last/next check. A check in progress is shown next to the last
completed result rather than replacing it. Rows can be filtered by state or text
and sorted by severity, name or last check. A banner appears only when the worker
is offline or the checker cannot load.

Each history bar takes the colour of the identity assessment from the latest
completed check in its period: teal for consistent, red for a mismatch signal,
blue for inconclusive, amber for a model outside the reference library, and gray
for no result. **Hatching** marks a period whose latest check found the API
unavailable, so an outage is never confused with a mismatch. Empty periods are
pale gray, and a tick above a bar marks a checker version change. The colours are
validated for colour-vision deficiency; every state also has a text label.

Hover or keyboard-focus a bar for a summary of that check: its state and closest
match, plus the expected model's weight, valid samples or availability only when
they are not routine. Each history strip is
a single tab stop; use the arrow keys to move between periods. Click (or tap) a
bar to open that check in the evidence drawer: a one-paragraph explanation, the
top candidate weights (plus the expected model), and each probe's outcome. Method
provenance is collapsed; settings the run used differently from the current
configuration are noted beside its time. Click a monitor's name for its Overview:
the current identity with the same explanation, when it was checked and is next
due, and availability for the selected period (success rate, per-period strip and
any failures by reason). Settings appear in the drawer subtitle and on `/manage`.
A History tab lists the monitor's full, paginated check list. Table rows show a
channel or differing expected model only when set, and a closest match that is
just the expected model is dimmed so disagreements stand out.
Drawer views are linkable (`/#monitor=…&run=…`) and the browser Back button
closes them. Links from `/manage` use `/?monitor=…&run=…`, which the status page
moves into the fragment, because Safari can percent-encode the `#` of a followed
link; a request for such an encoded path (`/%23monitor=…`) redirects to the fragment.

Availability is **successful Codex probes / probes with observed provider
outcomes**, not continuous uptime. Credential errors, rate/quota errors, upstream
errors and timeouts remain distinguishable. Local runner failures are monitoring
gaps. Invalid fingerprint samples can still have successful API responses.
Daily budgets count started CLI probes, including failures and confirmations;
the default is 120 probes per monitor per UTC day. Hidden behavior inside an
upstream gateway is outside this request budget. Durations include CLI overhead;
token counts are saved when supplied. TTFT/TBT remain null because the CLI event
stream does not provide verified per-token timestamps.

Probes within a batch are sequential. All monitors sharing an API server
(scheme, hostname and port) also run sequentially, even if they use different
keys, models or base paths. The service concurrency setting limits parallel work
across different servers. The oldest due monitor runs first. Other clients using
the same gateway can still consume its account slots.

Failed probes show a specific reason, HTTP status when present, elapsed time and
suggested action. Recognized reasons distinguish gateway concurrency queue
timeouts, unavailable accounts, upstream overload, authentication, quota and
connection failures. Explanations use fixed text; arbitrary provider errors and
stderr are never published or saved. A generic HTTP/category explanation means
the detailed reason was not available. Historical diagnoses recovered from local
gateway logs are explicitly labeled; identity scores and observations stay intact.
Codex CLI can discard a provider's HTTP 429 response body. In that case the page
reports the observed status and explains the ambiguity instead of guessing which
limit was reached. This app does not automatically read a provider's private logs.

If the worker stops, the UI marks existing results stale within 45 seconds. If
the web service itself cannot be reached, the page keeps the last results on
screen, says how old they are, and retries with backoff. An
incompatible checker produces unknown identity and an explicit diagnostic state.
Restarts mark interrupted runs unknown and resume schedules without replaying
all missed slots. Results are retained for 90 days; private prompts/responses
are removed after 30 days by default.

## Updating upstream

```bash
# Merge upstream using your normal Git workflow, then:
cd status-site
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/pytest -q
.venv/bin/modeltrace-status --config /absolute/path/to/config.toml doctor
systemctl --user restart modeltrace-status-worker modeltrace-status-web
```

The adapter contract test exercises the current upstream library using a real
reference sample, without network requests. An upstream API/schema change may
require updating the adapter; the scheduler, storage, and UI use normalized
results. Existing historical results are never silently rescored. Restart the
worker after changing the scorer/bank so one process uses a fixed method version.

## Backup and recovery

```bash
.venv/bin/modeltrace-status --config /absolute/path/to/config.toml backup /private/path/status-backup.sqlite3
```

The command uses SQLite's backup API for a consistent live snapshot and creates
the destination with mode 0600. Back up the private data directory's
`admin-password` and `session-secret` too. To restore, stop both services, replace
the database, remove the old database's `-wal` and `-shm` files while stopped,
and restart. Do not copy a live SQLite file without its transaction state.

## Docker Compose alternative

`deploy/compose.yaml` uses host networking on Linux to reach APIs bound to
`127.0.0.1`. In the mounted configuration use:

```toml
[service]
upstream = "/opt/modeltrace"
data_dir = "/var/lib/modeltrace-status"
secrets_file = "/config/credentials.env"
codex = "/usr/local/bin/codex"
```

Add your monitor definitions, then run:

```bash
MODELTRACE_CONFIG_DIR=/private/config-directory docker compose -f deploy/compose.yaml up -d --build
```

The configuration directory is mounted read-only, and the database uses a named
volume. The image pins the tested Codex version; override the Docker build argument
only after validating a new version. The Compose web binding is `0.0.0.0:7861`.
The live installation on the development machine uses systemd, not Docker.
