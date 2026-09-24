import {
  $, $$, html, patch, badge, intervalLabel, ago, formatFull, tickRelative, clock,
  request, poller, dialogControls, toast, confirmDialog,
} from './common.js';

let csrf = '';
let providers = [];
let activity = null;
let activityError = null;
let editing = null;
let baseline = '';
let query = '';
const expanded = new Set();
const submitting = new Set();
const checkErrors = new Map();

// ---------------------------------------------------------------------------
// API and session

async function api(path, {method = 'GET', body, retry = true} = {}) {
  try {
    return await request(`/api/admin/${path}`, {method, body, headers: method === 'GET' ? {} : {'X-CSRF-Token': csrf}});
  } catch (error) {
    if (error.status === 403 && method !== 'GET' && retry && path !== 'login') {
      // The CSRF token rotates when this browser signs in again (e.g. in another tab).
      const session = await request('/api/admin/session');
      csrf = session.csrf;
      if (session.authenticated) return api(path, {method, body, retry: false});
      error.status = 401;
    }
    if (error.status === 401 && path !== 'login') expired();
    throw error;
  }
}

function showLogin(message = '') {
  poll.stop();
  $('#management').hidden = true;
  $('#login').hidden = false;
  $('#sign-out').hidden = true;
  $('#live').hidden = true;
  $('#login-error').textContent = message;
  $('#login-form').elements.password.focus();
}

async function showManagement() {
  $('#login').hidden = true;
  $('#management').hidden = false;
  $('#sign-out').hidden = false;
  $('#live').hidden = false;
  await reload();
  poll.start();
}

async function reload() {
  try {
    await load();
  } catch (error) {
    if (error.status !== 401) patch($('#providers'), html`<div class="empty provider-card"><p>Couldn’t load providers. ${error.message}</p><button type="button" class="button" data-reload>Try again</button></div>`);
  }
}

function expired() {
  if ($('#management').hidden) return;
  const lost = dirty();
  if ($('#editor').open) $('#editor').close();
  showLogin(lost ? 'Your session expired, so unsaved changes were discarded. Sign in again.' : 'Your session expired. Sign in again.');
}

async function load() {
  const result = await api('providers');
  providers = result.providers;
  patch($('#reference-models'), result.models.map(m => html`<option value="${m}"></option>`));
  render();
  applyActivity(result.activity);
}

// ---------------------------------------------------------------------------
// Rendering

function visibleProviders() {
  if (!query) return providers;
  return providers.filter(p => [p.name, p.base_url, ...p.monitors.flatMap(m => [m.model, m.expected_model, m.channel])]
    .join(' ').toLowerCase().includes(query));
}

function render() {
  const models = providers.reduce((n, p) => n + p.monitors.length, 0);
  const paused = providers.filter(p => !p.enabled).length;
  $('#provider-summary').textContent = providers.length
    ? `${providers.length} provider${providers.length === 1 ? '' : 's'} · ${models} model${models === 1 ? '' : 's'}${paused ? ` · ${paused} paused` : ''}`
    : 'No providers configured yet.';
  const list = visibleProviders();
  if (!providers.length) {
    patch($('#providers'), html`<div class="empty provider-card"><p>Add a provider to start scheduled identity checks.</p><button type="button" class="button primary" data-add>+ Add provider</button></div>`);
    return;
  }
  if (!list.length) {
    patch($('#providers'), html`<div class="empty provider-card"><p>No providers match “${query}”.</p></div>`);
    return;
  }
  patch($('#providers'), list.map(providerCard));
}

