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
        self.assertIn("execute targetTab javascript", osa.call_args.args[0])

    def test_debugger_dropped_mid_call_is_retried_once(self) -> None:
        dropped = HTTPException(502, {"error": "chrome_debugger_evaluate_failed",
                                      "message": "Debugger is not attached to the tab with id: 42."})
        with patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js",
                             side_effect=[dropped, "b2s="]) as send:
            self.assertEqual("b2s=", tools_browser._execute_js_for_target("Google Chrome", "1", _target(), 10))
        self.assertEqual(2, send.call_count)

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
                patch.object(tools_browser, "_probe_chrome_apple_events_js", return_value="denied"), \
                patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", side_effect=FOREIGN):
            with self.assertRaises(HTTPException) as ctx:
                tools_browser._execute_js_for_target("Google Chrome", "1+1", _target(), 10)
        self.assertEqual("chrome_foreign_extension_frame", ctx.exception.detail["error"])

    def test_a_stale_denied_flag_is_rechecked_before_giving_up(self) -> None:
        def probe(force=False):
            tools_browser._CHROME_NATIVE_JS_DENIED = False
            return "allowed"

        with patch.object(tools_browser, "_CHROME_NATIVE_JS_DENIED", True), \
                patch.object(tools_browser, "_probe_chrome_apple_events_js", side_effect=probe), \
                patch.object(tools_browser.chrome_background_bridge, "is_connected", return_value=True), \
                patch.object(tools_browser.chrome_background_bridge, "request_execute_js", side_effect=FOREIGN), \
                patch.object(tools_browser, "_tab_identity_guard", return_value="set targetTab to tab 1"), \
                patch.object(tools_browser, "_run_osascript", return_value="Mg==") as osa:
            self.assertEqual("Mg==", tools_browser._execute_js_for_target("Google Chrome", "1+1", _target(), 10))
        self.assertIn("execute targetTab javascript", osa.call_args.args[0])

    def test_probe_runs_in_a_web_tab_never_a_chrome_page(self) -> None:
        with patch.object(tools_browser, "_chrome_is_running", return_value=True), \
                patch.object(tools_browser, "_run_osascript", return_value="2") as osa:
            self.assertEqual("allowed", tools_browser._probe_chrome_apple_events_js(force=True))
        script = osa.call_args.args[0]
        self.assertIn('starts with "https://"', script)
        self.assertNotIn("active tab of front window", script)


if __name__ == "__main__":
    unittest.main()
