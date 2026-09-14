from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi import HTTPException

from mcp_server.tools_browser import (
    _safari_visual_claim_js,
    _safari_visual_claim_script,
    _visual_claim_event_js,
    _visual_claim_js,
    _visual_claim_script,
    _visual_companion_source,
    _execute_js_for_target,
    _chrome_execute_js_via_url_bridge,
    _claim_tab_visual,
    browser_open_url,
)
from mcp_server.tools_browser_agent import _batch_js, _browser_state_bootstrap, _observe_js, _ensure_visual_companion
from mcp_server.update_helper import _sync_runtime, _tracked_files

ROOT = Path(__file__).resolve().parents[1]


class BrowserVisualCompanionTests(unittest.TestCase):
    def test_visual_event_is_metadata_only_and_never_copies_typed_text(self) -> None:
        secret = "TOP_SECRET_SENTINEL_DO_NOT_LEAK"
        script = _batch_js([{"type": "type", "element_id": "e_1", "text": secret}], None)
        self.assertIn(secret, script)  # normal browser action still has to carry the value
        start = script.index("function __mcpVisual")
        end = script.index("function __mcpId", start)
        visual_helper = script[start:end]
        self.assertNotIn(secret, visual_helper)
        self.assertNotIn("selector", visual_helper)
        self.assertNotIn("location.href", visual_helper)
        self.assertNotIn("innerText", visual_helper)
        self.assertIn("action:String(action||'Working').slice(0,40)", visual_helper)
        self.assertIn("claim:true", visual_helper)

    def test_visual_history_sidebar_is_private_bounded_and_closed_by_default(self) -> None:
        source = (ROOT / "menu_app/BrowserVisualCompanion/visual.js").read_text(encoding="utf-8")
        self.assertIn("const MAX_HISTORY = 30", source)
        self.assertIn("sessionStorage", source)
        self.assertIn("history-rail", source)
        self.assertIn("history-panel", source)
        self.assertIn("This tab only · no page content stored", source)
        self.assertIn("companion.classList.toggle('sidebar-open')", source)
        self.assertNotIn('<div class="companion sidebar-open">', source)
        self.assertIn("SAFE_TARGETS", source)
        self.assertIn("SAFE_DETAILS", source)

    def test_visual_history_metadata_is_categorical_and_never_copies_typed_value(self) -> None:
        secret = "SIDEBAR_SECRET_MUST_NOT_LEAK"
        script = _batch_js([{"type": "type", "element_id": "e_1", "text": secret}], None)
        self.assertIn(secret, script)
        start = script.index("function __mcpVisualTarget")
        end = script.index("function __mcpId", start)
        helper = script[start:end]
        self.assertNotIn(secret, helper)
        self.assertNotIn("innerText", helper)
        self.assertNotIn("location.href", helper)
        self.assertIn("target_kind:__mcpVisualTarget(el)", helper)
        self.assertIn("['Up','Down','Into view']", helper)
        for label in ("Button", "Link", "Text field", "Menu", "Checkbox", "Option", "Tab", "Date", "Item"):
            self.assertIn(label, helper)

    def test_visual_companion_mounts_only_after_a_claimed_mcp_event(self) -> None:
        source = (ROOT / "menu_app/BrowserVisualCompanion/visual.js").read_text(encoding="utf-8")
        install_start = source.index("function install()")
        install_end = source.index("install();", install_start)
        install_body = source[install_start:install_end]
        self.assertNotIn("ensureUI();", install_body)
        self.assertIn("event.claim !== true", source)
        self.assertIn("const ui = ensureUI();", source)

    def test_safari_open_url_claim_is_metadata_only(self) -> None:
        expected = "https://example.com/path?q=private"
        js = _safari_visual_claim_js(expected)
        script = _safari_visual_claim_script(3, expected)
        self.assertIn("tab 3 of window 1", script)
        self.assertNotIn("eval(", script)
        claim_start = js.rfind("(()=>{try{const expected=")
        self.assertGreaterEqual(claim_start, 0)
        claim_js = js[claim_start:]
        self.assertIn("document.readyState==='loading'", claim_js)
        self.assertIn("location.href", claim_js)  # used only to ensure the final document is the claimed target
        self.assertIn("claim:true", claim_js)
        self.assertIn("action:'Opened'", claim_js)
        self.assertIn("target_kind:'Page'", claim_js)
        for forbidden in ("document.title", "innerText", "textContent"):
            self.assertNotIn(forbidden, claim_js)

    def test_chrome_execute_javascript_access_error_uses_url_bridge_fallback(self) -> None:
        class Target:
            browser = "Google Chrome"
            window_index = 1
            tab_index = 2
            native_id = "123"
            url = "https://example.com/"
            title = "Example"

        direct = HTTPException(500, "Access not allowed. (-1723)")
        with patch("mcp_server.tools_browser._run_osascript", side_effect=direct), \
             patch("mcp_server.tools_browser._chrome_execute_js_via_url_bridge", return_value="OK") as bridge:
            out = _execute_js_for_target("Google Chrome", "(function(){return 'OK';})()", Target(), 5)
        self.assertEqual("OK", out)
        bridge.assert_called_once()

    def test_chrome_bridge_unavailable_keeps_manual_secure_toggle_error(self) -> None:
        class Target:
            browser = "Google Chrome"
            window_index = 1
            tab_index = 2
            native_id = "123"
            url = "https://example.com/"
            title = "Example"

        direct = HTTPException(500, "Access not allowed. (-1723)")
        bridge_error = HTTPException(412, "bridge unavailable")
        with patch("mcp_server.tools_browser._run_osascript", side_effect=direct), \
             patch("mcp_server.tools_browser._chrome_execute_js_via_url_bridge", side_effect=bridge_error):
            with self.assertRaises(HTTPException) as raised:
                _execute_js_for_target("Google Chrome", "(function(){return 'x';})()", Target(), 5)
        self.assertEqual(412, raised.exception.status_code)
        self.assertIn("View → Developer → Allow JavaScript from Apple Events", str(raised.exception.detail))
        self.assertIn("real user input", str(raised.exception.detail))

    def test_chrome_url_bridge_is_bounded_private_and_navigation_tolerant(self) -> None:
        import inspect
        source = inspect.getsource(_chrome_execute_js_via_url_bridge)
        self.assertIn("javascript:", source)
        self.assertIn("btoa(unescape(encodeURIComponent", source)
        self.assertIn("chunk_size = 3000", source)
        self.assertIn("8_000_000", source)
        self.assertIn("for bridge_attempt in range(3)", source)
        self.assertIn("__macMcpBridgeOriginalTitle", source)
        self.assertIn("delete window.__macMcpBridgeResult", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("new Function", source)

    def test_chrome_url_bridge_retries_after_navigation_discards_first_stage(self) -> None:
        class Target:
            browser = "Google Chrome"
            window_index = 1
            tab_index = 2
            native_id = "123"
            url = "https://example.com/"
            title = "Example"

        class FixedUUID:
            hex = "abcdef1234567890"

        marker = "__MAC_MCP_BRIDGE_abcdef123456__"
        with patch("mcp_server.tools_browser.uuid.uuid4", return_value=FixedUUID()), \
             patch("mcp_server.tools_browser.time.sleep"), \
             patch("mcp_server.tools_browser._run_osascript", side_effect=[
                 marker + "TIMEOUT",
                 marker + "READY:4",
                 marker + "CHUNK:T0s=",
                 "",
             ]) as run:
            out = _chrome_execute_js_via_url_bridge("(function(){return 'OK';})()", Target(), 6)
        self.assertEqual("OK", out)
        self.assertEqual(4, run.call_count)

    def test_chrome_background_open_restores_previous_active_tab(self) -> None:
        import inspect
        source = inspect.getsource(browser_open_url)
        self.assertIn("set previousIndex to active tab index", source)
        self.assertIn("set active tab index to previousIndex", source)

    def test_chrome_open_url_claim_uses_same_private_shared_companion(self) -> None:
        expected = "https://www.google.com/travel/flights?q=private"
        js = _visual_claim_js(expected)
        script = _visual_claim_script("Google Chrome", 4, expected)
        self.assertIn('tell application "Google Chrome"', script)
        self.assertIn('execute javascript', script)
        self.assertIn('tab 4 of window 1', script)
        claim_start = js.rfind("(()=>{try{const expected=")
        self.assertGreaterEqual(claim_start, 0)
        claim_js = js[claim_start:]
        self.assertIn("claim:true", claim_js)
        self.assertIn("action:'Opened'", claim_js)
        self.assertIn("target_kind:'Page'", claim_js)
        for forbidden in ("document.title", "innerText", "textContent"):
            self.assertNotIn(forbidden, claim_js)
        event_js = _visual_claim_event_js(expected)
        self.assertNotIn("__macMcpVisualCompanionLoaded", event_js)
        import inspect
        claim_path = inspect.getsource(_claim_tab_visual)
        self.assertIn("_execute_js_for_target", claim_path)
        self.assertIn("_visual_companion_source()", claim_path)
        self.assertIn("_visual_claim_event_js(expected_url)", claim_path)

    def test_shared_webextension_is_browser_neutral_and_fallback_source_is_identical(self) -> None:
        source = (ROOT / "menu_app/BrowserVisualCompanion/visual.js").read_text(encoding="utf-8")
        self.assertEqual(source, _visual_companion_source())
        manifest = json.loads((ROOT / "menu_app/BrowserVisualCompanion/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("Mac MCP Visual Companion", manifest["name"])
        self.assertNotIn("Safari page", manifest["description"])
        self.assertNotIn("Chrome page", manifest["description"])

    def test_browser_tools_embed_visual_companion_when_safari_extension_is_unavailable(self) -> None:
        source = _visual_companion_source()
        self.assertIn("__macMcpVisualCompanionLoaded", source)
        self.assertIn("window.addEventListener('mac-mcp-visual'", source)
        bootstrap = _browser_state_bootstrap()
        self.assertNotIn(source, bootstrap)
        import inspect
        ensure_path = inspect.getsource(_ensure_visual_companion)
        self.assertIn("__macMcpVisualCompanionLoaded", ensure_path)
        self.assertIn("_visual_companion_source()", ensure_path)
        claim = _safari_visual_claim_js("https://example.com/")
        self.assertIn("__macMcpVisualCompanionLoaded", claim)
        self.assertIn("action:'Opened'", claim)

    def test_visual_attribute_does_not_advance_browser_dom_revision(self) -> None:
        bootstrap = _browser_state_bootstrap()
        self.assertIn("rec.attributeName==='data-mac-mcp-visual-event'", bootstrap)
        self.assertIn("continue;", bootstrap)
        observe = _observe_js("interactive", 20)
        self.assertIn("__mcpVisual('Inspecting'", observe)


    def test_visual_companion_has_no_idle_infinite_animation_or_hidden_blur(self) -> None:
        source = (ROOT / "menu_app/BrowserVisualCompanion/visual.js").read_text(encoding="utf-8")
        self.assertIn(".frame.active { opacity: 1; animation: mcpPulse", source)
        self.assertIn(".pill.active .dot { animation: mcpDot", source)
        self.assertIn(".history-panel {", source)
        self.assertIn("visibility: hidden", source)
        self.assertIn(".companion.sidebar-open .history-panel", source)
        self.assertIn("visibility: visible", source)

    def test_manifest_is_visual_content_script_without_privileged_browser_permissions(self) -> None:
        manifest = json.loads((ROOT / "menu_app/BrowserVisualCompanion/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(3, manifest["manifest_version"])
        scripts = manifest["content_scripts"]
        self.assertEqual(1, len(scripts))
        self.assertEqual(["visual.js"], scripts[0]["js"])
        self.assertEqual(["http://*/*", "https://*/*"], scripts[0]["matches"])
        self.assertEqual(["http://*/*", "https://*/*"], manifest.get("host_permissions"))
        permissions = set(manifest.get("permissions", [])) | set(manifest.get("optional_permissions", []))
        self.assertTrue({"tabs", "cookies", "webRequest", "nativeMessaging"}.isdisjoint(permissions))

    def test_build_script_embeds_appex_with_extension_entrypoint(self) -> None:
        source = (ROOT / "menu_app/build_app.sh").read_text(encoding="utf-8")
        self.assertIn('EXTENSION_BUNDLE_ID="${APP_BUNDLE_ID}.safari"', source)
        self.assertIn("-application-extension", source)
        self.assertIn("_NSExtensionMain", source)
        self.assertIn('SIGN_IDENTITY="${MAC_MCP_CODESIGN_IDENTITY:--}"', source)
        self.assertIn("--options runtime --timestamp", source)
        self.assertIn('--sign "${SIGN_IDENTITY}" "${EXTENSION}"', source)
        self.assertIn('--sign "${SIGN_IDENTITY}" "${APP}"', source)
        self.assertIn('BrowserVisualCompanion/manifest.json', source)
        self.assertIn('BrowserVisualCompanion/visual.js', source)

    def test_menu_onboarding_uses_safari_extension_state_and_preferences_api(self) -> None:
        app_state = (ROOT / "menu_app/Sources/AppState.swift").read_text(encoding="utf-8")
        menu = (ROOT / "menu_app/Sources/MenuBarView.swift").read_text(encoding="utf-8")
        self.assertIn("SFSafariExtensionManager.getStateOfSafariExtension", app_state)
        self.assertIn("SFSafariApplication.showPreferencesForExtension", app_state)
        self.assertIn("safariExtensionRegistered", app_state)
        self.assertIn("openUnsignedSafariExtensionSetup", app_state)
        self.assertIn("activateFileViewerSelecting", app_state)
        self.assertIn("Add Temporary Extension", app_state)
        self.assertIn('Button("Enable in Safari…")', menu)
        self.assertIn('Button("Developer Setup…")', menu)
        self.assertIn("Chrome Visual Companion", menu)
        self.assertIn("Browser Activity", menu)
        self.assertIn("openChromeExtensionSetup", app_state)
        self.assertIn("menu_app/ChromeVisualCompanion", app_state)
        self.assertIn("focus-safe background tabs", app_state)
        self.assertIn("background-safe visual capture", menu)

    def test_installer_explains_adhoc_safari_setup(self) -> None:
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("Signature=adhoc", source)
        self.assertIn("Developer Setup…", source)
        self.assertIn("Add Temporary Extension", source)
        self.assertIn("menu_app/BrowserVisualCompanion", source)
        self.assertIn("chrome://extensions", source)
        self.assertIn("Load unpacked", source)
        self.assertIn("menu_app/ChromeVisualCompanion", source)
        self.assertIn("no Apple Events JavaScript toggle is required", source)
        self.assertIn("prepare_chrome_companion", source)

    def test_updater_tracks_and_syncs_new_safari_extension_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            runtime = root / "runtime"
            stage = root / "stage"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "menu_app").mkdir()
            (repo / "menu_app/build_app.sh").write_text("#!/bin/zsh\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "old"], cwd=repo, check=True)

            extension_dir = repo / "menu_app/BrowserVisualCompanion"
            extension_dir.mkdir()
            (extension_dir / "manifest.json").write_text('{"manifest_version":3}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "extension"], cwd=repo, check=True)
            target = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            files = _tracked_files(repo, target)
            rel = "menu_app/BrowserVisualCompanion/manifest.json"
            self.assertIn(rel, files)

            stage.mkdir()
            runtime.mkdir()
            (stage / rel).parent.mkdir(parents=True)
            (stage / rel).write_text('{"manifest_version":3}\n', encoding="utf-8")
            count = _sync_runtime(stage, runtime, [], [rel])
            self.assertEqual(1, count)
            self.assertTrue((runtime / rel).is_file())


if __name__ == "__main__":
    unittest.main()
