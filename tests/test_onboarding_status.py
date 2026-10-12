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
        self.assertEqual("granted", body["accessibility"])
        self.assertEqual("not_determined", body["screen_recording"])
        self.assertEqual("cloudflare", body["public_endpoint_mode"])
        self.assertTrue(body["permission_restart_needed"])


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
