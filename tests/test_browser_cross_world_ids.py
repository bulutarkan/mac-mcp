"""Element ids survive Chrome running page scripts in a second world (debugger vs Apple Events).

When another extension's frame (a password manager on a sign-in field) blocks the companion's debugger,
Mac MCP falls back to Apple Events JavaScript, which runs in a different world of the same page: the
first world's window state is invisible there. Ids, the page token and the counter live on the DOM, so
an id from one world still finds its element in the other and the two worlds never hand out one id twice.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "mcp_server" / "browser_js" / "agent_helpers.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
// One DOM shared by both worlds; each world has its own window globals.
function el(tag) {
  const attrs = {};
  return {tagName: tag.toUpperCase(), isConnected: true, attrs,
    getAttribute(n) { return n in attrs ? attrs[n] : null; },
    setAttribute(n, v) { attrs[n] = String(v); }};
}
const root = el('html');
const nodes = [el('input'), el('textarea'), el('button')];
const document = {
  documentElement: root, readyState: 'complete',
  querySelector(sel) {
    const m = /^\[data-mac-mcp-id="([^"]+)"\]$/.exec(sel);
    return m ? (nodes.find((n) => n.getAttribute('data-mac-mcp-id') === m[1]) || null) : null;
  },
};
const source = fs.readFileSync(process.argv[1], 'utf8');
function world() {
  const ctx = {document, Math, Date, Number, String, Object, Array, WeakMap, RegExp, JSON,
    performance: {getEntriesByType: () => [], now: () => 0}, setTimeout, clearTimeout};
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return ctx;
}
const a = world(), b = world();
const sa = vm.runInContext('__mcpState()', a);
const idInput = a.__mcpId(nodes[0], sa), idArea = a.__mcpId(nodes[1], sa);
const sb = vm.runInContext('__mcpState()', b);
const out = {
  sameToken: sa.pageToken === sb.pageToken,
  recovered: b.__mcpRecoverElement(idArea, sb) === nodes[1],
  reused: b.__mcpId(nodes[0], sb) === idInput,
  fresh: b.__mcpId(nodes[2], sb),
  ids: [idInput, idArea],
  bogus: b.__mcpRecoverElement('e_x"]', sb),
};
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CrossWorldIdTests(unittest.TestCase):
    def test_an_id_from_one_world_finds_its_element_in_the_other(self) -> None:
        proc = subprocess.run(["node", "-e", _HARNESS, str(HELPERS)], capture_output=True, text=True,
                              timeout=30, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["sameToken"])
        self.assertTrue(out["recovered"])
        self.assertTrue(out["reused"])
        # The second world continues the shared counter instead of handing out an id again.
        self.assertNotIn(out["fresh"], out["ids"])
        self.assertTrue(out["fresh"].endswith("_3"))
        self.assertIsNone(out["bogus"])


if __name__ == "__main__":
    unittest.main()