function providerCard(p) {
  const open = expanded.has(p.id);
  return html`<article class="provider-card${open ? ' is-open' : ''}" aria-labelledby="provider-${p.id}">
    <header class="provider-head">
      <h2 class="provider-title"><button type="button" class="provider-toggle" data-expand="${p.id}" data-key="expand:${p.id}" aria-expanded="${open}" aria-controls="config-${p.id}" title="${open ? 'Hide' : 'Show'} configuration">
        <span class="chevron" aria-hidden="true"></span>
        <span class="provider-name" id="provider-${p.id}">${p.name}</span>
        ${p.enabled ? '' : badge('paused')}
      </button></h2>
      <div class="provider-actions">
        <button type="button" class="button small" data-check-all="${p.id}" data-key="check-all:${p.id}" ${p.enabled && p.monitors.some(m => m.enabled) ? '' : 'disabled'}>Check all</button>
        <button type="button" class="button small" data-edit="${p.id}" data-key="edit:${p.id}">Edit</button>
        <button type="button" class="button quiet small" data-provider-toggle="${p.id}" data-key="ptoggle:${p.id}" aria-label="${p.enabled ? 'Pause' : 'Resume'} provider ${p.name}">${p.enabled ? 'Pause' : 'Resume'}</button>
      </div>
    </header>
    <section class="provider-config" id="config-${p.id}" aria-label="${p.name} configuration" ${open ? '' : 'hidden'}>
      <dl class="config-facts">
        <div><dt>API base URL</dt><dd class="mono">${p.base_url}</dd></div>
      </dl>
      <div class="config-table" role="table" aria-label="Model configuration">
        <div class="config-row config-head" role="row">
          <span role="columnheader">Requested model</span><span role="columnheader">Expected model</span><span role="columnheader">Reasoning</span>
          <span role="columnheader">Interval</span><span role="columnheader">Channel</span>
        </div>
        ${p.monitors.map(m => html`<div class="config-row" role="row">
          <span role="cell" class="mono">${m.model}</span>
          <span role="cell" data-label="Expected" class="mono${m.expected_model === m.model ? ' muted' : ''}">${m.expected_model === m.model ? 'Same' : m.expected_model}</span>
          <span role="cell" data-label="Reasoning">${m.effort}</span>
          <span role="cell" data-label="Interval" class="num">${intervalLabel(m.interval)}</span>
          <span role="cell" data-label="Channel">${m.channel}</span>
        </div>`)}
      </div>
    </section>
    ${p.monitors.map(m => modelRow(p, m))}
  </article>`;
}

function modelRow(p, m) {
  // Full settings live in the configuration pane; only show what tells rows of the same model apart.
  const twins = p.monitors.filter(x => x.model === m.model && x.id !== m.id);
  const meta = [
    m.channel !== 'Standard' || twins.length ? m.channel : null,
    twins.some(x => x.channel === m.channel) ? m.effort : null,
  ].filter(Boolean).join(' · ');
  const name = `${p.name} / ${m.model}${meta ? ` (${meta})` : ''}`;
  return html`<div class="model-row${m.enabled && p.enabled ? '' : ' is-paused'}">
    <div class="m-model"><span class="mono">${m.model}</span>${meta ? html`<div class="sub">${meta}</div>` : ''}</div>
    <div class="m-identity" data-identity="${m.id}"></div>
    <div class="m-activity" data-activity="${m.id}" aria-live="polite"></div>
    <div class="m-actions">
      <button type="button" class="button small" data-check="${m.id}" data-name="${name}" data-key="check:${m.id}">Check</button>
      <button type="button" class="button quiet small" data-model-toggle="${m.id}" data-key="mtoggle:${m.id}" aria-label="${m.enabled ? 'Pause' : 'Resume'} ${name}">${m.enabled ? 'Pause' : 'Resume'}</button>
    </div>
  </div>`;
}

function resultLabel(state) {
  if (state.run_state === 'interrupted') return ['Stopped', 'none'];
  if (state.run_state === 'budget_exhausted') return ['Daily limit', 'warn'];
  if (state.run_state !== 'completed' || state.availability === 'unknown') return ['Check error', 'warn'];
  if (state.availability === 'unavailable') return ['Failed', 'outage'];
  if (state.availability === 'partial') return ['Partial', 'warn'];
  return ['Done', 'good'];
}

