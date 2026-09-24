import {
  $, $$, html, patch, badge, stateLabel, stateTone, percent, seconds, intervalLabel,
  clock, ago, formatDay, formatFull, formatRange, formatTime, tickRelative,
  request, poller, prefs, dialogControls,
} from './common.js';

const GROUPS = ['provider', 'model', 'none'];
const WINDOWS = ['24h', '7d', '30d'];
const SORTS = ['severity', 'score', 'monitor', 'checked'];
const WINDOW_LABEL = {'24h': '24 hours', '7d': '7 days', '30d': '30 days'};
const AXIS = {'24h': ['24h ago', '12h ago', 'now'], '7d': ['7d ago', '3.5d ago', 'now'], '30d': ['30d ago', '15d ago', 'now']};
const rate = tps => tps < 10 ? tps.toFixed(1) : String(Math.round(tps));
const speedText = speed => { const {ttft_ms, output_tps} = speed ?? {}; return (
  [ttft_ms != null ? `${seconds(ttft_ms)} TTFT` : null, output_tps != null ? `${rate(output_tps)} tok/s` : null].filter(Boolean).join(' · ')); };
const meter = weight => html`<div class="meter" aria-hidden="true"><i style="width:${(Math.max(0, Math.min(1, weight)) * 100).toFixed(1)}%"></i></div>`;
// A completed check with no usable response reads as an outage, not as "no result".
const shownState = run => run.state !== 'completed' ? run.state
  : run.identity === 'unknown' && run.availability === 'unavailable' ? 'unavailable' : run.identity;
const timeAxis = () => html`<div class="time-axis" aria-hidden="true">${AXIS[ui.window].map(label => html`<span>${label}</span>`)}</div>`;

// Lower is worse. Sorting by severity puts problems first.
const SEVERITY = {
  repeated_mismatch: 0, mismatch_signal: 1, unavailable: 2, checker_error: 3, budget_exhausted: 4,
  inconclusive: 5, not_in_library: 6, interrupted: 7, changed: 8, unknown: 9, consistent: 10, paused: 11,
};
const FILTERS = [
  ['all', 'All', null], ['mismatch', 'Mismatch', 'bad'], ['unavailable', 'Unavailable', 'outage'],
  ['inconclusive', 'Inconclusive', 'info'], ['other', 'Other', 'warn'], ['consistent', 'Consistent', 'good'],
  ['paused', 'Paused', 'none'],
];

function category(state) {
  if (state === 'mismatch_signal' || state === 'repeated_mismatch') return 'mismatch';
  if (['unavailable', 'inconclusive', 'consistent', 'paused'].includes(state)) return state;
  return 'other';
}

const ui = {
  group: prefs.get('mt-group', GROUPS, 'provider'),
  window: prefs.get('mt-window', WINDOWS, '24h'),
  sort: prefs.get('mt-sort', SORTS, 'severity'),
  reverse: false,
  filter: 'all',
  query: '',
  focusBar: new Map(),  // monitor id -> bucket index holding the history's single tab stop
};

let data = null;
let lastSuccess = 0;
let lastError = null;
let inflight = null;
let viewsById = new Map();

// ---------------------------------------------------------------------------
// Derived state

function view(m) {
  // Older servers only send `latest`; never let a running check hide the last result.
  const done = m.latest_completed !== undefined ? m.latest_completed : (m.latest?.state === 'running' ? null : m.latest);
  let state;
  if (!m.enabled) state = 'paused';
  else if (!done) state = 'unknown';
  else if (done.state !== 'completed') state = done.state;
  else if (done.identity === 'unknown' && done.availability === 'unavailable') state = 'unavailable';
  else state = done.identity;
  // A configuration or checker change invalidates the assessment; staleness only ages it.
  const flag = !m.enabled ? null : m.configuration_changed ? 'changed' : m.stale ? 'stale' : null;
  const current = flag === 'changed' ? 'changed' : state;
  return {
    m, done, state, flag, current,
    category: category(current),
    severity: SEVERITY[current] ?? 9,
    top: done?.candidates?.[0] ?? null,
    active: m.active ?? null,
    checkedAt: done?.finished_at || done?.started_at || 0,
    name: `${m.provider} / ${m.model}`,
  };
}

function failureTitle(run) {
  const attempt = run?.attempts?.findLast(a => a.outcome !== 'responded');
  return attempt?.diagnostic?.title ?? null;
}

function matches(v) {
  if (ui.filter !== 'all' && v.category !== ui.filter) return false;
  if (!ui.query) return true;
  const haystack = [v.m.provider, v.m.model, v.m.expected_model, v.m.channel, v.top?.model].join(' ').toLowerCase();
  return ui.query.split(/\s+/).every(term => haystack.includes(term));
}

// Best score first; monitors without one stay last in either direction.
function byScore(a, b) {
  if (a == null || b == null) return (a == null) - (b == null);
  return ui.reverse ? a - b : b - a;
}

function compare(a, b) {
  const byName = a.name.localeCompare(b.name);
  if (ui.sort === 'score') return byScore(a.m.score.value, b.m.score.value) || byName;
  let result;
  switch (ui.sort) {
    case 'monitor': result = byName; break;
    case 'checked': result = (a.checkedAt || Infinity) - (b.checkedAt || Infinity); break;
    default: result = a.severity - b.severity;
  }
  return (ui.reverse ? -result : result) || byName;
}

function groupKey(v) {
  return ui.group === 'provider' ? v.m.provider : ui.group === 'model' ? v.m.expected_model : '';
}

// ---------------------------------------------------------------------------
// Rendering

function render() {
  if (!data) return;
  const views = data.monitors.map(view);
  viewsById = new Map(views.map(v => [v.m.id, v]));
  renderNotice();
  renderFilters(views);
  renderControls();
  renderHead();
  renderRows(views);
  $('#method-version').textContent = data.checker?.version
    ? `Checker ${data.checker.version.slice(0, 8)} · ${data.checker.models?.length ?? 0} reference models${data.checker.codex_version ? ' · ' + data.checker.codex_version : ''}`
    : 'Checker not ready';
  if (drawer.el.open && drawer.tab === 'overview') renderOverview();
  tickRelative($('#monitors'));
  tick();
}

