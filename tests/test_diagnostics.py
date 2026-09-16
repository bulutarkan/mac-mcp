from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mcp_server import diagnostics
from mcp_server import cli


class DiagnosticsTests(unittest.TestCase):
    def test_settings_check_never_returns_setting_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            secret = "SENTINEL_DO_NOT_LEAK_12345"
            path.write_text(json.dumps({"provider": {"api_key": secret}, "secret": secret}))
            with patch("mcp_server.diagnostics.settings_path", return_value=path):
                row = diagnostics._check_settings().to_dict()
            text = json.dumps(row)
            self.assertNotIn(secret, text)
            self.assertIn("secret", row["details"]["top_level_keys"])

    def test_dashboard_token_check_only_reports_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dashboard-token"
            secret = "DASHBOARD_SUPER_SECRET_SENTINEL"
            path.write_text(secret)
            os.chmod(path, 0o600)
            with patch("mcp_server.diagnostics.dashboard_token_path", return_value=path):
                row = diagnostics._check_dashboard_token().to_dict()
            self.assertEqual(row["status"], "pass")
            self.assertNotIn(secret, json.dumps(row))
            self.assertEqual(row["details"]["mode"], "0o600")

    def test_runtime_companion_query_does_not_echo_credential(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dashboard-token"
            secret = "DASHBOARD_SUPER_SECRET_SENTINEL"
            path.write_text(secret)
            os.chmod(path, 0o600)
            with patch("mcp_server.diagnostics.dashboard_token_path", return_value=path), \
                 patch("mcp_server.diagnostics._request_json", return_value=(200, {"ok": True, "version": "x", "chrome_companion_connected": True})) as request:
                row = diagnostics._check_runtime_companion_state().to_dict()
            self.assertEqual(row["reason_code"], "CHROME_COMPANION_CONNECTED")
            self.assertNotIn(secret, json.dumps(row))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], f"Bearer {secret}")

    def test_support_bundle_is_owner_only_and_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            secret = "SENTINEL_SUPPORT_SECRET"
            settings.write_text(json.dumps({"api_key": secret}))
            with patch("mcp_server.diagnostics.settings_path", return_value=settings):
                report = diagnostics.build_report([diagnostics._check_settings()])
            path = diagnostics.write_support_bundle(report, Path(td) / "bundle.json")
            raw = path.read_text()
            self.assertNotIn(secret, raw)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            payload = json.loads(raw)
            self.assertFalse(payload["privacy"]["raw_env_included"])
            self.assertFalse(payload["privacy"]["credential_values_included"])
            self.assertNotIn("environment", payload)

    def test_local_port_falls_back_to_live_runtime_settings(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PORT": ""}, clear=False), \
             patch("mcp_server.diagnostics.load_runtime_settings", return_value={"server": {"port": 8765}}):
            self.assertEqual(diagnostics._local_host_port(), ("127.0.0.1", 8765))

    def test_managed_server_accepts_legacy_pid_file(self) -> None:
        with patch("mcp_server.diagnostics._read_pid_file", side_effect=[43210, None]), \
             patch("mcp_server.diagnostics._pid_alive", return_value=True):
            row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.reason_code, "SERVER_RUNNING")
        self.assertEqual(row.details["managed_by"], "pid_file")
        self.assertEqual(row.details["pid"], 43210)

    def test_managed_server_accepts_launchctl_without_pid_file(self) -> None:
        with patch("mcp_server.diagnostics._read_pid_file", return_value=None), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=54321):
            row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.reason_code, "SERVER_RUNNING")
        self.assertEqual(row.details["managed_by"], "launchctl")
        self.assertEqual(row.details["label"], "mac-mcp-uvicorn")

    def test_managed_server_listener_is_last_safe_fallback(self) -> None:
        with patch("mcp_server.diagnostics._read_pid_file", return_value=None), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=None), \
             patch("mcp_server.diagnostics._local_host_port", return_value=("127.0.0.1", 8765)), \
             patch("mcp_server.diagnostics._listener_pid", return_value=65432):
            row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.details["managed_by"], "listener")
        self.assertEqual(row.details["port"], 8765)

    def test_invalid_settings_has_stable_reason_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{broken")
            with patch("mcp_server.diagnostics.settings_path", return_value=path):
                row = diagnostics._check_settings()
            self.assertEqual(row.status, "fail")
            self.assertEqual(row.reason_code, "SETTINGS_INVALID_JSON")

    def test_doctor_cli_json_is_machine_readable(self) -> None:
        fake = diagnostics.build_report([
            diagnostics.result("fixture", "test", "pass", "FIXTURE_OK", "Fixture passed.")
        ])
        out = io.StringIO()
        with patch("mcp_server.diagnostics.run_doctor", return_value=fake), redirect_stdout(out):
            code = cli.main(["doctor", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["checks"][0]["reason_code"], "FIXTURE_OK")


if __name__ == "__main__":
    unittest.main()

class RuntimeDiagnosticsRouteTests(unittest.TestCase):
    def test_runtime_diagnostics_requires_auth_and_reports_connection_only(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), "diag-token"))
            client = TestClient(app)
            self.assertEqual(client.get("/dashboard/api/diagnostics/runtime").status_code, 401)
            with patch("mcp_server.dashboard_routes.chrome_background_bridge.is_connected", return_value=True):
                response = client.get(
                    "/dashboard/api/diagnostics/runtime",
                    headers={"Authorization": "Bearer diag-token"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                set(response.json()),
                {"ok", "version", "chrome_companion_connected"},
            )
            self.assertTrue(response.json()["chrome_companion_connected"])

    def test_runtime_diagnostics_is_loopback_only(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        with tempfile.TemporaryDirectory() as td:
            manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(manager, load_settings(), "diag-token"))
            response = TestClient(app).get(
                "/dashboard/api/diagnostics/runtime",
                headers={"Authorization": "Bearer diag-token", "x-forwarded-for": "8.8.8.8"},
            )
            self.assertEqual(response.status_code, 403)
