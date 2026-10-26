import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import steps from '../steps.js';

const {
  SCREENS, RAIL, CLIENTS, AGENTS, evaluate, primaryAction, secondaryAction, indexOf, nextScreen, appRow, agentRow,
  orderedClients, agentsSummary, appGroups, callSentence, stackParts, STACK_LIMIT, startView, connectStatus,
  requestStatus, requestHelp, chatgptView, otherRows, HELP_AFTER_MS, CHATGPT_HELP_AFTER_MS, shortError,
} = steps;

test('the screens, with the tour as a detour and two connect branches', () => {
  assert.deepEqual(SCREENS.map((s) => s.id),
    ['welcome', 'apps', 'browser', 'safety', 'start', 'connect', 'chatgpt', 'other', 'request', 'permissions', 'browsers', 'done']);
  assert.deepEqual(RAIL, ['Start', 'Connect', 'First request', 'Optional', 'Done']);
  for (const id of ['welcome', 'apps', 'browser', 'safety']) assert.equal(SCREENS[indexOf(id)].rail, null);
});

test('set up now is the main path: the welcome skips the tour (#126)', () => {
  const stopped = evaluate({ server_running: false });
  const running = evaluate({ server_running: true });
  assert.deepEqual(primaryAction('welcome', stopped), { label: 'Set up now', action: 'next' });
  assert.deepEqual(secondaryAction('welcome'), { label: 'Take the tour first', action: 'tour' });
  assert.equal(nextScreen('welcome', stopped), 'start');
  // A server that already runs needs no Start screen.
  assert.equal(nextScreen('welcome', running), 'connect');
  // The tour's end leads into setup the same way.
  assert.equal(nextScreen('safety', stopped), 'start');
  assert.equal(nextScreen('apps', stopped), 'browser');
  // The branches return to the path; the path never walks into a branch.
  assert.equal(nextScreen('connect', running), 'request');
  assert.equal(nextScreen('chatgpt', running), 'request');
  assert.equal(nextScreen('other', running), 'request');
  assert.equal(nextScreen('start', running), 'connect');
  assert.equal(nextScreen('request', running), 'permissions');
  assert.equal(nextScreen('done', running), 'done');
  for (const id of SCREENS.map((s) => s.id)) assert.equal(secondaryAction(id) !== null, id === 'welcome', id);
});

test('permissions come after the first request and are optional (#247)', () => {
  assert.ok(indexOf('permissions') > indexOf('request'));
  assert.equal(SCREENS.find((s) => s.id === 'permissions').rail, 'Optional');
  const running = evaluate({ server_running: true, accessibility: 'not_determined', screen_recording: 'denied' });
  assert.deepEqual(primaryAction('permissions', running), { label: 'Skip for now', action: 'next' });
});

