from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent
from mcp_server.main import BROWSER_ACT_DESCRIPTION
from mcp_server.tools_browser_agent import (
    _browser_act_locked,
    _dom_key_action,
    _dom_key_js,
    _dom_key_spec,
    _late_target_wait_s,
)


def _act(actions, **kwargs):
    with patch.object(agent, "_run_json_js", return_value={"ok": True}):
        return _browser_act_locked(
            MagicMock(), "Safari", actions,
            window_index=1, tab_index=1, tab_handle="tab-1", return_state="none", **kwargs,
        )


class DomKeySpecTests(unittest.TestCase):
    def test_named_keys_and_characters_map_to_dom_key_fields(self) -> None:
        self.assertEqual(("Enter", "Enter", 13), _dom_key_spec("return"))
        self.assertEqual(("Enter", "Enter", 13), _dom_key_spec("Enter"))
        self.assertEqual(("Escape", "Escape", 27), _dom_key_spec("esc"))
        self.assertEqual(("ArrowDown", "ArrowDown", 40), _dom_key_spec("arrow_down"))
        self.assertEqual(("a", "KeyA", 65), _dom_key_spec("a"))
        self.assertEqual(("7", "Digit7", 55), _dom_key_spec("7"))
        self.assertIsNone(_dom_key_spec("F5"))
        self.assertIsNone(_dom_key_spec(""))

    def test_script_emulates_implicit_submission_and_legacy_key_codes(self) -> None:
        script = _dom_key_js("e1", ("Enter", "Enter", 13), ["cmd", "shift", "bogus"])
        self.assertIn("requestSubmit", script)
        self.assertIn("keyCode", script)
        spec = json.loads(script.split("spec=", 1)[1].split(",eid=", 1)[0])
        self.assertEqual(
            {"key": "Enter", "code": "Enter", "keyCode": 13, "ctrlKey": False, "shiftKey": True, "altKey": False, "metaKey": True},
            spec,
        )


class DomKeyActionTests(unittest.TestCase):
    def _run(self, *responses, action=None):
        queue = list(responses)

        def respond(*args, **kwargs):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        with patch.object(agent, "_run_json_js", side_effect=respond), \
                patch.object(agent, "cancellable_sleep"), \
                patch.object(agent, "_ACTION_VERIFY_TIMEOUT_S", 0.2):
            return _dom_key_action(MagicMock(), "Safari", action or {"type": "key", "key": "Enter"}, "e1", 1, 1, "tab-1")

    def _dispatched(self, **extra):
        return {
            "ok": True, "element_id": "e1", "key": "Enter", "keydown_handled": False, "form_submitted": False,
            "_verify_revision": 5, "_verify_url": "https://x.test/", "_verify_title": "X",
            "_verify_state": {"connected": True, "value": "25 Nov", "cls": "a"}, **extra,
        }

    def _state(self, **extra):
        return {"ok": True, "connected": True, "url": "https://x.test/", "title": "X", "dom_revision": 5,
                "value": "25 Nov", "class_name": "a", **extra}

    def test_dom_mutation_after_key_counts_as_effect(self) -> None:
        result = self._run(self._dispatched(), self._state(dom_revision=6))
        self.assertTrue(result["ok"])
        self.assertEqual("dom_mutated", result["verification"])
        self.assertEqual("dom", result["input_mode"])
        self.assertEqual("untrusted", result["input_trust"])

    def test_form_submission_or_page_handled_keydown_is_reported(self) -> None:
        submitted = self._run(self._dispatched(form_submitted=True), self._state())
        self.assertEqual(("form_submitted", True), (submitted["verification"], submitted["ok"]))
        handled = self._run(self._dispatched(keydown_handled=True), self._state())
        self.assertEqual(("keydown_handled_by_page", True), (handled["verification"], handled["ok"]))

    def test_ignored_key_reports_no_effect_without_retry(self) -> None:
        result = self._run(self._dispatched(), self._state())
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertIn("untrusted", result["reason"])

    def test_unsupported_key_and_missing_focus_fail_closed(self) -> None:
        unsupported = self._run(action={"type": "key", "key": "F5"})
        self.assertEqual("UNSUPPORTED_DOM_KEY", unsupported["reason_code"])
        missing = self._run({"ok": False, "error": "no_focused_element", "reason_code": "DOM_KEY_TARGET_REQUIRED"})
        self.assertEqual(("DOM_KEY_TARGET_REQUIRED", False), (missing["reason_code"], missing["ok"]))


