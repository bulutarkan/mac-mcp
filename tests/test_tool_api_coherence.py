from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from mcp.types import Tool

from mcp_server.main import _invoke_registered_tool, _tool_input_schema
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext
from mcp_server.policy_scope import ResourceScope


class ToolAvailabilityCoherenceTests(unittest.TestCase):
    def test_discovery_reads_fastmcp_tool_input_schema(self) -> None:
        tool = Tool(
            name="read_file",
            description="Read a file",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        )
        schema = _tool_input_schema(tool)
        self.assertEqual(["path"], schema["required"])
        self.assertEqual("string", schema["properties"]["path"]["type"])

    def test_full_catalog_uses_same_profile_filter_as_list_tools(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                context = [PolicyContext(profile="standard")]
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=TelemetryManager(db_path=Path(td) / "telemetry.sqlite3"),
                    policy_context_provider=lambda: context[0],
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    return {"ok": True}

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    return {"ok": True}

                with patch.dict(os.environ, {"MAC_MCP_TOOL_PROFILE": "full"}, clear=False):
                    listed = {tool.name for tool in await mcp.list_tools()}
                    discoverable = {tool.name for tool in await mcp.list_available_tools(compact=False)}

                self.assertNotIn("run_command", listed)
                self.assertNotIn("run_command", discoverable)
                self.assertIn("read_file", discoverable)

                context[0] = PolicyContext(profile="trusted")
                trusted = {tool.name for tool in await mcp.list_available_tools(compact=False)}
                self.assertIn("run_command", trusted)

        asyncio.run(run())

    def test_delegated_scope_hides_other_tool_families_without_leaking_scope(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                context = PolicyContext(
                    profile="trusted",
                    actor="agent:agt_test",
                    agent_id="agt_test",
                    scope=ResourceScope(tool_families=("browser",)),
                )
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=TelemetryManager(db_path=Path(td) / "telemetry.sqlite3"),
                    policy_context_provider=lambda: context,
                )

                @mcp.tool(name="browser_find")
                def browser_find(query: str = "") -> dict:
                    return {"ok": True}

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    return {"ok": True}

                names = {tool.name for tool in await mcp.list_available_tools(compact=False)}
                self.assertIn("browser_find", names)
                self.assertNotIn("read_file", names)

                denied = mcp.effective_tool_availability("read_file")
                self.assertFalse(denied["available"])
                self.assertEqual("scope_denied", denied["reason"])
                self.assertNotIn("scope", denied)
                self.assertNotIn("tool_families", denied)

        asyncio.run(run())


class ToolInvokeCoherenceTests(unittest.TestCase):
    def test_nested_ok_false_is_outer_failure_with_invocation_status(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=TelemetryManager(db_path=Path(td) / "telemetry.sqlite3"),
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="ask_choice")
                def ask_choice(question: str, choices: list[str]) -> dict:
                    return {"ok": False, "error": "invalid_choice"}

                result = await _invoke_registered_tool(
                    mcp,
                    "ask_choice",
                    {"question": "x", "choices": ["a", "b"]},
                )
                self.assertFalse(result["ok"])
                self.assertTrue(result["invocation_ok"])
                self.assertFalse(result["tool_ok"])
                self.assertFalse(result["result"]["ok"])

        asyncio.run(run())

    def test_nested_success_without_ok_is_treated_as_completed(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=TelemetryManager(db_path=Path(td) / "telemetry.sqlite3"),
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    return {"content": "hello"}

                result = await _invoke_registered_tool(mcp, "read_file", {"path": "/tmp/a"})
                self.assertTrue(result["ok"])
                self.assertTrue(result["invocation_ok"])
                self.assertTrue(result["tool_ok"])

        asyncio.run(run())

    def test_registered_tool_exception_matches_direct_call(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=TelemetryManager(db_path=Path(td) / "telemetry.sqlite3"),
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    raise HTTPException(409, "boom")

                with self.assertRaises(Exception) as direct_ctx:
                    await mcp.call_tool("read_file", {"path": "/tmp/a"})
                with self.assertRaises(type(direct_ctx.exception)) as nested_ctx:
                    await _invoke_registered_tool(mcp, "read_file", {"path": "/tmp/a"})
                self.assertEqual(str(direct_ctx.exception), str(nested_ctx.exception))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