function applyActivity(next) {
  if (next && (!activity || next.at >= activity.at)) {
    activity = next;
    clock.sync(next.at);
  }
  const live = $('#live');
  live.dataset.state = activityError ? 'error' : !activity ? 'connecting' : activity.worker_online ? 'live' : 'offline';
  $('#live-text').textContent = activityError ? 'Status unavailable' : !activity ? 'Connecting…' : activity.worker_online ? 'Worker online' : 'Worker offline';

  for (const button of $$('[data-check]')) {
    const mid = button.dataset.check;
    const state = activity?.monitors[mid];
    const provider = providers.find(p => p.monitors.some(m => m.id === mid));
    let label = 'Check', busy = false, cell;
    if (submitting.has(mid)) {
      label = 'Queuing';
      busy = true;
      cell = html`<span>Queuing…</span><div class="progress indeterminate"><i></i></div>`;
    } else if (state?.state === 'running') {
      const verb = !state.enabled ? 'Finishing' : state.kind === 'confirmation' ? 'Confirming' : 'Checking';
      label = 'Checking';
      busy = true;
      cell = html`<span class="num">${verb} ${state.completed_samples}/${state.planned_samples}</span><div class="progress"><i style="width:${Math.round(100 * state.completed_samples / Math.max(1, state.planned_samples))}%"></i></div>`;
    } else if (state?.state === 'queued') {
      label = 'Queued';
      busy = true;
      cell = html`<span>${activity.worker_online ? 'Queued' : 'Queued · worker offline'}</span>`;
    } else if (checkErrors.has(mid)) {
      cell = html`<span class="error-text" title="${checkErrors.get(mid)}">Couldn’t queue check</span>`;
    } else if (!state) {
      cell = html`<span class="muted">Status unavailable</span>`;
    } else {
      const last = state.finished_at ? resultLabel(state) : null;
      const schedule = !state.enabled ? (provider?.enabled ? 'paused' : 'provider paused') : state.next_due ? html`next <span data-until="${state.next_due}"></span>` : 'due now';
      const outcome = last && last[0] !== 'Done' ? html`<i class="dot tone-${last[1]}" aria-hidden="true"></i> ${last[0]} · ` : '';
      cell = html`${last ? html`<span title="${formatFull(state.finished_at)}">${outcome}<span data-ago="${state.finished_at}">${ago(state.finished_at)}</span></span>` : html`<span class="muted">No checks yet</span>`}<span class="muted"> · ${schedule}</span>`;
    }
    patch($(`[data-activity="${CSS.escape(mid)}"]`), cell);
    const identity = state?.identity
      ? html`<a href="/?monitor=${encodeURIComponent(mid)}&run=${encodeURIComponent(state.identity_run_id)}" title="Open on the status page">${badge(state.identity)}</a>`
      : html`<span class="muted small">No result yet</span>`;
    patch($(`[data-identity="${CSS.escape(mid)}"]`), identity);
    // The activity column shows progress; the button only reflects availability.
    button.setAttribute('aria-label', busy ? `${label} · ${button.dataset.name}` : `Check ${button.dataset.name} now`);
    button.disabled = busy || !state?.enabled;
    button.dataset.busy = String(busy);
  }
  tickRelative($('#providers'));
}

async function refreshActivity() {
  if ($('#management').hidden) return;
  try {
    const next = await api('activity');
    activityError = null;
    applyActivity(next);
  } catch (error) {
    activityError = error;
    applyActivity();
    throw error;
  }
}

const poll = poller(refreshActivity, {interval: 2000, retry: 2000, maxBackoff: 15000});

// ---------------------------------------------------------------------------
// Actions

async function queueCheck(mid) {
  submitting.add(mid);
  checkErrors.delete(mid);
  applyActivity();
  try {
    const result = await api(`monitors/${encodeURIComponent(mid)}/check`, {method: 'POST', body: {}});
    submitting.delete(mid);
    applyActivity(result.activity);
    return result.state;
  } catch (error) {
    submitting.delete(mid);
    checkErrors.set(mid, error.message);
    applyActivity();
    throw error;
  }
}

