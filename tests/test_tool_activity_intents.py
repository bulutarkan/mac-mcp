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
from mcp_server.runtime_settings import tool_activity_setting
from mcp_server.steering import SteeringIdentity


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
                    intent_descriptions_provider=lambda: bool(
                        tool_activity_setting("require_descriptions", False)
                    ),
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
                    intent_descriptions_provider=lambda: bool(
                        tool_activity_setting("require_descriptions", False)
                    ),
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
                    intent_descriptions_provider=lambda: bool(
                        tool_activity_setting("require_descriptions", False)
                    ),
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

    def test_call_started_publishes_stable_session_identity(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                settings = root / "settings.json"
                self._write_settings(settings, enabled=True)
                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
                mcp = ObservedFastMCP(
                    name="intent-session-test",
                    telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="trusted"),
                    intent_descriptions_provider=lambda: bool(
                        tool_activity_setting("require_descriptions", False)
                    ),
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    return {"ok": True, "command": command}

                queue = telemetry.subscribe()
                try:
                    with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False), \
                         patch.object(mcp, "get_context", return_value=object()), \
                         patch(
                             "mcp_server.observability.steering_identity_from_context",
                             return_value=SteeringIdentity(key="client-a", source="client_id"),
                         ):
                        await mcp.call_tool(
                            "run_command",
                            {"command": "printf one", "description": "Checking first client"},
                        )
                        started_one = await asyncio.wait_for(queue.get(), timeout=1)
                        finished_one = await asyncio.wait_for(queue.get(), timeout=1)

                        await mcp.call_tool(
                            "run_command",
                            {"command": "printf two", "description": "Checking same client"},
                        )
                        started_two = await asyncio.wait_for(queue.get(), timeout=1)
                        finished_two = await asyncio.wait_for(queue.get(), timeout=1)

                    with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=False), \
                         patch.object(mcp, "get_context", return_value=object()), \
                         patch(
                             "mcp_server.observability.steering_identity_from_context",
                             return_value=SteeringIdentity(key="client-b", source="client_id"),
                         ):
                        await mcp.call_tool(
                            "run_command",
                            {"command": "printf three", "description": "Checking second client"},
                        )
                        started_three = await asyncio.wait_for(queue.get(), timeout=1)
                        finished_three = await asyncio.wait_for(queue.get(), timeout=1)

                    self.assertEqual("call_started", started_one["kind"])
                    self.assertEqual("call_finished", finished_one["kind"])
                    self.assertEqual(started_one["session_id"], finished_one["session_id"])
                    self.assertEqual(started_one["session_id"], started_two["session_id"])
                    self.assertEqual(started_two["session_id"], finished_two["session_id"])
                    self.assertNotEqual(started_one["session_id"], started_three["session_id"])
                    self.assertEqual(started_three["session_id"], finished_three["session_id"])
                    self.assertTrue(str(started_one["session_id"]).startswith("sess_"))
                    self.assertTrue(str(started_three["session_id"]).startswith("sess_"))
                finally:
                    telemetry.unsubscribe(queue)

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
                    intent_descriptions_provider=lambda: bool(
                        tool_activity_setting("require_descriptions", False)
                    ),
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
