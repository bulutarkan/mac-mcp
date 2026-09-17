from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server.native_action_verification import (
    effect_changed,
    readiness_reason,
    verification_required,
)
from mcp_server.security import load_settings
from mcp_server import tools_ui


def state(**overrides):
    base = {
        "connected": True,
        "role": "AXButton",
        "subrole": "",
        "title": "Continue",
        "value": "",
        "character_count": 0,
        "selected": False,
        "focused": False,
        "enabled": True,
        "hidden": None,
        "visible": None,
        "offscreen": None,
        "busy": False,
        "position": {"x": 100, "y": 100, "width": 120, "height": 30},
        "child_count": 0,
        "window_title": "Demo",
        "window_count": 1,
        "window_position": {"x": 20, "y": 20, "width": 800, "height": 600},
        "window_minimized": False,
        "window_child_count": 8,
        "sheet_count": 0,
        "popover_count": 0,
        "menu_count": 0,
        "in_sheet": False,
        "in_popover": False,
        "popover_covers_target": False,
        "modal_sheet_blocks_target": False,
    }
    base.update(overrides)
    return base


class NativeReadinessLogicTests(unittest.TestCase):
    def test_disabled_button_is_not_ready(self) -> None:
        self.assertEqual("ELEMENT_DISABLED", readiness_reason(state(enabled=False)))

    def test_zero_bounds_and_minimized_window_are_not_ready(self) -> None:
        self.assertEqual(
            "ELEMENT_ZERO_BOUNDS",
            readiness_reason(state(position={"x": 1, "y": 1, "width": 0, "height": 10})),
        )
        self.assertEqual("WINDOW_MINIMIZED", readiness_reason(state(window_minimized=True)))

    def test_modal_sheet_and_popover_geometry_block_underlying_target(self) -> None:
        self.assertEqual("ELEMENT_OCCLUDED", readiness_reason(state(modal_sheet_blocks_target=True)))
        self.assertEqual("ELEMENT_OCCLUDED", readiness_reason(state(popover_covers_target=True)))

    def test_stale_observation_identity_fails_closed(self) -> None:
        observed = {"role": "AXButton", "subrole": "", "title": "Continue"}
        self.assertEqual(
            "STALE_ELEMENT_PATH",
            readiness_reason(state(title="Delete"), observed_node=observed),
        )

    def test_effect_detection_is_action_specific(self) -> None:
        before = state(role="AXTextField", value="old", character_count=3)
        after = state(role="AXTextField", value="new value", character_count=9)
        changed, why = effect_changed(before, after, {"type": "type", "text": "new value"})
        self.assertTrue(changed)
        self.assertEqual("text_state_changed", why)

        changed, why = effect_changed(state(), state(sheet_count=1), {"type": "click"})
        self.assertTrue(changed)
        self.assertEqual("window_state_changed", why)

        changed, why = effect_changed(state(), state(connected=False), {"type": "menu"})
        self.assertTrue(changed)
        self.assertEqual("target_disconnected", why)

        changed, why = effect_changed(state(), state(), {"type": "click"})
        self.assertFalse(changed)
        self.assertEqual("state_unchanged", why)

    def test_verification_scope_keeps_non_element_actions_out(self) -> None:
        self.assertTrue(verification_required("click", "w1/1"))
        self.assertTrue(verification_required("paste", "w1/1"))
        self.assertFalse(verification_required("scroll", "w1/1"))
        self.assertFalse(verification_required("click", None))


class NativeVerificationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = {
            "app": "DemoApp",
            "pid": 4321,
            "window_index": 1,
            "app_handle": "mapp_test",
            "window_handle": "mwin_test",
        }

    def _act(self, action):
        return tools_ui.act_ui(
            self.settings,
            [action],
            app="DemoApp",
            return_state=False,
            allow_risky=True,
            preserve_focus=False,
        )

    def test_readiness_wait_recovers_when_element_becomes_enabled(self) -> None:
        blocked = state(enabled=False)
        ready_state = state(enabled=True)
        with patch.object(tools_ui, "_probe_native_action_state", side_effect=[(blocked, None), (ready_state, None)]), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.2]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "DemoApp",
                {"type": "click", "element_id": "w1/1", "readiness_stable_ms": 0},
                {"role": "AXButton", "subrole": "", "title": "Continue"},
                app_pid=4321,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(2, result["attempts"])

    def test_effect_wait_accepts_delayed_state_change(self) -> None:
        before = state()
        after = state(sheet_count=1)
        with patch.object(tools_ui, "_probe_native_effect_state", side_effect=[(before, None), (after, None)]), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.2]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_effect(
                "DemoApp", {"type": "click", "element_id": "w1/1"}, before, app_pid=4321
            )
        self.assertTrue(result["effect_observed"])
        self.assertEqual("window_state_changed", result["verification"])
        self.assertEqual(2, result["attempts"])

    def test_effect_wait_times_out_without_replaying_action(self) -> None:
        before = state()
        with patch.object(tools_ui, "_probe_native_effect_state", return_value=(before, None)) as probe, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.7]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_effect(
                "DemoApp", {"type": "click", "element_id": "w1/1", "verify_timeout_s": 0.55}, before, app_pid=4321
            )
        self.assertFalse(result["effect_observed"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(2, probe.call_count)

    def test_readiness_probe_is_side_effect_free(self) -> None:
        script = tools_ui._native_action_state_script("DemoApp", "w1/1", app_pid=4321)
        self.assertIn("AXElementBusy", script)
        self.assertIn("AXOffScreen", script)
        self.assertIn("count of sheets of w", script)
        self.assertNotIn('perform action "AXPress"', script)
        self.assertNotIn("click targetElement", script)
        self.assertNotIn("keystroke", script)

    def test_disabled_element_never_executes_action(self) -> None:
        blocked = {
            "ready": False,
            "reason_code": "ELEMENT_DISABLED",
            "retryable": True,
            "state": {"enabled": False},
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=blocked), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertFalse(result["ok"])
        self.assertEqual("ELEMENT_DISABLED", result["reason_code"])
        perform.assert_not_called()

    def test_click_without_observable_effect_is_not_success(self) -> None:
        ready = {"ready": True, "state": state(), "stable_for_ms": 150, "attempts": 2}
        no_effect = {
            "effect_observed": False,
            "verification": "no_effect_after_bounded_wait",
            "reason_code": "ACTION_NO_EFFECT",
            "automatic_retry": False,
            "attempts": 4,
            "duration_ms": 550,
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=no_effect):
            result = self._act({"type": "click", "element_id": "w1/1"})
        perform.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual("no_effect_after_bounded_wait", result["actions"][0]["verification"])

    def test_delayed_click_effect_is_accepted_without_second_action(self) -> None:
        ready = {"ready": True, "state": state(), "stable_for_ms": 150, "attempts": 2}
        effect = {
            "effect_observed": True,
            "verification": "window_state_changed",
            "attempts": 3,
            "duration_ms": 210,
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["actions"][0]["effect_observed"])
        self.assertEqual("window_state_changed", result["actions"][0]["verification"])
        perform.assert_called_once()

    def test_typing_requires_observable_text_change(self) -> None:
        ready = {
            "ready": True,
            "state": state(role="AXTextField", value="before", character_count=6),
            "stable_for_ms": 150,
        }
        effect = {"effect_observed": True, "verification": "text_state_changed", "attempts": 1, "duration_ms": 20}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "type", "element_id": "w1/1", "text": "after"})
        self.assertTrue(result["ok"])
        self.assertEqual("text_state_changed", result["actions"][0]["verification"])


if __name__ == "__main__":
    unittest.main()
