"""#188: the app can tell whether the Chrome companion is connected, which one, and whether it needs a reload."""
from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import chrome_background_bridge as bridge_module
from mcp_server.chrome_background_bridge import EXPECTED_COMPANION_FEATURES, ChromeBackgroundBridge

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class CompanionStatusTests(unittest.TestCase):
    def test_disconnected_companion_reports_nothing_but_that(self) -> None:
        status = ChromeBackgroundBridge().status()
        self.assertEqual({"connected": False, "version": None, "connected_at": None, "last_seen_at": None,
                          "missing_features": [], "needs_reload": False}, status)

    def test_connected_companion_reports_its_version_and_activity(self) -> None:
        bridge = ChromeBackgroundBridge()
        with patch.object(bridge_module.time, "time", return_value=1000.0):
            connection = bridge._attach(object(), asyncio.new_event_loop(), list(EXPECTED_COMPANION_FEATURES), "2.2.0")
        with patch.object(bridge_module.time, "time", return_value=1020.0):
            bridge.touch(connection)
        status = bridge.status()
        self.assertEqual((True, "2.2.0", 1000.0, 1020.0, [], False),
                         (status["connected"], status["version"], status["connected_at"], status["last_seen_at"],
                          status["missing_features"], status["needs_reload"]))
        bridge._detach(connection)
        self.assertFalse(bridge.status()["connected"])

    def test_an_older_companion_needs_a_reload_even_on_the_same_version(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), asyncio.new_event_loop(), ["dialogs", "gestures"], "2.2.0")
        status = bridge.status()
        self.assertTrue(status["needs_reload"])
        self.assertIn("frames", status["missing_features"])

    def test_untrusted_version_text_is_bounded(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(object(), asyncio.new_event_loop(), [], "<script>" + "9" * 200)
        self.assertLessEqual(len(bridge.status()["version"]), 20)
        bridge._attach(object(), asyncio.new_event_loop(), [], {"not": "text"})
        self.assertIsNone(bridge.status()["version"])

    def test_expected_features_match_the_companion_shipped_in_this_source(self) -> None:
        background = (ROOT / "menu_app" / "ChromeVisualCompanion" / "background.js").read_text(encoding="utf-8")
        shipped = re.search(r"const FEATURES = \[(.*?)\];", background).group(1)
        self.assertEqual(sorted(re.findall(r"'([a-z_]+)'", shipped)), sorted(EXPECTED_COMPANION_FEATURES))
        self.assertIn("version: chrome.runtime.getManifest().version", background)


class RuntimeEndpointTests(unittest.TestCase):
    def test_runtime_diagnostics_carry_the_companion_status(self) -> None:
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "t.sqlite3", usage_enabled=False, async_writes=False)
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), dashboard_token=DASHBOARD_TOKEN))
            with TestClient(app) as client:
                body = client.get("/dashboard/api/diagnostics/runtime",
                                  headers={"authorization": f"Bearer {DASHBOARD_TOKEN}"}).json()
                denied = client.get("/dashboard/api/diagnostics/runtime")
        self.assertIn("chrome_companion_connected", body)  # kept for older menu apps
        self.assertEqual(body["chrome_companion_connected"], body["chrome_companion"]["connected"])
        self.assertIn("needs_reload", body["chrome_companion"])
        self.assertEqual(401, denied.status_code)



class NativeCompanionSurfaceTests(unittest.TestCase):
    def test_settings_shows_the_live_chrome_state_and_setup_watches_for_it(self) -> None:
        sources = ROOT / "menu_app" / "Sources"
        settings = (sources / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn("let chrome = state.chromeCompanion", settings)
        self.assertIn('Button("Reload in Chrome…")', settings)
        self.assertIn("await state.refreshChromeCompanion()", settings)  # polled while the pane is open
        state = (sources / "AppState.swift").read_text(encoding="utf-8")
        self.assertIn('NSPasteboard.general.setString(extensionSourceURL.path, forType: .string)', state)
        self.assertIn("watchChromeCompanion()", state)
        self.assertIn('payload["browsers"] = browserCompanionStates', state)
        bridge = (sources / "OnboardingBridge.swift").read_text(encoding="utf-8")
        self.assertIn('static let browsers: Set<String> = ["safari", "chrome"]', bridge)


if __name__ == "__main__":
    unittest.main()
