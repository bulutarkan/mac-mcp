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


class RecipeScreen(ThemedTestCase):
    BODY = {"recipes": [{"recipe_id": "r1", "status": "active", "name": "Morning",
                         "parameters": {"city": {"type": "string", "required": True}, "n": {"type": "int"}}},
                        {"recipe_id": "r2", "status": "paused", "name": "Plain"}]}

    def run_recipe(self, response, **args):
        with patch.object(cli, "_recipe_request", return_value=response) as request:
            result = self.run_cmd(cli.recipe, NS(**{"json": False, **args}))
        return (*result, request)

    def test_list(self) -> None:
        code, text, *_ = self.run_recipe((0, self.BODY), recipe_command="list")
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · recipe\n\n"), text)
        self.assertIn("● Morning  r1 · city:string, n:int?", text)
        self.assertIn("● Plain    r2", text)
        self.assertIn("✓ 2 recipes", text)

    def test_empty(self) -> None:
        code, text, *_ = self.run_recipe((0, {"recipes": []}), recipe_command="list")
        self.assertEqual(0, code)
        self.assertIn("ℹ No active recipes", text)
        self.assertIn("computer_plan(save_as_recipe=…)", text)

    def test_server_down_names_start(self) -> None:
        down = {"error": "server_unreachable", "message": "Mac MCP is not running (x). Start it with: mac-mcp start"}
        code, text, err, *_ = self.run_recipe((3, down), recipe_command="list")
        self.assertEqual((3, ""), (code, err))
        self.assertIn("✗ Mac MCP is not running — try: mac-mcp start", flat(text))
        code, text, *_ = self.run_recipe((3, down), recipe_command="run", recipe_id="r1", param=None)
        self.assertEqual(3, code)
        self.assertIn("✗ Mac MCP is not running — try: mac-mcp start", flat(text))

    def test_run_done(self) -> None:
        code, text, _, _, request = self.run_recipe((0, {"ok": True, "name": "Morning", "steps_executed": 3}),
                                                    recipe_command="run", recipe_id="r1", param=["city=Ankara"])
        self.assertEqual(0, code)
        self.assertIn("✓ Ran r1", text)
        self.assertIn("✓ Morning completed   3 steps", text)
        self.assertEqual({"recipe_id": "r1", "values": {"city": "Ankara"}}, request.call_args[0][2])

    def test_run_needs_approval(self) -> None:
        code, text, *_ = self.run_recipe((0, {"ok": False, "name": "Morning", "status": "approval_required",
                                              "message": "Approve it in Settings"}),
                                         recipe_command="run", recipe_id="r1", param=None)
        self.assertEqual(2, code)
        self.assertIn("⚠ Morning needs approval first", text)
        self.assertIn("Approve it in Settings", text)

    def test_run_failed(self) -> None:
        code, text, *_ = self.run_recipe((0, {"ok": False, "status": "failed", "message": "step 2 broke"}),
                                         recipe_command="run", recipe_id="r1", param=None)
        self.assertEqual(1, code)
        self.assertIn("✗ Run r1", text)
        self.assertIn("failed: step 2 broke", text)
        self.assertIn("✗ The recipe did not complete", text)

    def test_bad_param(self) -> None:
        code, text, err, *_ = self.run_recipe((0, {}), recipe_command="run", recipe_id="r1", param=["city"])
        self.assertEqual((1, ""), (code, err))
        self.assertIn("✗ --param must be name=value: city", text)


