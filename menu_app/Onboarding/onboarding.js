// Mac MCP onboarding controller: navigation, the bridge to the app, and live status.
// A classic script (WKWebView does not load ES modules from file:// pages).
(function () {
'use strict';
const { SCREENS, CLIENTS, AGENTS, FIRST_REQUEST, evaluate, primaryAction, indexOf, appRow, agentRow, orderedClients } = window.OnboardingSteps;

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

const demoApps = [
  { id: 'claude-desktop', installed: true, added: false },
  { id: 'claude-code', installed: true, added: true },
  { id: 'codex', installed: true, added: true },
  { id: 'opencode', installed: false, added: false },
];
const demoAgents = { codex: false, claude: true, opencode: false };

function demo(action, args) {
  const replies = {
    status: () => demoStatus,
    apps: () => demoApps.map((app) => ({ ...app })),
    connectApp: () => {
      const app = demoApps.find((item) => item.id === args.client);
      if (app) app.added = true;
      return { ok: true, next: 'Quit Claude and open it again, then ask it something about your Mac.' };
    },
    agents: () => ({ ...demoAgents }),
    enableAgent: () => {
      demoAgents[args.provider] = true;
      return { ...demoAgents };
    },
  };
  return Promise.resolve(replies[action] ? replies[action]() : { ok: true });
}

// ---------- state ----------
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let current = Math.max(0, indexOf(params.get('screen') || 'welcome'));
let status = null;
let evaluation = evaluate(null);
let apps = null;
let appsProblem = null;
let agentsEnabled = null;
const appNotes = {};
let appsRemote = null;
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
  if (apps && appsRemote !== e.remote) renderApps();
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
  if (screen.id === 'connect' && !apps) loadApps();
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
    const result = await call('requestPermission', { permission: name });
    // macOS could not show its prompt: say where to turn it on instead of looking refused.
    if (result && result.prompt_error) toast(result.prompt_error);
  } catch (error) {
    toast(error.message);
  }
  refresh();
}

// The AI apps on this Mac, each with one button: Mac MCP does the setup itself.
async function loadApps() {
  try {
    apps = await call('apps');
    appsProblem = null;
  } catch (error) {
    // Not finding the apps is not the same as there being none: say so and offer a retry.
    apps = [];
    appsProblem = error.message || 'Mac MCP could not look for AI apps on this Mac.';
  }
  agentsEnabled = await call('agents').catch(() => null);
  renderApps();
}

function renderApps() {
  appsRemote = evaluation.remote;
  const list = $('[data-live="apps"]');
  list.textContent = '';
  if (appsProblem) {
    const element = document.createElement('li');
    element.className = 'app';
    element.dataset.state = 'problem';
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = 'Could not look for AI apps';
    const detail = document.createElement('span');
    detail.textContent = appsProblem;
    text.append(name, detail);
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Try again';
    button.addEventListener('click', () => { apps = null; loadApps(); });
    element.append(text, button);
    list.append(element);
    renderAgents();
    return;
  }
  for (const item of orderedClients(apps)) {
    const row = appRow(item, (apps || []).find((found) => found.id === item.id), evaluation.remote);
    const element = document.createElement('li');
    element.className = 'app';
    element.dataset.app = item.id;
    element.dataset.state = row.state;
    const mark = document.createElement('i');
    mark.className = 'app-mark';
    mark.textContent = item.mark;
    mark.setAttribute('aria-hidden', 'true');
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = item.name;
    const detail = document.createElement('span');
    detail.textContent = appNotes[item.id] || row.detail;
    text.append(name, detail);
    element.append(mark, text);
    if (row.button) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = row.button;
      button.setAttribute('aria-label', `${row.button}: ${item.name}`);
      button.addEventListener('click', () => (row.action === 'settings'
        ? call('openSettings', { section: 'connections' })
        : connectApp(item, element)));
      element.append(button);
    }
    list.append(element);
  }
  renderAgents();
}

// Agents Mac MCP can hand work to: only those on this Mac, each turned on by the owner.
function renderAgents() {
  const block = $('[data-live="agents-block"]');
  const list = $('[data-live="agents"]');
  list.textContent = '';
  const rows = AGENTS.map((agent) => [agent, agentRow(agent, apps, agentsEnabled)]).filter(([, row]) => row);
  block.hidden = rows.length === 0 || agentsEnabled === null;
  for (const [agent, row] of rows) {
    const element = document.createElement('li');
    element.className = 'app';
    element.dataset.agent = agent.id;
    element.dataset.app = agent.client;
    element.dataset.state = row.state;
    // Same mark as the app row above, so the text sits in the text column.
    const mark = document.createElement('i');
    mark.className = 'app-mark';
    mark.textContent = (CLIENTS.find((client) => client.id === agent.client) || {}).mark || '';
    mark.setAttribute('aria-hidden', 'true');
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = agent.name;
    const detail = document.createElement('span');
    detail.textContent = row.detail;
    text.append(name, detail);
    element.append(mark, text);
    if (row.button) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = row.button;
      button.setAttribute('aria-label', `${row.button}: ${agent.name}`);
      button.addEventListener('click', () => enableAgent(agent, element));
      element.append(button);
    }
    list.append(element);
  }
}

async function enableAgent(agent, element) {
  element.classList.add('is-working');
  $('button', element).disabled = true;
  try {
    agentsEnabled = await call('enableAgent', { provider: agent.id });
    toast(`${agent.name} is on. AI apps can hand work to it.`);
  } catch (error) {
    toast(error.message);
  }
  renderAgents();
}

async function connectApp(item, element) {
  element.classList.add('is-working');
  $('button', element).disabled = true;
  try {
    const result = await call('connectApp', { client: item.id });
    appNotes[item.id] = result.next;
    toast(item.needsRemote ? 'Address copied. Paste it in ChatGPT.' : `Mac MCP is added to ${item.name}.`);
    apps = await call('apps').catch(() => apps);
  } catch (error) {
    appNotes[item.id] = error.message;
  }
  renderApps();
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
  $('[data-action="manual-setup"]').addEventListener('click', () => call('openSettings', { section: 'connections' }));
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
wire();
show(current);
})();
