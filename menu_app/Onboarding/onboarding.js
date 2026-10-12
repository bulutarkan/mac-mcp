// Mac MCP onboarding controller: navigation, the bridge to the app, and live status.
// A classic script (WKWebView does not load ES modules from file:// pages).
(function () {
'use strict';
const { SCREENS, CLIENTS, FIRST_REQUEST, evaluate, primaryAction, indexOf } = window.OnboardingSteps;

// ---------- bridge ----------
// The app answers every message with window.macmcp.receive({id, ok, result | error}).
// Opened in a browser (no app), a small demo stands in so the page can be previewed.
const pending = new Map();
let sequence = 0;
const native = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.macmcp;

window.macmcp = {
  receive(message) {
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.ok) waiter.resolve(message.result);
    else waiter.reject(new Error(message.error || 'The app could not do that.'));
  },
};

function call(action, args = {}) {
  if (!native) return demo(action, args);
  sequence += 1;
  const id = sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    native.postMessage({ id, action, args });
  });
}

const params = new URLSearchParams(location.search);
const demoStatus = {
  fresh: { server_running: false },
  running: { server_running: true, accessibility: 'not_determined', screen_recording: 'not_determined', permission_holder: 'Mac MCP', public_endpoint_mode: 'none' },
  connected: { server_running: true, accessibility: 'granted', screen_recording: 'granted', permission_holder: 'Mac MCP', public_endpoint_mode: 'none', first_call: { tool: 'read_file' } },
  done: { server_running: true, accessibility: 'granted', screen_recording: 'granted', permission_holder: 'Mac MCP', public_endpoint_mode: 'none', first_call: { tool: 'system_info' }, first_success: { tool: 'system_info' } },
}[params.get('demo') || 'running'];

function demo(action, args) {
  const replies = {
    status: () => demoStatus,
    connectConfig: () => `claude mcp add-json --scope user mac-mcp '{"type":"http","url":"http://127.0.0.1:8765/mcp","headers":{"Authorization":"Bearer \${MAC_MCP_API_KEY}"}}'\n\nThen run: claude mcp get mac-mcp  (expect Status: Connected)\n# demo for ${args.client}`,
  };
  return Promise.resolve(replies[action] ? replies[action]() : { ok: true });
}

// ---------- state ----------
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let current = Math.max(0, indexOf(params.get('screen') || 'welcome'));
let status = null;
let evaluation = evaluate(null);
let client = CLIENTS[0].id;
let config = '';
let pollTimer = null;
let celebrated = false;
let restarting = false;
let restartedFor = '';

const screens = $$('.screen');
const rail = $('.rail');
const primary = $('#primary');
const back = $('#back');
const skip = $('#skip');

function toast(text) {
  const element = $('.toast');
  element.textContent = text;
  element.classList.add('is-shown');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('is-shown'), 2200);
}

async function copy(text, done) {
  try {
    await call('copy', { text });
    toast(done);
  } catch (error) {
    toast(error.message);
  }
}

// ---------- rendering ----------
function buildRail() {
  const steps = [...new Set(SCREENS.map((screen) => screen.rail))];
  rail.innerHTML = '';
  for (const name of steps) {
    const item = document.createElement('li');
    item.textContent = name;
    item.dataset.rail = name;
    rail.append(item);
  }
}

function renderRail() {
  const screen = SCREENS[current];
  const order = [...new Set(SCREENS.map((s) => s.rail))];
  const at = order.indexOf(screen.rail);
  for (const item of $$('li', rail)) {
    const index = order.indexOf(item.dataset.rail);
    item.classList.toggle('is-current', index === at);
    item.classList.toggle('is-done', index < at);
    item.setAttribute('aria-current', index === at ? 'step' : 'false');
  }
}

function renderLive() {
  const e = evaluation;
  // Start
  const ring = $('.ring');
  ring.classList.toggle('is-on', e.start);
  $('.power').classList.toggle('is-on', e.start);
  $('[data-live="power-state"]').textContent = e.start ? 'Running on this Mac' : 'Not running yet';
  $('[data-live="start-text"]').textContent = e.start
    ? 'Mac MCP is running on this Mac. It starts again when you log in.'
    : 'AI apps reach your Mac through a small server that runs here. Start it once; it starts again when you log in.';
  // Permissions
  // Until Mac MCP Server runs the server, macOS lists the permissions under another name.
  const holder = $('[data-live="holder"]');
  holder.textContent = e.holder || 'Mac MCP';
  holder.title = e.holder && !e.holderIsMacMCP
    ? `On this Mac the permissions are listed under “${e.holder}” until Mac MCP Server runs the server.`
    : '';
  for (const name of ['accessibility', 'screen_recording']) {
    const granted = e.permissions[name] === 'granted';
    const row = $(`.perm[data-perm="${name}"]`);
    row.classList.toggle('is-granted', granted);
    const button = $('button', row);
    button.textContent = granted ? 'Allowed' : 'Allow';
    button.disabled = granted || !e.start;
    $(`.settings-row[data-mirror="${name}"]`).classList.toggle('is-granted', granted);
  }
  // Connect
  const connectState = $('[data-live="connect-state"]');
  connectState.classList.toggle('is-done', e.connect);
  connectState.lastChild.textContent = e.connect
    ? `Connected. Your AI app called ${e.firstTool || 'Mac MCP'}.`
    : 'Waiting for your AI app to call Mac MCP';
  // First request
  const requestState = $('[data-live="request-state"]');
  requestState.classList.toggle('is-done', e.request);
  requestState.lastChild.textContent = e.request ? 'It worked. Your AI app can use your Mac.' : 'Waiting for the request to arrive';
  if (e.request && SCREENS[current].id === 'request' && !celebrated) {
    celebrated = true;
    $('.burst').classList.add('is-on');
  }
  renderPrimary();
}