async function withButton(button, busyLabel, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  try {
    await action();
  } catch (error) {
    if (error.status !== 401) toast(error.message, {tone: 'bad'});
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

document.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  const d = button.dataset;
  if ('add' in d || button.id === 'add-provider') openEditor(null);
  else if ('reload' in d) reload();
  else if (d.expand) {
    if (!expanded.delete(d.expand)) expanded.add(d.expand);
    render();
    applyActivity();
  } else if (d.edit) openEditor(providers.find(p => p.id === d.edit));
  else if (d.providerToggle) {
    const p = providers.find(x => x.id === d.providerToggle);
    await withButton(button, p.enabled ? 'Pausing…' : 'Resuming…', async () => {
      await api(`providers/${encodeURIComponent(p.id)}`, {method: 'PATCH', body: {enabled: !p.enabled}});
      await load();
      toast(`${p.name} ${p.enabled ? 'paused' : 'resumed'}.`);
    });
  } else if (d.modelToggle) {
    const m = providers.flatMap(p => p.monitors).find(x => x.id === d.modelToggle);
    await withButton(button, m.enabled ? 'Pausing…' : 'Resuming…', async () => {
      await api(`monitors/${encodeURIComponent(m.id)}`, {method: 'PATCH', body: {enabled: !m.enabled}});
      await load();
      toast(`${m.model} ${m.enabled ? 'paused' : 'resumed'}.`);
    });
  } else if (d.check) {
    queueCheck(d.check).catch(error => { if (error.status !== 401) toast(`Couldn’t queue ${d.name}: ${error.message}`, {tone: 'bad'}); });
  } else if (d.checkAll) {
    const p = providers.find(x => x.id === d.checkAll);
    await withButton(button, 'Queuing…', async () => {
      const targets = p.monitors.filter(m => activity?.monitors[m.id]?.enabled && !['running', 'queued'].includes(activity.monitors[m.id].state));
      const results = await Promise.allSettled(targets.map(m => queueCheck(m.id)));
      const queued = results.filter(r => r.status === 'fulfilled' && r.value === 'queued').length;
      const failed = results.filter(r => r.status === 'rejected').length;
      toast(failed ? `Queued ${queued} checks; ${failed} failed.` : queued ? `Queued ${queued} check${queued === 1 ? '' : 's'} for ${p.name}.` : 'All models are already queued or checking.', {tone: failed ? 'bad' : 'good'});
    });
  } else if (d.reveal) {
    const input = button.closest('form').elements[d.reveal];
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', String(show));
  }
});

$('#search').addEventListener('input', event => {
  query = event.target.value.trim().toLowerCase();
  render();
  applyActivity();
});

$('#sign-out').addEventListener('click', async () => {
  try { await api('logout', {method: 'POST', body: {}}); } catch { /* signing out locally is enough */ }
  location.reload();
});

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const button = event.submitter ?? form.querySelector('[type=submit]');
  if (!form.elements.password.value) { $('#login-error').textContent = 'Enter the admin password.'; return; }
  button.disabled = true;
  button.textContent = 'Signing in…';
  $('#login-error').textContent = '';
  try {
    const result = await api('login', {method: 'POST', body: {password: form.elements.password.value}});
    csrf = result.csrf;
    form.reset();
    await showManagement();
  } catch (error) {
    $('#login-error').textContent = error.status === 401 ? 'Incorrect password.' : error.message;
    form.elements.password.select();
  } finally {
    button.disabled = false;
    button.textContent = 'Sign in';
  }
});

// ---------------------------------------------------------------------------
// Provider editor

const form = $('#provider-form');
const editors = $('#model-editors');

