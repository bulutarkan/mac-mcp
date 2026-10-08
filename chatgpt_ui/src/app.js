import { McpAppBridge } from './bridge.js';

const $ = (id) => document.getElementById(id);
const all = (selector) => Array.from(document.querySelectorAll(selector));
const isNum = (x) => x != null && x !== '' && Number.isFinite(Number(x));
const formatInt = (x) => isNum(x) ? new Intl.NumberFormat('en-US').format(Number(x)) : '—';
const compact = (x) => isNum(x)
  ? new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(x))
  : 'n/a';
const latency = (ms) => !isNum(ms) ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
const PROVIDERS = { codex: 'Codex', opencode: 'OpenCode', chatgpt: 'ChatGPT Web' };
const ACTIVE = new Set(['running', 'starting']);
const FAILED = new Set(['failed', 'error', 'cancelled', 'canceled', 'timeout', 'killed']);
const state = { data: null, connected: false, busy: false, editing: false, catalog: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
function add(parent, ...children) { children.forEach(child => parent.appendChild(child)); return parent; }
function uptime(seconds) {
  if (!isNum(seconds)) return '—';
  const s = Number(seconds), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function setStatus(mode) {
  state.connected = mode === 'online';
  $('status').classList.toggle('online', mode === 'online');
  $('status').classList.toggle('offline', mode === 'offline');
  $('status-text').textContent = { online: 'Online', offline: 'Offline' }[mode] || 'Connecting';
  updateControls();
}
function updateControls() {
  $('refresh').disabled = !state.connected || state.busy;
  $('refresh').classList.toggle('spinning', state.busy);
  $('agent-save').disabled = !state.connected || state.busy || !state.catalog;
}
function notify(message, error = false) {
  const item = $('notice');
  item.hidden = !message;
  item.textContent = message || '';
  item.classList.toggle('error', Boolean(error));
}
function switchTab(tab) {
  if (!all('[data-panel]').some(s => s.dataset.panel === tab)) return;
  all('[data-tab]').forEach(b => b.classList.toggle('selected', b.dataset.tab === tab));
  all('[data-panel]').forEach(s => { s.hidden = s.dataset.panel !== tab; });
}
all('[data-tab]').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
all('[data-goto]').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.goto)));

function renderAgents(agents, target, limit) {
  const container = $(target);
  container.replaceChildren();
  if (!agents.length) { add(container, el('p', 'empty', 'No delegated agents yet.')); return; }
  for (const row of agents.slice(0, limit)) {
    const status = String(row.status || 'unknown').toLowerCase();
    const tone = ACTIVE.has(status) ? 'active' : FAILED.has(status) ? 'failed' : 'done';
    const main = add(el('div', 'row-main'),
      el('strong', null, row.role || 'Agent'),
      el('small', null, `${PROVIDERS[row.provider] || row.provider || 'unknown'} · ${row.model || 'default'}`));
    add(container, add(el('div', 'row'), el('i', `dot ${tone}`), main, el('span', 'row-end', status)));
  }
}
function renderProviders(providers, target, detailed) {
  const container = $(target);
  container.replaceChildren();
  if (!providers.length) { add(container, el('p', 'empty', 'No provider usage reported.')); return; }
  const max = Math.max(1, ...providers.map(p => Number(p.total_tokens) || 0));
  for (const row of providers) {
    const total = isNum(row.total_tokens) ? Number(row.total_tokens) : null;
    const line = add(el('div', 'provider-line'),
      el('strong', null, PROVIDERS[row.name] || row.name || 'Unknown'),
      el('span', 'row-end big', compact(total)));
    const meta = detailed
      ? `${formatInt(row.turns || 0)} turns · in ${compact(row.input_tokens)} · out ${compact(row.output_tokens)}`
      : `${formatInt(row.turns || 0)} turns`;
    const bar = add(el('div', 'bar'), el('i'));
    bar.firstChild.style.width = `${total ? Math.max(2, total / max * 100) : 0}%`;
    add(container, add(el('div', 'row provider'), add(el('div', 'row-main'), line, el('small', null, meta), bar)));
  }
}

