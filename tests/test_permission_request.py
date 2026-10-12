from __future__ import annotations

import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import permission_probe as probe
from mcp_server import permission_request as requester


class PermissionHolderTests(unittest.TestCase):
    """macOS asks for and lists permissions under the responsible process."""

    def bundle(self, root: Path, name: str, identifier: str, display: str) -> Path:
        exe = root / f"{name}.app" / "Contents" / "MacOS" / "bin"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        (exe.parent.parent / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleIdentifier": identifier, "CFBundleDisplayName": display, "CFBundleName": display}))
        return exe

    def test_mac_mcp_server_is_listed_as_mac_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = self.bundle(Path(td), "Mac MCP Server", "com.bulutarkan.mac-mcp.server", "Mac MCP")
            with patch.object(probe, "_responsible_executable", return_value=exe):
                identity = probe.process_identity()
        self.assertEqual("Mac MCP", identity["listed_as"])
        self.assertEqual("mac_mcp", identity["holder"])

    def test_python_is_reported_as_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = self.bundle(Path(td), "Python", "org.python.python", "Python")
            with patch.object(probe, "_responsible_executable", return_value=exe):
                identity = probe.process_identity()
        self.assertEqual("Python", identity["listed_as"])
        self.assertEqual("other", identity["holder"])

    def test_responsible_executable_of_this_process_is_found(self) -> None:
        self.assertTrue(probe._responsible_executable().exists())


class PermissionRequestTests(unittest.TestCase):
    def test_accessibility_prompt_then_state(self) -> None:
        with patch.object(requester, "_prompt_accessibility") as prompt, \
                patch.object(requester, "accessibility_state", return_value=probe.NOT_DETERMINED):
            result = requester.request_permission("accessibility")
        prompt.assert_called_once()
        self.assertEqual({"permission": "accessibility", "state": "not_determined", "restart_needed": False}, result)

    def test_screen_recording_needs_a_restart_until_it_takes_effect(self) -> None:
        with patch.object(requester, "_request_screen_capture", return_value=True) as request, \
                patch.object(requester, "screen_recording_state", return_value=probe.DENIED):
            result = requester.request_permission("screen_recording")
        request.assert_called_once()
        self.assertTrue(result["restart_needed"])

    def test_unknown_permissions_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            requester.request_permission("microphone")


class PermissionRouteTests(unittest.TestCase):
    TOKEN = "permission-route-token"

    def test_route_requests_a_known_permission(self) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        td = tempfile.mkdtemp(prefix="mac-mcp-permission-route-")
        self.addCleanup(shutil.rmtree, td, True)
        app = Starlette(routes=create_dashboard_routes(
            TelemetryManager(db_path=Path(td) / "t.sqlite3"), load_settings(), self.TOKEN))
        client = TestClient(app)
        auth = {"authorization": f"Bearer {self.TOKEN}"}
        self.assertEqual(401, client.post("/dashboard/api/permissions/request", json={"permission": "accessibility"}).status_code)
        self.assertEqual(400, client.post("/dashboard/api/permissions/request", json={"permission": "camera"}, headers=auth).status_code)
        reply = {"permission": "accessibility", "state": "granted", "restart_needed": False}
        with patch("mcp_server.dashboard_routes.request_permission", return_value=reply) as request:
            response = client.post("/dashboard/api/permissions/request", json={"permission": "accessibility"}, headers=auth)
        self.assertEqual(200, response.status_code)
        self.assertEqual("granted", response.json()["state"])
        request.assert_called_once_with("accessibility")


if __name__ == "__main__":
    unittest.main()
