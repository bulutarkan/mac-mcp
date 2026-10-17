"""Without a terminal, the phase 2 commands print exactly what they printed before the theme.

The menu app reads connect-config and update text; scripts read the rest.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, client_setup, privacy_inventory
from mcp_server.update_helper import UpdateInfo

APPS = [
    {"id": "codex", "name": "Codex", "installed": True, "added": True, "keyless": True},
    {"id": "claude-desktop", "name": "Claude Desktop", "installed": True, "added": True, "keyless": False},
    {"id": "kimi", "name": "Kimi Code", "installed": True, "added": False, "keyless": None},
    {"id": "goose", "name": "Goose", "installed": False, "added": False, "keyless": None},
]


def info(**changes) -> UpdateInfo:
    base = dict(repo="/r", runtime="/rt", branch="main", remote="origin", deployed_commit="05b6e68c1111",
                repo_commit="aaaa", target_commit="05b6e68c1111", behind_by=0, update_available=False, dirty=False)
    base.update(changes)
    return UpdateInfo(**base)


class PlainPhase2(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"COLUMNS": "100", "TERM": "xterm-256color"})
        env.start()
        self.addCleanup(env.stop)

    def capture(self, func, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()

    def test_clients(self) -> None:
        with patch.object(client_setup, "apps_status", return_value=APPS):
            code, out, _ = self.capture(cli.clients, argparse.Namespace(json=False))
        self.assertEqual(0, code)
        self.assertEqual(
            "Codex: added\n"
            "Claude Desktop: added (old setup with the key in its settings; run mac-mcp connect to update)\n"
            "Kimi Code: installed\n"
            "Goose: not installed\n", out)

    def test_connect_success_and_failure(self) -> None:
        with patch.object(client_setup, "connect", return_value={"ok": True, "name": "Codex", "next": "Start a new Codex session."}):
            code, out, _ = self.capture(cli.connect_app, argparse.Namespace(client="codex", json=False))
        self.assertEqual((0, "Mac MCP is added to Codex. Start a new Codex session.\n"), (code, out))
        with patch.object(client_setup, "connect", side_effect=client_setup.SetupError("Kimi Code is not installed on this Mac.")):
            code, out, err = self.capture(cli.connect_app, argparse.Namespace(client="kimi", json=False))
        self.assertEqual((1, "", "mac-mcp connect: Kimi Code is not installed on this Mac.\n"), (code, out, err))

    def test_connect_config(self) -> None:
        settings = type("S", (), {"allow_no_auth": False, "api_key": "not-printed"})()
        with patch.object(cli, "_connect_config_endpoint", return_value=("http://127.0.0.1:8765/mcp", "local")), \
             patch.object(cli, "load_settings", return_value=settings):
            code, out, _ = self.capture(cli.connect_config, argparse.Namespace(
                client="codex", endpoint="auto", name="mac-mcp", auth_env="MAC_MCP_API_KEY"))
        self.assertEqual(0, code)
        self.assertTrue(out.startswith("Client: codex\nTarget: Codex CLI config.toml"), out[:80])
        self.assertIn("\n\n[mcp_servers.mac-mcp]\nurl = \"http://127.0.0.1:8765/mcp\"\n", out)
        self.assertIn("\nSecret setup:\n", out)
        self.assertNotIn("not-printed", out)

    def test_update_check_states(self) -> None:
        from mcp_server.update_helper import format_check
        for case in (info(), info(update_available=True, behind_by=2, target_commit="bbbbbbbb2222", release_id="stable-2.2.1-r1",
                                  release_version="2.2.1"),
                     info(dirty=True), info(unverified_ahead=49)):
            with self.subTest(case=case), patch.object(cli, "install_kind", return_value="git"), \
                    patch.object(cli, "validate_update_state"), patch.object(cli, "check_update", return_value=case):
                code, out, _ = self.capture(cli.update, argparse.Namespace(
                    check=True, json=False, repo=None, runtime=None, branch="main", remote="origin"))
            self.assertEqual(format_check(case) + "\n", out)
            self.assertEqual(2 if case.dirty else 0, code)

    def test_update_pkg(self) -> None:
        with patch.object(cli, "install_kind", return_value="pkg"), \
                patch("mcp_server.install_kind._fetch_latest_release", side_effect=OSError("offline in tests")):
            code, out, _ = self.capture(cli.update, argparse.Namespace(check=True, json=False))
        self.assertEqual(0, code)
        self.assertTrue(out.startswith("[mac-mcp update] "), out)

    def test_logs_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "mac-mcp.log"
            log.write_text("one\ntwo\nthree\n")
            updates = Path(temp) / "update"
            updates.mkdir()
            with patch.object(cli, "managed_logs", return_value={"server": log}), \
                 patch.object(cli, "update_logs_dir", return_value=updates), \
                 patch.object(cli, "log_limits", return_value={"max_bytes": 10 * 1024 * 1024, "backups": 3, "update_logs_kept": 20}):
                code, out, _ = self.capture(cli.logs, argparse.Namespace(component="server", lines=2, list=False))
                self.assertEqual((0, f"== {log} (last 2 lines, secrets redacted)\ntwo\nthree\n"), (code, out))
                code, out, _ = self.capture(cli.logs, argparse.Namespace(component="server", lines=2, list=True))
                self.assertEqual(0, code)
                self.assertEqual(
                    f"server            0.0 MB  {log}\n"
                    f"update            0.0 MB  {updates}\n"
                    "bounds: 10 MB x 4 files per log, audit 5 MB x 4, newest 20 update logs\n", out)
                log.unlink()
                code, out, err = self.capture(cli.logs, argparse.Namespace(component="server", lines=2, list=False))
                self.assertEqual((1, "", f"No server log yet ({log}).\n"), (code, out, err))

    def test_privacy_delete_preview_and_yes(self) -> None:
        plan = [{"key": "memory", "name": "Memory", "exists": True, "bytes": 2_097_152, "paths": ["/s/memory"]},
                {"key": "lessons", "name": "Lessons", "exists": False, "bytes": 0, "paths": ["/s/lessons"]}]
        with patch.object(privacy_inventory, "delete_plan", return_value=plan):
            code, out, _ = self.capture(cli.privacy_delete, argparse.Namespace(all=False, store=["memory", "lessons"], yes=False, json=False))
        self.assertEqual(0, code)
        self.assertEqual(
            "  Memory: 2.0 MB (/s/memory)\n"
            "  Lessons: empty (/s/lessons)\n"
            "\nNothing was changed. Mac MCP stops while the files move to the Trash, then starts again.\n"
            "To do this, run: mac-mcp privacy delete --store memory --store lessons --yes\n", out)
        result = {"moved": [{"path": "/s/memory", "to": "/T/memory"}], "failed": [{"path": "/s/x", "error": "busy"}]}
        with patch.object(privacy_inventory, "delete_plan", return_value=plan), \
             patch.object(privacy_inventory, "trash_stores", return_value=result), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")):
            code, out, _ = self.capture(cli.privacy_delete, argparse.Namespace(all=False, store=["memory"], yes=True, json=False))
        self.assertEqual(1, code)
        self.assertEqual("Moved /s/memory -> /T/memory\nCould not move /s/x (busy)\n", out)
        with patch.object(privacy_inventory, "delete_plan", return_value=plan):
            code, out, err = self.capture(cli.privacy_delete, argparse.Namespace(all=False, store=None, yes=False, json=False))
        self.assertEqual(2, code)
        self.assertTrue(err.startswith("Choose --all or --store from: "), err)


if __name__ == "__main__":
    unittest.main()