// Only monitoring problems get a banner; per-state counts live in the filter chips.
function renderNotice() {
  let notice = null;
  if (!data.worker_online) notice = html`<strong>Monitoring is offline.</strong> The worker last reported <span data-ago="${data.heartbeat?.at || 0}"></span>; results below are the last known state.`;
  else if (!data.checker?.ready) notice = html`<strong>Checker unavailable.</strong> The upstream fingerprint checker could not be loaded, so no identity assessments are being made.`;
  $('#notice').hidden = !notice;
  if (notice) patch($('#notice'), notice);
}

function renderFilters(views) {
  const counts = {all: views.length};
  for (const v of views) counts[v.category] = (counts[v.category] ?? 0) + 1;
  patch($('#filters'), FILTERS
    .filter(([key]) => key === 'all' || counts[key] || ui.filter === key)
    .map(([key, label, tone]) => html`<button type="button" class="chip" data-filter="${key}" data-key="filter:${key}" aria-pressed="${ui.filter === key}">${tone ? html`<i class="dot tone-${tone}" aria-hidden="true"></i>` : ''}${label}<span class="count">${counts[key] ?? 0}</span></button>`));
}

function renderControls() {
  for (const b of $$('[data-group]')) b.setAttribute('aria-pressed', String(b.dataset.group === ui.group));
  for (const b of $$('[data-window]')) b.setAttribute('aria-pressed', String(b.dataset.window === ui.window));
  $('#sort-select').value = ui.sort;
}

function renderHead() {
  const sortButton = (key, label) => html`<button type="button" class="sort" data-sort="${key}" data-key="sort:${key}" aria-pressed="${ui.sort === key}" aria-label="Sort by ${label}${ui.sort === key ? (ui.reverse ? ', reversed' : '') : ''}">${label}<span class="sort-mark" aria-hidden="true">${ui.sort === key ? (ui.reverse ? '↑' : '↓') : ''}</span></button>`;
  patch($('#table-head'), html`
    <div class="c-monitor">${sortButton('monitor', 'Monitor')}</div>
    <div class="c-identity"><span title="Average weight the fingerprint gave the expected model over scheduled checks in ${WINDOW_LABEL[ui.window]}">${sortButton('score', 'Score')}</span></div>
    <div class="c-closest"><span class="head-label">Closest match</span></div>
    <div class="c-speed" title="Latest check: time to first token and answer decode rate. Colored against checks of the same model and reasoning in this window."><span class="head-label">Speed</span></div>
    <div class="c-history" title="History · ${WINDOW_LABEL[ui.window]}">${timeAxis()}</div>
    <div class="c-checked">${sortButton('checked', 'Checked')}</div>`);
}

function renderRows(views) {
  const container = $('#monitors');
  if (!views.length) {
    patch(container, html`<div class="empty"><p>No monitors are configured yet.</p><a class="button" href="/manage">Add a provider</a></div>`);
    return;
  }
  const visible = views.filter(matches).sort(compare);
  if (!visible.length) {
    patch(container, html`<div class="empty"><p>No monitors match the current filter.</p><button type="button" class="button" data-clear-filters>Clear filters</button></div>`);
    return;
  }
  const groups = new Map();
  for (const v of visible) {
    const key = groupKey(v);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(v);
  }
  const ordered = ui.sort === 'score' ? [...groups].sort(([, a], [, b]) => byScore(groupScoreValue(a), groupScoreValue(b))) : [...groups];
  patch(container, ordered.map(([label, items]) => html`
    <section class="group" aria-label="${label || 'Monitors'}">
      ${ui.group === 'none' ? '' : html`<h2 class="group-head"><span class="group-name">${stateDots(items)}${label}</span>${groupScore(items)}</h2>`}
      <ul class="rows" role="list">${items.map(row)}</ul>
    </section>`));
}

const scoreTone = value => value >= .8 ? 'good' : value >= .5 ? 'warn' : 'bad';

function scoreCell(score, subject) {
  if (score?.value == null) return html`<span class="muted">—</span>`;
  const title = `${subject} ${score.checks} scored check${score.checks === 1 ? '' : 's'} in ${WINDOW_LABEL[ui.window]}`;
  return html`<div class="score tone-${scoreTone(score.value)}" title="${title}">
    <span class="num">${percent(score.value)}</span>
    ${meter(score.value)}
  </div>`;
}

// Each monitor counts once, however often it is checked.
function groupScoreValue(items) {
  const scored = items.filter(v => v.m.score.value != null);
  return scored.length ? scored.reduce((n, v) => n + v.m.score.value, 0) / scored.length : null;
}

function groupScore(items) {
  const scored = items.filter(v => v.m.score.value != null);
  if (!scored.length) return '';
  const value = groupScoreValue(items);
  return html`<span class="group-score">${scoreCell({value, checks: scored.reduce((n, v) => n + v.m.score.checks, 0)}, `Average of ${scored.length} model${scored.length === 1 ? '' : 's'} over`)}</span>`;
}

// One dot per model, in row order, so a provider whose models disagree is visible at a glance.
function stateDots(items) {
  const label = items.map(v => `${v.m.model}: ${stateLabel(v.current).toLowerCase()}`).join(', ');
  return html`<span class="state-dots" role="img" aria-label="${label}" title="${label}">${items.map(v => html`<i class="dot tone-${v.m.enabled ? stateTone(v.current) : 'none'}"></i>`)}</span>`;
}

