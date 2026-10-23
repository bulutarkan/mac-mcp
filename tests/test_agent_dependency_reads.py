"""A team task reads the full results of the tasks it depends on or reviews, and nothing else (#106)."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context

LONG_REPORT = "\n".join(f"line {number}: detail {number * 7}" for number in range(1, 2001))


class DependencyReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents, self.old_teams = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        self.by_task: dict[str, str] = {}
        self.prompts: dict[str, str] = {}
        self.patches = [
            patch.object(agents, "provider_enabled", return_value=True),
            patch.object(agents, "_find_binary", return_value="/tmp/fake-opencode"),
            patch.object(agents, "_validate_provider_access_mode", return_value=None),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.old_agents, self.old_teams
        self.temp.cleanup()

    def fake_spawn(self, *, prompt="", team_id=None, team_task_id=None, title=None, parent_agent_id=None, **_kwargs):
        agent_id = f"agt_{team_task_id}_{len(self.by_task) + 1}"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id, "title": title,
            "provider": "opencode", "status": "running", "phase": "running", "started_at": now,
            "spawn_requested_at": now, "last_activity_at": now, "updated_at": now,
            **agents._new_agent_lineage(agent_id, parent_agent_id),
        })
        self.by_task[team_task_id] = agent_id
        self.prompts[team_task_id] = prompt
        return {"ok": True, "agent_id": agent_id, "team_task_id": team_task_id, "status": "running", "title": title}

    def complete(self, task_id: str, text: str) -> None:
        agent_id = self.by_task[task_id]
        (agents.AGENTS_DIR / agent_id / "result.txt").write_text(text, encoding="utf-8")
        agents._update_meta(agent_id, lambda meta: meta.update(
            {"status": "completed", "phase": "completed", "ended_at": time.time(), "updated_at": time.time()}))

    def as_agent(self, task_id: str):
        agent_id = self.by_task[task_id]
        token = set_policy_context(PolicyContext(profile="trusted", actor=f"agent:{agent_id}", agent_id=agent_id))
        self.addCleanup(reset_policy_context, token)

    def start_team(self) -> str:
        team = agents.spawn_agents(settings=None, provider="opencode", cwd=str(self.root), access_mode="read_only", tasks=[
            {"id": "research", "prompt": "Write the long report."},
            {"id": "other", "prompt": "Unrelated work."},
            {"id": "synthesis", "prompt": "Use the research.", "depends_on": ["research"]},
        ])
        self.complete("research", LONG_REPORT)
        agents._team_tick(team["team_id"])
        self.assertIn("synthesis", self.by_task)
        return team["team_id"]

    def test_dependent_task_reads_the_whole_report_in_pages(self) -> None:
        team_id = self.start_team()
        self.as_agent("synthesis")
        research = self.by_task["research"]
        text, offset = "", 0
        while True:
            page = agents.get_agent(None, research, result_mode="full", result_offset=offset)["result_full"]
            text += page["text"]
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        self.assertEqual(LONG_REPORT, text)
        self.assertIn("line 1997: detail 13979", text)
        reads = agents._read_team(team_id)["dependency_reads"]["synthesis->research"]
        self.assertEqual(("synthesis", "research"), (reads["reader_task"], reads["target_task"]))
        self.assertGreaterEqual(reads["count"], 2)

    def test_a_task_it_does_not_depend_on_stays_closed(self) -> None:
        self.start_team()
        self.as_agent("synthesis")
        with self.assertRaises(HTTPException) as caught:
            agents.get_agent(None, self.by_task["other"], result_mode="full")
        self.assertEqual(403, caught.exception.status_code)
        self.assertEqual("lineage_not_authorized", caught.exception.detail["reason"])

    def test_the_link_does_not_work_backwards(self) -> None:
        self.start_team()
        self.as_agent("research")
        with self.assertRaises(HTTPException):
            agents.get_agent(None, self.by_task["synthesis"], result_mode="full")

    def test_only_reading_is_granted_not_control(self) -> None:
        self.start_team()
        self.as_agent("synthesis")
        with self.assertRaises(HTTPException):
            agents.agent_action(None, action="cancel", agent_id=self.by_task["research"])
        with self.assertRaises(HTTPException):
            agents.authorize_agent_control_request("wait_agents", {"agent_ids": [self.by_task["research"]]})
        agents.authorize_agent_control_request("get_agent", {"agent_id": self.by_task["research"]})

    def test_another_team_cannot_read_by_task_name(self) -> None:
        self.start_team()
        stranger = "agt_stranger"
        (agents.AGENTS_DIR / stranger).mkdir(parents=True)
        agents._write_meta(stranger, {"agent_id": stranger, "team_id": "team_other", "team_task_id": "synthesis",
                                      "status": "running", **agents._new_agent_lineage(stranger, None)})
        token = set_policy_context(PolicyContext(profile="trusted", actor=f"agent:{stranger}", agent_id=stranger))
        self.addCleanup(reset_policy_context, token)
        with self.assertRaises(HTTPException):
            agents.get_agent(None, self.by_task["research"], result_mode="full")

    def test_the_prompt_says_how_to_read_the_full_report(self) -> None:
        self.start_team()
        prompt = self.prompts["synthesis"]
        self.assertIn("Full dependency reports", prompt)
        self.assertIn(f"task research: agent_id={self.by_task['research']}", prompt)
        self.assertIn('result_mode="full"', prompt)
        self.assertIn("tool_invoke", prompt)
        self.assertNotIn("Full dependency reports", self.prompts["other"])

    def test_a_reviewer_reads_the_task_it_reviews(self) -> None:
        team = agents.spawn_agents(settings=None, provider="opencode", cwd=str(self.root), access_mode="read_only", tasks=[
            {"id": "draft", "prompt": "Draft."},
            {"id": "review", "prompt": "Review the draft.", "review_of": "draft"},
        ])
        self.complete("draft", LONG_REPORT)
        agents._team_tick(team["team_id"])
        self.as_agent("review")
        page = agents.get_agent(None, self.by_task["draft"], result_mode="full")["result_full"]
        self.assertTrue(page["text"].startswith("line 1: detail 7"))


if __name__ == "__main__":
    unittest.main()
