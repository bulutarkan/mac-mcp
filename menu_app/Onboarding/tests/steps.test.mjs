import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import steps from '../steps.js';

const {
  SCREENS, CLIENTS, AGENTS, evaluate, primaryAction, indexOf, appRow, agentRow, orderedClients,
  agentsSummary, appGroups, callSentence,
} = steps;

test('the tour comes first, then the setup in order', () => {
  assert.deepEqual(SCREENS.map((s) => s.id),
    ['welcome', 'apps', 'browser', 'safety', 'start', 'permissions', 'connect', 'request', 'done']);
  assert.equal(indexOf('start'), 4);
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
  assert.deepEqual(primaryAction('welcome', idle), { label: 'Show me how it works', action: 'next' });
  assert.deepEqual(primaryAction('start', idle), { label: 'Start Mac MCP', action: 'startServer' });
  const running = evaluate({ server_running: true, accessibility: 'granted', screen_recording: 'granted' });
  assert.deepEqual(primaryAction('start', running), { label: 'Continue', action: 'next' });
  assert.deepEqual(primaryAction('permissions', running), { label: 'Continue', action: 'next' });
  assert.deepEqual(primaryAction('connect', running), { label: 'Skip for now', action: 'next' });
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
  assert.equal(appRow(chatgpt, undefined, false).action, 'settings');
  assert.equal(appRow(chatgpt, undefined, true).button, 'Copy address');
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
  assert.deepEqual(AGENTS.map((a) => a.id), ['codex', 'claude', 'opencode', 'kimi', 'grok']);
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
});

test('the apps card shows what needs a click, then one row for the apps already set up', () => {
  const apps = [
    { id: 'claude-desktop', installed: true, added: false },
    { id: 'claude-code', installed: true, added: true },
    { id: 'codex', installed: true, added: true },
    { id: 'opencode', installed: false, added: false },
  ];
  const groups = appGroups(apps, false);
  assert.deepEqual(groups.action.map(({ client }) => client.id), ['claude-desktop', 'chatgpt']);
  assert.deepEqual(groups.added.map(({ client }) => client.id), ['claude-code', 'codex']);
  assert.equal(groups.present, 4);
  assert.equal(groups.all.length, CLIENTS.length);
  assert.equal(groups.all[groups.all.length - 1].client.id, 'opencode');
});

test('the first call is said in words, not as a tool name', () => {
  assert.equal(callSentence('run_command'), 'Your AI app ran a command on this Mac.');
  assert.equal(callSentence('read_file'), 'Your AI app read files on this Mac.');
  assert.equal(callSentence('browser_observe'), 'Your AI app looked at a web page.');
  assert.equal(callSentence('get_system_info'), 'Your AI app checked this Mac.');
  assert.equal(callSentence('something_new'), 'Your AI app used Mac MCP.');
  assert.equal(callSentence(null), 'Your AI app used Mac MCP.');
});
