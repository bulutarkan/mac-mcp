"""#170: the Chrome companion backs off while the Mac MCP server is unreachable."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

BACKGROUND = Path(__file__).parents[1] / "menu_app" / "ChromeVisualCompanion" / "background.js"

# Runs background.js with stubbed Chrome APIs, a fake WebSocket and manual timers.
_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const listeners = {};
const on = (name) => ({addListener: (fn) => { (listeners[name] = listeners[name] || []).push(fn); }});
const timers = [];
const sockets = [];
let serverUp = false;
class FakeSocket {
  constructor(url) { this.url = url; this.readyState = FakeSocket.CONNECTING; this.handlers = {}; this.sent = []; sockets.push(this); }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  emit(type, data) { (this.handlers[type] || []).forEach((fn) => fn(data)); }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() { if (this.readyState === FakeSocket.CLOSED) return; this.readyState = FakeSocket.CLOSED; this.emit('close', {}); }
}
FakeSocket.CONNECTING = 0; FakeSocket.OPEN = 1; FakeSocket.CLOSING = 2; FakeSocket.CLOSED = 3;
const context = {
  console, JSON, Math, Number, String, Promise, Map, Set, Object, Array, Error,
  WebSocket: FakeSocket,
  setTimeout: (fn, ms) => { const t = {fn, ms, cleared: false}; timers.push(t); return t; },
  clearTimeout: (t) => { if (t) t.cleared = true; },
  setInterval: () => ({}), clearInterval: () => {},
  importScripts: () => { context.MAC_MCP_CHROME_BRIDGE = {port: 9999, token: 'test-token', reconnect_ms: 1000}; },
  chrome: {
    runtime: {onStartup: on('startup'), onInstalled: on('installed'), onMessage: on('message'),
              getManifest: () => ({version: '1.0'}), lastError: null},
    alarms: {create: () => {}, onAlarm: on('alarm')},
    debugger: {onDetach: on('detach'), onEvent: on('event')},
    tabs: {onRemoved: on('removed')},
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);

// Drive every socket the companion opens: fail while the server is down, accept when up.
function settle() {
  const ws = sockets[sockets.length - 1];
  if (!ws || ws.readyState !== FakeSocket.CONNECTING) return;
  if (!serverUp) { ws.emit('error', {}); ws.close(); return; }
  ws.readyState = FakeSocket.OPEN; ws.emit('open', {});
  ws.emit('message', {data: JSON.stringify({type: 'hello_ack'})});
}
function pending() { return timers.filter((t) => !t.cleared && !t.fired); }
function fireNext() { const t = pending()[0]; t.fired = true; t.fn(); settle(); return t.ms; }

const out = {};
settle();  // the connect() at load
out.delays = [];
for (let i = 0; i < 9; i++) out.delays.push(fireNext());
// Alarms and page wakes during a backoff wait open no extra sockets.
const before = sockets.length;
listeners.alarm.forEach((fn) => fn({name: 'mac-mcp-reconnect'}));
listeners.message.forEach((fn) => fn({type: 'mac_mcp_bridge_wake'}));
out.extraSocketsDuringWait = sockets.length - before;
out.pendingTimers = pending().length;
// The server comes back: the next attempt is accepted and the backoff resets.
serverUp = true;
fireNext();
out.connected = sockets[sockets.length - 1].readyState === FakeSocket.OPEN;
out.hello = sockets[sockets.length - 1].sent[0];
sockets[sockets.length - 1].close();  // server restart
out.afterRestart = pending()[0].ms;
// A socket that opens but is never accepted (bad token) still counts as a failure.
serverUp = false;
const open = () => { const ws = sockets[sockets.length - 1]; ws.readyState = FakeSocket.OPEN; ws.emit('open', {}); ws.close(); };
fireNext();  // a plain failure
const retry = pending()[0]; retry.fired = true; retry.fn(); open();  // opens, then closes without hello_ack
out.afterRejected = pending()[0].ms;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ChromeCompanionBackoffTests(unittest.TestCase):
    def run_harness(self) -> dict:
        proc = subprocess.run(["node", "-e", _HARNESS, str(BACKGROUND)], capture_output=True, text=True,
                              timeout=30, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return json.loads(proc.stdout)

    def test_failed_connections_back_off_exponentially_with_jitter_up_to_30s(self) -> None:
        delays = self.run_harness()["delays"]
        for attempt, delay in enumerate(delays, start=1):
            ceiling = min(1000 * 2 ** attempt, 30000)
            with self.subTest(attempt=attempt):
                self.assertGreaterEqual(delay, ceiling * 0.5)
                self.assertLessEqual(delay, ceiling)
        self.assertGreaterEqual(delays[-1], 15000)  # capped, not growing without bound

    def test_alarms_and_wakes_wait_for_the_pending_retry(self) -> None:
        out = self.run_harness()
        self.assertEqual(0, out["extraSocketsDuringWait"])
        self.assertEqual(1, out["pendingTimers"])

    def test_an_accepted_connection_resets_the_backoff(self) -> None:
        out = self.run_harness()
        self.assertTrue(out["connected"])
        self.assertEqual({"type": "hello", "token": "test-token"}, {k: out["hello"][k] for k in ("type", "token")})
        self.assertLessEqual(out["afterRestart"], 1000)  # first retry after a server restart is quick again

    def test_an_open_socket_that_is_never_accepted_still_backs_off(self) -> None:
        self.assertGreater(self.run_harness()["afterRejected"], 1000)

    def test_backoff_cap_coalescing_and_revival_alarm_are_in_place(self) -> None:
        source = BACKGROUND.read_text()
        self.assertIn("const MAX_RECONNECT_MS = 30000;", source)
        self.assertIn("if (trigger !== 'timer' && reconnectTimer) return;", source)
        # The 30-second alarm that revives a suspended worker is kept.
        self.assertIn("chrome.alarms.create('mac-mcp-reconnect', {periodInMinutes: 0.5});", source)


if __name__ == "__main__":
    unittest.main()
