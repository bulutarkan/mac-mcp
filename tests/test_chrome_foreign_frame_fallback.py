from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser
from mcp_server.browser_tabs import TabTarget


def _target():
    return TabTarget(browser="Google Chrome", window_index=1, tab_index=1, native_id="42", tab_handle="btab_chrome_x",
                     title="Sign in", url="https://login.example.com")


FOREIGN = HTTPException(502, {"ok": False, "error": "chrome_debugger_evaluate_failed",
                              "message": "Cannot access a chrome-extension:// URL of different extension"})


class ForeignExtensionFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(tools_browser, "_CHROME_NATIVE_JS_DENIED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sign_in_page_with_a_password_manager_frame_uses_apple_events(self) -> None:
        with patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", side_effect=FOREIGN), \
                patch.object(tools_browser, "_tab_identity_guard", return_value="set targetTab to tab 1"), \
                patch.object(tools_browser, "_run_osascript", return_value="eyJvayI6dHJ1ZX0=") as osa:
            out = tools_browser._execute_js_for_target("Google Chrome", "1+1", _target(), 10)
        self.assertEqual("eyJvayI6dHJ1ZX0=", out)
        self.assertIn("execute javascript", osa.call_args.args[0])

    def test_other_companion_errors_still_raise(self) -> None:
        other = HTTPException(502, {"error": "chrome_debugger_evaluate_failed", "message": "Target closed"})
        with patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", side_effect=other), \
                patch.object(tools_browser, "_run_osascript") as osa:
            with self.assertRaises(HTTPException):
                tools_browser._execute_js_for_target("Google Chrome", "1+1", _target(), 10)
        osa.assert_not_called()

    def test_no_fallback_when_apple_events_javascript_is_off(self) -> None:
        with patch.object(tools_browser, "_CHROME_NATIVE_JS_DENIED", True), \
                patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", side_effect=FOREIGN):
            with self.assertRaises(HTTPException):
                tools_browser._execute_js_for_target("Google Chrome", "1+1", _target(), 10)


if __name__ == "__main__":
    unittest.main()
