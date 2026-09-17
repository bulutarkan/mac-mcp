from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp_server import artifact_pipeline as ap
from mcp_server.artifact_pipeline import ArtifactError
from mcp_server.security import load_settings
from mcp_server.tools_browser import browser_wait_for_download, browser_upload_artifact


class ArtifactIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        ap._ARTIFACTS.clear()

    def test_register_and_resolve_bind_exact_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "report.pdf"
            path.write_bytes(b"pdf-one")
            artifact = ap.register_artifact(path, source="test")
            resolved = ap.resolve_artifact(artifact["artifact_id"], expected_path=path)
            self.assertEqual(path.resolve(), Path(resolved["path"]))
            self.assertEqual(7, resolved["size"])
            self.assertEqual(64, len(resolved["sha256"]))

    def test_stale_artifact_fails_closed_after_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "report.pdf"
            path.write_bytes(b"original")
            artifact = ap.register_artifact(path)
            time.sleep(0.002)
            path.write_bytes(b"replacement")
            with self.assertRaises(ArtifactError) as raised:
                ap.resolve_artifact(artifact["artifact_id"], expected_path=path)
            self.assertEqual("ARTIFACT_STALE", raised.exception.code)

    def test_artifact_path_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = Path(td) / "first.txt"
            second = Path(td) / "second.txt"
            first.write_text("a")
            second.write_text("a")
            artifact = ap.register_artifact(first)
            with self.assertRaises(ArtifactError) as raised:
                ap.resolve_artifact(artifact["artifact_id"], expected_path=second)
            self.assertEqual("ARTIFACT_PATH_MISMATCH", raised.exception.code)

    def test_symlink_artifact_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "real.txt"
            link = root / "link.txt"
            target.write_text("x")
            link.symlink_to(target)
            with self.assertRaises(ArtifactError) as raised:
                ap.register_artifact(link)
            self.assertEqual("ARTIFACT_SYMLINK_REFUSED", raised.exception.code)


class NativeFileDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        ap._ARTIFACTS.clear()

    def test_open_dialog_revalidates_artifact_before_and_after_selection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.txt"
            path.write_text("safe")
            artifact = ap.register_artifact(path)
            selected = []
            with patch.object(ap, "wait_for_file_dialog", return_value=True), \
                 patch.object(ap, "_set_go_to_path", side_effect=lambda *args, **kwargs: selected.append("path")), \
                 patch.object(ap, "_click_panel_button", side_effect=lambda *args, **kwargs: selected.append("open")), \
                 patch.object(ap, "_wait_panel_closed", return_value=True):
                result = ap.drive_native_file_dialog(
                    pid=123, mode="open", artifact_id=artifact["artifact_id"], path=str(path)
                )
            self.assertTrue(result["ok"])
            self.assertEqual(["path", "open"], selected)
            self.assertEqual(artifact["sha256"], result["artifact"]["sha256"])

    def test_cancel_closes_panel_without_artifact(self) -> None:
        with patch.object(ap, "wait_for_file_dialog", return_value=True), \
             patch.object(ap, "_click_panel_button") as click, \
             patch.object(ap, "_wait_panel_closed", return_value=True):
            result = ap.drive_native_file_dialog(pid=123, mode="open", cancel=True)
        click.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_existing_destination_without_overwrite_never_touches_ui(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "existing.txt"
            dest.write_text("old")
            with patch.object(ap, "wait_for_file_dialog", return_value=True), \
                 patch.object(ap, "_set_go_to_path") as navigate:
                with self.assertRaises(ArtifactError) as raised:
                    ap.drive_native_file_dialog(pid=123, mode="save", destination=str(dest), overwrite=False)
            self.assertEqual("DESTINATION_EXISTS", raised.exception.code)
            navigate.assert_not_called()

    def test_overwrite_requires_observed_replace_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "existing.txt"
            dest.write_text("old")
            with patch.object(ap, "wait_for_file_dialog", return_value=True), \
                 patch.object(ap, "_set_go_to_path"), patch.object(ap, "_set_save_name"), \
                 patch.object(ap, "_click_panel_button"), \
                 patch.object(ap, "_click_replace_if_present", return_value=False):
                with self.assertRaises(ArtifactError) as raised:
                    ap.drive_native_file_dialog(pid=123, mode="save", destination=str(dest), overwrite=True)
            self.assertEqual("OVERWRITE_CONFIRMATION_NOT_VERIFIED", raised.exception.code)

    def test_verified_overwrite_registers_new_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "existing.txt"
            dest.write_text("old")
            def replace_confirmed(*args, **kwargs):
                time.sleep(0.002)
                dest.write_text("new content")
                return True
            with patch.object(ap, "wait_for_file_dialog", return_value=True), \
                 patch.object(ap, "_set_go_to_path"), patch.object(ap, "_set_save_name"), \
                 patch.object(ap, "_click_panel_button"), \
                 patch.object(ap, "_click_replace_if_present", side_effect=replace_confirmed):
                result = ap.drive_native_file_dialog(pid=123, mode="save", destination=str(dest), overwrite=True, timeout_s=2)
            self.assertTrue(result["ok"])
            self.assertTrue(result["replace_confirmed"])
            self.assertEqual("new content", dest.read_text())
            self.assertTrue(result["artifact"]["artifact_id"].startswith("artifact_"))


class DownloadArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        ap._ARTIFACTS.clear()

    def test_fast_download_created_before_wait_is_found_from_trigger_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = replace(load_settings(), download_dir=root, max_wait_s=3)
            trigger = int(time.time() * 1000)
            time.sleep(0.003)
            path = root / "invoice.pdf"
            path.write_bytes(b"pdf-data")
            result = browser_wait_for_download(
                settings, filename_contains="invoice", timeout_s=2,
                started_after_epoch_ms=trigger, stable_ms=100,
            )
            self.assertTrue(result["completed"])
            self.assertEqual(path, Path(result["path"]))
            self.assertTrue(result["artifact_id"].startswith("artifact_"))
            self.assertEqual(8, result["artifact"]["size"])

    def test_partial_browser_file_blocks_completion_until_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = replace(load_settings(), download_dir=root, max_wait_s=4)
            trigger = int(time.time() * 1000)
            final = root / "report.pdf"
            partial = root / "report.pdf.crdownload"
            final.write_bytes(b"half")
            partial.write_bytes(b"still-downloading")
            def finish():
                time.sleep(0.35)
                partial.unlink()
                time.sleep(0.002)
                final.write_bytes(b"complete")
            thread = threading.Thread(target=finish)
            thread.start()
            try:
                result = browser_wait_for_download(
                    settings, filename_contains="report", timeout_s=3,
                    started_after_epoch_ms=trigger, stable_ms=100,
                )
            finally:
                thread.join(2)
            self.assertTrue(result["completed"])
            self.assertEqual(b"complete", final.read_bytes())
            self.assertEqual(8, result["artifact"]["size"])


class BrowserUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        ap._ARTIFACTS.clear()
        self.settings = load_settings()

    def test_chrome_upload_uses_debugger_exact_artifact_and_verifies_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.pdf"
            path.write_bytes(b"abcd")
            artifact = ap.register_artifact(path)
            target = type("Target", (), {
                "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
                "tab_handle": "btab_test", "native_id": "441", "title": "Upload", "url": "https://example.test",
            })()
            bridge_result = {"ok": True, "metadata": '{"count":1,"name":"upload.pdf","size":4,"lastModified":1}'}
            from contextlib import nullcontext
            with patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)), \
                 patch("mcp_server.tools_browser.chrome_background_bridge.request_set_file_input", return_value=bridge_result) as set_file:
                result = browser_upload_artifact(
                    self.settings, "Google Chrome", "#file", artifact["artifact_id"], str(path), tab_handle="btab_test"
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["focus_preserved"])
            self.assertEqual("chrome_debugger_dom_set_file_input", result["transport"])
            set_file.assert_called_once_with("441", "#file", str(path.resolve()), timeout_s=20)

    def test_chrome_upload_refuses_metadata_mismatch(self) -> None:
        from contextlib import nullcontext
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.pdf"
            path.write_bytes(b"abcd")
            artifact = ap.register_artifact(path)
            target = type("Target", (), {
                "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
                "tab_handle": "btab_test", "native_id": "441", "title": "Upload", "url": "https://example.test",
            })()
            bridge_result = {"ok": True, "metadata": '{"count":1,"name":"other.pdf","size":4,"lastModified":1}'}
            with patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(target)), \
                 patch("mcp_server.tools_browser.chrome_background_bridge.request_set_file_input", return_value=bridge_result):
                with self.assertRaises(HTTPException) as raised:
                    browser_upload_artifact(
                        self.settings, "Google Chrome", "#file", artifact["artifact_id"], str(path), tab_handle="btab_test"
                    )
            self.assertEqual(409, raised.exception.status_code)


