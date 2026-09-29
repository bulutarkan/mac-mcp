from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.mobile_auth import MobileAuthStore
from mcp_server.mobile_routes import create_mobile_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

DASHBOARD_TOKEN = "mobile-dashboard-test-token-0123456789"
DASHBOARD_AUTH = {"authorization": "Bearer " + DASHBOARD_TOKEN}


class MobileDashboardTests(unittest.TestCase):
    def make_app(self, root: Path):
        telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
        store = MobileAuthStore(root / "mobile_auth.sqlite3")
        settings = load_settings()
        routes = []
        routes.extend(create_dashboard_routes(telemetry, settings, DASHBOARD_TOKEN))
        routes.extend(create_mobile_routes(
            telemetry,
            settings,
            DASHBOARD_TOKEN,
            steering=None,
            auth_store=store,
        ))
        return Starlette(routes=routes), telemetry, store

    @staticmethod
    def pair_code(pair_url: str) -> str:
        fragment = urlsplit(pair_url).fragment
        return parse_qs(fragment)["pair"][0]

    def create_pairing(self, client: TestClient):
        with patch("mcp_server.mobile_routes._public_mobile_url", return_value="https://mobile.example.test/mobile"):
            response = client.post("/dashboard/api/mobile/pairings", headers=DASHBOARD_AUTH)
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("https://mobile.example.test/mobile", body["mobile_url"])
        self.assertTrue(body["pair_url"].startswith("https://mobile.example.test/mobile#pair="))
        return body

    def test_remote_unauthenticated_shell_is_safe_and_api_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            client = TestClient(app, base_url="https://testserver")
            shell = client.get("/mobile", headers={"x-forwarded-for": "203.0.113.8"})
            self.assertEqual(200, shell.status_code)
            self.assertIn("Pair this device", shell.text)
            self.assertNotIn("result_preview", shell.text)
            api = client.get("/mobile/api/agents", headers={"x-forwarded-for": "203.0.113.8"})
            self.assertEqual(401, api.status_code)
            self.assertEqual("mobile_auth_required", api.json()["error"])

    def test_pairing_exchange_sets_secure_cookie_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            pair_info = self.create_pairing(manager)
            code = self.pair_code(pair_info["pair_url"])

            phone = TestClient(app, base_url="https://testserver")
            response = phone.post("/mobile/pair", json={"code": code, "device_name": "Tarkan iPhone"})
            self.assertEqual(200, response.status_code)
            cookie = response.headers.get("set-cookie", "")
            self.assertIn("mac_mcp_mobile=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertIn("Path=/mobile", cookie)

            second = TestClient(app, base_url="https://testserver").post(
                "/mobile/pair", json={"code": code, "device_name": "Other"}
            )
            self.assertEqual(401, second.status_code)
            self.assertEqual("invalid_or_expired_pairing", second.json()["error"])

    def test_expired_pairing_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, store = self.make_app(Path(td))
            client = TestClient(app, base_url="https://testserver")
            issued = store.issue_pairing(ttl_s=15)
            with patch("mcp_server.mobile_auth.time.time", return_value=issued["expires_at"] + 1):
                response = client.post("/mobile/pair", json={"code": issued["code"], "device_name": "iPhone"})
            self.assertEqual(401, response.status_code)

    def test_authorized_mobile_apis_are_minimal_and_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            app, telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            code = self.pair_code(self.create_pairing(manager)["pair_url"])
            phone = TestClient(app, base_url="https://testserver")
            self.assertEqual(200, phone.post("/mobile/pair", json={"code": code, "device_name": "iPhone"}).status_code)

            event_id = telemetry.start_call("mcp", "read_file", {"path": "/tmp/private-file"})
            telemetry.finish_call(event_id, result={"content": "secret-output"})

            fake_agent = {
                "agent_id": "agt_1",
                "status": "running",
                "phase": "working",
                "title": "Mobile test",
                "provider": "codex",
                "model": "GPT-5.6",
                "last_tool": "read_file",
                "result_preview": "must-not-leak",
                "working_directory": "/private/path",
            }
            fake_provider = {
                "providers": [{
                    "id": "codex",
                    "enabled": True,
                    "detected": True,
                    "version": "1.2.3",
                    "binary_path": "/private/bin",
                }]
            }
            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": [fake_agent], "count": 1},
            ), patch(
                "mcp_server.mobile_routes.provider_overview",
                return_value=fake_provider,
            ):
                status = phone.get("/mobile/api/status")
                agents = phone.get("/mobile/api/agents")

            self.assertEqual(200, status.status_code)
            self.assertEqual(1, status.json()["active_agents"])
            self.assertNotIn("binary_path", status.text)
            self.assertEqual(200, agents.status_code)
            self.assertIn("Mobile test", agents.text)
            self.assertNotIn("must-not-leak", agents.text)
            self.assertNotIn("/private/path", agents.text)

            sessions = phone.get("/mobile/api/sessions")
            self.assertEqual(200, sessions.status_code)
            self.assertEqual([], sessions.json()["sessions"])

            activity = phone.get("/mobile/api/activity")
            self.assertEqual(200, activity.status_code)
            self.assertIn("read_file", activity.text)
            self.assertNotIn("/tmp/private-file", activity.text)
            self.assertNotIn("secret-output", activity.text)

    def test_revoke_invalidates_existing_mobile_session(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            code = self.pair_code(self.create_pairing(manager)["pair_url"])
            phone = TestClient(app, base_url="https://testserver")
            pair = phone.post("/mobile/pair", json={"code": code, "device_name": "iPhone"})
            self.assertEqual(200, pair.status_code)
            self.assertEqual(200, phone.get("/mobile/api/status").status_code)

            devices = manager.get("/dashboard/api/mobile/devices", headers=DASHBOARD_AUTH)
            self.assertEqual(200, devices.status_code)
            device_id = devices.json()["devices"][0]["device_id"]
            revoked = manager.post(
                "/dashboard/api/mobile/revoke",
                headers=DASHBOARD_AUTH,
                json={"device_id": device_id},
            )
            self.assertEqual(200, revoked.status_code)
            self.assertTrue(revoked.json()["revoked"])
            self.assertEqual(401, phone.get("/mobile/api/status").status_code)

    def test_management_endpoints_are_local_only_even_with_dashboard_token(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            client = TestClient(app, base_url="https://testserver")
            headers = dict(DASHBOARD_AUTH)
            headers["x-forwarded-for"] = "203.0.113.9"
            with patch("mcp_server.mobile_routes._public_mobile_url", return_value="https://mobile.example.test/mobile"):
                response = client.post("/dashboard/api/mobile/pairings", headers=headers)
            self.assertEqual(403, response.status_code)
            devices = client.get("/dashboard/api/mobile/devices", headers=headers)
            self.assertEqual(403, devices.status_code)

    def test_existing_dashboard_remains_remote_denied(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            client = TestClient(app, base_url="https://testserver")
            response = client.get(
                "/dashboard/api/summary",
                headers={
                    **DASHBOARD_AUTH,
                    "x-forwarded-for": "203.0.113.10",
                },
            )
            self.assertEqual(403, response.status_code)
            self.assertIn("localhost only", response.text)


if __name__ == "__main__":
    unittest.main()
