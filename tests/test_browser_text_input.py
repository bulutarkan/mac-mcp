from unittest import TestCase
from unittest.mock import MagicMock, patch

from mcp_server.tools_browser import browser_type_selector
from mcp_server.tools_browser_agent import _verified_dom_action


class BrowserTextInputTests(TestCase):
    def _run_typing(self, readbacks, clock):
        dispatched = {"ok": True, "actions": [{
            "ok": True, "type": "type", "element_id": "e_editor",
            "verification": "input_dispatched", "_input_expected": "new text",
            "input_method": "editor_paste_event",
        }]}
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True}), \
             patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[dispatched, *readbacks]) as execute, \
             patch("mcp_server.tools_browser_agent.time.perf_counter", side_effect=clock), \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _verified_dom_action(
                MagicMock(), "Google Chrome",
                {"type": "type", "element_id": "e_editor", "text": "new text"},
                None, 1, None, "tab-editor",
            )
        return result, execute.call_args_list

    def test_editor_must_accept_two_delayed_readbacks(self):
        pending = {"connected": True, "matches": False, "value": "old text"}
        accepted = {"connected": True, "matches": True, "value": "new text"}
        result, calls = self._run_typing([pending, accepted, accepted], [0, 0.1, 0.2, 0.3])
        self.assertTrue(result["ok"])
        self.assertEqual("dom_readback_verified", result["verification"])
        self.assertFalse(result["persistence_verified"])
        self.assertNotIn("_input_expected", result)
        self.assertEqual(4, len(calls))

    def test_immediate_write_reverted_by_editor_is_failure_without_replay(self):
        first = {"connected": True, "matches": True, "value": "new text"}
        reverted = {"connected": True, "matches": False, "value": "old text"}
        result, calls = self._run_typing([first, reverted], [0, 0.1, 0.3, 2])
        self.assertFalse(result["ok"])
        self.assertEqual("input_not_applied", result["error"])
        self.assertEqual("old text", result["value"])
        self.assertFalse(result["automatic_retry"])
        self.assertFalse(result["foreground_fallback"])
        self.assertEqual(1, sum("var actions=" in call.args[2] for call in calls))

    def test_failed_readback_does_not_claim_input_success(self):
        result, _ = self._run_typing([RuntimeError("transport unavailable")], [0, 0.1])
        self.assertFalse(result["ok"])
        self.assertEqual("input_readback_failed", result["error"])

    def test_selector_uses_shared_verified_action_and_preserves_append_flag(self):
        expected = {"ok": False, "actions": [{"ok": False, "error": "input_not_applied"}]}
        with patch("mcp_server.tools_browser_agent.browser_act", return_value=expected) as act:
            result = browser_type_selector(
                MagicMock(), "Google Chrome", "#editor", " appended",
                clear=False, tab_handle="tab-editor",
            )
        self.assertEqual(expected, result)
        self.assertEqual({"type": "type", "selector": "#editor", "text": " appended", "clear": False}, act.call_args.kwargs["actions"][0])
        self.assertFalse(act.call_args.kwargs["allow_foreground"])

    def test_native_input_skips_readback_roundtrips_and_waits(self):
        dispatched = {"ok": True, "actions": [{
            "ok": True, "type": "type", "element_id": "e_native",
            "verification": "value_transformed", "value": "(555) 123-4567",
            "input_method": "native_value_setter", "persistence_verified": False,
        }]}
        with patch("mcp_server.tools_browser_agent._wait_for_element_readiness", return_value={"ready": True}), \
             patch("mcp_server.tools_browser_agent._run_json_js", return_value=dispatched) as execute, \
             patch("mcp_server.tools_browser_agent.cancellable_sleep") as sleep:
            result = _verified_dom_action(
                MagicMock(), "Safari", {"type": "type", "element_id": "e_native", "text": "5551234567"},
                None, 1, None, "tab-native",
            )
        self.assertTrue(result["ok"])
        self.assertEqual("value_transformed", result["verification"])
        execute.assert_called_once()
        sleep.assert_not_called()
