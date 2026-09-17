from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.security import load_settings


def node(value: str = "0", *, element_id: str = "w1/1", child_count: int = 0) -> dict:
    return {
        "element_id": element_id,
        "parent_id": "w1" if "/" in element_id else None,
        "role": "AXCheckBox",
        "subrole": "",
        "title": "Toggle",
        "description": "checkbox",
        "value": value,
        "position": {"x": 100, "y": 100, "width": 120, "height": 30},
        "enabled": True,
        "focused": False,
        "actions": ["AXPress"],
        "child_count": child_count,
    }


def effect_state(value: str = "1", *, child_count: int = 0, window_child_count: int = 1) -> dict:
    return {
        "connected": True,
        "value": value,
        "character_count": len(value),
        "selected": False,
        "enabled": True,
        "title": "Toggle",
        "child_count": child_count,
        "window_title": "Demo",
        "window_count": 1,
        "window_child_count": window_child_count,
        "sheet_count": 0,
        "popover_count": 0,
    }


def readiness_state(value: str = "0", *, child_count: int = 0, window_child_count: int = 1) -> dict:
    state = effect_state(value, child_count=child_count, window_child_count=window_child_count)
    state.update({
        "role": "AXCheckBox",
        "subrole": "",
        "description": "checkbox",
        "focused": False,
        "hidden": False,
        "visible": True,
        "offscreen": False,
        "busy": False,
        "position": {"x": 100, "y": 100, "width": 120, "height": 30},
        "window_position": {"x": 20, "y": 20, "width": 800, "height": 600},
        "window_minimized": False,
        "in_sheet": False,
        "in_popover": False,
        "popover_covers_target": False,
    })
    return state


