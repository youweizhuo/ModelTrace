// Shared helpers for the status and management pages. No build step: plain ES modules.

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// ---------------------------------------------------------------------------
// Escaped HTML templates. Every interpolated value is escaped unless it is the
// result of another html`` template, so markup can be composed safely.

const ENTITIES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ENTITIES[c]);

class Markup {
  constructor(value) { this.value = value; }
  toString() { return this.value; }
}

function fragment(value) {
  if (value == null || value === false) return '';
  if (value instanceof Markup) return value.value;
  if (Array.isArray(value)) return value.map(fragment).join('');
  return escape(value);
}

export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => { out += fragment(value) + strings[i + 1]; });
  return new Markup(out);
}

// Replace an element's markup only when it changed, keeping keyboard focus on
// the element with the same data-key so periodic refreshes don't steal it.
export function patch(element, markup) {
  const next = fragment(markup);
  if (element.dataset.rendered === next) return false;
  const focused = document.activeElement;
  const key = element.contains(focused) ? focused?.dataset.key : null;
  element.innerHTML = next;
  element.dataset.rendered = next;
  if (key) element.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus({preventScroll: true});
  return true;
}

// ---------------------------------------------------------------------------
// Vocabulary shared by both pages.

export const STATES = {
  consistent: {label: 'Consistent', tone: 'good'},
  mismatch_signal: {label: 'Mismatch signal', tone: 'bad'},
  repeated_mismatch: {label: 'Repeated mismatch', tone: 'bad'},
  inconclusive: {label: 'Inconclusive', tone: 'info'},
  not_in_library: {label: 'Not in library', tone: 'warn'},
  unknown: {label: 'No result', tone: 'none'},
  unavailable: {label: 'Unavailable', tone: 'outage'},
  paused: {label: 'Paused', tone: 'none'},
  stale: {label: 'Stale', tone: 'none'},
  changed: {label: 'Awaiting check', tone: 'none'},
  checker_error: {label: 'Checker error', tone: 'warn'},
  budget_exhausted: {label: 'Daily limit reached', tone: 'warn'},
  interrupted: {label: 'Interrupted', tone: 'none'},
  running: {label: 'Checking', tone: 'none'},
};

export const AVAILABILITY = {
  available: {label: 'Available', tone: 'good'},
  partial: {label: 'Partial', tone: 'warn'},
  unavailable: {label: 'Unavailable', tone: 'outage'},
  unknown: {label: 'Unknown', tone: 'none'},
};

export const stateLabel = key => STATES[key]?.label ?? AVAILABILITY[key]?.label ?? String(key ?? '').replaceAll('_', ' ');
export const stateTone = key => STATES[key]?.tone ?? AVAILABILITY[key]?.tone ?? 'none';

export function badge(key, {label = stateLabel(key), tone = stateTone(key)} = {}) {
  return html`<span class="badge tone-${tone}"><i class="dot" aria-hidden="true"></i>${label}</span>`;
}

