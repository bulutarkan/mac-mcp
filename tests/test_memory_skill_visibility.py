"""Memory and skills where agents see them (#110, part 1).

skill_search and skill_get are in the default core list; every memory and skill summary says when to use it
and fits the compact limit. Read-only agents may still remember what they learned, and nothing else
opens with it. Saving and recalling are silent: no instruction asks an agent to announce them.
"""
import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import unittest

from mcp_server import tools_agents
from mcp_server.main import MCP_AGENT_INSTRUCTIONS
from mcp_server.policy import declared_risk, evaluate_profile
from mcp_server.tool_summaries import COMPACT_DESCRIPTION_LIMIT, CORE_TOOL_NAMES, CORE_TOOL_SUMMARIES

MEMORY_AND_SKILLS = ("memory_search", "memory_add", "memory_update", "skill_search", "skill_get")


class MemorySkillVisibilityTests(unittest.TestCase):
    def test_skills_are_in_the_core_list_with_short_summaries(self):
        for tool in MEMORY_AND_SKILLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, CORE_TOOL_NAMES)
                self.assertLessEqual(len(CORE_TOOL_SUMMARIES[tool]), COMPACT_DESCRIPTION_LIMIT)

    def test_read_only_agents_may_remember_and_nothing_else_opens(self):
        for tool in ("memory_add", "memory_update", "memory_search", "skill_search", "skill_get"):
            with self.subTest(tool=tool):
                self.assertTrue(evaluate_profile("read_only", declared_risk(tool)).allowed)
        for tool in ("memory_delete", "lesson_record", "write_file", "edit_file", "run_command"):
            with self.subTest(tool=tool):
                self.assertFalse(evaluate_profile("read_only", declared_risk(tool)).allowed)
        # Browser-only agents stay scoped to the browser and to questions for the owner.
        self.assertNotIn("memory", tools_agents._AGENT_CAPABILITY_PROFILES["browser_only"]["tool_families"])

    def test_memory_is_used_silently(self):
        self.assertIn("say you saved or recalled anything; use it naturally", MCP_AGENT_INSTRUCTIONS)
        self.assertNotIn("mention what you remembered", MCP_AGENT_INSTRUCTIONS)
        self.assertLess(len(MCP_AGENT_INSTRUCTIONS.split()), 1100)

    def test_full_descriptions_say_when_and_stay_quiet(self):
        import asyncio

        from mcp.server.fastmcp import FastMCP
        from tests.test_core_catalog_descriptions import _build_mcp

        full = {tool.name: tool.description for tool in asyncio.run(FastMCP.list_tools(_build_mcp()))}
        self.assertIn("do not say you saved it", full["memory_add"])
        self.assertIn("do not say it came from memory", full["memory_search"])
        self.assertIn("do not say you updated it", full["memory_update"])
        self.assertTrue(full["skill_search"].startswith("Find a written procedure before a task"))


if __name__ == "__main__":
    unittest.main()
