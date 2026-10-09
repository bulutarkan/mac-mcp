from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _batch_js, _browser_act_locked, _select_prepare_js

_STALE = {"ok": False, "type": "click", "element_id": "e_old", "error": "stale_target",
          "reason_code": "ROUTE_CHANGED", "observe_again": True, "no_side_effect": True}


class StaleTargetJsTests(unittest.TestCase):
    def test_batch_and_select_check_the_remembered_target_before_acting(self) -> None:
        for js in (_batch_js([{"type": "click", "element_id": "e_1"}], "obs"), _select_prepare_js("e_1", "obs", "x")):
            self.assertIn("ROUTE_CHANGED", js)
            self.assertIn("TARGET_CHANGED", js)
            self.assertIn("no_side_effect:true", js)
            self.assertIn("function __mcpTargetFp(", js)
        batch = _batch_js([{"type": "click", "element_id": "e_1"}], "obs")
        # The route check runs before the target is looked up or touched.
        self.assertLess(batch.index("ROUTE_CHANGED"), batch.index("var el=a.element_id?target(a):null;"))

    def test_observe_and_find_refresh_fingerprints(self) -> None:
        self.assertIn("__mcpRememberTarget(el,desc.element_id,s);", agent._observe_js("interactive", 10))
        self.assertIn("__mcpRememberTarget(els[fi],out[fi].element_id,s)", agent._find_candidates_js("reply", None, None, 5))

    def test_badge_digits_and_typed_values_do_not_change_the_fingerprint(self) -> None:
        bootstrap = agent._browser_state_bootstrap()
        self.assertIn(".replace(/[0-9]+/g,'#')", bootstrap)
        self.assertIn("(editable?'':__mcpText(el))", bootstrap)
        # Hash anchors are not routes unless the app routes with #/ or #!.
        self.assertIn("h.indexOf('#/')===0||h.indexOf('#!')===0", bootstrap)


class StaleTargetActTests(unittest.TestCase):
    def _act(self, actions, verified_results, find_results=()):
        calls = []

        def fake_verified(_settings, _browser, action, *_args, **_kwargs):
            calls.append(dict(action))
            return dict(verified_results[len(calls) - 1], _js_calls=1)

        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "browser_find", side_effect=list(find_results)) as find, \
             patch.object(agent, "_verified_dom_action", side_effect=fake_verified), \
             patch.object(agent, "_run_json_js", return_value={"ok": True, "url": "u", "title": "t", "dom_revision": 1}):
            result = _browser_act_locked(MagicMock(), "Safari", actions, observation_id="obs_old",
                                         window_index=1, tab_index=1, tab_handle="tab", return_state="none")
        return result, calls, find

    def test_stale_id_without_a_locator_fails_without_counting_as_dispatched(self) -> None:
        result, calls, find = self._act([{"type": "click", "element_id": "e_old"}], [_STALE])
        self.assertFalse(result["ok"])
        self.assertFalse(result["mutation_dispatched"])
        self.assertEqual("stale_target", result["actions"][0]["error"])
        self.assertEqual(1, len(calls))
        find.assert_not_called()

    def test_stale_id_with_a_locator_is_resolved_again_once(self) -> None:
        fresh = {"element_id": "e_new", "text": "Delete", "role": "button", "confidence": 1.0}
        result, calls, find = self._act(
            [{"type": "click", "element_id": "e_old", "query": "Delete", "role": "button"}],
            [_STALE, {"ok": True, "type": "click", "element_id": "e_new"}],
            [{"ok": True, "best_match": fresh, "matches": [fresh]}],
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["mutation_dispatched"])
        self.assertEqual(["e_old", "e_new"], [call["element_id"] for call in calls])
        self.assertEqual(1, find.call_count)
        self.assertEqual("Delete", find.call_args.kwargs["query"])
        self.assertEqual({"stale_element_id": "e_old", "reason_code": "ROUTE_CHANGED"}, result["actions"][0]["re_resolved"])

    def test_locator_that_no_longer_matches_stops_safely(self) -> None:
        result, calls, _find = self._act(
            [{"type": "click", "element_id": "e_old", "query": "Delete"}],
            [_STALE],
            [{"ok": True, "best_match": None, "matches": []}],
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["mutation_dispatched"])
        self.assertEqual("target_not_found", result["actions"][0]["error"])
        self.assertEqual("e_old", result["actions"][0]["stale_element_id"])
        self.assertEqual(1, len(calls))

    def test_earlier_dispatch_is_still_reported(self) -> None:
        result, _calls, _find = self._act(
            [{"type": "click", "element_id": "e_a"}, {"type": "click", "element_id": "e_old"}],
            [{"ok": True, "type": "click", "element_id": "e_a"}, _STALE],
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["mutation_dispatched"])


if __name__ == "__main__":
    unittest.main()
