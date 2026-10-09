from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server.tools_browser_agent import _browser_act_locked

LIGHT_STATE = {"ok": True, "url": "https://example.test/after", "title": "After", "dom_revision": 9}


class BrowserDoProgressTests(unittest.TestCase):
    def _run(self, actions, compact_state):
        def fake_verified(_settings, _browser, action, *_args, **_kwargs):
            result = {"ok": True, "type": action["type"], "element_id": action["element_id"], "_js_calls": 1}
            if compact_state is not None:
                result["_compact_state"] = dict(compact_state)
            return result

        def fake_extract(*_args, **_kwargs):
            return {"ok": True, "type": "extract", "data": {}, "_js_calls": 1}

        with patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 1)), \
             patch("mcp_server.tools_browser_agent._verified_dom_action", side_effect=fake_verified), \
             patch("mcp_server.tools_browser_agent._extract_action", side_effect=fake_extract), \
             patch("mcp_server.tools_browser_agent._run_json_js", return_value=dict(LIGHT_STATE)) as light:
            result = _browser_act_locked(
                MagicMock(), "Safari", actions, observation_id="obs-1",
                window_index=1, tab_index=1, tab_handle="tab-1", return_state="none",
            )
        return result, light

    def test_progress_and_changes_come_from_one_final_read(self) -> None:
        # The action's own state is not enough: the final read also says what the batch changed.
        state = {"url": "https://example.test/next", "title": "Next", "dom_revision": 4, "active_element": None}
        changes = {"baseline": "observation", "url_changed": True, "appeared_count": 1,
                   "appeared": [{"element_id": "e_9", "tag": "button", "text": "Confirm"}], "disappeared_count": 0}
        with patch.dict(LIGHT_STATE, {"changes": changes}):
            result, light = self._run([{"type": "click", "element_id": "e_1"}], state)
        self.assertTrue(result["ok"])
        self.assertEqual({"url": "https://example.test/after", "title": "After", "dom_revision": 9}, result["progress"])
        self.assertEqual(changes, result["changes"])
        light.assert_called_once()
        self.assertIn("observationMeta||{})[\"obs-1\"]", light.call_args.args[2])
        self.assertEqual(2, result["internal_js_calls"])

    def test_progress_falls_back_when_state_is_missing_incomplete_or_stale(self) -> None:
        expected = {key: LIGHT_STATE[key] for key in ("url", "title", "dom_revision")}
        cases = {
            "no_state": ([{"type": "click", "element_id": "e_1"}], None),
            "no_revision": ([{"type": "click", "element_id": "e_1"}], {"url": "https://example.test/x", "title": "X"}),
            "stale": (
                [{"type": "click", "element_id": "e_1"}, {"type": "extract", "fields": ["price"]}],
                {"url": "https://example.test/x", "title": "X", "dom_revision": 2},
            ),
        }
        for name, (actions, state) in cases.items():
            with self.subTest(case=name):
                result, light = self._run(actions, state)
                self.assertTrue(result["ok"], result)
                self.assertEqual(expected, result["progress"])
                light.assert_called_once()


if __name__ == "__main__":
    unittest.main()
