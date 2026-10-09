from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from mcp_server import tools_agents as agents
from tests.test_agent_team_outcome import TeamFixture


class WaitRebuildTests(TeamFixture, unittest.TestCase):
    def wait(self, agent_id: str, timeout_s: int) -> tuple[dict, list]:
        calls: list[float] = []
        real = agents.get_agent

        def counted(settings, target, **kwargs):
            calls.append(time.monotonic())
            return real(settings, target, **kwargs)

        with patch.object(agents, "get_agent", side_effect=counted):
            result = agents.wait_agents(None, agent_ids=[agent_id], mode="all", timeout_s=timeout_s)
        return result, calls

    def test_an_unchanged_wait_does_not_rebuild_state(self) -> None:
        agent_id = self.agent("agt_quiet", "running")
        result, calls = self.wait(agent_id, 1)
        self.assertTrue(result["timed_out"])
        self.assertLessEqual(len(calls), 2, "only the first read (and the deadline read) rebuild state")
        self.assertGreaterEqual(result["progress"]["checks"], 3)
        self.assertEqual({"running": 1}, result["progress"]["states"])

    def test_a_written_change_is_seen_on_the_next_check(self) -> None:
        agent_id = self.agent("agt_finishing", "running")

        def finish() -> None:
            time.sleep(0.4)
            agents._update_meta(agent_id, lambda meta: meta.update(status="completed", ended_at=time.time()))

        threading.Thread(target=finish).start()
        started = time.monotonic()
        result, calls = self.wait(agent_id, 10)
        self.assertTrue(result["condition_met"])
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertLessEqual(len(calls), 3)

    def test_a_change_without_a_write_is_caught_by_the_periodic_refresh(self) -> None:
        agent_id = self.agent("agt_silent", "running")
        real = agents.get_agent
        reads = {"n": 0}

        def silent_death(settings, target, **kwargs):
            reads["n"] += 1
            state = real(settings, target, **kwargs)
            return {**state, "status": "failed"} if reads["n"] > 1 else state

        with patch.object(agents, "get_agent", side_effect=silent_death):
            started = time.monotonic()
            result = agents.wait_agents(None, agent_ids=[agent_id], mode="all", timeout_s=10)
        self.assertTrue(result["condition_met"])
        self.assertLess(time.monotonic() - started, agents._WAIT_FULL_REFRESH_S + 1.0)


if __name__ == "__main__":
    unittest.main()
