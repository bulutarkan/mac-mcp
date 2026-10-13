from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.observability import ObservedFastMCP, TelemetryManager, client_display_name


class ClientLabelTests(unittest.TestCase):
    def test_client_names_people_know(self) -> None:
        self.assertEqual("ChatGPT", client_display_name("openai-mcp"))
        self.assertEqual("ChatGPT", client_display_name("openai-mcp (codex)"))
        self.assertEqual("Claude Code", client_display_name("claude-code"))
        self.assertEqual("Codex", client_display_name("Codex"))
        self.assertEqual("Some Tool", client_display_name("Some<Tool>"[:4] + " Tool"))
        self.assertIsNone(client_display_name("  "))

    def test_live_tool_event_carries_the_app_but_storage_does_not(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "t.sqlite3", max_events=50)
                mcp = ObservedFastMCP(name="client-label-test", telemetry=telemetry)
                seen = []
                original = telemetry._publish
                telemetry._publish = lambda event: (seen.append(dict(event)), original(event))

                @mcp.tool(name="get_volume")
                def get_volume() -> dict:
                    return {"ok": True}

                with patch.object(ObservedFastMCP, "client_label", lambda self, context=None: "Claude Code"):
                    await mcp.call_tool("get_volume", {})
                started = [event for event in seen if event.get("kind") == "call_started"]
                self.assertEqual("Claude Code", started[0]["client"])
                telemetry.wait_events_idle()
                stored = telemetry.query_events(limit=1)[0]
                self.assertNotIn("client", stored)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
