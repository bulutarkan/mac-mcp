"""#91: a task waiting to start is checked against its dependencies again before it starts.

Dependencies were only re-evaluated for blocked tasks. A task that was ready, or queued for
admission, kept that state when a dependency went back: with nested review, a FAIL from the
reviewer's reviewer sends the reviewer back for revision, so the reviewed task is no longer
effectively complete, yet the queued dependent task still started on the old decision.
"""
from __future__ import annotations

import unittest

from mcp_server import tools_agents as agents
from tests.test_team_task_providers import TeamTaskProviderTests


class TeamDependencyRecheckTests(TeamTaskProviderTests):
    def team_with_passed_review(self, waiting_state: str) -> str:
        # build -> check (reviews build) -> audit (reviews check); ship depends on build.
        team_id = self.spawn([
            {"id": "build", "prompt": "build"},
            {"id": "check", "prompt": "review build", "review_of": "build"},
            {"id": "audit", "prompt": "review the review", "review_of": "check"},
            {"id": "ship", "prompt": "ship it", "depends_on": ["build"]},
        ], max_parallel=2)["team_id"]
        team = agents._read_team(team_id)
        for task in team["tasks"]:
            task["active_agent_id"] = None
        tasks = {task["id"]: task for task in team["tasks"]}
        tasks["build"]["state"] = "completed"
        # audit failed check, so check is back to blocked for a revision: build is no longer done.
        tasks["check"].update(state="blocked", gate_result="pass", revision_count=1, revision_feedback="redo")
        tasks["audit"].update(state="blocked", gate_result="fail")
        tasks["ship"]["state"] = waiting_state
        agents._write_team(team_id, team)
        self.spawned.clear()
        return team_id

    def states(self, team_id):
        return {task["id"]: task["state"] for task in agents._read_team(team_id)["tasks"]}

    def test_a_queued_task_waits_again_when_its_dependency_goes_back(self) -> None:
        team_id = self.team_with_passed_review("queued")
        agents._team_tick(team_id)
        self.assertNotIn("ship", [row["task"] for row in self.spawned])
        self.assertEqual("blocked", self.states(team_id)["ship"])

    def test_a_ready_task_waits_again_too(self) -> None:
        team_id = self.team_with_passed_review("ready")
        agents._team_tick(team_id)
        self.assertNotIn("ship", [row["task"] for row in self.spawned])
        self.assertEqual("blocked", self.states(team_id)["ship"])

    def test_a_queued_task_whose_dependency_failed_for_good_is_skipped(self) -> None:
        team_id = self.team_with_passed_review("queued")
        team = agents._read_team(team_id)
        for task in team["tasks"]:
            if task["id"] == "check":
                task["state"] = "quality_failed"
        agents._write_team(team_id, team)
        agents._team_tick(team_id)
        self.assertNotIn("ship", [row["task"] for row in self.spawned])
        ship = next(task for task in agents._read_team(team_id)["tasks"] if task["id"] == "ship")
        self.assertEqual(("skipped", "dependency_failed:build"), (ship["state"], ship["failure_reason"]))


if __name__ == "__main__":
    unittest.main()
