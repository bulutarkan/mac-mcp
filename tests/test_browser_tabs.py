from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.tools_browser import browser_activate_tab, browser_coordinate_click, browser_press_key


class BrowserTabHandleTests(unittest.TestCase):
    def setUp(self):
        browser_tabs._REGISTRY.clear()

    def test_chrome_native_id_produces_stable_handle_after_index_shift(self):
        first = [
            {"browser": "Google Chrome", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "101", "title": "Prague", "url": "https://example.com/prague"},
            {"browser": "Google Chrome", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "202", "title": "Vienna", "url": "https://example.com/vienna"},
        ]
        shifted = [
            {"browser": "Google Chrome", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "999", "title": "Sheets", "url": "https://docs.google.com"},
            {**first[0], "tab_index": 2},
            {**first[1], "tab_index": 3},
        ]
        with patch("mcp_server.browser_tabs._scan", side_effect=[first, shifted]):
            handle = browser_tabs.list_tabs("Google Chrome")[1]["tab_handle"]
            wi, ti, row = browser_tabs.resolve_tab("Google Chrome", handle)
        self.assertEqual((1, 3), (wi, ti))
        self.assertEqual("202", row["native_id"])

    def test_safari_pid_preserves_handle_after_index_shift(self):
        first = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "3001", "title": "Prague", "url": "https://example.com/prague"},
            {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "3002", "title": "Budapest", "url": "https://example.com/budapest"},
        ]
        shifted = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "9000", "title": "Sheets", "url": "https://docs.google.com"},
            {**first[0], "tab_index": 2},
            {**first[1], "tab_index": 3},
        ]
        with patch("mcp_server.browser_tabs._scan", side_effect=[first, shifted]):
            handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            wi, ti, row = browser_tabs.resolve_tab("Safari", handle)
        self.assertEqual((1, 3), (wi, ti))
        self.assertEqual("3002", row["native_id"])

    def test_native_key_refuses_focus_by_default(self):
        result = browser_press_key(None, browser="Safari", key="return")
        self.assertFalse(result["ok"])
        self.assertTrue(result["foreground_required"])

    def test_coordinate_click_refuses_focus_by_default(self):
        result = browser_coordinate_click(None, browser="Safari", x=10, y=10)
        self.assertFalse(result["ok"])
        self.assertTrue(result["foreground_required"])

    def test_activate_tab_requires_explicit_user_foreground_capability(self):
        with patch("mcp_server.tools_browser._run_osascript") as osascript, \
             patch("mcp_server.browser_tabs._scan") as scan:
            with self.assertRaises(HTTPException) as raised:
                browser_activate_tab(None, browser="Safari", window_index=1, tab_index=2)
        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual("FOREGROUND_NOT_AUTHORIZED", raised.exception.detail["reason_code"])
        osascript.assert_not_called()
        scan.assert_not_called()



if __name__ == "__main__":
    unittest.main()
