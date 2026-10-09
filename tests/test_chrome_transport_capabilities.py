from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser as tb


class ChromeTransportCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._denied = tb._CHROME_NATIVE_JS_DENIED
        tb._CHROME_JS_PROBE.update({"at": 0.0, "state": "unknown"})
        tb._CHROME_NATIVE_JS_DENIED = False

    def tearDown(self) -> None:
        tb._CHROME_NATIVE_JS_DENIED = self._denied
        tb._CHROME_JS_PROBE.update({"at": 0.0, "state": "unknown"})

    def _caps(self, *, connected=False, running=True, osa=None):
        with patch.object(tb.chrome_background_bridge, "is_connected", return_value=connected), \
             patch.object(tb, "_chrome_is_running", return_value=running), \
             patch.object(tb, "_run_osascript", side_effect=osa or (lambda *_a, **_k: "2")) as run:
            return tb.chrome_transport_capabilities(), run

    def test_companion_connected_needs_no_probe(self) -> None:
        caps, run = self._caps(connected=True)
        self.assertEqual("companion", caps["active_transport"])
        self.assertTrue(caps["background_dom_automation"])
        self.assertTrue(caps["trusted_background_pointer"])
        self.assertEqual("not_needed", caps["apple_events_javascript"])
        self.assertEqual([], caps["remediation"])
        run.assert_not_called()

    def test_disconnected_with_apple_events_enabled_works_in_background(self) -> None:
        caps, run = self._caps()
        self.assertEqual("apple_events_javascript", caps["active_transport"])
        self.assertTrue(caps["background_dom_automation"])
        self.assertFalse(caps["trusted_background_pointer"])
        self.assertIn("javascript", run.call_args.args[0])
        self.assertNotIn("activate", run.call_args.args[0])

    def test_disconnected_and_disabled_reports_foreground_only_with_remediation(self) -> None:
        def denied(*_a, **_k):
            raise HTTPException(500, "Executing JavaScript through AppleScript is turned off.")
        caps, _run = self._caps(osa=denied)
        self.assertEqual("url_bridge_foreground_only", caps["active_transport"])
        self.assertFalse(caps["background_dom_automation"])
        self.assertTrue(caps["focus_change_requires_authorization"])
        self.assertTrue(any("Allow JavaScript from Apple Events" in item for item in caps["remediation"]))
        self.assertTrue(tb._CHROME_NATIVE_JS_DENIED)

    def test_enabling_the_setting_later_clears_the_denied_flag(self) -> None:
        tb._CHROME_NATIVE_JS_DENIED = True
        caps, _run = self._caps()
        self.assertEqual("apple_events_javascript", caps["active_transport"])
        self.assertFalse(tb._CHROME_NATIVE_JS_DENIED)

    def test_chrome_not_running_is_never_launched_by_the_probe(self) -> None:
        caps, run = self._caps(running=False)
        self.assertEqual("unknown", caps["active_transport"])
        self.assertFalse(caps["chrome_running"])
        run.assert_not_called()

    def test_probe_is_cached_between_tab_listings(self) -> None:
        with patch.object(tb.chrome_background_bridge, "is_connected", return_value=False), \
             patch.object(tb, "_chrome_is_running", return_value=True), \
             patch.object(tb, "_run_osascript", return_value="2") as run:
            tb.chrome_transport_capabilities()
            tb.chrome_transport_capabilities()
        self.assertEqual(1, run.call_count)

    def test_list_tabs_carries_transport_for_chrome_only(self) -> None:
        with patch.object(tb.browser_tabs, "list_tabs", return_value=[]), \
             patch.object(tb, "chrome_transport_capabilities", return_value={"active_transport": "companion"}):
            self.assertEqual("companion", tb.browser_list_tabs(None, "Google Chrome")["transport"]["active_transport"])
            self.assertNotIn("transport", tb.browser_list_tabs(None, "Safari"))


if __name__ == "__main__":
    unittest.main()
