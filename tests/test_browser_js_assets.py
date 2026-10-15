"""#107: authored page scripts live in mcp_server/browser_js as .js assets."""
from __future__ import annotations

import inspect
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from mcp_server import tools_browser_agent as agent

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "mcp_server" / "browser_js"
# Templates take their data through these placeholders; the values only fill them for parsing.
PLACEHOLDERS = {
    "__BOOTSTRAP__": "", "__SPEC__": "{}", "__QUIET_MS__": "50", "__OBS__": "null", "__ACTIONS__": "[]",
    "__RASTERIZER__": "h2c", "__STATE_KEY__": '"k"', "__MODE__": '"viewport"', "__ELEMENT_ID__": "null",
    "__MAX_CSS_HEIGHT__": "1", "__MAX_DATA_URL_CHARS__": "1", "__SCOPE__": '"interactive"', "__MAX_ELEMENTS__": "40",
    "__QUERY__": '""', "__ROLE__": '""', "__TEXT__": '""', "__ACTIONABLE_ONLY__": "false", "__COLLECT_LIMIT__": "80",
    "__WITHIN_BLOCK__": "", "__MAX_CANDIDATES__": "80", "__SPECS__": "[]", "__ALIASES__": "{}", "__BUDGET__": "4000",
    "__CONTAINER_ID__": '""', "__ITEM_SELECTOR__": '""', "__KEY_FIELD__": '"text"',
}
# The larger page scripts once lived in Python f-strings ({{ }} escaping); they are assets now.
ASSET_BUILDERS = {
    "_dom_capture_start_js": "dom_capture.js", "_observe_js": "observe.js", "_find_candidates_js": "find.js",
    "_extract_action_js": "extract.js", "_scan_step_js": "scan_step.js",
}


class BrowserJsAssetTests(unittest.TestCase):
    def test_every_referenced_asset_exists_and_is_packaged(self) -> None:
        source = (ROOT / "mcp_server" / "tools_browser_agent.py").read_text(encoding="utf-8")
        referenced = set(re.findall(r'_browser_js\("([^"]+)"\)', source)) | {"agent_helpers.js"}
        self.assertEqual(referenced, {path.name for path in ASSETS.glob("*.js")})
        # Installed builds must ship the assets (tomllib is not available on Python 3.10).
        package_data = re.search(r"^mcp_server = \[(.*)\]$", (ROOT / "pyproject.toml").read_text(), re.M)
        self.assertIsNotNone(package_data)
        self.assertIn('"browser_js/*.js"', package_data.group(1))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_assets_parse_as_javascript(self) -> None:
        for path in sorted(ASSETS.glob("*.js")):
            text = path.read_text(encoding="utf-8")
            for name, value in PLACEHOLDERS.items():
                text = text.replace(name, value)
            with self.subTest(asset=path.name):
                proc = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
                                      input=text, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(0, proc.returncode, proc.stderr)

    def test_data_is_filled_in_after_the_library_so_it_is_never_re_substituted(self) -> None:
        # Values that look like placeholders stay literal data in the generated script.
        batch = agent._batch_js([{"type": "type", "element_id": "e1", "text": "__OBS__ __BOOTSTRAP__"}], "bobs__ACTIONS__")
        self.assertIn('"text": "__OBS__ __BOOTSTRAP__"', batch)
        self.assertIn('var expected="bobs__ACTIONS__";', batch)
        wait = agent._event_wait_js({"for": "selector", "selector": "#__SPEC__"}, "https://e.test/", 2.0)
        self.assertIn('"selector": "#__SPEC__"', wait)

    def test_large_page_scripts_are_assets_not_f_strings(self) -> None:
        for builder, asset in ASSET_BUILDERS.items():
            with self.subTest(builder=builder):
                source = inspect.getsource(getattr(agent, builder))
                self.assertIn(f'_browser_js("{asset}")', source)
                self.assertNotIn("(function(){{", source)
        # Every placeholder an asset uses is one this test can fill to parse it.
        for path in sorted(ASSETS.glob("*.js")):
            if path.name == "agent_helpers.js":
                continue
            with self.subTest(asset=path.name):
                used = set(re.findall(r"__[A-Z_]+__", path.read_text(encoding="utf-8")))
                self.assertLessEqual(used, set(PLACEHOLDERS))

    def test_data_that_looks_like_a_placeholder_stays_data_in_the_moved_builders(self) -> None:
        extract = agent._extract_action_js([{"name": "__BUDGET__", "selector": "h1"}], 500)
        self.assertIn('"name": "__BUDGET__"', extract)
        self.assertIn("budget=500", extract)
        scan = agent._scan_step_js("__ITEM_SELECTOR__", "li", "text")
        self.assertIn('cid="__ITEM_SELECTOR__", sel="li"', scan)
        capture = agent._dom_capture_start_js("element", "__MODE__", state_key="k")
        self.assertIn('var mode="element", elementId="__MODE__";', capture)

    def test_the_helper_library_comes_from_its_asset(self) -> None:
        self.assertEqual((ASSETS / "agent_helpers.js").read_text(encoding="utf-8"), agent._bootstrap_functions_source())
        self.assertIn(agent._bootstrap_functions_source().strip()[:120], agent._agent_api()["full"])


if __name__ == "__main__":
    unittest.main()
