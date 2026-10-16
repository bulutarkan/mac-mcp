// Mac MCP onboarding controller: navigation, the bridge to the app, and live status.
// A classic script (WKWebView does not load ES modules from file:// pages).
(function () {
'use strict';
const {
  SCREENS, CLIENTS, AGENTS, FIRST_REQUEST, evaluate, primaryAction, indexOf, appRow, agentRow,
  agentsSummary, appGroups, callSentence, stackParts, browserRow,
} = window.OnboardingSteps;

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
  running: { server_running: true, accessibility: 'not_determined', screen_recording: 'not_determined', permission_holder: 'Mac MCP', public_endpoint_mode: 'none', browsers: { safari: 'off', chrome: 'not_connected' } },
  connected: { server_running: true, accessibility: 'granted', screen_recording: 'granted', permission_holder: 'Mac MCP', public_endpoint_mode: 'none', first_call: { tool: 'read_file' } },
  done: { server_running: true, accessibility: 'granted', screen_recording: 'granted', permission_holder: 'Mac MCP', public_endpoint_mode: 'none', first_call: { tool: 'system_info' }, first_success: { tool: 'system_info' } },
}[params.get('demo') || 'running'];

const demoApps = [
  { id: 'claude-desktop', installed: true, added: false },
  { id: 'claude-code', installed: true, added: true },
  { id: 'codex', installed: true, added: true },
  { id: 'opencode', installed: false, added: false },
];
const demoAgents = { enabled: { codex: true, claude: true, opencode: false, kimi: false, grok: false },
  installed: { codex: true, claude: true, opencode: true, kimi: true, grok: true, antigravity: true, pi: true, cline: true, kilo: true, goose: true } };

function demo(action, args) {
  const replies = {
    status: () => demoStatus,
    apps: () => demoApps.map((app) => ({ ...app })),
    connectApp: () => {
      const app = demoApps.find((item) => item.id === args.client);
      if (app) app.added = true;
      return { ok: true, next: 'Quit Claude and open it again, then ask it something about your Mac.' };
    },
    agents: () => JSON.parse(JSON.stringify(demoAgents)),
    setupBrowser: () => {
      if (demoStatus.browsers) demoStatus.browsers[args.browser] = args.browser === 'safari' ? 'on' : 'connected';
      return true;
    },
    enableAgent: () => {
      demoAgents.enabled[args.provider] = true;
      return JSON.parse(JSON.stringify(demoAgents));
    },
    disableAgent: () => {
      demoAgents.enabled[args.provider] = false;
      return JSON.parse(JSON.stringify(demoAgents));
    },
  };
  const reply = replies[action] ? replies[action]() : { ok: true };
  // ?delay=ms makes the slow lookups as slow as on a real Mac, to see the loader.
  const delay = ['apps', 'agents'].includes(action) ? Number(params.get('delay')) || 0 : 0;
  return new Promise((resolve) => setTimeout(() => resolve(reply), delay));
}

// ---------- state ----------
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let current = Math.max(0, indexOf(params.get('screen') || 'welcome'));
let status = null;
let evaluation = evaluate(null);
let apps = null;
let appsLoading = false;
let appsProblem = null;
let agentsState = null;
let popupKind = null;
let popupReturn = null;
// Stacks whose +N was clicked stay open when the card redraws.
const openStacks = new Set();
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
  // Browser companions: optional, set up from here or later in Settings.
  for (const name of ['safari', 'chrome']) {
    const view = browserRow(name, e.browsers[name]);
    const chip = $(`.browser-chip[data-browser="${name}"]`);
    chip.classList.toggle('is-done', view.done);
    chip.title = view.detail;
    $(`[data-live="${name}-state"]`).textContent = view.state;
    const button = $('button', chip);
    button.hidden = !view.button;
    if (view.button) {
      button.textContent = view.button;
      button.setAttribute('aria-label', `${view.button}: ${view.detail}`);
    }
  }
  // Connect
  setStatus($('[data-live="connect-state"]'), e.connect,
    e.connect
      ? ['Mac MCP is connected', callSentence(e.firstTool)]
      : ['Waiting for your AI app', 'Open it and ask something about your Mac, like “Which macOS am I on?”']);
  if (apps && appsRemote !== e.remote) renderApps();
  // First request
  setStatus($('[data-live="request-state"]'), e.request,
    e.request ? ['It worked', 'Your AI app can use your Mac.'] : ['Waiting for the request', 'Paste it into your AI app and send it.']);
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
// Both lookups run together; the card shows a loader until both answered, then fades in
// whole instead of filling in row by row.
async function loadApps() {
  if (appsLoading) return;
  appsLoading = true;
  setCardLoading(true);
  const [found, agents] = await Promise.allSettled([call('apps'), call('agents')]);
  if (found.status === 'fulfilled') {
    apps = found.value;
    appsProblem = null;
  } else {
    // Not finding the apps is not the same as there being none: say so and offer a retry.
    apps = [];
    appsProblem = (found.reason && found.reason.message) || 'Mac MCP could not look for AI apps on this Mac.';
  }
  agentsState = agents.status === 'fulfilled' ? agents.value : null;
  appsLoading = false;
  renderApps();
  setCardLoading(false);
}

function setCardLoading(loading) {
  const card = $('.connect-card');
  card.toggleAttribute('data-loading', loading);
  card.setAttribute('aria-busy', String(loading));
  card.classList.remove('is-revealed');
  if (!loading) {
    void card.offsetWidth;  // restart the reveal when the list is loaded again
    card.classList.add('is-revealed');
  }
}

/** A status card: a pulse while waiting, a tick when done, a title and one line. */
function setStatus(card, done, [title, line]) {
  card.classList.toggle('is-done', done);
  $('.status-icon', card).innerHTML = done ? '✓' : '<i class="pulse"></i>';
  $(':scope > div > strong', card).textContent = title;
  $(':scope > div > span', card).textContent = line;
}

/** The maker's logo for a row; the name next to it says which app it is. */
function logoMark(client) {
  const mark = document.createElement('img');
  mark.className = 'app-mark';
  mark.src = `logos/${client.logo}.png`;
  mark.alt = '';
  return mark;
}

function appElement(item, row) {
  const element = document.createElement('li');
  element.className = 'app';
  element.dataset.app = item.id;
  element.dataset.state = row.state;
  const text = document.createElement('div');
  const name = document.createElement('strong');
  name.textContent = item.name;
  const detail = document.createElement('span');
  detail.textContent = appNotes[item.id] || row.detail;
  text.append(name, detail);
  element.append(logoMark(item), text);
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
  return element;
}

/**
 * The makers' logos, slightly overlapping, in one small row. Past five, a +N stands for the
 * rest; clicking it opens them in place (the row it sits in keeps its own click).
 */
function fillStack(stack, items, key) {
  stack.textContent = '';
  const { shown, extra } = stackParts(items);
  const open = extra.length > 0 && openStacks.has(key);
  stack.classList.toggle('is-open', open);
  for (const item of [...shown, ...extra]) {
    const mark = logoMark(item);
    mark.className = shown.includes(item) ? '' : 'extra';
    mark.title = item.name;
    stack.append(mark);
  }
  if (extra.length) {
    const more = document.createElement('span');
    more.className = 'more';
    more.textContent = `+${extra.length}`;
    more.title = extra.map((item) => item.name).join(', ');
    more.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openStacks.add(key);
      stack.classList.add('is-open');
    });
    stack.append(more);
  }
  return stack;
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
    $('[data-live="apps-count"]').textContent = '';
    renderAgents();
    return;
  }
  // Rows that need a click first; the apps Mac MCP is already added to share one row.
  const groups = appGroups(apps, evaluation.remote);
  $('[data-live="apps-count"]').textContent = groups.present ? `${groups.added.length} of ${groups.present} added` : '';
  const shown = groups.action.slice(0, 2);
  const hidden = groups.action.length - shown.length;
  for (const { client, row } of shown) list.append(appElement(client, row));
  const element = document.createElement('li');
  element.className = 'app summary';
  if (groups.added.length) {
    element.dataset.state = 'added';
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = `Added to ${groups.added.length} ${groups.added.length === 1 ? 'app' : 'apps'}`;
    const detail = document.createElement('span');
    detail.textContent = groups.added.map(({ client }) => client.name).join(', ');
    text.append(name, detail);
    if (hidden) detail.textContent += ` · ${hidden} more to set up`;
    const stack = document.createElement('span');
    stack.className = 'stack';
    element.append(fillStack(stack, groups.added.map(({ client }) => client), 'apps'), text);
  } else {
    element.dataset.state = 'more';
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = hidden ? `${hidden} more ${hidden === 1 ? 'app' : 'apps'} to set up` : 'Other AI apps';
    const detail = document.createElement('span');
    detail.textContent = 'See every app Mac MCP can be added to';
    text.append(name, detail);
    element.append(text);
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'All apps';
  button.setAttribute('aria-haspopup', 'dialog');
  button.addEventListener('click', () => openPopup('apps', button));
  element.append(button);
  list.append(element);
  renderAgents();
  if (popupKind === 'apps') renderPopup();
}

