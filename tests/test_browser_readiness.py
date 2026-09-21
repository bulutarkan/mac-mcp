from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from mcp_server.tools_browser_agent import (
    _batch_js,
    _browser_state_bootstrap,
    _capture_dom_visual_locked,
    _dom_capture_start_js,
    _render_readiness_js,
    _verified_dom_action,
    _wait_for_element_readiness,
    _wait_for_render_readiness,
)


class BrowserRenderReadinessTests(unittest.TestCase):
    def test_page_zero_bounds_is_rejected_before_defensive_pixel_floor(self) -> None:
        script = _dom_capture_start_js("full_page", None)
        guard = script.index('reason_code:mode==="element"?"ELEMENT_ZERO_BOUNDS":"ZERO_CONTENT_BOUNDS"')
        floor = script.index('var sourceW=Math.max(1,Math.ceil(rawW))')
        self.assertLess(guard, floor)
        self.assertIn('rawW<=0||rawH<=0', script)
        self.assertNotIn('mode==="viewport"?Math.max(1,innerWidth)', script)

    def test_legitimate_tiny_element_is_still_rounded_to_one_pixel_after_readiness_guard(self) -> None:
        script = _dom_capture_start_js("element", "e_test")
        self.assertIn('rawW=mode==="element"?Number(rect.width||0)', script)
        self.assertIn('ELEMENT_ZERO_BOUNDS', script)
        self.assertIn('var sourceW=Math.max(1,Math.ceil(rawW))', script)
        self.assertIn('var actualH=Math.max(1,Math.ceil(rawH))', script)

    def test_render_readiness_exposes_raw_bounds_and_liveness(self) -> None:
        script = _render_readiness_js("viewport", None)
        for token in ("page_alive", "raw_width", "raw_height", "ready_state", "ZERO_CONTENT_BOUNDS", "RENDER_NOT_READY"):
            self.assertIn(token, script)

    def test_delayed_zero_bounds_can_recover_within_bounded_retry(self) -> None:
        states = [
            {"ready": False, "reason_code": "ZERO_CONTENT_BOUNDS", "signature": "loading|0|0"},
            {"ready": True, "reason_code": None, "signature": "complete|1200|800", "load_age_ms": 500},
        ]
        with patch("mcp_server.tools_browser_agent._execute_js_unbounded", side_effect=[json.dumps(x) for x in states]), \
             patch("mcp_server.tools_browser_agent.time.sleep"):
            result = _wait_for_render_readiness("Safari", "viewport", None, 1, 1, "tab-a")
        self.assertTrue(result["ready"])
        self.assertEqual(2, result["attempts"])
        self.assertEqual("loaded", result["settled_by"])

    def test_permanent_zero_bounds_returns_structured_failure(self) -> None:
        zero = json.dumps({"ready": False, "reason_code": "ZERO_CONTENT_BOUNDS", "signature": "complete|0|0"})
        with patch("mcp_server.tools_browser_agent._RENDER_READINESS_TIMEOUT_S", 0.0), \
             patch("mcp_server.tools_browser_agent._execute_js_unbounded", return_value=zero):
            result = _wait_for_render_readiness("Safari", "full_page", None, 1, 1, "tab-a")
        self.assertFalse(result["ready"])
        self.assertTrue(result["timed_out"])
        self.assertEqual("ZERO_CONTENT_BOUNDS", result["reason_code"])

    def test_capture_never_reports_image_when_render_readiness_fails(self) -> None:
        readiness = {"ready": False, "reason_code": "ZERO_CONTENT_BOUNDS", "attempts": 4, "duration_ms": 320}
        with patch("mcp_server.tools_browser_agent._ensure_dom_rasterizer"), \
             patch("mcp_server.tools_browser_agent._wait_for_render_readiness", return_value=readiness), \
             patch("mcp_server.tools_browser_agent._execute_js_unbounded", return_value="cleaned"):
            image, error, meta = _capture_dom_visual_locked("Safari", "full_page", None, 1, 1, "tab-a")
        self.assertIsNone(image)
        self.assertIn("ZERO_CONTENT_BOUNDS", error or "")
        self.assertEqual("ZERO_CONTENT_BOUNDS", meta["reason_code"])
        self.assertEqual(4, meta["readiness_attempts"])


