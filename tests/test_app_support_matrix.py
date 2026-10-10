"""#129: capability discovery says exactly what each requested app supports."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import app_adapters
from mcp_server.app_adapters import mac_app, support_matrix
from mcp_server.security import load_settings

SETTINGS = load_settings()
REQUESTED = ("Messages", "Xcode", "Slack", "Visual Studio Code")
GENERIC = ("Slack", "Visual Studio Code")
US, RS = "\x1f", "\x1e"


class SupportMatrixTests(unittest.TestCase):
    def test_apps_without_an_adapter_report_generic_support_with_a_path_per_task(self) -> None:
        with patch.object(app_adapters, "_run") as run:
            for app in GENERIC:
                with self.subTest(app=app):
                    result = mac_app(SETTINGS, app=app, action="capabilities")
                    self.assertTrue(result["ok"])
                    self.assertFalse(result["supported"])
                    self.assertEqual("generic_ax_fallback", result["adapter"])
                    self.assertEqual([], result["actions"])
                    self.assertTrue(result["tasks"])
                    self.assertIn("native_api", result)
                    self.assertIsInstance(result["installed"], bool)
                    self.assertFalse(result["generic_fallback"]["automatic"])
        run.assert_not_called()  # discovery never scripts (or launches) the app

    def test_partial_apps_list_their_actions_and_remaining_tasks(self) -> None:
        result = mac_app(SETTINGS, app="iMessage", action="capabilities")
        self.assertEqual("Messages", result["app"])
        self.assertEqual(["find_chats"], result["actions"])
        self.assertIn("read_conversation", result["tasks"])

    def test_aliases_resolve_to_the_same_entry(self) -> None:
        for alias, name in (("vscode", "Visual Studio Code"), ("VS Code", "Visual Studio Code"),
                            ("iMessage", "Messages"), ("slack", "Slack"), ("xcode", "Xcode")):
            with self.subTest(alias=alias):
                self.assertEqual(name, mac_app(SETTINGS, app=alias, action="capabilities")["app"])

    def test_unsupported_actions_are_reported_not_implied(self) -> None:
        result = mac_app(SETTINGS, app="Slack", action="send_message")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_UNSUPPORTED", result["reason_code"])
        self.assertIn("find_conversation", result["tasks"])
        self.assertFalse(result["fallback"]["automatic"])

    def test_all_lists_first_party_and_generic_apps_together(self) -> None:
        result = mac_app(SETTINGS, app="all", action="capabilities")
        self.assertTrue(result["ok"])
        for app in GENERIC:
            self.assertEqual("generic_ax", result["apps"][app]["support"])
        self.assertEqual("partial_first_party", result["apps"]["Messages"]["support"])
        self.assertEqual(["find_chats"], result["apps"]["Messages"]["actions"])
        self.assertEqual(["list_workspaces"], result["apps"]["Xcode"]["actions"])
        self.assertEqual("first_party", result["apps"]["Notes"]["support"])
        self.assertIn("create_note", result["apps"]["Notes"]["actions"])

    def test_matrix_never_claims_first_party_actions_it_does_not_have(self) -> None:
        matrix = support_matrix()
        for app in GENERIC:
            self.assertNotIn("actions", matrix[app])
            self.assertEqual((), app_adapters.supported_actions(app))
        # Tasks without an adapter action keep their generic path.
        self.assertTrue(matrix["Messages"]["tasks"]["send_message"].startswith("generic_ax"))
        self.assertTrue(matrix["Xcode"]["tasks"]["build_or_test"].startswith("run_command"))

    def test_apps_outside_the_matrix_keep_the_generic_fallback(self) -> None:
        result = mac_app(SETTINGS, app="TextEdit", action="capabilities")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_UNSUPPORTED", result["reason_code"])


class MessagesAdapterTests(unittest.TestCase):
    RAW = RS.join([
        US.join(["any;-;+905551112233", "missing value", "Ada Lovelace", "+905551112233"]),
        US.join(["iMessage;+;chat42", "Engine Team", "Ada Lovelace; Charles Babbage", "ada@example.com; cb@example.com"]),
        US.join(["any;-;TikTok", "missing value", "missing value", "TikTok"]),
    ])

    def _find(self, **kwargs):
        with patch.object(app_adapters, "_run", return_value=self.RAW) as run:
            result = mac_app(SETTINGS, app="Messages", action="find_chats", **kwargs)
        return result, run.call_args.args[0]

    def test_chats_are_listed_with_people_and_handles(self) -> None:
        result, script = self._find()
        self.assertTrue(result["ok"])
        self.assertEqual(3, result["matched"])
        first, team, brand = result["chats"]
        self.assertEqual("Ada Lovelace", first["name"])  # unnamed chat: named after its people
        self.assertEqual(["Ada Lovelace", "Charles Babbage"], team["participants"])
        self.assertEqual(["TikTok"], brand["handles"])
        self.assertEqual([], brand["participants"])
        self.assertIn("does not expose message text", result["note"])
        self.assertNotIn("send", script.lower().replace("ascii", ""))

    def test_query_matches_name_person_or_handle_and_limit_bounds_the_list(self) -> None:
        self.assertEqual(["Engine Team"], [c["name"] for c in self._find(query="engine")[0]["chats"]])
        self.assertEqual(2, self._find(query="ada")[0]["matched"])
        self.assertEqual(1, self._find(query="cb@example")[0]["matched"])
        limited = self._find(limit=1)[0]
        self.assertEqual(1, limited["count"])
        self.assertTrue(limited["truncated"])

    def test_reading_never_launches_messages(self) -> None:
        with patch.object(app_adapters, "_run", return_value=app_adapters._NOT_RUNNING) as run:
            result = mac_app(SETTINGS, app="Messages", action="find_chats")
        script = run.call_args.args[0]
        self.assertLess(script.index('if application "Messages" is not running'), script.index('tell application "Messages"'))
        self.assertFalse(result["ok"])
        self.assertEqual("APP_NOT_RUNNING", result["reason_code"])
        self.assertFalse(result["retryable"])


class XcodeAdapterTests(unittest.TestCase):
    def test_workspaces_report_schemes_targets_and_the_active_one(self) -> None:
        raw = RS.join([
            US.join(["App.xcodeproj", "/p/App.xcodeproj", "true", "true", "App; AppTests", "App", "App; AppTests"]),
            US.join(["Lib.xcworkspace", "", "false", "false", "", "", ""]),
        ])
        with patch.object(app_adapters, "_run", return_value=raw):
            result = mac_app(SETTINGS, app="xcode", action="list_workspaces")
        self.assertTrue(result["ok"])
        app, lib = result["workspaces"]
        self.assertEqual({"name": "App.xcodeproj", "path": "/p/App.xcodeproj", "loaded": True, "active": True,
                          "schemes": ["App", "AppTests"], "active_scheme": "App", "targets": ["App", "AppTests"]}, app)
        self.assertEqual({"name": "Lib.xcworkspace", "path": None, "loaded": False, "active": False,
                          "schemes": [], "active_scheme": None, "targets": []}, lib)

    def test_reading_never_launches_xcode(self) -> None:
        with patch.object(app_adapters, "_run", return_value=app_adapters._NOT_RUNNING) as run:
            result = mac_app(SETTINGS, app="Xcode", action="list_workspaces")
        script = run.call_args.args[0]
        self.assertLess(script.index('if application "Xcode" is not running'), script.index('tell application "Xcode"'))
        self.assertEqual("APP_NOT_RUNNING", result["reason_code"])
        self.assertIn("xcodebuild -list", result["error"])

    def test_both_are_read_actions(self) -> None:
        self.assertTrue(app_adapters.is_read_action("find_chats"))
        self.assertTrue(app_adapters.is_read_action("list_workspaces"))


if __name__ == "__main__":
    unittest.main()
