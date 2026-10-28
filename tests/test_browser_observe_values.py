"""A repeat observe after a typed value is not 'not_modified'.

The page's mutation revision counts DOM changes only: typing into a field or choosing an option moves
no node, so an observe with previous_observation_id answered not_modified after a fill. Observations
now keep a fingerprint of the form controls' values and the check compares it too.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from mcp_server import tools_browser_agent as agent

HELPERS = Path(__file__).parents[1] / "mcp_server" / "browser_js" / "agent_helpers.js"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const fields = [{value: '', checked: false}, {value: 'x', checked: true}];
const document = {documentElement: {getAttribute: () => null, setAttribute: () => {}},
  querySelectorAll: () => fields};
const ctx = {document, Math, Date, Number, String, Object, Array, WeakMap, RegExp, JSON,
  performance: {getEntriesByType: () => [], now: () => 0}, setTimeout, clearTimeout};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const before = ctx.__mcpValuesFp();
const same = ctx.__mcpValuesFp();
fields[0].value = 'Ada';
const typed = ctx.__mcpValuesFp();
fields[1].checked = false;
const unchecked = ctx.__mcpValuesFp();
console.log(JSON.stringify({sameWhenUnchanged: before === same, typedDiffers: typed !== before,
  checkDiffers: unchecked !== typed}));
"""


class ObserveValuesTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_fingerprint_follows_typed_values_and_checks(self) -> None:
        proc = subprocess.run(["node", "-e", _HARNESS, str(HELPERS)], capture_output=True, text=True,
                              timeout=30, check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual({"sameWhenUnchanged": True, "typedDiffers": True, "checkDiffers": True},
                         json.loads(proc.stdout))

    def test_observations_keep_it_and_not_modified_compares_it(self) -> None:
        observe = (Path(__file__).parents[1] / "mcp_server" / "browser_js" / "observe.js").read_text()
        self.assertIn("values:__mcpValuesFp()", observe)
        check = agent._conditional_observe_js("bobs_x_1", "interactive", 40)
        self.assertIn("meta.values===__mcpValuesFp()", check)


if __name__ == "__main__":
    unittest.main()