class LoginsScreen(ThemedTestCase):
    ROWS = [{"id": "l1", "site": "github.com", "username": "me", "subdomains": True, "password": "never"},
            {"id": "l2", "site": "example.com", "username": "you", "subdomains": False}]

    def logins(self, stdin=None, **args):
        return self.run_cmd(cli.logins, NS(**{"json": False, **args}), stdin=stdin)

    def test_list_never_prints_a_password(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "list_logins", return_value=self.ROWS):
            code, text, _, raw = self.logins(logins_command="list")
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · logins\n\n"), text)
        self.assertIn("github.com   me · l1 · also subdomains", text)
        self.assertIn("example.com  you · l2", text)
        self.assertIn("✓ 2 saved logins", text)
        self.assertNotIn("never", raw)

    def test_empty(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "list_logins", return_value=[]):
            code, text, *_ = self.logins(logins_command="list")
        self.assertEqual(0, code)
        self.assertIn("ℹ No saved logins", text)

    def test_add_from_a_terminal_is_hidden(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "add_login", return_value={"site": "github.com"}) as add, \
             patch("getpass.getpass", return_value="pw-secret") as ask:
            code, text, err, raw = self.logins(stdin=TTYInput(""), logins_command="add", site="github.com", username="me",
                                               label=None, subdomains=False)
        self.assertEqual(0, code)
        ask.assert_called_once()
        self.assertEqual("pw-secret", add.call_args[0][2])
        self.assertNotIn("pw-secret", raw + err)
        self.assertIn("✓ Saved the login for github.com", text)

    def test_add_from_a_pipe(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "add_login", return_value={"site": "github.com"}) as add, patch("getpass.getpass") as ask:
            code, *_ = self.logins(stdin=io.StringIO("pw-1\n"), logins_command="add", site="github.com", username="me",
                                   label=None, subdomains=False)
        self.assertEqual(0, code)
        ask.assert_not_called()
        self.assertEqual("pw-1", add.call_args[0][2])

    def test_subdomains_and_remove(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "set_subdomains", return_value={"site": "github.com", "subdomains": True}):
            code, text, *_ = self.logins(logins_command="subdomains", login_id="l1", state="on")
        self.assertEqual(0, code)
        self.assertIn("✓ github.com: also on its subdomains", text)
        with patch.object(login_vault, "remove_login", return_value=True):
            code, text, *_ = self.logins(logins_command="remove", login_id="l1")
        self.assertEqual(0, code)
        self.assertIn("✓ Removed the saved login", text)
        with patch.object(login_vault, "remove_login", return_value=False):
            code, text, *_ = self.logins(logins_command="remove", login_id="l9")
        self.assertEqual(1, code)
        self.assertIn("✗ No such saved login", text)

    def test_import_preview_and_import(self) -> None:
        from mcp_server import login_vault
        items = [{"index": 0, "site": "a.com", "username": "x"}, {"index": 1, "site": "b.com", "username": "y"}]
        with patch.object(login_vault, "preview_import", return_value=items):
            code, text, *_ = self.logins(logins_command="import-preview", file="/f.csv")
        self.assertEqual(0, code)
        self.assertIn("0  a.com · x", text)
        self.assertIn("ℹ 2 logins can be imported — try: mac-mcp logins import /f.csv", flat(text))
        with patch.object(login_vault, "preview_import", return_value=[]):
            code, text, *_ = self.logins(logins_command="import-preview", file="/f.csv")
        self.assertIn("ℹ No usable logins", text)
        with patch.object(login_vault, "import_logins", return_value={"imported": 2, "skipped": 1, "file_deleted": True}):
            code, text, *_ = self.logins(logins_command="import", file="/f.csv", select="all", delete_file=True)
        self.assertEqual(0, code)
        self.assertIn("✓ Imported 2 logins · skipped 1   export file deleted", text)

    def test_error(self) -> None:
        from mcp_server import login_vault
        with patch.object(login_vault, "remove_login", side_effect=login_vault.VaultError("locked", "The vault is locked.")):
            code, text, err, _ = self.logins(logins_command="remove", login_id="l1")
        self.assertEqual((1, ""), (code, err))
        self.assertIn("✗ The vault is locked.", text)


REVIEW = {
    "pack": {"name": "Git Helper", "id": "git-helper", "version": "1.2.0", "kind": "skill", "publisher": "Mac MCP",
             "description": "Helps with git."},
    "signer": "mac-mcp-catalog", "catalog_digest": "0123456789abcdef0123", "installed_version": "1.1.0",
    "action": "update", "capabilities": ["read_files"], "effective": {"tools": ["run_command"]},
    "permission_changes": {"added": ["shell"], "removed": []},
    "files": [{"path": "SKILL.md", "size": 120, "sha256": "aaaaaaaaaaaaaaaabbbb"}],
    "scripts_held": ["scripts/check.sh"], "script_note": "enable it with enable-script", "note": "Review first.",
}


