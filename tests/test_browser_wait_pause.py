"""A browser_act wait with only a time is a plain pause, not a 10 s wait for a missing selector."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings


class WaitPauseTests(unittest.TestCase):
    def run_wait(self, action: dict) -> tuple[dict, float]:
        started = time.perf_counter()
        with patch.object(agent, "_run_json_js", side_effect=AssertionError("a pause never runs page script")):
            result = agent._wait_action(load_settings(), "Google Chrome", action, 1, None, "https://example.test/")
        return result, time.perf_counter() - started

    def test_wait_s_alone_pauses_for_that_long(self) -> None:
        result, took = self.run_wait({"type": "wait", "wait_s": 0.3})
        self.assertTrue(result["ok"])
        self.assertEqual("time", result["for"])
        self.assertTrue(result["matched"])
        self.assertGreaterEqual(took, 0.28)
        self.assertLess(took, 1.5)

    def test_seconds_is_accepted_and_the_pause_is_capped(self) -> None:
        with patch.object(agent.time, "sleep") as sleep:
            result, _ = self.run_wait({"type": "wait", "seconds": 600})
        self.assertEqual("time", result["for"])
        sleep.assert_called_once_with(agent._MAX_PAUSE_S)

    def test_a_wait_with_nothing_named_is_a_short_pause(self) -> None:
        with patch.object(agent.time, "sleep") as sleep:
            result, _ = self.run_wait({"type": "wait"})
        self.assertEqual("time", result["for"])
        sleep.assert_called_once_with(1.0)

    def test_a_wait_for_something_still_waits_for_it(self) -> None:
        with patch.object(agent, "_run_json_js", return_value={"ok": True, "matched": True}) as run:
            agent._wait_action(load_settings(), "Google Chrome", {"type": "wait", "selector": "#done"}, 1, None, "https://x.test/")
            agent._wait_action(load_settings(), "Google Chrome", {"type": "wait", "for": "text", "text": "Saved", "wait_s": 2},
                               1, None, "https://x.test/")
        self.assertEqual(2, run.call_count)


if __name__ == "__main__":
    unittest.main()
