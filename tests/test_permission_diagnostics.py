from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import diagnostics, managed_process, permission_probe as pp
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.managed_process import ProcessSnapshot
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "permission-diagnostics-test-token-0123456789"


def payload(ax="granted", screen="granted", targets=None):
    targets = targets if targets is not None else [{"app": "Safari", "bundle_id": "com.apple.Safari", "state": "granted"}]
    with patch.object(pp, "accessibility_state", return_value=ax), \
         patch.object(pp, "screen_recording_state", return_value=screen), \
         patch.object(pp, "automation_state", side_effect=lambda bundle: next(
             (t["state"] for t in targets if t["bundle_id"] == bundle), pp.NOT_RUNNING)), \
         patch.object(pp, "process_identity", return_value={"pid": 1, "executable": "/x/Python.app/Contents/MacOS/Python", "listed_as": "Python"}):
        return pp.probe_permissions()


class FakeCoreServices:
    def __init__(self, status):
        self.status = status
        self.ask_flags = []

        def create(_code, _data, _size, desc_ref):
            return 0

        def determine(_desc, _cls, _id, ask):
            self.ask_flags.append(ask)
            return self.status

        self.AECreateDesc = create
        self.AEDeterminePermissionToAutomateTarget = determine
        self.AEDisposeDesc = lambda _desc: 0


class PermissionProbeTests(unittest.TestCase):
    def test_automation_never_asks_and_maps_every_result(self) -> None:
        for status, expected in ((0, "granted"), (-1743, "denied"), (-1744, "not_determined"), (-600, "not_running"), (-50, "unknown")):
            fake = FakeCoreServices(status)
            with self.subTest(status=status), patch.object(pp, "_library", return_value=fake), \
                 patch.object(ctypes, "byref", side_effect=lambda value: value):
                self.assertEqual(expected, pp.automation_state("com.apple.Safari"))
            self.assertEqual([False], fake.ask_flags, "the probe must never prompt")

    def test_probe_reports_unknown_where_frameworks_are_unavailable(self) -> None:
        with patch.object(pp, "_library", return_value=None):
            self.assertEqual("unknown", pp.accessibility_state())
            self.assertEqual("unknown", pp.screen_recording_state())
            self.assertEqual("unknown", pp.automation_state("com.apple.Safari"))

    def test_automation_summary_prefers_denied_then_not_asked(self) -> None:
        denied = payload(targets=[
            {"app": "Safari", "bundle_id": "com.apple.Safari", "state": "granted"},
            {"app": "Calendar", "bundle_id": "com.apple.iCal", "state": "denied"},
        ])
        self.assertEqual("denied", denied["permissions"]["automation"]["state"])
        self.assertEqual("unknown", payload(targets=[])["permissions"]["automation"]["state"])
        mic = payload()["permissions"]["microphone"]
        self.assertEqual(("unknown", "Mac MCP Voice Helper"), (mic["state"], mic["identity"]["name"]))
        for entry in payload()["permissions"].values():
            self.assertTrue(entry["features"])
            self.assertTrue(entry["settings_url"].startswith("x-apple.systempreferences:"))


class PermissionRowTests(unittest.TestCase):
    def rows(self, data, *, server=True):
        with patch.object(diagnostics, "_server_permissions", return_value=data if server else None), \
             patch.object(diagnostics, "probe_permissions", return_value=data):
            return {row.check_id: row for row in diagnostics._permission_rows()}

    def test_server_permissions_map_to_statuses_with_recovery(self) -> None:
        rows = self.rows(payload(ax="denied", screen="denied", targets=[
            {"app": "Safari", "bundle_id": "com.apple.Safari", "state": "denied"},
            {"app": "Notes", "bundle_id": "com.apple.Notes", "state": "granted"},
        ]))
        ax = rows["permissions.accessibility"]
        self.assertEqual(("fail", "ACCESSIBILITY_DISABLED"), (ax.status, ax.reason_code))
        self.assertIn("Allow “Python”", ax.remediation)
        self.assertEqual("open_system_settings", ax.details["recovery"]["action"])
        self.assertEqual("server", ax.details["context"])
        self.assertEqual(("warn", "SCREEN_RECORDING_DENIED"), (rows["permissions.screen_recording"].status, rows["permissions.screen_recording"].reason_code))
        automation = rows["permissions.automation"]
        self.assertEqual("AUTOMATION_DENIED", automation.reason_code)
        self.assertIn("turn on Safari", automation.remediation)
        self.assertEqual(("info", "MICROPHONE_NOT_CHECKED"), (rows["permissions.microphone"].status, rows["permissions.microphone"].reason_code))
        self.assertIn("Mac MCP Voice Helper", rows["permissions.microphone"].summary)

    def test_without_a_server_the_terminal_is_described_and_not_failed(self) -> None:
        rows = self.rows(payload(ax="denied"), server=False)
        ax = rows["permissions.accessibility"]
        self.assertEqual("warn", ax.status)
        self.assertEqual("doctor_process", ax.details["context"])
        self.assertIn("for this terminal", ax.summary)

    def test_not_asked_and_unknown_are_not_reported_as_denied(self) -> None:
        rows = self.rows(payload(ax="unknown", screen="unknown", targets=[
            {"app": "Calendar", "bundle_id": "com.apple.iCal", "state": "not_determined"}]))
        self.assertEqual(("info", "ACCESSIBILITY_UNKNOWN"), (rows["permissions.accessibility"].status, rows["permissions.accessibility"].reason_code))
        self.assertEqual(("info", "AUTOMATION_NOT_ASKED_YET"), (rows["permissions.automation"].status, rows["permissions.automation"].reason_code))
        rows = self.rows(payload(targets=[]))
        self.assertEqual("AUTOMATION_NOT_CHECKED", rows["permissions.automation"].reason_code)

    def test_doctor_lists_all_four_permissions(self) -> None:
        with patch.object(diagnostics, "_server_permissions", return_value=payload()):
            ids = [row.check_id for row in diagnostics._permission_rows()]
        self.assertEqual(["permissions.accessibility", "permissions.screen_recording",
                          "permissions.automation", "permissions.microphone"], ids)


