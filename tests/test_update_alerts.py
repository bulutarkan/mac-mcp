"""#232: every install (install.sh, npm, .pkg) learns about a new release the same way: a background
check, one calm "A new update is ready" alert per release and the same Update button. Only what runs
behind it differs: the signed updater, or the newer package's release page."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._menu_sources import feature_source

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


class UpdateAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = feature_source("AppState")
        self.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        self.settings_view = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.notifications = (SOURCES / "AgentNotificationController.swift").read_text(encoding="utf-8")
        self.store = (SOURCES / "SettingsStore.swift").read_text(encoding="utf-8")

    def section(self, text: str, start: str, end: str) -> str:
        return text[text.index(start):text.index(end, text.index(start))]

    def test_one_readiness_for_every_install(self) -> None:
        ready = self.section(self.state, "var updateReady: Bool", "func performUpdate()")
        self.assertIn("packageUpdate.updateAvailable == true", ready)
        self.assertIn("info.updateAvailable && !info.dirty", ready)
        perform = self.section(self.state, "func performUpdate()", "func showUpdate()")
        self.assertIn("NSWorkspace.shared.open(url)", perform)
        self.assertIn("installUpdate()", perform)

    def test_the_background_watch_only_looks_and_respects_the_setting(self) -> None:
        self.assertIn("startUpdateWatch()", self.state[self.state.index("init(startBackgroundTasks"):])
        quiet = self.section(self.state, "private func checkForUpdatesQuietly()", "private func announceUpdateIfNew()")
        self.assertIn("guard settings.updateAlertsEnabled", quiet)
        self.assertIn('["update", "--check", "--json"]', quiet)
        self.assertIn("shouldShowNotice: false", quiet)
        self.assertNotIn("installUpdate", quiet)
        self.assertIn("updateWatchIntervalSeconds: Double = 6 * 60 * 60", self.state)

    def test_each_release_is_announced_once_without_a_version(self) -> None:
        announce = self.section(self.state, "private func announceUpdateIfNew()", "func setUpdateAlertsEnabled")
        self.assertIn("announcedUpdateKey", announce)
        self.assertIn("scheduleUpdateReady()", announce)
        alert = self.section(self.notifications, "func scheduleUpdateReady()", "func removeUpdateReady()")
        self.assertIn('content.title = "A new update is ready"', alert)
        self.assertNotIn("version", alert.lower())
        self.assertIn("updateReadyIdentifier", alert)
        self.assertIn('userInfo["kind"] as? String == "update"', self.notifications)

    def test_the_menu_and_settings_say_the_same_words(self) -> None:
        self.assertIn("if state.updateReady { updateCallout.transition(AppMotion.arrive) }", self.menu)
        callout = self.section(self.menu, "private var updateCallout", "private var headerSubtitle")
        self.assertIn('Text("A new update is ready")', callout)
        self.assertIn("state.performUpdate()", callout)
        self.assertIn('if state.updateReady { return "A new update is ready" }', self.settings_view)
        self.assertIn('Text("Let me know when an update is ready")', self.settings_view)

    def test_the_setting_defaults_to_on(self) -> None:
        self.assertIn("var updates: Bool?", self.store)
        self.assertIn("updateAlertsEnabled = current.notifications?.updates ?? true", self.store)
        self.assertIn("updates: updateAlertsEnabled", self.store)


if __name__ == "__main__":
    unittest.main()
