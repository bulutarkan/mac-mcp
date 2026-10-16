"""The CLI's themed screens, phase 3: dashboard, credential, autostart, recipe, logins, catalog,
conformance, repair and uninstall."""
from __future__ import annotations

import argparse
import io
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli
from mcp_server.cli_style import strip_ansi
from mcp_server.public_endpoint import CloudflareCredentialStatus, PublicEndpointError

NS = argparse.Namespace


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


TTY_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "COLUMNS": "80"}


def visible(text: str) -> str:
    lines = [line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n")]
    return re.sub(r"\d+\.\d+s", "0.0s", "\n".join(lines))


def flat(text: str) -> str:
    return " ".join(text.split())


class ThemedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, TTY_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "MAC_MCP_PLAIN"):
            os.environ.pop(name, None)

    def run_cmd(self, func, args, stdin=None):
        """Run a command with a terminal on stdout; returns (code, visible stdout, stderr, raw stdout)."""
        out, err = FakeTTY(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if stdin is None:
                code = func(args)
            else:
                with patch("sys.stdin", stdin):
                    code = func(args)
        return code, visible(out.getvalue()), err.getvalue(), out.getvalue()


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class DashboardScreen(ThemedTestCase):
    def run_dashboard(self, *, running=True, token="secret-token", opened=True):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            if token:
                path.write_text(token)
            with patch.object(cli, "_resolve_server_identity", return_value=(42, "pid_record") if running else (None, "not_running")), \
                 patch.object(cli, "dashboard_token_path", return_value=path), patch.object(cli, "_default_port", return_value=8765), \
                 patch.object(cli.webbrowser, "open", return_value=opened) as browser:
                result = self.run_cmd(cli.dashboard, NS())
        return (*result, browser)

    def test_opened_never_shows_the_token(self) -> None:
        code, text, _, raw, browser = self.run_dashboard()
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · dashboard\n\n"), text)
        self.assertIn("Address  http://127.0.0.1:8765/dashboard", text)
        self.assertIn("✓ Opened the dashboard in your browser", text)
        self.assertNotIn("secret-token", raw)
        self.assertIn("#token=secret-token", browser.call_args[0][0])

    def test_the_browser_did_not_open(self) -> None:
        code, text, *_ = self.run_dashboard(opened=False)
        self.assertEqual(0, code)
        self.assertIn("⚠ The browser did not open — try: mac-mcp dashboard", flat(text))

    def test_not_running(self) -> None:
        code, text, *_ = self.run_dashboard(running=False)
        self.assertEqual(1, code)
        self.assertIn("✗ Mac MCP is not running — try: mac-mcp start", flat(text))

    def test_token_missing(self) -> None:
        code, text, *_ = self.run_dashboard(token="")
        self.assertEqual(1, code)
        self.assertIn("✗ The dashboard key is missing — try: mac-mcp restart", flat(text))


class CredentialScreen(ThemedTestCase):
    def status(self, configured, secure, reason):
        state = CloudflareCredentialStatus(Path("/t"), configured, secure, reason)
        with patch.object(cli, "inspect_cloudflare_credential", return_value=state):
            return self.run_cmd(cli.credential, NS(provider="cloudflare", action="status"))

    def test_status_states_keep_their_codes(self) -> None:
        code, text, *_ = self.status(True, True, "ok")
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · credential\n\n"), text)
        self.assertIn("● Cloudflare Tunnel  saved · owner-only file", text)
        self.assertIn("✓ The Cloudflare Tunnel credential is saved", text)
        code, text, *_ = self.status(False, False, "missing")
        self.assertEqual(1, code)
        self.assertIn("● Cloudflare Tunnel  not saved", text)
        self.assertIn("⚠ No Cloudflare Tunnel credential is saved — try: mac-mcp credential cloudflare save", flat(text))
        code, text, *_ = self.status(True, False, "mode_0644")
        self.assertEqual(2, code)
        self.assertIn("● Cloudflare Tunnel  saved but unsafe · mode_0644", text)
        self.assertIn("✗ The saved credential is not safe", text)

    def test_save_from_a_terminal_is_hidden_and_never_printed(self) -> None:
        with patch.object(cli, "write_cloudflare_token") as write, \
             patch("getpass.getpass", return_value="tok-secret") as ask:
            code, text, err, raw = self.run_cmd(cli.credential, NS(provider="cloudflare", action="save"), stdin=TTYInput(""))
        self.assertEqual(0, code)
        ask.assert_called_once()
        write.assert_called_once_with("tok-secret")
        self.assertNotIn("tok-secret", raw + err)
        self.assertIn("✓ Saved the Cloudflare Tunnel credential", text)

    def test_save_from_a_pipe_reads_stdin(self) -> None:
        with patch.object(cli, "write_cloudflare_token") as write, patch("getpass.getpass") as ask:
            code, text, *_ = self.run_cmd(cli.credential, NS(provider="cloudflare", action="save"), stdin=io.StringIO("tok-1\n"))
        self.assertEqual(0, code)
        ask.assert_not_called()
        write.assert_called_once_with("tok-1\n")

    def test_save_error(self) -> None:
        with patch.object(cli, "write_cloudflare_token", side_effect=PublicEndpointError("token is empty")):
            code, text, *_ = self.run_cmd(cli.credential, NS(provider="cloudflare", action="save"), stdin=io.StringIO(""))
        self.assertEqual(2, code)
        self.assertIn("✗ Could not save the credential: token is empty", flat(text))

    def test_remove(self) -> None:
        with patch.object(cli, "remove_cloudflare_token", return_value=True):
            code, text, *_ = self.run_cmd(cli.credential, NS(provider="cloudflare", action="remove"))
        self.assertEqual(0, code)
        self.assertIn("✓ Removed the Cloudflare Tunnel credential", text)
        with patch.object(cli, "remove_cloudflare_token", return_value=False):
            code, text, *_ = self.run_cmd(cli.credential, NS(provider="cloudflare", action="remove"))
        self.assertEqual(0, code)
        self.assertIn("ℹ No Cloudflare Tunnel credential was saved", text)


class AutostartScreen(ThemedTestCase):
    def run_autostart(self, on, state=None, json=False):
        with patch.object(cli, "_start_at_login", return_value=on), patch.object(cli, "_supervisor_loaded", return_value=True), \
             patch.object(cli, "update_runtime_setting"), patch.object(cli, "_ensure_supervisor"):
            return self.run_cmd(cli.autostart, NS(state=state, json=json))

    def test_on(self) -> None:
        code, text, *_ = self.run_autostart(True)
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · autostart\n\n"), text)
        self.assertIn("● Start at login  on · a server you left running starts again when you log in", flat(text) + text)
        self.assertIn("✓ Start at login is on", text)

    def test_off_names_the_command(self) -> None:
        code, text, *_ = self.run_autostart(False)
        self.assertEqual(0, code)
        self.assertIn("● Start at login  off", text)
        self.assertIn("✓ Start at login is off — try: mac-mcp autostart on", flat(text))

    def test_save_error(self) -> None:
        with patch.object(cli, "update_runtime_setting", side_effect=RuntimeError("locked")):
            code, text, err, _ = self.run_cmd(cli.autostart, NS(state="on", json=False))
        self.assertEqual((1, ""), (code, err))
        self.assertIn("✗ Could not save the setting: locked", flat(text))

    def test_json_stays_json(self) -> None:
        import json
        code, _, _, raw = self.run_autostart(True, json=True)
        self.assertEqual(0, code)
        self.assertTrue(json.loads(raw)["start_at_login"])


if __name__ == "__main__":
    unittest.main()
