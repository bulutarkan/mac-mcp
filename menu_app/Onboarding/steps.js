// The onboarding's state, kept apart from the DOM so it can be tested with `node --test`.
// A classic script, not an ES module: WKWebView does not load modules from file:// pages.
(function (root) {
'use strict';


// Setup is the main path (#126): the welcome screen goes straight to it, and the tour is a detour
// reachable from there. `rail` null: not a setup step. `branch`: reached from Connect only.
const SCREENS = [
  { id: 'welcome', kind: 'intro', rail: null },
  { id: 'apps', kind: 'tour', rail: null },
  { id: 'browser', kind: 'tour', rail: null },
  { id: 'safety', kind: 'tour', rail: null },
  { id: 'start', kind: 'setup', rail: 'Start' },
  { id: 'connect', kind: 'setup', rail: 'Connect' },
  { id: 'chatgpt', kind: 'setup', rail: 'Connect', branch: true },
  { id: 'other', kind: 'setup', rail: 'Connect', branch: true },
  { id: 'request', kind: 'setup', rail: 'First request' },
  // Optional (#247): a first request works without them; Mac app and browser work asks for them here.
  { id: 'permissions', kind: 'setup', rail: 'Optional' },
  { id: 'browsers', kind: 'setup', rail: 'Optional' },
  { id: 'done', kind: 'finish', rail: 'Done' },
];

const RAIL = [...new Set(SCREENS.map((screen) => screen.rail).filter(Boolean))];

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
  { id: 'copilot', name: 'GitHub Copilot', logo: 'copilot', needsRemote: false },
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
  { id: 'copilot', client: 'copilot', name: 'GitHub Copilot', logo: 'copilot' },
];

// A logo stack shows this many; the rest wait behind a +N that opens them in place.
const STACK_LIMIT = 5;

const FIRST_REQUEST = 'Use Mac MCP to tell me which macOS version this Mac is running.';

// How long the first-request screen waits before it offers help (#126).
const HELP_AFTER_MS = 45000;
// How long after copying ChatGPT's address before the ChatGPT screen offers help.
const CHATGPT_HELP_AFTER_MS = 60000;

function indexOf(id) {
  return SCREENS.findIndex((screen) => screen.id === id);
}

/** The screen the primary button leads to: setup skips the tour, the branches return to the path. */
function nextScreen(id, evaluation) {
  const e = evaluation || evaluate(null);
  // Into setup: a running server needs no Start screen.
  if (id === 'welcome' || id === 'safety') return e.start ? 'connect' : 'start';
  if (id === 'connect' || id === 'chatgpt' || id === 'other') return 'request';
  const at = indexOf(id);
  for (let index = at + 1; index < SCREENS.length; index += 1) {
    if (!SCREENS[index].branch) return SCREENS[index].id;
  }
  return id;
}

/** What the live status (GET /dashboard/api/onboarding plus server_running) says is done. */
function evaluate(status) {
  const s = status || {};
  const running = s.server_running === true;
  // The status could not be read (an error, a timeout): not the same as a stopped server (#18).
  const unavailable = !running && s.server_state === 'unavailable';
  const accessibility = running ? s.accessibility || 'unknown' : 'unknown';
  const screen = running ? s.screen_recording || 'unknown' : 'unknown';
  // AI apps that checked in with this server (they listed its tools): connected, even before a request.
  const seen = running && Array.isArray(s.clients_seen)
    ? s.clients_seen.filter((client) => client && typeof client.name === 'string' && client.name) : [];
  const chatgpt = seen.find((client) => client.name === 'ChatGPT') || null;
  const worked = running && (Boolean(s.first_success) || seen.some((client) => client.first_success_at));
  const firstCall = running && s.first_call ? s.first_call : null;
  return {
    start: running,
    unavailable,
    // The owner's "Start Mac MCP when you log in" setting; null when the page was not told (#22).
    startsAtLogin: typeof s.start_at_login === 'boolean' ? s.start_at_login : null,
    connect: running && (Boolean(firstCall) || seen.length > 0),
    connected: seen.map((client) => client.name),
    request: worked,
    // The request reached Mac MCP but did not work (denied, blocked, an error).
    requestFailed: !worked && Boolean(firstCall) && firstCall.status !== 'success',
    failedStatus: !worked && firstCall ? firstCall.status || 'error' : null,
    firstTool: (firstCall && firstCall.tool) || null,
    chatgpt: { connected: Boolean(chatgpt), worked: Boolean(chatgpt && chatgpt.first_success_at) },
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
    browsers: browserStates(s.browsers),
  };
}

// Browser companions (#188): optional, so they never hold up the setup.
const SAFARI_STATES = ['on', 'off', 'developer'];
const CHROME_STATES = ['connected', 'reload', 'not_connected', 'not_installed'];

function browserStates(raw) {
  const b = raw || {};
  const safari = SAFARI_STATES.includes(b.safari) ? b.safari : 'unknown';
  const chrome = CHROME_STATES.includes(b.chrome) ? b.chrome : 'unknown';
  return { safari, chrome, any: safari === 'on' || chrome === 'connected' || chrome === 'reload' };
}

/** What a browser's row says, and its button (null: nothing to press). */
function browserRow(name, state) {
  if (name === 'safari') {
    switch (state) {
      case 'on': return { done: true, state: 'On', button: null, detail: 'On. Mac MCP frames the Safari tab it works in.' };
      case 'off': return { done: false, state: '', button: 'Enable', detail: 'Turn on the Mac MCP extension in Safari once, then allow it on websites.' };
      case 'developer': return { done: false, state: '', button: 'Developer setup', detail: 'This build needs Safari’s developer setup for its extension.' };
      default: return { done: false, state: 'Checking…', button: null, detail: 'Checking Safari…' };
    }
  }
  switch (state) {
    case 'connected': return { done: true, state: 'Connected', button: null, detail: 'Connected. Chrome tabs work in the background.' };
    case 'reload': return { done: false, state: '', button: 'Reload', detail: 'Connected, but older than Mac MCP: reload it once at chrome://extensions.' };
    case 'not_connected': return { done: false, state: '', button: 'Set up', detail: 'Background tabs and page actions without bringing Chrome forward.' };
    case 'not_installed': return { done: false, state: 'Not installed', button: null, detail: 'Google Chrome is not installed. Safari works without it.' };
    default: return { done: false, state: 'Checking…', button: null, detail: 'Checking Chrome…' };
  }
}

/** What the Start screen says: the state under the power button and the sentence below it. */
function startView(evaluation) {
  const e = evaluation || evaluate(null);
  const login = e.startsAtLogin === true ? 'It starts again when you log in.'
    : e.startsAtLogin === false ? 'It does not start at login; you can turn that on in Settings › General.'
      : 'You can have it start at login in Settings › General.';
  if (e.start) return { power: 'Running on this Mac', text: `Mac MCP is running on this Mac. ${login}` };
  if (e.unavailable) {
    return {
      power: 'Status unavailable',
      text: 'Mac MCP did not answer the status check. It may still be running: try again in a moment.',
    };
  }
  return {
    power: 'Not running yet',
    text: `AI apps reach your Mac through a small server that runs here. Start it once. ${login}`,
  };
}

/** The primary button for a screen: its label and what it does. */
function primaryAction(id, evaluation) {
  const e = evaluation || evaluate(null);
  switch (id) {
    case 'welcome':
      return { label: 'Set up now', action: 'next' };
    case 'safety':
      return { label: 'Set up Mac MCP', action: 'next' };
    case 'start':
      if (e.start) return { label: 'Continue', action: 'next' };
      return e.unavailable ? { label: 'Try again', action: 'refresh' } : { label: 'Start Mac MCP', action: 'startServer' };
    case 'permissions':
      return { label: e.permissions.done ? 'Continue' : 'Skip for now', action: 'next' };
    case 'browsers':
      return { label: e.browsers.any ? 'Continue' : 'Skip for now', action: 'next' };
    case 'chatgpt':
      return { label: e.chatgpt.connected ? 'Continue' : 'Skip for now', action: 'next' };
    case 'request':
      return { label: e.request ? 'Continue' : 'Skip for now', action: 'next' };
    case 'done':
      return { label: 'Finish', action: 'finish' };
    default:
      return { label: 'Continue', action: 'next' };
  }
}

/** The second button, where a screen has one. */
function secondaryAction(id) {
  if (id === 'welcome') return { label: 'Take the tour first', action: 'tour' };
  return null;
}

/** A list in words: "A", "A and B", "A, B and C". */
function listWords(names) {
  const all = names || [];
  return all.length < 2 ? all.join('') : `${all.slice(0, -1).join(', ')} and ${all[all.length - 1]}`;
}

/** The Connect screen's status card: done, its title and one line. */
function connectStatus(evaluation) {
  const e = evaluation || evaluate(null);
  if (!e.start) {
    return { done: false, title: 'Mac MCP is not running', line: 'Go back to Start and start it first.' };
  }
  if (!e.connect) {
    return { done: false, title: 'Waiting for your AI app', line: 'Set it up here, then open it. It checks in with Mac MCP when it starts.' };
  }
  if (e.connected.length) {
    const names = listWords(e.connected);
    return { done: true, title: `${names} ${e.connected.length === 1 ? 'is' : 'are'} connected`, line: 'Next, try one request.' };
  }
  return { done: true, title: 'Mac MCP is connected', line: callSentence(e.firstTool) };
}

/** The first-request screen's status card: "connected" is not yet "it worked". */
function requestStatus(evaluation) {
  const e = evaluation || evaluate(null);
  if (e.request) return { state: 'done', title: 'It worked', line: 'Your AI app can use your Mac.' };
  if (!e.start) return { state: 'problem', title: 'Mac MCP is not running', line: 'Start it again, then send the request.' };
  if (e.requestFailed) {
    const blocked = e.failedStatus === 'denied' || e.failedStatus === 'blocked';
    return {
      state: 'problem',
      title: 'The request reached Mac MCP but did not work',
      line: blocked ? 'Your permission settings stopped it.' : 'Send it once more.',
    };
  }
  if (e.connect) {
    const who = e.connected.length ? listWords(e.connected) : 'Your AI app';
    return { state: 'waiting', title: `${who} ${e.connected.length > 1 ? 'are' : 'is'} connected`, line: 'Waiting for the request. Paste it into your AI app and send it.' };
  }
  return { state: 'waiting', title: 'Waiting for the request', line: 'Paste it into your AI app and send it.' };
}

/**
 * What to try when the first request does not arrive, fitted to what Mac MCP has seen.
 * null while it is too early to worry (and once it worked).
 */
function requestHelp(evaluation, waitedMs, asked = false) {
  const e = evaluation || evaluate(null);
  if (e.request) return null;
  if (!e.start) {
    return { title: 'Mac MCP stopped', tips: ['Start it again: it has to run while your AI app works.'], action: { label: 'Start Mac MCP', action: 'startServer' } };
  }
  if (e.requestFailed) {
    const blocked = e.failedStatus === 'denied' || e.failedStatus === 'blocked';
    return blocked
      ? { title: 'Mac MCP stopped the request', tips: ['Your permission profile or an approval said no. You can review it in Settings › Permissions & Safety.', 'Then send the request again.'], action: { label: 'Open Permissions & Safety', action: 'openPermissions' } }
      : { title: 'The request did not work', tips: ['Send the same request once more.', 'Still failing? Settings › Help & Diagnostics shows what happened.'], action: null };
  }
  if (!asked && (waitedMs || 0) < HELP_AFTER_MS) return null;
  if (e.connect) {
    const who = e.connected.length === 1 ? e.connected[0] : 'Your AI app';
    const tips = [
      `${who} is connected but has not used Mac MCP yet. Ask it to use Mac MCP by name, as the request here does.`,
      'Some apps ask you to allow a tool the first time: look for a prompt in the app.',
    ];
    if (e.chatgpt.connected) tips.splice(1, 0, 'In ChatGPT, start a new chat and turn on Mac MCP from the + menu before you send it.');
    return { title: 'Not working yet?', tips, action: null };
  }
  return {
    title: 'Your AI app has not reached Mac MCP yet',
    tips: [
      'Quit the app and open it again, so it loads Mac MCP.',
      'Check that Mac MCP is listed in the app’s tools, connectors or MCP settings.',
      'Using ChatGPT? It needs the steps on its own screen, and a new chat.',
    ],
    action: { label: 'Back to Connect', action: 'connect' },
  };
}

/**
 * The ChatGPT screen: its four steps and what each says now.
 * local: { copied, reachable, copyError, copiedAt } from the page.
 */
function chatgptView(evaluation, local = {}, now = Date.now()) {
  const e = evaluation || evaluate(null);
  const remote = e.remote;
  const copied = Boolean(local.copied);
  const steps = [
    {
      id: 'remote',
      title: 'Turn on remote access',
      done: remote,
      detail: remote
        ? 'On. ChatGPT can reach this Mac at its public address.'
        : 'In Settings › Connections, choose Cloudflare Tunnel, ngrok or your own HTTPS address.',
      button: remote ? null : 'Open Settings',
      action: remote ? null : 'openConnections',
    },
    {
      id: 'copy',
      title: 'Copy this Mac’s address',
      done: (copied && local.reachable !== false) || e.chatgpt.connected,
      warn: copied && local.reachable === false && !e.chatgpt.connected,
      detail: local.copyError
        ? local.copyError
        : copied && local.reachable === false
          ? 'Copied, but the public address does not answer right now. Check remote access in Settings › Connections, then copy it again.'
          : copied
            ? 'Copied. It includes your Mac MCP key, so paste it only into ChatGPT.'
            : 'It includes your Mac MCP key, so paste it only into ChatGPT.',
      button: copied || e.chatgpt.connected ? 'Copy again' : 'Copy address',
      action: 'copyAddress',
      disabled: !remote || !e.start,
    },
    {
      id: 'add',
      title: 'Add it in ChatGPT',
      done: e.chatgpt.connected,
      detail: 'In Settings › Apps & Connectors, choose Create. Name it Mac MCP, paste the address as its URL, pick No authentication. No Create? Turn on Developer mode under Advanced.',
      button: 'Open ChatGPT',
      action: 'openChatGPT',
    },
    {
      id: 'chat',
      title: 'Use it in a new chat',
      done: e.chatgpt.worked,
      detail: 'Start a new chat, turn on Mac MCP from the + menu, then send the request on the next screen.',
      button: null,
      action: null,
    },
  ];
  let status;
  if (!e.start) status = { done: false, title: 'Mac MCP is not running', line: 'Go back to Start and start it first.' };
  else if (e.chatgpt.worked) status = { done: true, title: 'ChatGPT is connected', line: 'And its first request worked.' };
  else if (e.chatgpt.connected) status = { done: true, title: 'ChatGPT is connected', line: 'It checked in with this Mac. Next, try a request in a new chat.' };
  else if (copied) status = { done: false, title: 'Waiting for ChatGPT', line: 'It checks in when you press Create, and this turns green.' };
  else status = { done: false, title: 'Not connected yet', line: 'Follow the steps. This turns green when ChatGPT checks in.' };

  let help = null;
  if (e.start && !e.chatgpt.connected) {
    if (copied && local.reachable === false) {
      help = ['This Mac’s public address does not answer, so ChatGPT cannot reach it.',
        'Check remote access in Settings › Connections, then copy the address again.'];
    } else if (local.askedHelp || (copied && local.copiedAt && now - local.copiedAt >= CHATGPT_HELP_AFTER_MS)) {
      help = [
        'Paste the whole address you copied as the connector’s URL.',
        'Pick No authentication: the key is already in the address.',
        'Changed remote access since? Copy the address again and create the connector again.',
      ];
    }
  }
  return { steps, status, help };
}

/** The rows of the "another AI app" screen, from `mac-mcp connect --client other --json`. */
function otherRows(info) {
  const i = info || {};
  if (!i.command) return null;
  const command = [i.command, ...(i.args || [])].join(' ');
  const rows = {
    command: [
      { id: 'command', label: 'Command', value: i.command, copy: i.command },
      { id: 'args', label: 'Arguments', value: (i.args || []).join(' '), copy: (i.args || []).join(' ') },
      { id: 'json', label: 'Or as JSON', value: 'mcpServers › mac-mcp', copy: i.config || '', title: command },
    ],
    url: [{ id: 'url', label: 'URL', value: i.url, copy: i.url }],
  };
  if (i.auth_required) rows.url.push({ id: 'key', label: 'Header', value: 'Authorization: Bearer ‹key›', copyKey: true });
  if (i.public_url) rows.url.push({ id: 'public', label: 'Public URL', value: i.public_url, copy: i.public_url });
  return rows;
}

/**
 * One row of the Connect screen: an AI app as `mac-mcp clients` found it.
 * action: 'connect' (Mac MCP sets it up), 'chatgpt' (its own screen) or null.
 */
function appRow(client, found, remote, chatgptConnected = false) {
  if (client.needsRemote) {
    if (chatgptConnected) return { state: 'added', detail: 'Connected', button: 'Set up again', action: 'chatgpt' };
    return remote
      ? { state: 'ready', detail: 'Copy its address, then add it in ChatGPT', button: 'Set up', action: 'chatgpt' }
      : { state: 'ready', detail: 'A few steps, with a public address for this Mac', button: 'Set up', action: 'chatgpt' };
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
  let title;
  if (on.length === 0) title = 'No agent is on yet';
  else if (on.length === 1) title = `${on[0]} is on`;
  else title = `${on.length} agents are on`;
  const parts = [];
  // Names while they fit on a line; past three, a number says the rest.
  if (on.length > 1) parts.push(on.length > 3 ? `${on.slice(0, 3).join(', ')} +${on.length - 3}` : on.join(', '));
  if (off.length > 3) parts.push(on.length ? `${off.length} others are off` : `${off.length} on this Mac, all off`);
  else if (off.length) parts.push(`${listWords(off)} ${off.length === 1 ? 'is' : 'are'} off`);
  return { rows, title, detail: parts.join(' · '), count: `${on.length} on · ${off.length} off` };
}

/** A logo stack's first STACK_LIMIT items, and the rest that a +N stands for. */
function stackParts(items, limit = STACK_LIMIT) {
  const all = items || [];
  return { shown: all.slice(0, limit), extra: all.slice(limit) };
}

/**
 * The Connect card keeps one height whatever the number of apps: ChatGPT's row, rows that need
 * a click, then the apps Mac MCP is already added to in one row. Apps not on this Mac only
 * appear under All apps.
 */
function appGroups(apps, remote, chatgptConnected = false) {
  const rows = orderedClients(apps).map((client) => ({
    client, row: appRow(client, (apps || []).find((found) => found.id === client.id), remote, chatgptConnected),
  }));
  const local = rows.filter(({ client }) => !client.needsRemote);
  return {
    all: rows,
    chatgpt: rows.find(({ client }) => client.needsRemote),
    action: local.filter(({ row }) => row.state !== 'added' && row.state !== 'missing'),
    added: local.filter(({ row }) => row.state === 'added'),
    present: local.filter(({ row }) => row.state !== 'missing').length,
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

/** The first line of a CLI error, without its numbered steps (the page shows its own). */
function shortError(message, fallback) {
  const first = String(message || '').split('\n')[0].trim();
  if (!first) return fallback;
  return first.charAt(0).toUpperCase() + first.slice(1);
}

const api = {
  SCREENS, RAIL, CLIENTS, AGENTS, FIRST_REQUEST, HELP_AFTER_MS, CHATGPT_HELP_AFTER_MS, indexOf, nextScreen,
  evaluate, primaryAction, secondaryAction, appRow, agentRow, orderedClients, agentsSummary, appGroups,
  callSentence, stackParts, STACK_LIMIT, browserRow, startView, connectStatus, requestStatus, requestHelp,
  chatgptView, otherRows, listWords, shortError,
};
if (typeof module === 'object' && module.exports) module.exports = api;
else root.OnboardingSteps = api;
})(typeof window === 'object' ? window : globalThis);
