"""#163: saved logins that agents can use without seeing the password."""
from __future__ import annotations

import base64
import csv
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_sign_in as sign_in
from mcp_server import login_vault, tools_browser
from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings

SECRET = "Pa55-w0rd!sentinel"


class FakeSecurity:
    """Stands in for /usr/bin/security so no test touches a real keychain."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.calls: list = []

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append((list(argv), input))
        assert argv[0] == login_vault.SECURITY
        args = dict(zip(argv[1::1], argv[2::1]))
        account = args.get("-a")
        if argv[1] == "add-generic-password":
            first, second = (input or "").split("\n")[:2]
            assert first == second, "security reads the password twice"
            self.items[account] = first
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "find-generic-password":
            if account in self.items:
                return SimpleNamespace(returncode=0, stdout=self.items[account] + "\n", stderr="")
            return SimpleNamespace(returncode=44, stdout="", stderr="not found")
        if argv[1] == "delete-generic-password":
            self.items.pop(account, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


class VaultCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.security = FakeSecurity()
        patches = [
            patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.tmp / "state")}),
            patch.object(login_vault.subprocess, "run", side_effect=self.security),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class SiteMatchingTests(unittest.TestCase):
    def test_normalize_and_match(self) -> None:
        self.assertEqual("github.com", login_vault.normalize_site("https://www.GitHub.com/login?x=1"))
        self.assertEqual("accounts.google.com", login_vault.normalize_site("accounts.google.com"))
        self.assertEqual("localhost", login_vault.normalize_site("http://localhost:8891/login.html"))
        for bad in ("", "not a site", "https://"):
            with self.subTest(bad=bad), self.assertRaises(login_vault.VaultError):
                login_vault.normalize_site(bad)
        self.assertTrue(login_vault.site_matches("github.com", "github.com"))
        self.assertTrue(login_vault.site_matches("github.com", "www.github.com"))
        self.assertTrue(login_vault.site_matches("google.com", "accounts.google.com"))
        for lookalike in ("github.com.evil.io", "evilgithub.com", "g1thub.com", "github.co"):
            with self.subTest(host=lookalike):
                self.assertFalse(login_vault.site_matches("github.com", lookalike))


class VaultTests(VaultCase):
    def test_password_goes_to_the_keychain_on_stdin_and_nowhere_else(self) -> None:
        row = login_vault.add_login("https://github.com/login", "ada@example.com", SECRET, label="Work")
        self.assertEqual({"id", "site", "username", "label", "source", "created_at", "updated_at"}, set(row))
        argv, stdin = self.security.calls[-1]
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(f"{SECRET}\n{SECRET}\n", stdin)
        index = (self.tmp / "state" / "vault" / "logins.json")
        self.assertNotIn(SECRET, index.read_text())
        self.assertEqual(0o600, index.stat().st_mode & 0o777)
        self.assertEqual(0o700, index.parent.stat().st_mode & 0o777)
        self.assertNotIn(SECRET, json.dumps(login_vault.list_logins()))
        self.assertEqual(SECRET, login_vault.secret_for(row["id"]))

    def test_same_site_and_user_updates_and_remove_deletes_the_item(self) -> None:
        first = login_vault.add_login("github.com", "ada", SECRET)
        second = login_vault.add_login("https://www.github.com", "ada", "new-secret")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(login_vault.list_logins()))
        self.assertEqual("new-secret", login_vault.secret_for(first["id"]))
        self.assertTrue(login_vault.remove_login(first["id"]))
        self.assertEqual([], login_vault.list_logins())
        self.assertNotIn(first["id"], self.security.items)
        with self.assertRaises(login_vault.VaultError):
            login_vault.secret_for(first["id"])

    def test_invalid_input_is_refused(self) -> None:
        with self.assertRaises(login_vault.VaultError):
            login_vault.add_login("github.com", "ada", "two\nlines")
        with self.assertRaises(login_vault.VaultError):
            login_vault.add_login("github.com", "", SECRET)
        with self.assertRaises(login_vault.VaultError):
            login_vault.secret_for("../../etc")

    def test_logins_for_host_prefers_the_most_specific_site(self) -> None:
        login_vault.add_login("google.com", "personal", "a")
        login_vault.add_login("accounts.google.com", "work", "b")
        rows = login_vault.logins_for_host("accounts.google.com")
        self.assertEqual(["work", "personal"], [row["username"] for row in rows])
        self.assertEqual([], login_vault.logins_for_host("google.com.evil.io"))

    def test_passwords_export_preview_has_no_passwords_and_import_takes_chosen_rows(self) -> None:
        export = self.tmp / "Passwords.csv"
        with export.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Title", "URL", "Username", "Password", "Notes", "OTPAuth"])
            writer.writerow(["GitHub", "https://github.com/", "ada", SECRET, "", ""])
            writer.writerow(["Bank", "https://bank.example/", "ada", "bank-secret", "", ""])
            writer.writerow(["Broken", "", "x", "y", "", ""])
        preview = login_vault.preview_import(str(export))
        self.assertEqual([0, 1], [item["index"] for item in preview])
        self.assertNotIn(SECRET, json.dumps(preview))
        result = login_vault.import_logins(str(export), [0], delete_file=True)
        self.assertEqual({"imported": 1, "skipped": 0, "file_deleted": True}, result)
        self.assertFalse(export.exists())
        self.assertEqual(["github.com"], [row["site"] for row in login_vault.list_logins()])


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


class SignInTests(VaultCase):
    def run_sign_in(self, probe, fill=None, account=None, submit=False, fill_error=None):
        scripts = []

        def run_json(settings, browser, js, *args, **kwargs):
            scripts.append(js)
            if len(scripts) == 1:
                return probe
            if fill_error is not None:
                raise fill_error
            return fill if fill is not None else {"ok": True, "username": True, "password": True, "submitted": submit}

        class Lease:
            browser, window_index, tab_index, tab_handle, lease_generation = "Safari", 1, 1, "btab_safari_x", 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.object(agent, "_run_json_js", side_effect=run_json), \
             patch.object(agent, "_tab_lease", return_value=Lease()), \
             patch.object(agent, "_require_stable_handle_for_mutation"), \
             patch.object(agent, "delegated_agent_identity", return_value=None):
            result = sign_in.browser_sign_in(load_settings(), browser="Safari", tab_handle="btab_safari_x",
                                             account=account, submit=submit)
        return result, scripts

    def probe(self, host="github.com", protocol="https:", password=True, username=True):
        return {"ok": True, "host": host, "protocol": protocol, "password_field": password,
                "username_field": username, "username_filled": False}

    def test_fills_the_saved_login_and_returns_no_secret(self) -> None:
        login_vault.add_login("github.com", "ada@example.com", SECRET, label="Work")
        result, scripts = self.run_sign_in(self.probe(host="github.com"))
        self.assertTrue(result["ok"])
        self.assertEqual("Work", result["account"])
        self.assertTrue(result["filled_password"] and result["filled_username"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn(json.dumps(SECRET), scripts[1])  # only the page script carries it
        self.assertIn('if(location.hostname!==expected)', scripts[1])
        self.assertNotIn(SECRET, scripts[0])

    def test_lookalike_insecure_and_missing_cases_get_nothing(self) -> None:
        login_vault.add_login("github.com", "ada", SECRET)
        result, scripts = self.run_sign_in(self.probe(host="github.com.evil.io"))
        self.assertEqual("no_saved_login", result["error"])
        self.assertEqual(1, len(scripts))
        result, scripts = self.run_sign_in(self.probe(host="github.com", protocol="http:"))
        self.assertEqual("insecure_page", result["error"])
        self.assertEqual(1, len(scripts))
        result, _ = self.run_sign_in(self.probe(password=False, username=False))
        self.assertEqual("no_login_form", result["error"])

    def test_several_accounts_need_a_choice(self) -> None:
        login_vault.add_login("github.com", "ada", "a", label="Work")
        login_vault.add_login("github.com", "bob", "b", label="Personal")
        with self.assertRaises(HTTPException) as ctx:
            self.run_sign_in(self.probe())
        self.assertEqual("choose_account", ctx.exception.detail["error"])
        self.assertEqual({"Work", "Personal"}, set(ctx.exception.detail["accounts"]))
        result, scripts = self.run_sign_in(self.probe(), account="personal")
        self.assertEqual("Personal", result["account"])
        self.assertIn(json.dumps("b"), scripts[1])

    def test_username_step_sends_no_password(self) -> None:
        login_vault.add_login("github.com", "ada", SECRET)
        result, scripts = self.run_sign_in(self.probe(password=False), fill={"ok": True, "username": True, "password": False})
        self.assertIn("var expected=", scripts[1])
        self.assertNotIn(SECRET, scripts[1])
        self.assertIn("next_step", result)

    def test_a_failing_fill_does_not_leak_the_page_script(self) -> None:
        login_vault.add_login("github.com", "ada", SECRET)
        leaking = HTTPException(500, f"AppleScript error near: var pass={json.dumps(SECRET)}")
        with self.assertRaises(HTTPException) as ctx:
            self.run_sign_in(self.probe(), fill_error=leaking)
        self.assertNotIn(SECRET, json.dumps(ctx.exception.detail))
        result, _ = self.run_sign_in(self.probe(), fill={"ok": False, "error": "page_changed"})
        self.assertEqual("page_changed", result["error"])


class PageScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_password_fields_never_report_their_value(self) -> None:
        source = agent._bootstrap_functions_source()
        start = source.index("function __mcpSafeValue(")
        fn = source[start:source.index("\nfunction ", start + 10)]
        script = fn + "\nconsole.log(JSON.stringify([__mcpSafeValue({type:'password',value:'x'}),__mcpSafeValue({type:'password',value:''}),__mcpSafeValue({type:'text',value:'hi'}),__mcpSafeValue({value:'t'})]));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(["[hidden]", "", "hi", "t"], json.loads(out.stdout))
        self.assertIn("out.value=__mcpSafeValue(el).slice(0,160)", source)
        self.assertIn("value=('value' in el)?__mcpSafeValue(el):''", source)

    def test_fallback_osascript_gets_the_script_on_stdin(self) -> None:
        class Proc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                Proc.input = input
                return "ok", ""

        with patch.object(tools_browser.subprocess, "Popen", return_value=Proc()) as popen:
            self.assertEqual("ok", tools_browser._run_osascript_process(f'return "{SECRET}"', 5))
        self.assertEqual(["osascript", "-"], popen.call_args.args[0])
        self.assertIn(SECRET, Proc.input)


if __name__ == "__main__":
    unittest.main()