function row(v) {
  const {m, done, top, active} = v;
  const primary = ui.group === 'provider' ? m.model : ui.group === 'model' ? m.provider : null;
  // Settings live in the drawer subtitle and on /manage; rows only carry what changes the reading.
  const meta = [m.channel !== 'Standard' ? m.channel : null,
    m.expected_model !== m.model ? `expects ${m.expected_model}` : null].filter(Boolean).join(' · ');
  const mark = m.enabled ? stateTone(v.current) : 'none';
  const dim = m.enabled && v.flag === 'stale' ? ' is-stale' : '';
  return html`<li class="row cat-${v.category} mark-${mark}${dim}${m.enabled ? '' : ' is-paused'}">
    <div class="c-monitor">
      <button type="button" class="monitor-link" data-open="${m.id}" data-key="open:${m.id}" aria-label="Details for ${v.name}">
        ${primary === null ? html`<span class="muted">${m.provider} /</span> <span class="mono">${m.model}</span>` : html`<span class="${ui.group === 'provider' ? 'mono' : ''}">${primary}</span>`}
      </button>
      ${active ? html`<span class="activity" title="Checking: ${active.completed_samples} of ${active.planned_samples} probes"><span class="spinner" aria-hidden="true"></span>${active.completed_samples}/${active.planned_samples}</span>` : ''}
      ${meta ? html`<div class="sub">${meta}</div>` : ''}
    </div>
    <div class="c-identity">${identityCell(v)}</div>
    <div class="c-closest">${closestCell(v)}</div>
    <div class="c-speed">${speedCell(v)}</div>
    <div class="c-history">${bars(v)}</div>
    <div class="c-checked">
      ${done ? html`<span data-ago="${v.checkedAt}" title="${formatFull(v.checkedAt)}"></span>` : html`<span class="muted">never</span>`}
      <div class="sub">${active ? 'checking now' : !m.enabled ? 'paused' : m.next_due ? html`next <span data-until="${m.next_due}"></span>` : 'queued'}</div>
    </div>
  </li>`;
}

function identityCell(v) {
  const {m, done, active, flag, state} = v;
  const shown = flag === 'changed' ? 'changed' : state;
  const notes = [stateLabel(shown)];
  if (flag === 'changed') notes.push(`Last result: ${stateLabel(state)}`);
  else if (flag === 'stale') notes.push(data.worker_online ? 'Stale: check overdue' : 'Stale: worker offline');
  else if (!m.enabled && done) notes.push(`Last result: ${stateLabel(done.state === 'completed' ? done.identity : done.state)}`);
  const failure = failureTitle(done);
  if (failure && ['unavailable', 'checker_error'].includes(state)) notes.push(failure);
  // The state itself is the row's left bar; the cell carries it for hover and screen readers.
  return html`<div class="identity-cell" title="${notes.join(' · ')}"><span class="visually-hidden">${stateLabel(shown)}. </span>${scoreCell(m.score, `${m.expected_model} over`)}</div>`;
}

function closestCell(v) {
  const {top, m} = v;
  if (!top) return html`<span class="muted">—</span>`;
  const same = top.model === m.expected_model;
  return html`<div class="closest-line${same ? ' is-expected' : ''}"><span class="mono truncate" title="${top.model}${same ? ' (expected model)' : ''}">${top.model}</span><span class="num">${percent(top.weight)}</span></div>
    ${meter(top.weight)}`;
}

function speedCell(v) {
  const s = v.m.speed;
  if (!speedText(s)) return '';
  const title = `Median of the latest check’s probes. ${typicalSpeed(v.m)}.`;
  return html`<div class="speed" title="${title}">
    <div>${s.ttft_ms != null ? html`<span class="muted speed-label">TTFT</span><span class="num speed-value tone-${s.ttft_tone}">${seconds(s.ttft_ms)}</span>` : ''}</div>
    <div>${s.output_tps != null ? html`<span class="muted speed-label">TPS</span><span class="num speed-value tone-${s.tps_tone}">${rate(s.output_tps)}</span> <span class="muted">tok/s</span>` : ''}</div>
  </div>`;
}

function bars(v) {
  const {m} = v;
  const history = m.history;
  const lastWithRun = history.findLastIndex(b => b.latest_id);
  let focus = ui.focusBar.get(m.id);
  if (focus == null || focus >= history.length) focus = lastWithRun >= 0 ? lastWithRun : history.length - 1;
  let previous = null;
  const items = history.map((b, i) => {
    const changed = b.versions.length > 1 || (previous && b.versions.length && previous !== b.versions[0]);
    if (b.versions.length) previous = b.versions.at(-1);
    const state = b.latest_id ? (b.latest_identity || 'unknown') : 'empty';
    const outage = b.latest_availability === 'unavailable';
    const s = b.checks.at(-1);
    const label = b.latest_id
      ? `${formatRange(b.start, b.end)}: ${stateLabel(state)}${outage ? ', API unavailable' : ''}${s?.top ? `, closest ${s.top.model} ${percent(s.top.weight)}` : ''}. ${b.runs} check${b.runs === 1 ? '' : 's'}.`
      : `${formatRange(b.start, b.end)}: no check recorded.`;
    return html`<button type="button" class="bar s-${state}${outage ? ' outage' : ''}${changed ? ' version-mark' : ''}" data-bar="${i}" data-monitor="${m.id}" data-key="bar:${m.id}:${i}" tabindex="${i === focus ? 0 : -1}" aria-label="${label}" aria-disabled="${!b.latest_id}"></button>`;
  });
  return html`<div class="bars" role="group" aria-label="${v.name} history, ${WINDOW_LABEL[ui.window]}. Use arrow keys to move between periods." style="--n:${history.length}">${items}</div>`;
}

// ---------------------------------------------------------------------------
// Live indicator, errors and polling

function tick() {
  const live = $('#live');
  let state, text;
  if (!data) {
    state = lastError ? 'error' : 'connecting';
    text = lastError ? 'Cannot reach server' : 'Connecting…';
  } else if (lastError) {
    state = 'error';
    text = `Reconnecting · updated ${ago(lastSuccess)}`;
  } else if (!data.worker_online) {
    state = 'offline';
    text = 'Worker offline';
  } else {
    state = 'live';
    text = `Live · updated ${ago(lastSuccess)}`;
  }
  live.dataset.state = state;
  if ($('#live-text').textContent !== text) $('#live-text').textContent = text;
  tickRelative();
}

