from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server.computer_plan import ComputerPlanError, execute_computer_plan
from mcp_server.foreground_guard import (
    foreground_authorization,
    foreground_authorized,
)
from mcp_server.security import load_settings
from mcp_server.tools_browser import (
    _chrome_execute_js_via_url_bridge,
    _execute_js_for_target,
    browser_activate_tab,
    browser_coordinate_click,
    browser_open_url,
    browser_press_key,
    browser_upload_artifact,
)
from mcp_server.tools_browser_agent import browser_act


class ForegroundCapabilityGuardTests(unittest.TestCase):
    def assert_foreground_denied(self, exc: HTTPException, operation: str) -> None:
        self.assertEqual(403, exc.status_code)
        detail = exc.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual("FOREGROUND_NOT_AUTHORIZED", detail.get("reason_code"))
        self.assertEqual("foreground_not_authorized", detail.get("error"))
        self.assertEqual(operation, detail.get("operation"))
        self.assertTrue(detail.get("foreground_required"))

    # ASSURANCE: SEC-FOCUS-001
    def test_activate_tab_is_user_visible_and_denied_even_when_allow_foreground_false(self) -> None:
        with patch("mcp_server.tools_browser._run_osascript") as osascript, \
             patch("mcp_server.tools_browser.browser_tabs.list_tabs") as list_tabs:
            with self.assertRaises(HTTPException) as raised:
                browser_activate_tab(None, browser="Safari", window_index=1, tab_index=2, allow_foreground=False)
        self.assert_foreground_denied(raised.exception, "browser_activate_tab")
        osascript.assert_not_called()
        list_tabs.assert_not_called()

    # ASSURANCE: SEC-FOCUS-001
    def test_agent_cannot_self_authorize_activate_tab_with_true_flag(self) -> None:
        with patch("mcp_server.tools_browser._run_osascript") as osascript:
            with self.assertRaises(HTTPException) as raised:
                browser_activate_tab(None, browser="Google Chrome", window_index=1, tab_index=1, allow_foreground=True)
        self.assert_foreground_denied(raised.exception, "browser_activate_tab")
        osascript.assert_not_called()

    # ASSURANCE: SEC-FOCUS-001
    def test_foreground_open_flags_are_denied_before_browser_script(self) -> None:
        for kwargs in ({"background": False}, {"activate": True}):
            with self.subTest(kwargs=kwargs), \
                 patch("mcp_server.tools_browser.validate_url"), \
                 patch("mcp_server.tools_browser._run_osascript") as osascript:
                with self.assertRaises(HTTPException) as raised:
                    browser_open_url(load_settings(), "Safari", "https://example.test", **kwargs)
                self.assert_foreground_denied(raised.exception, "browser_open_url")
                osascript.assert_not_called()

    def test_model_set_allow_foreground_cannot_send_native_key_or_coordinate_click(self) -> None:
        with patch("mcp_server.tools_browser._run_osascript") as osascript:
            with self.assertRaises(HTTPException) as key_raised:
                browser_press_key(None, browser="Safari", key="return", allow_foreground=True)
            with self.assertRaises(HTTPException) as click_raised:
                browser_coordinate_click(None, browser="Safari", x=50, y=50, allow_foreground=True)
        self.assert_foreground_denied(key_raised.exception, "browser_press_key")
        self.assert_foreground_denied(click_raised.exception, "browser_coordinate_click")
        osascript.assert_not_called()

    def test_false_native_fallback_stays_non_mutating_and_does_not_suggest_self_escalation(self) -> None:
        key = browser_press_key(None, browser="Safari", key="return", allow_foreground=False)
        click = browser_coordinate_click(None, browser="Safari", x=1, y=1, allow_foreground=False)
        self.assertFalse(key["ok"]); self.assertFalse(click["ok"])
        self.assertEqual("FOREGROUND_REQUIRED", key["reason_code"])
        self.assertEqual("FOREGROUND_REQUIRED", click["reason_code"])
        self.assertNotIn("retry with allow_foreground=true", key["reason"].lower())
        self.assertNotIn("retry with allow_foreground=true", click["reason"].lower())

    # ASSURANCE: SEC-FOCUS-001
    def test_chrome_url_js_bridge_fails_closed_before_any_applescript(self) -> None:
        target = type("Target", (), {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_chrome_test", "native_id": "123",
            "title": "Example", "url": "https://example.test",
        })()
        with patch("mcp_server.tools_browser._run_osascript") as osascript:
            with self.assertRaises(HTTPException) as raised:
                _chrome_execute_js_via_url_bridge("(function(){return 'OK';})()", target, 5)
        self.assert_foreground_denied(raised.exception, "chrome_url_js_bridge")
        osascript.assert_not_called()

    # ASSURANCE: SEC-FOCUS-001
    def test_chrome_direct_js_denial_cannot_escalate_into_url_bridge(self) -> None:
        target = type("Target", (), {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_chrome_test", "native_id": "123",
            "title": "Example", "url": "https://example.test",
        })()
        direct_denied = HTTPException(500, "Access not allowed. (-1723)")
        with patch("mcp_server.tools_browser.chrome_background_bridge.is_connected", return_value=False), \
             patch("mcp_server.tools_browser._CHROME_NATIVE_JS_DENIED", False), \
             patch("mcp_server.tools_browser._run_osascript", side_effect=direct_denied) as osascript:
            with self.assertRaises(HTTPException) as raised:
                _execute_js_for_target(
                    "Google Chrome", "(function(){return 'OK';})()", target, 5,
                )
        self.assert_foreground_denied(raised.exception, "chrome_url_js_bridge")
        self.assertEqual(1, osascript.call_count)

    # ASSURANCE: SEC-FOCUS-001
    def test_cached_chrome_native_js_denial_still_cannot_enter_url_bridge(self) -> None:
        target = type("Target", (), {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_chrome_test", "native_id": "123",
            "title": "Example", "url": "https://example.test",
        })()
        with patch("mcp_server.tools_browser.chrome_background_bridge.is_connected", return_value=False), \
             patch("mcp_server.tools_browser._CHROME_NATIVE_JS_DENIED", True), \
             patch("mcp_server.tools_browser._run_osascript") as osascript:
            with self.assertRaises(HTTPException) as raised:
                _execute_js_for_target(
                    "Google Chrome", "(function(){return 'OK';})()", target, 5,
                )
        self.assert_foreground_denied(raised.exception, "chrome_url_js_bridge")
        osascript.assert_not_called()

    # ASSURANCE: SEC-FOCUS-001
    def test_safari_upload_is_denied_before_artifact_or_tab_side_effect(self) -> None:
        with patch("mcp_server.tools_browser.resolve_artifact") as resolve, \
             patch("mcp_server.tools_browser._tab_lease") as lease, \
             patch("mcp_server.tools_browser._run_osascript") as osascript:
            with self.assertRaises(HTTPException) as raised:
                browser_upload_artifact(
                    load_settings(), "Safari", "#file", "artifact_fake", "/tmp/fake.pdf",
                    tab_handle="btab_fake",
                )
        self.assert_foreground_denied(raised.exception, "browser_upload_artifact")
        resolve.assert_not_called(); lease.assert_not_called(); osascript.assert_not_called()

    def test_chrome_background_upload_path_does_not_require_foreground_capability(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.pdf"
            path.write_bytes(b"abcd")
            artifact = {
                "artifact_id": "artifact_test",
                "path": str(path),
                "filename": "upload.pdf",
                "size": 4,
            }
            target = type("Target", (), {
                "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
                "tab_handle": "btab_test", "native_id": "441", "title": "Upload", "url": "https://example.test",
            })()
            bridge = {"ok": True, "metadata": '{"count":1,"name":"upload.pdf","size":4,"lastModified":1}'}
            with patch("mcp_server.tools_browser.resolve_artifact", return_value=artifact), \
                 patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)), \
                 patch("mcp_server.tools_browser.chrome_background_bridge.request_set_file_input", return_value=bridge) as send:
                result = browser_upload_artifact(
                    load_settings(), "Google Chrome", "#file", "artifact_test", str(path), tab_handle="btab_test"
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["focus_preserved"])
            send.assert_called_once()

    # ASSURANCE: SEC-FOCUS-001
    def test_internal_grant_allows_exact_foreground_operation_and_does_not_leak(self) -> None:
        target = type("Target", (), {
            "browser": "Safari", "window_index": 1, "tab_index": 2,
            "tab_handle": "btab_live", "native_id": "3002", "title": "Second", "url": "https://example.test",
            "lease_generation": 1,
        })()
        scripts: list[str] = []
        with foreground_authorization("dashboard_show_tab"):
            self.assertTrue(foreground_authorized())
            with patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)), \
                 patch("mcp_server.tools_browser._run_osascript", side_effect=lambda script, timeout_s=30: scripts.append(script) or ""):
                result = browser_activate_tab(
                    None, browser="Safari", tab_handle="btab_live", allow_foreground=True,
                )
        self.assertFalse(foreground_authorized())
        self.assertTrue(result["foreground_forced"])
        self.assertIn("activate", scripts[0])
        self.assertIn("set current tab to targetTab", scripts[0])

    def test_foreground_grant_propagates_to_to_thread_only_inside_lexical_scope(self) -> None:
        async def run() -> tuple[bool, bool, bool]:
            before = await asyncio.to_thread(foreground_authorized)
            with foreground_authorization("dashboard_show_tab"):
                inside = await asyncio.to_thread(foreground_authorized)
            after = await asyncio.to_thread(foreground_authorized)
            return before, inside, after
        self.assertEqual((False, True, False), asyncio.run(run()))

    # ASSURANCE: SEC-FOCUS-001
    def test_computer_plan_rejects_user_visible_tab_activation_tool(self) -> None:
        async def caller(tool: str, args: dict):
            raise AssertionError("nested tool must not run")
        with self.assertRaises(ComputerPlanError) as raised:
            asyncio.run(execute_computer_plan(
                caller, plan_version=2,
                steps=[{"id":"activate","tool":"browser_activate_tab","arguments":{
                    "browser":"Safari","tab_handle":"btab_test","allow_foreground":True
                }}],
            ))
        self.assertIn("not allowed in computer_plan", str(raised.exception))

    # ASSURANCE: SEC-FOCUS-001
    def test_browser_act_key_cannot_escalate_via_allow_foreground_flag(self) -> None:
        target = type("Target", (), {
            "browser": "Safari", "window_index": 1, "tab_index": 1,
            "tab_handle": "btab_test", "native_id": "3001", "title": "Fixture", "url": "https://example.test",
            "lease_generation": 1,
        })()
        focus_ok = {"ok": True, "actions": [{"ok": True, "type": "focus"}]}
        with patch("mcp_server.tools_browser_agent._ensure_visual_companion"), \
             patch("mcp_server.tools_browser_agent._tab_lease", return_value=nullcontext(target)), \
             patch("mcp_server.tools_browser_agent._resolve_tab_target", return_value=(1, 1)), \
             patch("mcp_server.tools_browser_agent._run_json_js", return_value=focus_ok), \
             patch("mcp_server.tools_browser_agent.browser_press_key", wraps=__import__("mcp_server.tools_browser", fromlist=["browser_press_key"]).browser_press_key):
            with self.assertRaises(HTTPException) as raised:
                browser_act(
                    load_settings(), "Safari",
                    actions=[{"type": "key", "key": "return", "element_id": "e1"}],
                    tab_handle="btab_test", return_state="none", allow_foreground=True,
                )
        self.assert_foreground_denied(raised.exception, "browser_press_key")


if __name__ == "__main__":
    unittest.main()
