from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import artifact_pipeline as ap
from mcp_server import context_handoff as ch
from mcp_server import native_targets, tools_ui
from mcp_server.policy import PolicyContext, evaluate_tool_scope, resolve_risk
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings
from mcp_server.security_context import SecurityContextManager
from mcp_server.tools_browser import browser_upload_artifact


def _window(index: int, title: str, x: int) -> dict:
    return {
        "index": index,
        "title": title,
        "document": "",
        "identifier": "",
        "position": {"x": x, "y": 40, "width": 640, "height": 480},
        "subrole": "AXStandardWindow",
        "focused": index == 1,
        "main": index == 1,
    }


def _native_metadata() -> dict:
    with patch.object(native_targets, "_process_instance_token", return_value="fixture-start"):
        return native_targets.decorate_metadata({
            "active_app": "DemoApp",
            "pid": 4242,
            "bundle_id": "com.example.demo",
            "frontmost": True,
            "window_count": 2,
            "window_names": ["Editor A", "Editor B"],
            "windows": [_window(1, "Editor A", 20), _window(2, "Editor B", 700)],
        })


def _mail_metadata(subject: str = "Roadmap32 Unique Draft", *, duplicate: bool = False) -> dict:
    windows = [_window(1, subject, 20)]
    if duplicate:
        windows.append(_window(2, subject, 700))
    with patch.object(native_targets, "_process_instance_token", return_value="mail-fixture-start"):
        return native_targets.decorate_metadata({
            "active_app": "Mail",
            "pid": 4343,
            "bundle_id": "com.apple.mail",
            "frontmost": True,
            "window_count": len(windows),
            "window_names": [subject for _ in windows],
            "windows": windows,
        })


def _browser_row(*, browser: str = "Safari", handle: str = "btab_source", url: str = "https://example.test/article") -> dict:
    return {
        "browser": browser,
        "window_index": 1,
        "tab_index": 1,
        "active": True,
        "native_id": "9001",
        "title": "Article",
        "url": url,
        "tab_handle": handle,
    }


class ContextHandoffEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        ch.reset_handoffs_for_tests()
        ap._ARTIFACTS.clear()
        native_targets.reset_registries_for_tests()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()
        self.settings = load_settings()
        self.meta = _native_metadata()
        self.app_handle = self.meta["app_handle"]
        self.window_a = self.meta["windows"][0]["window_handle"]
        self.window_b = self.meta["windows"][1]["window_handle"]

    def _create_browser_text(self, *, selected: str = "Selected browser text") -> dict:
        source = _browser_row()
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True,
                 "result": json.dumps({"selection": selected, "url": source["url"], "title": source["title"]}),
             }):
            return ch.create_browser_text_handoff(
                self.settings,
                browser="Safari", tab_handle=source["tab_handle"],
                target_app="DemoApp", target_app_handle=self.app_handle,
                target_window_handle=self.window_a,
                target_observation_id="obs_target_a", target_element_id="w1/1",
                include_url=True, clear=False,
            )

    def test_browser_text_envelope_binds_source_target_and_integrity(self) -> None:
        created = self._create_browser_text()
        self.assertTrue(created["ok"])
        self.assertEqual("text_url", created["kind"])
        self.assertEqual("https://example.test", created["source"]["origin"])
        self.assertEqual("untrusted_web", created["source"]["provenance_class"])
        self.assertEqual(self.window_a, created["target"]["window_handle"])
        self.assertEqual(64, len(created["integrity"]["envelope_sha256"]))
        self.assertTrue(created["single_use"])
        resolved = ch.resolve_native_text_handoff(
            created["handoff_id"], app="DemoApp", app_handle=self.app_handle,
            window_handle=self.window_a, observation_id="obs_target_a", element_id="w1/1",
        )
        self.assertEqual("Selected browser text\n\nSource: https://example.test/article", resolved["text"])

    def test_wrong_native_target_is_rejected_before_transfer(self) -> None:
        created = self._create_browser_text()
        with self.assertRaises(ch.HandoffError) as raised:
            ch.resolve_native_text_handoff(
                created["handoff_id"], app="DemoApp", app_handle=self.app_handle,
                window_handle=self.window_b, observation_id="obs_target_a", element_id="w1/1",
            )
        self.assertEqual("HANDOFF_TARGET_MISMATCH", raised.exception.code)

    def test_integrity_tamper_is_rejected(self) -> None:
        created = self._create_browser_text()
        ch._HANDOFFS[created["handoff_id"]]["payload"]["rendered_text"] = "tampered"
        with self.assertRaises(ch.HandoffError) as raised:
            ch.resolve_native_text_handoff(
                created["handoff_id"], app="DemoApp", app_handle=self.app_handle,
                window_handle=self.window_a, observation_id="obs_target_a", element_id="w1/1",
            )
        self.assertEqual("HANDOFF_INTEGRITY_FAILED", raised.exception.code)

    def test_handoff_is_single_use_and_replay_fails_closed(self) -> None:
        created = self._create_browser_text()
        ch.mark_handoff_consumed(created["handoff_id"], consumer="test")
        with self.assertRaises(ch.HandoffError) as raised:
            ch.resolve_native_text_handoff(
                created["handoff_id"], app="DemoApp", app_handle=self.app_handle,
                window_handle=self.window_a, observation_id="obs_target_a", element_id="w1/1",
            )
        self.assertEqual("HANDOFF_CONSUMED", raised.exception.code)
        inspected = ch.inspect_handoff(created["handoff_id"])
        self.assertTrue(inspected["consumed"])
        self.assertEqual("test", inspected["consumed_by"])

    def test_browser_navigation_during_capture_is_rejected(self) -> None:
        source = _browser_row(url="https://example.test/old")
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True,
                 "result": json.dumps({"selection": "x", "url": "https://example.test/new", "title": "New"}),
             }):
            with self.assertRaises(ch.HandoffError) as raised:
                ch.create_browser_text_handoff(
                    self.settings, browser="Safari", tab_handle="btab_source",
                    target_app="DemoApp", target_app_handle=self.app_handle,
                    target_window_handle=self.window_a,
                    target_observation_id="obs_a", target_element_id="w1/1",
                )
        self.assertEqual("HANDOFF_SOURCE_NAVIGATED", raised.exception.code)

    def test_browser_download_cannot_be_laundered_as_local_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "download.pdf"
            path.write_bytes(b"pdf")
            artifact = ap.register_artifact(path, source="browser_download")
            result = ch.context_handoff(
                self.settings, action="create_artifact", source_type="artifact",
                path=str(path), artifact_id=artifact["artifact_id"],
                target_kind="native_file_dialog", target_app="DemoApp",
                target_app_handle=self.app_handle, target_window_handle=self.window_a,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("HANDOFF_BROWSER_PROVENANCE_REQUIRED", result["reason_code"])

    def test_stale_artifact_invalidates_existing_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.txt"
            path.write_text("original")
            artifact = ap.register_artifact(path, source="test")
            created = ch.create_artifact_handoff(
                self.settings, source_type="artifact", path=str(path), artifact_id=artifact["artifact_id"],
                target_kind="native_file_dialog", target_app="DemoApp",
                target_app_handle=self.app_handle, target_window_handle=self.window_a,
            )
            path.write_text("changed-content")
            with self.assertRaises(ch.HandoffError) as raised:
                ch.resolve_native_file_handoff(
                    created["handoff_id"], app="DemoApp", app_handle=self.app_handle,
                    window_handle=self.window_a,
                )
        self.assertEqual("ARTIFACT_STALE", raised.exception.code)


class ContextHandoffMailTests(unittest.TestCase):
    def setUp(self) -> None:
        ch.reset_handoffs_for_tests()
        ap._ARTIFACTS.clear()
        native_targets.reset_registries_for_tests()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()
        self.settings = load_settings()

    def _mail_text_handoff(self, subject: str = "Roadmap32 Unique Draft") -> tuple[dict, dict, str]:
        meta = _mail_metadata(subject)
        app_handle = meta["app_handle"]
        window_handle = meta["windows"][0]["window_handle"]
        node = {
            "element_id": "w1/1", "parent_id": "w1", "role": "AXWebArea", "subrole": "",
            "title": "", "description": "message body", "value": "",
            "position": {"x": 100, "y": 100, "width": 300, "height": 120},
            "enabled": True, "focused": False, "actions": [], "child_count": 0,
        }
        obs_id = tools_ui._save_observation("Mail", 1, [node], meta)
        source = _browser_row()
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True,
                 "result": json.dumps({"selection": "hello", "url": source["url"], "title": source["title"]}),
             }):
            handoff = ch.create_browser_text_handoff(
                self.settings, browser="Safari", tab_handle=source["tab_handle"],
                target_app="Mail", target_app_handle=app_handle, target_window_handle=window_handle,
                target_observation_id=obs_id, target_element_id="w1/1", clear=False,
            )
        return meta, handoff, obs_id

    def test_mail_text_handoff_requires_stable_unique_subject_window(self) -> None:
        meta = _mail_metadata("Duplicate Subject", duplicate=True)
        source = _browser_row()
        # Duplicate compose titles fall back to geometry identity, which is not safe enough
        # to map to one Mail outgoing-message object by subject.
        win = meta["windows"][0]
        self.assertEqual("fingerprint", win["identity_kind"])
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True, "result": json.dumps({"selection": "hello", "url": source["url"], "title": source["title"]}),
             }):
            handoff = ch.create_browser_text_handoff(
                self.settings, browser="Safari", tab_handle=source["tab_handle"],
                target_app="Mail", target_app_handle=meta["app_handle"],
                target_window_handle=win["window_handle"], target_observation_id="obs_mail",
                target_element_id="w1/1",
            )
        with self.assertRaises(ch.HandoffError) as raised:
            ch.resolve_mail_text_handoff(
                handoff["handoff_id"], app="Mail", app_handle=meta["app_handle"],
                window_handle=win["window_handle"], observation_id="obs_mail", element_id="w1/1",
            )
        self.assertEqual("HANDOFF_MAIL_DRAFT_IDENTITY_REQUIRED", raised.exception.code)

    def test_mail_text_mac_act_uses_sealed_subject_and_payload(self) -> None:
        meta, handoff, obs_id = self._mail_text_handoff()
        win = meta["windows"][0]
        ready = {
            "ready": True, "state": {
                "connected": True, "role": "AXWebArea", "subrole": "", "title": "",
                "value": "", "character_count": 0, "selected": False, "enabled": True,
                "position": {"x": 100, "y": 100, "width": 300, "height": 120},
                "window_position": {"x": 20, "y": 40, "width": 640, "height": 480},
                "window_title": "Roadmap32 Unique Draft", "window_count": 1, "window_child_count": 1,
                "sheet_count": 0, "popover_count": 0, "menu_count": 0,
            },
        }
        with patch.object(tools_ui, "_scan_native_windows", return_value=(meta, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_mail_draft_text_handoff", return_value={"ok": True, "verified": True}) as apply_mail:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "handoff_mail_text", "element_id": "w1/1", "handoff_id": handoff["handoff_id"]}],
                observation_id=obs_id, app_handle=meta["app_handle"], window_handle=win["window_handle"],
                state_mode="none", preserve_focus=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual("Roadmap32 Unique Draft", apply_mail.call_args.args[0])
        self.assertEqual("hello\n\nSource: https://example.test/article", apply_mail.call_args.args[1])
        self.assertFalse(apply_mail.call_args.kwargs["clear"])
        self.assertTrue(ch.inspect_handoff(handoff["handoff_id"])["consumed"])

    def test_mail_attachment_mac_act_uses_exact_artifact_and_consumes_once(self) -> None:
        meta = _mail_metadata("Roadmap32 Attachment Draft")
        win = meta["windows"][0]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "download.pdf"
            path.write_bytes(b"pdf-data")
            artifact = ap.register_artifact(path, source="browser_download")
            source = _browser_row(browser="Google Chrome", handle="btab_download", url="https://source.example/file")
            with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)):
                handoff = ch.create_artifact_handoff(
                    self.settings, source_type="browser_artifact", path=str(path), artifact_id=artifact["artifact_id"],
                    source_browser="Google Chrome", source_tab_handle="btab_download",
                    target_kind="mail_draft_attachment", target_app="Mail",
                    target_app_handle=meta["app_handle"], target_window_handle=win["window_handle"],
                )
            with patch.object(tools_ui, "_scan_native_windows", return_value=(meta, None)), \
                 patch.object(tools_ui, "_mail_draft_attachment_handoff", return_value={"ok": True, "verified": True}) as attach:
                result = tools_ui.act_ui(
                    self.settings, [{"type": "handoff_mail_attachment", "handoff_id": handoff["handoff_id"]}],
                    app="Mail", app_handle=meta["app_handle"], window_handle=win["window_handle"],
                    state_mode="none", preserve_focus=True,
                )
            self.assertTrue(result["ok"])
            self.assertEqual("Roadmap32 Attachment Draft", attach.call_args.args[0])
            self.assertEqual(artifact["artifact_id"], attach.call_args.args[1]["artifact_id"])
            self.assertTrue(ch.inspect_handoff(handoff["handoff_id"])["consumed"])

    def test_mail_helpers_fail_closed_on_ambiguous_or_unverified_result(self) -> None:
        with patch.object(tools_ui, "_run_osascript", return_value=(True, "DRAFT_COUNT\t2", "")):
            with self.assertRaises(ch.HandoffError) as raised:
                tools_ui._mail_draft_text_handoff("Duplicate", "text", clear=False)
        self.assertEqual("HANDOFF_MAIL_DRAFT_NOT_UNIQUE", raised.exception.code)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.txt"
            path.write_text("x")
            artifact = ap.register_artifact(path, source="test")
            with patch.object(tools_ui, "_run_osascript", return_value=(True, "VERIFY_FAILED\t0\t0", "")):
                with self.assertRaises(ch.HandoffError) as raised2:
                    tools_ui._mail_draft_attachment_handoff("Unique", artifact)
            self.assertEqual("HANDOFF_MAIL_ATTACHMENT_NOT_VERIFIED", raised2.exception.code)


class ContextHandoffBrowserUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        ch.reset_handoffs_for_tests()
        ap._ARTIFACTS.clear()
        self.settings = load_settings()

    def test_finder_to_browser_target_binds_tab_url_selector_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "finder.txt"
            path.write_text("finder file")
            target = _browser_row(browser="Google Chrome", handle="btab_upload", url="https://upload.example/form")
            with patch("mcp_server.tools_snapshot._read_selected_context", return_value={
                    "selected_paths": [str(path.resolve())], "selected_count": 1,
                 }), patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, target)):
                created = ch.create_artifact_handoff(
                    self.settings, source_type="finder", path=str(path), target_kind="browser_upload",
                    target_browser="Google Chrome", target_tab_handle="btab_upload", target_css_selector="#file",
                )
            artifact = created["payload"]
            resolved = ch.resolve_browser_upload_handoff(
                created["handoff_id"], browser="Google Chrome", tab_handle="btab_upload",
                current_url="https://upload.example/form", css_selector="#file",
                artifact_id=artifact["artifact_id"], path=artifact["path"],
            )
            self.assertEqual(artifact["sha256"], resolved["artifact"]["sha256"])
            with self.assertRaises(ch.HandoffError) as raised:
                ch.resolve_browser_upload_handoff(
                    created["handoff_id"], browser="Google Chrome", tab_handle="btab_upload",
                    current_url="https://upload.example/other", css_selector="#file",
                    artifact_id=artifact["artifact_id"], path=artifact["path"],
                )
            self.assertEqual("HANDOFF_TARGET_NAVIGATED", raised.exception.code)

    def test_chrome_upload_consumes_exact_handoff_before_bridge_and_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.pdf"
            path.write_bytes(b"abcd")
            artifact = ap.register_artifact(path, source="test")
            target_row = _browser_row(browser="Google Chrome", handle="btab_upload", url="https://upload.example/form")
            with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, target_row)):
                created = ch.create_artifact_handoff(
                    self.settings, source_type="artifact", path=str(path), artifact_id=artifact["artifact_id"],
                    target_kind="browser_upload", target_browser="Google Chrome",
                    target_tab_handle="btab_upload", target_css_selector="#file",
                )
            lease_target = type("Target", (), {
                "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
                "tab_handle": "btab_upload", "native_id": "441", "title": "Upload",
                "url": "https://upload.example/form",
            })()
            bridge_result = {"ok": True, "metadata": '{"count":1,"name":"upload.pdf","size":4,"lastModified":1}'}
            with patch("mcp_server.tools_browser._tab_lease", return_value=nullcontext(lease_target)), \
                 patch("mcp_server.tools_browser.chrome_background_bridge.request_set_file_input", return_value=bridge_result) as bridge:
                result = browser_upload_artifact(
                    self.settings, "Google Chrome", "#file", artifact["artifact_id"], str(path),
                    tab_handle="btab_upload", handoff_id=created["handoff_id"],
                )
                self.assertTrue(result["handoff_consumed"])
                with self.assertRaises(HTTPException) as raised:
                    browser_upload_artifact(
                        self.settings, "Google Chrome", "#file", artifact["artifact_id"], str(path),
                        tab_handle="btab_upload", handoff_id=created["handoff_id"],
                    )
            self.assertEqual(409, raised.exception.status_code)
            bridge.assert_called_once()


