"""#18/#19: the owner's pause, private mode and exclusions, enforced where capture happens."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mcp_server import privacy_policy


class PrivacySettings(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-privacy-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings = self.dir / "settings.json"
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        env.start()
        self.addCleanup(env.stop)
        # The rules are cached for a moment; later tests must not inherit a pause or an exclusion.
        self.addCleanup(privacy_policy._cache.update, {"at": -1.0, "value": None})
        self.rules()

    def rules(self, **privacy) -> None:
        self.settings.write_text(json.dumps({"privacy": privacy}), encoding="utf-8")
        privacy_policy._cache.update({"at": -1.0, "value": None})


class PolicyTests(PrivacySettings):
    def test_nothing_is_blocked_by_default(self) -> None:
        current = privacy_policy.policy()
        self.assertEqual((False, False, [], []), (current["paused"], current["private_mode"],
                                                 current["excluded_sites"], current["excluded_apps"]))
        privacy_policy.check_tool("screenshot")
        privacy_policy.check_site("https://bank.example.com/")
        privacy_policy.check_app("1Password 7")

    def test_sites_match_themselves_and_subdomains_only(self) -> None:
        self.rules(excluded_sites=["https://www.Chase.com/login", "mychart.org"])
        self.assertEqual(["chase.com", "mychart.org"], privacy_policy.policy()["excluded_sites"])
        self.assertEqual("chase.com", privacy_policy.excluded_site("https://secure.chase.com/x"))
        self.assertIsNone(privacy_policy.excluded_site("https://notchase.com/"))
        with self.assertRaises(privacy_policy.PrivacyBlocked) as caught:
            privacy_policy.check_site("https://chase.com/accounts")
        self.assertEqual(403, caught.exception.status_code)
        self.assertEqual("privacy_excluded_site", caught.exception.code)

    def test_apps_match_name_or_bundle_id(self) -> None:
        self.rules(excluded_apps=["1Password 7", "com.apple.MobileSMS"])
        with self.assertRaises(privacy_policy.PrivacyBlocked):
            privacy_policy.check_app("Messages", "com.apple.mobilesms")
        privacy_policy.check_app("Safari")
        self.rules(excluded_apps=["WhatsApp"])
        with self.assertRaises(privacy_policy.PrivacyBlocked):
            privacy_policy.check_screen(["Safari", "\u200eWhatsApp"])  # as CGWindowList names it

    def test_pause_stops_observation_tools_but_not_file_work(self) -> None:
        self.rules(observation_paused=True)
        for tool in ("screenshot", "browser_observe", "mac_observe", "clipboard_get", "ask_user_voice", "spawn_agent"):
            with self.subTest(tool=tool), self.assertRaises(privacy_policy.PrivacyBlocked):
                privacy_policy.check_tool(tool)
        privacy_policy.check_tool("read_file")
        privacy_policy.check_tool("run_command")

    def test_private_mode_keeps_nothing(self) -> None:
        self.rules(private_mode=True)
        self.assertFalse(privacy_policy.keeps_records())
        with self.assertRaises(privacy_policy.PrivacyBlocked):
            privacy_policy.check_tool("memory_add")
        privacy_policy.check_tool("browser_observe")

    def test_a_whole_screen_capture_is_refused_while_an_excluded_app_shows(self) -> None:
        self.rules(excluded_apps=["Banking"])
        with self.assertRaises(privacy_policy.PrivacyBlocked):
            privacy_policy.check_screen(["Finder", "Banking"])
        privacy_policy.check_screen(["Finder"])

    def test_listings_hide_titles_of_excluded_apps_and_sites(self) -> None:
        self.rules(excluded_apps=["Messages"], excluded_sites=["chase.com"])
        scrubbed = privacy_policy.scrub({"windows": [
            {"app": "Messages", "title": "Chat with Ayse"},
            {"app": "Safari", "title": "Chase accounts", "url": "https://chase.com/a"},
            {"app": "Notes", "title": "Groceries"},
        ]})["windows"]
        self.assertEqual("[excluded by privacy settings]", scrubbed[0]["title"])
        self.assertEqual(("", True), (scrubbed[1]["url"], scrubbed[1]["privacy_excluded"]))
        self.assertEqual("Groceries", scrubbed[2]["title"])

    def test_categories_label_but_never_block(self) -> None:
        self.assertEqual("password_manager", privacy_policy.app_category("1Password 7"))
        self.assertEqual("finance", privacy_policy.site_category("https://www.mybank.com/"))
        privacy_policy.check_app("1Password 7")


class EnforcementTests(PrivacySettings):
    def test_every_tool_call_meets_the_pause(self) -> None:
        from fastapi import HTTPException

        from mcp_server.main import _log

        self.rules(observation_paused=True)
        audit = unittest.mock.Mock()
        ran = []
        with self.assertRaises(HTTPException) as caught:
            _log(audit, "screenshot", lambda: ran.append(1))
        self.assertEqual(403, caught.exception.status_code)
        self.assertEqual([], ran)
        self.assertTrue(json.loads(audit.info.call_args.args[0])["outcome"].startswith("error:403:observation_paused"))
        self.assertEqual({"ok": True}, _log(audit, "read_file", lambda: {"ok": True}))

    def test_a_tab_showing_an_excluded_site_is_not_leased_for_reading(self) -> None:
        from mcp_server import browser_tabs, tools_browser

        self.rules(excluded_sites=["chase.com"])
        target = browser_tabs.TabTarget(browser="Safari", window_index=1, tab_index=1, tab_handle="btab_x",
                                        native_id="1", title="Chase", url="https://www.chase.com/", active=False)

        @contextmanager
        def lease(*args, **kwargs):
            yield target

        with patch.object(browser_tabs, "tab_lease", lease):
            with self.assertRaises(privacy_policy.PrivacyBlocked):
                with tools_browser._tab_lease("Safari", "btab_x", 1, None):
                    pass
            with tools_browser._tab_lease("Safari", "btab_x", 1, None, observe=False) as leased:
                self.assertIs(target, leased)  # closing or activating it still works

    def test_tab_listings_hide_excluded_sites(self) -> None:
        from mcp_server import browser_tabs, tools_browser
        from mcp_server.security import load_settings

        self.rules(excluded_sites=["chase.com"])
        rows = [{"tab_handle": "a", "title": "Chase", "url": "https://chase.com/"},
                {"tab_handle": "b", "title": "News", "url": "https://news.example/"}]
        with patch.object(browser_tabs, "list_tabs", return_value=rows), \
                patch.object(tools_browser, "_background_capability_summary", return_value=None, create=True):
            tabs = tools_browser.browser_list_tabs(load_settings(), "Safari")["tabs"]
        self.assertEqual(("", True, "a"), (tabs[0]["url"], tabs[0]["privacy_excluded"], tabs[0]["tab_handle"]))
        self.assertEqual("News", tabs[1]["title"])

    def test_screenshot_is_refused_when_an_excluded_app_is_on_screen_or_unknown(self) -> None:
        from mcp_server import tools_macos
        from mcp_server.security import load_settings

        self.rules(excluded_apps=["Messages"])
        with patch.object(tools_macos, "_visible_window_owners", return_value=["Finder", "Messages"]), \
                patch.object(tools_macos.subprocess, "run") as run, self.assertRaises(privacy_policy.PrivacyBlocked):
            tools_macos.screenshot(load_settings(), path=str(self.dir / "s.png"))
        run.assert_not_called()
        with patch.object(tools_macos, "_visible_window_owners", return_value=None), \
                self.assertRaisesRegex(privacy_policy.PrivacyBlocked, "privacy_check_unavailable"):
            tools_macos.screenshot(load_settings(), path=str(self.dir / "s.png"))

    def test_native_targets_in_excluded_apps_are_refused(self) -> None:
        from mcp_server import tools_ui

        self.rules(excluded_apps=["com.apple.MobileSMS"])
        blocked = tools_ui._privacy_block_names("Messages", "com.apple.MobileSMS")
        self.assertEqual(("PRIVACY_EXCLUDED_APP", False), (blocked["reason_code"], blocked["ok"]))
        self.assertIsNone(tools_ui._privacy_block_names("Notes", "com.apple.Notes"))

    def test_private_mode_keeps_no_tool_rows(self) -> None:
        from mcp_server.observability import TelemetryManager

        telemetry = TelemetryManager(db_path=self.dir / "telemetry.sqlite3")
        self.rules(private_mode=True)
        telemetry.finish_call(telemetry.start_call("mcp", "read_file", {}), result={"ok": True})
        telemetry.wait_events_idle()
        self.assertEqual({"first_call": None, "first_success": None}, telemetry.first_client_calls())
        self.rules()
        telemetry.finish_call(telemetry.start_call("mcp", "read_file", {}), result={"ok": True})
        telemetry.wait_events_idle()
        self.assertEqual("read_file", telemetry.first_client_calls()["first_call"]["tool"])


class DashboardTests(PrivacySettings):
    TOKEN = "privacy-route-token"

    def client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        telemetry = TelemetryManager(db_path=self.dir / "t.sqlite3")
        return TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), self.TOKEN)))

    def test_owner_sets_rules_and_sees_stores_and_recipients(self) -> None:
        client = self.client()
        auth = {"authorization": f"Bearer {self.TOKEN}"}
        self.assertEqual(401, client.get("/dashboard/api/privacy").status_code)
        saved = client.post("/dashboard/api/privacy/settings", headers=auth, json={
            "paused": True, "excluded_sites": ["chase.com", " chase.com "], "excluded_apps": ["Messages"]}).json()
        self.assertEqual((True, ["chase.com"], ["messages"]), (saved["paused"], saved["excluded_sites"], saved["excluded_apps"]))
        stored = json.loads(self.settings.read_text())["privacy"]
        self.assertEqual((True, ["chase.com"]), (stored["observation_paused"], stored["excluded_sites"]))
        self.assertEqual(400, client.post("/dashboard/api/privacy/settings", headers=auth, json={"paused": "yes"}).status_code)
        overview = client.get("/dashboard/api/privacy", headers=auth).json()
        self.assertIn("tool_activity", {row["key"] for row in overview["stores"]})
        self.assertIn("ai_apps", {row["key"] for row in overview["recipients"]})
        self.assertTrue(all("owner_only" in row and "kept" in row for row in overview["stores"]))


class PathsTests(unittest.TestCase):
    def test_environment_overrides_win_and_defaults_are_unchanged(self) -> None:
        from mcp_server import paths

        home = Path.home() / ".mac-mcp"
        with patch.dict(os.environ, {}, clear=False):
            for name in ("MAC_MCP_STATE_DIR", "MAC_MCP_SETTINGS_PATH", "MAC_MCP_MEMORY_DIR", "MAC_MCP_FILE_JOURNAL_DIR"):
                os.environ.pop(name, None)
            self.assertEqual(home, paths.state_dir())
            self.assertEqual(home / "settings.json", paths.settings_path())
            self.assertEqual(home / "memory", paths.memory_dir())
            self.assertEqual(home / "transactions", paths.transactions_dir())
        with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": "/tmp/state with space", "MAC_MCP_MEMORY_DIR": "~/mem"}):
            self.assertEqual(Path("/tmp/state with space"), paths.state_dir())
            self.assertEqual(Path("/tmp/state with space/runs"), paths.runs_dir())
            self.assertEqual(Path("~/mem").expanduser(), paths.memory_dir())
            # Memory and settings default to ~/.mac-mcp even with another state root, as they shipped.
            os.environ.pop("MAC_MCP_MEMORY_DIR")
            self.assertEqual(home / "memory", paths.memory_dir())

    def test_no_module_resolves_the_state_root_on_its_own(self) -> None:
        root = Path(__file__).resolve().parents[1] / "mcp_server"
        offenders = [path.name for path in root.glob("*.py")
                     if path.name != "paths.py" and 'os.getenv("MAC_MCP_STATE_DIR", str(Path.home()' in path.read_text()]
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()


class AtRestTests(PrivacySettings):
    """#24: what protects stored data on disk, and keeping it out of backups."""

    TOKEN = DashboardTests.TOKEN

    def run_result(self, stdout: str, code: int = 0):
        import subprocess

        return subprocess.CompletedProcess([], code, stdout, "")

    def test_filevault_and_backup_states_are_read_not_guessed(self) -> None:
        from mcp_server import privacy_inventory as inv

        for stdout, expected in (("true\n", True), ("false\n", False), ("", None)):
            with self.subTest(stdout=stdout), patch.object(inv.subprocess, "run", return_value=self.run_result(stdout)):
                self.assertEqual(expected, inv.filevault_on())
        for stdout, expected in (("[Excluded]    /x\n", True), ("[Included]    /x\n", False), ("", None)):
            with self.subTest(stdout=stdout), patch.object(inv.subprocess, "run", return_value=self.run_result(stdout)):
                self.assertEqual(expected, inv.backup_excluded(self.dir))

    def test_backup_exclusion_covers_content_stores_only(self) -> None:
        from mcp_server import privacy_inventory as inv

        stores = [{"key": "memory", "paths": [str(self.dir)]}, {"key": "audit_log", "paths": [str(self.dir)]},
                  {"key": "jobs", "paths": [str(self.dir / "missing")]}]
        calls = []
        with patch.object(inv, "stores", return_value=stores), \
                patch.object(inv.subprocess, "run", side_effect=lambda args, **k: calls.append(args) or self.run_result("")):
            result = inv.set_backup_exclusion(True)
        self.assertEqual([["/usr/bin/xattr", "-wx", inv._EXCLUDE_XATTR, inv._EXCLUDE_VALUE, str(self.dir)]], calls)
        self.assertEqual([str(self.dir)], result["changed"])

    def test_dashboard_applies_the_backup_choice(self) -> None:
        from mcp_server import privacy_inventory as inv

        client = DashboardTests.client(self)
        auth = {"authorization": f"Bearer {self.TOKEN}"}
        with patch.object(inv, "set_backup_exclusion", return_value={"changed": ["/x"], "failed": []}) as apply:
            body = client.post("/dashboard/api/privacy/settings", headers=auth, json={"exclude_from_backups": True}).json()
        apply.assert_called_once_with(True)
        self.assertEqual((True, ["/x"]), (body["exclude_from_backups"], body["backups"]["changed"]))
        with patch.object(inv, "filevault_on", return_value=True), patch.object(inv, "backup_excluded", return_value=True):
            overview = client.get("/dashboard/api/privacy", headers=auth).json()
        self.assertTrue(overview["filevault"])

    def test_logs_in_the_state_root_become_owner_only(self) -> None:
        from mcp_server.private_storage import secure_content_stores

        log = self.dir / "restart-1.log"
        log.write_text("x")
        log.chmod(0o644)
        secure_content_stores(state_dir=self.dir)
        self.assertEqual(0o600, log.stat().st_mode & 0o777)


