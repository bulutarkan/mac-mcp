from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import ax_native, tools_ui

FS, RS = "\x1f", "\x1e"


def reset_state(**overrides) -> None:
    ax_native._state.update({"allowed": False, "executable": None, "building": False, "disabled": False})
    ax_native._state.update(overrides)


def records(*rows) -> str:
    return RS.join(FS.join(row) for row in rows)


META = ["__META__", "Demo", "true", "1", "Main", "42", "com.example.demo"]
WINDOW = ["__WINDOW__", "1", "Main", "missing value", "missing value", "0", "25", "800", "600",
          "AXStandardWindow", "true", "true"]
NODE = ["__NODE__", "w1", "", "AXWindow", "missing value", "Main", "missing value", "missing value",
        "0", "25", "800", "600", "true", "true", "AXRaise", "0", "missing value"]


class ParserNormalizationTests(unittest.TestCase):
    def test_missing_value_and_empty_text_parse_the_same(self) -> None:
        empty = lambda row: ["" if cell == "missing value" else cell for cell in row]  # noqa: E731
        from_applescript = tools_ui._parse_observation(records(META, WINDOW, NODE))
        from_native = tools_ui._parse_observation(records(META, empty(WINDOW), empty(NODE)))
        self.assertEqual(from_applescript, from_native)
        window = from_native[0]["windows"][0]
        self.assertEqual("", window.get("document", ""))
        self.assertEqual("", from_native[1][0]["subrole"])


class GateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_state()

    def observe(self):
        return ax_native.observe(app="Demo", app_pid=None, window_index=0, max_depth=2,
                                 max_children=5, max_nodes=50, timeout_s=5)

    def test_off_unless_the_managed_server_enables_it(self) -> None:
        reset_state()
        with patch.object(ax_native.threading, "Thread") as thread:
            self.assertIsNone(self.observe())
        thread.assert_not_called()

    def test_kill_switch_wins_over_enable(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-observe")
        with patch.dict(os.environ, {"MAC_MCP_AX_NATIVE": "0"}), patch.object(ax_native.subprocess, "Popen") as popen:
            self.assertIsNone(self.observe())
        popen.assert_not_called()

    def test_first_enabled_call_builds_once_in_the_background_and_falls_back(self) -> None:
        reset_state(allowed=True)
        with patch.object(ax_native.threading, "Thread") as thread:
            for _ in range(3):
                self.assertIsNone(self.observe())
        thread.assert_called_once()


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-observe")

    def tearDown(self) -> None:
        reset_state()

    def run_with(self, returncode: int, stdout: bytes = b"", **kwargs):
        proc = MagicMock(returncode=returncode)
        proc.communicate.return_value = (stdout, b"")
        args = {"app": None, "app_pid": None, "window_index": 2, "max_depth": 3, "max_children": 7,
                "max_nodes": 90, "timeout_s": 5}
        args.update(kwargs)
        with patch.object(ax_native.subprocess, "Popen", return_value=proc) as popen:
            result = ax_native.observe(**args)
        return result, popen.call_args.args[0]

    def test_target_and_limits_reach_the_helper(self) -> None:
        _, command = self.run_with(0, app_pid=123, app="Ignored")
        self.assertEqual(["/fake/ax-observe", "--pid", "123", "--window", "2", "--max-depth", "3",
                          "--max-children", "7", "--max-nodes", "90"], command)
        _, command = self.run_with(0, app="Finder")
        self.assertEqual(["--name", "Finder"], command[1:3])
        _, command = self.run_with(0)
        self.assertEqual("--frontmost", command[1])

    def test_success_returns_records(self) -> None:
        result, _ = self.run_with(0, b"__META__")
        self.assertEqual((True, "__META__", ""), result)

    def test_untrusted_missing_app_or_crash_fall_back_to_applescript(self) -> None:
        for code in (3, 4, 1, -11):
            result, _ = self.run_with(code)
            self.assertIsNone(result, code)

    def test_timeout_is_an_error_not_a_fallback(self) -> None:
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("ax", 1)
        with patch.object(ax_native.subprocess, "Popen", return_value=proc):
            ok, _, error = ax_native.observe(app="Demo", app_pid=None, window_index=0, max_depth=1,
                                             max_children=1, max_nodes=1, timeout_s=1)
        self.assertFalse(ok)
        self.assertIn("timed out", error)
        proc.kill.assert_called_once()


class ObserverRoutingTests(unittest.TestCase):
    def test_native_result_skips_applescript(self) -> None:
        with patch.object(tools_ui.ax_native, "observe", return_value=(True, "raw", "")), \
             patch.object(tools_ui, "_run_osascript") as osascript:
            self.assertEqual((True, "raw", "", "ax_native"), tools_ui._read_native_tree("Demo", 0, 2, 5))
        osascript.assert_not_called()

    def test_unavailable_native_uses_applescript(self) -> None:
        with patch.object(tools_ui.ax_native, "observe", return_value=None), \
             patch.object(tools_ui, "_run_osascript", return_value=(True, "raw", "")) as osascript:
            self.assertEqual((True, "raw", "", "applescript"), tools_ui._read_native_tree("Demo", 0, 2, 5))
        osascript.assert_called_once()

    def test_fingerprint_probe_uses_native_records(self) -> None:
        raw = records(META, WINDOW, NODE)
        with patch.object(tools_ui.ax_native, "observe", return_value=(True, raw, "")), \
             patch.object(tools_ui, "_run_osascript") as osascript:
            native, error = tools_ui._probe_native_observation_fingerprint("Demo", 0)
        osascript.assert_not_called()
        self.assertIsNone(error)
        with patch.object(tools_ui.ax_native, "observe", return_value=None), \
             patch.object(tools_ui, "_run_osascript", return_value=(True, raw, "")):
            fallback, _ = tools_ui._probe_native_observation_fingerprint("Demo", 0)
        self.assertEqual(native, fallback)


@unittest.skipUnless(shutil.which("swiftc"), "macOS Swift toolchain required")
class RealBuildTests(unittest.TestCase):
    def test_helper_compiles_and_reports_its_target_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"MAC_MCP_AX_NATIVE_CACHE": td}):
            reset_state(allowed=True)
            try:
                ax_native._executable()
                deadline = time.time() + 240
                while ax_native._state["executable"] is None and not ax_native._state["disabled"] and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(ax_native._state["disabled"], "the helper failed to build")
                proc = subprocess.run([ax_native._state["executable"], "--pid", "999999"], capture_output=True)
                # Not trusted (3) in CI, otherwise no such app (4); both mean "fall back".
                self.assertIn(proc.returncode, {3, 4})
            finally:
                reset_state()


if __name__ == "__main__":
    unittest.main()
