from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser_agent as agent
from mcp_server.browser_schemas import validate_action
from mcp_server.chrome_background_bridge import ChromeBackgroundBridge

EXTENSION = Path(__file__).resolve().parents[1] / "menu_app" / "ChromeVisualCompanion"


class BridgeFeatureTests(unittest.TestCase):
    def test_an_older_companion_is_asked_to_reload_before_anything_is_sent(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), object(), features=None)
        with patch.object(bridge, "_request") as sent:
            with self.assertRaises(HTTPException) as ctx:
                bridge.request_handle_dialog(7, True)
        sent.assert_not_called()
        self.assertEqual("chrome_companion_update_required", ctx.exception.detail["error"])
        self.assertEqual("dialogs", ctx.exception.detail["feature"])

    def test_a_current_companion_gets_the_request(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), object(), features=["dialogs", "gestures", "alarm_reconnect"])
        with patch.object(bridge, "_request", return_value={"ok": True}) as sent:
            bridge.request_gesture(7, [{"type": "move", "x": 1, "y": 2}])
            bridge.request_handle_dialog(7, False, "x")
        self.assertEqual("gesture", sent.call_args_list[0].args[0])
        self.assertEqual({"chrome_tab_id": 7, "accept": False, "prompt_text": "x"}, sent.call_args_list[1].args[1])

    def test_dialog_details_reach_the_error(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), object(), features=[])
        response = {"ok": False, "error": "browser_dialog_open", "message": "A native browser dialog is open",
                    "dialog": {"dialog_type": "confirm", "message": "Delete?"}}
        with patch("mcp_server.chrome_background_bridge.asyncio.run_coroutine_threadsafe") as run, \
                patch.object(ChromeBackgroundBridge, "_send", new=lambda *args, **kwargs: None):
            run.return_value.result.return_value = None

            def answer(*args, **kwargs):
                with bridge._lock:
                    pending = next(iter(bridge._pending.values()))
                pending.response = response
                pending.event.set()
                return run.return_value
            run.side_effect = answer
            with self.assertRaises(HTTPException) as ctx:
                bridge.request_execute_js(7, "1")
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("confirm", ctx.exception.detail["dialog"]["dialog_type"])
        self.assertFalse(ctx.exception.detail["retryable"])
        self.assertIn("type: 'dialog'", ctx.exception.detail["required_action"])


class DialogActionTests(unittest.TestCase):
    def test_needs_an_explicit_decision_and_chrome(self) -> None:
        self.assertEqual("invalid_dialog_decision", agent._dialog_action("Google Chrome", {"type": "dialog"}, "t")["error"])
        safari = agent._dialog_action("Safari", {"type": "dialog", "decision": "accept"}, "t")
        self.assertEqual("DIALOG_UNSUPPORTED_SAFARI", safari["reason_code"])

    def test_answers_through_the_companion(self) -> None:
        with patch.object(agent, "_chrome_native_id", return_value="7"), \
                patch.object(agent.chrome_background_bridge, "request_handle_dialog",
                             return_value={"ok": True, "dialog": {"dialog_type": "confirm"}}) as answer:
            result = agent._dialog_action("Google Chrome", {"type": "dialog", "decision": "dismiss"}, "t")
        self.assertTrue(result["ok"])
        answer.assert_called_once_with("7", False, None)


class LeadingDialogTests(unittest.TestCase):
    def test_leading_dialog_is_answered_before_any_page_script(self) -> None:
        from contextlib import contextmanager
        from types import SimpleNamespace

        @contextmanager
        def lease(*args, **kwargs):
            yield SimpleNamespace(tab_handle="t", browser="Google Chrome", window_index=1, tab_index=1, native_id="7")

        with patch.object(agent, "_tab_lease", lease), \
                patch.object(agent, "_require_stable_handle_for_mutation"), \
                patch.object(agent, "_ensure_visual_companion", side_effect=AssertionError("page touched")), \
                patch.object(agent, "_dialog_action", return_value={"type": "dialog", "ok": True}) as answer:
            result = agent.browser_act(None, "Google Chrome", [{"type": "dialog", "decision": "dismiss"}], tab_handle="t")
        self.assertTrue(result["ok"])
        answer.assert_called_once()

    def test_failed_answer_stops_the_batch(self) -> None:
        from contextlib import contextmanager
        from types import SimpleNamespace

        @contextmanager
        def lease(*args, **kwargs):
            yield SimpleNamespace(tab_handle="t")

        with patch.object(agent, "_tab_lease", lease), \
                patch.object(agent, "_require_stable_handle_for_mutation"), \
                patch.object(agent, "_dialog_action", return_value={"type": "dialog", "ok": False, "error": "no_dialog_open"}):
            result = agent.browser_act(None, "Google Chrome", [{"type": "dialog", "decision": "accept"},
                                                               {"type": "click", "query": "Next"}], tab_handle="t")
        self.assertFalse(result["ok"])
        self.assertEqual("no_dialog_open", result["error"])
        self.assertEqual(1, result["action_count"])