test('the page shows the screens in the same order', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const order = [...html.matchAll(/data-screen="([a-z]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, SCREENS.map((s) => s.id));
});

test('nothing is done before the server answers', () => {
  const e = evaluate(null);
  assert.equal(e.start, false);
  assert.equal(e.connect, false);
  assert.equal(e.request, false);
  assert.equal(e.permissions.done, false);
});

test('live status marks each step', () => {
  const e = evaluate({
    server_running: true, first_call: { tool: 'read_file' }, first_success: null,
    accessibility: 'granted', screen_recording: 'not_determined', permission_holder: 'Mac MCP',
    public_endpoint_mode: 'none',
  });
  assert.equal(e.start, true);
  assert.equal(e.connect, true);
  assert.equal(e.request, false);
  assert.equal(e.permissions.accessibility, 'granted');
  assert.equal(e.permissions.done, false);
  assert.equal(e.holderIsMacMCP, true);
  assert.equal(e.remote, false);
});

test('the primary button says what happens', () => {
  const idle = evaluate({ server_running: false });
  assert.deepEqual(primaryAction('welcome', idle), { label: 'Set up now', action: 'next' });
  assert.deepEqual(primaryAction('start', idle), { label: 'Start Mac MCP', action: 'startServer' });
  const running = evaluate({ server_running: true, accessibility: 'granted', screen_recording: 'granted' });
  assert.deepEqual(primaryAction('start', running), { label: 'Continue', action: 'next' });
  assert.deepEqual(primaryAction('permissions', running), { label: 'Continue', action: 'next' });
  // Connected or not, the request screen comes next: it says what to try when nothing arrives.
  assert.deepEqual(primaryAction('connect', running), { label: 'Continue', action: 'next' });
  assert.deepEqual(primaryAction('chatgpt', running), { label: 'Skip for now', action: 'next' });
  const chatgpt = evaluate({ server_running: true, clients_seen: [{ name: 'ChatGPT', seen_at: 1 }] });
  assert.deepEqual(primaryAction('chatgpt', chatgpt), { label: 'Continue', action: 'next' });
  assert.deepEqual(primaryAction('done', running), { label: 'Finish', action: 'finish' });
});

test('ChatGPT is marked as needing remote access; local clients are not', () => {
  const chatgpt = CLIENTS.find((c) => c.id === 'chatgpt');
  assert.equal(chatgpt.needsRemote, true);
  assert.equal(CLIENTS.find((c) => c.id === 'claude-code').needsRemote, false);
  assert.ok(CLIENTS.every((c) => typeof c.name === 'string' && c.name.length > 0));
});

test('every app shows its maker\'s logo, and each logo file ships with the page', () => {
  for (const client of CLIENTS) {
    assert.ok(existsSync(new URL(`../logos/${client.logo}.png`, import.meta.url)), client.id);
  }
  const logo = (id) => CLIENTS.find((c) => c.id === id).logo;
  assert.equal(logo('claude-desktop'), logo('claude-code'));
  assert.equal(logo('chatgpt'), logo('codex'));
});

test('each AI app row says what one click will do', () => {
  const code = CLIENTS.find((c) => c.id === 'claude-code');
  const chatgpt = CLIENTS.find((c) => c.id === 'chatgpt');
  assert.equal(appRow(code, { installed: true, added: false }, false).button, 'Connect');
  assert.equal(appRow(code, { installed: true, added: true }, false).state, 'added');
  assert.equal(appRow(code, { installed: true, added: true, keyless: false }, false).button, 'Update');
  assert.equal(appRow(code, { installed: true, added: true, keyless: true }, false).state, 'added');
  assert.equal(appRow(code, { installed: false, added: false }, false).action, null);
  assert.equal(appRow(code, undefined, false).state, 'missing');
  // ChatGPT opens its own steps, with or without a public address yet.
  assert.deepEqual([appRow(chatgpt, undefined, false).action, appRow(chatgpt, undefined, false).button], ['chatgpt', 'Set up']);
  assert.equal(appRow(chatgpt, undefined, true).action, 'chatgpt');
  assert.equal(appRow(chatgpt, undefined, true, true).state, 'added');
});

test('apps on this Mac are listed first', () => {
  const order = orderedClients([{ id: 'codex', installed: true }]).map((c) => c.id);
  assert.deepEqual(order.slice(0, 2), ['chatgpt', 'codex']);
  assert.equal(order.length, CLIENTS.length);
});

test('a grant this server cannot see yet asks for a restart', () => {
  const e = evaluate({ server_running: true, accessibility: 'granted', screen_recording: 'granted', permission_restart_needed: true });
  assert.equal(e.restartNeeded, true);
  assert.equal(evaluate({ server_running: false, permission_restart_needed: true }).restartNeeded, false);
  assert.equal(evaluate({ server_running: true }).restartNeeded, false);
});

test('agents on this Mac can be switched on and off; others are not listed', () => {
  const apps = [{ id: 'codex', installed: true }, { id: 'claude-code', installed: true }, { id: 'opencode', installed: false }];
  const [codex, claude, opencode, kimi, grok] = AGENTS;
  const state = { enabled: { codex: false, claude: true, kimi: false }, installed: { kimi: true } };
  assert.equal(agentRow(codex, apps, state).action, 'enableAgent');
  assert.equal(agentRow(claude, apps, state).state, 'added');
  assert.equal(agentRow(claude, apps, state).action, 'disableAgent');
  assert.equal(agentRow(opencode, apps, state), null);
  // Agents without an AI app of their own are found by the server.
  assert.equal(agentRow(kimi, apps, state).detail, 'Off');
  assert.equal(agentRow(grok, apps, state), null);
  assert.deepEqual(AGENTS.map((a) => a.id), ['codex', 'claude', 'opencode', 'kimi', 'grok', 'antigravity', 'pi', 'cline', 'kilo', 'goose', 'copilot']);
  for (const agent of AGENTS) assert.ok(existsSync(new URL(`../logos/${agent.logo}.png`, import.meta.url)), agent.id);
});

test('the agents row sums them up in one line, whatever their number', () => {
  const apps = [{ id: 'codex', installed: true }, { id: 'claude-code', installed: true }, { id: 'opencode', installed: true }];
  const state = { enabled: { codex: true, claude: true }, installed: { kimi: true, grok: true } };
  const summary = agentsSummary(AGENTS, apps, state);
  assert.equal(summary.rows.length, 5);
  assert.equal(summary.title, '2 agents are on');
  assert.equal(summary.detail, 'Codex, Claude Code · OpenCode, Kimi Code and Grok Build are off');
  assert.equal(summary.count, '2 on · 3 off');
  assert.equal(agentsSummary(AGENTS, apps, { enabled: { codex: true } }).title, 'Codex is on');
  assert.equal(agentsSummary(AGENTS, apps, { enabled: {} }).title, 'No agent is on yet');
  // Past three names, a number says the rest.
  const many = { enabled: { codex: true, claude: true, opencode: true, kimi: true }, installed: { kimi: true, grok: true, antigravity: true, pi: true, cline: true, kilo: true, goose: true, copilot: true } };
  assert.equal(agentsSummary(AGENTS, apps, many).detail, 'Codex, Claude Code, OpenCode +1 · 7 others are off');
  assert.equal(agentsSummary(AGENTS, apps, { enabled: {}, installed: many.installed }).detail, '11 on this Mac, all off');
});

test('a logo stack shows five and keeps the rest behind a +N', () => {
  assert.equal(STACK_LIMIT, 5);
  const { shown, extra } = stackParts(AGENTS);
  assert.deepEqual(shown.map((a) => a.id), ['codex', 'claude', 'opencode', 'kimi', 'grok']);
  assert.deepEqual(extra.map((a) => a.id), ['antigravity', 'pi', 'cline', 'kilo', 'goose', 'copilot']);
  assert.equal(stackParts(AGENTS.slice(0, 5)).extra.length, 0);
  assert.deepEqual(stackParts(null), { shown: [], extra: [] });
});

test('the apps card shows what needs a click, then one row for the apps already set up', () => {
  const apps = [
    { id: 'claude-desktop', installed: true, added: false },
    { id: 'claude-code', installed: true, added: true },
    { id: 'codex', installed: true, added: true },
    { id: 'opencode', installed: false, added: false },
  ];
  const groups = appGroups(apps, false);
  // ChatGPT has a row of its own, always in view; the local apps are counted.
  assert.deepEqual(groups.action.map(({ client }) => client.id), ['claude-desktop']);
  assert.equal(groups.chatgpt.client.id, 'chatgpt');
  assert.deepEqual(groups.added.map(({ client }) => client.id), ['claude-code', 'codex']);
  assert.equal(groups.present, 3);
  assert.equal(groups.all.length, CLIENTS.length);
  // Apps not on this Mac come last, in list order.
  assert.deepEqual(groups.all.slice(-9).map(({ client }) => client.id),
    ['opencode', 'kimi', 'grok', 'antigravity', 'pi', 'cline', 'kilo', 'goose', 'copilot']);
  // Every agent with a CLI of its own is also an AI app Mac MCP can be added to.
  for (const agent of AGENTS) assert.ok(CLIENTS.some((client) => client.id === agent.client), agent.id);
});

test('the first call is said in words, not as a tool name', () => {
  assert.equal(callSentence('run_command'), 'Your AI app ran a command on this Mac.');
  assert.equal(callSentence('read_file'), 'Your AI app read files on this Mac.');
  assert.equal(callSentence('browser_observe'), 'Your AI app looked at a web page.');
  assert.equal(callSentence('get_system_info'), 'Your AI app checked this Mac.');
  assert.equal(callSentence('something_new'), 'Your AI app used Mac MCP.');
  assert.equal(callSentence(null), 'Your AI app used Mac MCP.');
});

test('the Connect card loads apps and agents together and shows them at once', async () => {
  const { readFileSync } = await import('node:fs');
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const script = readFileSync(new URL('../onboarding.js', import.meta.url), 'utf8');
  // The card starts as a loader, not as an empty list that fills in row by row.
  assert.match(html, /class="stage plain connect-card" data-loading aria-busy="true"/);
  assert.match(html, /Looking for AI apps on this Mac/);
  assert.match(script, /Promise\.allSettled\(\[call\('apps'\), call\('agents'\)\]\)/);
  assert.match(script, /if \(appsLoading\) return;/);
});

test('browser companions are read from the status and never block setup', () => {
  const { browserRow } = steps;
  const none = evaluate({ server_running: true });
  assert.deepEqual(none.browsers, { safari: 'unknown', chrome: 'unknown', any: false });
  const live = evaluate({ server_running: true, browsers: { safari: 'on', chrome: 'reload' } });
  assert.deepEqual(live.browsers, { safari: 'on', chrome: 'reload', any: true });
  assert.equal(evaluate({ server_running: true, browsers: { safari: 'nonsense', chrome: 42 } }).browsers.safari, 'unknown');
  // Optional: their own screen sits under Optional on the rail and never waits for them.
  assert.equal(SCREENS.find((s) => s.id === 'browsers').rail, 'Optional');
  assert.deepEqual(primaryAction('browsers', none), { label: 'Skip for now', action: 'next' });
  assert.deepEqual(primaryAction('browsers', live), { label: 'Continue', action: 'next' });

  assert.deepEqual(browserRow('safari', 'on'), { done: true, state: 'On', button: null,
    detail: 'On. Mac MCP frames the Safari tab it works in.' });
  assert.equal(browserRow('safari', 'off').button, 'Enable');
  assert.equal(browserRow('safari', 'developer').button, 'Developer setup');
  assert.equal(browserRow('chrome', 'connected').state, 'Connected');
  assert.equal(browserRow('chrome', 'reload').button, 'Reload');
  assert.match(browserRow('chrome', 'reload').detail, /reload/i);
  assert.equal(browserRow('chrome', 'not_connected').button, 'Set up');
  assert.deepEqual([browserRow('chrome', 'not_installed').state, browserRow('chrome', 'not_installed').button],
    ['Not installed', null]);
  assert.deepEqual([browserRow('chrome', 'unknown').state, browserRow('chrome', 'unknown').button], ['Checking…', null]);
});

test('a status that could not be read is not a stopped server (#18)', () => {
  const unreadable = evaluate({ server_running: false, server_state: 'unavailable' });
  assert.equal(unreadable.start, false);
  assert.equal(unreadable.unavailable, true);
  assert.deepEqual(primaryAction('start', unreadable), { label: 'Try again', action: 'refresh' });
  assert.equal(startView(unreadable).power, 'Status unavailable');
  assert.match(startView(unreadable).text, /may still be running/);
  const stopped = evaluate({ server_running: false, server_state: 'stopped' });
  assert.deepEqual(primaryAction('start', stopped), { label: 'Start Mac MCP', action: 'startServer' });
  assert.equal(startView(stopped).power, 'Not running yet');
  // A page told nothing about the state (an older app) still offers to start it.
  assert.deepEqual(primaryAction('start', evaluate({ server_running: false })), { label: 'Start Mac MCP', action: 'startServer' });
});

test('the Start screen says what the start-at-login setting does (#22)', () => {
  const on = startView(evaluate({ server_running: true, start_at_login: true }));
  assert.equal(on.text, 'Mac MCP is running on this Mac. It starts again when you log in.');
  const off = startView(evaluate({ server_running: true, start_at_login: false }));
  assert.match(off.text, /does not start at login; you can turn that on in Settings › General/);
  const unknown = startView(evaluate({ server_running: false }));
  assert.doesNotMatch(unknown.text, /starts again when you log in/);
  assert.match(unknown.text, /Settings › General/);
});

test('the page has no duplicate ids', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(ids.filter((id, i) => ids.indexOf(id) !== i), []);
});

