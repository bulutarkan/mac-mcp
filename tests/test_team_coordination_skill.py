"""#77: the team coordination guide ships with Mac MCP and every team task points to it first.

The skill is found through skill_get by any agent that has the Mac MCP tools, so nothing is written into a
provider's own skill folder. An owner's skill with the same name wins over it.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_agents as agents
from mcp_server import tools_skills as skills
from mcp_server import tools_team_messages

SKILL = skills.BUNDLED_SKILLS_DIR / agents.TEAM_COORDINATION_SKILL / "SKILL.md"


class BundledSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "MAC_MCP_SKILLS_DIR": str(Path(self.temp.name) / "skills"), "MAC_MCP_CLI_SKILLS_DIRS": "",
            "MAC_MCP_EMBEDDING": "feature_hash", "MAC_MCP_EMBEDDING_IDLE_SECONDS": "0",
            "MAC_MCP_EMBEDDING_MODEL_CACHE": str(Path(self.temp.name) / "cache"), "MAC_MCP_BUNDLED_SKILLS": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_the_guide_is_a_valid_skill_found_by_skill_get(self) -> None:
        parsed = skills._parse_skill(SKILL)
        self.assertEqual(agents.TEAM_COORDINATION_SKILL, parsed["name"])
        self.assertEqual([], parsed["diagnostics"])
        found = skills.skill_get(name=agents.TEAM_COORDINATION_SKILL)
        self.assertIn("team_messages", found["content"] if "content" in found else str(found))

    def test_an_owner_skill_with_the_same_name_wins(self) -> None:
        own = Path(os.environ["MAC_MCP_SKILLS_DIR"]) / agents.TEAM_COORDINATION_SKILL
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text(
            f"---\nname: {agents.TEAM_COORDINATION_SKILL}\ndescription: the owner's own version\n---\n\nOwner text.\n",
            encoding="utf-8")
        found = skills.skill_get(name=agents.TEAM_COORDINATION_SKILL)
        self.assertIn("Owner text.", str(found))
        self.assertNotIn("Coordinating in a Mac MCP team", str(found))

    def test_isolated_skill_state_does_not_see_it_unless_asked(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_BUNDLED_SKILLS": ""}):
            self.assertEqual([], skills._bundled_skill_paths())
        self.assertEqual([SKILL.resolve()], skills._bundled_skill_paths())


class GuideMatchesTheToolTests(unittest.TestCase):
    def test_actions_and_fields_it_names_exist(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for action in re.findall(r'action="([a-z_]+)"', text):
            self.assertIn(action, tools_team_messages.DESCRIPTION, action)
        for field in ("dedupe_key", "reply_to", "has_more", "kind"):
            self.assertIn(field, text)
        self.assertIn("has_more", tools_team_messages.DESCRIPTION)
        for kind in ("update", "question", "blocker", "reply"):
            self.assertIn(f"`{kind}`", text)


class TeamTaskPreambleTests(unittest.TestCase):
    def test_a_team_task_starts_with_the_pointer_and_keeps_the_rules(self) -> None:
        task = {"id": "api", "title": "API", "prompt": "Build the API."}
        team = {"team_id": "team_1", "messaging": {"enabled": True, "lead": True}, "tasks": [task, {"id": "ui"}]}
        prompt = agents._team_task_prompt(team, task, {"api": task})
        self.assertTrue(prompt.startswith("You are task api of a Mac MCP team."), prompt[:120])
        self.assertIn(f'skill_get (name="{agents.TEAM_COORDINATION_SKILL}")', prompt.splitlines()[0])
        self.assertLess(prompt.index("skill_get"), prompt.index("Build the API."))
        self.assertIn("Team messaging (the Mac MCP tool team_messages)", prompt)  # the inline rules stay

    def test_no_pointer_when_messaging_is_off(self) -> None:
        task = {"id": "api", "prompt": "Build the API."}
        prompt = agents._team_task_prompt({"team_id": "t", "messaging": {"enabled": False}, "tasks": [task]}, task, {"api": task})
        self.assertNotIn(agents.TEAM_COORDINATION_SKILL, prompt)
        self.assertTrue(prompt.startswith("Build the API."))


if __name__ == "__main__":
    unittest.main()
