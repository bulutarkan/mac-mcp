"""#163: saved logins that agents can use without seeing the password."""
from __future__ import annotations

import base64
import csv
import json
import logging
import os
import shutil
import sqlite3
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
from mcp_server.observability import TelemetryManager
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
        for lookalike in ("github.com.evil.io", "evilgithub.com", "g1thub.com", "github.co"):
            with self.subTest(host=lookalike):
                self.assertFalse(login_vault.site_matches("github.com", lookalike))
                self.assertFalse(login_vault.site_matches("github.com", lookalike, subdomains=True))

    def test_subdomains_only_when_the_login_allows_them(self) -> None:
        # #178: a subdomain may be run by someone else, so the exact site is the default.
        self.assertFalse(login_vault.site_matches("google.com", "accounts.google.com"))
        self.assertTrue(login_vault.site_matches("google.com", "accounts.google.com", subdomains=True))

    def test_shared_hosting_never_shares_a_login_across_subdomains(self) -> None:
        for saved, host in (("github.io", "evil.github.io"), ("ada.github.io", "x.ada.github.io"),
                            ("netlify.app", "shop.netlify.app"), ("blogspot.com", "news.blogspot.com")):
            with self.subTest(saved=saved, host=host):
                self.assertFalse(login_vault.site_matches(saved, host, subdomains=True))
        self.assertTrue(login_vault.site_matches("ada.github.io", "ada.github.io", subdomains=True))


