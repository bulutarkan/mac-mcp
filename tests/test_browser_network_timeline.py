"""#137: {type:'network'} reads a small, bounded ring of the tab's recent requests, with URL secrets redacted."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent

# A page without a DOM: enough of window/performance for network.js, with a fake fetch and XHR.
_PAGE = r"""
globalThis.window = globalThis;
globalThis.location = {href: 'https://app.test/inbox'};
let now = 1000, observerCallback = null;
const past = Array.from({length: 150}, (_, i) => ({name: '/img/' + i + '.png', initiatorType: 'img', startTime: i, duration: 5, responseStatus: 200, transferSize: 10}));
globalThis.performance = {now: () => now, getEntriesByType: (type) => type === 'resource' ? past : []};
globalThis.PerformanceObserver = class { constructor(cb) { observerCallback = cb; } observe() {} };
globalThis.fetch = (input, init) => Promise.resolve({status: 201});
class FakeXHR { constructor() { this.listeners = {}; this.status = 0; }
  addEventListener(name, fn) { this.listeners[name] = fn; } };
FakeXHR.prototype.open = function () {};
FakeXHR.prototype.send = function () { this.status = 404; (this.listeners.loadend || (() => {}))(); };
globalThis.XMLHttpRequest = FakeXHR;
const state = {};
const BOOT = 'function __mcpState(){return globalThis.__state||(globalThis.__state={});}';
const read = (options) => JSON.parse(Buffer.from(eval(TEMPLATE.replace('__BOOTSTRAP__', BOOT).replace('__NETWORK_OPTIONS__', JSON.stringify(options))), 'base64').toString('utf8'));
(async () => {
  const out = [];
  out.push(read({max_items: 100}));
  now = 2000;
  await fetch('https://api.test/v1/items?token=s3cretvalue123&page=2', {method: 'post'});
  const xhr = new XMLHttpRequest(); xhr.open('DELETE', '/v1/items/7'); xhr.send();
  await new Promise((resolve) => setTimeout(resolve, 0));
  out.push(read({since: out[0].next_since}));
  out.push(read({url_contains: 'API.TEST'}));
  observerCallback({getEntries: () => Array.from({length: 300}, (_, i) => ({name: '/late/' + i + '.js', initiatorType: 'script', startTime: 3000 + i, duration: 1}))});
  out.push(read({max_items: 100}));
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NetworkRingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        template = agent._browser_js("network.js")
        script = f"const TEMPLATE = {json.dumps(template)};\n{_PAGE}"
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
        cls.first, cls.since, cls.filtered, cls.flooded = json.loads(proc.stdout)

    def test_first_read_starts_from_what_the_browser_lists_and_stays_bounded(self) -> None:
        self.assertTrue(self.first["started_now"])
        self.assertEqual(100, self.first["count"])
        self.assertEqual(0, self.first["dropped"])  # only the newest 100 the browser lists are taken
        self.assertEqual("/img/149.png", self.first["requests"][-1]["url"].rsplit("test", 1)[-1])
        self.assertEqual(("GET", 200), (self.first["requests"][-1]["method"], self.first["requests"][-1]["status"]))

    def test_fetch_and_xhr_after_the_first_read_carry_method_and_status(self) -> None:
        self.assertFalse(self.since["started_now"])
        rows = {row["kind"]: row for row in self.since["requests"]}
        self.assertEqual(("POST", 201), (rows["fetch"]["method"], rows["fetch"]["status"]))
        self.assertEqual(("DELETE", 404, "https://app.test/v1/items/7"), (rows["xhr"]["method"], rows["xhr"]["status"], rows["xhr"]["url"]))
        self.assertEqual(["fetch", "xhr"], self.since["recording"])

    def test_ring_never_grows_past_its_bound(self) -> None:
        self.assertEqual(100, len(self.flooded["requests"]))
        self.assertEqual(100, self.flooded["buffer_max"])
        self.assertEqual(302, self.flooded["dropped"])
        self.assertEqual(["fetch"], [row["kind"] for row in self.filtered["requests"]])


class NetworkActionTests(unittest.TestCase):
    def test_query_secrets_are_redacted_and_unfinished_requests_marked(self) -> None:
        page = {"ok": True, "url": "https://app.test/?access_token=abcdef123456", "requests": [
            {"seq": 1, "kind": "fetch", "method": "GET", "url": "https://api.test/x?api_key=K123456789&q=shoes", "status": 200, "duration_ms": 9},
            {"seq": 2, "kind": "xhr", "method": "POST", "url": "https://api.test/save?password=hunter2hunter2"},
        ]}
        with patch.object(agent, "_run_json_js", return_value=page) as run:
            out = agent._network_action(MagicMock(), "Safari", {"type": "network", "cursor": "7", "url_contains": "api"}, 1, 1, "tab")
        self.assertIn('"since": 7', run.call_args.args[2])
        text = json.dumps(out)
        for secret in ("K123456789", "hunter2hunter2", "abcdef123456"):
            self.assertNotIn(secret, text)
        self.assertIn("q=shoes", out["requests"][0]["url"])
        self.assertTrue(out["requests"][1]["pending"])
        self.assertNotIn("pending", out["requests"][0])

    def test_network_is_a_read_only_action(self) -> None:
        self.assertIn("network", agent._NON_MUTATING_ACT_TYPES)
        self.assertEqual("network", agent.normalize_act_actions([{"type": "Network"}])[0]["type"])


if __name__ == "__main__":
    unittest.main()
