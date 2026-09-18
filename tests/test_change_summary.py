from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.change_summary import build_change_summary
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import resolve_risk
from mcp_server.security import load_settings

TOKEN = "changes-dashboard-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class ChangeSummaryTests(unittest.TestCase):
    def _record(self, manager: TelemetryManager, tool: str, args: dict, result: dict, *,
                session_id: str = "sess_change", agent_id: str | None = "agt_change",
                team_id: str | None = "team_change") -> str:
        declared, effective = resolve_risk(tool, args)
        event_id = manager.start_call(
            "mcp", tool, args,
            metadata={
                "declared_risk": declared.to_dict(),
                "effective_risk": effective.to_dict(),
                "session_id": session_id,
                "agent_id": agent_id,
                "team_id": team_id,
                "actor": f"agent:{agent_id}" if agent_id else "authenticated",
            },
        )
        manager.finish_call(event_id, result=result)
        return event_id

    def test_controlled_workflow_matches_expected_changes_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
            self._record(
                manager, "write_file",
                {"path": "/tmp/demo.txt", "content": "PASSWORD=hunter2"},
                {"ok": True, "path": "/tmp/demo.txt", "bytes": 16},
            )
            self._record(
                manager, "browser_open_url",
                {"browser": "Safari", "url": "https://example.com/private/path?token=secret", "new_tab": True},
                {"ok": True, "browser": "Safari", "url": "https://example.com/private/path?token=secret", "tab_handle": "tab_change"},
            )
            self._record(
                manager, "run_command",
                {"command": "echo TOP_SECRET_COMMAND", "cwd": "/tmp"},
                {"ok": True, "stdout": "TOP_SECRET_COMMAND"},
            )
            self._record(
                manager, "http_request",
                {"method": "POST", "url": "https://api.example.com/send?token=secret", "body": "VERY_SECRET_BODY"},
                {"ok": True, "status_code": 200},
            )
            # Read-only work is intentionally absent from What Changed.
            self._record(
                manager, "read_file", {"path": "/tmp/demo.txt"},
                {"ok": True, "path": "/tmp/demo.txt", "content": "read-only"},
            )

            summary = manager.change_summary(session_id="sess_change")
            self.assertEqual({"commands": 1, "external": 1, "files": 1, "tabs": 1}, summary["counts"])
            self.assertEqual(4, summary["change_count"])
            rendered = json.dumps(summary, ensure_ascii=False)
            for secret in (
                "hunter2", "TOP_SECRET_COMMAND", "VERY_SECRET_BODY", "token=secret", "/private/path",
            ):
                self.assertNotIn(secret, rendered)
            self.assertIn("https://example.com", rendered)
            self.assertIn("https://api.example.com", rendered)
            self.assertIn("/tmp/demo.txt", rendered)
            self.assertIn("Ran command", rendered)

    def test_read_only_browser_calls_are_not_changes(self) -> None:
        declared, effective = resolve_risk("browser_list_tabs", {"browser": "Safari"})
        event = {
            "event_id": "evt_read_browser", "timestamp": 1.0, "tool": "browser_list_tabs", "status": "success",
            "arguments": {"browser": "Safari"}, "result": {"ok": True, "tabs": []},
            "effective_risk": effective.to_dict(), "declared_risk": declared.to_dict(),
        }
        self.assertEqual(0, build_change_summary([event])["change_count"])

    def test_failed_side_effect_is_not_reported_as_change(self) -> None:
        event = {
            "event_id": "evt_failed", "timestamp": 1.0, "tool": "write_file", "status": "error",
            "arguments": {"path": "/tmp/nope"}, "result": None,
            "effective_risk": {"family": "files", "capabilities": ["local_write"], "destructive": True},
        }
        summary = build_change_summary([event])
        self.assertEqual(0, summary["change_count"])
        self.assertEqual([], summary["items"])

    def test_session_agent_and_team_filters_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
            self._record(manager, "write_file", {"path": "/tmp/a", "content": "a"}, {"ok": True, "path": "/tmp/a"}, session_id="s1", agent_id="a1", team_id="t1")
            self._record(manager, "write_file", {"path": "/tmp/b", "content": "b"}, {"ok": True, "path": "/tmp/b"}, session_id="s2", agent_id="a2", team_id="t1")
            self.assertEqual(1, manager.change_summary(session_id="s1")["change_count"])
            self.assertEqual("/tmp/a", manager.change_summary(agent_id="a1")["items"][0]["target"])
            self.assertEqual(2, manager.change_summary(team_id="t1")["change_count"])
            sets = manager.recent_change_sets(limit=5)
            self.assertEqual(2, len(sets))

    def test_existing_database_schema_adds_session_linkage_column(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100)
            import sqlite3
            con = sqlite3.connect(db)
            con.execute("DROP INDEX IF EXISTS idx_tool_events_session_time")
            con.execute("ALTER TABLE tool_events DROP COLUMN session_id")
            con.commit(); con.close()

            reopened = TelemetryManager(db_path=db, max_events=100)
            con = sqlite3.connect(db)
            columns = {row[1] for row in con.execute("PRAGMA table_info(tool_events)").fetchall()}
            indexes = {row[1] for row in con.execute("PRAGMA index_list(tool_events)").fetchall()}
            con.close()
            self.assertIn("session_id", columns)
            self.assertIn("idx_tool_events_session_time", indexes)
            event_id = reopened.start_call("mcp", "get_volume", {}, metadata={"session_id": "sess_migrated"})
            reopened.finish_call(event_id, result={"ok": True})
            self.assertEqual("sess_migrated", reopened.query_events(limit=1)[0]["session_id"])

    def test_dashboard_assets_include_what_changed_card_and_refresh_hooks(self) -> None:
        root = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "dashboard.js").read_text(encoding="utf-8")
        css = (root / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn("What Changed on My Mac", html)
        self.assertIn('id="changeList"', html)
        self.assertIn("/dashboard/api/changes", js)
        self.assertIn("renderChanges", js)
        self.assertIn("scheduleChangesRefresh", js)
        self.assertIn(".changes-card", css)

    def test_observed_tool_call_persists_public_session_id(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
                mcp = ObservedFastMCP(name="changes-session-test", telemetry=telemetry)

                @mcp.tool(name="get_volume")
                def get_volume() -> dict:
                    return {"ok": True, "volume": 55}

                await mcp.call_tool("get_volume", {})
                event = telemetry.query_events(limit=1)[0]
                self.assertEqual("actor:global", event["session_id"])
        asyncio.run(run())


class ChangeSummaryRouteTests(unittest.TestCase):
    def test_changes_route_supports_recent_sets_and_session_drilldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
            declared, effective = resolve_risk("write_file", {"path": "/tmp/route", "content": "secret"})
            event_id = manager.start_call(
                "mcp", "write_file", {"path": "/tmp/route", "content": "secret"},
                metadata={
                    "declared_risk": declared.to_dict(), "effective_risk": effective.to_dict(),
                    "session_id": "sess_route", "agent_id": "agt_route", "team_id": "team_route",
                },
            )
            manager.finish_call(event_id, result={"ok": True, "path": "/tmp/route"})
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), TOKEN))
            client = TestClient(app)
            self.assertEqual(401, client.get("/dashboard/api/changes").status_code)
            recent = client.get("/dashboard/api/changes?hours=1", headers=AUTH)
            self.assertEqual(200, recent.status_code)
            self.assertEqual(1, len(recent.json()["change_sets"]))
            drill = client.get("/dashboard/api/changes?session_id=sess_route", headers=AUTH)
            self.assertEqual(200, drill.status_code)
            payload = drill.json()
            self.assertEqual("sess_route", payload["identity"]["session_id"])
            self.assertEqual("/tmp/route", payload["items"][0]["target"])


if __name__ == "__main__":
    unittest.main()
