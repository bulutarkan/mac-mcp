from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import _is_loopback, _persist_permission_profile, browser_event_context, create_dashboard_routes
from mcp_server.observability import TelemetryManager, sanitize_value
from mcp_server.security import dashboard_authorized, ensure_dashboard_token, load_settings
from mcp_server.steering import SteeringIdentity, SteeringManager
from mcp_server.version import __version__


DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
DASHBOARD_AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}


class SanitizerTests(unittest.TestCase):
    def test_redacts_secrets_and_preserves_useful_context(self) -> None:
        payload = {
            "authorization": "Bearer top-secret-value-123456",
            "api_key": "sk-proj-secretsecretsecret123456",
            "command": "curl https://example.com?token=url-token-123456",
            "prompt": "PASSWORD=hunter2\nkeep this context visible",
            "nested": {"cookie": "session=secret"},
            "image": "data:image/png;base64," + "A" * 1500,
        }
        sanitized = sanitize_value(payload, preview_chars=512)
        text = json.dumps(sanitized, ensure_ascii=False)
        for secret in ("top-secret-value", "secretsecretsecret", "url-token-123456", "hunter2", "session=secret"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("keep this context visible", text)
        self.assertIn("image data", text)

    def test_redacts_provider_identity_metadata(self) -> None:
        payload = {
            "openai/session": "raw-conversation-id",
            "openai/subject": "raw-account-subject",
            "openai/organization": "raw-org-id",
            "openai/location": "precise-provider-location",
        }
        text = json.dumps(sanitize_value(payload), ensure_ascii=False)
        for key, value in payload.items():
            self.assertNotIn(key, text)
            self.assertNotIn(value, text)
        self.assertEqual({}, sanitize_value(payload))
        free_text = "failure openai/session=raw-session-value openai/subject:raw-subject-value"
        rendered = str(sanitize_value(free_text))
        self.assertNotIn("openai/session", rendered)
        self.assertNotIn("raw-session-value", rendered)
        self.assertNotIn("openai/subject", rendered)
        self.assertNotIn("raw-subject-value", rendered)


class TelemetryTests(unittest.TestCase):
    def test_event_persists_and_summary_counts_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100)
            ok_id = manager.start_call("mcp", "read_file", {"path": "/tmp/example"})
            manager.finish_call(ok_id, result={"ok": True})
            err_id = manager.start_call("mcp", "run_command", {"command": "false"})
            manager.finish_call(err_id, error=RuntimeError("expected failure"))

            summary = manager.summary(24)
            self.assertEqual(summary["total_calls"], 2)
            self.assertEqual(summary["success_calls"], 1)
            self.assertEqual(summary["error_calls"], 1)

            reopened = TelemetryManager(db_path=db, max_events=100)
            events = reopened.query_events(limit=10)
            self.assertEqual({event["tool"] for event in events}, {"read_file", "run_command"})

    def test_startup_migration_scrubs_legacy_provider_identity_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "telemetry.sqlite3"
            manager = TelemetryManager(db_path=db, max_events=100)
            event_id = manager.start_call("mcp", "read_file", {"path": "/tmp/example"})
            manager.finish_call(event_id, result={"ok": True})
            legacy_args = json.dumps({
                "_meta": {"openai/session": "legacy-session-value", "openai/subject": "legacy-subject-value"},
                "path": "/tmp/example",
            })
            legacy_result = json.dumps({
                "ok": True,
                "meta": {"openai/organization": "legacy-org-value", "openai/location": "legacy-location-value"},
            })
            import sqlite3
            con = sqlite3.connect(db)
            con.execute(
                "UPDATE tool_events SET arguments_json=?, result_json=? WHERE event_id=?",
                (legacy_args, legacy_result, event_id),
            )
            con.execute("PRAGMA user_version=0")
            con.commit(); con.close()

            migrated = TelemetryManager(db_path=db, max_events=100)
            event = migrated.query_events(limit=1)[0]
            rendered = json.dumps(event, ensure_ascii=False)
            for marker in ("openai/session", "openai/subject", "openai/organization", "openai/location",
                           "legacy-session-value", "legacy-subject-value", "legacy-org-value", "legacy-location-value"):
                self.assertNotIn(marker, rendered)
            con = sqlite3.connect(db)
            raw = " ".join(str(value or "") for value in con.execute(
                "SELECT arguments_json, result_json FROM tool_events WHERE event_id=?", (event_id,)
            ).fetchone())
            version = con.execute("PRAGMA user_version").fetchone()[0]
            con.close()
            self.assertEqual(1, version)
            self.assertNotIn("openai/", raw.lower())
            self.assertNotIn("legacy-session-value", raw)
            raw_bytes = db.read_bytes().lower()
            self.assertNotIn(b"openai/session", raw_bytes)
            self.assertNotIn(b"legacy-session-value", raw_bytes)
            wal = db.with_name(db.name + "-wal")
            if wal.exists():
                wal_bytes = wal.read_bytes().lower()
                self.assertNotIn(b"openai/session", wal_bytes)
                self.assertNotIn(b"legacy-session-value", wal_bytes)

    def test_database_recovers_if_storage_directory_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "dashboard"
            manager = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
            shutil.rmtree(root)
            event_id = manager.start_call("mcp", "get_volume", {})
            manager.finish_call(event_id, result={"ok": True})
            self.assertEqual(manager.summary(24)["total_calls"], 1)

    def test_sse_subscription_receives_start_and_finish(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                queue = manager.subscribe()
                event_id = manager.start_call("mcp", "get_volume", {})
                manager.finish_call(event_id, result={"ok": True})
                started = await asyncio.wait_for(queue.get(), timeout=1)
                finished = await asyncio.wait_for(queue.get(), timeout=1)
                manager.unsubscribe(queue)
                self.assertEqual(started["kind"], "call_started")
                self.assertEqual(finished["kind"], "call_finished")
                self.assertEqual(finished["status"], "success")

        asyncio.run(run())


class BrowserVisibilityTests(unittest.TestCase):
    def test_browser_context_keeps_only_minimal_site_metadata(self) -> None:
        context = browser_event_context({
            "tool": "browser_do",
            "arguments": {
                "browser": "Safari",
                "url": "https://www.example.com/private/path?token=secret-value",
                "actions": [{"type": "click", "selector": "#account"}],
            },
            "result": {
                "tab_handle": "tab_test123",
                "title": "Private Account Dashboard",
            },
        })
        self.assertEqual(context, {
            "browser": "Safari",
            "tab_handle": "tab_test123",
            "site": "example.com",
            "action": "Browser task",
        })
        rendered = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("private/path", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("Private Account", rendered)
        self.assertNotIn("#account", rendered)

    def test_browser_context_can_use_nested_opened_result(self) -> None:
        context = browser_event_context({
            "tool": "browser_do",
            "arguments": {"browser": "Google Chrome"},
            "result": {
                "opened": {
                    "url": "https://news.example.org/story?id=42",
                    "tab_handle": "tab_nested",
                }
            },
        })
        self.assertEqual(context["site"], "news.example.org")
        self.assertEqual(context["tab_handle"], "tab_nested")

    def test_non_browser_event_has_no_browser_context(self) -> None:
        self.assertIsNone(browser_event_context({
            "tool": "run_command",
            "arguments": {"command": "echo browser_open_url"},
        }))


class SecuritySemanticsRouteTests(unittest.TestCase):
    def test_permission_profile_persistence_preserves_env_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("MCP_ALLOW_NO_AUTH=false\nMAC_MCP_PERMISSION_PROFILE=trusted\nSECRET_PLACEHOLDER=keep-me\n", encoding="utf-8")
            _persist_permission_profile("read_only", env_file)
            text = env_file.read_text(encoding="utf-8")
            self.assertIn("MCP_ALLOW_NO_AUTH=false", text)
            self.assertIn("SECRET_PLACEHOLDER=keep-me", text)
            self.assertIn("MAC_MCP_PERMISSION_PROFILE=read_only", text)
            self.assertNotIn("MAC_MCP_PERMISSION_PROFILE=trusted", text)
            self.assertEqual(0o600, env_file.stat().st_mode & 0o777)

    def test_profile_change_is_immediate_persistent_and_restart_free(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            with patch("mcp_server.dashboard_routes._persist_permission_profile") as persist, \
                 patch.dict("os.environ", {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False):
                response = TestClient(app).post(
                    "/dashboard/api/security/profile", json={"profile": "read_only"}, headers=DASHBOARD_AUTH
                )
                self.assertEqual(200, response.status_code)
                payload = response.json()
                self.assertTrue(payload["ok"])
                self.assertEqual("read_only", payload["active_profile"])
                self.assertFalse(payload["restart_required"])
                self.assertTrue(payload["existing_scoped_agents_retain_profile"])
                self.assertEqual("read_only", __import__("os").environ["MAC_MCP_PERMISSION_PROFILE"])
                persist.assert_called_once_with("read_only")

    def test_profile_change_rejects_unknown_and_remote_requests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            with patch("mcp_server.dashboard_routes._persist_permission_profile") as persist:
                invalid = TestClient(app).post(
                    "/dashboard/api/security/profile", json={"profile": "approval_heavy"}, headers=DASHBOARD_AUTH
                )
                remote = TestClient(app).post(
                    "/dashboard/api/security/profile",
                    json={"profile": "standard"},
                    headers={**DASHBOARD_AUTH, "x-forwarded-for": "8.8.8.8"},
                )
                self.assertEqual(400, invalid.status_code)
                self.assertEqual(403, remote.status_code)
                persist.assert_not_called()

    def test_security_semantics_is_local_only_and_separates_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            with patch.dict("os.environ", {"MAC_MCP_PERMISSION_PROFILE": "standard"}, clear=False):
                local = TestClient(app).get("/dashboard/api/security/semantics", headers=DASHBOARD_AUTH)
                remote = TestClient(app).get(
                    "/dashboard/api/security/semantics",
                    headers={**DASHBOARD_AUTH, "x-forwarded-for": "8.8.8.8"},
                )
            self.assertEqual(200, local.status_code)
            payload = local.json()
            self.assertEqual("standard", payload["active_profile"])
            self.assertFalse(payload["ask_confirmation_is_automatic_gate"])
            self.assertEqual(403, remote.status_code)


class SteeringLifecycleRouteTests(unittest.TestCase):
    def test_versioned_steering_api_keeps_legacy_activity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            steering = SteeringManager()
            identity = SteeringIdentity(key="client:test-lifecycle", source="client_id")
            session_id = steering.session_id_for(identity)
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN, steering))

            state_response = TestClient(app).get("/dashboard/api/steering", headers=DASHBOARD_AUTH)
            self.assertEqual(200, state_response.status_code)
            payload = state_response.json()
            self.assertEqual(1, payload["schema_version"])
            session = payload["sessions"][0]
            self.assertEqual(1, session["schema_version"])
            self.assertEqual("idle", session["state"])
            self.assertEqual("idle", session["activity_state"])
            self.assertEqual("ready", session["lifecycle_state"])
            self.assertEqual(0, session["queued"])
            self.assertEqual(0, session["pending_instruction_count"])

            send_response = TestClient(app).post(
                "/dashboard/api/steering",
                json={"session_id": session_id, "text": "change direction"}, headers=DASHBOARD_AUTH,
            )
            self.assertEqual(200, send_response.status_code)
            sent = send_response.json()
            self.assertEqual(1, sent["schema_version"])
            self.assertEqual("queued", sent["message"]["lifecycle_state"])
            self.assertEqual("idle", sent["message"]["activity_state"])

            queued = TestClient(app).get("/dashboard/api/steering", headers=DASHBOARD_AUTH).json()["sessions"][0]
            self.assertEqual("queued", queued["lifecycle_state"])
            self.assertEqual(1, queued["queued"])
            self.assertEqual(1, queued["pending_instruction_count"])


class BrowserShowTabRouteTests(unittest.TestCase):
    def test_show_tab_is_explicit_foreground_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            with patch("mcp_server.dashboard_routes.browser_activate_tab") as activate:
                activate.return_value = {
                    "ok": True,
                    "browser": "Safari",
                    "tab_handle": "tab_live",
                    "foreground_forced": True,
                }
                response = TestClient(app).post(
                    "/dashboard/api/browser/show-tab",
                    json={"browser": "Safari", "tab_handle": "tab_live"}, headers=DASHBOARD_AUTH,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["foreground_forced"])
                activate.assert_called_once_with(
                    unittest.mock.ANY,
                    browser="Safari",
                    tab_handle="tab_live",
                    allow_foreground=True,
                )

    def test_show_tab_remains_localhost_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            with patch("mcp_server.dashboard_routes.browser_activate_tab") as activate:
                response = TestClient(app).post(
                    "/dashboard/api/browser/show-tab",
                    json={"browser": "Safari", "tab_handle": "tab_live"},
                    headers={**DASHBOARD_AUTH, "x-forwarded-for": "8.8.8.8"},
                )
                self.assertEqual(response.status_code, 403)
                activate.assert_not_called()


class DashboardSecurityTests(unittest.TestCase):
    def test_only_loopback_addresses_are_local(self) -> None:
        self.assertTrue(_is_loopback("127.0.0.1"))
        self.assertTrue(_is_loopback("::1"))
        self.assertFalse(_is_loopback("8.8.8.8"))
        self.assertFalse(_is_loopback("192.168.1.10"))

    def test_dashboard_token_file_is_owner_only_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            token_file = Path(td) / "state" / "dashboard-token"
            first = ensure_dashboard_token(token_file)
            second = ensure_dashboard_token(token_file)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(0o600, token_file.stat().st_mode & 0o777)
            self.assertEqual(0o700, token_file.parent.stat().st_mode & 0o777)

    def test_dashboard_token_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "real-token"
            target.write_text("x" * 64, encoding="utf-8")
            link = root / "dashboard-token"
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                ensure_dashboard_token(link)

    def test_dashboard_bearer_comparison(self) -> None:
        self.assertTrue(dashboard_authorized(DASHBOARD_TOKEN, f"Bearer {DASHBOARD_TOKEN}"))
        self.assertFalse(dashboard_authorized(DASHBOARD_TOKEN, None))
        self.assertFalse(dashboard_authorized(DASHBOARD_TOKEN, "Bearer wrong"))

    def test_sensitive_dashboard_api_requires_auth_even_on_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), DASHBOARD_TOKEN))
            client = TestClient(app)
            self.assertEqual(200, client.get("/dashboard").status_code)
            self.assertEqual(401, client.get("/dashboard/api/summary").status_code)
            self.assertEqual(401, client.get("/dashboard/api/summary", headers={"authorization": "Bearer wrong"}).status_code)
            self.assertEqual(200, client.get("/dashboard/api/summary", headers=DASHBOARD_AUTH).status_code)
            self.assertEqual(401, client.get("/dashboard/events").status_code)


class VersionTests(unittest.TestCase):
    def test_runtime_and_package_versions_match(self) -> None:
        project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