function addModel(model = {}) {
  const item = $('#model-template').content.firstElementChild.cloneNode(true);
  item.dataset.id = model.id ?? '';
  const field = name => item.querySelector(`[data-field="${name}"]`);
  field('model').value = model.model ?? '';
  field('expected_model').value = model.expected_model && model.expected_model !== model.model ? model.expected_model : '';
  field('effort').value = model.effort ?? 'medium';
  field('interval').value = Math.round((model.interval ?? 3600) / 60);
  field('channel').value = model.channel ?? 'Standard';
  field('enabled').checked = model.enabled === undefined ? true : Boolean(model.enabled);
  item.querySelector('[data-remove]').addEventListener('click', () => {
    item.remove();
    updateModelTitles();
    editors.querySelector('[data-field="model"]')?.focus();
  });
  field('model').addEventListener('input', updateModelTitles);
  editors.append(item);
  updateModelTitles();
  return item;
}

function updateModelTitles() {
  const items = [...editors.children];
  items.forEach((item, i) => {
    const name = item.querySelector('[data-field="model"]').value.trim();
    item.querySelector('[data-title]').textContent = `Model ${i + 1}${name ? ` · ${name}` : ''}`;
    const remove = item.querySelector('[data-remove]');
    remove.disabled = items.length === 1;
    remove.title = items.length === 1 ? 'A provider needs at least one model' : '';
  });
  $('#model-count').textContent = items.length ? `(${items.length})` : '';
  $('#add-model').disabled = items.length >= 30;
}

function payload() {
  return {
    name: form.elements.name.value.trim(),
    base_url: form.elements.base_url.value.trim(),
    api_key: form.elements.api_key.value.trim(),
    enabled: form.elements.enabled.checked,
    monitors: [...editors.children].map(item => {
      const get = name => item.querySelector(`[data-field="${name}"]`);
      const model = get('model').value.trim();
      return {
        id: item.dataset.id || undefined, model,
        expected_model: get('expected_model').value.trim() || model,
        effort: get('effort').value, channel: get('channel').value.trim(),
        interval: Math.round(Number(get('interval').value) * 60), enabled: get('enabled').checked,
      };
    }),
  };
}

const dirty = () => $('#editor').open && JSON.stringify(payload()) !== baseline;

function clearErrors() {
  for (const el of $$('[data-error]', form)) el.textContent = '';
  for (const el of $$('[aria-invalid]', form)) el.removeAttribute('aria-invalid');
  for (const el of $$('.model-editor.is-invalid', form)) el.classList.remove('is-invalid');
  $('#save-error').textContent = '';
}

function validate() {
  clearErrors();
  let first = null;
  const fail = (input, scope, key, message) => {
    input.setAttribute('aria-invalid', 'true');
    scope.querySelector(`[data-error="${key}"]`).textContent = message;
    input.closest('.model-editor')?.classList.add('is-invalid');
    first ??= input;
  };
  const el = form.elements;
  const data = payload();
  if (!data.name) fail(el.name, form, 'name', 'Enter a provider name.');
  let url = null;
  try { url = new URL(data.base_url); } catch { /* reported below */ }
  if (!data.base_url) fail(el.base_url, form, 'base_url', 'Enter the API base URL.');
  else if (!url || !['http:', 'https:'].includes(url.protocol)) fail(el.base_url, form, 'base_url', 'Use an http:// or https:// URL.');
  else if (url.username || url.password) fail(el.base_url, form, 'base_url', 'Don’t put credentials in the URL; use the API key field.');
  else if (url.search || url.hash) fail(el.base_url, form, 'base_url', 'Remove the query string or fragment.');
  if (!editing && !data.api_key) fail(el.api_key, form, 'api_key', 'An API key is required for a new provider.');
  const seen = new Map();
  [...editors.children].forEach((item, i) => {
    const m = data.monitors[i];
    const get = name => item.querySelector(`[data-field="${name}"]`);
    const minutes = Number(get('interval').value);
    if (!m.model) fail(get('model'), item, 'model', 'Enter the model name to request.');
    else if (m.model.length > 160) fail(get('model'), item, 'model', 'Use at most 160 characters.');
    if (m.expected_model.length > 160) fail(get('expected_model'), item, 'expected_model', 'Use at most 160 characters.');
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 10080) fail(get('interval'), item, 'interval', 'Use whole minutes from 1 to 10080 (one week).');
    if (!m.channel) fail(get('channel'), item, 'channel', 'Enter a channel label, e.g. Standard.');
    const signature = [m.model, m.channel, m.effort].join('\u0000');
    if (m.model && seen.has(signature)) fail(get('model'), item, 'model', `Same model, channel and reasoning as model ${seen.get(signature) + 1}.`);
    else seen.set(signature, i);
  });
  if (first) {
    $('#save-error').textContent = 'Fix the highlighted fields.';
    first.focus();
  }
  return !first;
}