class BrowserElementReadinessTests(unittest.TestCase):
    def test_shared_readiness_primitive_checks_pointer_events_hit_test_and_stability(self) -> None:
        bootstrap = _browser_state_bootstrap()
        for token in (
            "function __mcpElementReadiness",
            "ELEMENT_POINTER_EVENTS_NONE",
            "ELEMENT_OCCLUDED",
            "ELEMENT_DISABLED",
            "ELEMENT_UNSTABLE",
            "elementFromPoint",
            "stable_for_ms",
        ):
            self.assertIn(token, bootstrap)
        self.assertIn("out.ready=!!rd.ready", bootstrap)

    def test_element_readiness_retries_until_target_becomes_actionable(self) -> None:
        states = [
            {"ready": False, "reason_code": "ELEMENT_UNSTABLE", "stable_for_ms": 90},
            {"ready": True, "reason_code": None, "stable_for_ms": 330, "rect": {"x": 1, "y": 1, "w": 80, "h": 30}},
        ]
        with patch("mcp_server.tools_browser_agent._run_json_js", side_effect=states), \
             patch("mcp_server.tools_browser_agent.time.sleep"):
            result = _wait_for_element_readiness(MagicMock(), "Safari", {"type": "click", "element_id": "e_1"}, 1, 1, "tab-a")
        self.assertTrue(result["ready"])
        self.assertEqual(2, result["_js_calls"])

    def test_occluded_element_times_out_without_executing_action(self) -> None:
        blocked = {"ready": False, "reason_code": "ELEMENT_OCCLUDED", "stable_for_ms": 500}
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={**blocked, "_js_calls": 2}), \
             patch("mcp_server.tools_browser_agent._run_json_js") as run:
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1"}, None, 1, 1, "tab-a"
            )
        self.assertFalse(result["ok"])
        self.assertEqual("element_not_ready", result["error"])
        self.assertEqual("ELEMENT_OCCLUDED", result["reason_code"])
        self.assertEqual(0, run.call_count)

    def test_click_without_observable_effect_is_not_success(self) -> None:
        batch_result = {
            "ok": True,
            "actions": [{
                "ok": True,
                "type": "click",
                "element_id": "e_1",
                "effect_observed": False,
                "verification": "no_immediate_effect",
                "_verify_revision": 5,
                "_verify_url": "https://example.test/app",
                "_verify_title": "App",
                "_verify_state": {"connected": True, "value": "", "text": "", "checked": False, "modalFingerprint": ""},
                "activation_trace": {
                    "mode": "synthetic_dom", "target_click_seen": True,
                    "window_click_seen": True, "click_is_trusted": False,
                },
            }],
            "state": {"url": "https://example.test/app", "title": "App", "dom_revision": 5},
        }
        unchanged = {
            "ok": True,
            "connected": True,
            "url": "https://example.test/app",
            "title": "App",
            "dom_revision": 5,
            "value": "",
            "text": "",
            "checked": False,
            "modal_fingerprint": "",
        }
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True, "stable_for_ms": 500, "_js_calls": 1}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[batch_result, unchanged]), \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=[0.0, 0.01, 0.20]), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1", "verify_timeout_s": 0.1}, None, 1, 1, "tab-a"
            )
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertEqual("no_effect_after_bounded_wait", result["verification"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual("synthetic_dom", result["activation_mode"])
        self.assertEqual("untrusted", result["input_trust"])

    # ASSURANCE: SEC-COMP-001
    def test_unrelated_dom_revision_is_not_accepted_as_click_effect(self) -> None:
        batch_result = {
            "ok": True,
            "actions": [{
                "ok": True, "type": "click", "element_id": "e_1",
                "effect_observed": False, "verification": "no_immediate_effect",
                "_verify_revision": 5, "_verify_url": "https://example.test/app", "_verify_title": "App",
                "_verify_state": {"connected": True, "value": "", "text": "", "checked": False, "modalFingerprint": ""},
            }],
            "state": {"url": "https://example.test/app", "title": "App", "dom_revision": 5},
        }
        unrelated_mutation = {
            "ok": True, "connected": True, "url": "https://example.test/app", "title": "App",
            "dom_revision": 6, "value": "", "text": "", "checked": False, "modal_fingerprint": "",
        }
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True, "stable_for_ms": 500, "_js_calls": 1}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[batch_result, unrelated_mutation]), \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=[0.0, 0.01, 0.20]), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1", "verify_timeout_s": 0.1}, None, 1, 1, "tab-a"
            )
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])

    def test_async_modal_state_change_is_accepted_without_second_activation(self) -> None:
        batch_result = {
            "ok": True,
            "actions": [{
                "ok": True,
                "type": "click",
                "element_id": "e_1",
                "effect_observed": False,
                "verification": "deferred_pending",
                "_verify_revision": 5,
                "_verify_url": "https://example.test/app",
                "_verify_title": "App",
                "_verify_state": {"connected": True, "value": "", "text": "", "checked": False, "modalFingerprint": ""},
            }],
            "state": {"url": "https://example.test/app", "title": "App", "dom_revision": 5},
        }
        changed = {
            "ok": True,
            "connected": True,
            "url": "https://example.test/app",
            "title": "App",
            "dom_revision": 6,
            "value": "",
            "text": "",
            "checked": False,
            "modal_fingerprint": "div|dialog|open|Product details|500|600",
            "activation_trace": {
                "mode": "synthetic_dom", "target_click_seen": True,
                "window_click_seen": True, "click_is_trusted": False,
            },
        }
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True, "stable_for_ms": 500, "_js_calls": 1}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[batch_result, changed]), \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=[0.0, 0.01]), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1", "verify_timeout_s": 0.1}, None, 1, 1, "tab-a"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["effect_observed"])
        self.assertEqual("async_state_changed", result["verification"])
        self.assertEqual("untrusted", result["input_trust"])

    # ASSURANCE: SEC-COMP-001
    def test_action_correlated_network_activity_is_accepted_as_effect(self) -> None:
        batch_result = {
            "ok": True,
            "actions": [{
                "ok": True, "type": "click", "element_id": "e_1",
                "effect_observed": False, "verification": "no_immediate_effect",
                "_verify_revision": 5, "_verify_url": "https://example.test/app", "_verify_title": "App",
                "_verify_state": {"connected": True, "value": "", "text": "", "checked": False, "modalFingerprint": "", "activationNetworkCount": 0},
                "activation_trace": {"mode": "synthetic_dom", "target_click_seen": True, "window_click_seen": True, "click_is_trusted": False, "network_count": 0},
            }],
            "state": {"url": "https://example.test/app", "title": "App", "dom_revision": 5},
        }
        network_changed = {
            "ok": True, "connected": True, "url": "https://example.test/app", "title": "App",
            "dom_revision": 5, "value": "", "text": "", "checked": False, "modal_fingerprint": "",
            "activation_network_count": 1,
            "activation_trace": {"mode": "synthetic_dom", "target_click_seen": True, "window_click_seen": True, "click_is_trusted": False, "network_count": 1, "network_paths": ["same-origin:/api/cart"]},
        }
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True, "stable_for_ms": 500, "_js_calls": 1}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[batch_result, network_changed]), \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=[0.0, 0.01]), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e_1", "verify_timeout_s": 0.1}, None, 1, 1, "tab-a"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["effect_observed"])
        self.assertEqual("async_network_activity", result["verification"])
        self.assertEqual(1, result["activation_trace"]["network_count"])
        self.assertEqual(["same-origin:/api/cart"], result["activation_trace"]["network_paths"])

    # ASSURANCE: SEC-FOCUS-001
    def test_trusted_input_fails_closed_on_safari_without_foreground_fallback(self) -> None:
        readiness = {"ready": True, "stable_for_ms": 500, "rect": {"x": 10, "y": 20, "w": 100, "h": 40}, "_js_calls": 1}
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value=readiness), \
             patch("mcp_server.tools_browser_agent._run_json_js") as run:
            result = _verified_dom_action(
                MagicMock(), "Safari",
                {"type": "click", "element_id": "e_1", "input_mode": "trusted"},
                None, 1, 1, "tab-a",
            )
        self.assertFalse(result["ok"])
        self.assertEqual("TRUSTED_INPUT_UNAVAILABLE", result["reason_code"])
        self.assertFalse(result["foreground_fallback"])
        self.assertFalse(result["automatic_retry"])
        run.assert_not_called()

    # ASSURANCE: SEC-FOCUS-001
    def test_trusted_chrome_click_uses_background_debugger_coordinates_and_verifies_effect(self) -> None:
        readiness = {"ready": True, "stable_for_ms": 500, "rect": {"x": 10, "y": 20, "w": 100, "h": 40}, "_js_calls": 1}
        before = {
            "ok": True, "connected": True, "url": "https://example.test/app", "title": "App",
            "dom_revision": 5, "value": "", "text": "", "checked": False, "modal_fingerprint": "", "activation_network_count": 0,
        }
        changed = {
            **before, "dom_revision": 6, "aria_checked": "true",
        }
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value=readiness), \
             patch("mcp_server.tools_browser_agent.chrome_background_bridge.is_connected", return_value=True), \
             patch("mcp_server.tools_browser_agent.chrome_background_bridge.request_dispatch_mouse") as dispatch, \
             patch("mcp_server.tools_browser_agent.browser_tabs.resolve_tab", return_value=(1, 2, {"native_id": "99"})), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[before, changed]), \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=[0.0, 0.01]), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Google Chrome",
                {"type": "click", "element_id": "e_1", "input_mode": "trusted", "verify_timeout_s": 0.1},
                None, 1, 2, "tab-a",
            )
        dispatch.assert_called_once_with("99", 60.0, 40.0, click_count=1, timeout_s=8.0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["effect_observed"])
        self.assertEqual("trusted_chrome_cdp", result["activation_mode"])
        self.assertEqual("browser_debugger", result["input_trust"])
        self.assertEqual("async_state_changed", result["verification"])

    def test_click_path_has_no_automatic_keyboard_second_activation(self) -> None:
        script = _batch_js([{"type": "click", "element_id": "e_1"}], None)
        self.assertNotIn("__mcpKeyboardActivate(activated||el)", script)
        self.assertIn("deferred_pending", script)


if __name__ == "__main__":
    unittest.main()
