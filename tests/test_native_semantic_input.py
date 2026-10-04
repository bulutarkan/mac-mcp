from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.security import load_settings


class NativeSemanticWriteTests(unittest.TestCase):
    def test_exact_replace_uses_axvalue_without_activation(self) -> None:
        with patch.object(tools_ui, "_run_osascript", return_value=(True, "", None)) as run:
            ok, message = tools_ui._semantic_text_write(
                "DemoApp",
                "w1/2",
                'hello "world"',
                replace=True,
                app_pid=4321,
            )
        self.assertTrue(ok)
        self.assertIn("AXValue", message)
        script = run.call_args.args[0]
        self.assertIn('attribute "AXValue"', script)
        self.assertIn("unix id is 4321", script)
        self.assertNotIn("set frontmost to true", script)
        self.assertNotIn("keystroke", script)
        self.assertNotIn("click targetElement", script)

    def test_insert_and_paste_path_use_selected_text_without_activation(self) -> None:
        with patch.object(tools_ui, "_run_osascript", return_value=(True, "", None)) as run:
            ok, message = tools_ui._semantic_text_write(
                "DemoApp",
                "w1/2",
                "inserted",
                replace=False,
                app_pid=4321,
            )
        self.assertTrue(ok)
        self.assertIn("AXSelectedText", message)
        script = run.call_args.args[0]
        self.assertIn('attribute "AXSelectedText"', script)
        self.assertNotIn("set frontmost to true", script)

    def test_semantic_write_failure_never_auto_falls_back_to_keyboard(self) -> None:
        with patch.object(
            tools_ui,
            "_run_osascript",
            return_value=(False, "", "semantic_attribute_unavailable:AXValue"),
        ), patch.object(tools_ui, "_run_cliclick") as cliclick, patch.object(
            tools_ui, "_set_clipboard"
        ) as clipboard:
            with self.assertRaises(tools_ui.NativeForegroundRequiredError) as caught:
                tools_ui._semantic_text_write(
                    "DemoApp",
                    "w1/2",
                    "hello",
                    replace=True,
                    app_pid=4321,
                )
        self.assertIn("trusted local foreground capability", str(caught.exception))
        self.assertIn("AXValue", caught.exception.semantic_reason)
        cliclick.assert_not_called()
        clipboard.assert_not_called()

    def test_perform_action_defaults_type_and_paste_to_semantic_paths(self) -> None:
        with patch.object(
            tools_ui,
            "_semantic_text_write",
            return_value=(True, "semantic"),
        ) as semantic, patch.object(tools_ui, "_type_text_foreground") as legacy_type, patch.object(
            tools_ui, "_paste_text_foreground"
        ) as legacy_paste:
            ok, _ = tools_ui._perform_action(
                "DemoApp",
                {"type": "type", "element_id": "w1/2", "text": "replace", "clear": True},
                None,
                app_pid=4321,
                activate_target=False,
            )
            self.assertTrue(ok)
            self.assertTrue(semantic.call_args.kwargs["replace"])

            ok, _ = tools_ui._perform_action(
                "DemoApp",
                {"type": "paste", "element_id": "w1/2", "text": "insert"},
                None,
                app_pid=4321,
                activate_target=False,
            )
            self.assertTrue(ok)
            self.assertFalse(semantic.call_args.kwargs["replace"])

        legacy_type.assert_not_called()
        legacy_paste.assert_not_called()


class NativeSemanticActFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = {
            "app": "TargetApp",
            "pid": 4321,
            "window_index": 1,
            "app_handle": "mapp_target",
            "window_handle": "mwin_target",
            "bundle_id": "com.example.target",
        }
        self.ready = {
            "ready": True,
            "state": {
                "connected": True,
                "role": "AXTextField",
                "subrole": "",
                "title": "Target",
                "value": "before",
                "character_count": 6,
                "selected": False,
                "focused": True,
                "enabled": True,
                "position": {"x": 100, "y": 100, "width": 120, "height": 30},
                "window_position": {"x": 20, "y": 20, "width": 800, "height": 600},
                "window_title": "Target Window",
                "window_count": 1,
                "window_child_count": 1,
                "sheet_count": 0,
                "popover_count": 0,
                "menu_count": 0,
            },
            "attempts": 1,
        }
        self.user_focus = {
            "app": "UserApp",
            "pid": 999,
            "app_handle": "mapp_user",
            "window_count": 1,
            "window_index": 1,
            "window_handle": "mwin_user",
            "window_identity_status": "stable",
        }

    def _act(self, action: dict):
        return tools_ui.act_ui(
            self.settings,
            [action],
            app="TargetApp",
            return_state=False,
            allow_risky=True,
        )

    def test_semantic_unavailable_surfaces_foreground_required_without_fallback(self) -> None:
        with patch.object(
            tools_ui, "_resolve_action_native_target", return_value=(self.target, None)
        ), patch.object(
            tools_ui, "_wait_for_native_readiness", return_value=self.ready
        ), patch.object(
            tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)
        ), patch.object(
            tools_ui, "_post_action_focus_decision", return_value=("preserved", None)
        ), patch.object(
            tools_ui,
            "_perform_action",
            side_effect=tools_ui.NativeForegroundRequiredError(
                "semantic write unavailable",
                semantic_reason="AXValue_not_settable",
            ),
        ):
            result = self._act(
                {"type": "type", "element_id": "w1/2", "text": "hello"}
            )

        self.assertFalse(result["ok"])
        self.assertEqual("FOREGROUND_REQUIRED", result["reason_code"])
        action = result["actions"][0]
        self.assertEqual("FOREGROUND_REQUIRED", action["reason_code"])
        self.assertTrue(action["foreground_required"])
        self.assertFalse(action["automatic_retry"])
        self.assertEqual("AXValue_not_settable", action["semantic_reason"])
        self.assertEqual("background_semantic", action["focus_mode"])

    def test_text_without_element_id_fails_explicitly_before_execution(self) -> None:
        with patch.object(tools_ui, "_resolve_action_native_target") as resolve_target, patch.object(
            tools_ui, "_perform_action"
        ) as perform:
            result = self._act({"type": "type", "text": "hello"})
        self.assertFalse(result["ok"])
        self.assertEqual("ELEMENT_ID_REQUIRED", result["reason_code"])
        self.assertEqual("unsupported", result["focus_mode"])
        resolve_target.assert_not_called()
        perform.assert_not_called()

    def test_unknown_action_is_unsupported_not_foreground_required(self) -> None:
        with patch.object(tools_ui, "_resolve_action_native_target") as resolve_target, patch.object(
            tools_ui, "_perform_action"
        ) as perform:
            result = self._act({"type": "teleport"})
        self.assertFalse(result["ok"])
        self.assertEqual("UNSUPPORTED_NATIVE_ACTION", result["reason_code"])
        self.assertEqual("unsupported", result["focus_mode"])
        resolve_target.assert_not_called()
        perform.assert_not_called()

    def test_invalid_input_mode_fails_before_execution(self) -> None:
        with patch.object(
            tools_ui, "_resolve_action_native_target", return_value=(self.target, None)
        ), patch.object(
            tools_ui, "_wait_for_native_readiness", return_value=self.ready
        ), patch.object(tools_ui, "_perform_action") as perform:
            result = self._act(
                {
                    "type": "type",
                    "element_id": "w1/2",
                    "text": "hello",
                    "input_mode": "magic",
                }
            )
        self.assertFalse(result["ok"])
        self.assertEqual("INVALID_NATIVE_INPUT_MODE", result["reason_code"])
        perform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
