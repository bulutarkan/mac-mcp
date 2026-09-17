from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.security import load_settings


def ready_state(role: str = "AXButton", value: str = "0") -> dict:
    return {
        "connected": True,
        "role": role,
        "subrole": "",
        "title": "Target",
        "value": value,
        "character_count": len(value),
        "selected": False,
        "enabled": True,
        "position": {"x": 100, "y": 100, "width": 120, "height": 30},
        "window_position": {"x": 20, "y": 20, "width": 800, "height": 600},
        "window_title": "Target Window",
        "window_count": 1,
        "window_child_count": 1,
        "sheet_count": 0,
        "popover_count": 0,
        "menu_count": 0,
    }


class NativeFocusPolicyTests(unittest.TestCase):
    def test_background_safe_action_classification(self) -> None:
        self.assertFalse(tools_ui._action_requires_foreground({"type": "click", "element_id": "w1/1"}))
        self.assertFalse(tools_ui._action_requires_foreground({"type": "scroll", "element_id": "w1/1"}))
        self.assertFalse(tools_ui._action_requires_foreground({"type": "accessibility_action", "element_id": "w1/1", "name": "AXPress"}))
        self.assertTrue(tools_ui._action_requires_foreground({"type": "double_click", "element_id": "w1/1"}))
        self.assertTrue(tools_ui._action_requires_foreground({"type": "type", "element_id": "w1/1", "text": "x"}))
        self.assertTrue(tools_ui._action_requires_foreground({"type": "key", "key": "return"}))

    def test_post_action_focus_decision_respects_third_party_user_change(self) -> None:
        previous = {"pid": 11, "window_index": 1}
        target = {"pid": 22, "window_index": 1}
        with patch.object(tools_ui, "_current_focus_key", return_value=((33, 1), None)):
            decision, error = tools_ui._post_action_focus_decision(previous, target)
        self.assertEqual("user_changed", decision)
        self.assertIsNone(error)

    def test_target_script_can_skip_activation(self) -> None:
        script = tools_ui._target_script(
            "DemoApp", "w1/1", 'perform action "AXPress" of targetElement',
            activate=False, app_pid=4321,
        )
        self.assertIn("unix id is 4321", script)
        self.assertNotIn("set frontmost to true", script)

    def test_focus_transition_detects_other_app_or_window(self) -> None:
        current = {"pid": 11, "window_index": 1}
        self.assertTrue(tools_ui._focus_transition_needed(current, {"pid": 22, "window_index": 1}))
        self.assertTrue(tools_ui._focus_transition_needed(current, {"pid": 11, "window_index": 2}))
        self.assertFalse(tools_ui._focus_transition_needed(current, {"pid": 11, "window_index": 1}))


class NativeFocusActTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = {
            "app": "TargetApp",
            "pid": 4321,
            "window_index": 1,
            "app_handle": "mapp_target",
            "window_handle": "mwin_target",
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
        self.ready = {"ready": True, "state": ready_state(), "attempts": 1, "stable_for_ms": 120}
        self.effect = {"effect_observed": True, "verification": "target_state_changed", "attempts": 1, "duration_ms": 20}

    def _act(self, action: dict, **kwargs):
        return tools_ui.act_ui(
            self.settings,
            [action],
            app="TargetApp",
            return_state=False,
            allow_risky=True,
            **kwargs,
        )