export const percent = (n, digits = 0) => n == null || !Number.isFinite(n) ? '—' : `${(n * 100).toFixed(digits)}%`;
export const seconds = ms => Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)} s` : '—';

export function intervalLabel(value) {
  if (value % 86400 === 0) return `${value / 86400} d`;
  if (value % 3600 === 0) return `${value / 3600} h`;
  return `${Math.round(value / 60)} min`;
}

// ---------------------------------------------------------------------------
// Time. Relative times use the server's clock so a skewed client clock can't
// make fresh results look old (or stale results look fresh).

let clockOffset = 0;
export const clock = {
  sync(serverSeconds) { if (Number.isFinite(serverSeconds)) clockOffset = serverSeconds - Date.now() / 1000; },
  now: () => Date.now() / 1000 + clockOffset,
};

function span(delta) {
  const s = Math.abs(delta);
  if (s < 60) return null;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function ago(timestamp) {
  if (!timestamp) return 'never';
  const text = span(clock.now() - timestamp);
  return text ? `${text} ago` : 'just now';
}

export function until(timestamp) {
  const delta = timestamp - clock.now();
  if (delta <= 0) return 'due now';
  return `in ${span(delta) ?? '<1m'}`;
}

const dateFormat = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
const timeFormat = new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit'});
export const formatDate = t => dateFormat.format(new Date(t * 1000));
const dayFormat = new Intl.DateTimeFormat(undefined, {weekday: 'short', month: 'short', day: 'numeric'});
export const formatDay = t => dayFormat.format(new Date(t * 1000));
export const formatTime = t => timeFormat.format(new Date(t * 1000));
export const formatFull = t => new Date(t * 1000).toLocaleString();
export function formatRange(start, end) {
  const a = new Date(start * 1000), b = new Date(end * 1000);
  return a.toDateString() === b.toDateString() ? `${dateFormat.format(a)} – ${timeFormat.format(b)}` : `${dateFormat.format(a)} – ${dateFormat.format(b)}`;
}

// Elements with data-ago / data-until hold timestamps and are refreshed by tickRelative().
export function tickRelative(root = document) {
  for (const el of $$('[data-ago]', root)) {
    const text = ago(Number(el.dataset.ago));
    if (el.textContent !== text) el.textContent = text;
  }
  for (const el of $$('[data-until]', root)) {
    const text = until(Number(el.dataset.until));
    if (el.textContent !== text) el.textContent = text;
  }
}

// ---------------------------------------------------------------------------
// Networking: bounded requests, JSON that tolerates non-JSON error pages, and
// errors whose messages are safe to show.

export class RequestError extends Error {
  constructor(message, {status = 0, kind = 'http', detail = null} = {}) {
    super(message);
    this.status = status;
    this.kind = kind;
    this.detail = detail;
  }
}

const STATUS_MESSAGES = {
  401: 'Your session has expired. Sign in again.',
  403: 'The request was refused. Reload the page and try again.',
  404: 'That item no longer exists. It may have been removed.',
  413: 'The request is too large.',
  429: 'Too many attempts. Wait a few minutes and try again.',
};

export async function request(url, {method = 'GET', body, headers = {}, timeout = 15000, signal} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort('timeout'), timeout);
  const onAbort = () => controller.abort('cancelled');
  signal?.addEventListener('abort', onAbort, {once: true});
  let response;
  try {
    response = await fetch(url, {
      method, cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
      headers: body === undefined ? headers : {'Content-Type': 'application/json', ...headers},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    const reason = controller.signal.reason;
    if (reason === 'cancelled') throw new RequestError('Request cancelled', {kind: 'cancelled'});
    if (reason === 'timeout') throw new RequestError('The server took too long to respond.', {kind: 'timeout'});
    throw new RequestError('Could not reach the server. Check your connection.', {kind: 'network'});
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
  const text = await response.text().catch(() => '');
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* e.g. a proxy's HTML error page */ }
  if (!response.ok) {
    // Only validation errors (400) carry messages written for people.
    const specific = response.status === 400 && typeof data?.error === 'string' ? data.error : null;
    const message = specific || STATUS_MESSAGES[response.status]
      || (response.status >= 500 ? `The server had a problem (HTTP ${response.status}). Try again shortly.` : `Request failed (HTTP ${response.status}).`);
    throw new RequestError(message, {status: response.status, detail: data});
  }
  if (data === null) throw new RequestError('The server sent an unreadable response.', {status: response.status, kind: 'parse'});
  return data;
}

// Poll with exponential backoff on failure; pause while the tab is hidden.
export function poller(task, {interval, retry = interval, maxBackoff = 60000}) {
  let timer = null, failures = 0, running = false, stopped = false, missed = false, again = false;
  async function run() {
    clearTimeout(timer);
    if (stopped) return;
    if (document.hidden) { missed = true; return; }
    if (running) { again = true; return; }  // e.g. settings changed mid-request
    running = true;
    try {
      await task();
      failures = 0;
    } catch {
      failures += 1;
    } finally {
      running = false;
      const delay = again ? 0 : failures ? Math.min(maxBackoff, retry * 2 ** (failures - 1)) : interval;
      again = false;
      if (!stopped) timer = setTimeout(run, delay);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && missed) { missed = false; run(); }
  });
  addEventListener('online', () => { if (failures) run(); });
  return {
    now: run,
    stop() { stopped = true; clearTimeout(timer); },
    start() { stopped = false; run(); },
    get failures() { return failures; },
  };
}

// ---------------------------------------------------------------------------
// Preferences (storage can throw in some private-browsing modes).

export const prefs = {
  get(key, allowed, fallback) {
    try {
      const value = localStorage.getItem(key);
      return allowed.includes(value) ? value : fallback;
    } catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
};

// ---------------------------------------------------------------------------
// Dialogs, toasts and confirmation.

// Route every close gesture (Escape, backdrop click, close buttons) through
// requestClose so callers can guard unsaved state or sync the URL.
export function dialogControls(dialog, {requestClose}) {
  dialog.addEventListener('cancel', event => { event.preventDefault(); requestClose(); });
  dialog.addEventListener('mousedown', event => { dialog.dataset.pressedBackdrop = String(event.target === dialog); });
  dialog.addEventListener('click', event => {
    if (event.target === dialog && dialog.dataset.pressedBackdrop === 'true') requestClose();
  });
  for (const button of $$('[data-close]', dialog)) button.addEventListener('click', () => requestClose());
}

export function toast(message, {tone = 'good', timeout = 4000} = {}) {
  let region = $('#toasts');
  if (!region) {
    region = document.createElement('div');
    region.id = 'toasts';
    region.className = 'toasts';
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    document.body.append(region);
  }
  const item = document.createElement('div');
  item.className = `toast tone-${tone}`;
  item.innerHTML = String(html`<i class="dot" aria-hidden="true"></i><span>${message}</span>`);
  region.append(item);
  const remove = () => { item.classList.add('leaving'); setTimeout(() => item.remove(), 200); };
  if (tone === 'bad') {
    const close = document.createElement('button');
    close.className = 'icon-button';
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', remove);
    item.append(close);
  } else {
    setTimeout(remove, timeout);
  }
}

export function confirmDialog({title, body, confirm = 'Confirm', danger = false}) {
  const dialog = document.createElement('dialog');
  dialog.className = 'confirm';
  dialog.innerHTML = String(html`<form method="dialog">
      <h2>${title}</h2><p>${body}</p>
      <div class="dialog-actions">
        <button value="cancel" class="button">Cancel</button>
        <button value="ok" class="button ${danger ? 'danger' : 'primary'}">${confirm}</button>
      </div></form>`);
  document.body.append(dialog);
  return new Promise(resolve => {
    dialog.addEventListener('close', () => { resolve(dialog.returnValue === 'ok'); dialog.remove(); }, {once: true});
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close('cancel'); });
    dialog.showModal();
    dialog.querySelector('button[value="cancel"]').focus();
  });
}
