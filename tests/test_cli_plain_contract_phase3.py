"""Without a terminal, the phase 3 commands print exactly what they printed before the theme.

The menu app reads autostart, credential and logins (JSON or exit code); the installer pipes the
Cloudflare token into credential save; scripts read the rest.
"""
from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mcp_server import catalog, cli, lifecycle, login_vault
from mcp_server.public_endpoint import CloudflareCredentialStatus, PublicEndpointError

NS = argparse.Namespace

REVIEW = {
    "pack": {"name": "Git Helper", "id": "git-helper", "version": "1.2.0", "kind": "skill", "publisher": "Mac MCP",
             "description": "Helps with git."},
    "signer": "mac-mcp-catalog", "catalog_digest": "0123456789abcdef0123", "installed_version": "1.1.0",
    "action": "update", "capabilities": ["read_files"], "effective": {"tools": ["run_command"]},
    "permission_changes": {"added": ["shell"], "removed": []},
    "files": [{"path": "SKILL.md", "size": 120, "sha256": "aaaaaaaaaaaaaaaabbbb"}],
    "scripts_held": ["scripts/check.sh"], "script_note": "enable it with enable-script", "note": "Review first.",
}


class PlainPhase3(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"COLUMNS": "100", "TERM": "xterm-256color"})
        env.start()
        self.addCleanup(env.stop)

    def capture(self, func, args, stdin: str | None = None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if stdin is None:
                code = func(args)
            else:
                with patch("sys.stdin", io.StringIO(stdin)):
                    code = func(args)
        return code, out.getvalue(), err.getvalue()

    # ── dashboard ─────────────────────────────────────────────────────────
    def test_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            with patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")):
                self.assertEqual((1, "mac-mcp is not running with verified ownership (not_running). Start it first with: mac-mcp start\n"),
                                 self.capture(cli.dashboard, NS())[:2])
            with patch.object(cli, "_resolve_server_identity", return_value=(42, "pid_record")), \
                 patch.object(cli, "dashboard_token_path", return_value=token), patch.object(cli, "_default_port", return_value=8765):
                self.assertEqual((1, "Dashboard credential is unavailable. Restart Mac MCP once, then run: mac-mcp dashboard\n"),
                                 self.capture(cli.dashboard, NS())[:2])
                token.write_text("secret-token")
                with patch.object(cli.webbrowser, "open", return_value=True) as opened:
                    code, out, _ = self.capture(cli.dashboard, NS())
                self.assertEqual((0, "dashboard: http://127.0.0.1:8765/dashboard (authenticated local launch)\n"), (code, out))
                self.assertIn("#token=secret-token", opened.call_args[0][0])
                with patch.object(cli.webbrowser, "open", return_value=False):
                    code, out, _ = self.capture(cli.dashboard, NS())
                self.assertEqual("dashboard: http://127.0.0.1:8765/dashboard (authenticated local launch)\n"
                                 "The browser did not open automatically; run 'mac-mcp dashboard' again from this Mac.\n", out)

    # ── credential ────────────────────────────────────────────────────────
    def test_credential_status(self) -> None:
        cases = [((True, True, "ok"), 0, "Cloudflare Tunnel credential: configured (secure 0600 owner-only file).\n"),
                 ((False, False, "missing"), 1, "Cloudflare Tunnel credential: not configured.\n"),
                 ((True, False, "mode_0644"), 2, "Cloudflare Tunnel credential: configured but unsafe (mode_0644).\n")]
        for (configured, secure, reason), code, text in cases:
            state = CloudflareCredentialStatus(Path("/t"), configured, secure, reason)
            with patch.object(cli, "inspect_cloudflare_credential", return_value=state):
                self.assertEqual((code, text), self.capture(cli.credential, NS(provider="cloudflare", action="status"))[:2])

    def test_credential_save_and_remove(self) -> None:
        with patch.object(cli, "write_cloudflare_token") as write:
            code, out, _ = self.capture(cli.credential, NS(provider="cloudflare", action="save"), stdin="tok-123\n")
        self.assertEqual((0, "Cloudflare Tunnel credential saved securely (owner-only 0600).\n"), (code, out))
        write.assert_called_once_with("tok-123\n")
        with patch.object(cli, "write_cloudflare_token", side_effect=PublicEndpointError("token is empty")):
            code, out, _ = self.capture(cli.credential, NS(provider="cloudflare", action="save"), stdin="")
        self.assertEqual((2, "Could not save Cloudflare Tunnel credential: token is empty\n"), (code, out))
        with patch.object(cli, "remove_cloudflare_token", return_value=True):
            self.assertEqual((0, "Cloudflare Tunnel credential removed.\n"),
                             self.capture(cli.credential, NS(provider="cloudflare", action="remove"))[:2])
        with patch.object(cli, "remove_cloudflare_token", return_value=False):
            self.assertEqual((0, "Cloudflare Tunnel credential was not configured.\n"),
                             self.capture(cli.credential, NS(provider="cloudflare", action="remove"))[:2])

    # ── autostart ─────────────────────────────────────────────────────────
    def test_autostart(self) -> None:
        with patch.object(cli, "_supervisor_loaded", return_value=True):
            with patch.object(cli, "_start_at_login", return_value=True):
                self.assertEqual((0, "Start at login: on (a server you left running starts again when you log in).\n"),
                                 self.capture(cli.autostart, NS(state=None, json=False))[:2])
            with patch.object(cli, "_start_at_login", return_value=False):
                self.assertEqual((0, "Start at login: off (run 'mac-mcp start' or use the menu bar app after you log in).\n"),
                                 self.capture(cli.autostart, NS(state=None, json=False))[:2])
        with patch.object(cli, "update_runtime_setting", side_effect=RuntimeError("locked")):
            self.assertEqual((1, "", "mac-mcp autostart: could not save the setting: locked\n"),
                             self.capture(cli.autostart, NS(state="on", json=False)))

    # ── recipe ────────────────────────────────────────────────────────────
    def test_recipe_list(self) -> None:
        body = {"recipes": [{"recipe_id": "r1", "status": "active", "name": "Morning",
                             "parameters": {"city": {"type": "string", "required": True}, "n": {"type": "int"}}},
                            {"recipe_id": "r2", "status": "paused", "name": "Plain"}]}
        with patch.object(cli, "_recipe_request", return_value=(0, body)):
            self.assertEqual((0, "r1  [active]  Morning  (city:string, n:int?)\nr2  [paused]  Plain\n"),
                             self.capture(cli.recipe, NS(recipe_command="list", json=False))[:2])
        with patch.object(cli, "_recipe_request", return_value=(0, {"recipes": []})):
            self.assertEqual((0, "No active recipes. Save one with computer_plan(save_as_recipe=...) and activate it.\n"),
                             self.capture(cli.recipe, NS(recipe_command="list", json=False))[:2])
        down = {"error": "server_unreachable", "message": "Mac MCP is not running (x). Start it with: mac-mcp start"}
        with patch.object(cli, "_recipe_request", return_value=(3, down)):
            self.assertEqual((3, "", down["message"] + "\n"), self.capture(cli.recipe, NS(recipe_command="list", json=False)))

    def test_recipe_run(self) -> None:
        def go(code, body, param=None):
            with patch.object(cli, "_recipe_request", return_value=(code, body)) as request:
                result = self.capture(cli.recipe, NS(recipe_command="run", recipe_id="r1", param=param, json=False))
            return result, request
        (result, request) = go(0, {"ok": True, "name": "Morning", "steps_executed": 3}, ["city=Ankara"])
        self.assertEqual((0, "Recipe Morning completed (3 steps).\n", ""), result)
        self.assertEqual({"recipe_id": "r1", "values": {"city": "Ankara"}}, request.call_args[0][2])
        (result, _) = go(0, {"ok": False, "status": "approval_required", "message": "Approve it in Settings"})
        self.assertEqual((2, "", "Recipe did not complete: approval_required: Approve it in Settings\n"), result)
        (result, _) = go(0, {"ok": False, "status": "failed", "message": ""})
        self.assertEqual((1, "", "Recipe did not complete: failed\n"), result)
        (result, _) = go(3, {"error": "server_unreachable", "message": "Mac MCP is not running."})
        self.assertEqual((3, "", "Recipe did not complete: server_unreachable: Mac MCP is not running.\n"), result)
        self.assertEqual((1, "", "--param must be name=value: city\n"),
                         self.capture(cli.recipe, NS(recipe_command="run", recipe_id="r1", param=["city"], json=False)))

    # ── logins ────────────────────────────────────────────────────────────
    def test_logins(self) -> None:
        rows = [{"id": "l1", "site": "github.com", "username": "me", "subdomains": True},
                {"id": "l2", "site": "example.com", "username": "you", "subdomains": False}]
        with patch.object(login_vault, "list_logins", return_value=rows):
            self.assertEqual((0, "l1  github.com  me  (and subdomains)\nl2  example.com  you\n"),
                             self.capture(cli.logins, NS(logins_command="list", json=False))[:2])
        with patch.object(login_vault, "list_logins", return_value=[]):
            self.assertEqual((0, "No saved logins.\n"), self.capture(cli.logins, NS(logins_command="list", json=False))[:2])
        with patch.object(login_vault, "add_login", return_value={"site": "github.com"}) as add:
            code, out, _ = self.capture(cli.logins, NS(logins_command="add", site="github.com", username="me", label=None,
                                                       subdomains=False, json=False), stdin="pw-secret\n")
        self.assertEqual((0, "Saved login for github.com.\n"), (code, out))
        self.assertEqual(("github.com", "me", "pw-secret"), add.call_args[0])
        with patch.object(login_vault, "set_subdomains", return_value={"site": "github.com", "subdomains": True}):
            self.assertEqual((0, "github.com: also on its subdomains.\n"),
                             self.capture(cli.logins, NS(logins_command="subdomains", login_id="l1", state="on", json=False))[:2])
        with patch.object(login_vault, "set_subdomains", return_value={"site": "github.com", "subdomains": False}):
            self.assertEqual((0, "github.com: this site only.\n"),
                             self.capture(cli.logins, NS(logins_command="subdomains", login_id="l1", state="off", json=False))[:2])
        for removed, code, text in ((True, 0, "Removed.\n"), (False, 1, "No such saved login.\n")):
            with patch.object(login_vault, "remove_login", return_value=removed):
                self.assertEqual((code, text), self.capture(cli.logins, NS(logins_command="remove", login_id="l1", json=False))[:2])
        with patch.object(login_vault, "preview_import", return_value=[{"index": 0, "site": "a.com", "username": "x"},
                                                                        {"index": 1, "site": "b.com", "username": "y"}]):
            self.assertEqual((0, "0  a.com  x\n1  b.com  y\n"),
                             self.capture(cli.logins, NS(logins_command="import-preview", file="/f.csv", json=False))[:2])
        with patch.object(login_vault, "preview_import", return_value=[]):
            self.assertEqual((0, "No usable logins.\n"),
                             self.capture(cli.logins, NS(logins_command="import-preview", file="/f.csv", json=False))[:2])
        with patch.object(login_vault, "import_logins", return_value={"imported": 2, "skipped": 1, "file_deleted": True}) as imp:
            self.assertEqual((0, "Imported 2 login(s), skipped 1; export file deleted.\n"),
                             self.capture(cli.logins, NS(logins_command="import", file="/f.csv", select="0,1",
                                                         delete_file=True, json=False))[:2])
        self.assertEqual(("/f.csv", [0, 1]), imp.call_args[0])
        with patch.object(login_vault, "import_logins", return_value={"imported": 1, "skipped": 0, "file_deleted": False}):
            self.assertEqual((0, "Imported 1 login(s), skipped 0.\n"),
                             self.capture(cli.logins, NS(logins_command="import", file="/f.csv", select="all",
                                                         delete_file=False, json=False))[:2])
        with patch.object(login_vault, "remove_login", side_effect=login_vault.VaultError("locked", "The vault is locked.")):
            self.assertEqual((1, "", "mac-mcp logins: The vault is locked.\n"),
                             self.capture(cli.logins, NS(logins_command="remove", login_id="l1", json=False)))

    # ── catalog ───────────────────────────────────────────────────────────
    def test_catalog_list_and_review(self) -> None:
        listed = {"signer": "mac-mcp-catalog", "packs": [
            {"id": "git-helper", "version": "1.2.0", "kind": "skill", "name": "Git Helper", "installed_version": "1.1.0",
             "update_available": True},
            {"id": "notes", "version": "0.1.0", "kind": "recipe", "name": "Notes", "installed_version": None,
             "update_available": False}]}
        with patch.object(catalog, "list_packs", return_value=listed):
            self.assertEqual((0, "Catalog signed by mac-mcp-catalog\n"
                                 "git-helper  1.2.0  skill  installed 1.1.0, update available  - Git Helper\n"
                                 "notes  0.1.0  recipe  not installed  - Notes\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="list", json=False))[:2])
        expected_review = (
            "Git Helper (git-helper 1.2.0, skill) by Mac MCP\n"
            "  Helps with git.\n"
            "  signed by mac-mcp-catalog; catalog 0123456789abcdef\n"
            "  action: update (installed 1.1.0)\n"
            "  declared capabilities: read_files\n"
            "  recipe tools: run_command\n"
            "  permission changes: +shell  --\n"
            "  file SKILL.md  120 B  sha256 aaaaaaaaaaaaaaaa\n"
            "  scripts held back: scripts/check.sh (enable it with enable-script)\n"
            "  Review first.\n")
        with patch.object(catalog, "review", return_value=REVIEW):
            self.assertEqual((0, expected_review),
                             self.capture(cli.catalog_command, NS(catalog_command="review", pack_id="git-helper", json=False))[:2])
            self.assertEqual((1, expected_review + "Nothing installed. Run again with --yes to install this exact version.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="install", pack_id="git-helper", yes=False,
                                                                  json=False))[:2])
        with patch.object(catalog, "review", return_value={**REVIEW, "blocked": "signature mismatch"}):
            self.assertTrue(self.capture(cli.catalog_command, NS(catalog_command="review", pack_id="x", json=False))[1]
                            .endswith("  Review first.\n  BLOCKED: signature mismatch\n"))

    def test_catalog_install_script_and_error(self) -> None:
        with patch.object(catalog, "install", return_value={"pack_id": "notes", "version": "0.1.0", "recipe_id": "rc"}):
            self.assertEqual((0, "Installed notes 0.1.0. Recipe is a draft: activate it after review.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="install", pack_id="notes", yes=True,
                                                                  json=False))[:2])
        with patch.object(catalog, "install", return_value={"pack_id": "git-helper", "version": "1.2.0"}):
            self.assertEqual((0, "Installed git-helper 1.2.0.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="install", pack_id="git-helper", yes=True,
                                                                  json=False))[:2])
        script = {"path": "scripts/check.sh", "sha256": "f" * 64, "content": "#!/bin/sh\necho hi"}
        with patch.object(catalog, "enable_script",
                          side_effect=catalog.CatalogError("CONFIRM_REQUIRED", "confirm", script=script)):
            self.assertEqual((1, f"scripts/check.sh  sha256 {'f' * 64}\n---\n#!/bin/sh\necho hi\n---\n"
                                 "Nothing enabled. Run again with --yes to install this script; it can run anything.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="enable-script", pack_id="git-helper",
                                                                  path="scripts/check.sh", yes=False, json=False))[:2])
        with patch.object(catalog, "enable_script", return_value={"path": "scripts/check.sh", "pack_id": "git-helper"}):
            self.assertEqual((0, "Enabled scripts/check.sh for git-helper.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="enable-script", pack_id="git-helper",
                                                                  path="scripts/check.sh", yes=True, json=False))[:2])
        with patch.object(catalog, "review", side_effect=catalog.CatalogError("UNKNOWN_PACK", "No pack named x.")):
            self.assertEqual((1, "", "mac-mcp catalog: No pack named x.\n"),
                             self.capture(cli.catalog_command, NS(catalog_command="review", pack_id="x", json=False)))

    # ── conformance ───────────────────────────────────────────────────────
    def test_conformance(self) -> None:
        report = {"ok": True, "checks": [{"check_id": "browser.a", "status": "pass", "summary": "A works."},
                                         {"check_id": "native.b", "status": "fail", "summary": "B broke.", "remediation": "Fix B."}],
                  "counts": {"pass": 1, "fail": 1}, "metrics": {"deterministic_pass_rate": 50.0, "focus_safety_regressions": 0},
                  "duration_ms": 12}
        with patch("mcp_server.conformance.run_conformance", return_value=report):
            code, out, _ = self.capture(cli.conformance, NS(live=False, json=False))
        self.assertEqual(0, code)
        self.assertEqual("Mac MCP Computer Use Conformance\n================================\n"
                         "[PASS] browser.a: A works.\n[FAIL] native.b: B broke.\n       Fix: Fix B.\n\n"
                         "Result: OK | pass=1 warn=0 fail=1 info=0\n"
                         "Deterministic pass rate: 50.0% | focus regressions=0 | duration=12ms\n", out)
        with patch("mcp_server.conformance.run_conformance", return_value={**report, "ok": False}):
            self.assertEqual(1, self.capture(cli.conformance, NS(live=False, json=False))[0])

    # ── repair ────────────────────────────────────────────────────────────
    def checks(self, broken: bool):
        return [lifecycle.Check("venv", "Python environment", True, "Python 3.14"),
                lifecycle.Check("cli", "mac-mcp command", not broken, "/u/.local/bin/mac-mcp",
                                "" if not broken else "recreate the launcher for this runtime")]

    def test_repair(self) -> None:
        with patch.object(lifecycle, "repair_checks", return_value=self.checks(False)):
            self.assertEqual((0, "  [ok ] Python environment: Python 3.14\n  [ok ] mac-mcp command: /u/.local/bin/mac-mcp\n"
                                 "Nothing to repair.\n"), self.capture(cli.repair, NS(yes=False))[:2])
        with patch.object(lifecycle, "repair_checks", return_value=self.checks(True)):
            self.assertEqual((0, "  [ok ] Python environment: Python 3.14\n"
                                 "  [fix] mac-mcp command: /u/.local/bin/mac-mcp -> recreate the launcher for this runtime\n"
                                 "Nothing was changed. To repair these, run: mac-mcp repair --yes\n"),
                             self.capture(cli.repair, NS(yes=False))[:2])
        with patch.object(lifecycle, "repair_checks", side_effect=[self.checks(True), self.checks(False)]), \
             patch("mcp_server.cli_bootstrap.ensure_cli_launcher") as launcher:
            code, out, _ = self.capture(cli.repair, NS(yes=True))
        self.assertEqual((0, "  [ok ] Python environment: Python 3.14\n"
                             "  [fix] mac-mcp command: /u/.local/bin/mac-mcp -> recreate the launcher for this runtime\n"
                             "After repair:\n  [ok ] Python environment: Python 3.14\n  [ok ] mac-mcp command: /u/.local/bin/mac-mcp\n"),
                         (code, out))
        launcher.assert_called_once()

    # ── uninstall ─────────────────────────────────────────────────────────
    def test_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, state, app = root / "runtime", root / "state", root / "Mac MCP.app"
            for path in (runtime, state, app):
                path.mkdir()
            with patch.object(cli, "PROJECT_ROOT", runtime), patch.object(cli, "STATE_DIR", state), \
                 patch.object(cli, "_uninstall_app_path", return_value=app), \
                 patch.object(cli, "default_cli_path", return_value=root / "cli"), \
                 patch.object(cli, "_launch_agents_dir", return_value=root / "agents"), \
                 patch.object(cli, "_uninstall_source_path", return_value=root / "src"):
                code, out, _ = self.capture(cli.uninstall, NS(yes=False, include_state=False, include_source=False))
                self.assertEqual(0, code)
                self.assertEqual(
                    "Would move to the Trash:\n"
                    f"  - Menu bar app: {app} (includes the Safari Visual Companion extension)\n"
                    f"  - Server runtime: {runtime} (includes the MCP API key in mcp_server/.env; AI apps connected with it stop working)\n"
                    "Kept:\n"
                    f"  - Your settings, keys and tokens, memory, logs and run history: {state}\n"
                    "  Add --include-state to move your settings, keys and tokens too.\n"
                    "Nothing is deleted: removed parts go to one folder in the Trash, so you can put them back.\n"
                    "Not changed: AI app configurations that point at Mac MCP, the PATH line in ~/.zprofile, "
                    "saved logins in your Keychain, and the Chrome companion (remove it at chrome://extensions).\n"
                    "\nNothing was changed. To do this, run: mac-mcp uninstall --yes\n", out)
                code, out, _ = self.capture(cli.uninstall, NS(yes=False, include_state=True, include_source=False))
                self.assertTrue(out.endswith("\nNothing was changed. To do this, run: mac-mcp uninstall --yes --include-state\n"))
                with patch.object(cli, "_trash_root", return_value=root / "Trash"), \
                     patch.object(cli, "_forget_package_receipt"), patch.object(cli.time, "strftime", return_value="20261016-120000"):
                    code, out, _ = self.capture(cli.uninstall, NS(yes=True, include_state=False, include_source=False))
                self.assertEqual(0, code)
                self.assertTrue(out.startswith("This is not the default install, so its server and login jobs were not "
                                               "stopped; stop them yourself.\nMoved to the Trash:\n"), out[:160])
                self.assertIn(f"Everything moved is in: {root / 'Trash' / 'Mac MCP uninstall 20261016-120000'}\n", out)
                self.assertFalse(app.exists())
                failed = [{"key": "app", "path": "/x/app", "result": "failed", "error": "PermissionError"}]
                with patch.object(cli, "_trash_root", return_value=root / "Trash"), patch.object(cli, "_forget_package_receipt"), \
                     patch.object(lifecycle, "apply_uninstall", return_value=failed):
                    code, out, _ = self.capture(cli.uninstall, NS(yes=True, include_state=False, include_source=False))
                self.assertEqual(1, code)
                self.assertTrue(out.endswith("Could not move /x/app (PermissionError); remove it by hand if you want it gone.\n"))


if __name__ == "__main__":
    unittest.main()
