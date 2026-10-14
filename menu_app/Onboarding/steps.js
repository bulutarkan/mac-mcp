// The onboarding's state, kept apart from the DOM so it can be tested with `node --test`.
// A classic script, not an ES module: WKWebView does not load modules from file:// pages.
(function (root) {
'use strict';


const SCREENS = [
  { id: 'welcome', kind: 'tour', rail: 'Tour' },
  { id: 'apps', kind: 'tour', rail: 'Tour' },
  { id: 'browser', kind: 'tour', rail: 'Tour' },
  { id: 'safety', kind: 'tour', rail: 'Tour' },
  { id: 'start', kind: 'setup', rail: 'Start' },
  { id: 'permissions', kind: 'setup', rail: 'Permissions' },
  { id: 'connect', kind: 'setup', rail: 'Connect' },
  { id: 'request', kind: 'setup', rail: 'First request' },
  { id: 'done', kind: 'finish', rail: 'Done' },
];

// Apps `mac-mcp connect` sets up; the app does the setup, never this page.
const CLIENTS = [
  { id: 'claude-desktop', name: 'Claude Desktop', mark: '✳', needsRemote: false },
  { id: 'claude-code', name: 'Claude Code', mark: '>_', needsRemote: false },
  { id: 'chatgpt', name: 'ChatGPT', mark: '◎', needsRemote: true },
  { id: 'codex', name: 'Codex', mark: '{ }', needsRemote: false },
  { id: 'opencode', name: 'OpenCode', mark: '[ ]', needsRemote: false },
];

// Agents Mac MCP can hand work to (Settings › Agents). Each starts off; the owner turns it on.
const AGENTS = [
  { id: 'codex', client: 'codex', name: 'Codex' },
  { id: 'claude', client: 'claude-code', name: 'Claude Code' },
  { id: 'opencode', client: 'opencode', name: 'OpenCode' },
];

const FIRST_REQUEST = 'Use Mac MCP to tell me which macOS version this Mac is running.';

function indexOf(id) {
  return SCREENS.findIndex((screen) => screen.id === id);
}

/** What the live status (GET /dashboard/api/onboarding plus server_running) says is done. */
function evaluate(status) {
  const s = status || {};
  const running = s.server_running === true;
  const accessibility = running ? s.accessibility || 'unknown' : 'unknown';
  const screen = running ? s.screen_recording || 'unknown' : 'unknown';
  return {
    start: running,
    connect: running && Boolean(s.first_call),
    request: running && Boolean(s.first_success),
    firstTool: (s.first_call && s.first_call.tool) || null,
    permissions: {
      accessibility,
      screen_recording: screen,
      done: accessibility === 'granted' && screen === 'granted',
    },
    holderIsMacMCP: s.permission_holder === 'Mac MCP',
    holder: s.permission_holder || null,
    remote: Boolean(s.public_endpoint_mode) && s.public_endpoint_mode !== 'none',
    // Granted to Mac MCP, but the running server sees it only after a restart.
    restartNeeded: running && s.permission_restart_needed === true,
  };
}

/** The primary button for a screen: its label and what it does. */
function primaryAction(id, evaluation) {
  const e = evaluation || evaluate(null);
  switch (id) {
    case 'welcome':
      return { label: 'Show me how it works', action: 'next' };
    case 'safety':
      return { label: 'Set up Mac MCP', action: 'next' };
    case 'start':
      return e.start ? { label: 'Continue', action: 'next' } : { label: 'Start Mac MCP', action: 'startServer' };
    case 'permissions':
      return { label: e.permissions.done ? 'Continue' : 'Allow later', action: 'next' };
    case 'connect':
      return { label: e.connect ? 'Continue' : 'Skip for now', action: 'next' };
    case 'request':
      return { label: e.request ? 'Continue' : 'Skip for now', action: 'next' };
    case 'done':
      return { label: 'Finish', action: 'finish' };
    default:
      return { label: 'Continue', action: 'next' };
  }
}

/**
 * One row of the Connect screen: an AI app as `mac-mcp clients` found it.
 * action: 'connect' (Mac MCP sets it up), 'settings' (needs a public address first) or null.
 */
function appRow(client, found, remote) {
  if (client.needsRemote) {
    return remote
      ? { state: 'ready', detail: 'Copies its address, then you paste it in ChatGPT', button: 'Copy address', action: 'connect' }
      : { state: 'blocked', detail: 'Needs a public address for this Mac first', button: 'Set up', action: 'settings' };
  }
  if (found && found.added && found.keyless === false) {
    // Added by hand earlier, with Mac MCP's key in the app's own settings.
    return { state: 'legacy', detail: 'Added the old way, with its key in the app’s settings', button: 'Update', action: 'connect' };
  }
  if (found && found.added) return { state: 'added', detail: 'Mac MCP is added', button: 'Set up again', action: 'connect' };
  if (found && found.installed) return { state: 'ready', detail: 'On this Mac', button: 'Connect', action: 'connect' };
  return { state: 'missing', detail: 'Not on this Mac', button: null, action: null };
}

/**
 * One agent row: on, ready to turn on, or null when it is not on this Mac (not listed).
 * apps: what `mac-mcp clients` found; enabled: { codex: bool, ... } from Settings.
 */
function agentRow(agent, apps, enabled) {
  if (enabled && enabled[agent.id] === true) {
    return { state: 'added', detail: 'On', button: null, action: null };
  }
  const found = (apps || []).find((app) => app.id === agent.client);
  if (!found || !found.installed) return null;
  return { state: 'ready', detail: 'Off', button: 'Turn on', action: 'enableAgent' };
}

/** Apps on this Mac first, keeping the list's order otherwise. */
function orderedClients(apps) {
  const present = (client) => client.needsRemote || Boolean((apps || []).find((a) => a.id === client.id && a.installed));
  return [...CLIENTS.filter(present), ...CLIENTS.filter((client) => !present(client))];
}

const api = { SCREENS, CLIENTS, AGENTS, FIRST_REQUEST, indexOf, evaluate, primaryAction, appRow, agentRow, orderedClients };
if (typeof module === 'object' && module.exports) module.exports = api;
else root.OnboardingSteps = api;
})(typeof window === 'object' ? window : globalThis);
