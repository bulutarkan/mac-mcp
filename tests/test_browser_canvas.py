from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent


class PointActionTests(unittest.TestCase):
    def _run(self, action, check, browser="Google Chrome"):
        with patch.object(agent, "_run_json_js", return_value=check), \
                patch.object(agent, "_chrome_native_id", return_value="7"), \
                patch.object(agent.chrome_background_bridge, "request_dispatch_mouse", return_value={"ok": True}) as click, \
                patch.object(agent.chrome_background_bridge, "request_gesture", return_value={"ok": True}) as gesture:
            result = agent._point_action(None, browser, action, "bobs_1", 1, 1, "t")
        return result, click, gesture

    def test_recognizes_coordinate_actions_only_without_a_target(self) -> None:
        self.assertTrue(agent._is_point_action({"type": "click", "x": 10, "y": 20}))
        self.assertFalse(agent._is_point_action({"type": "click", "x": 10, "y": 20, "element_id": "e_1"}))
        self.assertFalse(agent._is_point_action({"type": "click", "x": 10, "y": 20, "query": "Save"}))
        self.assertFalse(agent._is_point_action({"type": "type", "x": 10, "y": 20, "text": "a"}))

    def test_click_and_hover_dispatch_trusted_input_at_the_point(self) -> None:
        result, click, _ = self._run({"type": "double_click", "x": 120, "y": 80}, {"ok": True, "hit": {"tag": "canvas"}})
        self.assertTrue(result["ok"])
        self.assertEqual(("7", 120.0, 80.0), click.call_args.args)
        self.assertEqual(2, click.call_args.kwargs["click_count"])
        result, _, gesture = self._run({"type": "hover", "x": 5, "y": 6}, {"ok": True})
        self.assertEqual([("move", 5.0, 6.0)], [(s["type"], s["x"], s["y"]) for s in gesture.call_args.args[1]])

    def test_moved_page_is_refused_before_input(self) -> None:
        result, click, _ = self._run({"type": "click", "x": 1, "y": 1},
                                     {"ok": False, "error": "stale_coordinates", "reason_code": "VIEWPORT_CHANGED"})
        self.assertEqual("stale_coordinates", result["error"])
        click.assert_not_called()

    def test_safari_has_no_background_pointer(self) -> None:
        result, click, _ = self._run({"type": "click", "x": 1, "y": 1}, {"ok": True}, browser="Safari")
        self.assertEqual("POINT_INPUT_UNSUPPORTED_SAFARI", result["reason_code"])
        click.assert_not_called()


class TrustedKeyTests(unittest.TestCase):
    def _press(self, action):
        with patch.object(agent, "_chrome_native_id", return_value="7"), \
                patch.object(agent.chrome_background_bridge, "request_gesture", return_value={"ok": True}) as gesture:
            result = agent._trusted_key_action("Google Chrome", action, "t")
        return result, gesture

    def test_printable_key_carries_text_and_shortcuts_do_not(self) -> None:
        result, gesture = self._press({"type": "key", "key": "a"})
        step = gesture.call_args.args[1][0]
        self.assertEqual(("a", "KeyA", "a", 0), (step["key"], step["code"], step["text"], step["modifiers"]))
        self.assertEqual("keys", gesture.call_args.kwargs["feature"])
        _, gesture = self._press({"type": "key", "key": "z", "modifiers": ["cmd", "shift"]})
        step = gesture.call_args.args[1][0]
        self.assertEqual((4 | 8, ""), (step["modifiers"], step["text"]))
        _, gesture = self._press({"type": "key", "key": "ArrowDown"})
        self.assertEqual("", gesture.call_args.args[1][0]["text"])

    def test_unknown_key_is_refused(self) -> None:
        result, gesture = self._press({"type": "key", "key": "Hyperdrive"})
        self.assertEqual("unsupported_key", result["error"])
        gesture.assert_not_called()


class CanvasObservationTests(unittest.TestCase):
    def test_observation_records_its_viewport_and_flags_canvas_pages(self) -> None:
        source = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertIn("view:{{sx:scrollX,sy:scrollY,w:innerWidth,h:innerHeight}}", source)
        self.assertIn("canvas:canvasHint", source)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_point_check_script_is_valid_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "point.js"
            for source in (agent._point_check_js("bobs_1", 10, 20), agent._observe_js("interactive", 40)):
                script.write_text(source, encoding="utf-8")
                completed = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