async function refresh() {
  inflight?.abort();
  const controller = new AbortController();
  inflight = controller;
  const requested = ui.window;
  try {
    const result = await request(`/api/status?window=${requested}&tz=${new Date().getTimezoneOffset()}`, {signal: controller.signal, timeout: 20000});
    if (requested !== ui.window) return;
    clock.sync(result.at);
    data = result;
    lastSuccess = clock.now();
    lastError = null;
    $('#error').hidden = true;
    render();
    openInitialLink();
  } catch (error) {
    if (error.kind === 'cancelled') return;
    lastError = error;
    const box = $('#error');
    box.hidden = false;
    box.textContent = data
      ? `Couldn’t refresh status: ${error.message} Showing results from ${ago(lastSuccess)}; retrying automatically.`
      : `Couldn’t load status: ${error.message} Retrying automatically.`;
    if (!data) patch($('#monitors'), html`<div class="empty"><p>Status is unavailable right now.</p><button type="button" class="button" data-retry>Retry now</button></div>`);
    tick();
    openInitialLink();
    throw error;
  } finally {
    if (inflight === controller) inflight = null;
  }
}

const poll = poller(refresh, {interval: 30000, retry: 5000});

// A shared link opens after the first load so monitor names are known
// (the drawer also works for monitors that have since been removed).
let initialLinkHandled = false;
function openInitialLink() {
  if (initialLinkHandled) return;
  initialLinkHandled = true;
  if (location.hash) syncFromHash();
}

// ---------------------------------------------------------------------------
// Tooltip (pointer and keyboard; touch goes straight to the drawer)

const tooltip = $('#tooltip');

function showTip(button) {
  const v = viewsById.get(button.dataset.monitor);
  const b = v?.m.history[Number(button.dataset.bar)];
  if (!b) return;
  const s = b.checks.at(-1);
  const earlier = b.runs - b.checks.length;
  tooltip.innerHTML = String(b.checks.length > 1 ? html`
    <div class="tip-time">${formatRange(b.start, b.end)} · ${b.runs} checks</div>
    <ol class="tip-checks">${b.checks.toReversed().map(c => html`<li>
      <span class="num muted">${formatTime(c.started_at)}</span>
      <span class="history-state"><i class="dot tone-${stateTone(shownState(c))}" aria-hidden="true"></i>${stateLabel(shownState(c))}</span>
      <span class="num">${c.top ? percent(c.top.weight) : '—'}</span>
    </li>`)}</ol>
    ${earlier ? html`<p class="tip-note">${earlier} earlier check${earlier === 1 ? '' : 's'} not shown.</p>` : ''}
    <p class="tip-hint">Click to open these checks</p>`
    : s ? html`
    <div class="tip-time">${formatRange(b.start, b.end)}</div>
    <div class="tip-state">${badge(shownState(s))}</div>
    <dl class="tip-facts">
      <div><dt>Closest</dt><dd>${s.top ? html`<span class="mono">${s.top.model}</span> · ${percent(s.top.weight)}` : 'no fingerprint'}</dd></div>
      ${s.top && s.top.model !== v.m.expected_model ? html`<div><dt>${v.m.expected_model}</dt><dd>${percent(s.expected_weight)}</dd></div>` : ''}
      ${speedText(s) ? html`<div><dt>Speed</dt><dd>${speedText(s)}</dd></div>` : ''}
      ${s.valid_samples < s.planned_samples ? html`<div><dt>Valid samples</dt><dd>${s.valid_samples}/${s.planned_samples}</dd></div>` : ''}
      ${s.availability !== 'available' ? html`<div><dt>Availability</dt><dd>${stateLabel(s.availability)}${s.http_status ? ` · HTTP ${s.http_status}` : ''}</dd></div>` : ''}
    </dl>
    ${s.failure ? html`<p class="tip-note">${s.failure}</p>` : ''}
    ${b.versions.length > 1 ? html`<p class="tip-note">Checker changed during this period.</p>` : ''}
    <p class="tip-hint">Click for full evidence</p>`
    : html`<div class="tip-time">${formatRange(b.start, b.end)}</div><p class="tip-note">No check recorded in this period.</p>`);
  tooltip.hidden = false;
  const r = button.getBoundingClientRect();
  const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
  tooltip.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2))}px`;
  const above = r.top - h - 8;
  tooltip.style.top = `${above >= 8 ? above : Math.min(innerHeight - h - 8, r.bottom + 8)}px`;
}

const hideTip = () => { tooltip.hidden = true; };

// ---------------------------------------------------------------------------
// Drawer with shareable #monitor=…&run=…&tab=… links

const drawer = {
  el: $('#drawer'), monitor: null, run: null, tab: 'check', pushed: false,
  history: null,  // {monitor, runs, next, loading, error}
  token: 0,
};
const runCache = new Map();
const ID = /^[A-Za-z0-9_-]{1,80}$/;

// Links from other pages carry the drawer state in the query string: Safari has
// been seen to percent-encode a '#' in a followed link, making it part of the path.
{
  const query = new URLSearchParams(location.search);
  if (query.has('monitor') || query.has('run')) history.replaceState(null, '', `${location.pathname}#${query}`);
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const clean = key => (ID.test(params.get(key) ?? '') ? params.get(key) : null);
  const tab = params.get('tab');
  return {monitor: clean('monitor'), run: clean('run'), tab: ['overview', 'history'].includes(tab) ? tab : 'check'};
}

function writeHash(state, method) {
  const params = new URLSearchParams();
  if (state.monitor) params.set('monitor', state.monitor);
  if (state.run) params.set('run', state.run);
  if (state.tab !== 'check') params.set('tab', state.tab);
  history[method](null, '', `${location.pathname}${location.search}#${params}`);
}

function openDrawer(state) {
  const wasOpen = drawer.el.open;
  writeHash(state, wasOpen ? 'replaceState' : 'pushState');
  if (!wasOpen) drawer.pushed = true;
  showDrawer(state);
}

function closeDrawer() {
  if (!drawer.el.open) return;
  if (drawer.pushed) {
    drawer.pushed = false;
    history.back();  // popstate hides the drawer
  } else {
    history.replaceState(null, '', location.pathname + location.search);
    hideDrawer();
  }
}

