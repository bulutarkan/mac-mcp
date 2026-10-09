from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import mcp_server.main as main
from mcp_server import observability
from mcp_server.observability import ObservedFastMCP
from mcp_server.tool_summaries import ADVANCED_BROWSER_TOOLS, COMPACT_DESCRIPTION_LIMIT, CORE_TOOL_SUMMARIES


def _build_mcp() -> ObservedFastMCP:
    captured = []

    class Capturing(ObservedFastMCP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    with patch.object(main, "ObservedFastMCP", Capturing):
        main.create_app()
    return captured[-1]


class CoreCatalogDescriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def _env(self):
        env = {"MAC_MCP_PERMISSION_PROFILE": "trusted"}
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        os.environ.pop("MAC_MCP_TOOL_PROFILE", None)
        self.addCleanup(patcher.stop)

    def test_core_descriptions_are_complete_and_within_the_limit(self) -> None:
        self._env()
        full = {tool.name: tool.description or "" for tool in asyncio.run(self.mcp.list_available_tools(compact=False))}
        compact = {tool.name: tool.description or "" for tool in asyncio.run(self.mcp.list_tools())}
        self.assertTrue(compact)
        for name, description in compact.items():
            with self.subTest(tool=name):
                self.assertLessEqual(len(description), COMPACT_DESCRIPTION_LIMIT)
                self.assertFalse(description.endswith("..."))
                if len(full[name]) > COMPACT_DESCRIPTION_LIMIT:
                    self.assertEqual(CORE_TOOL_SUMMARIES[name], description)
        for name, summary in CORE_TOOL_SUMMARIES.items():
            with self.subTest(summary=name):
                self.assertLessEqual(len(summary), COMPACT_DESCRIPTION_LIMIT)
                self.assertIn(name, observability._CORE_TOOL_NAMES)

        act = compact["browser_act"]
        for phrase in ("BATCH-FIRST:", "observe once", "one browser_act", "type/select/click/scroll", "observe verify",
                       "Custom dropdowns: select", "query/role/text_match", "+intent", "Split only for dependencies"):
            self.assertIn(phrase, act)
        observe = compact["browser_observe"]
        for phrase in ("BATCH-FIRST HINT", "one browser_act", "verify once", "dependency/rerender", "stale/takeover",
                       "consequential verification"):
            self.assertIn(phrase, observe)
        self.assertIn("fail closed", compact["mac_act"])
        self.assertIn("never submits", compact["browser_upload_artifact"])

    def test_tool_discover_returns_full_description_with_schema(self) -> None:
        self._env()

        async def discover(include_schema: bool) -> dict:
            result = await self.mcp.call_tool("tool_discover", {"query": "browser_act", "limit": 20, "include_schema": include_schema,
                                                             "description": "Check catalog descriptions"})
            blocks = result[0] if isinstance(result, tuple) else result
            payload = json.loads(blocks[0].text)
            return next(item for item in payload["tools"] if item["name"] == "browser_act")

        short = asyncio.run(discover(False))
        self.assertTrue(short["description_truncated"])
        self.assertEqual(CORE_TOOL_SUMMARIES["browser_act"], short["description"])
        full = asyncio.run(discover(True))
        self.assertFalse(full["description_truncated"])
        self.assertIn("Perform up to 20 browser actions", full["description"])
        self.assertIn("input_schema", full)

    def test_every_long_registered_core_tool_has_a_summary(self) -> None:
        from mcp.server.fastmcp import FastMCP

        registered = asyncio.run(FastMCP.list_tools(self.mcp))
        for tool in registered:
            if tool.name in observability._CORE_TOOL_NAMES and len(tool.description or "") > COMPACT_DESCRIPTION_LIMIT:
                with self.subTest(tool=tool.name):
                    self.assertIn(tool.name, CORE_TOOL_SUMMARIES)

    def test_shell_execution_mode_guidance_is_visible(self) -> None:
        for phrase in ("run_command for a short command", "start_background_job", "get_job_output/wait_jobs", "stop_job",
                       "run_commands_parallel only", "tool_discover"):
            self.assertIn(phrase, main.MCP_AGENT_INSTRUCTIONS)
        self.assertIn("start_background_job", CORE_TOOL_SUMMARIES["run_command"])

    def test_browser_routes_agree_in_catalog_and_instructions(self) -> None:
        self._env()
        compact = {tool.name: tool.description or "" for tool in asyncio.run(self.mcp.list_tools())}
        full = {tool.name: tool.description or "" for tool in asyncio.run(self.mcp.list_available_tools(compact=False))}
        # Nothing calls itself the preferred browser route any more; the two routes are distinct.
        for text in (compact["browser_do"], full["browser_do"], main.MCP_AGENT_INSTRUCTIONS):
            self.assertNotIn("Preferred one-call", text)
        self.assertIn("One-shot workflow", compact["browser_do"])
        self.assertIn("observe -> browser_act instead", compact["browser_do"])
        self.assertIn("browser_observe -> batched browser_act", full["browser_do"])
        for phrase in ("interactive work on a page is browser_observe -> one batched browser_act -> verify",
                       "is a single browser_do", "advanced compatibility tools reached through tool_discover"):
            self.assertIn(phrase, main.MCP_AGENT_INSTRUCTIONS)
        # Selector primitives stay registered and callable but out of the default catalog.
        for name in ADVANCED_BROWSER_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, compact)
                self.assertNotIn(name, observability._CORE_TOOL_NAMES)
                self.assertIn(name, full)
                self.assertIn(ADVANCED_BROWSER_TOOLS[name], observability._CORE_TOOL_NAMES)

    def test_tool_discover_labels_selector_tools_as_advanced(self) -> None:
        self._env()
        result = asyncio.run(self.mcp.call_tool("tool_discover", {"query": "selector", "limit": 50,
                                                                   "description": "Check advanced tier"}))
        blocks = result[0] if isinstance(result, tuple) else result
        tools = {item["name"]: item for item in json.loads(blocks[0].text)["tools"]}
        self.assertEqual("advanced_compat", tools["browser_click_selector"]["tier"])
        self.assertEqual("browser_act", tools["browser_click_selector"]["prefer"])
        self.assertEqual("browser_act", tools["browser_type_selector"]["prefer"])
        result = asyncio.run(self.mcp.call_tool("tool_discover", {"query": "browser_act", "limit": 50,
                                                                   "description": "Check core tier"}))
        blocks = result[0] if isinstance(result, tuple) else result
        act = next(item for item in json.loads(blocks[0].text)["tools"] if item["name"] == "browser_act")
        self.assertNotIn("tier", act)

    def test_unsummarized_long_description_is_marked_as_cut(self) -> None:
        clipped = observability._clip_description("word " * 100)
        self.assertLessEqual(len(clipped), COMPACT_DESCRIPTION_LIMIT)
        self.assertTrue(clipped.endswith("[cut; tool_discover has the full text]"))


if __name__ == "__main__":
    unittest.main()