    def test_background_ax_click_does_not_activate_or_restore_when_focus_stays_put(self) -> None:
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)), \
             patch.object(tools_ui, "_post_action_focus_decision", return_value=("preserved", None)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.effect), \
             patch.object(tools_ui, "_restore_focus_context") as restore:
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertTrue(result["ok"])
        self.assertFalse(perform.call_args.args[5])
        restore.assert_not_called()
        action = result["actions"][0]
        self.assertEqual("background_ax", action["focus_mode"])
        self.assertTrue(action["focus_preserved"])
        self.assertFalse(action["focus_restore_attempted"])

    def test_global_input_temporarily_focuses_then_restores_exact_previous_window(self) -> None:
        ready = {"ready": True, "state": ready_state(role="AXTextField", value=""), "attempts": 1}
        effect = {"effect_observed": True, "verification": "text_state_changed", "attempts": 1, "duration_ms": 20}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)), \
             patch.object(tools_ui, "_post_action_focus_decision", return_value=("restore", None)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")) as perform, \
             patch.object(tools_ui, "_restore_focus_context", return_value=(True, "previous focus restored", True)) as restore, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "type", "element_id": "w1/1", "text": "hello"})
        self.assertTrue(result["ok"])
        self.assertTrue(perform.call_args.args[5])
        restore.assert_called_once()
        action = result["actions"][0]
        self.assertEqual("temporary_foreground_restore", action["focus_mode"])
        self.assertTrue(action["focus_restored"])
        self.assertTrue(action["focus_restore_exact"])

    def test_ambiguous_previous_window_blocks_required_focus_switch_before_action(self) -> None:
        ambiguous = dict(self.user_focus, window_count=2, window_handle=None, window_identity_status="ambiguous")
        ready = {"ready": True, "state": ready_state(role="AXTextField", value=""), "attempts": 1}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(ambiguous, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = self._act({"type": "type", "element_id": "w1/1", "text": "hello"})
        self.assertFalse(result["ok"])
        self.assertEqual("FOCUS_SNAPSHOT_AMBIGUOUS", result["reason_code"])
        self.assertFalse(result["retryable"])
        perform.assert_not_called()

    def test_restore_failure_after_effect_is_fail_closed_and_never_auto_retried(self) -> None:
        ready = {"ready": True, "state": ready_state(role="AXTextField", value=""), "attempts": 1}
        effect = {"effect_observed": True, "verification": "text_state_changed", "attempts": 1, "duration_ms": 20}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)), \
             patch.object(tools_ui, "_post_action_focus_decision", return_value=("restore", None)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")) as perform, \
             patch.object(tools_ui, "_restore_focus_context", return_value=(False, "restore failed", True)), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "type", "element_id": "w1/1", "text": "hello"})
        perform.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertEqual("FOCUS_RESTORE_FAILED", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        action = result["actions"][0]
        self.assertTrue(action["effect_observed"])
        self.assertFalse(action["focus_restored"])

    def test_unexpected_background_focus_change_is_restored(self) -> None:
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)), \
             patch.object(tools_ui, "_post_action_focus_decision", return_value=("restore", None)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_restore_focus_context", return_value=(True, "previous focus restored", True)) as restore, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.effect):
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertTrue(result["ok"])
        self.assertFalse(perform.call_args.args[5])
        restore.assert_called_once()
        self.assertTrue(result["actions"][0]["focus_restored"])

    def test_concurrent_user_focus_change_is_never_overwritten(self) -> None:
        ready = {"ready": True, "state": ready_state(role="AXTextField", value=""), "attempts": 1}
        effect = {"effect_observed": True, "verification": "text_state_changed", "attempts": 1, "duration_ms": 20}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_capture_focus_context", return_value=(self.user_focus, None)), \
             patch.object(tools_ui, "_post_action_focus_decision", return_value=("user_changed", None)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")) as perform, \
             patch.object(tools_ui, "_restore_focus_context") as restore, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "type", "element_id": "w1/1", "text": "hello"})
        self.assertTrue(result["ok"])
        perform.assert_called_once()
        restore.assert_not_called()
        action = result["actions"][0]
        self.assertTrue(action["focus_user_changed"])
        self.assertTrue(action["focus_restore_skipped_user_change"])
        self.assertFalse(action["focus_restore_attempted"])

    def test_preserve_focus_false_keeps_legacy_foreground_behavior(self) -> None:
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_capture_focus_context") as capture, \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.effect):
            result = self._act({"type": "click", "element_id": "w1/1"}, preserve_focus=False)
        self.assertTrue(result["ok"])
        capture.assert_not_called()
        self.assertTrue(perform.call_args.args[5])
        self.assertEqual("foreground_allowed", result["actions"][0]["focus_mode"])


class NativeFocusSchemaTests(unittest.TestCase):
    def test_openapi_defaults_preserve_focus_true(self) -> None:
        with open("openapi/custom-gpt-actions.json", encoding="utf-8") as fh:
            schema = json.load(fh)
        operation = schema["paths"]["/api/mac_act"]["post"]
        prop = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["preserve_focus"]
        self.assertIs(prop["default"], True)
        self.assertIn("restore", prop["description"].lower())


if __name__ == "__main__":
    unittest.main()
