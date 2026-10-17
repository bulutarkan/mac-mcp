"""Mission Control desktops: the mac_app adapter, the helper wrapper and the Chrome path."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import desktops
from mcp_server.app_adapters import is_read_action, mac_app, normalize_app_name, supported_actions
from mcp_server.chrome_background_bridge import EXPECTED_COMPANION_FEATURES, ChromeBackgroundBridge
from mcp_server.policy import AccessMode, resolve_risk

SETTINGS = SimpleNamespace()


def _listing(chrome_windows=(), front_pid=100):
    """A helper `spaces` answer: one display, desktops 3 (on screen) and 43 (empty)."""
    return {
        "ok": True,
        "frontmost": {"app": "Safari", "pid": front_pid, "bundle_id": "com.apple.Safari"},
        "displays": [{
            "display": "D1", "visible_space": 3, "all_desktop_windows": [],
            "spaces": [
                {"id": 3, "kind": "desktop", "visible": True, "empty": False, "windows": []},
                {"id": 43, "kind": "desktop", "visible": False, "empty": not chrome_windows,
                 "windows": [{"window_id": wid, "app": "Google Chrome"} for wid in chrome_windows]},
            ],
        }],
    }


class RegistryTests(unittest.TestCase):
    def test_aliases_and_actions(self) -> None:
        for name in ("Mission Control", "desktops", "spaces", "DESKTOP"):
            self.assertEqual("Mission Control", normalize_app_name(name))
        self.assertEqual(
            ("list_desktops", "create_desktop", "remove_desktop", "open_on_desktop", "move_to_desktop"),
            supported_actions("desktops"),
        )
        self.assertTrue(is_read_action("list_desktops"))
        self.assertFalse(is_read_action("open_on_desktop"))

    def test_policy_reads_listing_and_adds_no_destructive_gate(self) -> None:
        _, listing = resolve_risk("mac_app", {"app": "Mission Control", "action": "list_desktops"})
        self.assertEqual(AccessMode.READ_ONLY, listing.requested_access_mode)
        for action in ("create_desktop", "remove_desktop", "open_on_desktop", "move_to_desktop"):
            _, risk = resolve_risk("mac_app", {"app": "Mission Control", "action": action})
            self.assertFalse(risk.destructive, action)


class AdapterTests(unittest.TestCase):
    def test_list_desktops_passes_the_listing_through(self) -> None:
        with patch.object(desktops, "list_desktops", return_value=_listing()):
            result = mac_app(SETTINGS, app="desktops", action="list_desktops")
        self.assertTrue(result["ok"])
        self.assertEqual("Mission Control", result["app"])
        self.assertEqual(43, result["displays"][0]["spaces"][1]["id"])
        self.assertNotIn("verified", result)

    def test_open_on_desktop_forwards_app_path_and_url(self) -> None:
        answer = {"ok": True, "space_id": 43, "windows": [{"window_id": 9, "spaces": [43]}], "focus_changed": False}
        with patch.object(desktops, "open_on_desktop", return_value=answer) as opened:
            result = mac_app(SETTINGS, app="Mission Control", action="open_on_desktop", item_id="43",
                             target_app="com.apple.TextEdit", path="/tmp/a.txt")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        opened.assert_called_once_with(43, "com.apple.TextEdit", ["/tmp/a.txt"])

    def test_open_on_desktop_needs_target_app_and_desktop_id(self) -> None:
        missing_app = mac_app(SETTINGS, app="Mission Control", action="open_on_desktop", item_id="43")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", missing_app["reason_code"])
        bad_id = mac_app(SETTINGS, app="Mission Control", action="open_on_desktop", item_id="left", target_app="Notes")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", bad_id["reason_code"])

    def test_create_desktop_needs_a_display(self) -> None:
        result = mac_app(SETTINGS, app="Mission Control", action="create_desktop")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])

    def test_helper_refusal_keeps_its_reason_and_details(self) -> None:
        refusal = {"ok": False, "error": "desktop 43 still has windows", "reason_code": "SPACE_NOT_EMPTY",
                   "windows": [{"window_id": 9, "app": "TextEdit"}]}
        with patch.object(desktops, "remove_desktop", return_value=refusal):
            result = mac_app(SETTINGS, app="Mission Control", action="remove_desktop", item_id="43")
        self.assertFalse(result["ok"])
        self.assertEqual("SPACE_NOT_EMPTY", result["reason_code"])
        self.assertEqual([{"window_id": 9, "app": "TextEdit"}], result["windows"])

    def test_companion_message_becomes_the_error_text(self) -> None:
        # The Chrome companion explains failures in "message", which must not clash with the error's own text.
        refusal = {"ok": False, "reason_code": "CHROME_OPEN_WINDOW_FAILED", "error": "chrome_companion_update_required",
                   "message": "Reload it once at chrome://extensions", "feature": "open_window"}
        with patch.object(desktops, "open_on_desktop", return_value=refusal):
            result = mac_app(SETTINGS, app="Mission Control", action="open_on_desktop", item_id="43", target_app="Chrome")
        self.assertEqual("CHROME_OPEN_WINDOW_FAILED", result["reason_code"])
        self.assertEqual("chrome_companion_update_required", result["error"])
        self.assertEqual("Reload it once at chrome://extensions", result["detail"])
        self.assertEqual("open_window", result["feature"])

    def test_desktop_changes_skip_the_human_active_app_guard(self) -> None:
        # They never touch a window the person is using, so an active person does not block them.
        answer = {"ok": True, "space_id": 43, "windows": [{"window_id": 9, "spaces": [43]}]}
        with patch("mcp_server.app_adapters.native_app_human_takeover", return_value={"reason_code": "HUMAN_ACTIVE_RESOURCE"}), \
             patch.object(desktops, "move_windows", return_value=answer):
            result = mac_app(SETTINGS, app="Mission Control", action="move_to_desktop", item_id="43", window_ids=[9])
        self.assertTrue(result["ok"])


class HelperWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(desktops, "_helper", return_value="/bin/spaces-helper")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_parses_json_and_passes_arguments(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "space_id": 9}), stderr="")
        with patch("mcp_server.desktops.subprocess.run", return_value=completed) as run:
            self.assertEqual({"ok": True, "space_id": 9}, desktops.remove_desktop(9))
        self.assertEqual(["/bin/spaces-helper", "remove", "9"], run.call_args.args[0])

    def test_run_reports_unreadable_answer(self) -> None:
        completed = SimpleNamespace(returncode=5, stdout="", stderr="boom")
        with patch("mcp_server.desktops.subprocess.run", return_value=completed):
            result = desktops.list_desktops()
        self.assertEqual("SPACES_HELPER_FAILED", result["reason_code"])

    def test_move_requires_window_ids(self) -> None:
        self.assertEqual("INVALID_ARGUMENT", desktops.move_windows(43, [])["reason_code"])

    def test_textedit_without_a_document_gets_a_new_file(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(desktops, "_AGENT_DOCUMENTS", Path(folder)), \
             patch.object(desktops, "_run", return_value={"ok": True}) as run:
            result = desktops.open_on_desktop(43, "TextEdit", [])
            created = result["created_document"]
            self.assertTrue(Path(created).is_file())
            self.assertEqual("", Path(created).read_text())
        self.assertEqual(["open", "43", "TextEdit", created], run.call_args.args[0])

    def test_textedit_with_a_document_opens_it(self) -> None:
        with patch.object(desktops, "_run", return_value={"ok": True}) as run:
            result = desktops.open_on_desktop(43, "com.apple.TextEdit", ["/tmp/a.txt"])
        self.assertNotIn("created_document", result)
        self.assertEqual(["open", "43", "com.apple.TextEdit", "/tmp/a.txt"], run.call_args.args[0])

    def test_non_chrome_apps_use_the_helper_open(self) -> None:
        with patch.object(desktops, "_run", return_value={"ok": True}) as run:
            desktops.open_on_desktop(43, "com.apple.TextEdit", ["/tmp/a.txt"])
        run.assert_called_once_with(["open", "43", "com.apple.TextEdit", "/tmp/a.txt"], timeout_s=20.0)


class ChromeTests(unittest.TestCase):
    def _bridge(self, *, connected=True, window=None, error=None):
        bridge = MagicMock()
        bridge.is_connected.return_value = connected
        if error is not None:
            bridge.request_open_window.side_effect = error
        else:
            bridge.request_open_window.return_value = window or {"ok": True, "chrome_window_id": 5, "chrome_tab_id": 6}
        return bridge

    def _run_sequence(self, listings):
        """A fake helper: `spaces` answers come from `listings`, every other command succeeds."""
        calls = []
        answers = iter(listings)

        def run(args, timeout_s=15.0):
            calls.append(list(args))
            if args[0] == "spaces":
                return next(answers)
            return {"ok": True, "pids": [7]}
        return run, calls

    def test_chrome_window_comes_from_the_companion_while_assigned(self) -> None:
        run, calls = self._run_sequence([_listing(), _listing(chrome_windows=[77])])
        bridge = self._bridge()
        with patch.object(desktops, "_run", side_effect=run), \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", bridge):
            result = desktops.open_on_desktop(43, "Google Chrome", ["https://example.com"])
        self.assertTrue(result["ok"], result)
        self.assertEqual([{"window_id": 77, "app": "Google Chrome", "spaces": [43]}], result["windows"])
        self.assertFalse(result["focus_changed"])
        bridge.request_open_window.assert_called_once_with("https://example.com")
        self.assertIn(["assign", "43", desktops.CHROME_BUNDLE_ID], calls)
        self.assertEqual(["assign", "0", desktops.CHROME_BUNDLE_ID], calls[-1])

    def test_assignment_is_lifted_when_the_companion_fails(self) -> None:
        run, calls = self._run_sequence([_listing()])
        bridge = self._bridge(error=HTTPException(502, {"error": "chrome_windows_create_failed"}))
        with patch.object(desktops, "_run", side_effect=run), \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", bridge):
            result = desktops.open_on_desktop(43, "chrome", [])
        self.assertEqual("CHROME_OPEN_WINDOW_FAILED", result["reason_code"])
        self.assertEqual(["assign", "0", desktops.CHROME_BUNDLE_ID], calls[-1])

    def test_open_chrome_without_companion_refuses_instead_of_stealing_focus(self) -> None:
        run, calls = self._run_sequence([_listing()])
        with patch.object(desktops, "_run", side_effect=run), \
             patch.object(desktops, "_chrome_running", return_value=True), \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", self._bridge(connected=False)):
            result = desktops.open_on_desktop(43, "com.google.Chrome", [])
        self.assertEqual("CHROME_COMPANION_REQUIRED", result["reason_code"])
        self.assertNotIn("assign", [call[0] for call in calls])

    def test_closed_chrome_starts_without_a_window(self) -> None:
        run, _ = self._run_sequence([_listing(), _listing(chrome_windows=[77])])
        bridge = self._bridge()
        bridge.is_connected.side_effect = [False, True, True]
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(desktops, "_run", side_effect=run), \
             patch.object(desktops, "_chrome_running", return_value=False), \
             patch("mcp_server.desktops.subprocess.run", return_value=completed) as launch, \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", bridge):
            result = desktops.open_on_desktop(43, "Google Chrome", [])
        self.assertTrue(result["ok"], result)
        self.assertIn("--no-startup-window", launch.call_args.args[0])
        self.assertIn("-g", launch.call_args.args[0])

    def test_chrome_refuses_non_web_addresses_and_unknown_desktops(self) -> None:
        self.assertEqual("INVALID_ARGUMENT", desktops.open_on_desktop(43, "Google Chrome", ["file:///etc/hosts"])["reason_code"])
        with patch.object(desktops, "_run", return_value=_listing()):
            self.assertEqual("SPACE_NOT_FOUND", desktops.open_on_desktop(99, "Google Chrome", [])["reason_code"])


class SwitchSettingNoteTests(unittest.TestCase):
    def test_listing_explains_the_desktop_switching_setting(self) -> None:
        for switching, expected in ((True, desktops.SWITCH_SETTING_ON_NOTE), (False, desktops.SWITCH_SETTING_OFF_NOTE)):
            with patch.object(desktops, "_run", return_value={**_listing(), "switches_to_app_desktop": switching}):
                self.assertEqual(expected, desktops.list_desktops()["note"])
        self.assertIn("Google Chrome", desktops.SWITCH_SETTING_ON_NOTE)
        self.assertIn("Command-Tab", desktops.SWITCH_SETTING_OFF_NOTE)


class ChromeCatchTests(unittest.TestCase):
    """The owner's own Chrome windows are elsewhere: never assign Chrome, catch its new window."""

    def _owner_chrome_listing(self):
        listing = _listing()
        listing["displays"][0]["spaces"][0]["windows"] = [{"window_id": 5, "app": "Google Chrome"}]
        return listing

    def _process(self, first_line, final=None):
        proc = MagicMock()
        proc.stdout.readline.return_value = first_line
        proc.communicate.return_value = (json.dumps(final or {}), None)
        return proc

    def test_new_window_is_caught_without_assigning_chrome(self) -> None:
        bridge = MagicMock()
        bridge.is_connected.return_value = True
        bridge.request_open_window.return_value = {"ok": True, "chrome_window_id": 9, "chrome_tab_id": 10}
        final = {"ok": True, "method": "caught_and_moved", "windows": [{"window_id": 88, "spaces": [43]}]}
        proc = self._process('{"ready":true}\n', final)
        runs = []
        with patch.object(desktops, "_run", side_effect=lambda args, timeout_s=15.0: runs.append(args) or self._owner_chrome_listing()), \
             patch.object(desktops, "_helper", return_value="/bin/spaces-helper"), \
             patch("mcp_server.desktops.subprocess.Popen", return_value=proc) as popen, \
             patch("mcp_server.desktops.select.select", return_value=([proc.stdout], [], [])), \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", bridge):
            result = desktops.open_on_desktop(43, "Google Chrome", ["https://example.com"])
        self.assertTrue(result["ok"], result)
        self.assertEqual("caught_and_moved", result["method"])
        self.assertEqual((9, 10), (result["chrome_window_id"], result["chrome_tab_id"]))
        self.assertEqual(["/bin/spaces-helper", "catch", "43", desktops.CHROME_BUNDLE_ID, "8"], popen.call_args.args[0])
        self.assertNotIn("assign", [args[0] for args in runs])
        bridge.request_open_window.assert_called_once_with("https://example.com")

    def test_helper_refusal_stops_before_chrome_opens_anything(self) -> None:
        bridge = MagicMock()
        bridge.is_connected.return_value = True
        refusal = {"ok": False, "reason_code": "APP_IN_FRONT_WOULD_SWITCH_DESKTOP", "switches_to_app_desktop": True}
        proc = self._process(json.dumps(refusal) + "\n")
        with patch.object(desktops, "_run", return_value=self._owner_chrome_listing()), \
             patch.object(desktops, "_helper", return_value="/bin/spaces-helper"), \
             patch("mcp_server.desktops.subprocess.Popen", return_value=proc), \
             patch("mcp_server.desktops.select.select", return_value=([proc.stdout], [], [])), \
             patch("mcp_server.chrome_background_bridge.chrome_background_bridge", bridge):
            result = desktops.open_on_desktop(43, "chrome", [])
        self.assertEqual("APP_IN_FRONT_WOULD_SWITCH_DESKTOP", result["reason_code"])
        bridge.request_open_window.assert_not_called()


class BridgeTests(unittest.TestCase):
    def test_open_window_is_an_expected_companion_feature(self) -> None:
        self.assertIn("open_window", EXPECTED_COMPANION_FEATURES)

    def test_open_window_needs_an_updated_companion(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(MagicMock(), MagicMock(), ["dialogs"], "2.2.0")
        with self.assertRaises(HTTPException) as caught:
            bridge.request_open_window("https://example.com")
        self.assertEqual("chrome_companion_update_required", caught.exception.detail["error"])

    def test_open_window_sends_the_address(self) -> None:
        bridge = ChromeBackgroundBridge()
        bridge._attach(MagicMock(), MagicMock(), ["open_window"], "2.2.0")
        with patch.object(bridge, "_request", return_value={"ok": True}) as request:
            bridge.request_open_window(None)
        request.assert_called_once_with("open_window", {"url": ""}, timeout_s=6.0)


if __name__ == "__main__":
    unittest.main()
