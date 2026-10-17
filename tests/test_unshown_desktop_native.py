"""Windows on a desktop no display shows (an agent's own desktop): observed and acted on natively."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import ax_native, tools_ui
from mcp_server.native_targets import public_window_rows
from mcp_server.security import load_settings

FS = "\x1f"
RS = "\x1e"


def _window_record(index: int, title: str, *extra: str) -> str:
    return FS.join(["__WINDOW__", str(index), title, "", "", "0", "0", "800", "600", "AXStandardWindow", "false", "false", *extra])


class ObservationRecordTests(unittest.TestCase):
    def test_native_window_record_carries_desktop_and_window_id(self) -> None:
        raw = RS.join([
            FS.join(["__META__", "TextEdit", "false", "2", "a || b", "4321", "com.apple.TextEdit"]),
            _window_record(1, "a", "true", "101"),
            _window_record(2, "b", "false", "102"),
        ])
        metadata, _ = tools_ui._parse_observation(raw)
        shown, hidden = metadata["windows"]
        self.assertEqual((True, 101), (shown["on_shown_desktop"], shown["window_id"]))
        self.assertEqual((False, 102), (hidden["on_shown_desktop"], hidden["window_id"]))
        self.assertFalse(public_window_rows(metadata)[1]["on_shown_desktop"])

    def test_applescript_record_without_the_fields_still_parses(self) -> None:
        metadata, _ = tools_ui._parse_observation(_window_record(1, "a"))
        self.assertNotIn("on_shown_desktop", metadata["windows"][0])
        self.assertNotIn("on_shown_desktop", public_window_rows(metadata)[0])


class UnshownDesktopActTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = {
            "app": "Calculator", "pid": 4321, "window_index": 1, "app_handle": "mapp_calc",
            "window_handle": "mwin_calc", "bundle_id": "com.apple.calculator", "on_shown_desktop": False,
        }
        self.node = {"element_id": "w1/1/7", "role": "AXButton", "title": "", "description": "7"}

    def _act(self, action: dict, answer: dict | None):
        stored = {"active_app": "Calculator", "nodes": {"w1/1/7": self.node}}
        with patch.object(tools_ui, "_get_observation", return_value=stored), \
             patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_delegated_native_human_guard") as guard, \
             patch.object(tools_ui, "_perform_action") as perform, \
             patch.object(tools_ui, "_wait_for_native_readiness") as readiness, \
             patch.object(ax_native, "act", return_value=answer) as native:
            result = tools_ui.act_ui(
                self.settings, [{"element_id": "w1/1/7", **action}], observation_id="obs", return_state=False,
            )
        # Nothing reaches System Events, and the person using the app elsewhere does not block it.
        perform.assert_not_called()
        readiness.assert_not_called()
        guard.assert_not_called()
        return result, native

    def test_click_presses_natively_and_checks_the_element_is_the_observed_one(self) -> None:
        result, native = self._act({"type": "click"}, {"ok": True, "performed": "AXPress", "effect_observed": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual("native_window_changed", result["actions"][0]["verification"])
        kwargs = native.call_args.kwargs
        self.assertEqual(("press", "w1/1/7", 4321), (kwargs["op"], kwargs["element_id"], kwargs["app_pid"]))
        self.assertEqual({"role": "AXButton", "title": "", "description": "7"}, kwargs["expect"])

    def test_click_without_effect_fails_like_the_shown_desktop_path(self) -> None:
        result, _ = self._act({"type": "click"}, {"ok": True, "performed": "AXPress", "effect_observed": False})
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])

    def test_right_click_shows_the_menu(self) -> None:
        _, native = self._act({"type": "click", "button": "right"}, {"ok": True, "performed": "AXShowMenu", "effect_observed": True})
        self.assertEqual(("action", "AXShowMenu"), (native.call_args.kwargs["op"], native.call_args.kwargs["name"]))

    def test_type_replaces_and_paste_inserts(self) -> None:
        answer = {"ok": True, "attribute": "AXValue", "effect_observed": True, "verified": True}
        result, native = self._act({"type": "type", "text": "hello"}, answer)
        self.assertTrue(result["ok"])
        self.assertEqual(("set_text", "hello", True), (native.call_args.kwargs["op"], native.call_args.kwargs["text"],
                                                       native.call_args.kwargs["replace"]))
        _, native = self._act({"type": "paste", "text": "x"}, {**answer, "attribute": "AXSelectedText"})
        self.assertFalse(native.call_args.kwargs["replace"])

    def test_foreground_input_is_refused_without_running(self) -> None:
        for action in ({"type": "key", "key": "a"}, {"type": "double_click"}, {"type": "type", "text": "x", "input_mode": "foreground"}):
            result, native = self._act(action, {"ok": True})
            self.assertFalse(result["ok"], action)
            self.assertIn(result["reason_code"], {"UNSHOWN_DESKTOP_ACTION_UNSUPPORTED", "WINDOW_BOUND_ACTION_REQUIRES_ELEMENT"}, action)
            native.assert_not_called()

    def test_changed_element_and_missing_helper_are_reported(self) -> None:
        stale = {"ok": False, "reason_code": "STALE_ELEMENT_PATH", "error": "w1/1/7 is now a different element"}
        result, _ = self._act({"type": "click"}, stale)
        self.assertEqual("STALE_ELEMENT_PATH", result["reason_code"])
        result, _ = self._act({"type": "click"}, None)
        self.assertEqual("NATIVE_HELPER_UNAVAILABLE", result["reason_code"])


class NativeActRequestTests(unittest.TestCase):
    def test_act_builds_helper_arguments(self) -> None:
        with patch.object(ax_native, "_executable", return_value="/bin/ax-observe"), \
             patch.object(ax_native, "_call", return_value=(True, '{"ok": true}', "")) as call:
            answer = ax_native.act(app_pid=7, element_id="w2/1", op="set_text", text="hi", replace=False,
                                   expect={"role": "AXTextArea", "title": None})
        self.assertEqual({"ok": True}, answer)
        self.assertEqual(
            ["--mode", "act", "--pid", "7", "--element", "w2/1", "--op", "set_text",
             "--text", "hi", "--replace", "false", "--expect-role", "AXTextArea"],
            call.call_args.args[1],
        )

    def test_act_without_helper_returns_none(self) -> None:
        with patch.object(ax_native, "_executable", return_value=None):
            self.assertIsNone(ax_native.act(app_pid=7, element_id="w1", op="press"))


if __name__ == "__main__":
    unittest.main()