// Agents Mac MCP can hand work to: one row whatever their number; the list opens in a popup.
function renderAgents() {
  const block = $('[data-live="agents-block"]');
  const summary = agentsSummary(AGENTS, apps, agentsState);
  block.hidden = summary.rows.length === 0 || agentsState === null;
  $('[data-live="agents-count"]').textContent = summary.count;
  $('[data-live="agents-title"]').textContent = summary.title;
  $('[data-live="agents-detail"]').textContent = summary.detail;
  fillStack($('[data-live="agents-stack"]'), summary.rows.map(([agent]) => agent), 'agents');
  if (popupKind === 'agents') renderPopup();
}

// ---------- popup: every app, or every agent with its switch ----------
function openPopup(kind, opener) {
  popupKind = kind;
  popupReturn = opener || null;
  $('[data-live="scrim"]').hidden = false;
  $('[data-live="popup"]').hidden = false;
  renderPopup();
  $('[data-action="close-popup"]').focus();
}

function closePopup() {
  if (!popupKind) return;
  popupKind = null;
  $('[data-live="scrim"]').hidden = true;
  $('[data-live="popup"]').hidden = true;
  if (popupReturn) popupReturn.focus();
  popupReturn = null;
}

function renderPopup() {
  const list = $('[data-live="popup-list"]');
  list.textContent = '';
  if (popupKind === 'apps') {
    $('[data-live="popup-title"]').textContent = 'All AI apps';
    $('[data-live="popup-note"]').textContent = 'Apps that are not on this Mac are listed at the end.';
    for (const { client, row } of appGroups(apps, evaluation.remote).all) list.append(appElement(client, row));
    return;
  }
  $('[data-live="popup-title"]').textContent = 'Agents Mac MCP can hand work to';
  $('[data-live="popup-note"]').textContent = 'Each uses your own account with it. Change them later in Settings › Agents.';
  for (const [agent, row] of agentsSummary(AGENTS, apps, agentsState).rows) {
    const element = document.createElement('li');
    element.className = 'app';
    element.dataset.agent = agent.id;
    element.dataset.state = row.state;
    const text = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = agent.name;
    const detail = document.createElement('span');
    detail.textContent = row.detail;
    text.append(name, detail);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'switch';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', String(row.on));
    toggle.setAttribute('aria-label', agent.name);
    toggle.addEventListener('click', () => setAgent(agent, !row.on, element));
    element.append(logoMark(agent), text, toggle);
    list.append(element);
  }
}