function hideDrawer() {
  drawer.pushed = false;
  drawer.token++;
  drawer.history = null;
  if (!drawer.el.open) return;
  drawer.el.close();  // restores focus to the opener if it still exists
  if (!drawer.returnFocus?.isConnected) {
    const key = drawer.monitor && `open:${drawer.monitor}`;
    (key && $(`[data-key="${CSS.escape(key)}"]`))?.focus({preventScroll: true});
  }
}

function syncFromHash() {
  const state = readHash();
  if (state.monitor || state.run) showDrawer(state);
  else hideDrawer();
}

async function runById(id) {
  if (runCache.has(id)) return runCache.get(id);
  const run = await request(`/api/runs/${encodeURIComponent(id)}`);
  if (run.state !== 'running') {
    if (runCache.size > 200) runCache.delete(runCache.keys().next().value);
    runCache.set(id, run);
  }
  return run;
}

function setDrawerTitle(monitorId, run) {
  const m = viewsById.get(monitorId)?.m ?? run?.monitor;
  if (!m) return;
  $('#drawer-title').textContent = `${m.provider} / ${m.model}`;
  $('#drawer-subtitle').textContent = [m.expected_model !== m.model ? `expects ${m.expected_model}` : null,
    `${m.effort} reasoning`, `every ${intervalLabel(m.interval)}`, m.channel !== 'Standard' ? m.channel : null].filter(Boolean).join(' · ');
}

