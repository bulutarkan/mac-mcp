"""#88: after leading dialog answers, every action's index and outcome match the final list.

The dialogs were put in front of the rest's actions after the rest had numbered its unconfirmed
actions, so [dialog, click] said unconfirmed_actions=[0] (the dialog) instead of [1] (the click);
a dialog-only call returned actions without an outcome.
"""
from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent


@contextlib.contextmanager
def _lease(*_args, **_kwargs):
    yield SimpleNamespace(browser="Google Chrome", window_index=1, tab_index=1, tab_handle="t", lease_generation=1)


class LeadingDialogOutcomeTests(unittest.TestCase):
    def act(self, actions, rest=None, dialog=None):
        with patch.object(agent, "_tab_lease", _lease), \
             patch.object(agent, "_require_stable_handle_for_mutation"), \
             patch.object(agent, "_ensure_visual_companion"), \
             patch.object(agent, "_dialog_action", return_value=dialog or {"type": "dialog", "ok": True}), \
             patch.object(agent, "_browser_act_locked", return_value=rest or {"ok": True, "actions": []}):
            return agent.browser_act(None, "Google Chrome", actions, tab_handle="t")

    def test_unconfirmed_indexes_count_the_dialogs_in_front(self) -> None:
        rest = {"ok": True, "actions": [{"type": "click", "ok": True, "outcome": "page_reacted"}]}
        result = self.act([{"type": "dialog", "decision": "accept"}, {"type": "dialog", "decision": "accept"},
                           {"type": "click", "query": "Next"}], rest=rest)
        self.assertEqual(["confirmed", "confirmed", "page_reacted"], [item["outcome"] for item in result["actions"]])
        self.assertEqual([2], result["unconfirmed_actions"])
        self.assertEqual(agent.CONFIRM_HINT, result["confirm_hint"])

    def test_a_confirmed_rest_leaves_no_stale_index(self) -> None:
        rest = {"ok": True, "actions": [{"type": "click", "ok": True, "outcome": "confirmed"}]}
        result = self.act([{"type": "dialog", "decision": "accept"}, {"type": "click", "query": "Next"}], rest=rest)
        self.assertNotIn("unconfirmed_actions", result)
        self.assertNotIn("confirm_hint", result)

    def test_dialog_only_calls_report_each_outcome(self) -> None:
        result = self.act([{"type": "dialog", "decision": "dismiss"}])
        self.assertEqual(["confirmed"], [item["outcome"] for item in result["actions"]])
        failed = self.act([{"type": "dialog", "decision": "accept"}, {"type": "click", "query": "Next"}],
                          dialog={"type": "dialog", "ok": False, "error": "no_dialog_open"})
        self.assertEqual(["failed"], [item["outcome"] for item in failed["actions"]])
        self.assertEqual(1, failed["action_count"])


if __name__ == "__main__":
    unittest.main()