class CatalogScreen(ThemedTestCase):
    def catalog(self, **args):
        return self.run_cmd(cli.catalog_command, NS(**{"json": False, **args}))

    def test_list_with_an_update(self) -> None:
        from mcp_server import catalog
        listed = {"signer": "mac-mcp-catalog", "packs": [
            {"id": "git-helper", "version": "1.2.0", "kind": "skill", "name": "Git Helper", "installed_version": "1.1.0",
             "update_available": True},
            {"id": "notes", "version": "0.1.0", "kind": "recipe", "name": "Notes", "installed_version": None,
             "update_available": False}]}
        with patch.object(catalog, "list_packs", return_value=listed):
            code, text, *_ = self.catalog(catalog_command="list")
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · catalog\n\n"), text)
        self.assertIn("● Git Helper  git-helper 1.2.0 · skill · update available, installed 1.1.0", text)
        self.assertIn("● Notes       notes 0.1.0 · recipe · not installed", text)
        self.assertIn("Signed by mac-mcp-catalog", text)
        self.assertIn("⚠ 1 update available — try: mac-mcp catalog review git-helper", flat(text))

    def test_review(self) -> None:
        from mcp_server import catalog
        with patch.object(catalog, "review", return_value=REVIEW):
            code, text, *_ = self.catalog(catalog_command="review", pack_id="git-helper")
        self.assertEqual(0, code)
        for part in ("Pack", "Git Helper · git-helper 1.2.0 · skill", "Publisher", "Signed by", "mac-mcp-catalog · catalog 0123456789abcdef",
                     "Action", "update · installed 1.1.0", "Capabilities", "read_files", "Recipe tools", "run_command",
                     "Permissions", "+shell", "Files", "SKILL.md · 120 B · sha256 aaaaaaaaaaaaaaaa",
                     "Held back", "scripts/check.sh · enable it with enable-script", "Helps with git.", "Review first."):
            self.assertIn(part, text)
        self.assertIn("✓ Reviewed git-helper 1.2.0", text)
        with patch.object(catalog, "review", return_value={**REVIEW, "blocked": "signature mismatch"}):
            code, text, *_ = self.catalog(catalog_command="review", pack_id="git-helper")
        self.assertIn("✗ Blocked: signature mismatch", text)

    def test_install_preview_and_done(self) -> None:
        from mcp_server import catalog
        with patch.object(catalog, "review", return_value=REVIEW), patch.object(catalog, "install") as install:
            code, text, *_ = self.catalog(catalog_command="install", pack_id="git-helper", yes=False)
        self.assertEqual(1, code)
        install.assert_not_called()
        self.assertIn("Git Helper · git-helper 1.2.0 · skill", text)
        self.assertIn("⚠ Nothing installed — try: mac-mcp catalog install git-helper --yes", flat(text))
        with patch.object(catalog, "install", return_value={"pack_id": "notes", "version": "0.1.0", "recipe_id": "rc"}):
            code, text, *_ = self.catalog(catalog_command="install", pack_id="notes", yes=True)
        self.assertEqual(0, code)
        self.assertIn("✓ Installed notes 0.1.0", text)
        self.assertIn("✓ notes 0.1.0 is installed", text)
        self.assertIn("The recipe is a draft: activate it after review.", flat(text))

    def test_enable_script_preview_is_raw(self) -> None:
        from mcp_server import catalog
        content = "#!/bin/sh\n  echo " + "x" * 120 + "\n\nexit 0"
        script = {"path": "scripts/check.sh", "sha256": "f" * 64, "content": content}
        with patch.object(catalog, "enable_script", side_effect=catalog.CatalogError("CONFIRM_REQUIRED", "confirm", script=script)):
            code, text, _, raw = self.catalog(catalog_command="enable-script", pack_id="git-helper", path="scripts/check.sh", yes=False)
        self.assertEqual(1, code)
        self.assertIn("\n" + content + "\n", raw)
        self.assertIn("f" * 64, text)
        self.assertIn("⚠ Nothing enabled · this script can run anything — try: mac-mcp catalog enable-script git-helper "
                      "scripts/check.sh --yes", flat(text))
        with patch.object(catalog, "enable_script", return_value={"path": "scripts/check.sh", "pack_id": "git-helper"}):
            code, text, *_ = self.catalog(catalog_command="enable-script", pack_id="git-helper", path="scripts/check.sh", yes=True)
        self.assertEqual(0, code)
        self.assertIn("✓ Enabled scripts/check.sh for git-helper", text)

    def test_error(self) -> None:
        from mcp_server import catalog
        with patch.object(catalog, "review", side_effect=catalog.CatalogError("UNKNOWN_PACK", "No pack named x.")):
            code, text, err, _ = self.catalog(catalog_command="review", pack_id="x")
        self.assertEqual((1, ""), (code, err))
        self.assertIn("✗ No pack named x.", text)
        with patch.object(catalog, "install", side_effect=catalog.CatalogError("CHANGED", "The pack changed since review.")):
            code, text, *_ = self.catalog(catalog_command="install", pack_id="x", yes=True)
        self.assertEqual(1, code)
        self.assertIn("✗ Install x", text)
        self.assertIn("✗ The pack changed since review.", text)


