from __future__ import annotations

import sqlite3
import tempfile
import time
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
    def make_app(self, root: Path, steering=None):
        telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
        store = MobileAuthStore(root / "mobile_auth.sqlite3")
        settings = load_settings()
        routes = []
        routes.extend(create_dashboard_routes(telemetry, settings, DASHBOARD_TOKEN))
        routes.extend(create_mobile_routes(
            telemetry,
            settings,
            DASHBOARD_TOKEN,
            steering=steering,
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
        self.assertTrue(body["pair_url"].startswith("https://mobile.example.test/mobile?pair=1#pair="))
        self.assertRegex(body["manual_code"], r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$")
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
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Path=/mobile", cookie)
            self.assertIn("Max-Age=", cookie)
            self.assertIn("expires=", cookie.lower())

            second = TestClient(app, base_url="https://testserver").post(
                "/mobile/pair", json={"code": code, "device_name": "Other"}
            )
            self.assertEqual(401, second.status_code)
            self.assertEqual("invalid_or_expired_pairing", second.json()["error"])




    def test_legacy_mobile_auth_database_migrates_manual_pairing_columns(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "mobile_auth.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE mobile_pairings (
                    code_hash TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                )
                """
            )
            conn.commit()
            conn.close()

            store = MobileAuthStore(db_path)
            issued = store.issue_pairing()
            self.assertRegex(issued["manual_code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")

            conn = sqlite3.connect(db_path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(mobile_pairings)")}
            row = conn.execute(
                "SELECT manual_code_hash, failed_attempts FROM mobile_pairings"
            ).fetchone()
            conn.close()

            self.assertIn("manual_code_hash", columns)
            self.assertIn("failed_attempts", columns)
            self.assertIsNotNone(row[0])
            self.assertNotIn(issued["manual_code"].replace("-", ""), row[0])
            self.assertEqual(0, row[1])
            self.assertIsNotNone(
                store.consume_pairing(issued["manual_code"], device_name="Home Screen")
            )

    def test_manual_pairing_code_creates_persistent_session_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            pairing = self.create_pairing(manager)
            manual_code = pairing["manual_code"]

            home = TestClient(app, base_url="https://testserver")
            response = home.post(
                "/mobile/pair",
                json={"code": manual_code.lower(), "device_name": "Home Screen"},
            )
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertNotIn("session_token", body)
            self.assertEqual("no-store", response.headers["cache-control"])
            self.assertIn("mac_mcp_mobile=", response.headers.get("set-cookie", ""))

            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": [], "count": 0},
            ):
                self.assertEqual(200, home.get("/mobile/api/status").status_code)

            reused = TestClient(app, base_url="https://testserver").post(
                "/mobile/pair",
                json={"code": manual_code, "device_name": "Other Home Screen"},
            )
            self.assertEqual(401, reused.status_code)

            devices = manager.get("/dashboard/api/mobile/devices", headers=DASHBOARD_AUTH)
            self.assertEqual(200, devices.status_code)
            device_id = next(
                row["device_id"]
                for row in devices.json()["devices"]
                if row["device_name"] == "Home Screen"
            )
            revoked = manager.post(
                "/dashboard/api/mobile/revoke",
                headers=DASHBOARD_AUTH,
                json={"device_id": device_id},
            )
            self.assertTrue(revoked.json()["revoked"])
            self.assertEqual(401, home.get("/mobile/api/status").status_code)

    def test_legacy_mobile_bearer_remains_accepted_during_cookie_migration(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, store = self.make_app(Path(td))
            issued = store.issue_pairing()
            session = store.consume_pairing(issued["code"], device_name="Legacy iPhone")
            self.assertIsNotNone(session)
            client = TestClient(app, base_url="https://testserver")
            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": [], "count": 0},
            ):
                response = client.get(
                    "/mobile/api/status",
                    headers={"authorization": "Bearer " + session["token"]},
                )
            self.assertEqual(200, response.status_code)

    def test_manual_pairing_expiry_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, store = self.make_app(Path(td))
            issued = store.issue_pairing(ttl_s=15)
            client = TestClient(app, base_url="https://testserver")
            with patch("mcp_server.mobile_auth.time.time", return_value=issued["expires_at"] + 1):
                response = client.post(
                    "/mobile/pair",
                    json={"code": issued["manual_code"], "device_name": "Home Screen"},
                )
            self.assertEqual(401, response.status_code)

    def test_manual_pairing_five_wrong_attempts_lock_pairing_window(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            pairing = self.create_pairing(manager)
            client = TestClient(app, base_url="https://testserver")

            for index in range(5):
                wrong = f"ZZZZ-ZZ{index:02d}"[-9:]
                response = client.post(
                    "/mobile/pair",
                    json={"code": wrong, "device_name": "Home Screen"},
                )
                self.assertEqual(401, response.status_code)

            correct = client.post(
                "/mobile/pair",
                json={"code": pairing["manual_code"], "device_name": "Home Screen"},
            )
            self.assertEqual(401, correct.status_code)

    def test_wrong_codes_from_another_source_cannot_burn_owner_pairing(self):
        from mcp_server import mobile_auth

        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            pairing = self.create_pairing(manager)
            client = TestClient(app, base_url="https://testserver")
            with patch(
                "mcp_server.mobile_routes._client_address",
                side_effect=lambda request: request.headers.get("x-test-source", "unknown"),
            ):
                for index in range(mobile_auth.MAX_MANUAL_ATTEMPTS + 1):
                    wrong = client.post(
                        "/mobile/pair",
                        json={"code": f"ZZZZ-ZZ{index:02d}", "device_name": "Stranger"},
                        headers={"x-test-source": "203.0.113.9"},
                    )
                    self.assertEqual(401, wrong.status_code)
                locked = client.post(
                    "/mobile/pair",
                    json={"code": pairing["manual_code"], "device_name": "Stranger"},
                    headers={"x-test-source": "203.0.113.9"},
                )
                self.assertEqual(401, locked.status_code)
                owner = client.post(
                    "/mobile/pair",
                    json={"code": pairing["manual_code"], "device_name": "Owner"},
                    headers={"x-test-source": "198.51.100.7"},
                )
            self.assertEqual(200, owner.status_code)

    def test_total_wrong_codes_across_sources_still_close_the_window(self):
        from mcp_server import mobile_auth

        with tempfile.TemporaryDirectory() as td:
            _app, _telemetry, store = self.make_app(Path(td))
            issued = store.issue_pairing()
            for index in range(mobile_auth.MAX_MANUAL_TOTAL_ATTEMPTS):
                self.assertIsNone(store.consume_pairing("ZZZZ-ZZZZ", source=f"198.18.{index // 250}.{index % 250}"))
            self.assertIsNone(store.consume_pairing(issued["manual_code"], source="198.51.100.7"))

    def test_rate_limited_form_pairing_redirects_to_the_pairing_view(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            self.create_pairing(manager)
            client = TestClient(app, base_url="https://testserver", follow_redirects=False)
            responses = [
                client.post(
                    "/mobile/pair",
                    data={"code": f"BAD{index}", "device_name": "Phone"},
                    headers={"x-forwarded-for": "203.0.113.77"},
                )
                for index in range(9)
            ]
            self.assertEqual(303, responses[-1].status_code)
            self.assertEqual("/mobile?pair_error=rate_limited", responses[-1].headers["location"])

    def test_mobile_page_explains_lost_access_without_storing_the_session(self):
        root = Path(__file__).resolve().parents[1] / "mcp_server" / "mobile"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "mobile.js").read_text(encoding="utf-8")
        self.assertIn('id="accessLost"', html)
        self.assertIn('role="status"', html)
        self.assertIn("Settings → Connections → Mobile", html)
        self.assertIn('id="pairRateLimited"', html)
        self.assertIn('locked(pairedBefore() ? "access_lost" : "")', script)
        self.assertIn("clearDashboard();", script)
        self.assertIn('localStorage.setItem(PAIRED_MARKER_KEY, "1")', script)
        self.assertEqual(1, script.count("localStorage.setItem("))

    def test_manual_pairing_ip_rate_limit_returns_429(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            self.create_pairing(manager)
            client = TestClient(app, base_url="https://testserver")

            status_codes = []
            for index in range(9):
                response = client.post(
                    "/mobile/pair",
                    json={"code": f"BAD{index}", "device_name": "Home Screen"},
                    headers={"x-forwarded-for": "203.0.113.55"},
                )
                status_codes.append(response.status_code)
            self.assertEqual(429, status_codes[-1])
            self.assertEqual("60", response.headers.get("retry-after"))

    def test_new_pairing_invalidates_previous_manual_code(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            first = self.create_pairing(manager)
            second = self.create_pairing(manager)

            old = TestClient(app, base_url="https://testserver").post(
                "/mobile/pair",
                json={"code": first["manual_code"], "device_name": "Old"},
            )
            self.assertEqual(401, old.status_code)

            fresh = TestClient(app, base_url="https://testserver").post(
                "/mobile/pair",
                json={"code": second["manual_code"], "device_name": "Fresh"},
            )
            self.assertEqual(200, fresh.status_code)

    def test_top_level_pairing_redirects_with_cookie_without_exposing_bearer(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            code = self.pair_code(self.create_pairing(manager)["pair_url"])

            phone = TestClient(app, base_url="https://testserver")
            response = phone.post(
                "/mobile/pair",
                data={"code": code, "device_name": "iPhone"},
                follow_redirects=False,
            )
            self.assertEqual(303, response.status_code)
            self.assertEqual("/mobile", response.headers["location"])
            self.assertEqual("no-store", response.headers["cache-control"])
            self.assertNotIn("mcpmob_", response.text)
            self.assertIn("mac_mcp_mobile=", response.headers.get("set-cookie", ""))

            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": [], "count": 0},
            ):
                first = phone.get("/mobile/api/status")
                second = phone.get("/mobile/api/status")
            self.assertEqual(200, first.status_code)
            self.assertEqual(200, second.status_code)

            js = (Path(__file__).parents[1] / "mcp_server" / "mobile" / "mobile.js").read_text()
            self.assertNotIn('get("session")', js)
            self.assertNotIn('headers.Authorization', js)
            self.assertIn('localStorage.removeItem(LEGACY_STORAGE_KEY)', js)

    def test_mobile_agent_list_is_bounded_and_prioritizes_active_agents(self):
        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td))
            manager = TestClient(app, base_url="https://testserver")
            code = self.pair_code(self.create_pairing(manager)["pair_url"])
            phone = TestClient(app, base_url="https://testserver")
            self.assertEqual(
                200,
                phone.post("/mobile/pair", json={"code": code, "device_name": "iPhone"}).status_code,
            )

            fake_agents = [
                {
                    "agent_id": "active_1",
                    "title": "Active One",
                    "status": "running",
                    "provider": "codex",
                    "model": "GPT-5.6",
                },
                {
                    "agent_id": "active_2",
                    "title": "Active Two",
                    "status": "starting",
                    "provider": "opencode",
                    "model": "Muse",
                },
            ]
            fake_agents.extend(
                {
                    "agent_id": f"done_{idx}",
                    "title": f"Done {idx}",
                    "status": "completed",
                    "provider": "codex",
                    "model": "GPT-5.6",
                    "ended_at": time.time() - 60,
                }
                for idx in range(20)
            )
            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": fake_agents, "count": len(fake_agents)},
            ):
                response = phone.get("/mobile/api/agents")
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(2, body["active_count"])
            self.assertEqual(6, body["count"])
            self.assertEqual(["Active One", "Active Two"], [row["title"] for row in body["agents"][:2]])
            self.assertNotIn("result_preview", response.text)

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
            with patch(
                "mcp_server.mobile_routes.list_agents",
                return_value={"ok": True, "agents": [fake_agent], "count": 1},
            ):
                status = phone.get("/mobile/api/status")
                agents = phone.get("/mobile/api/agents")

            self.assertEqual(200, status.status_code)
            self.assertEqual(1, status.json()["active_agents"])
            self.assertEqual(1, status.json()["calls_1h"])
            self.assertEqual(100.0, status.json()["success_rate"])
            self.assertNotIn("agent_count", status.json())
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

    def test_mobile_sessions_expose_grouping_signals_without_error_details(self):
        class FakeSteering:
            session_ttl_minutes = 10

            def sessions(self):
                now = time.time()
                return [
                    {
                        "schema_version": 1,
                        "session_id": "sess_active",
                        "flow_number": 1,
                        "label": "Safari · example.com",
                        "detail": "browser observe",
                        "tool": "browser_observe",
                        "state": "working",
                        "queued": 1,
                        "activity_state": "working",
                        "lifecycle_state": "queued",
                        "last_transition_at": now - 3,
                        "created_at": now - 40,
                        "last_activity_at": now - 2,
                        "activity_ms": 2_000,
                        "active_calls": 1,
                        "pending_instruction_count": 1,
                        "awaiting_acknowledgement_count": 0,
                        "last_error": None,
                    },
                    {
                        "schema_version": 1,
                        "session_id": "sess_attention",
                        "flow_number": 2,
                        "label": "Agent session",
                        "detail": "Idle",
                        "tool": "read_file",
                        "state": "idle",
                        "queued": 0,
                        "activity_state": "idle",
                        "lifecycle_state": "ready",
                        "last_transition_at": now - 8,
                        "created_at": now - 50,
                        "last_activity_at": now - 8,
                        "activity_ms": 8_000,
                        "active_calls": 0,
                        "pending_instruction_count": 0,
                        "awaiting_acknowledgement_count": 0,
                        "last_error": "security:private-policy-detail",
                    },
                ]

            def recent(self, _limit=30):
                now = time.time()
                return [
                    {
                        "kind": "instruction",
                        "session_id": "sess_active",
                        "lifecycle_state": "queued",
                        "text": "must-not-leak",
                        "transitioned_at": now - 4,
                    },
                    {
                        "kind": "session",
                        "session_id": "sess_expired",
                        "status": "session_expired",
                        "lifecycle_state": "expired",
                        "created_at": now - 90,
                        "transitioned_at": now - 6,
                        "tool": "write_file",
                        "last_error": "secret-terminal-detail",
                    },
                ]

        with tempfile.TemporaryDirectory() as td:
            app, _telemetry, _store = self.make_app(Path(td), steering=FakeSteering())
            manager = TestClient(app, base_url="https://testserver")
            code = self.pair_code(self.create_pairing(manager)["pair_url"])
            phone = TestClient(app, base_url="https://testserver")
            self.assertEqual(
                200,
                phone.post("/mobile/pair", json={"code": code, "device_name": "iPhone"}).status_code,
            )

            response = phone.get("/mobile/api/sessions")
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertEqual(1, body["schema_version"])
            self.assertEqual(2, body["count"])
            self.assertEqual(10, body["session_ttl_minutes"])
            self.assertEqual(["sess_active", "sess_attention"], [row["session_id"] for row in body["sessions"]])
            self.assertFalse(body["sessions"][0]["needs_attention"])
            self.assertTrue(body["sessions"][1]["needs_attention"])
            self.assertEqual("sess_expired", body["recent"][0]["session_id"])
            self.assertTrue(body["recent"][0]["needs_attention"])
            self.assertNotIn("private-policy-detail", response.text)
            self.assertNotIn("secret-terminal-detail", response.text)
            self.assertNotIn("must-not-leak", response.text)

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