class CategoryAndSourceTests(PrivacySettings):
    TOKEN = "privacy-route-token"
    def test_categories_block_only_when_the_owner_chooses(self) -> None:
        privacy_policy.check_site("https://www.mybank.com/")
        self.rules(blocked_categories=["finance", "bogus"])
        self.assertEqual(["finance"], privacy_policy.policy()["blocked_categories"])
        with self.assertRaisesRegex(privacy_policy.PrivacyBlocked, "privacy_sensitive_site"):
            privacy_policy.check_site("https://www.mybank.com/")
        privacy_policy.check_app("1Password 7")  # password managers not chosen
        self.rules(blocked_categories=["password_manager"])
        with self.assertRaisesRegex(privacy_policy.PrivacyBlocked, "privacy_sensitive_app"):
            privacy_policy.check_app("1Password 7")

    def test_results_say_what_was_read_and_why_it_was_allowed(self) -> None:
        from mcp_server.main import _log

        def read_page():
            privacy_policy.check_site("https://www.mybank.com/statements")
            return {"ok": True}

        result = _log(unittest.mock.Mock(), "browser_observe", read_page)
        self.assertEqual("www.mybank.com", result["privacy_source"]["site"])
        self.assertEqual("finance", result["privacy_source"]["category"])
        self.assertIn("labeled only", result["privacy_source"]["rule"])
        self.assertNotIn("privacy_source", _log(unittest.mock.Mock(), "read_file", lambda: {"ok": True}))

    def test_json_text_results_carry_the_source_too(self) -> None:
        from mcp_server.main import _log

        def observe_text():
            privacy_policy.check_app("Finder")
            return '{"ok":true,"nodes":[]}'

        text = _log(unittest.mock.Mock(), "mac_observe", observe_text)
        self.assertEqual("Finder", json.loads(text)["privacy_source"]["app"])
        listed = _log(unittest.mock.Mock(), "mac_observe", lambda: [observe_text(), object()])
        self.assertEqual("Finder", json.loads(listed[0])["privacy_source"]["app"])
        self.assertEqual("plain", _log(unittest.mock.Mock(), "mac_observe", lambda: (privacy_policy.check_app("X"), "plain")[1]))

    def test_dashboard_validates_categories(self) -> None:
        client = DashboardTests.client(self)
        auth = {"authorization": f"Bearer {DashboardTests.TOKEN}"}
        self.assertEqual(400, client.post("/dashboard/api/privacy/settings", headers=auth,
                                          json={"blocked_categories": ["everything"]}).status_code)
        body = client.post("/dashboard/api/privacy/settings", headers=auth,
                           json={"blocked_categories": ["health", "health"]}).json()
        self.assertEqual(["health"], body["blocked_categories"])


