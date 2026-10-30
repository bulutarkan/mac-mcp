"""#391 (b): a page that stops answering during verification is not proof that the action navigated."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import tools_browser_agent as agent


class LostPageTests(unittest.TestCase):
    def moved(self, answer):
        side = answer if isinstance(answer, Exception) else None
        with patch.object(agent, "cancellable_sleep"), \
                patch.object(agent, "_run_json_js", side_effect=side, return_value=None if side else answer):
            return agent._moved_after_lost_page(MagicMock(), "Safari", 1, 1, "tab", "https://a.test/page")

    def test_another_url_afterwards_is_navigation(self) -> None:
        self.assertTrue(self.moved({"url": "https://a.test/next"}))

    def test_the_same_page_answering_again_is_not(self) -> None:
        self.assertFalse(self.moved({"url": "https://a.test/page"}))

    def test_a_page_that_still_cannot_be_read_is_unknown(self) -> None:
        self.assertIsNone(self.moved(HTTPException(504, "timed out")))

    def test_unknown_outcome_is_never_retried_and_never_a_known_failure(self) -> None:
        self.assertTrue(agent._OUTCOME_UNKNOWN["outcome_unknown"])
        self.assertFalse(agent._OUTCOME_UNKNOWN["automatic_retry"])
        self.assertNotIn(agent._OUTCOME_UNKNOWN["error"], agent._KNOWN_FAILURE_ERRORS)

    def test_a_key_press_whose_check_loses_the_page_is_unknown_not_navigation(self) -> None:
        dispatched = {"ok": True, "element_id": "e1", "_verify_url": "https://a.test/page", "_verify_revision": 1,
                      "_verify_state": {}}
        calls = [dispatched, HTTPException(403, "denied"), HTTPException(504, "timed out")]

        def run(*_args, **_kwargs):
            value = calls.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(agent, "_run_json_js", side_effect=run), patch.object(agent, "cancellable_sleep"):
            result = agent._dom_key_action(MagicMock(), "Safari", {"key": "Enter"}, "e1", 1, 1, "tab")
        self.assertEqual("action_outcome_unknown", result["error"])
        self.assertNotEqual("async_navigation", result.get("verification"))


if __name__ == "__main__":
    unittest.main()
