from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.chrome_background_bridge import ChromeBackgroundBridge, _resolve_chrome_companion_port, ensure_chrome_companion_config, ensure_chrome_companion_token
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.security import load_settings
from mcp_server.tools_browser import browser_open_url


ROOT = Path(__file__).resolve().parents[1]


class ChromeBackgroundTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests exercise Chrome transport semantics, not bootstrap allowlist policy.
        # Public browser access is therefore made explicit under secure-bootstrap defaults.
        self._browser_allowlist = patch.dict(os.environ, {"BROWSER_ALLOWLIST": "*"}, clear=False)
        self._browser_allowlist.start()
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()

    def tearDown(self) -> None:
        self._browser_allowlist.stop()

    def test_safari_extension_sources_are_untouched_by_chrome_transport(self) -> None:
        safari_manifest = json.loads((ROOT / "menu_app/BrowserVisualCompanion/manifest.json").read_text())
        self.assertNotIn("background", safari_manifest)
        self.assertNotIn("permissions", safari_manifest)
        self.assertEqual(["visual.js"], safari_manifest["content_scripts"][0]["js"])
        self.assertEqual(
            (ROOT / "menu_app/BrowserVisualCompanion/visual.js").read_bytes(),
            (ROOT / "menu_app/ChromeVisualCompanion/visual.js").read_bytes(),
        )

    def test_chrome_extension_has_service_worker_with_debugger_but_no_tabs_or_native_messaging_permission(self) -> None:
        manifest = json.loads((ROOT / "menu_app/ChromeVisualCompanion/manifest.json").read_text())
        self.assertEqual("background.js", manifest["background"]["service_worker"])
        permissions = set(manifest.get("permissions", [])) | set(manifest.get("optional_permissions", []))
        self.assertIn("debugger", permissions)
        self.assertTrue({"tabs", "nativeMessaging", "cookies", "webRequest"}.isdisjoint(permissions))
        worker = (ROOT / "menu_app/ChromeVisualCompanion/background.js").read_text()
        self.assertIn("chrome.tabs.create", worker)
        self.assertIn("active: false", worker)
        self.assertNotIn("chrome.tabs.update", worker)
        self.assertIn("mac_mcp_bridge_wake", worker)
        self.assertIn("dispatch_mouse", worker)
        self.assertIn("Input.dispatchMouseEvent", worker)
        self.assertIn("mousePressed", worker)
        self.assertIn("mouseReleased", worker)
        wake = (ROOT / "menu_app/ChromeVisualCompanion/bridge_wake.js").read_text()
        self.assertIn("chrome.runtime.sendMessage", wake)
        self.assertIn("mac_mcp_bridge_wake", wake)
        self.assertIn("bridge_wake.js", manifest["content_scripts"][0]["js"])

    # ASSURANCE: SEC-FOCUS-001
    def test_dispatch_mouse_rpc_is_explicit_and_bounded(self) -> None:
        bridge = ChromeBackgroundBridge()
        with patch.object(bridge, "_request", return_value={"ok": True, "dispatched": True}) as request:
            result = bridge.request_dispatch_mouse("123", 45.5, 66.25, click_count=2, timeout_s=7.0)
        self.assertTrue(result["dispatched"])
        request.assert_called_once_with(
            "dispatch_mouse",
            {"chrome_tab_id": 123, "x": 45.5, "y": 66.25, "click_count": 2},
            timeout_s=7.0,
        )

    def test_bridge_port_prefers_env_then_running_uvicorn_argv(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PORT": "18766"}, clear=False), \
             patch("sys.argv", ["uvicorn", "mcp_server.main:app", "--port", "19999"]):
            self.assertEqual(18766, _resolve_chrome_companion_port())

        with patch.dict(os.environ, {}, clear=False), \
             patch("mcp_server.chrome_background_bridge.os.getenv", side_effect=lambda key, default=None: "" if key == "MAC_MCP_PORT" else os.environ.get(key, default)), \
             patch("sys.argv", ["uvicorn", "mcp_server.main:app", "--host", "0.0.0.0", "--port", "18767"]):
            self.assertEqual(18767, _resolve_chrome_companion_port())

        with patch.dict(os.environ, {}, clear=False), \
             patch("mcp_server.chrome_background_bridge.os.getenv", side_effect=lambda key, default=None: "" if key == "MAC_MCP_PORT" else os.environ.get(key, default)), \
             patch("sys.argv", ["uvicorn", "mcp_server.main:app", "--port=18768"]):
            self.assertEqual(18768, _resolve_chrome_companion_port())

    def test_bridge_port_preserves_existing_config_without_runtime_port_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-chrome-port-preserve-") as tmp:
            config = Path(tmp) / "bridge_config.js"
            config.write_text('globalThis.MAC_MCP_CHROME_BRIDGE = {"port":18769,"token":"redacted"};\n')
            with patch("mcp_server.chrome_background_bridge.os.getenv", side_effect=lambda key, default=None: "" if key == "MAC_MCP_PORT" else os.environ.get(key, default)), \
                 patch("sys.argv", ["helper-script"]):
                self.assertEqual(18769, _resolve_chrome_companion_port(config))

            with patch.dict(os.environ, {"MAC_MCP_PORT": "18770"}, clear=False), \
                 patch("sys.argv", ["helper-script"]):
                self.assertEqual(18770, _resolve_chrome_companion_port(config))

    def test_bridge_config_uses_dedicated_owner_only_token_and_no_url_secret(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            extension = root / "menu_app" / "ChromeVisualCompanion"
            extension.mkdir(parents=True)
            token_file = root / "state" / "chrome-token"
            with patch.dict(os.environ, {
                "MAC_MCP_CHROME_BRIDGE_TOKEN_FILE": str(token_file),
                "MAC_MCP_PORT": "18766",
            }, clear=False):
                token = ensure_chrome_companion_token()
                config = ensure_chrome_companion_config(root)
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(0o600, stat.S_IMODE(token_file.stat().st_mode))
            self.assertIsNotNone(config)
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            text = config.read_text(encoding="utf-8")
            self.assertIn('"port":18766', text)
            self.assertIn(token, text)

        worker = (ROOT / "menu_app/ChromeVisualCompanion/background.js").read_text(encoding="utf-8")
        self.assertIn("/chrome-background-bridge`", worker)
        self.assertNotIn("?token=", worker)
        self.assertIn("type: 'hello', token: TOKEN", worker)
        self.assertIn("getLastFocused({windowTypes: ['normal']})", worker)
        self.assertIn("active: false", worker)

    def test_background_open_uses_extension_and_claims_normal_stable_handle(self) -> None:
        created = {
            "browser": "Google Chrome", "window_index": 2, "tab_index": 4,
            "active": False, "native_id": "444", "title": "Example",
            "url": "https://example.com/", "tab_handle": "btab_chrome_test",
        }
        with patch("mcp_server.tools_browser._chrome_is_running", return_value=True), \
             patch("mcp_server.tools_browser._open_chrome_background_tab_via_extension",
                   return_value=(created, {"chrome_tab_id": 444})) as transport, \
             patch("mcp_server.tools_browser.browser_tabs.claim_created_tab",
                   return_value={"generation": 7}) as claim, \
             patch("mcp_server.tools_browser._claim_tab_visual", return_value=True), \
             patch("mcp_server.tools_browser._run_osascript") as applescript:
            result = browser_open_url(
                load_settings(), "Google Chrome", "https://example.com/",
                new_tab=True, background=True,
            )
        transport.assert_called_once_with("https://example.com/")
        claim.assert_called_once_with("Google Chrome", "btab_chrome_test")
        applescript.assert_not_called()
        self.assertEqual("btab_chrome_test", result["tab_handle"])
        self.assertEqual(7, result["lease_generation"])
        self.assertEqual("chrome_extension", result["background_transport"])
        self.assertFalse(result["foreground_forced"])

    def test_cold_background_open_launches_first_url_and_claims_stable_handle(self) -> None:
        created = {
            "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
            "active": True, "native_id": "333", "title": "Example",
            "url": "https://example.com/", "tab_handle": "btab_chrome_cold",
        }
        with patch("mcp_server.tools_browser._chrome_is_running", return_value=False), \
             patch("mcp_server.tools_browser._open_chrome_cold_background",
                   return_value=(created, {"chrome_tab_id": "333", "companion_connected": True})) as cold, \
             patch("mcp_server.tools_browser._open_chrome_background_tab_via_extension") as extension, \
             patch("mcp_server.tools_browser.browser_tabs.claim_created_tab",
                   return_value={"generation": 3}) as claim, \
             patch("mcp_server.tools_browser._claim_tab_visual", return_value=True), \
             patch("mcp_server.tools_browser._run_osascript") as applescript:
            result = browser_open_url(
                load_settings(), "Google Chrome", "https://example.com/",
                new_tab=True, background=True,
            )
        cold.assert_called_once_with("https://example.com/")
        extension.assert_not_called()
        applescript.assert_not_called()
        claim.assert_called_once_with("Google Chrome", "btab_chrome_cold")
        self.assertEqual("chrome_cold_launch", result["background_transport"])
        self.assertEqual("btab_chrome_cold", result["tab_handle"])
        self.assertEqual(3, result["lease_generation"])
        self.assertTrue(result["companion_connected"])
        self.assertFalse(result["foreground_forced"])

    def test_cold_launch_command_uses_background_open_and_argv_only_profile_override(self) -> None:
        from mcp_server.tools_browser import _chrome_cold_launch_command
        with patch.dict(os.environ, {"MAC_MCP_CHROME_USER_DATA_DIR": "/tmp/Profile With Spaces"}, clear=False):
            command = _chrome_cold_launch_command("https://example.com/?q=a&b=1")
        self.assertEqual("/usr/bin/open", command[0])
        self.assertIn("-g", command)
        self.assertIn("-n", command)
        self.assertIn("--args", command)
        self.assertIn("--use-mock-keychain", command)
        self.assertIn(f"--user-data-dir={Path('/tmp/Profile With Spaces').resolve()}", command)
        self.assertEqual("https://example.com/?q=a&b=1", command[-1])
        self.assertNotIn("sh", command[:1])
        with patch.dict(os.environ, {
            "MAC_MCP_CHROME_USER_DATA_DIR": "/tmp/Profile",
            "MAC_MCP_CHROME_REMOTE_DEBUGGING_PORT": "18774",
        }, clear=False):
            debug_command = _chrome_cold_launch_command("https://example.com/")
        self.assertIn("--remote-debugging-port=18774", debug_command)
        self.assertIn("--enable-unsafe-extension-debugging", debug_command)

    def test_background_transport_failure_is_fail_closed_without_applescript_fallback(self) -> None:
        failure = HTTPException(409, {
            "ok": False, "error": "chrome_background_transport_unavailable", "retryable": True,
        })
        with patch("mcp_server.tools_browser._chrome_is_running", return_value=True), \
             patch("mcp_server.tools_browser._open_chrome_background_tab_via_extension", side_effect=failure), \
             patch("mcp_server.tools_browser._run_osascript") as applescript:
            with self.assertRaises(HTTPException) as raised:
                browser_open_url(
                    load_settings(), "Google Chrome", "https://example.com/",
                    new_tab=True, background=True,
                )
        applescript.assert_not_called()
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("chrome_background_transport_unavailable", raised.exception.detail["error"])


class ChromeTabLeaseParityTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()
        self.rows = [{
            "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
            "active": True, "native_id": "9901", "title": "Account",
            "url": "https://example.com/account",
        }]

    def _context(self, agent_id: str):
        return set_policy_context(PolicyContext(
            profile="browser_only", actor=f"agent:{agent_id}", agent_id=agent_id,
        ))

    def test_two_callers_cannot_use_same_chrome_tab_simultaneously(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        failures: list[HTTPException] = []

        with patch("mcp_server.browser_tabs._scan", return_value=self.rows):
            handle = browser_tabs.list_tabs("Google Chrome")[0]["tab_handle"]

            def holder() -> None:
                with browser_tabs.tab_lease("Google Chrome", tab_handle=handle, allow_rebind=True):
                    entered.set()
                    release.wait(2)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                with browser_tabs.tab_lease("Google Chrome", tab_handle=handle, allow_rebind=True):
                    pass
            except HTTPException as exc:
                failures.append(exc)
            finally:
                release.set()
                thread.join(2)

        self.assertEqual(1, len(failures))
        self.assertEqual(409, failures[0].status_code)
        self.assertEqual("tab_busy", failures[0].detail["error"])

    def test_created_chrome_tab_owned_by_one_agent_cannot_be_stolen_by_another(self) -> None:
        with patch("mcp_server.browser_tabs._scan", return_value=self.rows):
            handle = browser_tabs.list_tabs("Google Chrome")[0]["tab_handle"]
            token = self._context("agt_a")
            try:
                lease = browser_tabs.claim_created_tab("Google Chrome", handle)
                self.assertEqual("agent:agt_a", lease["owner"])
            finally:
                reset_policy_context(token)

            token = self._context("agt_b")
            try:
                with self.assertRaises(HTTPException) as raised:
                    with browser_tabs.tab_lease(
                        "Google Chrome", tab_handle=handle, allow_rebind=True,
                    ):
                        pass
                self.assertEqual(409, raised.exception.status_code)
                self.assertEqual("tab_owned_by_other_agent", raised.exception.detail["error"])
            finally:
                reset_policy_context(token)


if __name__ == "__main__":
    unittest.main()
