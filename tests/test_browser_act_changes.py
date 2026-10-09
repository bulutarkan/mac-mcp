"""browser_act reports what changed, offers near matches, and reaches off-screen targets."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _browser_act_locked


def _act(actions, finds):
    calls = []

    def fake_find(*_args, **kwargs):
        calls.append(kwargs)
        return finds[len(calls) - 1]

    with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
         patch.object(agent, "browser_find", side_effect=fake_find), \
         patch.object(agent, "_run_json_js", return_value={"ok": True, "url": "u", "title": "t", "dom_revision": 1}):
        result = _browser_act_locked(MagicMock(), "Safari", actions, observation_id="obs-1",
                                     window_index=1, tab_index=1, tab_handle="tab", return_state="none")
    return result, calls


class NearMatchTests(unittest.TestCase):
    def test_missed_constrained_target_returns_near_matches_from_one_relaxed_search(self) -> None:
        near = {"element_id": "e_7", "role": "link", "tag": "a", "text": "Submit", "confidence": 0.9, "rect": {}}
        result, calls = _act(
            [{"type": "click", "query": "Submit", "role": "button"}],
            [{"best_match": None, "matches": []}, {"best_match": near, "matches": [near]}],
        )
        self.assertFalse(result["ok"])
        failed = result["actions"][0]
        self.assertEqual("target_not_found", failed["error"])
        self.assertEqual([{"element_id": "e_7", "role": "link", "tag": "a", "text": "Submit", "confidence": 0.9}],
                         failed["near_matches"])
        self.assertEqual(2, len(calls))
        self.assertEqual("button", calls[0]["role"])
        self.assertNotIn("role", calls[1])
        self.assertEqual(5, calls[1]["max_results"])
        self.assertFalse(result["mutation_dispatched"])

    def test_nothing_to_relax_means_no_second_search(self) -> None:
        result, calls = _act([{"type": "click", "query": "Submit"}], [{"best_match": None, "matches": []}])
        self.assertEqual("target_not_found", result["actions"][0]["error"])
        self.assertNotIn("near_matches", result["actions"][0])
        self.assertEqual(1, len(calls))


class OffscreenTargetTests(unittest.TestCase):
    def test_finder_keeps_rendered_offscreen_elements_and_flags_them(self) -> None:
        script = agent._find_candidates_js("Submit", "button", None, 60)
        self.assertIn("if(__mcpSemanticVisible(el)) return true;", script)
        self.assertIn("offscreen.add(el); return true;", script)
        self.assertIn("if(offscreen.has(el))d.offscreen=true;", script)

    def test_offscreen_candidate_loses_to_an_on_screen_twin(self) -> None:
        twin = {"element_id": "e_top", "tag": "button", "role": "button", "text": "Submit", "actionable": True}
        below = dict(twin, element_id="e_below", offscreen=True)
        payload = {"ok": True, "elements": [below, twin]}
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "_ensure_visual_companion"), \
             patch.object(agent, "_run_json_js", return_value=payload):
            found = agent._browser_find_impl(MagicMock(), "Safari", "Submit", role="button", tab_handle="tab")
        self.assertEqual("e_top", found["best_match"]["element_id"])
        flagged = next(m for m in found["matches"] if m["element_id"] == "e_below")
        self.assertTrue(flagged["offscreen"])
        self.assertLess(flagged["confidence"], found["best_match"]["confidence"])

    def test_readiness_scrolls_an_offscreen_or_covered_target_once_but_never_while_observing(self) -> None:
        source = agent._bootstrap_functions_source()
        start = source.index("function __mcpElementReadiness(el,kind,minStableMs){")
        wrapper = source[start:source.index("function __mcpElementReadinessOnce", start)]
        self.assertIn("k==='observe'", wrapper)
        self.assertIn("'ELEMENT_OFFSCREEN'", wrapper)
        self.assertIn("'ELEMENT_OCCLUDED'", wrapper)
        self.assertIn("behavior:'instant'", wrapper)  # a smooth scroll would still be moving at the re-check
        self.assertIn("again.scrolled_into_view=true", wrapper)
        self.assertEqual(2, wrapper.count("__mcpElementReadinessOnce("))


class ChangesSummaryTests(unittest.TestCase):
    def test_summary_compares_with_the_acted_observation_then_the_previous_act(self) -> None:
        script = agent._changes_js("bobs_x")
        self.assertIn('(s.observationMeta||{})["bobs_x"]', script)
        self.assertIn("base=(meta&&meta.ids)?meta:s.lastSeen", script)
        for key in ("appeared_count", "disappeared_count", "url_changed", "invalid_fields", "messages", "modal"):
            self.assertIn(key, script)
        self.assertIn("s.lastSeen={ids:now.map", script)

    def test_compact_state_and_changes_share_one_final_read(self) -> None:
        read = {"ok": True, "url": "https://e.test/b", "title": "B", "dom_revision": 3,
                "changes": {"url_changed": True, "appeared_count": 0, "disappeared_count": 2}}
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(agent, "_verified_dom_action",
                          return_value={"ok": True, "type": "click", "element_id": "e1", "_js_calls": 1}), \
             patch.object(agent, "_run_json_js", return_value=read) as run_js:
            result = _browser_act_locked(MagicMock(), "Safari", [{"type": "click", "element_id": "e1"}],
                                         observation_id="obs-1", window_index=1, tab_index=1,
                                         tab_handle="tab", return_state="compact")
        run_js.assert_called_once()
        self.assertEqual({"url": "https://e.test/b", "title": "B", "dom_revision": 3}, result["state"])
        self.assertEqual(read["changes"], result["changes"])


if __name__ == "__main__":
    unittest.main()
