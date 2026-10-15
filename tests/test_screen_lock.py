"""Native UI tools say the screen is locked instead of failing with a misleading error."""
from __future__ import annotations

import platform
import unittest
from unittest.mock import patch

from mcp_server import screen_lock, tools_macos, tools_snapshot, tools_ui
from mcp_server.security import load_settings


class ScreenLockDetectionTests(unittest.TestCase):
    @unittest.skipUnless(platform.system() == "Darwin", "macOS session API")
    def test_reads_a_boolean_from_the_login_session(self) -> None:
        self.assertIn(screen_lock.is_screen_locked(), (True, False, None))

    def test_unknown_outside_macos(self) -> None:
        with patch.object(screen_lock.platform, "system", return_value="Linux"):
            self.assertIsNone(screen_lock.is_screen_locked())

    def test_error_names_the_tool_and_what_still_works(self) -> None:
        out = screen_lock.locked_error("mac_observe")
        self.assertFalse(out["ok"])
        self.assertEqual("SCREEN_LOCKED", out["reason_code"])
        self.assertFalse(out["retryable"])
        self.assertIn("mac_observe", out["error"])
        self.assertIn("browser", out["hint"])


class LockedToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        patcher = patch.object(screen_lock, "is_screen_locked", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_observe_does_not_read_the_tree(self) -> None:
        with patch.object(tools_ui, "_observe_ui") as observe:
            out = tools_ui.observe_ui(self.settings, app="TextEdit")
        observe.assert_not_called()
        self.assertEqual("SCREEN_LOCKED", out["reason_code"])

    def test_act_sends_no_input(self) -> None:
        with patch.object(tools_ui, "_get_observation") as stored:
            out = tools_ui.act_ui(self.settings, actions=[{"type": "type", "element_id": "w1/1", "text": "x"}],
                                  observation_id="obs_x", app="TextEdit")
        stored.assert_not_called()
        self.assertEqual("SCREEN_LOCKED", out["reason_code"])

    def test_screenshot_does_not_capture_the_lock_screen(self) -> None:
        with patch.object(tools_macos.privacy_policy, "check_tool"), \
                patch.object(tools_macos.subprocess, "run") as run:
            out = tools_macos.screenshot(self.settings, path="/tmp/never.png")
        run.assert_not_called()
        self.assertEqual("SCREEN_LOCKED", out["reason_code"])

    def test_snapshot_system_section_reports_the_lock(self) -> None:
        with patch.object(tools_snapshot, "get_system_info", return_value={"ok": True, "stdout": ""}):
            out = tools_snapshot._read_system(self.settings)
        self.assertTrue(out["screen_locked"])


class UnlockedToolsTests(unittest.TestCase):
    def test_unknown_lock_state_does_not_block_observe(self) -> None:
        settings = load_settings()
        with patch.object(screen_lock, "is_screen_locked", return_value=None), \
                patch.object(tools_ui, "_observe_ui", return_value={"ok": False, "error": "x"}) as observe:
            tools_ui.observe_ui(settings, app="TextEdit")
        observe.assert_called_once()

    def test_unknown_lock_state_is_left_out_of_the_snapshot(self) -> None:
        with patch.object(screen_lock, "is_screen_locked", return_value=None), \
                patch.object(tools_snapshot, "get_system_info", return_value={"ok": True, "stdout": ""}):
            out = tools_snapshot._read_system(load_settings())
        self.assertNotIn("screen_locked", out)


if __name__ == "__main__":
    unittest.main()