class ConformanceScreen(ThemedTestCase):
    def report(self, checks, ok=True):
        return {"ok": ok, "checks": checks, "metrics": {"deterministic_pass_rate": 100.0, "focus_safety_regressions": 0},
                "duration_ms": 1234}

    def run_conformance(self, report, live=False):
        with patch("mcp_server.conformance.run_conformance", return_value=report) as run:
            result = self.run_cmd(cli.conformance, NS(live=live, json=False))
        return (*result, run)

    def test_all_pass(self) -> None:
        checks = [{"check_id": f"browser.c{i}", "category": "computer_use", "status": "pass", "summary": "ok"} for i in range(3)]
        checks += [{"check_id": "native.a", "category": "computer_use", "status": "pass", "summary": "ok"},
                   {"check_id": "computer_plan.x", "category": "computer_use", "status": "pass", "summary": "ok"}]
        code, text, _, raw, _ = self.run_conformance(self.report(checks))
        self.assertEqual(0, code)
        self.assertIn("focus regressions · 1.2s", strip_ansi(raw))
        self.assertTrue(text.startswith("  ◆ Mac MCP · conformance\n\n"), text)
        self.assertIn("✓ Ran the Computer Use checks", text)
        self.assertIn("Browser     ✓ 3 checks passed", text)
        self.assertIn("Native app  ✓ 1 check passed", text)
        self.assertIn("Planning    ✓ 1 check passed", text)
        self.assertIn("✓ All 5 Computer Use checks passed   100.0% · 0 focus regressions · 0.0s", text)

    def test_a_failure_has_its_fix(self) -> None:
        checks = [{"check_id": "browser.a", "category": "computer_use", "status": "pass", "summary": "A works."},
                  {"check_id": "observe.b", "category": "computer_use", "status": "fail", "summary": "B broke.", "remediation": "Fix B."}]
        code, text, *_ = self.run_conformance(self.report(checks, ok=False))
        self.assertEqual(1, code)
        self.assertIn("Observation  ✗ B broke.", text)
        self.assertIn("Fix: Fix B.", text)
        self.assertIn("✗ 1 failed — fix it, then run: mac-mcp conformance", flat(text))

    def test_live_checks_have_their_own_group(self) -> None:
        checks = [{"check_id": "browser.a", "category": "computer_use", "status": "pass", "summary": "ok"},
                  {"check_id": "live.safari", "category": "live", "status": "warn", "summary": "Safari is closed."}]
        code, text, _, _, run = self.run_conformance(self.report(checks), live=True)
        self.assertEqual(0, code)
        run.assert_called_once_with(live=True)
        self.assertIn("Live     ⚠ Safari is closed.", text)
        self.assertIn("⚠ 1 thing to look at", text)


if __name__ == "__main__":
    unittest.main()