class DeleteAndNoticeTests(PrivacySettings):
    TOKEN = "privacy-route-token"
    def test_delete_previews_then_moves_only_chosen_stores_to_the_trash(self) -> None:
        import argparse
        from contextlib import redirect_stdout
        from io import StringIO

        from mcp_server import cli

        state = self.dir / "state"
        runs = state / "runs"
        runs.mkdir(parents=True)
        (runs / "run.json").write_text("{}")
        memory = self.dir / "memory"
        memory.mkdir()
        trash = self.dir / "Trash"
        trash.mkdir()
        env = {"MAC_MCP_STATE_DIR": str(state), "MAC_MCP_MEMORY_DIR": str(memory)}
        with patch.dict(os.environ, env), patch.object(cli, "_trash_root", return_value=trash), \
                patch.object(cli, "STATE_DIR", state):
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, cli.privacy_delete(argparse.Namespace(all=False, store=["run_evidence"],
                                                                          yes=False, json=False)))
            self.assertIn("Nothing was changed", out.getvalue())
            self.assertTrue(runs.exists())
            out = StringIO()
            with redirect_stdout(out):
                code = cli.privacy_delete(argparse.Namespace(all=False, store=["run_evidence"], yes=True, json=True))
        self.assertEqual(0, code)
        result = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertFalse(runs.exists())
        self.assertTrue(memory.exists())  # not chosen
        self.assertEqual(["run_evidence"], [row["store"] for row in result["moved"]])
        self.assertTrue(Path(result["moved"][0]["to"]).joinpath("run.json").exists())
        self.assertIsNone(result["restarted"])  # not the default install: nothing stopped or started

    def test_settings_and_audit_cannot_be_deleted_this_way(self) -> None:
        import argparse
        from contextlib import redirect_stderr
        from io import StringIO

        from mcp_server import cli

        with redirect_stderr(StringIO()):
            self.assertEqual(2, cli.privacy_delete(argparse.Namespace(all=False, store=["audit_log"], yes=True, json=True)))

    def test_started_agents_name_where_their_data_goes(self) -> None:
        from mcp_server import privacy_inventory as inv

        self.assertIn("OpenAI (Codex)", inv.with_provider_notice({"provider": "codex"})["data_notice"])
        team = inv.with_provider_notice({"agents": [{"provider": "claude"}, {"provider": "codex"}]})
        self.assertIn("Anthropic", team["data_notice"])
        self.assertNotIn("data_notice", inv.with_provider_notice({"provider": "unknown"}))

    def test_menu_summary_carries_pause_and_private_mode(self) -> None:
        self.rules(observation_paused=True)
        client = DashboardTests.client(self)
        body = client.get("/dashboard/api/summary", headers={"authorization": f"Bearer {DashboardTests.TOKEN}"}).json()
        self.assertEqual({"paused": True, "private_mode": False}, body["privacy"])


