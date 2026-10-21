"""#87: the 20-action limit counts the caller's actions, not the checks expect adds.

browser_act let 20 actions through, then added a wait for each expect; the locked path counted that
longer list against the same limit and refused it with 400 after the tab was leased (and, for
browser_do, after the tab was opened), not marked as not executed.
"""
from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser_agent as agent
from mcp_server.workflow_checkpoints import exception_not_executed


class Reached(Exception):
    pass


@contextlib.contextmanager
def _lease(*_args, **_kwargs):
    yield SimpleNamespace(browser="Safari", window_index=1, tab_index=1, tab_handle="btab_safari_x", lease_generation=1)


class BrowserActionLimitTests(unittest.TestCase):
    def act(self, actions):
        with patch.object(agent, "_require_stable_handle_for_mutation"), \
             patch.object(agent, "_ensure_visual_companion"), \
             patch.object(agent, "_tab_lease", _lease), \
             patch.object(agent, "_dialog_action", return_value={"type": "dialog", "ok": True}), \
             patch.object(agent, "_resolve_tab_target", side_effect=Reached):
            return agent._browser_act_impl(None, "Safari", actions, tab_handle="btab_safari_x")

    def test_twenty_actions_with_expect_checks_reach_the_page(self) -> None:
        actions = [{"type": "click", "element_id": f"e_{i}", "expect": {"text": f"done {i}"}} for i in range(20)]
        with self.assertRaises(Reached):
            self.act(actions)

    def test_after_leading_dialogs_the_rest_still_counts_only_the_callers_actions(self) -> None:
        actions = [{"type": "dialog", "decision": "accept"}]
        actions += [{"type": "click", "element_id": f"e_{i}", "expect": {"gone": "Saving"}} for i in range(19)]
        with self.assertRaises(Reached):
            self.act(actions)

    def test_more_than_twenty_actions_are_refused_before_anything_runs(self) -> None:
        with self.assertRaises(HTTPException) as refused:
            self.act([{"type": "click", "element_id": f"e_{i}"} for i in range(21)])
        self.assertEqual(400, refused.exception.status_code)
        self.assertTrue(exception_not_executed(refused.exception))

    def test_a_caller_cannot_pass_its_own_actions_off_as_expect_checks(self) -> None:
        disguised = [{"type": "wait", "for": "text", "text": "x", "_expect_for": 0, "_expect": {"text": "x"}}] * 21
        with self.assertRaises(HTTPException) as refused:
            self.act(disguised)
        self.assertEqual(400, refused.exception.status_code)
        # Within the limit their bookkeeping keys are dropped, so they are ordinary waits.
        seen = []
        with patch.object(agent, "_browser_act_locked", side_effect=lambda **kw: seen.extend(kw["actions"]) or {}):
            with patch.object(agent, "_resolve_tab_target", side_effect=Reached):
                self.act(disguised[:3])
        self.assertEqual([], [item for item in seen if any(str(k).startswith("_") for k in item)])


if __name__ == "__main__":
    unittest.main()
