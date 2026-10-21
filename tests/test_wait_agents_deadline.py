"""#99: wait_agents returns within its timeout even when the team scheduler is slow.

The first team tick ran before the deadline was set, and every refresh ticked again synchronously;
a tick that starts tasks (worktree preparation) or waits for another tick's team lock held a
timeout_s=1 wait for as long as that took.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from mcp_server import tools_agents as agents
from mcp_server.security import load_settings
from tests.test_team_task_providers import TeamTaskProviderTests


class WaitAgentsDeadlineTests(TeamTaskProviderTests):
    def setUp(self) -> None:
        super().setUp()
        # Background ticks must end before the temporary folders go.
        self.addCleanup(lambda: [run["thread"].join(10) for run in list(agents._WAIT_TICKS.values())])

    def test_a_slow_scheduler_never_holds_the_wait_past_its_timeout(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        real = agents._team_tick

        def slow(target):
            time.sleep(2.5)
            return real(target)

        with patch.object(agents, "_team_tick", side_effect=slow):
            started = time.monotonic()
            result = agents.wait_agents(load_settings(), team_id=team_id, timeout_s=1)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.8, "the wait outlived its timeout")
            self.assertTrue(result["timed_out"])
            self.assertEqual(["agt_1"], [row["agent_id"] for row in result["agents"]])
            # One background tick per team at a time, however many refreshes asked for one.
            self.assertEqual(1, sum(1 for run in agents._WAIT_TICKS.values() if run["thread"].is_alive()))

    def test_a_quick_tick_is_still_waited_for_and_its_work_seen(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y", "depends_on": ["a"]}])["team_id"]
        agents._update_meta("agt_1", lambda meta: meta.update(status="completed", ended_at=time.time()))
        result = agents.wait_agents(load_settings(), team_id=team_id, timeout_s=0)
        # The tick that started b finished within the short grace, so b's agent is in the answer.
        self.assertEqual(["a", "b"], [row["task"] for row in self.spawned])
        self.assertEqual(["agt_1", "agt_2"], sorted(row["agent_id"] for row in result["agents"]))

    def test_an_error_in_a_tick_that_finished_in_time_reaches_the_waiter(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        with patch.object(agents, "_team_tick", side_effect=RuntimeError("scheduler broke")):
            with self.assertRaisesRegex(RuntimeError, "scheduler broke"):
                agents.wait_agents(load_settings(), team_id=team_id, timeout_s=1)


if __name__ == "__main__":
    unittest.main()
