from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext


class ToolActivityIntentTests(unittest.TestCase):
    def _write_settings(self, path: Path, *, enabled: bool) -> None:
        path.write_text(
            json.dumps({
                "tool_activity": {
                    "show_bubble": enabled,
                    "require_descriptions": enabled,
                }
            }),
            encoding="utf-8",
        )

    def test_schema_adds_required_description_only_when_enabled(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = root / "settings.json"
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
                mcp = ObservedFastMCP(
                    name="intent-schema-test",
                    telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    return {"ok": True, "command": command}

                with patch.dict(
                    os.environ,
                    {
                        "MAC_MCP_SETTINGS_PATH": str(settings),
                        "MAC_MCP_TOOL_PROFILE": "full",
                    },
                    clear=False,
                ):
                    self._write_settings(settings, enabled=False)
                    disabled = next(
                        tool for tool in await mcp.list_available_tools(compact=False)
                        if tool.name == "run_command"
                    )
                    self.assertNotIn("description", disabled.inputSchema.get("properties", {}))
                    self.assertNotIn("description", disabled.inputSchema.get("required", []))

                    self._write_settings(settings, enabled=True)
                    enabled = next(
                        tool for tool in await mcp.list_available_tools(compact=False)
                        if tool.name == "run_command"
                    )
                    description = enabled.inputSchema["properties"]["description"]
                    self.assertIn("description", enabled.inputSchema["required"])
                    self.assertEqual(3, description["minLength"])
                    self.assertEqual(80, description["maxLength"])

        asyncio.run(run())

    def test_required_description_is_stripped_before_handler_and_kept_in_telemetry(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = root / "settings.json"
                self._write_settings(settings, enabled=True)
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
                seen: list[str] = []
                mcp = ObservedFastMCP(
                    name="intent-call-test",
                    telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    seen.append(command)
                    return {"ok": True}

                with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False):
                    await mcp.call_tool(
                        "run_command",
                        {
                            "command": "printf ok",
                            "description": "Checking Disk Health",
                        },
                    )

                self.assertEqual(["printf ok"], seen)
                event = telemetry.query_events(limit=1)[0]
                self.assertEqual("Checking Disk Health", event["arguments"]["description"])
                self.assertEqual("printf ok", event["arguments"]["command"])

        asyncio.run(run())

    def test_missing_or_sensitive_description_fails_before_execution(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = root / "settings.json"
                self._write_settings(settings, enabled=True)
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
                calls = 0
                mcp = ObservedFastMCP(
                    name="intent-validation-test",
                    telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True}

                with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False):
                    with self.assertRaisesRegex(ToolError, "intent_description_required"):
                        await mcp.call_tool("run_command", {"command": "printf nope"})

                    with self.assertRaisesRegex(ToolError, "intent_description_sensitive"):
                        await mcp.call_tool(
                            "run_command",
                            {
                                "command": "printf nope",
                                "description": "Reading /Users/example/private.txt",
                            },
                        )

                self.assertEqual(0, calls)

        asyncio.run(run())

    def test_disabled_feature_preserves_legacy_call_shape(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = root / "settings.json"
                self._write_settings(settings, enabled=False)
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
                seen: list[tuple[str, str]] = []
                mcp = ObservedFastMCP(
                    name="intent-disabled-test",
                    telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                )

                @mcp.tool(name="run_command")
                def run_command(command: str, description: str = "") -> dict:
                    seen.append((command, description))
                    return {"ok": True}

                with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False):
                    await mcp.call_tool(
                        "run_command",
                        {"command": "printf legacy", "description": "legacy argument"},
                    )

                self.assertEqual([("printf legacy", "legacy argument")], seen)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
