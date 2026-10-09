from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent
from mcp_server.browser_schemas import validate_action


def _page(*names, at_end=False):
    return {"ok": True, "at_end": at_end, "container": {"tag": "div", "scroll_top": 0},
            "items": [{"key": name.lower(), "text": name, "href": ""} for name in names]}


class ScanLoopTests(unittest.TestCase):
    def _scan(self, pages, **action):
        steps = iter(pages)
        with patch.object(agent, "_run_json_js", side_effect=lambda *a, **k: next(steps)), \
                patch.object(agent, "cancellable_sleep", lambda seconds: None):
            return agent._scan_action(None, "Google Chrome", {"type": "scan", **action}, 1, 1, "tab")

    def test_collects_beyond_the_first_render_and_stops_at_the_end(self) -> None:
        result = self._scan([_page("A", "B", "C"), _page("C", "D", "E"), _page("E", "F", at_end=True),
                             _page("F", at_end=True), _page("F", at_end=True)])
        self.assertEqual(["A", "B", "C", "D", "E", "F"], [item["text"] for item in result["items"]])
        self.assertEqual("end_of_list", result["stopped"])
        self.assertTrue(result["complete"])
        self.assertEqual(5, result["steps"])

    def test_items_loaded_after_reaching_the_bottom_are_still_collected(self) -> None:
        # Infinite scroll: the page is at its end until the scroll event loads more.
        result = self._scan([_page("A", at_end=True), _page("A", "B", at_end=True), _page("B", "C", at_end=True),
                             _page("C", at_end=True), _page("C", at_end=True)])
        self.assertEqual(["A", "B", "C"], [item["text"] for item in result["items"]])
        self.assertEqual("end_of_list", result["stopped"])

    def test_stalled_loading_stops_after_two_empty_steps(self) -> None:
        result = self._scan([_page("A"), _page("A"), _page("A"), _page("Z")])
        self.assertEqual("no_new_items", result["stopped"])
        self.assertFalse(result["complete"])
        self.assertEqual(3, result["steps"])

    def test_item_step_and_payload_caps(self) -> None:
        many = [_page(*[f"row {step}-{i}" for i in range(10)]) for step in range(10)]
        self.assertEqual(("max_items", 15), (lambda r: (r["stopped"], r["count"]))(self._scan(many, max_items=15)))
        self.assertEqual("max_steps", self._scan(many, max_steps=2)["stopped"])
        big = [_page(*[("x" * 190) + f"{step}-{i}" for i in range(400)]) for step in range(3)]
        self.assertEqual("payload_budget", self._scan(big, max_items=500)["stopped"])

    def test_page_errors_are_returned_with_what_was_collected(self) -> None:
        result = self._scan([_page("A"), {"ok": False, "error": "stale_element", "observe_again": True}])
        self.assertFalse(result["ok"])
        self.assertEqual("stale_element", result["error"])
        self.assertEqual(["A"], [item["text"] for item in result["items"]])

    def test_scan_is_a_valid_action_type(self) -> None:
        self.assertEqual("scan", validate_action(0, {"type": "scan", "max_items": 50})["type"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_step_script_is_valid_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "scan.js"
            script.write_text(agent._scan_step_js("e_1", "li.result", "href"), encoding="utf-8")
            completed = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
