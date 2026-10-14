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
    """Without Mac MCP Server (a runtime with no helper) the server asks macOS itself."""

    def setUp(self) -> None:
        # Never the real runtime: its helper would really open a macOS prompt.
        self.runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-no-helper-"))
        self.addCleanup(shutil.rmtree, self.runtime, True)

    def test_accessibility_prompt_then_state(self) -> None:
        with patch.object(requester, "_prompt_accessibility") as prompt, \
                patch.object(requester, "accessibility_state", return_value=probe.NOT_DETERMINED):
            result = requester.request_permission("accessibility", runtime=self.runtime)
        prompt.assert_called_once()
        self.assertEqual({"permission": "accessibility", "state": "not_determined", "restart_needed": False}, result)

    def test_screen_recording_needs_a_restart_until_it_takes_effect(self) -> None:
        with patch.object(requester, "_request_screen_capture", return_value=True) as request, \
                patch.object(requester, "screen_recording_state", return_value=probe.DENIED):
            result = requester.request_permission("screen_recording", runtime=self.runtime)
        request.assert_called_once()
        self.assertTrue(result["restart_needed"])

    def test_unknown_permissions_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            requester.request_permission("microphone", runtime=self.runtime)


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


class HelperPermissionTests(unittest.TestCase):
    """With Mac MCP Server installed, it asks for and reports permissions (fresh, never cached)."""

    def setUp(self) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-helper-perm-"))
        self.addCleanup(shutil.rmtree, self.runtime, True)
        self.exe = self.runtime / ".helper" / "Mac MCP Server.app" / "Contents" / "MacOS" / "mac-mcp-server"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(0o755)
        self.calls: list[list[str]] = []
        self.answers = {"accessibility": "granted", "screen-recording": "denied"}

    def fake_run(self, cmd, **_kwargs):
        self.calls.append([str(c) for c in cmd])
        out = self.answers.get(cmd[-1], "") if "--check" in cmd else ""
        return __import__("subprocess").CompletedProcess(cmd, 0 if out == "granted" else 1, out + "\n", "")

    def test_fresh_state_asks_a_new_helper_process(self) -> None:
        self.assertEqual("granted", requester.fresh_state("accessibility", runtime=self.runtime, run=self.fake_run))
        self.assertEqual("denied", requester.fresh_state("screen_recording", runtime=self.runtime, run=self.fake_run))
        self.assertEqual([str(self.exe), "--check", "screen-recording"], self.calls[-1])

    def test_request_opens_the_helper_as_an_app_so_macos_shows_its_prompt(self) -> None:
        with patch.object(requester, "accessibility_state", return_value=probe.DENIED):
            result = requester.request_permission("accessibility", runtime=self.runtime, run=self.fake_run)
        opened = self.calls[0]
        self.assertEqual(["/usr/bin/open", "-n", "-W", "-a", str(self.exe.parents[2])], opened[:5])
        self.assertEqual(["--args", "--request", "accessibility"], opened[5:])
        self.assertEqual("granted", result["state"])
        self.assertTrue(result["restart_needed"], "granted to Mac MCP, but this process only sees it after a restart")

    def test_status_reports_fresh_states_and_whether_a_restart_is_needed(self) -> None:
        with patch.object(requester, "accessibility_state", return_value=probe.GRANTED), \
                patch.object(requester, "screen_recording_state", return_value=probe.DENIED):
            status = requester.permission_status(runtime=self.runtime, run=self.fake_run)
        self.assertEqual({"accessibility": "granted", "screen_recording": "denied", "restart_needed": False}, status)
        self.answers["screen-recording"] = "granted"
        with patch.object(requester, "accessibility_state", return_value=probe.GRANTED), \
                patch.object(requester, "screen_recording_state", return_value=probe.DENIED):
            status = requester.permission_status(runtime=self.runtime, run=self.fake_run)
        self.assertTrue(status["restart_needed"])

    def test_a_helper_that_cannot_tell_is_unknown_not_denied(self) -> None:
        def broken(cmd, **_kwargs):
            self.calls.append([str(c) for c in cmd])
            return __import__("subprocess").CompletedProcess(cmd, 64, "", "unknown permission\n")
        self.assertEqual(probe.UNKNOWN, requester.fresh_state("accessibility", runtime=self.runtime, run=broken))

    def test_a_prompt_macos_could_not_show_says_where_to_allow_it(self) -> None:
        def open_fails(cmd, **kwargs):
            if cmd[0] == "/usr/bin/open":
                raise OSError("LaunchServices refused to open the helper")
            return self.fake_run(cmd, **kwargs)
        with patch.object(requester, "accessibility_state", return_value=probe.DENIED):
            result = requester.request_permission("screen_recording", runtime=self.runtime, run=open_fails)
        self.assertEqual("denied", result["state"])
        self.assertIn("could not show the permission prompt", result["prompt_error"])
        self.assertIn("Screen & System Audio Recording", result["prompt_error"])
        with patch.object(requester, "accessibility_state", return_value=probe.DENIED):
            shown = requester.request_permission("screen_recording", runtime=self.runtime, run=self.fake_run)
        self.assertNotIn("prompt_error", shown, "the owner saying no is not an error")

    def test_without_the_helper_the_server_asks_itself(self) -> None:
        shutil.rmtree(self.runtime / ".helper")
        with patch.object(requester, "_prompt_accessibility") as prompt, \
                patch.object(requester, "accessibility_state", return_value=probe.GRANTED):
            result = requester.request_permission("accessibility", runtime=self.runtime, run=self.fake_run)
        prompt.assert_called_once()
        self.assertEqual([], self.calls)
        self.assertEqual("granted", result["state"])