class NativeDeltaContractTests(unittest.TestCase):
    def test_state_mode_defaults_to_delta_and_legacy_boolean_maps_cleanly(self) -> None:
        self.assertEqual(("delta", False), tools_ui._normalize_action_state_mode(None, None))
        self.assertEqual(("none", False), tools_ui._normalize_action_state_mode(None, False))
        self.assertEqual(("full", True), tools_ui._normalize_action_state_mode(None, True))
        self.assertEqual(("delta", False), tools_ui._normalize_action_state_mode("delta", None))
        with self.assertRaises(ValueError):
            tools_ui._normalize_action_state_mode("full", False)
        with self.assertRaises(ValueError):
            tools_ui._normalize_action_state_mode("compact", None)

    def test_openapi_exposes_delta_default_and_screenshot_opt_in(self) -> None:
        with open("openapi/custom-gpt-actions.json", encoding="utf-8") as fh:
            schema = json.load(fh)
        props = schema["paths"]["/api/mac_act"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
        self.assertEqual("delta", props["state_mode"]["default"])
        self.assertEqual(["none", "delta", "full"], props["state_mode"]["enum"])
        self.assertIs(props["include_screenshot"]["default"], False)
        self.assertTrue(props["return_state"]["deprecated"])


class NativeDeltaActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        tools_ui._OBSERVATIONS.clear()
        self.base_node = node("0")
        self.observation_id = tools_ui._save_observation(
            "DemoApp", 1, [self.base_node], {"active_app": "DemoApp", "windows": []}
        )
        self.target = {
            "app": "DemoApp",
            "pid": None,
            "window_index": 1,
            "app_handle": None,
            "window_handle": None,
        }
        self.ready = {
            "ready": True,
            "state": readiness_state("0"),
            "attempts": 1,
            "duration_ms": 1,
            "stable_for_ms": 120,
        }
        self.changed = {
            "effect_observed": True,
            "verification": "target_state_changed",
            "attempts": 1,
            "duration_ms": 1,
            "state": effect_state("1"),
        }

    def _call(self, **kwargs):
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.changed):
            return tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=self.observation_id,
                app="DemoApp",
                allow_risky=True,
                preserve_focus=False,
                **kwargs,
            )

    def test_default_delta_uses_effect_probe_and_skips_full_accessibility_refresh(self) -> None:
        with patch.object(tools_ui, "_collect_observation") as collect:
            raw = self._call()
        collect.assert_not_called()
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual("delta", payload["state_mode"])
        self.assertEqual("verification_probe", payload["delta"]["source"])
        self.assertFalse(payload["delta"]["structural_refresh"])
        self.assertEqual(1, payload["delta"]["changed_count"])
        self.assertEqual("1", payload["delta"]["changed_nodes"][0]["value"])
        self.assertEqual([], payload["delta"]["added_nodes"])
        self.assertEqual([], payload["delta"]["removed_element_ids"])
        self.assertFalse(payload["screenshot"]["requested"])
        self.assertNotEqual(self.observation_id, payload["observation_id"])
        derived = tools_ui._get_observation(payload["observation_id"])
        self.assertIsNotNone(derived)
        self.assertEqual("1", derived["nodes"]["w1/1"]["value"])

    def test_delta_observation_can_chain_without_full_refresh(self) -> None:
        with patch.object(tools_ui, "_collect_observation") as collect:
            first = json.loads(self._call())
            first_id = first["observation_id"]
            first_node = tools_ui._get_observation(first_id)["nodes"]["w1/1"]
            ready2 = dict(self.ready, state=readiness_state("1"))
            changed2 = dict(self.changed, state=effect_state("2"))
            with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
                 patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready2), \
                 patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")), \
                 patch.object(tools_ui, "_wait_for_native_effect", return_value=changed2):
                second = json.loads(tools_ui.act_ui(
                    self.settings,
                    [{"type": "click", "element_id": "w1/1"}],
                    observation_id=first_id,
                    app="DemoApp",
                    allow_risky=True,
                    preserve_focus=False,
                ))
        collect.assert_not_called()
        self.assertEqual("1", first_node["value"])
        self.assertEqual("2", second["delta"]["changed_nodes"][0]["value"])
        self.assertEqual("2", tools_ui._get_observation(second["observation_id"])["nodes"]["w1/1"]["value"])

    def test_structural_change_falls_back_to_screenshot_free_accessibility_refresh(self) -> None:
        structural = dict(self.changed, verification="window_state_changed", state=effect_state("1", child_count=1, window_child_count=2))
        post_nodes = [node("1", child_count=1), node("new", element_id="w1/2")]
        post_payload = {
            "ok": True,
            "observation_id": "obs_refresh",
            "active_app": "DemoApp",
            "app_handle": None,
            "window_handle": None,
            "node_count": 2,
            "nodes": post_nodes,
            "screenshot": {"requested": False, "included_as_image_content": False, "mime_type": None},
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=structural), \
             patch.object(tools_ui, "_collect_observation", return_value=(post_payload, None)) as collect:
            payload = json.loads(tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=self.observation_id,
                app="DemoApp",
                allow_risky=True,
                preserve_focus=False,
            ))
        self.assertTrue(payload["ok"])
        self.assertEqual("accessibility_refresh", payload["delta"]["source"])
        self.assertTrue(payload["delta"]["structural_refresh"])
        self.assertIn("structural_effect", payload["delta"]["refresh_reasons"])
        self.assertEqual(1, payload["delta"]["added_count"])
        self.assertEqual("w1/2", payload["delta"]["added_nodes"][0]["element_id"])
        self.assertFalse(collect.call_args.kwargs["include_screenshot"])

    def test_none_mode_skips_post_state(self) -> None:
        with patch.object(tools_ui, "_collect_observation") as collect:
            payload = self._call(state_mode="none")
        collect.assert_not_called()
        self.assertIsInstance(payload, dict)
        self.assertEqual("none", payload["state_mode"])
        self.assertNotIn("observation_id", payload)

    def test_full_mode_refreshes_without_screenshot_by_default(self) -> None:
        post_payload = {
            "ok": True,
            "observation_id": "obs_full",
            "active_app": "DemoApp",
            "app_handle": None,
            "window_handle": None,
            "node_count": 1,
            "nodes": [node("1")],
            "screenshot": {"requested": False, "included_as_image_content": False, "mime_type": None},
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.changed), \
             patch.object(tools_ui, "_collect_observation", return_value=(post_payload, None)) as collect:
            payload = json.loads(tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=self.observation_id,
                app="DemoApp",
                allow_risky=True,
                preserve_focus=False,
                state_mode="full",
            ))
        self.assertEqual("full", payload["state_mode"])
        self.assertFalse(collect.call_args.kwargs["include_screenshot"])

    def test_legacy_return_state_true_keeps_old_full_plus_screenshot_behavior(self) -> None:
        post_payload = {
            "ok": True,
            "observation_id": "obs_full",
            "active_app": "DemoApp",
            "app_handle": None,
            "window_handle": None,
            "node_count": 1,
            "nodes": [node("1")],
            "screenshot": {"requested": True, "included_as_image_content": False, "mime_type": None},
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=self.ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=self.changed), \
             patch.object(tools_ui, "_collect_observation", return_value=(post_payload, None)) as collect:
            payload = json.loads(tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=self.observation_id,
                app="DemoApp",
                allow_risky=True,
                preserve_focus=False,
                return_state=True,
            ))
        self.assertEqual("full", payload["state_mode"])
        self.assertTrue(collect.call_args.kwargs["include_screenshot"])


if __name__ == "__main__":
    unittest.main()
