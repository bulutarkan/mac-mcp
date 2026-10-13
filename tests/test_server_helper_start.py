"""`mac-mcp start` runs the server under Mac MCP Server for the default install."""
from __future__ import annotations

import argparse
import io
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import cli


class HelperSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="mac-mcp-helper-select-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        self.helper = Path("/rt/.helper/Mac MCP Server.app/Contents/MacOS/mac-mcp-server")

    def pick(self, *, default=True, reload=False, env=None, helper=True):
        environ = {k: v for k, v in os.environ.items() if k != "MAC_MCP_SERVER_HELPER"}
        environ.update(env or {})
        with patch.object(cli, "STATE_DIR", self.state), \
                patch.object(cli, "DEFAULT_STATE_DIR", self.state if default else self.state / "other"), \
                patch("mcp_server.server_helper.helper_executable", return_value=self.helper if helper else None), \
                patch.dict(os.environ, environ, clear=True):
            return cli._server_helper_for_start(argparse.Namespace(reload=reload))

    def test_default_install_with_a_helper_uses_it(self) -> None:
        self.assertEqual(self.helper, self.pick())

    def test_isolated_reload_disabled_or_missing_helper_start_directly(self) -> None:
        self.assertIsNone(self.pick(default=False))
        self.assertIsNone(self.pick(reload=True))
        self.assertIsNone(self.pick(env={"MAC_MCP_SERVER_HELPER": "0"}))
        self.assertIsNone(self.pick(helper=False))


class HelperStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="mac-mcp-helper-start-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        self.launchctl: list[tuple] = []

    def record(self, *args, **_kwargs):
        self.launchctl.append(args)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def start(self, *, child=4242, healthy=True):
        helper = Path("/rt/.helper/Mac MCP Server.app/Contents/MacOS/mac-mcp-server")
        written = {}
        snapshot = SimpleNamespace(pid=child)
        out = io.StringIO()
        with patch.object(cli, "STATE_DIR", self.state), patch.object(cli, "LOG_FILE", self.state / "mac-mcp.log"), \
                patch.object(cli, "PID_FILE", self.state / "mac-mcp.pid"), \
                patch.object(cli, "_launchctl_run", side_effect=self.record), \
                patch.object(cli, "_launchctl_pid", return_value=900), \
                patch("mcp_server.server_helper.server_child_pid", return_value=child), \
                patch.object(cli, "process_snapshot", return_value=snapshot), \
                patch.object(cli, "matches_role", return_value=True), \
                patch.object(cli, "write_process_record", side_effect=lambda *a, **k: written.update(pid=a[2], meta=k.get("metadata"))), \
                patch.object(cli, "_restart_health_ok", return_value=healthy), \
                patch.object(cli, "_startup_health_timeout_s", return_value=2.0), redirect_stdout(out):
            code = cli._start_server_via_helper(
                argparse.Namespace(host="127.0.0.1"), 8765, helper,
                ["/py", "-m", "uvicorn", "mcp_server.main:app", "--port", "8765"],
                {"PATH": "/usr/bin", "MCP_API_KEY": "secret", "MAC_MCP_PORT": "8765"},
            )
        return code, written, out.getvalue()

    def test_starts_the_job_and_records_the_python_child(self) -> None:
        code, written, text = self.start()
        self.assertEqual(0, code, text)
        self.assertEqual(4242, written["pid"])
        self.assertEqual("helper", written["meta"]["ownership_source"])
        plist = plistlib.loads((self.state / "server.plist").read_bytes())
        self.assertEqual("com.macmcp.server", plist["Label"])
        self.assertNotIn("MCP_API_KEY", plist["EnvironmentVariables"])
        self.assertEqual(0o600, (self.state / "server.plist").stat().st_mode & 0o777)
        self.assertIn("bootstrap", [call[0] for call in self.launchctl])
        self.assertIn("Mac MCP", text)

    def test_unhealthy_start_unloads_the_job(self) -> None:
        code, _written, _text = self.start(healthy=False)
        self.assertEqual(1, code)
        self.assertEqual("bootout", self.launchctl[-1][0])

    def test_server_shutdown_is_bounded(self) -> None:
        # Open MCP streams must not hold a stop, restart or update open indefinitely.
        cmd = cli._server_command(argparse.Namespace(host="127.0.0.1", reload=False), 8765)
        self.assertEqual(str(cli.GRACEFUL_SHUTDOWN_S), cmd[cmd.index("--timeout-graceful-shutdown") + 1])
        self.assertIn("mcp_server.main:app", cmd)
        # The stop/restart wait must outlast the whole shutdown, or a stop gives up on a server
        # that no longer listens but has not exited yet (2026-10-13 outage).
        self.assertGreaterEqual(cli.STOP_TIMEOUT_S, cli.GRACEFUL_SHUTDOWN_S * 3)

    def test_stop_unloads_the_server_job_for_the_default_install(self) -> None:
        with patch.object(cli, "STATE_DIR", self.state), patch.object(cli, "DEFAULT_STATE_DIR", self.state), \
                patch.object(cli, "PID_FILE", self.state / "mac-mcp.pid"), patch.object(cli, "_load_env"), \
                patch.object(cli, "_record_intent"), patch.object(cli, "_default_port", return_value=8765), \
                patch.object(cli, "_resolve_server_identity", return_value=(4242, "pid_record")), \
                patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True), \
                patch.object(cli, "_launchctl_run", side_effect=self.record), redirect_stdout(io.StringIO()):
            (self.state / "server.plist").write_text("x")
            self.assertEqual(0, cli.stop(argparse.Namespace(timeout=1, force=True)))
        self.assertIn(("bootout", f"gui/{os.getuid()}/com.macmcp.server"), [call[:2] for call in self.launchctl])
        self.assertFalse((self.state / "server.plist").exists())


if __name__ == "__main__":
    unittest.main()
