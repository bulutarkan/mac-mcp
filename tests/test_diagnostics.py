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
from mcp_server.managed_process import ProcessSnapshot, ProcessValidation


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

    def test_managed_server_reports_legacy_pid_as_unfingerprinted(self) -> None:
        snap = ProcessSnapshot(
            43210,
            "Tue Sep 29 12:00:00 2026",
            "/usr/bin/python3",
            "/usr/bin/python3 -m uvicorn mcp_server.main:app --host 127.0.0.1 --port 8765",
            str(Path(diagnostics.__file__).resolve().parent.parent),
        )
        validation = ProcessValidation(
            "legacy_match", 43210, "server", "legacy", "legacy_role_match", snap,
        )
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "mac-mcp.pid").write_text("43210\n", encoding="utf-8")
            with patch("mcp_server.diagnostics.state_dir", return_value=state), \
                 patch("mcp_server.diagnostics.validate_process_record", return_value=validation):
                row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "warn")
        self.assertEqual(row.reason_code, "SERVER_PID_LEGACY_UNFINGERPRINTED")
        self.assertEqual(row.details["managed_by"], "pid_file")
        self.assertEqual(row.details["pid"], 43210)

    def test_managed_server_accepts_verified_launchctl_without_pid_file(self) -> None:
        root = Path(diagnostics.__file__).resolve().parent.parent
        snap = ProcessSnapshot(
            54321,
            "Tue Sep 29 12:00:00 2026",
            "/usr/bin/python3",
            "/usr/bin/python3 -m uvicorn mcp_server.main:app --host 127.0.0.1 --port 8765",
            str(root),
        )
        with tempfile.TemporaryDirectory() as td, \
             patch("mcp_server.diagnostics.state_dir", return_value=Path(td)), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=54321), \
             patch("mcp_server.diagnostics.process_snapshot", return_value=snap), \
             patch("mcp_server.diagnostics.matches_role", return_value=True):
            row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.reason_code, "SERVER_LAUNCHD_VERIFIED")
        self.assertEqual(row.details["managed_by"], "launchctl")
        self.assertEqual(row.details["label"], "mac-mcp-uvicorn")

    def test_managed_server_listener_is_verified_before_fallback(self) -> None:
        root = Path(diagnostics.__file__).resolve().parent.parent
        snap = ProcessSnapshot(
            65432,
            "Tue Sep 29 12:00:00 2026",
            "/usr/bin/python3",
            "/usr/bin/python3 -m uvicorn mcp_server.main:app --host 127.0.0.1 --port 8765",
            str(root),
        )
        with tempfile.TemporaryDirectory() as td, \
             patch("mcp_server.diagnostics.state_dir", return_value=Path(td)), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=None), \
             patch("mcp_server.diagnostics._local_host_port", return_value=("127.0.0.1", 8765)), \
             patch("mcp_server.diagnostics.listener_pids", return_value=[65432]), \
             patch("mcp_server.diagnostics.process_snapshot", return_value=snap), \
             patch("mcp_server.diagnostics.matches_role", return_value=True):
            row = diagnostics._check_managed_process("server")
        self.assertEqual(row.status, "pass")
        self.assertEqual(row.reason_code, "SERVER_LISTENER_VERIFIED")
        self.assertEqual(row.details["managed_by"], "verified_listener")
        self.assertEqual(row.details["port"], 8765)

    def test_invalid_settings_has_stable_reason_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{broken")
            with patch("mcp_server.diagnostics.settings_path", return_value=path):
                row = diagnostics._check_settings()
            self.assertEqual(row.status, "fail")
            self.assertEqual(row.reason_code, "SETTINGS_INVALID_JSON")

    def test_missing_settings_reports_provider_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            with patch("mcp_server.diagnostics.settings_path", return_value=path):
                row = diagnostics._check_settings().to_dict()
        self.assertEqual("info", row["status"])
        self.assertEqual("SETTINGS_NOT_CREATED", row["reason_code"])
        self.assertTrue(row["details"]["provider_fail_closed"])
        self.assertEqual("missing", row["details"]["load_status"])
        self.assertIn("delegated providers are fail-closed", row["summary"])

    def test_invalid_settings_reports_provider_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            with patch("mcp_server.diagnostics.settings_path", return_value=path):
                row = diagnostics._check_settings().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("SETTINGS_INVALID_JSON", row["reason_code"])
        self.assertTrue(row["details"]["provider_fail_closed"])
        self.assertEqual("invalid_json", row["details"]["load_status"])

    def test_unreadable_settings_has_distinct_reason_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            with patch("mcp_server.diagnostics.settings_path", return_value=path), \
                 patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                row = diagnostics._check_settings().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("SETTINGS_UNREADABLE", row["reason_code"])
        self.assertTrue(row["details"]["provider_fail_closed"])
        self.assertEqual("PermissionError", row["details"]["error_type"])

    def test_ngrok_dependency_uses_same_env_resolver_as_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "ngrok"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(binary, 0o755)
            with patch.dict(os.environ, {"NGROK_BIN": str(binary)}, clear=False):
                row = diagnostics._check_ngrok_dependency().to_dict()
        self.assertEqual("pass", row["status"])
        self.assertEqual("NGROK_AVAILABLE", row["reason_code"])
        self.assertEqual("env:NGROK_BIN", row["details"]["source"])
        self.assertTrue(row["details"]["path"].endswith("/ngrok"))

    def test_cloudflared_dependency_uses_same_env_resolver_as_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "cloudflared"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(binary, 0o755)
            with patch.dict(os.environ, {"CLOUDFLARED_BIN": str(binary)}, clear=False):
                row = diagnostics._check_cloudflared_dependency().to_dict()
        self.assertEqual("pass", row["status"])
        self.assertEqual("env:CLOUDFLARED_BIN", row["details"]["source"])

    def test_permission_coherence_reports_profile_provenance(self) -> None:
        with patch.dict(os.environ, {
            "MAC_MCP_PERMISSION_PROFILE": "trusted",
            "MCP_ALLOW_SHELL": "true",
        }, clear=False):
            row = diagnostics._check_permission_coherence().to_dict()
        self.assertEqual("trusted", row["details"]["profile"])
        self.assertEqual("env", row["details"]["profile_source"])

    def test_update_recovery_corrupt_journal_fails_doctor_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with patch("mcp_server.diagnostics.update_state_path", return_value=path), \
                    patch("mcp_server.update_state.update_state_path", return_value=path):
                row = diagnostics._check_update_recovery_state().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("UPDATE_STATE_CORRUPT", row["reason_code"])
        self.assertEqual("update_state_corrupt", row["details"]["state_error"])

    def test_update_recovery_incomplete_transaction_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({
                "transaction_version": 1,
                "transaction_id": "upd-test",
                "status": "runtime_synced",
            }), encoding="utf-8")
            with patch("mcp_server.diagnostics.update_state_path", return_value=path):
                row = diagnostics._check_update_recovery_state().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("UPDATE_RECOVERY_REQUIRED", row["reason_code"])
        self.assertEqual("runtime_synced", row["details"]["update_status"])

    def test_update_recovery_failed_transaction_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({
                "transaction_version": 1,
                "transaction_id": "upd-test",
                "status": "recovery_failed",
            }), encoding="utf-8")
            with patch("mcp_server.diagnostics.update_state_path", return_value=path):
                row = diagnostics._check_update_recovery_state().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("UPDATE_RECOVERY_FAILED", row["reason_code"])

    def test_update_recovery_degraded_state_fails_doctor_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({
                "status": "failed",
                "runtime_rollback": {"status": "restore_unverified"},
                "rollback_health": {"status": "failed"},
            }), encoding="utf-8")
            with patch("mcp_server.diagnostics.update_state_path", return_value=path):
                row = diagnostics._check_update_recovery_state().to_dict()
        self.assertEqual("fail", row["status"])
        self.assertEqual("UPDATE_ROLLBACK_DEGRADED", row["reason_code"])
        self.assertEqual("restore_unverified", row["details"]["runtime_rollback_status"])

    def test_update_recovery_verified_rollback_is_not_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({
                "status": "failed",
                "runtime_rollback": {"status": "restored"},
                "rollback_health": {"status": "passed"},
            }), encoding="utf-8")
            with patch("mcp_server.diagnostics.update_state_path", return_value=path):
                row = diagnostics._check_update_recovery_state().to_dict()
        self.assertEqual("warn", row["status"])
        self.assertEqual("UPDATE_LAST_RUN_FAILED", row["reason_code"])

    def test_unreachable_public_endpoint_degrades_doctor_but_not_local_verdict(self) -> None:
        local = diagnostics.result("fixture", "test", "pass", "FIXTURE_OK", "Fixture passed.")
        down = diagnostics.result(
            "public.endpoint", "network", "warn", "PUBLIC_ENDPOINT_UNREACHABLE", "Unreachable.",
        )
        report = diagnostics.build_report([local, down])
        self.assertFalse(report["ok"])
        self.assertTrue(report["local_ok"])
        self.assertEqual("degraded", report["health"])
        self.assertEqual("unavailable", report["public_endpoint"])
        self.assertIn("DEGRADED", diagnostics.format_report(report))

        for argv, expected in ((["doctor", "--json"], 1), (["doctor", "--json", "--local-only"], 0)):
            with self.subTest(argv=argv), patch("mcp_server.diagnostics.run_doctor", return_value=report), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(expected, cli.main(argv))

        local_only = diagnostics.build_report([local, diagnostics.result(
            "public.endpoint", "network", "info", "PUBLIC_ENDPOINT_LOCAL_ONLY", "Local only.",
        )])
        self.assertTrue(local_only["ok"])
        self.assertEqual("healthy", local_only["health"])
        self.assertEqual("local_only", local_only["public_endpoint"])

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
