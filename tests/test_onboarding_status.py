from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.observability import TelemetryManager


class FirstClientCallTests(unittest.TestCase):
    """#59: the first call an AI app made marks the "connect" and "first request" steps."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-onboarding-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.telemetry = TelemetryManager(db_path=self.dir / "telemetry.sqlite3")

    def call(self, source: str, tool: str, *, error: str | None = None, agent_id: str | None = None) -> None:
        metadata = {"agent_id": agent_id} if agent_id else None
        event_id = self.telemetry.start_call(source, tool, {}, metadata=metadata)
        self.telemetry.finish_call(event_id, result=None if error else {"ok": True}, error=error)

    def test_nothing_recorded_yet(self) -> None:
        self.assertEqual({"first_call": None, "first_success": None}, self.telemetry.first_client_calls())

    def test_dashboard_and_delegated_agent_calls_do_not_count(self) -> None:
        self.call("dashboard", "file_transaction_undo")
        self.call("mcp", "read_file", agent_id="agt_0123abcd")
        self.assertEqual({"first_call": None, "first_success": None}, self.telemetry.first_client_calls())

    def test_first_call_and_first_success_from_an_ai_app(self) -> None:
        self.call("mcp", "browser_act", error="target not found")
        self.call("rest", "system_info")
        found = self.telemetry.first_client_calls()
        self.assertEqual("browser_act", found["first_call"]["tool"])
        self.assertEqual("mcp", found["first_call"]["source"])
        self.assertEqual("system_info", found["first_success"]["tool"])
        self.assertEqual({"at", "tool", "source", "status"}, set(found["first_call"]))


class ClientCheckinTests(unittest.TestCase):
    """#126: an AI app that checks in (lists the tools) is "connected" before its first request works."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-checkin-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.telemetry = TelemetryManager(db_path=self.dir / "telemetry.sqlite3")

    def call(self, client: str | None, *, error: str | None = None, agent_id: str | None = None,
             source: str = "mcp") -> None:
        metadata = {k: v for k, v in {"client": client, "agent_id": agent_id}.items() if v}
        event_id = self.telemetry.start_call(source, "system_info", {}, metadata=metadata)
        self.telemetry.finish_call(event_id, result=None if error else {"ok": True}, error=error)

    def test_listing_the_tools_is_connected_but_no_request_yet(self) -> None:
        self.telemetry.note_client("ChatGPT")
        [seen] = self.telemetry.client_checkins()
        self.assertEqual("ChatGPT", seen["name"])
        self.assertIsNotNone(seen["seen_at"])
        self.assertIsNone(seen["first_call_at"])
        self.assertIsNone(seen["first_success_at"])

    def test_a_failed_call_then_a_working_one(self) -> None:
        self.call("ChatGPT", error="denied")
        self.call("ChatGPT")
        [seen] = self.telemetry.client_checkins()
        self.assertEqual("error", seen["first_call_status"])
        self.assertIsNotNone(seen["first_success_at"])

    def test_agents_unknown_apps_and_the_dashboard_do_not_count(self) -> None:
        self.call("Codex", agent_id="agt_0123abcd")
        self.call(None)
        self.call("Claude Code", source="dashboard")
        self.telemetry.note_client("Mac MCP agent")
        self.telemetry.note_client("  ")
        self.assertEqual([], self.telemetry.client_checkins())

    def test_the_list_is_bounded(self) -> None:
        for index in range(40):
            self.telemetry.note_client(f"App {index}")
        names = [entry["name"] for entry in self.telemetry.client_checkins()]
        self.assertEqual(16, len(names))
        self.assertEqual("App 39", names[-1])

    def test_listing_tools_over_mcp_checks_the_app_in(self) -> None:
        import asyncio

        from mcp_server.observability import ObservedFastMCP

        mcp = ObservedFastMCP(name="checkin-test", telemetry=self.telemetry)

        @mcp.tool(name="get_volume")
        def get_volume() -> dict:
            return {"ok": True}

        with patch.object(ObservedFastMCP, "client_label", lambda self, context=None: "ChatGPT"):
            asyncio.run(mcp.list_tools())
        self.assertEqual(["ChatGPT"], [entry["name"] for entry in self.telemetry.client_checkins()])


class OnboardingRouteTests(unittest.TestCase):
    TOKEN = "onboarding-route-token"

    def client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.security import load_settings

        td = tempfile.mkdtemp(prefix="mac-mcp-onboarding-route-")
        self.addCleanup(shutil.rmtree, td, True)
        self.telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
        app = Starlette(routes=create_dashboard_routes(self.telemetry, load_settings(), self.TOKEN))
        return TestClient(app)

    def test_route_needs_the_dashboard_token_and_reports_each_step(self) -> None:
        client = self.client()
        self.assertEqual(401, client.get("/dashboard/api/onboarding").status_code)
        perms = {"accessibility": "granted", "screen_recording": "not_determined", "restart_needed": True}
        with patch("mcp_server.dashboard_routes.permission_status", return_value=perms), \
                patch("mcp_server.dashboard_routes.server_setting", return_value="cloudflare"):
            body = client.get("/dashboard/api/onboarding", headers={"authorization": f"Bearer {self.TOKEN}"}).json()
        self.assertTrue(body["ok"])
        self.assertIsNone(body["first_call"])
        self.assertIsNone(body["first_success"])
        self.assertEqual([], body["clients_seen"])
        self.assertEqual("granted", body["accessibility"])
        self.assertEqual("not_determined", body["screen_recording"])
        self.assertEqual("cloudflare", body["public_endpoint_mode"])
        self.assertTrue(body["permission_restart_needed"])
        self.telemetry.note_client("ChatGPT")
        with patch("mcp_server.dashboard_routes.permission_status", return_value=perms):
            body = client.get("/dashboard/api/onboarding", headers={"authorization": f"Bearer {self.TOKEN}"}).json()
        self.assertEqual(["ChatGPT"], [entry["name"] for entry in body["clients_seen"]])


class PermissionsViewTests(OnboardingRouteTests):
    def test_settings_see_fresh_permission_states(self) -> None:
        client = self.client()
        report = {"ok": True, "permissions": {"accessibility": {"key": "accessibility", "state": "denied"},
                                               "screen_recording": {"key": "screen_recording", "state": "denied"}}}
        perms = {"accessibility": "granted", "screen_recording": "granted", "restart_needed": True}
        with patch("mcp_server.dashboard_routes.probe_permissions", return_value=report), \
                patch("mcp_server.dashboard_routes.permission_status", return_value=perms):
            body = client.get("/dashboard/api/diagnostics/permissions",
                              headers={"authorization": f"Bearer {self.TOKEN}"}).json()
        self.assertEqual("granted", body["permissions"]["accessibility"]["state"])
        self.assertEqual("granted", body["permissions"]["screen_recording"]["state"])
        self.assertTrue(body["restart_needed"])


if __name__ == "__main__":
    unittest.main()
