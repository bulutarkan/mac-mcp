"""A sleeping Chrome companion is woken through an open tab instead of failing or switching transport."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import chrome_background_bridge as bridge_module
from mcp_server import tools_browser
from mcp_server.chrome_background_bridge import ChromeBackgroundBridge


class CompanionWakeTests(unittest.TestCase):
    def test_wake_waits_for_the_worker_to_connect(self) -> None:
        bridge = ChromeBackgroundBridge()

        def waker() -> bool:
            # The woken worker connects a moment later.
            bridge._loop, bridge._websocket = object(), object()
            return True

        bridge.set_waker(waker)
        self.assertTrue(bridge.wake(timeout_s=1.0))

    def test_an_unanswered_wake_is_not_retried_for_a_minute(self) -> None:
        bridge = ChromeBackgroundBridge()
        calls = []
        bridge.set_waker(lambda: calls.append(1) or True)
        self.assertFalse(bridge.wake(timeout_s=0.2))
        self.assertFalse(bridge.wake(timeout_s=0.2))
        self.assertEqual(1, len(calls))
        bridge._wake_failed_at -= bridge_module._WAKE_RETRY_S + 1
        self.assertFalse(bridge.wake(timeout_s=0.0))
        self.assertEqual(2, len(calls))

    def test_no_waker_and_a_failing_waker_give_up_at_once(self) -> None:
        self.assertFalse(ChromeBackgroundBridge().wake(timeout_s=5.0))
        bridge = ChromeBackgroundBridge()
        bridge.set_waker(lambda: (_ for _ in ()).throw(RuntimeError("no Apple Events")))
        self.assertFalse(bridge.wake(timeout_s=5.0))

    def test_a_request_wakes_first_and_still_refuses_clearly_when_nothing_connects(self) -> None:
        bridge = ChromeBackgroundBridge()
        woke = []
        bridge.set_waker(lambda: woke.append(1) or False)
        with self.assertRaises(HTTPException) as ctx:
            bridge._request("execute_js", {}, timeout_s=1.0)
        self.assertEqual("chrome_background_transport_unavailable", ctx.exception.detail["error"])
        self.assertEqual([1], woke)

    def test_the_waker_fires_the_event_only_when_allowed(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_CHROME_WAKE": "off"}), \
                patch.object(tools_browser, "_run_osascript", side_effect=AssertionError("sent")):
            self.assertFalse(tools_browser._wake_chrome_companion())
        with patch.dict(os.environ, {"MAC_MCP_CHROME_WAKE": "on"}), \
                patch.object(tools_browser, "_CHROME_NATIVE_JS_DENIED", False), \
                patch.object(tools_browser, "_run_osascript", return_value="1") as script:
            self.assertTrue(tools_browser._wake_chrome_companion())
        self.assertIn("macmcp-bridge-wake", script.call_args.args[0])
        self.assertIn('is not running then return "0"', script.call_args.args[0])

    def test_companion_keeps_its_worker_alive_and_listens_for_the_wake_event(self) -> None:
        root = os.path.join(os.path.dirname(__file__), "..", "menu_app", "ChromeVisualCompanion")
        with open(os.path.join(root, "background.js"), encoding="utf-8") as handle:
            background = handle.read()
        with open(os.path.join(root, "bridge_wake.js"), encoding="utf-8") as handle:
            wake = handle.read()
        self.assertIn("chrome.runtime.getPlatformInfo", background)
        self.assertIn("window.addEventListener('macmcp-bridge-wake', macMcpWakeBridge);", wake)


if __name__ == "__main__":
    unittest.main()