function hydrate(data) {
  if (!data || data.ok !== true || !data.stats) return false;
  state.data = data;
  const s = data.stats;
  $('version').textContent = data.version ? `v${data.version}` : '—';
  $('calls').textContent = formatInt(s.calls);
  $('active-agents').textContent = formatInt(data.active_agents);
  $('latency').textContent = latency(s.latency_ms);
  $('errors').textContent = formatInt(s.errors);
  $('usage-calls').textContent = formatInt(s.calls);
  $('usage-active').textContent = formatInt(s.active_calls);
  $('usage-latency').textContent = latency(s.latency_ms);
  $('usage-errors').textContent = formatInt(s.errors);
  $('usage-uptime').textContent = uptime(s.uptime_seconds);
  const active = Number(data.active_agents) || 0;
  $('tab-agents').hidden = active === 0;
  $('tab-agents').textContent = String(active);
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const providers = Array.isArray(data.providers) ? data.providers : [];
  renderAgents(agents, 'overview-agents', 4);
  renderAgents(agents, 'agents', 20);
  renderProviders(providers, 'overview-providers', false);
  renderProviders(providers, 'usage-providers', true);
  renderSettings(data);
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  setStatus('online');
  return true;
}
function unpack(result) {
  if (result?.isError) throw new Error(result.content?.find(x => x.type === 'text')?.text || 'MCP tool failed');
  if (result?.structuredContent) return result.structuredContent;
  const text = result?.content?.find(x => x.type === 'text')?.text;
  if (!text) return result;
  try { return JSON.parse(text); } catch { return result; }
}

const app = new McpAppBridge({ name: 'Mac MCP', version: '0.2.1' });
// Host chrome (e.g. the desktop composer over a fullscreen app) is reported as safe-area insets.
app.onhostcontext = (context) => {
  const insets = context.safeAreaInsets;
  if (insets) for (const side of ['top', 'right', 'bottom', 'left']) {
    const value = Number(insets[side]);
    if (Number.isFinite(value) && value >= 0) document.documentElement.style.setProperty(`--safe-${side}`, `${value}px`);
  }
  if (context.displayMode) document.documentElement.dataset.mode = String(context.displayMode);
};
app.ontoolresult = (result) => {
  try { hydrate(unpack(result)); } catch (error) { notify(String(error.message || error), true); }
};
async function call(name, args = {}) {
  const hints = {
    mac_mcp_panel_state: 'Refresh Mac MCP Control Center status and usage',
    mac_mcp_panel_setting: 'Save Mac MCP default agent from the Control Center',
  };
  const result = await app.callServerTool({ name, arguments: hints[name] ? { ...args, description: hints[name] } : args });
  return unpack(result);
}
async function refresh() {
  if (!state.connected || state.busy) return;
  state.busy = true;
  updateControls();
  try { hydrate(await call('mac_mcp_panel_state')); notify(''); }
  catch (error) { notify(`Refresh failed: ${error.message || error}`, true); }
  finally { state.busy = false; updateControls(); }
}
$('refresh').addEventListener('click', refresh);

