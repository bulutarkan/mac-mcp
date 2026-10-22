"""Asking the owner a question changes nothing on the Mac, so every agent may ask it.

ask_user and ask_confirmation are marked read-only for MCP clients (no client-side
approval before a question) and are allowed for read-only and browser-only delegated agents.
The voice tools record audio and stay out of read-only profiles.
"""
import asyncio
import unittest

from mcp_server import tools_agents
from mcp_server.policy import annotations_for_tool, declared_risk, evaluate_profile

ASK_TOOLS = ("ask_user", "ask_confirmation")


class AskToolsPolicyTests(unittest.TestCase):
    def test_ask_tools_are_read_only_for_clients(self):
        for tool in ASK_TOOLS:
            with self.subTest(tool=tool):
                hints = annotations_for_tool(tool)
                self.assertTrue(hints.readOnlyHint)
                self.assertFalse(hints.destructiveHint)
                self.assertFalse(hints.openWorldHint)

    def test_read_only_and_browser_only_agents_may_ask(self):
        for profile in ("read_only", "browser_only", "developer", "standard", "trusted"):
            for tool in ASK_TOOLS:
                with self.subTest(profile=profile, tool=tool):
                    self.assertTrue(evaluate_profile(profile, declared_risk(tool)).allowed)

    def test_voice_tools_stay_out_of_read_only(self):
        for tool in ("ask_user_voice", "voice_capture"):
            with self.subTest(tool=tool):
                self.assertFalse(evaluate_profile("read_only", declared_risk(tool)).allowed)
                self.assertFalse(annotations_for_tool(tool).readOnlyHint)

    def test_scoped_agent_presets_include_the_ask_tools_family(self):
        self.assertEqual(declared_risk("ask_user").family, "interactive")
        for name in ("browser_only", "developer"):
            with self.subTest(preset=name):
                self.assertIn("interactive", tools_agents._AGENT_CAPABILITY_PROFILES[name]["tool_families"])
        self.assertNotIn("voice", tools_agents._AGENT_CAPABILITY_PROFILES["browser_only"]["tool_families"])


class AskToolsCatalogTests(unittest.TestCase):
    def test_ask_choice_is_gone_and_ask_user_stays(self):
        # ask_user's one-click suggestions cover what ask_choice did, and the owner can still type an answer.
        from mcp.server.fastmcp import FastMCP
        from tests.test_core_catalog_descriptions import _build_mcp

        names = {tool.name for tool in asyncio.run(FastMCP.list_tools(_build_mcp()))}
        self.assertIn("ask_user", names)
        self.assertIn("ask_confirmation", names)
        self.assertNotIn("ask_choice", names)


if __name__ == "__main__":
    unittest.main()
