from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent


def _click_result(**extra):
    return {
        "index": 0, "type": "click", "element_id": "e1", "ok": True, "deferred": False,
        "effect_observed": False, "verification": "no_immediate_effect", "observe_again": True,
        "_verify_revision": 3, "_verify_url": "https://x.test/", "_verify_title": "X",
        "_verify_state": {"connected": True, "cls": "btn"}, **extra,
    }


def _post(**extra):
    return {"ok": True, "connected": True, "url": "https://x.test/", "title": "X",
            "dom_revision": 3, "class_name": "btn", **extra}


class ClickDomEffectTests(unittest.TestCase):
    def setUp(self) -> None:
        # These cases script the stepwise readiness-then-action calls explicitly.
        fused = patch.object(agent, "_try_fused_action", return_value={"js_calls": 0})
        fused.start()
        self.addCleanup(fused.stop)

    def _verify(self, click, *posts):
        queue = [{"ok": True, "actions": [click]}, *posts]

        def respond(*args, **kwargs):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        with patch.object(agent, "_wait_for_element_readiness", return_value={"ready": True, "_js_calls": 1}), \
                patch.object(agent, "_run_json_js", side_effect=respond), \
                patch.object(agent, "cancellable_sleep"), \
                patch.object(agent, "_ACTION_VERIFY_TIMEOUT_S", 0.2):
            return agent._verified_dom_action(
                MagicMock(), "Safari", {"type": "click", "element_id": "e1"}, None, 1, 1, "tab-1",
            )

    def test_delayed_mutation_after_quiet_page_counts_as_effect(self) -> None:
        result = self._verify(_click_result(_verify_quiet_ms=900), _post(), _post(dom_revision=4))
        self.assertTrue(result["ok"])
        self.assertEqual("async_dom_mutated", result["verification"])
        self.assertNotIn("observe_again", result)
        self.assertNotIn("_verify_quiet_ms", result)

    def test_mutation_on_a_busy_page_is_not_an_effect(self) -> None:
        result = self._verify(_click_result(_verify_quiet_ms=40), _post(dom_revision=9))
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])

    def test_quiet_page_without_any_change_is_still_no_effect(self) -> None:
        result = self._verify(_click_result(_verify_quiet_ms=900), _post())
        self.assertFalse(result["ok"])
        self.assertEqual("no_effect_after_bounded_wait", result["verification"])

    def test_missing_quiet_measurement_keeps_old_behavior(self) -> None:
        result = self._verify(_click_result(), _post(dom_revision=4))
        self.assertFalse(result["ok"])

    def test_target_state_change_is_still_reported_as_state_change(self) -> None:
        result = self._verify(_click_result(_verify_quiet_ms=900), _post(class_name="btn active", dom_revision=4))
        self.assertEqual("async_state_changed", result["verification"])


class ClickScriptTests(unittest.TestCase):
    def test_batch_script_checks_quiet_page_mutations_with_threshold(self) -> None:
        script = agent._batch_js([{"type": "click", "element_id": "e1"}], None)
        self.assertIn(f"beforeQuietMs>={agent._DOM_EFFECT_QUIET_MS}", script)
        self.assertIn("verification='dom_mutated'", script)
        self.assertIn("_verify_quiet_ms:beforeQuietMs", script)
        self.assertNotIn("__QUIET_MS__", script.split("var actions=", 1)[0])

    def test_typed_text_cannot_inject_template_placeholders(self) -> None:
        script = agent._batch_js([{"type": "type", "element_id": "e1", "text": "__QUIET_MS__ __OBS__"}], None)
        self.assertIn('"__QUIET_MS__ __OBS__"', script)

    def test_visual_companion_mutations_are_ignored_by_every_observer(self) -> None:
        bootstrap = agent._browser_state_bootstrap()
        self.assertIn("function __mcpInternalMutation(rec)", bootstrap)
        self.assertIn("__mcpInternalHost(rec.target)", bootstrap)
        source = open(agent.__file__, encoding="utf-8").read()
        self.assertNotIn("rec.attributeName==='data-mac-mcp-visual-event')continue", source)
        self.assertEqual(3, source.count("if(__mcpInternalMutation(rec))continue;"))


if __name__ == "__main__":
    unittest.main()