// Settings: one writable preference (default agent); the rest is read-only status.
const onOff = (node, value) => { node.textContent = value ? 'On' : 'Off'; node.className = `mono ${value ? 'on' : 'off'}`; };
function renderSettings(data) {
  const s = data.settings || {};
  const agent = s.default_agent || {};
  $('agent-provider').textContent = agent.provider ? (PROVIDERS[agent.provider] || agent.provider) : 'Not set';
  $('agent-model').textContent = agent.model || 'Provider default';
  $('agent-reasoning').textContent = agent.reasoning || 'Default';
  const writable = Boolean(data.settings_writable) && (s.enabled_providers || []).length > 0;
  $('agent-edit').hidden = !writable || state.editing;
  $('agent-mode').hidden = writable;
  if (!writable && state.editing) closeEditor();
  onOff($('notify-agents'), s.notifications?.agent_completion);
  onOff($('notify-bubble'), s.notifications?.activity_bubble);
  const c = s.connection || {};
  $('conn-version').textContent = data.version ? `v${data.version}` : '—';
  $('conn-profile').textContent = c.profile || '—';
  $('conn-endpoint').textContent = c.public_host ? `${c.endpoint_mode} · ${c.public_host}` : (c.endpoint_mode || 'local');
}
function selectedProvider() {
  return $('agent-providers').querySelector('[aria-checked="true"]')?.dataset.provider || null;
}
function fillReasoning() {
  const model = state.catalog?.models.find(m => m.id === $('agent-model-input').value.trim());
  const values = model ? model.reasoning : (state.catalog?.reasoning || []);
  const current = state.pendingReasoning ?? $('agent-reasoning-input').value;
  const fallback = model?.default_reasoning ? `Default (${model.default_reasoning})` : 'Default';
  $('agent-reasoning-input').replaceChildren(el('option', null, fallback), ...values.map(v => {
    const option = el('option', null, v); option.value = v; return option;
  }));
  $('agent-reasoning-input').firstChild.value = '';
  $('agent-reasoning-input').value = values.includes(current) ? current : '';
  $('agent-reasoning-input').disabled = values.length === 0;
}
async function loadCatalog(provider) {
  state.catalog = null;
  $('agent-models').replaceChildren();
  $('agent-catalog-note').textContent = 'Loading models…';
  updateControls();
  try {
    const catalog = await call('mac_mcp_panel_state', { models_for: provider });
    if (selectedProvider() !== provider) return;
    state.catalog = catalog;
    $('agent-models').replaceChildren(...catalog.models.map(m => {
      const option = el('option'); option.value = m.id; if (m.label) option.label = m.label; return option;
    }));
    $('agent-catalog-note').textContent = catalog.models.length
      ? (catalog.truncated
        ? `Showing ${formatInt(catalog.models.length)} of ${formatInt(catalog.total)} models — type an exact id for others.`
        : `${formatInt(catalog.models.length)} models discovered.`)
      : 'No models discovered; the provider default will be used.';
  } catch (error) {
    $('agent-catalog-note').textContent = `Models unavailable: ${error.message || error}`;
  }
  fillReasoning();
  state.pendingReasoning = null;
  updateControls();
}
function chooseProvider(provider, keepSelection = false) {
  all('#agent-providers button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.provider === provider)));
  if (!keepSelection) { $('agent-model-input').value = ''; $('agent-reasoning-input').value = ''; state.pendingReasoning = null; }
  void loadCatalog(provider);
}
function openEditor() {
  const s = state.data?.settings || {};
  const enabled = s.enabled_providers || [];
  const current = enabled.includes(s.default_agent?.provider) ? s.default_agent.provider : enabled[0];
  $('agent-providers').replaceChildren(...enabled.map(name => {
    const button = el('button', null, PROVIDERS[name] || name);
    Object.assign(button, { type: 'button' });
    button.dataset.provider = name;
    button.setAttribute('role', 'radio');
    button.addEventListener('click', () => { if (selectedProvider() !== name) chooseProvider(name); });
    return button;
  }));
  const saved = current === s.default_agent?.provider;
  $('agent-model-input').value = saved ? (s.default_agent?.model || '') : '';
  state.pendingReasoning = saved ? (s.default_agent?.reasoning || '') : '';
  $('agent-reasoning-input').replaceChildren();
  state.editing = true;
  $('agent-view').hidden = true;
  $('agent-form').hidden = false;
  $('agent-edit').hidden = true;
  chooseProvider(current, true);
}
function closeEditor() {
  state.editing = false;
  state.catalog = null;
  $('agent-form').hidden = true;
  $('agent-view').hidden = false;
  if (state.data) renderSettings(state.data);
}
$('agent-edit').addEventListener('click', openEditor);
$('agent-cancel').addEventListener('click', closeEditor);
$('agent-model-input').addEventListener('input', fillReasoning);
$('agent-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy || !state.connected || !state.catalog) return;
  const provider = selectedProvider();
  const model = $('agent-model-input').value.trim();
  if (model && !state.catalog.truncated && !state.catalog.models.some(m => m.id === model)) { notify('Pick a model from the discovered list, or leave it empty.', true); return; }
  const reasoning = $('agent-reasoning-input').value;
  state.busy = true;
  updateControls();
  try {
    await call('mac_mcp_panel_setting', { name: 'default_agent', value: { provider, model: model || null, reasoning: reasoning || null } });
    closeEditor();
    hydrate(await call('mac_mcp_panel_state'));
    notify(`Default agent set to ${PROVIDERS[provider] || provider}${model ? ` · ${model}` : ''}.`);
  } catch (error) { notify(`Not saved: ${error.message || error}`, true); }
  finally { state.busy = false; updateControls(); }
});

// Data arrives with the host's tool result; refreshes are manual only (no polling).
// A single fallback read covers hosts that open the panel without delivering a result.
// No direct network requests, credentials, or cross-origin fetches from this iframe.
app.connect().then(() => {
  setStatus('online');
  setTimeout(() => { if (!state.data) void refresh(); }, 1500);
}).catch((error) => {
  setStatus('offline');
  notify(`MCP Apps bridge unavailable: ${error.message || error}`, true);
});
updateControls();
