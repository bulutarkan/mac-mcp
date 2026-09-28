from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import patch

from mcp_server.foreground_guard import foreground_authorization
from mcp_server.tools_browser import browser_press_key
from mcp_server.tools_browser_agent import browser_act


def _target(
    *, browser: str = "Safari", active: bool = True, window_index: int = 1,
    tab_index: int = 2, generation: int = 7,
):
    slug = "chrome" if browser == "Google Chrome" else "safari"
    return type(
        "Target",
        (),
        {
            "browser": browser,
            "window_index": window_index,
            "tab_index": tab_index,
            "tab_handle": f"btab_{slug}_target",
            "native_id": "3002",
            "title": "Target",
            "url": "https://example.test/target",
            "active": active,
            "lease_generation": generation,
        },
    )()


class BrowserKeyTargetingTests(unittest.TestCase):
    def test_native_key_requires_stable_tab_handle(self) -> None:
        with foreground_authorization("test"), patch("mcp_server.tools_browser._run_osascript") as osa:
            result = browser_press_key(
                None, browser="Safari", key="return", allow_foreground=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("TAB_TARGET_REQUIRED", result["reason_code"])
        osa.assert_not_called()

    def test_inactive_target_fails_without_key_or_focus_change(self) -> None:
        target = _target(active=False)
        with foreground_authorization("test"),              patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)),              patch("mcp_server.tools_browser._run_osascript") as osa:
            result = browser_press_key(
                None, browser="Safari", key="return",
                tab_handle=target.tab_handle, lease_generation=7,
                allow_foreground=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("TAB_TARGET_NOT_ACTIVE", result["reason_code"])
        osa.assert_not_called()

    def test_non_front_window_target_fails_without_key_or_focus_change(self) -> None:
        target = _target(active=True, window_index=2)
        with foreground_authorization("test"),              patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)),              patch("mcp_server.tools_browser._run_osascript") as osa:
            result = browser_press_key(
                None, browser="Safari", key="return",
                tab_handle=target.tab_handle, lease_generation=7,
                allow_foreground=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("TAB_TARGET_NOT_ACTIVE", result["reason_code"])
        osa.assert_not_called()

    def test_stale_lease_generation_fails_before_key(self) -> None:
        target = _target(active=True, generation=8)
        with foreground_authorization("test"),              patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)),              patch("mcp_server.tools_browser._run_osascript") as osa:
            result = browser_press_key(
                None, browser="Safari", key="return",
                tab_handle=target.tab_handle, lease_generation=7,
                allow_foreground=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("STALE_TAB_LEASE", result["reason_code"])
        self.assertEqual(7, result["expected_lease_generation"])
        self.assertEqual(8, result["actual_lease_generation"])
        osa.assert_not_called()

    def test_active_exact_target_uses_identity_and_active_guards(self) -> None:
        target = _target(active=True)
        with foreground_authorization("test"),              patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)),              patch("mcp_server.tools_browser._run_osascript", return_value="") as osa:
            result = browser_press_key(
                None, browser="Safari", key="return",
                tab_handle=target.tab_handle, lease_generation=7,
                allow_foreground=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(target.tab_handle, result["tab_handle"])
        self.assertEqual(7, result["lease_generation"])
        script = osa.call_args.args[0]
        self.assertIn("MAC_MCP_TAB_TARGET_NOT_ACTIVE", script)
        self.assertIn("set actualNativeId", script)
        self.assertIn('tell window 1', script)
        self.assertIn("key code 36", script)

    def test_chrome_active_exact_target_uses_active_tab_index_guard(self) -> None:
        target = _target(browser="Google Chrome", active=True, tab_index=3)
        with foreground_authorization("test"), \
             patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)), \
             patch("mcp_server.tools_browser._run_osascript", return_value="") as osa:
            result = browser_press_key(
                None, browser="Google Chrome", key="tab",
                tab_handle=target.tab_handle, lease_generation=7,
                allow_foreground=True,
            )
        self.assertTrue(result["ok"])
        script = osa.call_args.args[0]
        self.assertIn("if active tab index is not 3", script)
        self.assertIn("MAC_MCP_TAB_TARGET_NOT_ACTIVE", script)
        self.assertIn("key code 48", script)

    def test_browser_act_propagates_pinned_handle_and_generation_to_native_key(self) -> None:
        target = _target(active=True, tab_index=1, generation=11)
        progress = {"ok": True, "url": target.url, "title": target.title, "dom_revision": 3}
        with patch("mcp_server.tools_browser_agent._ensure_visual_companion"),              patch("mcp_server.tools_browser_agent._tab_lease", return_value=nullcontext(target)),              patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 1)),              patch("mcp_server.tools_browser_agent._run_json_js", return_value=progress),              patch(
                 "mcp_server.tools_browser_agent.browser_press_key",
                 return_value={
                     "ok": True, "tab_handle": target.tab_handle,
                     "lease_generation": target.lease_generation,
                 },
             ) as press:
            result = browser_act(
                None, "Safari", actions=[{"type": "key", "key": "return"}],
                tab_handle=target.tab_handle, return_state="none",
                allow_foreground=True,
            )
        self.assertTrue(result["ok"])
        kwargs = press.call_args.kwargs
        self.assertEqual(target.tab_handle, kwargs["tab_handle"])
        self.assertEqual(11, kwargs["lease_generation"])


if __name__ == "__main__":
    unittest.main()
