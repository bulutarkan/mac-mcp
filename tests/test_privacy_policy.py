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