class VaultTests(VaultCase):
    def test_password_goes_to_the_keychain_on_stdin_and_nowhere_else(self) -> None:
        row = login_vault.add_login("https://github.com/login", "ada@example.com", SECRET, label="Work")
        self.assertEqual({"id", "site", "username", "label", "source", "created_at", "updated_at", "subdomains", "subdomains_possible"}, set(row))
        self.assertFalse(row["subdomains"])
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

    def test_new_logins_stay_on_their_site_until_the_owner_allows_subdomains(self) -> None:
        row = login_vault.add_login("google.com", "ada", SECRET)
        self.assertEqual([], login_vault.logins_for_host("accounts.google.com"))
        self.assertEqual("google.com", login_vault.site_allowing_subdomains_would_match("accounts.google.com"))
        self.assertTrue(login_vault.set_subdomains(row["id"], True)["subdomains"])
        self.assertEqual(["ada"], [r["username"] for r in login_vault.logins_for_host("accounts.google.com")])
        self.assertIsNone(login_vault.site_allowing_subdomains_would_match("accounts.google.com"))
        # Saving the same login again (a new password) keeps the owner's choice.
        self.assertTrue(login_vault.add_login("google.com", "ada", "new")["subdomains"])
        self.assertFalse(login_vault.set_subdomains(row["id"], False)["subdomains"])
        with self.assertRaises(login_vault.VaultError):
            login_vault.set_subdomains("lv_000000000000", True)

    def test_logins_saved_before_178_keep_working_on_subdomains(self) -> None:
        login_vault.add_login("google.com", "ada", SECRET)
        index = self.tmp / "state" / "vault" / "logins.json"
        rows = json.loads(index.read_text())
        for row in rows:
            row.pop("subdomains", None)
        index.write_text(json.dumps(rows))
        self.assertTrue(login_vault.list_logins()[0]["subdomains"])
        self.assertEqual(["ada"], [r["username"] for r in login_vault.logins_for_host("accounts.google.com")])

    def test_shared_hosting_login_is_never_offered_to_a_neighbour(self) -> None:
        row = login_vault.add_login("ada.github.io", "ada", SECRET, subdomains=True)
        self.assertFalse(row["subdomains_possible"])
        self.assertEqual([], login_vault.logins_for_host("x.ada.github.io"))
        self.assertIsNone(login_vault.site_allowing_subdomains_would_match("x.ada.github.io"))
        self.assertEqual([row["id"]], [r["id"] for r in login_vault.logins_for_host("ada.github.io")])

    def test_cli_saves_and_changes_the_subdomain_choice(self) -> None:
        import contextlib
        import io
        from mcp_server import cli

        def run(*argv, stdin=""):
            out = io.StringIO()
            with patch.object(cli.sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
                code = cli.main(["logins", *argv])
            return code, json.loads(out.getvalue().strip().splitlines()[-1])

        code, saved = run("add", "--site", "google.com", "--username", "ada", "--subdomains", "--json", stdin=SECRET + "\n")
        self.assertEqual(0, code)
        self.assertTrue(saved["login"]["subdomains"])
        code, changed = run("subdomains", saved["login"]["id"], "off", "--json")
        self.assertEqual(0, code)
        self.assertFalse(changed["login"]["subdomains"])
        self.assertEqual([], login_vault.logins_for_host("accounts.google.com"))

    def test_logins_for_host_prefers_the_most_specific_site(self) -> None:
        login_vault.add_login("google.com", "personal", "a", subdomains=True)
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
    def run_sign_in(self, probe, fill=None, account=None, submit=False, fill_error=None, frame=None):
        scripts = []
        self.frames_seen = []

        def run_json(settings, browser, js, *args, **kwargs):
            from mcp_server.tools_browser import current_frame_scope
            self.frames_seen.append(current_frame_scope())
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
                                             account=account, submit=submit, frame=frame)
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

    def test_a_login_form_in_a_child_frame_gets_the_frames_own_login(self) -> None:
        # #36: both page scripts run inside the frame, so the site is the frame's, never the page around it.
        login_vault.add_login("idmsa.apple.com", "ada@icloud.com", SECRET)
        login_vault.add_login("www.icloud.com", "page-login", "other-secret-value")
        result, scripts = self.run_sign_in(self.probe(host="idmsa.apple.com"), frame="idmsa.apple.com")
        self.assertTrue(result["ok"])
        self.assertEqual("idmsa.apple.com", result["site"])
        self.assertEqual("idmsa.apple.com", result["frame"])
        self.assertEqual(["idmsa.apple.com", "idmsa.apple.com"], self.frames_seen)
        self.assertIn(json.dumps("idmsa.apple.com"), scripts[1])  # the fill re-checks the frame's host
        self.assertNotIn("other-secret-value", scripts[1])
        # No frame: scripts run in the top page as before, and the frame scope does not leak out.
        login_vault.add_login("github.com", "ada", SECRET)
        result, _ = self.run_sign_in(self.probe(host="github.com"))
        self.assertEqual([None, None], self.frames_seen)
        self.assertNotIn("frame", result)
        from mcp_server.tools_browser import current_frame_scope
        self.assertIsNone(current_frame_scope())

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

    def test_a_subdomain_gets_nothing_and_says_where_the_login_is_saved(self) -> None:
        login_vault.add_login("github.com", "ada", SECRET)
        result, scripts = self.run_sign_in(self.probe(host="gist.github.com"))
        self.assertFalse(result["ok"])
        self.assertEqual("login_for_other_site", result["error"])
        self.assertIn("only for github.com", result["message"])
        self.assertIn("Saved Logins", result["message"])
        self.assertEqual(1, len(scripts))
        self.assertNotIn(SECRET, json.dumps(result))

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

    def test_a_new_password_only_form_gets_nothing(self) -> None:
        # Roadmap #31: a sign-up or password-change form is never a place for a saved login.
        login_vault.add_login("github.com", "ada", SECRET)
        probe = {**self.probe(), "new_password_only": True, "password_field": False, "username_field": False}
        result, scripts = self.run_sign_in(probe, submit=True)
        self.assertEqual("not_a_sign_in_form", result["error"])
        self.assertEqual(1, len(scripts))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_telemetry_and_logs_never_hold_the_secret(self) -> None:
        # #81 acceptance: the telemetry row and every log line of a sign-in carry no secret bytes.
        login_vault.add_login("github.com", "ada@example.com", SECRET)
        arguments = {"browser": "Safari", "tab_handle": "btab_safari_x", "submit": True, "description": "Sign in"}
        records: list = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            result, _ = self.run_sign_in(self.probe(), submit=True)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertNotIn(SECRET, " ".join(record.getMessage() for record in records))
        db = self.tmp / "telemetry.sqlite3"
        manager = TelemetryManager(db, usage_enabled=False, async_writes=False)
        manager.finish_call(manager.start_call("mcp", "browser_sign_in", arguments), result=result)
        con = sqlite3.connect(db)
        try:
            stored = " ".join(str(v or "") for row in con.execute("SELECT * FROM tool_events") for v in row)
        finally:
            con.close()
        self.assertIn("browser_sign_in", stored)
        self.assertNotIn(SECRET, stored)

    def test_a_login_removed_mid_session_is_not_filled_again(self) -> None:
        saved = login_vault.add_login("github.com", "ada", SECRET)
        result, scripts = self.run_sign_in(self.probe())
        self.assertTrue(result["ok"])
        login_vault.remove_login(saved["id"])
        result, scripts = self.run_sign_in(self.probe())
        self.assertEqual("no_saved_login", result["error"])
        self.assertEqual(1, len(scripts))  # only the probe ran; no page script carried a password

    def test_a_failing_fill_does_not_leak_the_page_script(self) -> None:
        login_vault.add_login("github.com", "ada", SECRET)
        leaking = HTTPException(500, f"AppleScript error near: var pass={json.dumps(SECRET)}")
        with self.assertRaises(HTTPException) as ctx:
            self.run_sign_in(self.probe(), fill_error=leaking)
        self.assertNotIn(SECRET, json.dumps(ctx.exception.detail))
        result, _ = self.run_sign_in(self.probe(), fill={"ok": False, "error": "page_changed"})
        self.assertEqual("page_changed", result["error"])


