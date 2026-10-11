from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import applescript_host as host
from mcp_server import tools_browser


def reset_state(**overrides) -> None:
    host.shutdown()
    host._state.update({"executable": None, "building": False, "disabled": False, "failures": 0, "allowed": False})
    host._state.update(overrides)
    host._live = 0


class HostGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_state()

    def test_off_unless_the_managed_server_enables_it(self) -> None:
        reset_state()
        with patch.object(host.threading, "Thread") as thread:
            with self.assertRaises(host.HostUnavailable):
                host.run("return 1", 5)
        thread.assert_not_called()

    def test_kill_switch_wins_over_enable(self) -> None:
        reset_state(allowed=True)
        with patch.dict(os.environ, {"MAC_MCP_APPLESCRIPT_HOST": "0"}), \
             patch.object(host.threading, "Thread") as thread:
            with self.assertRaises(host.HostUnavailable):
                host.run("return 1", 5)
        thread.assert_not_called()

    def test_first_enabled_call_starts_one_background_build_and_falls_back(self) -> None:
        reset_state(allowed=True)
        with patch.object(host.threading, "Thread") as thread:
            for _ in range(3):
                with self.assertRaises(host.HostUnavailable):
                    host.run("return 1", 5)
        thread.assert_called_once()


class FakeHost:
    def __init__(self, reply=None, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.proc = MagicMock()
        self.proc.poll.return_value = None
        self.killed = False

    def request(self, script, timeout_s):
        if self.error:
            raise self.error
        return self.reply

    def kill(self):
        self.killed = True


class HostReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/host")

    def tearDown(self) -> None:
        reset_state()

    def run_with(self, fake: FakeHost):
        # Each case gets a fresh helper; the pool would otherwise reuse the last one.
        host._idle.clear()
        host._live = 0
        with patch.object(host, "_Host", return_value=fake):
            return host.run("script", 5)

    def test_replies_are_shaped_like_osascript(self) -> None:
        self.assertEqual((True, "a, b", ""), self.run_with(FakeHost({"ok": True, "result": "a, b"})))
        ok, _, err = self.run_with(FakeHost({"ok": False, "error": "MAC_MCP_TAB_TARGET_MISSING", "number": -2700}))
        self.assertEqual((False, "execution error: MAC_MCP_TAB_TARGET_MISSING (-2700)"), (ok, err))
        _, _, syntax = self.run_with(FakeHost({"ok": False, "error": "Expected end of line.", "number": -2741}))
        self.assertTrue(syntax.startswith("syntax error:") and syntax.endswith("(-2741)"))

    def test_timeout_kills_the_helper_and_frees_its_slot(self) -> None:
        fake = FakeHost(error=host.HostTimeout("AppleScript timed out after 1s"))
        with self.assertRaises(host.HostTimeout):
            self.run_with(fake)
        self.assertTrue(fake.killed)
        self.assertEqual(0, host._live)
        self.assertEqual([], host._idle)

    def test_a_crashed_helper_is_a_failure_not_a_silent_osascript_retry(self) -> None:
        for attempt in range(host._FAILURE_LIMIT):
            ok, _, err = self.run_with(FakeHost(error=EOFError("AppleScript host exited")))
            self.assertFalse(ok)
            self.assertIn("AppleScript host failed", err)
        self.assertTrue(host._state["disabled"], "repeated crashes switch the helper off")
        with self.assertRaises(host.HostUnavailable):
            host.run("return 1", 5)

    def test_a_healthy_helper_is_reused(self) -> None:
        fake = FakeHost({"ok": True, "result": "1"})
        with patch.object(host, "_Host", return_value=fake) as factory:
            host.run("a", 5)
            host.run("b", 5)
        factory.assert_called_once()


class IdleRetirementTests(unittest.TestCase):
    """#169: helpers left idle are stopped after a grace period; busy ones never are."""

    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/host")

    def tearDown(self) -> None:
        reset_state()

    def wait_for(self, predicate, timeout=3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_a_helper_idle_past_the_grace_period_is_stopped_and_frees_its_slot(self) -> None:
        fake = FakeHost({"ok": True, "result": "1"})
        with patch.object(host, "_idle_retire_s", return_value=0.2), patch.object(host, "_Host", return_value=fake):
            host.run("a", 5)
            self.assertEqual([fake], host._idle)
            self.assertTrue(self.wait_for(lambda: fake.killed))
        self.assertEqual([], host._idle)
        self.assertEqual(0, host._live)
        self.assertIsNone(host._reaper, "an empty pool keeps no timer")

    def test_a_helper_reused_within_the_grace_period_stays_warm(self) -> None:
        fake = FakeHost({"ok": True, "result": "1"})
        with patch.object(host, "_idle_retire_s", return_value=60), \
             patch.object(host, "_Host", return_value=fake) as factory:
            host.run("a", 5)
            host.run("b", 5)
        factory.assert_called_once()
        self.assertFalse(fake.killed)

    def test_only_expired_helpers_are_stopped_and_the_timer_rearms_for_the_rest(self) -> None:
        old, fresh = FakeHost(), FakeHost()
        now = time.monotonic()
        old.idle_since, fresh.idle_since = now - 100, now - 1
        host._idle[:] = [old, fresh]
        host._live = 2
        with patch.object(host, "_idle_retire_s", return_value=50):
            host._reap_idle()
            self.assertTrue(old.killed)
            self.assertFalse(fresh.killed)
            self.assertEqual([fresh], host._idle)
            self.assertEqual(1, host._live)
            self.assertIsNotNone(host._reaper, "the fresh helper still gets retired later")

    def test_a_busy_helper_is_never_retired_mid_call(self) -> None:
        class SlowHost(FakeHost):
            def request(self, script, timeout_s):
                time.sleep(0.4)
                return {"ok": True, "result": "done"}

        slow = SlowHost()
        with patch.object(host, "_idle_retire_s", return_value=0.1), patch.object(host, "_Host", return_value=slow):
            self.assertEqual((True, "done", ""), host.run("a", 5))
            self.assertFalse(slow.killed)
            # Back in the pool it ages from now, then retires.
            self.assertTrue(self.wait_for(lambda: slow.killed))

    def test_shutdown_cancels_the_timer(self) -> None:
        with patch.object(host, "_idle_retire_s", return_value=60), \
             patch.object(host, "_Host", return_value=FakeHost({"ok": True, "result": "1"})):
            host.run("a", 5)
        timer = host._reaper
        self.assertIsNotNone(timer)
        host.shutdown()
        self.assertIsNone(host._reaper)
        self.assertTrue(timer.finished.is_set())

    def test_grace_period_setting_is_bounded(self) -> None:
        for raw, expected in (("", 120.0), ("abc", 120.0), ("1", 5.0), ("600", 600.0), ("99999", 3600.0)):
            env = {"MAC_MCP_APPLESCRIPT_HOST_IDLE_S": raw} if raw else {}
            with self.subTest(raw=raw), patch.dict(os.environ, env, clear=False):
                if not raw:
                    os.environ.pop("MAC_MCP_APPLESCRIPT_HOST_IDLE_S", None)
                self.assertEqual(expected, host._idle_retire_s())


class BrowserBridgeFallbackTests(unittest.TestCase):
    def test_unavailable_host_uses_osascript(self) -> None:
        with patch.object(tools_browser.applescript_host, "run", side_effect=host.HostUnavailable("not ready")), \
             patch.object(tools_browser, "_run_osascript_process", return_value="via osascript") as process:
            self.assertEqual("via osascript", tools_browser._run_osascript("return 1", 5))
        process.assert_called_once()

    def test_host_errors_map_like_osascript_errors(self) -> None:
        missing = (False, "", "execution error: MAC_MCP_TAB_TARGET_MISSING (-2700)")
        with patch.object(tools_browser.applescript_host, "run", return_value=missing):
            with self.assertRaises(HTTPException) as ctx:
                tools_browser._run_osascript("x", 5)
        self.assertEqual("tab_target_closed", ctx.exception.detail["error"])
        with patch.object(tools_browser.applescript_host, "run", side_effect=host.HostTimeout("late")):
            with self.assertRaises(HTTPException) as ctx:
                tools_browser._run_osascript("x", 5)
        self.assertEqual(408, ctx.exception.status_code)


@unittest.skipUnless(shutil.which("swiftc") and shutil.which("osascript"), "macOS Swift toolchain required")
class RealHostTests(unittest.TestCase):
    def test_compiled_helper_matches_osascript_output(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"MAC_MCP_APPLESCRIPT_HOST_CACHE": td}):
            reset_state(allowed=True)
            try:
                with self.assertRaises(host.HostUnavailable):
                    host.run("return 1", 5)
                deadline = time.time() + 240
                while host._state["executable"] is None and not host._state["disabled"] and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(host._state["disabled"], "the helper failed to build")
                for script in ('return "héllo"', "return 1+1", 'return {"a", "b"}', "return missing value", "return true"):
                    expected = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()
                    self.assertEqual((True, expected, ""), host.run(script, 10), script)
                ok, _, err = host.run('error "boom"', 10)
                self.assertFalse(ok)
                self.assertIn("boom", err)
                with self.assertRaises(host.HostTimeout):
                    host.run("delay 5", 0.5)
                self.assertEqual(0, host._live)
            finally:
                reset_state()


if __name__ == "__main__":
    unittest.main()