class BrowserActKeyRoutingTests(unittest.TestCase):
    def _patches(self, dom_result=None):
        dom = MagicMock(return_value=dom_result or {"ok": True, "type": "key", "key": "Enter", "_js_calls": 2})
        native = MagicMock(return_value={"ok": False, "foreground_required": True, "reason_code": "FOREGROUND_REQUIRED"})
        return dom, native

    def test_key_without_foreground_uses_dom_path_and_never_native(self) -> None:
        dom, native = self._patches()
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_dom_key_action", dom), \
                patch.object(agent, "browser_press_key", native):
            result = _act([{"type": "key", "key": "Enter"}])
        self.assertTrue(result["ok"])
        dom.assert_called_once()
        native.assert_not_called()

    def test_explicit_native_mode_keeps_foreground_guard(self) -> None:
        dom, native = self._patches()
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_dom_key_action", dom), \
                patch.object(agent, "browser_press_key", native):
            result = _act([{"type": "key", "key": "Enter", "input_mode": "native"}])
        self.assertFalse(result["ok"])
        self.assertEqual("FOREGROUND_REQUIRED", result["reason_code"])
        dom.assert_not_called()

    def test_type_then_enter_runs_in_one_batch_on_the_resolved_input(self) -> None:
        dom, native = self._patches()
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "browser_find", return_value={"best_match": {"element_id": "e9", "confidence": 1.0}}), \
                patch.object(agent, "_verified_dom_action", return_value={"ok": True, "type": "type", "_js_calls": 1}), \
                patch.object(agent, "_dom_key_action", dom), \
                patch.object(agent, "browser_press_key", native):
            result = _act([
                {"type": "type", "query": "Departure", "text": "25 Nov 2026"},
                {"type": "key", "key": "Enter", "query": "Departure"},
            ])
        self.assertTrue(result["ok"])
        self.assertEqual("e9", dom.call_args.args[3])

    def test_dom_key_respects_delegated_mutation_revalidation(self) -> None:
        dom, native = self._patches()
        blocked = {"ok": False, "error": "human_takeover", "reason_code": "HUMAN_TAKEOVER"}
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "delegated_agent_identity", return_value={"agent_id": "a1"}), \
                patch.object(agent.browser_tabs, "revalidate_mutation_lease", return_value=(None, blocked)), \
                patch.object(agent, "_dom_key_action", dom):
            result = _act([{"type": "key", "key": "Enter"}], lease_generation=1)
        self.assertFalse(result["ok"])
        self.assertEqual("HUMAN_TAKEOVER", result["reason_code"])
        dom.assert_not_called()


class LateTargetWaitTests(unittest.TestCase):
    def test_wait_only_after_a_successful_mutation_or_explicit_wait(self) -> None:
        self.assertEqual(0.0, _late_target_wait_s({}, []))
        self.assertEqual(0.0, _late_target_wait_s({}, [{"ok": True, "type": "scroll"}]))
        self.assertEqual(0.0, _late_target_wait_s({}, [{"ok": False, "type": "click"}]))
        self.assertEqual(2.5, _late_target_wait_s({}, [{"ok": True, "type": "key"}]))
        self.assertEqual(4.0, _late_target_wait_s({"wait_s": 4}, []))
        self.assertEqual(10.0, _late_target_wait_s({"wait_s": 99}, []))
        self.assertEqual(0.0, _late_target_wait_s({"wait_s": 0}, [{"ok": True, "type": "click"}]))

    def _run_batch(self, actions, finds):
        found = iter(finds)
        calls = []

        def fake_find(*args, **kwargs):
            calls.append(kwargs)
            return next(found)

        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "browser_find", side_effect=fake_find), \
                patch.object(agent, "_verified_dom_action",
                             side_effect=lambda *a, **k: {"ok": True, "type": a[2]["type"], "_js_calls": 1}):
            return _act(actions), calls

    def test_confirm_button_revealed_by_earlier_step_is_awaited_in_same_batch(self) -> None:
        result, calls = self._run_batch(
            [{"type": "click", "query": "25 November"}, {"type": "click", "query": "Done", "role": "button"}],
            [{"best_match": {"element_id": "day", "confidence": 1.0}},
             {"best_match": None, "matches": []},
             {"best_match": {"element_id": "done", "confidence": 0.96}}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(3, len(calls))
        self.assertEqual(0.0, calls[1].get("wait_timeout_s", 0.0))
        self.assertEqual(2.5, calls[2]["wait_timeout_s"])

    def test_first_action_missing_target_fails_fast(self) -> None:
        result, calls = self._run_batch(
            [{"type": "click", "query": "Done"}],
            [{"best_match": None, "matches": []}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual("target_not_found", result["error"])
        self.assertEqual(1, len(calls))

    def test_description_tells_agents_about_background_keys_and_waits(self) -> None:
        self.assertIn("background DOM keyboard events", BROWSER_ACT_DESCRIPTION)
        self.assertIn("wait_s", BROWSER_ACT_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
