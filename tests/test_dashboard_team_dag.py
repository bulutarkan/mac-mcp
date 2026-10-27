from __future__ import annotations

import json
import unittest

from mcp_server import tools_agents as agents
from tests.test_agent_team_outcome import TeamFixture


class DashboardTeamDagTests(TeamFixture, unittest.TestCase):
    def test_team_summary_lists_every_task_without_prompts_or_paths(self) -> None:
        team_id = "team_dag"
        a = self.agent("agt_a", "completed", team_id=team_id, task_id="plan")
        b = self.agent("agt_b", "running", team_id=team_id, task_id="build")
        self.team(team_id, [
            {"id": "plan", "title": "Plan the work " + "x" * 200, "state": "completed", "agent_ids": [a],
             "latest_agent_id": a, "prompt": "SECRET PROMPT", "cwd": "/Users/someone/private"},
            {"id": "build", "title": "Build", "state": "running", "depends_on": ["plan"], "agent_ids": [b],
             "active_agent_id": b, "latest_agent_id": b},
            {"id": "review", "title": "Review", "state": "queued", "depends_on": ["build"], "review_of": "build",
             "queued_reason": "global_capacity", "agent_ids": [], "gate_result": {"decision": "PASS", "feedback": "secret"}},
        ], [a, b])
        summary = agents.dashboard_team_summary(team_id)
        tasks = {task["id"]: task for task in summary["tasks"]}
        self.assertEqual(["plan", "build", "review"], [task["id"] for task in summary["tasks"]])
        self.assertEqual(["plan"], tasks["build"]["depends_on"])
        self.assertTrue(tasks["build"]["active"])
        self.assertEqual("agt_b", tasks["build"]["agent_id"])
        self.assertEqual("global_capacity", tasks["review"]["queued_reason"])
        self.assertEqual("pass", tasks["review"]["gate_result"])
        self.assertEqual(96, len(tasks["plan"]["title"]))
        text = json.dumps(summary)
        for leaked in ("SECRET PROMPT", "/Users/someone", "secret"):
            self.assertNotIn(leaked, text)
        self.assertIn("summary_at", summary)
        self.assertIn("total_tokens_used", summary["budget"])

    def test_each_task_names_who_runs_it_even_before_it_starts(self) -> None:
        # The menu's team card shows a waiting member with its provider, model and reasoning.
        team_id = "team_who"
        a = self.agent("agt_atlas", "running", team_id=team_id, task_id="count")
        self.team(team_id, [
            {"id": "count", "title": "Atlas", "state": "running", "agent_ids": [a], "active_agent_id": a,
             "latest_agent_id": a, "provider": "codex", "model": "gpt-6.1-sol", "reasoning": "high"},
            {"id": "check", "title": "Echo", "role": "reviewer", "state": "blocked", "depends_on": ["count"],
             "review_of": "count", "agent_ids": [], "provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
            {"id": "plain", "title": "Plain", "state": "blocked", "agent_ids": []},
        ], [a])
        tasks = {task["id"]: task for task in agents.dashboard_team_summary(team_id)["tasks"]}
        self.assertEqual(("codex", "gpt-6.1-sol", "high"),
                         (tasks["count"]["provider"], tasks["count"]["model"], tasks["count"]["reasoning"]))
        self.assertEqual(("codex", "gpt-6-luna", "max"),
                         (tasks["check"]["provider"], tasks["check"]["model"], tasks["check"]["reasoning"]))
        self.assertIsNone(tasks["check"]["agent_id"])
        # A task without its own choice runs with the team's.
        self.assertEqual("opencode", tasks["plain"]["provider"])


if __name__ == "__main__":
    unittest.main()
