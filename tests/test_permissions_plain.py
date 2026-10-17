"""#246: Settings › Permissions & Safety answers in plain words first (what AI apps can do, when Mac
MCP asks first) and keeps the policy terms under Details."""
from __future__ import annotations

import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


class PlainPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        source = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.pane = source[source.index("private var permissionsPane"):source.index("private func choiceRow")]
        self.display = (SOURCES / "StatusDisplay.swift").read_text(encoding="utf-8")

    def test_plain_summaries_come_before_the_details(self) -> None:
        details = self.pane.index('DisclosureGroup("Details", isExpanded: $permissionDetailsShown)')
        for heading in ('GroupBox("What AI apps can do")', 'GroupBox("When Mac MCP asks you first")'):
            self.assertLess(self.pane.index(heading), details)
        for term in ("Allowed Capabilities", "Destructive families", "Agent access ceiling", "Headless behavior"):
            self.assertGreater(self.pane.index(term), details, term)  # only inside Details

    def test_warnings_stay_visible_outside_details(self) -> None:
        details = self.pane.index('DisclosureGroup("Details"')
        self.assertLess(self.pane.index("High-risk calls fail closed until repaired"), details)
        self.assertLess(self.pane.index("is not a global server preset"), details)

    def test_every_known_profile_and_approval_level_has_a_plain_summary(self) -> None:
        for name in ("trusted", "standard", "read_only", "off", "critical", "high_risk"):
            self.assertIn(f'case "{name}":', self.display)
        self.assertIn("SafetyDisplay.profileSummary(item.name) ?? item.approvalBehavior.summary", self.pane)
        self.assertIn("SafetyDisplay.approvalSummary(item.name) ?? item.summary", self.pane)


if __name__ == "__main__":
    unittest.main()