async function setAgent(agent, on, element) {
  element.classList.add('is-working');
  $('button', element).disabled = true;
  try {
    agentsState = await call(on ? 'enableAgent' : 'disableAgent', { provider: agent.id });
    toast(on ? `${agent.name} is on. AI apps can hand work to it.` : `${agent.name} is off.`);
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
  for (const row of $$('.perm[data-perm]')) {
    $('button', row).addEventListener('click', () => requestPermission(row.dataset.perm));
  }
  for (const row of $$('.browser-chip[data-browser]')) {
    $('button', row).addEventListener('click', async () => {
      try {
        await call('setupBrowser', { browser: row.dataset.browser });
      } catch (error) {
        toast(error.message);
      }
      refresh();
    });
  }
  for (const link of $$('[data-action="open-settings-pane"]')) {
    link.addEventListener('click', () => call('openSystemSettings', { permission: link.dataset.permission }));
  }
  $('[data-action="manual-setup"]').addEventListener('click', () => call('openSettings', { section: 'connections' }));
  const strip = $('[data-action="open-agents"]');
  strip.addEventListener('click', () => openPopup('agents', strip));
  $('[data-action="close-popup"]').addEventListener('click', closePopup);
  $('[data-live="scrim"]').addEventListener('click', closePopup);
  $('[data-action="copy-prompt"]').addEventListener('click', () => copy(FIRST_REQUEST, 'Copied. Paste it into your AI app.'));
  document.addEventListener('keydown', (event) => {
    if (popupKind) {
      // The popup keeps the keys: Escape closes it, nothing moves the tour behind it.
      if (event.key === 'Escape') closePopup();
      return;
    }
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
