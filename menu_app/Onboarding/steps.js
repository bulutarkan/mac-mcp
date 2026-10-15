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
// logo: logos/<name>.png, one per maker (Claude's two apps share one, as do OpenAI's).
const CLIENTS = [
  { id: 'claude-desktop', name: 'Claude Desktop', logo: 'claude', needsRemote: false },
  { id: 'claude-code', name: 'Claude Code', logo: 'claude', needsRemote: false },
  { id: 'chatgpt', name: 'ChatGPT', logo: 'openai', needsRemote: true },
  { id: 'codex', name: 'Codex', logo: 'openai', needsRemote: false },
  { id: 'opencode', name: 'OpenCode', logo: 'opencode', needsRemote: false },
  { id: 'kimi', name: 'Kimi Code', logo: 'kimi', needsRemote: false },
  { id: 'grok', name: 'Grok Build', logo: 'grok', needsRemote: false },
  { id: 'antigravity', name: 'Antigravity', logo: 'antigravity', needsRemote: false },
  { id: 'pi', name: 'pi', logo: 'pi', needsRemote: false },
  { id: 'cline', name: 'Cline', logo: 'cline', needsRemote: false },
  { id: 'kilo', name: 'Kilo Code', logo: 'kilo', needsRemote: false },
  { id: 'goose', name: 'Goose', logo: 'goose', needsRemote: false },
];

// Agents Mac MCP can hand work to (Settings › Agents). Each starts off; the owner turns it on.
// client: the AI app of the same CLI, whose install also tells that the agent is there.
// logo: logos/<name>.png.
const AGENTS = [
  { id: 'codex', client: 'codex', name: 'Codex', logo: 'openai' },
  { id: 'claude', client: 'claude-code', name: 'Claude Code', logo: 'claude' },
  { id: 'opencode', client: 'opencode', name: 'OpenCode', logo: 'opencode' },
  { id: 'kimi', client: 'kimi', name: 'Kimi Code', logo: 'kimi' },
  { id: 'grok', client: 'grok', name: 'Grok Build', logo: 'grok' },
  { id: 'antigravity', client: 'antigravity', name: 'Antigravity', logo: 'antigravity' },
  { id: 'pi', client: 'pi', name: 'pi', logo: 'pi' },
  { id: 'cline', client: 'cline', name: 'Cline', logo: 'cline' },
  { id: 'kilo', client: 'kilo', name: 'Kilo Code', logo: 'kilo' },
  { id: 'goose', client: 'goose', name: 'Goose', logo: 'goose' },
];

// A logo stack shows this many; the rest wait behind a +N that opens them in place.
const STACK_LIMIT = 5;

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
 * apps: what `mac-mcp clients` found; agents: { enabled: { codex: bool, ... }, installed: { ... } }
 * from Settings and the server's provider detection.
 */
function agentRow(agent, apps, agents) {
  const enabled = (agents && agents.enabled) || {};
  const installed = (agents && agents.installed) || {};
  if (enabled[agent.id] === true) {
    return { state: 'added', on: true, detail: 'On', action: 'disableAgent' };
  }
  const found = agent.client && (apps || []).find((app) => app.id === agent.client && app.installed);
  if (!found && installed[agent.id] !== true) return null;
  return { state: 'off', on: false, detail: 'Off', action: 'enableAgent' };
}

/** The agents on this Mac and the line that sums them up in one row. */
function agentsSummary(agents, apps, state) {
  const rows = agents.map((agent) => [agent, agentRow(agent, apps, state)]).filter(([, row]) => row);
  const on = rows.filter(([, row]) => row.on).map(([agent]) => agent.name);
  const off = rows.filter(([, row]) => !row.on).map(([agent]) => agent.name);
  const list = (names) => (names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`);
  let title;
  if (on.length === 0) title = 'No agent is on yet';
  else if (on.length === 1) title = `${on[0]} is on`;
  else title = `${on.length} agents are on`;
  const parts = [];
  // Names while they fit on a line; past three, a number says the rest.
  if (on.length > 1) parts.push(on.length > 3 ? `${on.slice(0, 3).join(', ')} +${on.length - 3}` : on.join(', '));
  if (off.length > 3) parts.push(on.length ? `${off.length} others are off` : `${off.length} on this Mac, all off`);
  else if (off.length) parts.push(`${list(off)} ${off.length === 1 ? 'is' : 'are'} off`);
  return { rows, title, detail: parts.join(' · '), count: `${on.length} on · ${off.length} off` };
}

/** A logo stack's first STACK_LIMIT items, and the rest that a +N stands for. */
function stackParts(items, limit = STACK_LIMIT) {
  const all = items || [];
  return { shown: all.slice(0, limit), extra: all.slice(limit) };
}

/**
 * The Connect card keeps one height whatever the number of apps: rows that need a click,
 * then the apps Mac MCP is already added to in one row. Apps not on this Mac only appear
 * under All apps.
 */
function appGroups(apps, remote) {
  const rows = orderedClients(apps).map((client) => ({ client, row: appRow(client, (apps || []).find((found) => found.id === client.id), remote) }));
  return {
    all: rows,
    action: rows.filter(({ row }) => row.state !== 'added' && row.state !== 'missing'),
    added: rows.filter(({ row }) => row.state === 'added'),
    present: rows.filter(({ row }) => row.state !== 'missing').length,
  };
}

// What the AI app's first call did, in words: the tool name means nothing to most people.
const CALL_WORDS = [
  [/^(run_command|run_commands_parallel|start_background_job)$/, 'ran a command on this Mac'],
  [/^(read_file|read_multiple_files|list_directory|search_files|file_info)$/, 'read files on this Mac'],
  [/^(write_file|edit_file|move_file|create_directory)$/, 'changed a file on this Mac'],
  [/^browser_/, 'looked at a web page'],
  [/^(mac_|screenshot)/, 'looked at an app on this Mac'],
  [/system_info$/, 'checked this Mac'],
  [/^memory_/, 'searched its memory'],
];

function callSentence(tool) {
  const name = String(tool || '');
  const found = CALL_WORDS.find(([pattern]) => pattern.test(name));
  return `Your AI app ${found ? found[1] : 'used Mac MCP'}.`;
}

/** Apps on this Mac first, keeping the list's order otherwise. */
function orderedClients(apps) {
  const present = (client) => client.needsRemote || Boolean((apps || []).find((a) => a.id === client.id && a.installed));
  return [...CLIENTS.filter(present), ...CLIENTS.filter((client) => !present(client))];
}

const api = {
  SCREENS, CLIENTS, AGENTS, FIRST_REQUEST, indexOf, evaluate, primaryAction, appRow, agentRow, orderedClients,
  agentsSummary, appGroups, callSentence, stackParts, STACK_LIMIT,
};
if (typeof module === 'object' && module.exports) module.exports = api;
else root.OnboardingSteps = api;
})(typeof window === 'object' ? window : globalThis);