class StoreSizeTests(unittest.TestCase):
    def test_sizes_cover_every_file_however_many(self) -> None:
        # 2026-10-13: a 20,000-file cap showed the 3.5 GB agents store as 1.3 GB.
        from mcp_server import privacy_inventory as inv

        root = Path(tempfile.mkdtemp(prefix="mac-mcp-sizes-"))
        self.addCleanup(shutil.rmtree, root, True)
        for index in range(30):
            (root / f"f{index}").write_bytes(b"x" * 10_000)
        measured = inv._measure([root])
        self.assertEqual(30, measured["files"])
        self.assertGreaterEqual(measured["bytes"], 300_000)
        self.assertLess(measured["bytes"], 400_000)


class StatusPrivacyTests(PrivacySettings):
    def test_status_says_when_observation_is_paused(self) -> None:
        import argparse
        from contextlib import redirect_stdout
        from io import StringIO

        from mcp_server import cli
        from mcp_server.managed_process import ProcessValidation

        self.rules(observation_paused=True, excluded_sites=["chase.com"])
        missing = ProcessValidation("missing", None, "ngrok", None, "record_missing")
        public = type("Public", (), {"mode": "none", "endpoint_url": None})()
        out = StringIO()
        with patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
                patch.object(cli, "_validate_managed_pid", return_value=missing), \
                patch.object(cli, "_launchctl_pid", return_value=None), \
                patch.object(cli, "_server_health", return_value="ok"), \
                patch.object(cli, "resolve_public_endpoint", return_value=public), \
                patch.object(cli, "supervisor_report", return_value={"enabled": True, "loaded": True}), \
                redirect_stdout(out):
            cli.status(argparse.Namespace(local_only=False, json=True))
        report = json.loads(out.getvalue())
        self.assertEqual({"paused": True, "private_mode": False, "excluded_apps": 0, "excluded_sites": 1},
                         report["privacy"])


