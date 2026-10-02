from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = (ROOT / "menu_app/Sources/AppState.swift").read_text(encoding="utf-8")
SETTINGS_VIEW = (ROOT / "menu_app/Sources/SettingsView.swift").read_text(encoding="utf-8")


class SettingsFreshnessValidationTests(unittest.TestCase):
    def test_shared_settings_data_state_covers_required_phases(self) -> None:
        self.assertIn("struct SettingsDataState: Equatable", APP_STATE)
        for phase in ("loading", "fresh", "stale", "unavailable", "error"):
            self.assertIn(f"case {phase}", APP_STATE)
        for state in (
            "providerSettingsState",
            "mobileSettingsState",
            "permissionsSettingsState",
            "browserSettingsState",
        ):
            self.assertIn(state, APP_STATE)

    def test_provider_refresh_preserves_last_known_good_and_surfaces_failure(self) -> None:
        start = APP_STATE.index("func refreshProviders() async")
        end = APP_STATE.index("func refreshMobileDevices() async", start)
        block = APP_STATE[start:end]
        self.assertIn("let previous = providerSettingsState", block)
        self.assertIn("Could not fully refresh providers", block)
        self.assertIn("settingsFailureState(from: previous", block)
        self.assertNotIn("Provider detection is supplemental Settings data", block)
        self.assertIn('case .error, .unavailable: return "Unavailable"', SETTINGS_VIEW)
        self.assertIn('case .loading: return "Checking…"', SETTINGS_VIEW)

    def test_mobile_query_failure_is_not_rendered_as_empty_success(self) -> None:
        start = APP_STATE.index("func refreshMobileDevices() async")
        end = APP_STATE.index("func createMobilePairing() async", start)
        block = APP_STATE[start:end]
        self.assertIn("Could not load connected devices", block)
        self.assertIn("mobileSettingsState", block)
        mobile = SETTINGS_VIEW[SETTINGS_VIEW.index("private var mobilePane"):SETTINGS_VIEW.index("private var advancedPane")]
        self.assertIn("switch state.mobileSettingsState.phase", mobile)
        self.assertIn("Connected devices could not be loaded. Use Retry above.", mobile)
        self.assertIn("No paired mobile devices.", mobile)
        self.assertIn("state.mobilePairingIssue", mobile)
        self.assertIn("state.mobileDeviceActionIssue", mobile)

    def test_permissions_and_browser_refresh_have_inline_state(self) -> None:
        self.assertIn("browserSettingsState = .fresh()", APP_STATE)
        self.assertIn("browserSettingsState = .error(message)", APP_STATE)
        self.assertIn("permissionsSettingsState, .fresh()", APP_STATE)
        self.assertIn("Could not refresh permission semantics", APP_STATE)
        self.assertIn("state.browserSettingsState", SETTINGS_VIEW)
        self.assertIn("state.permissionsSettingsState", SETTINGS_VIEW)
        self.assertIn("state.permissionActionIssue", SETTINGS_VIEW)

    def test_inline_validation_blocks_invalid_endpoint_and_runtime_saves(self) -> None:
        self.assertIn("private var publicURLValidationMessage", SETTINGS_VIEW)
        self.assertIn('components.scheme?.lowercased() == "https"', SETTINGS_VIEW)
        self.assertIn("components.user == nil, components.password == nil", SETTINGS_VIEW)
        self.assertIn("private var serverPortValidationMessage", SETTINGS_VIEW)
        self.assertIn("(1...65535).contains(settings.serverPort)", SETTINGS_VIEW)
        self.assertIn("private var cliPathValidationMessage", SETTINGS_VIEW)
        self.assertIn("FileManager.default.isExecutableFile", SETTINGS_VIEW)
        self.assertIn("private func persistEndpointSettings()", SETTINGS_VIEW)
        self.assertIn("private func persistRuntimeSettings()", SETTINGS_VIEW)
        self.assertIn("restart required", SETTINGS_VIEW)
        self.assertIn("applies live", SETTINGS_VIEW)

    def test_stale_state_shows_timestamp_and_retry(self) -> None:
        self.assertIn('"Last updated " + $0.formatted', SETTINGS_VIEW)
        self.assertIn('return ("Stale"', SETTINGS_VIEW)
        self.assertIn('Button("Retry", action: retry)', SETTINGS_VIEW)


if __name__ == "__main__":
    unittest.main()
