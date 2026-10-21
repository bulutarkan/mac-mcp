"""Settings and menu say what they know (#17, #19, #20, #21).

- #17: the permission profile rows wait while a change is saved, and say so.
- #19: loading, an empty answer and a failed one are different screens: the app list, MCP Servers,
  Devices and General's "All caught up".
- #20: the menu's "n failed" count shows only the failed calls, with a way back.
- #21: every Voice picker and field has a name VoiceOver reads.
"""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
SETTINGS = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
SERVERS = (SOURCES / "McpServersPane.swift").read_text(encoding="utf-8")
MACS = (SOURCES / "MacsPane.swift").read_text(encoding="utf-8")
MENU = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")


def block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


class PermissionProfileProgressTests(unittest.TestCase):
    def test_rows_wait_while_a_change_is_saved(self) -> None:
        rows = block(SETTINGS, 'GroupBox("What AI apps can do")', 'GroupBox("When Mac MCP asks you first")')
        self.assertIn(".disabled(state.permissionProfileChanging)", rows)
        self.assertIn('Text("Saving…")', rows)


class LoadingEmptyErrorTests(unittest.TestCase):
    def test_the_app_list_tells_an_error_from_an_empty_list(self) -> None:
        self.assertNotIn("Start Mac MCP to see this list.", SETTINGS)
        loader = block(SETTINGS, "private func loadAppSupport() async {", "\n    }\n")
        self.assertIn("appSupportProblem =", loader)
        self.assertNotIn(": []", loader.split("if result.ok")[1].split("} else {")[1])
        listing = block(SETTINGS, 'GroupBox("What agents can do in each app")', ".task { await loadAppSupport() }")
        self.assertLess(listing.index("appSupportProblem"), listing.index("apps.isEmpty"))
        self.assertIn('Button("Try again")', listing)

    def test_mcp_servers_are_not_empty_before_they_load(self) -> None:
        self.assertIn('Text("Loading your servers…")', SERVERS)
        self.assertIn('Text(!loaded ? "Servers" : servers.isEmpty ? "No servers yet"', SERVERS)

    def test_devices_are_not_only_this_mac_before_they_load(self) -> None:
        summary = block(MACS, "private var macsSummary: String {", "\n    }\n")
        self.assertLess(summary.index("guard loaded"), summary.index('"Only this Mac yet"'))
        self.assertIn('"Checking…"', summary)

    def test_general_is_not_all_caught_up_while_it_checks(self) -> None:
        issues = block(SETTINGS, 'GroupBox("Issues")', "updateCard")
        self.assertLess(issues.index("state.connectionState == .connecting"), issues.index('Text("All caught up")'))
        self.assertIn('Text("Checking…")', issues)


class FailedFilterTests(unittest.TestCase):
    def test_the_failed_count_filters_the_list(self) -> None:
        card = block(MENU, "private var activityCard: some View {", ".disclosureGroupStyle(MenuDisclosureStyle())")
        self.assertIn("showFailedOnly.toggle()", card)
        self.assertIn("let shown = showFailedOnly ? failed : events", card)
        self.assertIn('Button("Show all") { showFailedOnly = false }', card)
        self.assertIn('"No failed calls now."', card)
        self.assertIn(".accessibilityLabel(showFailedOnly", card)


class VoiceLabelTests(unittest.TestCase):
    def test_every_voice_control_has_a_name(self) -> None:
        voice = block(SETTINGS, "Groq API key", ".onChange(of: settings.inputDevice)")
        for name in ('Picker("Input device"', 'Picker("Output device"', 'Picker("Language"',
                     '.accessibilityLabel("Voice name")', '.accessibilityLabel("Speech rate")',
                     '.accessibilityLabel("Groq API key")'):
            self.assertIn(name, voice)
        self.assertNotIn('Picker("",', voice)
        for label in ('Text("Input")', 'Text("Output")', 'Text("Language")', 'Text("Voice")', 'Text("Rate")'):
            self.assertIn(f"{label}.foregroundStyle(.secondary).accessibilityHidden(true)", voice)


if __name__ == "__main__":
    unittest.main()
