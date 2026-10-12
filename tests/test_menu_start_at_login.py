import unittest

from tests._menu_sources import feature_source

ROOT_SOURCES = __import__("pathlib").Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


class MenuStartAtLoginTests(unittest.TestCase):
    def test_settings_store_reads_and_writes_start_at_login(self) -> None:
        store = (ROOT_SOURCES / "SettingsStore.swift").read_text(encoding="utf-8")
        self.assertIn("var start_at_login: Bool?", store)
        self.assertIn("@Published var startAtLogin = true", store)
        self.assertIn("startAtLogin = current.server.start_at_login ?? true", store)
        self.assertIn("start_at_login: startAtLogin", store)

    def test_toggle_applies_through_the_cli(self) -> None:
        state = feature_source("AppState")
        body = state[state.index("func setStartAtLogin(_ enabled: Bool)"):]
        body = body[:body.index("\n    }\n") + 6]
        self.assertIn('["autostart", enabled ? "on" : "off", "--json"]', body)
        self.assertIn("settings.startAtLogin = enabled", body)
        view = (ROOT_SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        general = view[view.index("private var generalPane"):view.index('GroupBox("Issues")')]
        self.assertIn('GroupBox("Startup")', general)
        self.assertIn("startAtLoginBinding", general)
        self.assertIn("Start Mac MCP when you log in", general)


if __name__ == "__main__":
    unittest.main()
