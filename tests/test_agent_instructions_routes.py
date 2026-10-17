"""The server instructions every AI app gets on connecting name the right tool for each kind of job,
including the ones added after the browser guidance (knowledge folders, memory, lessons, skills,
recipes, scheduled tasks, delegation, the owner's own MCP servers) and stay compact."""
from __future__ import annotations

import unittest

from mcp_server.main import MCP_AGENT_INSTRUCTIONS
from mcp_server.policy import RISK_REGISTRY


class InstructionRouteTests(unittest.TestCase):
    def test_each_kind_of_job_has_its_tool(self) -> None:
        for phrase in (
            "-> knowledge_search", "Mac MCP Settings > Privacy", "It limits nothing else",
            "-> mac_app adapters before UI automation", "-> memory_search before asking the user", "never secrets",
            "-> lesson_search", "skill_search and skill_get", "computer_plan(save_as_recipe=...)",
            "recipe(action='run')", "-> scheduled_tasks", "wait for the owner's approval",
            "-> spawn_agents, then wait_agents", "-> ask_user", "-> browser_checkpoint", "<server>__<tool>",
            "do not work around it",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, MCP_AGENT_INSTRUCTIONS)

    def test_every_tool_it_names_exists(self) -> None:
        for name in ("knowledge_search", "mac_app", "memory_search", "memory_add", "lesson_search", "lesson_feedback",
                     "skill_search", "skill_get", "computer_plan", "recipe", "scheduled_tasks", "spawn_agents",
                     "wait_agents", "ask_user", "browser_checkpoint", "search_files", "read_file", "tool_discover"):
            with self.subTest(tool=name):
                self.assertIn(name, RISK_REGISTRY)

    def test_it_stays_compact(self) -> None:
        self.assertLess(len(MCP_AGENT_INSTRUCTIONS.split()), 1000)


if __name__ == "__main__":
    unittest.main()
