from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.policy import evaluate_tool_scope
from mcp_server.policy_scope import ResourceScope
from mcp_server.tools_browser import browser_close_tab


class BrowserMultiCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()

    @staticmethod
    def _rows():
        return [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "101", "title": "Keep", "url": "https://example.com/keep"},
            {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "102", "title": "Close A", "url": "https://example.com/a"},
            {"browser": "Safari", "window_index": 1, "tab_index": 3, "active": False,
             "native_id": "103", "title": "Close B", "url": "https://example.com/b"},
        ]

    def test_multiple_handles_close_in_order_and_return_titles(self) -> None:
        rows = self._rows()
        state = [dict(row) for row in rows]

        def scan(_browser):
            return [dict(row) for row in state]

        def run_script(script, timeout_s=30):
            # The identity guard includes the expected URL. Simulate the browser close
            # so the next stable-handle resolution observes shifted indices.
            if 'actualNativeId is not "102"' in script:
                state[:] = [row for row in state if row["native_id"] != "102"]
            elif 'actualNativeId is not "103"' in script:
                state[:] = [row for row in state if row["native_id"] != "103"]
            for index, row in enumerate(state, start=1):
                row["tab_index"] = index
            return ""

        with patch("mcp_server.browser_tabs._scan", side_effect=scan), \
             patch("mcp_server.tools_browser._run_osascript", side_effect=run_script):
            listed = browser_tabs.list_tabs("Safari")
            handles = [listed[1]["tab_handle"], listed[2]["tab_handle"]]
            result = browser_close_tab(None, "Safari", tab_handles=handles)

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["closed_count"])
        self.assertEqual(["Close A", "Close B"], [item["title"] for item in result["closed"]])
        self.assertEqual(["https://example.com/a", "https://example.com/b"], [item["url"] for item in result["closed"]])
        self.assertEqual(["Keep"], [row["title"] for row in state])

    def test_unknown_handle_prevalidation_prevents_partial_close(self) -> None:
        rows = self._rows()
        scripts = []
        with patch("mcp_server.browser_tabs._scan", return_value=rows), \
             patch("mcp_server.tools_browser._run_osascript", side_effect=lambda script, timeout_s=30: scripts.append(script) or ""):
            handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            with self.assertRaises(HTTPException) as ctx:
                browser_close_tab(None, "Safari", tab_handles=[handle, "btab_missing"])
        self.assertEqual(404, ctx.exception.status_code)
        self.assertEqual([], scripts)

    def test_duplicate_handles_only_close_once(self) -> None:
        rows = self._rows()[:2]
        state = [dict(row) for row in rows]
        scripts = []

        def scan(_browser):
            return [dict(row) for row in state]

        def run_script(script, timeout_s=30):
            scripts.append(script)
            state[:] = [row for row in state if row["url"] != "https://example.com/a"]
            return ""

        with patch("mcp_server.browser_tabs._scan", side_effect=scan), \
             patch("mcp_server.tools_browser._run_osascript", side_effect=run_script):
            handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            result = browser_close_tab(None, "Safari", tab_handles=[handle, handle])
        self.assertEqual(1, result["closed_count"])
        self.assertEqual(1, len(scripts))

    def test_legacy_single_index_behavior_still_works(self) -> None:
        rows = self._rows()
        with patch("mcp_server.browser_tabs._scan", return_value=rows), \
             patch("mcp_server.tools_browser._run_osascript", return_value=""):
            result = browser_close_tab(None, "Safari", window_index=1, tab_index=2)
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["tab_index"])
        self.assertEqual(1, result["closed_count"])
        self.assertEqual("Close A", result["closed"][0]["title"])

    def test_scoped_multi_close_checks_every_handle(self) -> None:
        scope = ResourceScope(browser_tabs=("tab-a", "tab-b"), tool_families=("browser",), access_mode="workspace_write")
        allowed = evaluate_tool_scope(scope, "browser_close_tab", {"tab_handles": ["tab-a", "tab-b"]})
        denied = evaluate_tool_scope(scope, "browser_close_tab", {"tab_handles": ["tab-a", "tab-c"]})
        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)


if __name__ == "__main__":
    unittest.main()
