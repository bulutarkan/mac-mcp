"""Without a terminal the CLI prints exactly what it printed before the theme (#CLI theme phase 1).

The menu app, the installer and scripts read this text, so these tests pin it.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli
from mcp_server.managed_process import ProcessValidation

GOLDEN = Path(__file__).resolve().parent / "golden"
PLAIN_ENV = {"COLUMNS": "100", "TERM": "xterm-256color", "COLORTERM": "truecolor"}


def _public(mode, url=None):
    return type("Public", (), {"mode": mode, "endpoint_url": url})()


class PlainContract(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, PLAIN_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "MAC_MCP_PLAIN"):
            os.environ.pop(name, None)

    def run_status(self, *, server=(4321, "pid_record"), health="ok", public=None, supervisor=None):
        missing = ProcessValidation("missing", None, "server", None, "record_missing")
        out = StringIO()
        with patch.object(cli, "_resolve_server_identity", return_value=server), \
             patch.object(cli, "_validate_managed_pid", return_value=missing), \
             patch.object(cli, "_launchctl_pid", return_value=None), \
             patch.object(cli, "_server_health", return_value=health), \
             patch.object(cli, "CLOUDFLARE_PID_FILE", Path("/nonexistent/mac-mcp/cloudflared.pid")), \
             patch.object(cli, "resolve_public_endpoint", return_value=public or _public("none")), \
             patch.object(cli, "supervisor_report", return_value=supervisor or {"enabled": True, "loaded": True, "intent": "running"}), \
             patch.object(cli, "LOG_FILE", Path("/state/mac-mcp.log")), \
             redirect_stdout(out):
            code = cli.status(argparse.Namespace(json=False, local_only=False))
        return code, out.getvalue()

    def test_status_healthy_local_only(self) -> None:
        code, text = self.run_status()
        self.assertEqual(0, code)
        self.assertEqual(
            "mac-mcp is running (pid 4321; identity verified via pid_record).\n"
            "mac-mcp log: /state/mac-mcp.log\n"
            "public endpoint mode: local only\n"
            "crash supervisor: watching (intent running)\n",
            text,
        )

    def test_status_stopped(self) -> None:
        code, text = self.run_status(server=(None, "not_running"))
        self.assertEqual(1, code)
        self.assertEqual(
            "mac-mcp is not running.\n"
            "public endpoint mode: local only\n"
            "crash supervisor: watching (intent running)\n",
            text,
        )

    def test_status_tunnel_missing(self) -> None:
        code, text = self.run_status(public=_public("cloudflare", "https://mac.example.com/mcp"))
        self.assertEqual(2, code)
        self.assertIn("Cloudflare Tunnel is configured but no verified mac-mcp cloudflared process is running", text)
        self.assertIn("overall: degraded (local server running; selected public endpoint unavailable)\n", text)

    def test_doctor_prints_format_report(self) -> None:
        from mcp_server import diagnostics
        report = {"ok": True, "health": "healthy", "counts": {"pass": 1, "warn": 0, "fail": 0, "info": 0},
                  "checks": [{"check_id": "runtime.python", "category": "runtime", "status": "pass",
                              "summary": "Python 3.14.7 on arm64"}]}
        out = StringIO()
        with patch.object(diagnostics, "run_doctor", return_value=report), redirect_stdout(out):
            code = cli.doctor(argparse.Namespace(json=False, support_bundle=None, local_only=False, verbose=False))
        self.assertEqual(0, code)
        self.assertEqual(diagnostics.format_report(report) + "\n", out.getvalue())
        self.assertTrue(out.getvalue().startswith("Mac MCP Doctor\n==============\n[PASS] runtime.python: "))

    def test_start_prints_its_steps_messages(self) -> None:
        out = StringIO()

        def fake_server(args):
            print("mac-mcp started (pid 4321).")
            return 0

        with patch.object(cli, "_public_endpoint_config", return_value=_public("none")), \
             patch.object(cli, "validate_bootstrap_security"), \
             patch.object(cli, "load_settings"), \
             patch.object(cli, "_record_intent"), \
             patch.object(cli, "_start_server", side_effect=fake_server), \
             patch.object(cli, "_stop_unselected_public_processes"), \
             redirect_stdout(out):
            code = cli.start(argparse.Namespace(host="127.0.0.1", port=8765))
        self.assertEqual(0, code)
        self.assertEqual("mac-mcp started (pid 4321).\npublic endpoint mode: local only\n", out.getvalue())

    def test_stop_prints_not_running(self) -> None:
        out = StringIO()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")), \
             patch.object(cli, "PID_FILE", Path("/nonexistent/mac-mcp/server.pid")), \
             patch.object(cli, "_stop_pid", return_value=True), \
             patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        self.assertEqual(0, code)
        self.assertEqual("mac-mcp is not running.\n", out.getvalue())

    def test_restart_hands_off_and_returns(self) -> None:
        out = StringIO()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", side_effect=lambda args: print("handed off") or 0) as spawn, \
             redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False))
        self.assertEqual((0, "handed off\n"), (code, out.getvalue()))
        spawn.assert_called_once()

    def test_bare_command_is_an_argparse_error(self) -> None:
        err = StringIO()
        with patch.object(sys, "argv", ["mac-mcp"]), redirect_stderr(err), redirect_stdout(StringIO()), \
                self.assertRaises(SystemExit) as stop:
            cli.main([])
        self.assertEqual(2, stop.exception.code)
        self.assertIn("the following arguments are required: command", err.getvalue())

    def test_help_text_is_unchanged(self) -> None:
        out = StringIO()
        # argparse names the program after argv[0]; the installed CLI's is mac-mcp.
        with patch.object(sys, "argv", ["mac-mcp", "--help"]), redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertEqual((GOLDEN / "cli_help.txt").read_text(encoding="utf-8"), out.getvalue())


if __name__ == "__main__":
    unittest.main()
