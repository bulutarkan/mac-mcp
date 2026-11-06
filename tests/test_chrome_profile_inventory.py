"""Multi-profile Chrome inventory, synthetic-window guard and fail-closed exposure."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs, tools_browser


def _profile_tab(tab_id, wid, idx, name):
    return {
        "native_id": str(tab_id),
        "chrome_window_id": wid,
        "chrome_index": idx,
        "active": idx == 0,
        "title": name,
        "url": "https://example.test/" + name.lower(),
    }


def _applescript_row(native, window_idx, tab_idx, name):
    return {
        "browser": "Google Chrome", "window_index": window_idx,
        "tab_index": tab_idx, "active": True,
        "native_id": str(native), "title": name,
        "url": "https://example.test/" + name.lower(),
    }


class ChromeProfileInventoryTests(unittest.TestCase):
    def test_chrome_handle_is_scoped_to_companion_owner(self):
        a = browser_tabs._chrome_handle("101", "profile-connection-A")
        b = browser_tabs._chrome_handle("101", "profile-connection-B")
        self.assertNotEqual(a, b)
        self.assertEqual(a, browser_tabs._chrome_handle("101", "profile-connection-A"))
        self.assertTrue(a.startswith("btab_chrome_"))

    def test_ambiguous_legacy_id_refused_before_native_mutation(self):
        inventory = [_profile_tab(101, 1001, 0, "A")]
        legacy = [_applescript_row(101, 1, 1, "A"),
                  _applescript_row(101, 2, 1, "A")]
        with self.assertRaises(ValueError):
            browser_tabs._merge_chrome_inventory(inventory, legacy)

    def test_merge_preserves_owner_namespace(self):
        item = _profile_tab(101, 1001, 0, "A")
        item["owner_id"] = "verified-connection"
        result = browser_tabs._merge_chrome_inventory([item], [])
        self.assertEqual("verified-connection", result[0]["owner_id"])
        handle = browser_tabs._chrome_handle(
            result[0]["native_id"], result[0]["owner_id"])
        self.assertNotEqual(handle, browser_tabs._chrome_handle("101"))

    def test_merge_preserves_matching_native_window_identity(self):
        entries = [_profile_tab(101, 1001, 0, "Findspo"), _profile_tab(202, 2002, 0, "Leanspots")]
        legacy = [_applescript_row(101, 2, 5, "Findspo")]
        result = browser_tabs._merge_chrome_inventory(entries, legacy)
        self.assertEqual(2, len(result))
        rows = {row["native_id"]: row for row in result}
        self.assertEqual(2, rows["101"]["window_index"])
        self.assertEqual(5, rows["101"]["tab_index"])
        self.assertFalse(rows["101"]["companion_only"])
        self.assertEqual(3, rows["202"]["window_index"])
        self.assertTrue(rows["202"]["companion_only"])
        self.assertEqual(2002, rows["202"]["chrome_window_id"])

    def test_legacy_unowned_profile_is_not_mixed_into_authorized_set(self):
        result = browser_tabs._merge_chrome_inventory(
            [_profile_tab(101, 1001, 0, "Findspo")],
            [_applescript_row(101, 1, 1, "Findspo"), _applescript_row(303, 4, 1, "Other")],
        )
        self.assertEqual(["101"], [row["native_id"] for row in result])

    def test_apple_script_unsafe_profile_native_action_is_refused(self):
        tab = browser_tabs._merge_chrome_inventory(
            [_profile_tab(202, 2002, 0, "Leanspots")], []
        )[0]
        tab["tab_handle"] = "btab_chrome_example"
        target = browser_tabs._target_from_row(tab)
        self.assertTrue(target.companion_only)
        with self.assertRaises(HTTPException) as raised:
            tools_browser._tab_identity_guard(target)
        self.assertEqual("chrome_profile_requires_companion", raised.exception.detail["error"])

    def test_two_apple_script_windows_matching_same_profile_rejected(self):
        entries = [_profile_tab(101, 1001, 0, "Findspo"),
                   _profile_tab(102, 1001, 1, "OtherFindspoTab")]
        legacy = [_applescript_row(101, 1, 1, "Findspo"),
                  _applescript_row(102, 2, 1, "OtherFindspoTab")]
        with self.assertRaises(ValueError):
            browser_tabs._merge_chrome_inventory(entries, legacy)

    def test_list_tabs_discloses_partial_coverage_when_companion_is_stale(self):
        with patch.object(tools_browser.browser_tabs, "list_tabs", return_value=[]), \
             patch.object(tools_browser.chrome_background_bridge, "inventory_supported", return_value=False), \
             patch.object(tools_browser, "chrome_transport_capabilities", return_value={}):
            answer = tools_browser.browser_list_tabs(None, "Google Chrome")
        self.assertTrue(answer["inventory"]["partial"])
        self.assertEqual("legacy_applescript_only", answer["inventory"]["coverage"])

    def test_list_tabs_identifies_full_connected_profile_coverage(self):
        with patch.object(tools_browser.browser_tabs, "list_tabs", return_value=[]), \
             patch.object(tools_browser.chrome_background_bridge, "inventory_supported", return_value=True), \
             patch.object(tools_browser, "chrome_transport_capabilities", return_value={}):
            answer = tools_browser.browser_list_tabs(None, "Google Chrome")
        self.assertFalse(answer["inventory"]["partial"])
        self.assertEqual("connected_companion_profiles", answer["inventory"]["coverage"])


if __name__ == "__main__":
    unittest.main()