class ArtifactSecurityBoundaryTests(unittest.TestCase):
    def test_untrusted_web_upload_requires_web_host_approval(self) -> None:
        from mcp_server.policy import PolicyContext, resolve_risk
        from mcp_server.security_context import SecurityContextManager
        manager = SecurityContextManager()
        context = PolicyContext(profile="standard", actor="artifact-test")
        key = manager.identity_key(context, None)
        public = "artifact-session"
        manager.observe_browser_result(
            key=key, public_session_id=public, tool="browser_observe",
            arguments={"browser":"Safari"},
            result={"ok":True,"url":"https://evil.example/upload","tab_handle":"tab-upload"},
        )
        arguments = {
            "browser":"Safari", "css_selector":"#file", "artifact_id":"artifact_0123456789abcdef",
            "path":"/tmp/report.pdf", "tab_handle":"tab-upload",
        }
        _, risk = resolve_risk("browser_upload_artifact", arguments)
        decision = manager.evaluate(
            key=key, public_session_id=public, tool="browser_upload_artifact",
            risk=risk, arguments=arguments, profile="standard",
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.approval_required)
        self.assertEqual("web_host_boundary_approval_required", decision.code)

    def test_sensitive_artifact_path_stays_secret_egress_gated_even_trusted(self) -> None:
        from mcp_server.policy import PolicyContext, resolve_risk
        from mcp_server.security_context import SecurityContextManager
        manager = SecurityContextManager()
        context = PolicyContext(profile="trusted", actor="artifact-test")
        key = manager.identity_key(context, None)
        public = "artifact-sensitive-session"
        manager.observe_browser_result(
            key=key, public_session_id=public, tool="browser_observe",
            arguments={"browser":"Safari"},
            result={"ok":True,"url":"https://evil.example/upload","tab_handle":"tab-upload"},
        )
        arguments = {
            "browser":"Safari", "css_selector":"#file", "artifact_id":"artifact_0123456789abcdef",
            "path":"~/.ssh/id_ed25519", "tab_handle":"tab-upload",
        }
        _, risk = resolve_risk("browser_upload_artifact", arguments)
        decision = manager.evaluate(
            key=key, public_session_id=public, tool="browser_upload_artifact",
            risk=risk, arguments=arguments, profile="trusted",
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.approval_required)
        self.assertEqual("secret_egress_approval_required", decision.code)
        self.assertNotIn("id_ed25519", str(decision.target_summary))

    def test_chrome_companion_uses_dom_set_file_input_files(self) -> None:
        worker = (Path(__file__).parents[1] / "menu_app" / "ChromeVisualCompanion" / "background.js").read_text()
        self.assertIn("DOM.setFileInputFiles", worker)
        self.assertIn("set_file_input", worker)


class ArtifactHashRaceTests(unittest.TestCase):
    def test_register_refuses_file_changed_during_hash(self) -> None:
        ap._ARTIFACTS.clear()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "moving.bin"
            path.write_bytes(b"first")
            real_hash = ap._sha256
            def mutate_during_hash(target):
                digest = real_hash(target)
                time.sleep(0.002)
                target.write_bytes(b"second-longer")
                return digest
            with patch.object(ap, "_sha256", side_effect=mutate_during_hash):
                with self.assertRaises(ArtifactError) as raised:
                    ap.register_artifact(path)
            self.assertEqual("ARTIFACT_CHANGED_DURING_HASH", raised.exception.code)


class BrowserJavaScriptNilResultTests(unittest.TestCase):
    def test_safari_script_initializes_result_before_undefined_javascript(self) -> None:
        from mcp_server import tools_browser
        target = type("Target", (), {
            "browser": "Safari", "window_index": 1, "tab_index": 1,
            "tab_handle": "btab_safari_nil", "native_id": "3001", "title": "Fixture", "url": "http://127.0.0.1/",
        })()
        scripts = []
        with patch.object(tools_browser, "_run_osascript", side_effect=lambda script, timeout_s=30: scripts.append(script) or ""):
            out = tools_browser._execute_js_for_target("Safari", "document.body.focus()", target, 5)
        self.assertEqual("", out)
        self.assertIn('set r to ""', scripts[0])
        self.assertLess(scripts[0].index('set r to ""'), scripts[0].index('set r to do JavaScript'))


if __name__ == "__main__":
    unittest.main()