class BackupStoreTests(unittest.TestCase):
    def test_backups_move_to_the_trash_but_package_rollback_stays(self) -> None:
        from mcp_server import privacy_inventory as inv

        root = Path(tempfile.mkdtemp(prefix="mac-mcp-backups-"))
        self.addCleanup(shutil.rmtree, root, True)
        state = root / "state"
        (state / "backups" / "promote-1").mkdir(parents=True)
        (state / "backups" / "promote-1" / "cli.py").write_text("x")
        (state / "backups" / "pkg-123").mkdir()
        trash = root / "Trash"
        trash.mkdir()
        with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(state)}):
            self.assertIn("backups", {row["key"] for row in inv.stores()})
            result = inv.trash_stores(["backups"], trash_root=trash, stamp="t")
        self.assertEqual(["promote-1"], [Path(row["path"]).name for row in result["moved"]])
        self.assertTrue((state / "backups" / "pkg-123").exists())
        self.assertTrue((trash / "Mac MCP data t" / "backups" / "promote-1" / "cli.py").exists())


class AppAdapterExclusionTests(PrivacySettings):
    """An app excluded in Settings › Privacy is not read or operated through mac_app either. Notes, Messages
    and Photos checked this themselves; Mail, Calendar, Reminders, Contacts and the rest did not."""

    def test_every_adapter_refuses_an_excluded_app_before_running_anything(self) -> None:
        from mcp_server import app_adapters
        from mcp_server.security import load_settings

        cases = [("Mail", "find_messages", {"query": "invoice"}), ("Calendar", "find_events", {"query": "standup"}),
                 ("Reminders", "list_reminders", {}), ("Contacts", "find_contacts", {"query": "Ali"}),
                 ("Mail", "create_draft", {"title": "Hi", "body": "x"}), ("Finder", "selection", {})]
        self.rules(excluded_apps=["com.apple.mail", "Calendar", "com.apple.reminders", "Contacts", "Finder"])
        with patch.object(app_adapters, "_run", side_effect=AssertionError("the app must not be scripted")):
            for app, action, args in cases:
                with self.subTest(app=app, action=action):
                    result = app_adapters.mac_app(load_settings(), app=app, action=action, **args)
                    self.assertEqual(("privacy_excluded_app", False, True),  # as Notes and Messages answered
                                     (result["reason_code"], result["ok"], result["privacy_blocked"]))
        # Capabilities still answer, and an app that is not excluded still runs.
        self.assertTrue(app_adapters.mac_app(load_settings(), app="Mail", action="capabilities")["ok"])
        with patch.object(app_adapters, "_run", return_value="") as ran:
            notes = app_adapters.mac_app(load_settings(), app="Notes", action="find_notes", query="plan")
        self.assertTrue(ran.called)
        self.assertNotEqual("privacy_excluded_app", notes.get("reason_code"))

    def test_every_adapter_app_is_mapped_to_its_bundle(self) -> None:
        from mcp_server import app_adapters

        self.assertEqual(set(app_adapters._ACTIONS) - {"Mission Control"}, set(app_adapters._BUNDLE_IDS))
