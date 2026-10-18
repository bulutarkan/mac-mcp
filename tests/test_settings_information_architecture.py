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
        section_start = self.view.index("enum SettingsSection: String")
        section_end = self.view.index("private enum ConnectionsTab", section_start)
        section = self.view[section_start:section_end]
        for case in ("general", "agents", "activity", "permissions", "connections", "voice", "advanced"):
            self.assertIn(f"case {case}", section)
        self.assertNotIn("case subagents", section)
        self.assertNotIn("case browser", section)
        self.assertNotIn("case mobile", section)
        self.assertIn('TextField("Search Settings"', self.view)
        self.assertIn("filteredSections.filter { $0.group == group }", self.view)

    def test_sidebar_groups_every_section_under_a_few_headings(self) -> None:
        # #250: fourteen destinations under four headings (the first group has none), no new entries.
        groups = self.view[self.view.index("enum SettingsSectionGroup"):self.view.index("private enum ConnectionsTab")]
        for title in ('"Agents"', '"Safety"', '"Connect"', '"Support"'):
            self.assertIn(f"return {title}", groups)
        mapping = self.view[self.view.index("var group: SettingsSectionGroup"):self.view.index("enum SettingsSectionGroup")]
        cases = self.view[self.view.index("enum SettingsSection: String"):self.view.index("var id: String { rawValue }")]
        for name in [line.strip()[5:] for line in cases.splitlines() if line.strip().startswith("case ")]:
            self.assertIn(f".{name}", mapping, name)
        sidebar = self.view[self.view.index("private var sidebar"):self.view.index("private func sidebarRow")]
        self.assertIn("ScrollView", sidebar)
        self.assertIn("proxy.scrollTo(selection)", sidebar)
        self.assertIn(".accessibilityAddTraits(.isHeader)", sidebar)

    def test_connections_owns_endpoint_browser_and_mobile_navigation(self) -> None:
        self.assertIn("private enum ConnectionsTab", self.view)
        self.assertIn("private var connectionsPane", self.view)
        self.assertIn("private var publicEndpointCard", self.view)
        self.assertIn('paneHeader(\n                    "Connections"', self.view)
        self.assertIn("switch connectionsTab {\n            case .browser: browserPane", self.view)
        self.assertIn("case .clients: clientsPane", self.view)
        self.assertIn('sectionLead("Browser Companions"', self.view)
        self.assertIn('sectionLead(\n                    "Mobile Access"', self.view)
        self.assertIn('"Choose a public endpoint above before pairing a phone."', self.view)

    def test_general_owns_update_surface_and_advanced_is_runtime_only(self) -> None:
        general_start = self.view.index("private var generalPane")
        sidebar_start = self.view.index("private var sidebar", general_start)
        general = self.view[general_start:sidebar_start]
        self.assertIn("updateCard", general)
        self.assertIn('"Open Connections"', general)
        # #64: General is a status overview; activity and alert controls have their own pane.
        general = general[:general.index("private var activityPane")]
        self.assertIn("state.connectionState.display", general)
        self.assertNotIn("state.serverRunning ? \"Running\"", general)
        self.assertIn('Text("All caught up")', general)
        self.assertIn("generalIssues", general)
        self.assertNotIn('GroupBox("Tool Activity")', general)
        self.assertNotIn('GroupBox("Agent Notifications")', general)
        activity = self.view[self.view.index("private var activityPane"):self.view.index("private var endpointDisplay")]
        self.assertIn('GroupBox("Tool Activity")', activity)
        self.assertIn('GroupBox("Agent Notifications")', activity)
        self.assertIn("case .activity: activityPane", self.view)
        # One update row; versions and updater progress sit behind a Details disclosure.
        update = self.view[self.view.index("private var updateCard"):self.view.index("private var updateSummaryTitle")]
        self.assertIn("DisclosureGroup(isExpanded: $showUpdateDetails)", update)
        self.assertIn("state.checkForUpdates()", update)
        self.assertIn("state.performUpdate()", update)

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
        self.assertIn('window.contentMinSize = NSSize(width: 820, height: 560)', self.window)
        self.assertIn('window.contentMaxSize = NSSize(width: 1240, height: 860)', self.window)
        self.assertIn('setFrameAutosaveName("MacMCPSettingsWindow")', self.window)

    def test_settings_can_open_on_a_requested_pane(self) -> None:
        self.assertIn("func show(state: AppState, settings: SettingsStore, section: SettingsSection? = nil)", self.window)
        self.assertIn("SettingsNavigation.shared.requestedSection = section", self.window)
        self.assertIn("initialSection: section ?? .general", self.window)
        self.assertIn("_selection = MacMCPState(initialValue: initialSection)", self.view)
        self.assertIn(".onChange(of: navigation.requestedSection)", self.view)

    def test_sidebar_and_search_are_accessible(self) -> None:
        self.assertIn(".accessibilityAddTraits(selection == item ? .isSelected : [])", self.view)
        self.assertIn('.accessibilityLabel("Search Settings")', self.view)
        self.assertIn(".focused($searchFocused)", self.view)
        self.assertIn('.keyboardShortcut("f", modifiers: .command)', self.view)
        heatmap = self.view[self.view.index("private func usageHeatmap"):self.view.index("private func usageHeatCells")]
        self.assertIn(".accessibilityRepresentation", heatmap)
        self.assertIn("usageMonthSummaries(usage)", heatmap)


if __name__ == "__main__":
    unittest.main()