class ShadowDomTests(unittest.TestCase):
    def test_fields_and_submit_reach_into_open_shadow_roots(self) -> None:
        # Reddit renders its login inputs inside faceplate-text-input shadow roots.
        script = sign_in._fill_js("www.reddit.com", "u", "p", True)
        self.assertIn("if(e.shadowRoot)deep(e.shadowRoot,sel,out)", script)
        self.assertIn("deep(document,'input[type=password]')", script)
        self.assertIn("var form=formOf(f.pw)", script)
        self.assertIn("node=r&&r.host?r.host:null", script)
        self.assertIn("/^\\s*(log ?in|sign ?in", script)
        if shutil.which("node"):
            out = subprocess.run(["node", "-e", f"new Function({json.dumps(script)})"], capture_output=True, text=True)
            self.assertEqual(0, out.returncode, out.stderr)


class PageScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_script_never_picks_a_new_password_field(self) -> None:
        harness = r"""
function el(type, auto, name){return {type:type,autocomplete:auto||'',name:name||'',id:'',placeholder:'',disabled:false,readOnly:false,value:'',
  form:null,getAttribute:function(k){return k==='type'?type:null;},getBoundingClientRect:function(){return {width:20,height:20};},
  closest:function(){return null;},getRootNode:function(){return {};},compareDocumentPosition:function(){return 4;},
  querySelectorAll:function(){return [];}};}
globalThis.Node={DOCUMENT_POSITION_FOLLOWING:4};
globalThis.getComputedStyle=function(){return {visibility:'visible',display:'block'};};
function page(inputs){globalThis.document={querySelectorAll:function(sel){
  if(sel==='input[type=password]')return inputs.filter(function(e){return e.type==='password';});
  if(sel==='input')return inputs;return [];}};
  var f=fields();return {pw:f.pw?f.pw.autocomplete:null,user:f.user?f.user.name:null,newOnly:f.newOnly};}
var email=el('email','email','email');
console.log(JSON.stringify([
  page([email, el('password','new-password'), el('password','new-password')]),
  page([email, el('password','current-password'), el('password','new-password')]),
  page([email, el('password','')]),
]));
"""
        out = subprocess.run(["node", "-e", sign_in._FIELDS_JS + harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, out.returncode, out.stderr)
        signup, change, login = json.loads(out.stdout)
        self.assertEqual({"pw": None, "user": None, "newOnly": True}, signup)
        self.assertEqual("current-password", change["pw"])
        self.assertFalse(change["newOnly"])
        self.assertEqual({"pw": "", "user": "email", "newOnly": False}, login)

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