class PortAndTunnelRecoveryTests(unittest.TestCase):
    def test_port_conflict_names_the_program_and_never_offers_to_kill_it(self) -> None:
        snapshot = ProcessSnapshot(4242, "start", "/Applications/Docker.app/Contents/MacOS/com.docker.backend", "docker", "/")
        with patch.object(managed_process, "process_snapshot", return_value=snapshot):
            owner = managed_process.listener_owner(4242)
        self.assertEqual("Docker", owner["name"])
        advice = managed_process.port_conflict_advice(8765, [owner])
        self.assertIn("Docker (pid 4242)", advice)
        self.assertIn("never stops programs it did not start", advice)
        self.assertIn("Settings > Advanced > Server Port", advice)
        self.assertIn("Activity Monitor", managed_process.port_conflict_advice(8765, [{"pid": 7, "name": None}]))
        self.assertIn("lsof -nP -iTCP:8765", managed_process.port_conflict_advice(8765, []))

    def test_doctor_port_conflict_row_names_owner_and_links_advanced(self) -> None:
        snapshot = ProcessSnapshot(4242, "start", "/usr/local/bin/node", "node server.js", "/")
        with patch("mcp_server.diagnostics.listener_pids", return_value=[4242]), \
             patch("mcp_server.diagnostics.process_snapshot", return_value=snapshot), \
             patch("mcp_server.diagnostics.matches_role", return_value=False), \
             patch.object(managed_process, "process_snapshot", return_value=snapshot), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=None), \
             patch("mcp_server.diagnostics._local_host_port", return_value=("127.0.0.1", 8765)), \
             patch("mcp_server.diagnostics.state_dir", return_value=Path("/nonexistent/mac-mcp-test-state")):
            row = diagnostics._check_managed_process("server")
        self.assertEqual("SERVER_PORT_FOREIGN_LISTENER", row.reason_code)
        self.assertIn("node", row.summary)
        self.assertEqual("node", row.details["foreign_listeners"][0]["name"])
        self.assertEqual({"action": "open_settings", "pane": "advanced"}, row.details["recovery"])

    def test_selected_tunnel_problems_are_specific(self) -> None:
        cloudflare = type("Public", (), {"mode": "cloudflare", "endpoint_url": "https://mac.example.com/mcp"})()
        missing = type("Resolved", (), {"path": None, "source": "path"})()
        with patch.object(diagnostics, "resolve_public_endpoint", return_value=cloudflare), \
             patch.object(diagnostics, "resolve_cloudflared_binary", return_value=missing):
            row = diagnostics._check_cloudflared_dependency()
        self.assertEqual(("fail", "CLOUDFLARED_MISSING_FOR_SELECTED_MODE"), (row.status, row.reason_code))
        self.assertIn("brew install cloudflared", row.remediation)

        stopped = diagnostics.result("process.cloudflared", "process", "info", "CLOUDFLARED_PROCESS_NOT_DETECTED", "x")
        with patch.object(diagnostics, "resolve_public_endpoint", return_value=cloudflare), \
             patch.object(diagnostics, "_check_managed_process", return_value=stopped):
            row = diagnostics._check_cloudflare_for_selected_mode()
        self.assertEqual(("warn", "CLOUDFLARED_STOPPED"), (row.status, row.reason_code))
        self.assertEqual({"action": "restart"}, row.details["recovery"])

    def test_public_route_advice_points_at_the_failing_component(self) -> None:
        running = diagnostics.result("process.cloudflared", "process", "pass", "CLOUDFLARED_RUNNING_VERIFIED", "x")
        stopped = diagnostics.result("process.cloudflared", "process", "warn", "CLOUDFLARED_STOPPED", "x")
        url = "https://mac.example.com/health"
        with patch.object(diagnostics, "_local_host_port", return_value=("127.0.0.1", 8765)):
            with patch.object(diagnostics, "port_is_listening", return_value=False):
                self.assertEqual({"action": "restart"}, diagnostics._public_route_advice("cloudflare", url)[1])
            with patch.object(diagnostics, "port_is_listening", return_value=True):
                with patch.object(diagnostics, "_check_managed_process", return_value=stopped):
                    advice, recovery = diagnostics._public_route_advice("cloudflare", url)
                    self.assertEqual({"action": "restart"}, recovery)
                with patch.object(diagnostics, "_check_managed_process", return_value=running):
                    advice, recovery = diagnostics._public_route_advice("cloudflare", url)
                self.assertEqual({"action": "view_logs", "log": "cloudflared"}, recovery)
                self.assertIn("Public hostname", advice)
                advice, recovery = diagnostics._public_route_advice("custom", url)
                self.assertIn("reverse proxy", advice)


class PermissionEndpointTests(unittest.TestCase):
    def test_endpoint_requires_the_dashboard_token(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))
            self.assertEqual(401, client.get("/dashboard/api/diagnostics/permissions").status_code)
            with patch("mcp_server.dashboard_routes.probe_permissions", return_value=payload()):
                response = client.get("/dashboard/api/diagnostics/permissions", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(200, response.status_code)
        self.assertEqual("granted", response.json()["permissions"]["accessibility"]["state"])


if __name__ == "__main__":
    unittest.main()