function renderPrimary() {
  const { label } = primaryAction(SCREENS[current].id, evaluation);
  primary.textContent = label;
}

function show(index) {
  current = Math.max(0, Math.min(SCREENS.length - 1, index));
  screens.forEach((element, i) => {
    element.classList.toggle('is-active', i === current);
    element.classList.toggle('is-before', i < current);
    element.setAttribute('aria-hidden', i === current ? 'false' : 'true');
    element.inert = i !== current;
  });
  const screen = SCREENS[current];
  back.hidden = current === 0;
  skip.textContent = screen.kind === 'tour' ? 'Skip tour' : 'Close';
  renderRail();
  renderPrimary();
  if (screen.id === 'connect' && !config) loadConfig(client);
  if (screen.kind !== 'tour') startPolling();
  else stopPolling();
  const heading = $('h1', screens[current]);
  if (heading) heading.focus({ preventScroll: true });
}

// ---------- live status ----------
async function refresh() {
  try {
    status = await call('status');
  } catch {
    status = null;
  }
  evaluation = evaluate(status);
  renderLive();
  // At most one automatic restart for the same grants, so a grant the server still
  // cannot use never turns into a restart loop.
  const grants = `${evaluation.permissions.accessibility}|${evaluation.permissions.screen_recording}`;
  if (evaluation.restartNeeded && !restarting && grants !== restartedFor) {
    restartedFor = grants;
    restartForPermission();
  }
}

// A new grant reaches the running server only after it restarts: do it for the owner.
async function restartForPermission() {
  restarting = true;
  toast('Restarting Mac MCP to use the new permission…');
  try {
    await call('restartServer');
  } catch (error) {
    toast(error.message);
  }
  restarting = false;
  refresh();
}

function startPolling() {
  if (pollTimer) return;
  refresh();
  pollTimer = setInterval(refresh, 2000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// ---------- actions ----------
async function runPrimary() {
  const { action } = primaryAction(SCREENS[current].id, evaluation);
  if (action === 'next') return show(current + 1);
  if (action === 'finish') return call('finish', { outcome: 'completed' });
  if (action === 'startServer') {
    primary.disabled = true;
    $('.ring').classList.add('is-starting');
    try {
      await call('startServer');
    } catch (error) {
      toast(error.message);
    }
    $('.ring').classList.remove('is-starting');
    primary.disabled = false;
    refresh();
  }
  return undefined;
}

async function requestPermission(name) {
  // macOS shows its own window naming Mac MCP, with a button to System Settings.
  $('[data-live="permission-hint"]').hidden = false;
  try {
    await call('requestPermission', { permission: name });
  } catch (error) {
    toast(error.message);
  }
  refresh();
}

async function loadConfig(id) {
  client = id;
  for (const button of $$('.clients button')) button.setAttribute('aria-checked', String(button.dataset.client === id));
  const meta = CLIENTS.find((item) => item.id === id);
  $('[data-live="client-name"]').textContent = meta.name;
  const pre = $('[data-live="config"]');
  if (meta.needsRemote && !evaluation.remote) {
    config = '';
    pre.textContent = 'ChatGPT connects from the internet, so it needs a public HTTPS address for this Mac first.\n\nSet one up in Settings › Connections (Cloudflare Tunnel, ngrok or your own), then come back here. Remote access stays off until you do.';
    return;
  }
  pre.textContent = 'Loading…';
  try {
    config = await call('connectConfig', { client: id });
    pre.textContent = config;
  } catch (error) {
    config = '';
    pre.textContent = error.message;
  }
}

function buildClients() {
  const group = $('.clients');
  for (const item of CLIENTS) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.name;
    button.dataset.client = item.id;
    button.setAttribute('role', 'radio');
    button.setAttribute('aria-checked', String(item.id === client));
    button.addEventListener('click', () => loadConfig(item.id));
    group.append(button);
  }
}

// ---------- wiring ----------
function wire() {
  primary.addEventListener('click', runPrimary);
  back.addEventListener('click', () => show(current - 1));
  skip.addEventListener('click', () => {
    if (SCREENS[current].kind === 'tour') show(indexOf('start'));
    else call('finish', { outcome: 'skipped' });
  });
  for (const row of $$('.perm')) {
    $('button', row).addEventListener('click', () => requestPermission(row.dataset.perm));
  }
  for (const link of $$('[data-action="open-settings-pane"]')) {
    link.addEventListener('click', () => call('openSystemSettings', { permission: link.dataset.permission }));
  }
  $('[data-action="copy-config"]').addEventListener('click', () => {
    if (config) copy(config, 'Setup copied. Paste it into your AI app.');
  });
  $('[data-action="copy-prompt"]').addEventListener('click', () => copy(FIRST_REQUEST, 'Copied. Paste it into your AI app.'));
  document.addEventListener('keydown', (event) => {
    if (event.target.closest && event.target.closest('pre')) return;
    if (event.key === 'ArrowRight' && SCREENS[current].kind === 'tour') show(current + 1);
    else if (event.key === 'ArrowLeft') show(current - 1);
    else if (event.key === 'Enter' && !(event.target instanceof HTMLButtonElement)) runPrimary();
  });
  for (const heading of $$('h1')) heading.tabIndex = -1;
}

$('[data-live="prompt"]').textContent = `“${FIRST_REQUEST}”`;
buildRail();
buildClients();
wire();
show(current);
})();
