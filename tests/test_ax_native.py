from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
    """The one-shot helper (one process per observation), which the worker falls back to."""

    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-observe")
        one_shot = patch.dict(os.environ, {"MAC_MCP_AX_WORKER": "0"})
        one_shot.start()
        self.addCleanup(one_shot.stop)

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


FAKE_HELPER = r'''
import json, os, sys, time
args = sys.argv[1:]
if args[:1] != ["--serve"]:
    sys.stdout.write("ONESHOT|" + " ".join(args))
    sys.exit(int(os.environ.get("FAKE_ONESHOT_CODE", "0")))
count = 0
for line in sys.stdin:
    count += 1
    request = json.loads(line)
    if str(count) == os.environ.get("FAKE_CRASH_ON"):
        sys.exit(1)
    if str(count) == os.environ.get("FAKE_HANG_ON"):
        time.sleep(30)
    if os.environ.get("FAKE_GARBLE"):
        sys.stdout.write("not json\n"); sys.stdout.flush(); continue
    code = int(os.environ.get("FAKE_CODE", "0"))
    out = "" if code else "WORKER|%d|%s" % (os.getpid(), " ".join(request["args"]))
    sys.stdout.write(json.dumps({"id": request["id"], "code": code, "out": out, "err": ""}) + "\n")
    sys.stdout.flush()
'''


class WorkerTests(unittest.TestCase):
    """#185: one long-lived helper answers observations; the one-shot helper stays the fallback."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        helper = os.path.join(self.dir, "fake-ax-observe")
        with open(helper, "w") as handle:
            handle.write(f"#!{sys.executable}\n{FAKE_HELPER}")
        os.chmod(helper, 0o755)
        reset_state(allowed=True, executable=helper)
        self.env = patch.dict(os.environ, {"MAC_MCP_AX_WORKER": "1"})
        self.env.start()
        self.spawned = []
        real_popen = subprocess.Popen

        def counting(command, *args, **kwargs):
            self.spawned.append(list(command))
            return real_popen(command, *args, **kwargs)

        self.popen = patch.object(ax_native.subprocess, "Popen", side_effect=counting)
        self.popen.start()

    def tearDown(self) -> None:
        self.popen.stop()
        ax_native._stop_worker()
        ax_native._worker = None
        self.env.stop()
        reset_state()
        shutil.rmtree(self.dir, ignore_errors=True)

    def observe(self, timeout_s: float = 5, **kwargs):
        args = {"app": "Demo", "app_pid": None, "window_index": 1, "max_depth": 3, "max_children": 7,
                "max_nodes": 90, "timeout_s": timeout_s}
        args.update(kwargs)
        return ax_native.observe(**args)

    def serve_spawns(self) -> int:
        return sum(1 for command in self.spawned if command[1:] == ["--serve"])

    def test_one_helper_answers_many_observations_with_the_same_records(self) -> None:
        first = self.observe()
        second = self.observe(app_pid=7)
        self.assertEqual(1, self.serve_spawns())
        self.assertTrue(first[0] and second[0])
        self.assertEqual(first[1].split("|")[1], second[1].split("|")[1], "the same process answered")
        self.assertEqual("--name Demo --window 1 --max-depth 3 --max-children 7 --max-nodes 90", first[1].split("|")[2])
        self.assertEqual("--pid 7", second[1].split("|")[2][:7])
        self.assertEqual("", first[2])

    def test_helper_exit_codes_mean_the_same_as_before(self) -> None:
        with patch.dict(os.environ, {"FAKE_CODE": "4"}):
            self.assertIsNone(self.observe())
        ax_native._stop_worker()
        with patch.dict(os.environ, {"FAKE_CODE": "3"}):
            self.assertIsNone(self.observe())

    def test_a_crashed_helper_falls_back_for_that_call_and_is_replaced(self) -> None:
        with patch.dict(os.environ, {"FAKE_CRASH_ON": "1"}):
            ok, raw, _ = self.observe()
        self.assertTrue(ok)
        self.assertTrue(raw.startswith("ONESHOT|"), raw)
        ok, raw, _ = self.observe()
        self.assertTrue(raw.startswith("WORKER|"), raw)
        self.assertEqual(2, self.serve_spawns())

    def test_a_garbled_answer_falls_back_and_replaces_the_helper(self) -> None:
        with patch.dict(os.environ, {"FAKE_GARBLE": "1"}):
            ok, raw, _ = self.observe()
        self.assertTrue(raw.startswith("ONESHOT|"), raw)
        self.assertIsNone(ax_native._worker.proc)

    def test_a_hung_helper_times_out_is_killed_and_the_next_call_gets_a_new_one(self) -> None:
        with patch.dict(os.environ, {"FAKE_HANG_ON": "1"}):
            started = time.monotonic()
            ok, _, error = self.observe(timeout_s=0.6)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(ok)
        self.assertIn("timed out", error)
        self.assertIsNone(ax_native._worker.proc)
        ok, raw, _ = self.observe()
        self.assertTrue(raw.startswith("WORKER|"), raw)
        self.assertEqual(2, self.serve_spawns())

    def test_an_idle_helper_exits_and_the_next_call_starts_one(self) -> None:
        with patch.object(ax_native._Worker, "IDLE_S", 0.2):
            self.observe()
            proc = ax_native._worker.proc
            deadline = time.monotonic() + 5
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), "the idle helper is still running")
            self.assertIsNone(ax_native._worker.proc)
            self.assertTrue(self.observe()[1].startswith("WORKER|"))
        self.assertEqual(2, self.serve_spawns())

    def test_a_call_that_finds_the_helper_busy_runs_its_own(self) -> None:
        self.observe()
        with ax_native._worker.busy:
            ok, raw, _ = self.observe()
        self.assertTrue(raw.startswith("ONESHOT|"), raw)

    def test_worker_switch_off_keeps_one_process_per_call(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_AX_WORKER": "0"}):
            self.assertTrue(self.observe()[1].startswith("ONESHOT|"))
        self.assertEqual(0, self.serve_spawns())

    def test_cancelling_a_hung_observation_stops_it_without_a_fallback_run(self) -> None:
        from mcp_server.tool_cancellation import (
            ToolCancellationContext, ToolCancelledError, reset_tool_cancellation, set_tool_cancellation,
        )
        context = ToolCancellationContext()
        token = set_tool_cancellation(context)
        timer = threading.Timer(0.5, context.cancel)
        timer.start()
        try:
            with patch.dict(os.environ, {"FAKE_HANG_ON": "1"}), self.assertRaises(ToolCancelledError):
                self.observe(timeout_s=10)
        finally:
            timer.cancel()
            reset_tool_cancellation(token)
        self.assertEqual([], [command for command in self.spawned if command[1:] != ["--serve"]])


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
                # --serve answers the same, one JSON line per request, and exits when stdin closes.
                serve = subprocess.run(
                    [ax_native._state["executable"], "--serve"], capture_output=True, timeout=30,
                    input=b'{"id": 1, "args": ["--pid", "999999"]}\nnot json\n{"id": 2, "args": ["--pid", "999999"]}\n',
                )
                answers = [json.loads(line) for line in serve.stdout.decode().splitlines()]
                self.assertEqual(0, serve.returncode)
                self.assertEqual([1, None, 2], [answer["id"] for answer in answers])
                self.assertEqual([proc.returncode, 2, proc.returncode], [answer["code"] for answer in answers])
            finally:
                reset_state()


if __name__ == "__main__":
    unittest.main()
