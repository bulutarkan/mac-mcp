import { test } from 'node:test';
import assert from 'node:assert/strict';
import steps from '../steps.js';

const { SCREENS, CLIENTS, evaluate, primaryAction, indexOf } = steps;

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
