from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_VIEW = ROOT / "menu_app" / "Sources" / "SettingsView.swift"
WINDOW_CONTROLLER = ROOT / "menu_app" / "Sources" / "SettingsWindowController.swift"


class SettingsInformationArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.view = SETTINGS_VIEW.read_text(encoding="utf-8")
        cls.window = WINDOW_CONTROLLER.read_text(encoding="utf-8")

    def test_sidebar_uses_user_intent_sections(self) -> None:
        section_start = self.view.index("private enum SettingsSection")
        section_end = self.view.index("private enum ConnectionsTab", section_start)
        section = self.view[section_start:section_end]
        for case in ("general", "agents", "permissions", "connections", "voice", "advanced"):
            self.assertIn(f"case {case}", section)
        self.assertNotIn("case subagents", section)
        self.assertNotIn("case browser", section)
        self.assertNotIn("case mobile", section)
        self.assertIn('TextField("Search Settings"', self.view)
        self.assertIn("ForEach(filteredSections)", self.view)

    def test_connections_owns_endpoint_browser_and_mobile_navigation(self) -> None:
        self.assertIn("private enum ConnectionsTab", self.view)
        self.assertIn("private var connectionsPane", self.view)
        self.assertIn("private var publicEndpointCard", self.view)
        self.assertIn('paneHeader(\n                    "Connections"', self.view)
        self.assertIn("if connectionsTab == .browser", self.view)
        self.assertIn('sectionLead("Browser Companions"', self.view)
        self.assertIn('sectionLead(\n                    "Mobile Access"', self.view)
        self.assertIn('"Choose a public endpoint above before pairing a phone."', self.view)

    def test_general_owns_update_surface_and_advanced_is_runtime_only(self) -> None:
        general_start = self.view.index("private var generalPane")
        sidebar_start = self.view.index("private var sidebar", general_start)
        general = self.view[general_start:sidebar_start]
        self.assertIn("updateCard", general)
        self.assertIn('"Open Connections"', general)

        advanced_start = self.view.index("private var advancedPane")
        update_helpers = self.view.index("private var currentUpdateCommit", advanced_start)
        advanced = self.view[advanced_start:update_helpers]
        self.assertIn('GroupBox("Runtime")', advanced)
        self.assertIn('GroupBox("Session Lifecycle")', advanced)
        self.assertNotIn("updateCard", advanced)
        self.assertNotIn("publicEndpointMode", advanced)
        self.assertIn(".number.grouping(.never)", advanced)

    def test_settings_window_is_resizable_with_bounded_size(self) -> None:
        self.assertIn('.styleMask = [.titled, .closable, .resizable]', self.window)
        self.assertIn('NSSize(width: 920, height: 640)', self.window)
        self.assertIn('NSSize(width: 820, height: 560)', self.window)
        self.assertIn('NSSize(width: 1240, height: 860)', self.window)
        self.assertIn('setFrameAutosaveName("MacMCPSettingsWindow")', self.window)


if __name__ == "__main__":
    unittest.main()
