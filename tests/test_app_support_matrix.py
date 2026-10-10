"""#129: capability discovery says exactly what each requested app supports."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import app_adapters
from mcp_server.app_adapters import mac_app, support_matrix
from mcp_server.security import load_settings

SETTINGS = load_settings()
REQUESTED = ("Messages", "Xcode", "Slack", "Visual Studio Code")


class SupportMatrixTests(unittest.TestCase):
    def test_requested_apps_report_generic_support_with_a_path_per_task(self) -> None:
        with patch.object(app_adapters, "_run") as run:
            for app in REQUESTED:
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
        for app in REQUESTED:
            self.assertEqual("generic_ax", result["apps"][app]["support"])
        self.assertEqual("first_party", result["apps"]["Notes"]["support"])
        self.assertIn("create_note", result["apps"]["Notes"]["actions"])

    def test_matrix_never_claims_first_party_actions_it_does_not_have(self) -> None:
        matrix = support_matrix()
        for app in REQUESTED:
            self.assertNotIn("actions", matrix[app])
            self.assertEqual((), app_adapters.supported_actions(app))

    def test_apps_outside_the_matrix_keep_the_generic_fallback(self) -> None:
        result = mac_app(SETTINGS, app="TextEdit", action="capabilities")
        self.assertFalse(result["ok"])
        self.assertEqual("APP_ADAPTER_UNSUPPORTED", result["reason_code"])


if __name__ == "__main__":
    unittest.main()