async function showDrawer(state) {
  let {monitor, run, tab} = state;
  if (!drawer.el.open) {
    hideTip();
    drawer.returnFocus = document.activeElement;
    drawer.el.showModal();
    $('#pane-check').innerHTML = '';
  }
  const latest = monitor ? viewsById.get(monitor)?.done?.id ?? null : null;
  if (tab === 'overview' && !viewsById.has(monitor)) tab = 'check';  // removed monitor: evidence only
  if (monitor && !run && tab === 'check') {
    run = latest;
    if (!run) tab = 'history';
  }
  if (monitor !== drawer.monitor) drawer.history = null;
  Object.assign(drawer, {monitor, run, tab});
  for (const button of $$('[role=tab]', drawer.el)) {
    const selected = button.dataset.tab === tab;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  $('#pane-overview').hidden = tab !== 'overview';
  $('#pane-check').hidden = tab !== 'check';
  $('#pane-history').hidden = tab !== 'history';
  $('#tab-check').disabled = !run && !latest;
  $('#tab-overview').disabled = !viewsById.has(monitor);
  setDrawerTitle(monitor);
  if (tab === 'overview') renderOverview();
  if (tab === 'check') await renderCheck(run);
  if (drawer.monitor) {
    if (!drawer.history) loadHistory(drawer.monitor, false);
    else renderHistory();
  }
}

function selectTab(tab) {
  if (tab === drawer.tab) return;
  openDrawer({monitor: drawer.monitor, run: drawer.run, tab});
}

async function renderCheck(id) {
  const pane = $('#pane-check');
  const token = ++drawer.token;
  if (!id) { pane.innerHTML = String(html`<p class="muted">No completed check yet.</p>`); return; }
  if (pane.dataset.run !== id) pane.innerHTML = String(html`<div class="skeleton-block"></div><div class="skeleton-block short"></div>`);
  pane.dataset.run = id;
  let run;
  try {
    run = await runById(id);
  } catch (error) {
    if (token === drawer.token) pane.innerHTML = String(html`<p class="error-text">Couldn’t load this check. ${error.message}</p><button type="button" class="button" data-reload-run>Try again</button>`);
    return;
  }
  if (token !== drawer.token) return;
  if (!drawer.monitor) {
    drawer.monitor = run.monitor_id;
    writeHash({monitor: run.monitor_id, run: id, tab: 'check'}, 'replaceState');
    loadHistory(run.monitor_id, false);
  }
  setDrawerTitle(run.monitor_id, run);
  pane.innerHTML = String(checkMarkup(run));
  tickRelative(pane);
}

function explanation(run) {
  const expected = run.monitor.expected_model, top = run.candidates?.[0];
  const planned = run.planned_samples ?? 3;
  if (run.state === 'running') return `This check is in progress: ${run.attempts.length} of ${planned} probes completed.`;
  if (run.state === 'interrupted') return 'This check stopped before finishing, because the worker restarted or the monitor was changed or paused.';
  if (run.state === 'budget_exhausted') return 'The monitor reached its daily probe budget. Checks resume after the UTC day rolls over.';
  if (run.state === 'checker_error') return 'The upstream checker was unavailable or incompatible, so no assessment was made.';
  switch (run.identity) {
    case 'consistent': return `The expected model ${expected} ranked first with ${percent(top?.weight)} relative weight.`;
    case 'mismatch_signal':
    case 'repeated_mismatch':
      return `${top?.model} ranked first with ${percent(top?.weight)}; the expected model ${expected} received ${percent(run.expected_weight)}.`;
    case 'inconclusive':
      return run.valid_samples < planned
        ? `Only ${run.valid_samples} of ${planned} samples were valid${run.attempts.some(a => a.replaces != null) ? ', even after a replacement probe' : ''}; ${planned} are required for an assessment.`
        : 'The scores did not meet the thresholds for a consistent or mismatch result.';
    case 'not_in_library': return `${expected} isn’t in the reference library, so its identity can’t be assessed. The closest reference model is shown for context.`;
    default:
      return run.availability === 'unavailable' ? 'No probe got a usable response, so no fingerprint was taken.' : 'No identity result was produced for this check.';
  }
}

// A 404 on every probe while sibling models on the same provider respond almost
// always means the gateway doesn't serve this model name.
function modelNotServedHint(run) {
  if (!run.attempts.length || !run.attempts.every(a => a.http_status === 404)) return '';
  const siblings = [...viewsById.values()].filter(v => v.m.provider === run.monitor.provider && v.m.id !== run.monitor_id);
  const working = siblings.find(v => v.m.metrics.responded > 0);
  return working
    ? html` <strong>${run.monitor.provider}</strong> answered for <span class="mono">${working.m.model}</span>, so the API and key work; the gateway most likely doesn’t serve <span class="mono">${run.monitor.model}</span>. Check the model name in Manage.`
    : '';
}

function checkMarkup(run) {
  const expected = run.monitor.expected_model;
  const planned = run.planned_samples ?? 3;
  let candidates = (run.candidates ?? []).slice(0, 3);
  const expectedEntry = run.candidates?.find(c => c.model === expected);
  if (expectedEntry && !candidates.includes(expectedEntry)) candidates = [...candidates, expectedEntry];
  const tokens = run.attempts.reduce((n, a) => n + (a.usage?.output_tokens ?? 0), 0);
  const runs = drawer.history?.monitor === run.monitor_id ? drawer.history.runs : [];
  const index = runs.findIndex(r => r.id === run.id);
  const newer = index > 0 ? runs[index - 1] : null, older = index >= 0 ? runs[index + 1] : null;
  return html`
    <div class="check-head">
      <div>${badge(shownState(run))}
        <p class="muted small">${formatFull(run.started_at)} · <span data-ago="${run.started_at}"></span></p></div>
      <div class="pager">
        <button type="button" class="button small" data-goto="${older?.id ?? ''}" ${older ? '' : 'disabled'} aria-label="Older check">← Older</button>
        <button type="button" class="button small" data-goto="${newer?.id ?? ''}" ${newer ? '' : 'disabled'} aria-label="Newer check">Newer →</button>
      </div>
    </div>
    ${slotTabs(run)}
    <p class="verdict-text">${explanation(run)}</p>
    <section class="panel">
      <h3 title="Weights are relative within the reference library; they aren’t probabilities of authenticity.">Fingerprint candidates</h3>
      ${candidates.length ? html`<ul class="candidates" role="list">${candidates.map(c => html`
        <li class="${c.model === expected ? 'is-expected' : ''}">
          <div class="candidate-line"><span class="mono">${c.model}</span>${c.model === expected ? html`<span class="tag">expected</span>` : ''}<span class="num">${percent(c.weight, 1)}</span></div>
          ${meter(c.weight)}
        </li>`)}</ul>`
        : html`<p class="muted">No fingerprint scores for this check.</p>`}
    </section>
    <section class="panel">
      <h3>Probes · ${run.valid_samples}/${planned} valid</h3>
      ${run.attempts.length ? html`<ol class="probes">${probes(run)}</ol>` : html`<p class="muted">No probes were sent.</p>`}
    </section>
    <details class="run-details">
      <summary>Run details</summary>
      <dl class="facts">
        <div><dt>Requested model</dt><dd class="mono">${run.monitor.model}</dd></div>
        <div><dt>Reasoning</dt><dd>${run.monitor.effort}</dd></div>
        <div><dt>Output tokens</dt><dd>${tokens || 'not reported'}</dd></div>
        <div><dt>Codex</dt><dd class="mono">${run.provenance?.codex_version ?? 'unavailable'}</dd></div>
        <div><dt>Checker</dt><dd class="mono">${run.checker_version}</dd></div>
      </dl>
    </details>`;
}

// Checks sharing this one's history period, so a busy hour stays reachable from its bar.
function slotTabs(run) {
  const bucket = viewsById.get(run.monitor_id)?.m.history.find(b => b.checks.some(c => c.id === run.id));
  if (!bucket || bucket.checks.length < 2) return '';
  return html`<div class="slot-tabs" role="group" aria-label="Checks ${formatRange(bucket.start, bucket.end)}">
    <span class="muted small">${formatRange(bucket.start, bucket.end)}</span>
    ${bucket.checks.map(c => html`<button type="button" class="slot-tab" data-goto="${c.id}" aria-current="${c.id === run.id}"><i class="dot tone-${stateTone(shownState(c))}" aria-hidden="true"></i>${formatTime(c.started_at)}</button>`)}
  </div>`;
}

const SOURCES = {
  gateway_logs: 'Correlated gateway log', codex_error: 'Provider error reported by Codex',
  http_status: 'HTTP status only; detailed reason not recorded', category: 'Recorded failure category',
};

// Scorer diagnostics are indexed over responded probes only, in order.
function probes(run) {
  let responded = 0;
  return run.attempts.map((a, i) => {
    const index = a.outcome === 'responded' ? responded++ : null;
    const sample = index === null ? null : run.diagnostics?.find(d => d.index === index);
    const replacement = run.attempts.findIndex(other => other.replaces === i);
    return probe(a, i, sample, replacement);
  });
}

function probe(a, i, sample, replacement) {
  const ok = a.outcome === 'responded';
  const rejected = ok && sample && sample.accepted === false;
  const d = a.diagnostic;
  return html`<li class="probe ${rejected ? 'rejected' : ok ? 'ok' : 'failed'}">
    <div class="probe-line"><span class="probe-icon" aria-hidden="true">${rejected ? '!' : ok ? '✓' : '✕'}</span>
      <strong>Probe ${i + 1}</strong>${a.replaces != null ? html`<span class="tag">replaces probe ${a.replaces + 1}</span>` : ''}<span>${rejected ? 'Not usable as a sample' : ok ? 'Valid sample' : d?.title ?? stateLabel(a.outcome)}</span>
      <span class="muted num">${ok && sample?.accepted ? `${sample.parsed_numbers} numbers · ` : ''}${a.http_status ? `HTTP ${a.http_status} · ` : ''}${speedText(a) ? `${speedText(a)} · ` : ''}${seconds(a.duration_ms)}</span></div>
    ${rejected ? html`<p>The response contained ${sample.parsed_numbers} numbers; the fingerprint parser needs at least ${sample.minimum_numbers}, so it was excluded from scoring.</p><p class="muted">The API worked. ${replacement >= 0 ? `Probe ${replacement + 1} was sent with a fresh challenge to replace it.` : 'No replacement was sent (the per-check replacement limit or daily budget was reached).'}</p>` : ''}
    ${d && !ok ? html`<p>${d.detail}</p><p class="muted">${d.action}</p><p class="small muted mono">${d.code} · ${SOURCES[d.source] ?? d.source}</p>` : ''}
  </li>`;
}

// ---------------------------------------------------------------------------
// Overview: monitor, availability and provider context from the status snapshot.

const OUTCOMES = {
  responded: 'Responded', provider_error: 'Provider error', auth_error: 'Credential rejected', rate_limit: 'Rate or quota limit',
  timeout: 'Timed out', runner_error: 'Local runner error', tool_use: 'Tool use rejected', output_limit: 'Output limit', interrupted: 'Interrupted',
};

function historyStrip(m) {
  const items = m.history.map(b => {
    const state = b.latest_id ? b.latest_identity || 'unknown' : 'empty';
    const outage = b.latest_availability === 'unavailable';
    const label = `${formatRange(b.start, b.end)}: ${b.latest_id ? `${stateLabel(state)}${outage ? ', API unavailable' : ''}` : 'no check'}`;
    return html`<span class="bar s-${state}${outage ? ' outage' : ''}" role="img" aria-label="${label}" title="${label}"></span>`;
  });
  return html`<div class="bars static" style="--n:${m.history.length}">${items}</div>`;
}

const availabilityTone = rate => rate >= .95 ? 'good' : rate >= .8 ? 'warn' : 'bad';

function stat(label, value, tone, caption, {title = '', weight = null} = {}) {
  return html`<div class="stat${tone ? ` tone-${tone}` : ''}" title="${title}">
    <span class="stat-label">${label}</span>
    <span class="stat-value num">${value ?? '—'}</span>
    ${weight != null ? meter(weight) : ''}
    <span class="stat-caption">${caption}</span>
  </div>`;
}

const typicalSpeed = m => `Typical for ${m.expected_model} at ${m.effort} reasoning: ${speedText(m.speed.typical)} over ${m.speed.checks} check${m.speed.checks === 1 ? '' : 's'} in ${WINDOW_LABEL[ui.window]}`;

// One screen that answers "is this monitor OK right now?". Settings are in the
// drawer subtitle and on /manage; per-check detail is in the Check tab.
function renderOverview() {
  const pane = $('#pane-overview');
  const v = viewsById.get(drawer.monitor);
  if (!v) { patch(pane, html`<p class="muted">This monitor is no longer configured.</p>`); return; }
  const {m, done, active} = v;
  const {score, speed, metrics} = m, typical = speed.typical;
  const measured = metrics.responded + metrics.failed;
  const failures = Object.entries(metrics.outcomes).filter(([k]) => k !== 'responded').sort((a, b) => b[1] - a[1]);
  const note = v.flag === 'changed' ? 'Settings or the checker changed since this result; the next check will replace it.'
    : v.flag === 'stale' ? (data.worker_online ? 'This result is overdue for a new check.' : 'The worker is offline, so this result may be out of date.') : '';
  const schedule = active ? `checking now (${active.completed_samples}/${active.planned_samples})` : !m.enabled ? 'paused' : m.next_due ? html`next check <span data-until="${m.next_due}"></span>` : 'queued';
  const period = WINDOW_LABEL[ui.window];
  patch(pane, html`
    <div class="check-head">
      <div class="identity-line">${badge(v.flag === 'changed' ? 'changed' : v.state)}${active ? html`<span class="activity"><span class="spinner" aria-hidden="true"></span>${active.completed_samples}/${active.planned_samples}</span>` : ''}</div>
      ${done ? html`<button type="button" class="button small" data-goto="${done.id}">Latest check →</button>` : ''}
    </div>
    <p class="overview-summary">${done ? html`${explanation(done)}${modelNotServedHint(done)}` : 'No completed check yet.'}</p>
    ${note ? html`<p class="overview-note">${note}</p>` : ''}
    <p class="muted small">${done ? html`Checked <span data-ago="${v.checkedAt}"></span> · ` : ''}${schedule}</p>

    <div class="stats">
      ${stat('Identity score', score.value != null ? percent(score.value) : null, score.value != null ? scoreTone(score.value) : null,
        score.checks ? `${score.checks} check${score.checks === 1 ? '' : 's'} in ${period}` : `no scored checks in ${period}`,
        {weight: score.value, title: 'Average weight the fingerprint gave the expected model.'})}
      ${stat('Availability', measured ? percent(metrics.success_rate, metrics.success_rate === 1 ? 0 : 1) : null, measured ? availabilityTone(metrics.success_rate) : null,
        measured ? `${metrics.responded} of ${measured} probes answered` : `no probes in ${period}`,
        {title: 'Local runner failures are monitoring gaps, not outages.'})}
      ${stat('Time to first token', speed.ttft_ms != null ? seconds(speed.ttft_ms) : null, speed.ttft_tone,
        typical.ttft_ms != null ? `typical ${seconds(typical.ttft_ms)}` : 'no timing yet',
        {title: `Latest check. Hidden reasoning counts toward it. ${typicalSpeed(m)}.`})}
      ${stat('Decode speed', speed.output_tps != null ? `${rate(speed.output_tps)} tok/s` : null, speed.tps_tone,
        typical.output_tps != null ? `typical ${rate(typical.output_tps)} tok/s` : 'no timing yet',
        {title: `Latest check. ${typicalSpeed(m)}.`})}
    </div>

    <section class="panel">
      <h3>History · ${period}</h3>
      ${historyStrip(m)}
      ${timeAxis()}
      ${failures.length ? html`<p class="muted small">Failed probes: ${failures.map(([k, n]) => `${(OUTCOMES[k] ?? stateLabel(k)).toLowerCase()} ×${n}`).join(', ')}</p>` : ''}
    </section>`);
  tickRelative(pane);
}

async function loadHistory(monitor, more) {
  if (!more) drawer.history = {monitor, runs: [], next: null, loading: false, error: null};
  const state = drawer.history;
  if (state.loading) return;
  state.loading = true;
  state.error = null;
  renderHistory();
  try {
    const result = await request(`/api/monitors/${encodeURIComponent(monitor)}/history${more && state.next ? `?before=${state.next}` : ''}`);
    if (drawer.history !== state) return;
    state.runs.push(...result.runs);
    state.next = result.next_before;
  } catch (error) {
    if (drawer.history === state) state.error = error.message;
  } finally {
    state.loading = false;
  }
  if (drawer.history !== state) return;
  renderHistory();
  // The pager in the check pane depends on the loaded list.
  if (drawer.tab === 'check' && drawer.run && runCache.has(drawer.run)) $('#pane-check').innerHTML = String(checkMarkup(runCache.get(drawer.run)));
  tickRelative($('#drawer'));
}

function renderHistory() {
  const state = drawer.history;
  const pane = $('#pane-history');
  if (!state) return;
  let day = null;
  const entries = state.runs.map(r => {
    const s = shownState(r), top = r.candidates?.[0];
    const date = formatDay(r.started_at);
    const heading = date !== day ? html`<li class="history-day">${date}</li>` : '';
    day = date;
    return html`${heading}<li><button type="button" class="history-entry" data-goto="${r.id}" aria-current="${r.id === drawer.run}">
      <span class="num muted" title="${formatFull(r.started_at)}">${formatTime(r.started_at)}</span>
      <span class="history-state"><i class="dot tone-${stateTone(s)}" aria-hidden="true"></i>${stateLabel(s)}</span>
      <span class="history-closest">${top ? html`<span class="mono">${top.model}</span> <span class="num muted">${percent(top.weight)}</span>` : html`<span class="muted">—</span>`}</span>
      <span class="num muted">${speedText(r.speed)}</span>
    </button></li>`;
  });
  pane.innerHTML = String(html`
    ${state.runs.length ? html`<ol class="history-list">${entries}</ol>` : state.loading ? '' : html`<p class="muted">No checks recorded yet.</p>`}
    ${state.loading ? html`<div class="skeleton-block"></div>` : ''}
    ${state.error ? html`<p class="error-text">Couldn’t load history. ${state.error}</p>` : ''}
    ${state.next && !state.loading ? html`<button type="button" class="button" data-more>Load earlier checks</button>` : ''}`);
}

// ---------------------------------------------------------------------------
// Events

function setGroup(group) { ui.group = group; prefs.set('mt-group', group); render(); }
function setSort(sort) {
  ui.reverse = ui.sort === sort ? !ui.reverse : false;
  ui.sort = sort;
  prefs.set('mt-sort', sort);
  render();
}
function setWindow(windowName) {
  if (windowName === ui.window) return;
  ui.window = windowName;
  prefs.set('mt-window', windowName);
  renderControls();
  poll.now();
}

document.addEventListener('click', event => {
  const target = event.target.closest('button, a');
  if (!target) return;
  const d = target.dataset;
  if (d.group) setGroup(d.group);
  else if (d.window) setWindow(d.window);
  else if (d.sort) setSort(d.sort);
  else if (d.filter) { ui.filter = ui.filter === d.filter ? 'all' : d.filter; render(); }
  else if ('clearFilters' in d) { ui.filter = 'all'; ui.query = ''; $('#search').value = ''; render(); }
  else if ('retry' in d) poll.now();
  else if (d.open) openDrawer({monitor: d.open, run: null, tab: 'overview'});
  else if (d.bar !== undefined) {
    const b = viewsById.get(d.monitor)?.m.history[Number(d.bar)];
    ui.focusBar.set(d.monitor, Number(d.bar));
    if (b?.latest_id) openDrawer({monitor: d.monitor, run: b.latest_id, tab: 'check'});
  } else if (d.tab) selectTab(d.tab);
  else if (d.goto) openDrawer({monitor: drawer.monitor, run: d.goto, tab: 'check'});
  else if ('more' in d) loadHistory(drawer.monitor, true);
  else if ('reloadRun' in d) { runCache.delete(drawer.run); renderCheck(drawer.run); }
});

$('#sort-select').addEventListener('change', event => { ui.reverse = false; ui.sort = event.target.value; prefs.set('mt-sort', ui.sort); render(); });

$('#search').addEventListener('input', event => { ui.query = event.target.value.trim().toLowerCase(); render(); });
$('#search').addEventListener('keydown', event => {
  if (event.key === 'Escape' && event.target.value) { event.target.value = ''; ui.query = ''; render(); }
});
document.addEventListener('keydown', event => {
  if (event.key === '/' && !event.target.closest('input, select, textarea, dialog')) { event.preventDefault(); $('#search').focus(); }
  if (event.key === 'Escape') hideTip();
});

// Roving focus inside each history strip.
$('#monitors').addEventListener('keydown', event => {
  const bar = event.target.closest('[data-bar]');
  if (!bar) return;
  const siblings = $$('[data-bar]', bar.parentElement);
  const i = siblings.indexOf(bar);
  const next = {ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: siblings.length - 1}[event.key];
  if (next === undefined) return;
  event.preventDefault();
  const target = siblings[Math.max(0, Math.min(siblings.length - 1, next))];
  bar.tabIndex = -1;
  target.tabIndex = 0;
  ui.focusBar.set(bar.dataset.monitor, Number(target.dataset.bar));
  target.focus();
});

$('#drawer').addEventListener('keydown', event => {
  const tab = event.target.closest('[role=tab]');
  if (!tab || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  const tabs = $$('[role=tab]', drawer.el).filter(t => !t.disabled);
  const next = tabs[(tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
  if (next && next !== tab) { next.focus(); selectTab(next.dataset.tab); }
});

$('#monitors').addEventListener('pointerover', event => {
  const bar = event.target.closest('[data-bar]');
  if (bar && event.pointerType !== 'touch') showTip(bar);
});
$('#monitors').addEventListener('pointerout', event => { if (event.target.closest('[data-bar]')) hideTip(); });
$('#monitors').addEventListener('focusin', event => {
  const bar = event.target.closest('[data-bar]');
  if (bar && bar.matches(':focus-visible')) showTip(bar);
});
$('#monitors').addEventListener('focusout', hideTip);
addEventListener('scroll', hideTip, {passive: true});
addEventListener('resize', hideTip);

dialogControls($('#drawer'), {requestClose: closeDrawer});
addEventListener('popstate', syncFromHash);

renderControls();
setInterval(tick, 10000);
poll.start();
