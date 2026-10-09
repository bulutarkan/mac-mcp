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


if __name__ == "__main__":
    unittest.main()
