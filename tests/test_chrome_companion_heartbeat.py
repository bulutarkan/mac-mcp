"""A companion socket that looks open but no longer hears the server is dropped and reconnected.

After Mac MCP closed a Chrome tab, the bridge went quiet while the worker kept believing its socket was
OPEN, so every page wake and alarm was skipped as 'already connected' and Chrome calls failed until
the extension was reloaded.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

BACKGROUND = Path(__file__).parents[1] / "menu_app" / "ChromeVisualCompanion" / "background.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const listeners = {};
const on = (name) => ({addListener: (fn) => { (listeners[name] = listeners[name] || []).push(fn); }});
const sockets = [];
const intervals = [];
let clock = 1000000;
class FakeSocket {
  constructor(url) { this.readyState = FakeSocket.CONNECTING; this.handlers = {}; this.sent = []; sockets.push(this); }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  emit(type, data) { (this.handlers[type] || []).forEach((fn) => fn(data)); }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() { if (this.readyState === FakeSocket.CLOSED) return; this.readyState = FakeSocket.CLOSED; this.emit('close', {code: 1006}); }
}
FakeSocket.CONNECTING = 0; FakeSocket.OPEN = 1; FakeSocket.CLOSING = 2; FakeSocket.CLOSED = 3;
const context = {
  console, JSON, Math, Number, String, Promise, Map, Set, Object, Array, Error,
  Date: {now: () => clock},
  WebSocket: FakeSocket,
  setTimeout: () => ({}), clearTimeout: () => {},
  setInterval: (fn) => { const t = {fn, cleared: false}; intervals.push(t); return t; },
  clearInterval: (t) => { if (t) t.cleared = true; },
  importScripts: () => { context.MAC_MCP_CHROME_BRIDGE = {port: 9999, token: 'test-token', reconnect_ms: 1000}; },
  chrome: {
    runtime: {onStartup: on('startup'), onInstalled: on('installed'), onMessage: on('message'),
              getManifest: () => ({version: '1.0'}), getPlatformInfo: (cb) => cb && cb({}), lastError: null},
    alarms: {create: () => {}, onAlarm: on('alarm')},
    debugger: {onDetach: on('detach'), onEvent: on('event')},
    tabs: {onRemoved: on('removed')},
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const out = {};
const first = sockets[0];
first.readyState = FakeSocket.OPEN; first.emit('open', {});
first.emit('message', {data: JSON.stringify({type: 'hello_ack'})});
// A page wake while the server answers: nothing new.
clock += 10000;
listeners.message.forEach((fn) => fn({type: 'mac_mcp_bridge_wake'}));
out.socketsWhileHealthy = sockets.length;
// The server goes quiet but this side still says OPEN: the next wake reconnects at once.
clock += 31000;
listeners.message.forEach((fn) => fn({type: 'mac_mcp_bridge_wake'}));
out.socketsAfterStaleWake = sockets.length;
out.firstClosed = first.readyState === FakeSocket.CLOSED;
const second = sockets[1];
second.readyState = FakeSocket.OPEN; second.emit('open', {});
second.emit('message', {data: JSON.stringify({type: 'hello_ack'})});
out.secondPingTimerLive = intervals.filter((t) => !t.cleared).length === 1;
// The heartbeat alone also notices a silent socket.
clock += 60000;
intervals.filter((t) => !t.cleared).forEach((t) => t.fn());
out.socketsAfterSilentHeartbeat = sockets.length;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CompanionHeartbeatTests(unittest.TestCase):
    def test_a_silent_open_socket_is_replaced(self) -> None:
        proc = subprocess.run(["node", "-e", _HARNESS, str(BACKGROUND)], capture_output=True, text=True,
                              timeout=30, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(1, out["socketsWhileHealthy"])
        self.assertEqual(2, out["socketsAfterStaleWake"])
        self.assertTrue(out["firstClosed"])
        # Closing the stale socket must not stop the new socket's heartbeat.
        self.assertTrue(out["secondPingTimerLive"])
        self.assertEqual(3, out["socketsAfterSilentHeartbeat"])


if __name__ == "__main__":
    unittest.main()
