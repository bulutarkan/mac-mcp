from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mcp_server.tools_browser import _safari_visual_claim_js, _safari_visual_claim_script
from mcp_server.tools_browser_agent import _batch_js, _browser_state_bootstrap, _observe_js
from mcp_server.update_helper import _sync_runtime, _tracked_files

ROOT = Path(__file__).resolve().parents[1]


class SafariVisualCompanionTests(unittest.TestCase):
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
        source = (ROOT / "menu_app/SafariExtension/visual.js").read_text(encoding="utf-8")
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
        source = (ROOT / "menu_app/SafariExtension/visual.js").read_text(encoding="utf-8")
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
        self.assertIn("document.readyState==='loading'", js)
        self.assertIn("location.href", js)  # used only to ensure the final document is the claimed target
        self.assertIn("claim:true", js)
        self.assertIn("action:'Opened'", js)
        self.assertIn("target_kind:'Page'", js)
        for forbidden in ("document.title", "innerText", "textContent"):
            self.assertNotIn(forbidden, js)

    def test_visual_attribute_does_not_advance_browser_dom_revision(self) -> None:
        bootstrap = _browser_state_bootstrap()
        self.assertIn("rec.attributeName==='data-mac-mcp-visual-event'", bootstrap)
        self.assertIn("continue;", bootstrap)
        observe = _observe_js("interactive", 20)
        self.assertIn("__mcpVisual('Inspecting'", observe)

    def test_manifest_is_visual_content_script_without_privileged_browser_permissions(self) -> None:
        manifest = json.loads((ROOT / "menu_app/SafariExtension/manifest.json").read_text(encoding="utf-8"))
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
        self.assertIn("Safari Visual Companion", menu)

    def test_installer_explains_adhoc_safari_setup(self) -> None:
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("Signature=adhoc", source)
        self.assertIn("Developer Setup…", source)
        self.assertIn("Add Temporary Extension", source)
        self.assertIn("Mac MCP Safari Visual Companion.appex/Contents/Resources", source)

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

            extension_dir = repo / "menu_app/SafariExtension"
            extension_dir.mkdir()
            (extension_dir / "manifest.json").write_text('{"manifest_version":3}\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "extension"], cwd=repo, check=True)
            target = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            files = _tracked_files(repo, target)
            rel = "menu_app/SafariExtension/manifest.json"
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
