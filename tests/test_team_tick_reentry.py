"""#90: a team tick that calls itself for the same team starts each task once.

Reading a cancelled agent settles its cancellation, and settling ticks the agent's team. Inside a tick of that
team, the inner tick started the free task and wrote the team; the outer tick, working on its own copy, started
it again and wrote over the first agent, leaving it running outside the team.
"""
from __future__ import annotations

import unittest

from mcp_server import tools_agents as agents
from tests.test_team_task_providers import TeamTaskProviderTests


class TeamTickReentryTests(TeamTaskProviderTests):
    def cancel_unsettled(self, agent_id: str) -> None:
        agents._update_meta(agent_id, lambda meta: meta.update(
            status="cancelled", cancellation={"state": "stopping", "requested_at": 0, "requested_by": "user"},
        ))

    def test_the_freed_slot_starts_the_next_task_once(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}, {"id": "c", "prompt": "y"}], max_parallel=1)["team_id"]
        self.cancel_unsettled("agt_1")
        team = agents._team_tick(team_id)
        self.assertEqual(["a", "c"], [row["task"] for row in self.spawned])
        states = {task["id"]: (task["state"], task.get("agent_ids")) for task in team["tasks"]}
        self.assertEqual({"a": ("cancelled", ["agt_1"]), "c": ("running", ["agt_2"])}, states)
        self.assertEqual("confirmed", agents._read_meta("agt_1")["cancellation"]["state"])

    def test_the_work_the_inner_tick_was_asked_for_still_happens(self) -> None:
        # The inner call only marks the team; the outer tick runs once more and finds what it would have done.
        team_id = self.spawn([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}, {"id": "c", "prompt": "z"}],
                             max_parallel=1)["team_id"]
        self.cancel_unsettled("agt_1")
        agents._team_tick(team_id)
        self.finish = lambda agent_id: agents._update_meta(agent_id, lambda meta: meta.update(status="completed"))
        self.finish("agt_2")
        agents._team_tick(team_id)
        self.assertEqual(["a", "b", "c"], [row["task"] for row in self.spawned])
        self.assertFalse(getattr(agents._TICKING, "teams", {}))


if __name__ == "__main__":
    unittest.main()