function openEditor(provider) {
  editing = provider;
  form.reset();
  clearErrors();
  const el = form.elements;
  el.name.value = provider?.name ?? '';
  el.base_url.value = provider?.base_url ?? '';
  el.enabled.checked = provider ? Boolean(provider.enabled) : true;
  el.api_key.type = 'password';
  for (const b of $$('[data-reveal]', form)) { b.textContent = 'Show'; b.setAttribute('aria-pressed', 'false'); }
  el.api_key.placeholder = provider ? 'Saved key — leave blank to keep it' : '';
  $('#key-hint').textContent = provider
    ? 'Enter a new key only to replace it. Changing the key or URL invalidates current results until a new check completes.'
    : 'Stored in the server’s private database and never sent back to the browser.';
  editors.replaceChildren();
  (provider?.monitors.length ? provider.monitors : [{}]).forEach(addModel);
  $('#editor-title').textContent = provider ? `Edit ${provider.name}` : 'Add provider';
  $('#editor-subtitle').textContent = provider ? provider.base_url : 'Connect an API and choose the models to monitor.';
  $('#remove-provider').hidden = !provider;
  $('#save-provider').textContent = provider ? 'Save changes' : 'Add provider';
  baseline = JSON.stringify(payload());
  $('#editor').showModal();
  if (!provider) el.name.focus();
}

async function requestCloseEditor() {
  if (dirty() && !await confirmDialog({title: 'Discard changes?', body: 'Your edits to this provider haven’t been saved.', confirm: 'Discard', danger: true})) return;
  $('#editor').close();
}

dialogControls($('#editor'), {requestClose: requestCloseEditor});
$('#editor').addEventListener('close', () => {
  form.elements.api_key.value = '';
  editing = null;
  baseline = '';
});
$('#add-model').addEventListener('click', () => addModel({effort: 'medium'}).querySelector('[data-field="model"]').focus());

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!validate()) return;
  const button = $('#save-provider');
  const data = payload();
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Saving…';
  try {
    await api(editing ? `providers/${encodeURIComponent(editing.id)}` : 'providers', {method: editing ? 'PUT' : 'POST', body: data});
    baseline = JSON.stringify(data);
    $('#editor').close();
    await load();
    toast(`Saved ${data.name}.`);
  } catch (error) {
    if (error.status !== 401) $('#save-error').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

$('#remove-provider').addEventListener('click', async () => {
  const provider = editing;
  if (!provider) return;
  const ok = await confirmDialog({
    title: `Remove ${provider.name}?`,
    body: 'Its API key and model monitors will be deleted. Existing check history stays in the database.',
    confirm: 'Remove provider', danger: true,
  });
  if (!ok) return;
  try {
    await api(`providers/${encodeURIComponent(provider.id)}`, {method: 'DELETE'});
    baseline = '';
    $('#editor').close();
    await load();
    toast(`Removed ${provider.name}.`);
  } catch (error) {
    if (error.status !== 401) $('#save-error').textContent = error.message;
  }
});

addEventListener('beforeunload', event => { if (dirty()) event.preventDefault(); });
setInterval(() => tickRelative($('#providers')), 10000);

// ---------------------------------------------------------------------------
// Start

request('/api/admin/session')
  .then(async result => {
    csrf = result.csrf;
    if (result.authenticated) await showManagement();
    else showLogin();
  })
  .catch(error => showLogin(`Couldn’t connect to the management service. ${error.message}`));