test('a status the bridge could not read is unavailable, not stopped (#126)', () => {
  const script = readFileSync(new URL('../onboarding.js', import.meta.url), 'utf8');
  assert.match(script, /catch \{\s*\/\/[^\n]*\n\s*status = \{ server_running: false, server_state: 'unavailable' \};/);
});

test('connected is an app checking in; it worked is a successful request (#126)', () => {
  const seen = evaluate({ server_running: true, clients_seen: [{ name: 'ChatGPT', seen_at: 1 }] });
  assert.equal(seen.connect, true);
  assert.equal(seen.request, false);
  assert.deepEqual(seen.connected, ['ChatGPT']);
  assert.deepEqual(seen.chatgpt, { connected: true, worked: false });
  assert.deepEqual(connectStatus(seen), { done: true, title: 'ChatGPT is connected', line: 'Next, try one request.' });
  assert.equal(requestStatus(seen).state, 'waiting');
  assert.equal(requestStatus(seen).title, 'ChatGPT is connected');
  const worked = evaluate({ server_running: true, clients_seen: [{ name: 'Claude', seen_at: 1, first_success_at: 2 }] });
  assert.equal(worked.request, true);
  assert.equal(requestStatus(worked).state, 'done');
  // A stopped server shows nothing as connected, whatever it saw before.
  assert.equal(evaluate({ server_running: false, clients_seen: [{ name: 'ChatGPT' }] }).connect, false);
  assert.equal(connectStatus(evaluate({ server_running: true })).done, false);
  assert.equal(connectStatus(evaluate({ server_running: false })).title, 'Mac MCP is not running');
  const two = evaluate({ server_running: true, clients_seen: [{ name: 'Claude' }, { name: 'Codex' }] });
  assert.equal(connectStatus(two).title, 'Claude and Codex are connected');
});

test('the first-request screen offers help that fits what Mac MCP saw (#126)', () => {
  const none = evaluate({ server_running: true });
  assert.equal(requestHelp(none, 1000), null);
  const early = requestHelp(none, 1000, true);
  assert.equal(early.title, 'Your AI app has not reached Mac MCP yet');
  assert.equal(early.action.action, 'connect');
  assert.equal(requestHelp(none, HELP_AFTER_MS).title, early.title);
  const connected = requestHelp(evaluate({ server_running: true, clients_seen: [{ name: 'ChatGPT' }] }), HELP_AFTER_MS);
  assert.equal(connected.title, 'Not working yet?');
  assert.ok(connected.tips.some((tip) => /new chat/.test(tip)));
  assert.ok(connected.tips[0].startsWith('ChatGPT is connected but has not used Mac MCP yet'));
  const denied = evaluate({ server_running: true, first_call: { tool: 'read_file', status: 'denied' } });
  assert.equal(requestStatus(denied).state, 'problem');
  assert.equal(requestHelp(denied, 0).action.action, 'openPermissions');
  const failed = evaluate({ server_running: true, first_call: { tool: 'read_file', status: 'error' } });
  assert.equal(requestHelp(failed, 0).title, 'The request did not work');
  assert.equal(requestHelp(evaluate({ server_running: false }), 0).action.action, 'startServer');
  assert.equal(requestHelp(evaluate({ server_running: true, first_success: { tool: 'x' } }), HELP_AFTER_MS, true), null);
});

test('ChatGPT: numbered steps, a copy confirmation and a real check (#126)', () => {
  const local = evaluate({ server_running: true, public_endpoint_mode: 'none' });
  let view = chatgptView(local, {});
  assert.deepEqual(view.steps.map((step) => step.id), ['remote', 'copy', 'add', 'chat']);
  assert.equal(view.steps[0].action, 'openConnections');
  assert.equal(view.steps[1].disabled, true);
  assert.equal(view.steps[2].action, 'openChatGPT');
  assert.equal(view.status.title, 'Not connected yet');
  const remote = evaluate({ server_running: true, public_endpoint_mode: 'cloudflare' });
  view = chatgptView(remote, {});
  assert.equal(view.steps[0].done, true);
  assert.equal(view.steps[1].disabled, false);
  view = chatgptView(remote, { copied: true, reachable: true, copiedAt: 1000 }, 2000);
  assert.equal(view.steps[1].done, true);
  assert.match(view.steps[1].detail, /^Copied\./);
  assert.equal(view.status.title, 'Waiting for ChatGPT');
  assert.equal(view.help, null);
  assert.ok(chatgptView(remote, { copied: true, reachable: true, copiedAt: 1000 }, 1000 + CHATGPT_HELP_AFTER_MS).help);
  assert.ok(chatgptView(remote, { askedHelp: true }).help);
  // A public address that does not answer is said at once.
  view = chatgptView(remote, { copied: true, reachable: false, copiedAt: 1000 }, 1000);
  assert.equal(view.steps[1].warn, true);
  assert.match(view.help[0], /does not answer/);
  // Only ChatGPT's own check-in makes it connected.
  const seen = evaluate({ server_running: true, public_endpoint_mode: 'cloudflare', clients_seen: [{ name: 'ChatGPT', seen_at: 1 }] });
  view = chatgptView(seen, {});
  assert.equal(view.status.done, true);
  assert.equal(view.status.title, 'ChatGPT is connected');
  assert.equal(view.steps[2].done, true);
  const other = evaluate({ server_running: true, public_endpoint_mode: 'cloudflare', clients_seen: [{ name: 'Claude', seen_at: 1 }] });
  assert.equal(chatgptView(other, { copied: true }).status.done, false);
});

test('another AI app: the command form, the URL form, and the key only by copy (#126)', () => {
  assert.equal(otherRows(null), null);
  const rows = otherRows({ command: '/Users/x/.local/bin/mac-mcp', args: ['stdio'], config: '{"mcpServers":{}}',
    url: 'http://127.0.0.1:8765/mcp', public_url: 'https://mac.example.com/mcp', auth_required: true });
  assert.deepEqual(rows.command.map((row) => row.id), ['command', 'args', 'json']);
  assert.equal(rows.command[1].copy, 'stdio');
  assert.deepEqual(rows.url.map((row) => row.id), ['url', 'key', 'public']);
  assert.equal(rows.url[1].copyKey, true);
  assert.equal(rows.url[1].copy, undefined);
  const open = otherRows({ command: '/x/mac-mcp', args: ['stdio'], url: 'http://127.0.0.1:8000/mcp', auth_required: false });
  assert.deepEqual(open.url.map((row) => row.id), ['url']);
});

test('a CLI error shows its first line only', () => {
  assert.equal(shortError('chatGPT needs a public address.\nNext steps:\n  1. Open', 'x'), 'ChatGPT needs a public address.');
  assert.equal(shortError('', 'Fallback'), 'Fallback');
});