class GestureActionTests(unittest.TestCase):
    def _run(self, typ, action, centers, **kwargs):
        seq = iter(centers)
        with patch.object(agent, "_run_json_js", side_effect=lambda *a, **k: next(seq)), \
                patch.object(agent, "_chrome_native_id", return_value="7"), \
                patch.object(agent.chrome_background_bridge, "request_gesture",
                             return_value={"ok": True, "steps_done": 3}) as gesture:
            result = agent._gesture_action(None, "Google Chrome", typ, {"element_id": "e_1"}, action, 1, 1, "t",
                                           resolve_target=kwargs.get("resolve", lambda a: ({}, None)))
        return result, gesture

    def test_hover_moves_the_pointer_to_the_target_center(self) -> None:
        result, gesture = self._run("hover", {"type": "hover"}, [{"ok": True, "x": 40, "y": 20, "in_view": True}])
        self.assertTrue(result["ok"])
        steps = gesture.call_args.args[1]
        self.assertEqual([("move", 40.0, 20.0)], [(s["type"], s["x"], s["y"]) for s in steps])

    def test_drag_presses_moves_and_releases_at_the_drop_target(self) -> None:
        result, gesture = self._run("drag", {"type": "drag", "to_element_id": "e_9"},
                                    [{"ok": True, "x": 10, "y": 10, "in_view": True},
                                     {"ok": True, "x": 110, "y": 60, "in_view": True}])
        steps = gesture.call_args.args[1]
        self.assertEqual(["move", "down"], [s["type"] for s in steps[:2]])
        self.assertEqual(("up", 110.0, 60.0), (steps[-1]["type"], steps[-1]["x"], steps[-1]["y"]))
        self.assertEqual({"x": 110.0, "y": 60.0}, result["to"])

    def test_drag_by_offset_and_offscreen_endpoints(self) -> None:
        result, gesture = self._run("drag", {"type": "drag", "dx": 50, "dy": 0},
                                    [{"ok": True, "x": 10, "y": 10, "in_view": True}])
        self.assertEqual(60.0, gesture.call_args.args[1][-1]["x"])
        offscreen, gesture = self._run("drag", {"type": "drag", "to_element_id": "e_9"},
                                       [{"ok": True, "x": 10, "y": 10, "in_view": True},
                                        {"ok": True, "x": 10, "y": 900, "in_view": False}])
        self.assertFalse(offscreen["ok"])
        self.assertEqual("destination", offscreen["endpoint"])
        gesture.assert_not_called()

    def test_safari_has_no_background_pointer(self) -> None:
        result = agent._gesture_action(None, "Safari", "hover", {"element_id": "e_1"}, {}, 1, 1, "t",
                                       resolve_target=lambda a: ({}, None))
        self.assertEqual("GESTURE_UNSUPPORTED_SAFARI", result["reason_code"])

    def test_schema_rules(self) -> None:
        with self.assertRaises(ValueError):
            validate_action(0, {"type": "dialog"})
        with self.assertRaises(ValueError):
            validate_action(0, {"type": "drag", "query": "Card"})
        validate_action(0, {"type": "drag", "query": "Card", "to_query": "Done column"})
        validate_action(0, {"type": "dialog", "decision": "dismiss"})


class CompanionSourceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_background_script_parses(self) -> None:
        completed = subprocess.run(["node", "--check", str(EXTENSION / "background.js")], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_companion_declares_what_the_server_requires(self) -> None:
        source = (EXTENSION / "background.js").read_text(encoding="utf-8")
        for feature in ("dialogs", "gestures", "alarm_reconnect"):
            self.assertIn(f"'{feature}'", source)
        self.assertIn('"alarms"', (EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        # Dialogs are reported, never answered on their own.
        self.assertNotIn("handleJavaScriptDialog', {accept: true", source)
        # Requests on one tab run one at a time, and a held detach cannot block the next one.
        self.assertIn("withTab(Number(message.chrome_tab_id)", source)
        self.assertIn("setTimeout(resolve, 1500)", source)
        # A remembered dialog is re-checked with a short probe instead of hanging the next call.
        self.assertIn("openDialogs.get(tabId)", source)
        self.assertIn("DIALOG_PROBE_MS", source)
        self.assertIn("function enablePage", source)

    def test_draggable_elements_are_actionable_targets(self) -> None:
        self.assertIn("getAttribute('draggable')==='true'", agent._bootstrap_functions_source())


if __name__ == "__main__":
    unittest.main()