class ContextHandoffPolicyAndProvenanceTests(unittest.TestCase):
    def test_browser_handoff_marks_sticky_untrusted_provenance(self) -> None:
        manager = SecurityContextManager()
        context = PolicyContext(profile="standard", actor="handoff-test")
        key = manager.identity_key(context, None)
        manager.observe_browser_result(
            key=key, public_session_id="handoff-session", tool="context_handoff",
            arguments={"action": "create_browser_text", "browser": "Safari", "tab_handle": "btab_evil"},
            result={
                "ok": True,
                "source": {"kind": "browser", "url": "https://evil.example/article", "title": "Article", "tab_handle": "btab_evil"},
            },
        )
        state = manager.state_for_public_session("handoff-session")
        self.assertTrue(state["web_scoped"])
        self.assertEqual("tainted_untrusted_web", state["provenance_class"])
        _, risk = resolve_risk("mac_act", {"actions": [{"type": "type", "handoff_id": "handoff_x"}]})
        decision = manager.evaluate(
            key=key, public_session_id="handoff-session", tool="mac_act", risk=risk,
            arguments={"actions": [{"type": "type", "handoff_id": "handoff_x"}]}, profile="standard",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("web_host_boundary_approval_required", decision.code)

    def test_finder_handoff_result_does_not_create_web_taint(self) -> None:
        manager = SecurityContextManager()
        manager.observe_browser_result(
            key="session:local", public_session_id="local", tool="context_handoff",
            arguments={"action": "create_artifact", "source_type": "finder", "target_kind": "native_file_dialog"},
            result={"ok": True, "source": {"kind": "finder", "path": "/tmp/a"}, "target": {"kind": "native_file_dialog"}},
        )
        self.assertIsNone(manager.state_for_public_session("local"))

    def test_local_artifact_to_browser_tracks_target_origin_without_laundering_source(self) -> None:
        manager = SecurityContextManager()
        manager.observe_browser_result(
            key="session:upload", public_session_id="upload", tool="context_handoff",
            arguments={"action": "create_artifact", "source_type": "finder", "target_kind": "browser_upload"},
            result={
                "ok": True,
                "source": {"kind": "finder", "path": "/tmp/report.pdf", "provenance_class": "local"},
                "target": {"kind": "browser_upload", "url": "https://upload.example/form", "tab_handle": "btab_upload", "title": "Upload"},
            },
        )
        state = manager.state_for_public_session("upload")
        self.assertEqual("https://upload.example", state["current_origin"])
        self.assertFalse(state["web_scoped"])
        self.assertEqual("local", state["provenance_class"])
        _, risk = resolve_risk("browser_upload_artifact", {
            "browser": "Safari", "css_selector": "#file", "artifact_id": "artifact_x",
            "path": "~/.ssh/id_ed25519", "tab_handle": "btab_upload",
        })
        decision = manager.evaluate(
            key="session:upload", public_session_id="upload", tool="browser_upload_artifact", risk=risk,
            arguments={"browser": "Safari", "css_selector": "#file", "artifact_id": "artifact_x",
                       "path": "~/.ssh/id_ed25519", "tab_handle": "btab_upload"},
            profile="trusted",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("secret_egress_approval_required", decision.code)

    def test_browser_source_and_browser_target_keep_provenance_source_but_current_target(self) -> None:
        manager = SecurityContextManager()
        manager.observe_browser_result(
            key="session:cross", public_session_id="cross", tool="context_handoff",
            arguments={"action": "create_artifact", "source_type": "browser_artifact", "target_kind": "browser_upload"},
            result={
                "ok": True,
                "source": {"kind": "browser", "url": "https://source.example/file", "tab_handle": "btab_source", "title": "Source"},
                "target": {"kind": "browser_upload", "url": "https://target.example/form", "tab_handle": "btab_target", "title": "Target"},
            },
        )
        state = manager.state_for_public_session("cross")
        self.assertEqual("https://source.example", state["provenance_origin"])
        self.assertEqual("https://target.example", state["current_origin"])
        self.assertEqual("btab_target", state["tab_handle"])
        self.assertEqual("tainted_untrusted_web", state["provenance_class"])

    def test_scoped_handoff_checks_source_and_target_browser_handles(self) -> None:
        scope = ResourceScope(browser_tabs=("btab_allowed",), tool_families=("macos",), access_mode="read_only")
        _, risk = resolve_risk("context_handoff", {
            "action": "create_artifact", "source_type": "browser_artifact",
            "source_tab_handle": "btab_denied", "target_tab_handle": "btab_allowed",
            "target_kind": "browser_upload",
        })
        decision = evaluate_tool_scope(scope, "context_handoff", {
            "action": "create_artifact", "source_type": "browser_artifact",
            "source_tab_handle": "btab_denied", "target_tab_handle": "btab_allowed",
            "target_kind": "browser_upload",
        }, risk)
        self.assertFalse(decision.allowed)
        self.assertIn("browser_tab_not_allowed", decision.reasons)


class ContextHandoffMacActTests(unittest.TestCase):
    def setUp(self) -> None:
        ch.reset_handoffs_for_tests()
        native_targets.reset_registries_for_tests()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()
        self.settings = load_settings()
        self.meta = _native_metadata()

    def test_paste_handoff_injects_sealed_text_and_consumes_once(self) -> None:
        app_handle = self.meta["app_handle"]
        win_a = self.meta["windows"][0]["window_handle"]
        source = _browser_row()
        node = {
            "element_id": "w1/1", "parent_id": "w1", "role": "AXWebArea", "subrole": "",
            "title": "", "description": "message body", "value": "",
            "position": {"x": 100, "y": 100, "width": 300, "height": 120},
            "enabled": True, "focused": False, "actions": [], "child_count": 0,
        }
        obs_id = tools_ui._save_observation("DemoApp", 1, [node], self.meta)
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True,
                 "result": json.dumps({"selection": "hello", "url": source["url"], "title": source["title"]}),
             }):
            handoff = ch.create_browser_text_handoff(
                self.settings, browser="Safari", tab_handle="btab_source",
                target_app="DemoApp", target_app_handle=app_handle, target_window_handle=win_a,
                target_observation_id=obs_id, target_element_id="w1/1", clear=False,
            )
        ready = {
            "ready": True, "state": {
                "connected": True, "role": "AXWebArea", "subrole": "", "title": "",
                "value": "", "character_count": 0, "selected": False, "enabled": True,
                "position": {"x": 100, "y": 100, "width": 300, "height": 120},
                "window_position": {"x": 20, "y": 40, "width": 640, "height": 480},
                "window_title": "Editor A", "window_count": 2, "window_child_count": 1,
                "sheet_count": 0, "popover_count": 0, "menu_count": 0,
            },
        }
        verified = {
            "effect_observed": True, "verification": "text_changed", "attempts": 1,
            "state": {**ready["state"], "value": "hello\n\nSource: https://example.test/article", "character_count": 43},
        }
        with patch.object(tools_ui, "_scan_native_windows", return_value=(self.meta, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "paste completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=verified):
            result = tools_ui.act_ui(
                self.settings, [{"type": "paste", "element_id": "w1/1", "handoff_id": handoff["handoff_id"]}],
                observation_id=obs_id, app_handle=app_handle, window_handle=win_a,
                state_mode="none", preserve_focus=False,
            )
        self.assertTrue(result["ok"])
        called = perform.call_args.args[1]
        self.assertEqual("hello\n\nSource: https://example.test/article", called["text"])
        self.assertTrue(ch.inspect_handoff(handoff["handoff_id"])["consumed"])

    def test_wrong_bound_observation_fails_before_native_action(self) -> None:
        app_handle = self.meta["app_handle"]
        win_a = self.meta["windows"][0]["window_handle"]
        win_b = self.meta["windows"][1]["window_handle"]
        source = _browser_row()
        with patch.object(ch.browser_tabs, "resolve_tab", return_value=(1, 1, source)), \
             patch("mcp_server.tools_browser.browser_execute_js", return_value={
                 "ok": True,
                 "result": json.dumps({"selection": "hello", "url": source["url"], "title": source["title"]}),
             }):
            handoff = ch.create_browser_text_handoff(
                self.settings, browser="Safari", tab_handle="btab_source",
                target_app="DemoApp", target_app_handle=app_handle, target_window_handle=win_a,
                target_observation_id="obs_a", target_element_id="w1/1",
            )
        node = {
            "element_id": "w2/1", "parent_id": "w2", "role": "AXTextField", "subrole": "",
            "title": "Body", "description": "", "value": "", "position": {"x": 800, "y": 100, "width": 200, "height": 40},
            "enabled": True, "focused": False, "actions": [], "child_count": 0,
        }
        obs_b = tools_ui._save_observation("DemoApp", 2, [node], self.meta)
        with patch.object(tools_ui, "_scan_native_windows", return_value=(self.meta, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings, [{"type": "type", "element_id": "w2/1", "handoff_id": handoff["handoff_id"]}],
                observation_id=obs_b, app_handle=app_handle, window_handle=win_b,
                state_mode="none", preserve_focus=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("HANDOFF_TARGET_MISMATCH", result["reason_code"])
        perform.assert_not_called()
        self.assertFalse(ch.inspect_handoff(handoff["handoff_id"])["consumed"])


if __name__ == "__main__":
    unittest.main()
