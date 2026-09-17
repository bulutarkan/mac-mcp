from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.policy import evaluate_tool_scope
from mcp_server.policy_scope import ResourceScope
from mcp_server.tools_browser import browser_open_url, _require_stable_handle_for_mutation


class BrowserTargetHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()

    def test_safari_pid_change_never_resurrects_closed_handle_by_url(self) -> None:
        old = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "1001", "title": "Same", "url": "https://example.test/same",
        }]
        replacement = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "9009", "title": "Same", "url": "https://example.test/same",
        }]
        with patch("mcp_server.browser_tabs._scan", side_effect=[old, replacement, replacement]):
            old_handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            new_handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            self.assertNotEqual(old_handle, new_handle)
            with self.assertRaises(KeyError):
                browser_tabs.resolve_tab("Safari", old_handle)

    def test_existing_navigation_targets_handle_not_active_tab(self) -> None:
        before = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "2001", "title": "GitHub", "url": "https://github.com/example/repo"},
            {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "2002", "title": "Search", "url": "https://example.test/search"},
        ]
        after = [dict(before[0]), {
            "browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
            "native_id": "2002", "title": "Results", "url": "https://example.test/results",
        }]
        with patch("mcp_server.browser_tabs._scan", side_effect=[before, before, before, after]), \
             patch("mcp_server.tools_browser.validate_url"), \
             patch("mcp_server.tools_browser._claim_tab_visual", return_value=False), \
             patch("mcp_server.tools_browser._run_osascript", return_value="2|2002") as osa:
            target_handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            result = browser_open_url(
                None, "Safari", "https://example.test/results",
                new_tab=False, background=True, tab_handle=target_handle,
            )
        script = osa.call_args.args[0]
        self.assertIn("set targetTab to tab 2", script)
        self.assertIn('actualNativeId is not "2002"', script)
        self.assertNotIn("set URL of current tab", script)
        self.assertEqual(target_handle, result["tab_handle"])
        self.assertEqual("https://github.com/example/repo", after[0]["url"])

    def test_ambiguous_mutation_requires_stable_handle(self) -> None:
        rows = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1},
            {"browser": "Safari", "window_index": 1, "tab_index": 2},
        ]
        with patch("mcp_server.tools_browser.browser_tabs.list_tabs", return_value=rows), \
             browser_tabs.logical_owner_scope("session:test", profile="trusted"):
            with self.assertRaises(HTTPException) as ctx:
                _require_stable_handle_for_mutation("Safari", None, 1, "browser_act")
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("stable_tab_handle_required", ctx.exception.detail["error"])

    def test_session_owner_prevents_cross_conversation_tab_steal(self) -> None:
        rows = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "7001", "title": "Work", "url": "https://example.test/work",
        }]
        with patch("mcp_server.browser_tabs._scan", return_value=rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            with browser_tabs.logical_owner_scope("session:one", profile="trusted"):
                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True):
                    pass
            with browser_tabs.logical_owner_scope("session:two", profile="trusted"):
                with self.assertRaises(HTTPException) as ctx:
                    with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True):
                        pass
        self.assertEqual("tab_owned_by_other_agent", ctx.exception.detail["error"])


    def test_logical_owner_window_affinity_follows_owned_tab(self) -> None:
        rows = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "8101", "title": "Other", "url": "https://example.test/other"},
            {"browser": "Safari", "window_index": 2, "tab_index": 1, "active": True,
             "native_id": "8201", "title": "Owned", "url": "https://example.test/owned"},
        ]
        with patch("mcp_server.browser_tabs._scan", return_value=rows):
            owned_handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            with browser_tabs.logical_owner_scope("session:owner", profile="trusted"):
                with browser_tabs.tab_lease("Safari", tab_handle=owned_handle, allow_rebind=True):
                    pass
                self.assertEqual(2, browser_tabs.preferred_window_for_owner("Safari"))

    def test_new_safari_tab_uses_logical_owner_window_not_global_window_one(self) -> None:
        rows = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "9101", "title": "Other", "url": "https://example.test/other"},
            {"browser": "Safari", "window_index": 2, "tab_index": 1, "active": True,
             "native_id": "9201", "title": "Owned", "url": "https://example.test/owned"},
        ]
        created = {
            "browser": "Safari", "window_index": 2, "tab_index": 2, "active": False,
            "native_id": "9202", "title": "New", "url": "https://example.test/new",
            "tab_handle": "btab_safari_new",
        }
        with patch("mcp_server.browser_tabs._scan", return_value=rows),              patch("mcp_server.tools_browser.validate_url"),              patch("mcp_server.tools_browser.browser_tabs.find_created", return_value=created),              patch("mcp_server.tools_browser.browser_tabs.claim_created_tab", return_value={"generation": 2}),              patch("mcp_server.tools_browser._claim_tab_visual", return_value=False),              patch("mcp_server.tools_browser._run_osascript", return_value="2") as osa:
            owned_handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            with browser_tabs.logical_owner_scope("session:owner", profile="trusted"):
                with browser_tabs.tab_lease("Safari", tab_handle=owned_handle, allow_rebind=True):
                    pass
                result = browser_open_url(
                    None, "Safari", "https://example.test/new",
                    new_tab=True, background=True,
                )
        script = osa.call_args.args[0]
        self.assertIn("tell window 2", script)
        self.assertNotIn("tell window 1\n                set newTab", script)
        self.assertEqual(2, result["window_index"])

    def test_stale_handle_error_forbids_active_tab_fallback(self) -> None:
        rows = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "9301", "title": "Active", "url": "https://example.test/active",
        }]
        with patch("mcp_server.tools_browser.validate_url"),              patch("mcp_server.browser_tabs._scan", return_value=rows),              patch("mcp_server.tools_browser._run_osascript") as osa,              browser_tabs.logical_owner_scope("session:owner", profile="trusted"):
            with self.assertRaises(HTTPException) as ctx:
                browser_open_url(
                    None, "Safari", "https://example.test/new",
                    new_tab=False, background=True, tab_handle="btab_safari_gone",
                )
        self.assertEqual(404, ctx.exception.status_code)
        self.assertEqual("stale_tab_handle", ctx.exception.detail["error"])
        self.assertTrue(ctx.exception.detail["do_not_fallback_to_active_tab"])
        self.assertEqual("browser_list_tabs", ctx.exception.detail["required_action"])
        osa.assert_not_called()

    def test_browser_app_scope_blocks_unexpected_chrome_switch(self) -> None:
        scope = ResourceScope(browser_apps=("Safari",), tool_families=("browser",), access_mode="read_only")
        denied = evaluate_tool_scope(scope, "browser_open_url", {
            "browser": "Google Chrome", "url": "https://example.com",
        })
        allowed = evaluate_tool_scope(scope, "browser_open_url", {
            "browser": "Safari", "url": "https://example.com",
        })
        self.assertFalse(denied.allowed)
        self.assertIn("browser_app_not_allowed", denied.reasons)
        self.assertTrue(allowed.allowed)


if __name__ == "__main__":
    unittest.main()
