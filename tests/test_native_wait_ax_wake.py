"""#137: a native wait_until wakes on Accessibility notifications, with polling as the fallback."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp_server import ax_watch, computer_plan
from mcp_server.computer_plan import execute_computer_plan
from tests.test_ax_watch import FakeHelper, reset_state

READY = {"element_id": "w1/1", "role": "AXButton", "title": "Allow", "identifier": "allow-button"}


def native_wait(poll_ms: int, tool: str = "mac_observe") -> list[dict]:
    arguments = {"app": "DemoApp"} if tool == "mac_observe" else {"browser": "Safari", "tab_handle": "tab-a"}
    return [{
        "id": "wait_native", "type": "wait_until", "tool": tool, "arguments": arguments,
        "target": {"identifier": "allow-button", "role": "AXButton", "title": "Allow"},
        "timeout_s": 5.0, "poll_ms": poll_ms,
    }]


def observer(ready_on_read: int, *, pid: int = 77):
    reads: list[float] = []

    async def caller(tool: str, args: dict):
        reads.append(time.monotonic())
        nodes = [READY] if len(reads) >= ready_on_read else []
        return {"ok": True, "observation_id": f"o{len(reads)}", "app_pid": pid, "nodes": nodes}

    return caller, reads


class NativeWaitWakeTests(unittest.TestCase):
    def test_a_reported_change_wakes_the_wait_long_before_the_poll_interval(self) -> None:
        caller, reads = observer(ready_on_read=3)
        replies = iter([{"gen": 4, "changed": True}, {"gen": 5, "changed": True}])
        with patch.object(computer_plan.ax_watch, "wait_for_change", side_effect=lambda *a: next(replies)) as wait:
            started = time.monotonic()
            result = asyncio.run(execute_computer_plan(caller, plan_version=2, steps=native_wait(poll_ms=2000)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(3, len(reads))
        # Polling every 2 s would have taken 4 s; two wakes take at most two short gaps.
        self.assertLess(time.monotonic() - started, 1.0)
        # The first wait registers and closes the gap since the first read; later waits pass the last gen.
        self.assertEqual([(77, None), (77, 4)], [call.args[:2] for call in wait.call_args_list])
        self.assertEqual({"woken": 2, "fallback_reads": 0}, result["steps"][0]["ax_wake"])

    def test_reads_woken_by_a_busy_app_are_spaced_like_default_polling(self) -> None:
        caller, reads = observer(ready_on_read=4)
        with patch.object(computer_plan.ax_watch, "wait_for_change", return_value={"gen": 1, "changed": True}):
            result = asyncio.run(execute_computer_plan(caller, plan_version=2, steps=native_wait(poll_ms=150)))
        self.assertTrue(result["ok"], result)
        gaps = [later - earlier for earlier, later in zip(reads, reads[1:])]
        self.assertTrue(all(gap >= computer_plan._AX_WAKE_MIN_GAP_S - 0.01 for gap in gaps), gaps)

    def test_a_missed_notification_still_resolves_by_the_fallback_read(self) -> None:
        caller, reads = observer(ready_on_read=3)
        with patch.object(computer_plan.ax_watch, "wait_for_change",
                          return_value={"gen": 1, "changed": False}) as wait:
            result = asyncio.run(execute_computer_plan(caller, plan_version=2, steps=native_wait(poll_ms=150)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(3, len(reads))
        self.assertEqual({"woken": 0, "fallback_reads": 2}, result["steps"][0]["ax_wake"])
        # Without a notification it reads again after max(poll_ms, the fallback interval), not never.
        self.assertEqual(computer_plan._AX_WAKE_FALLBACK_S, wait.call_args.args[2])

    def test_without_a_watcher_it_polls_every_poll_ms_as_before(self) -> None:
        caller, reads = observer(ready_on_read=3)
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(seconds: float):
            sleeps.append(seconds)
            await real_sleep(0)

        with patch.object(computer_plan.ax_watch, "wait_for_change", return_value=None), \
             patch.object(computer_plan.asyncio, "sleep", side_effect=recording_sleep):
            result = asyncio.run(execute_computer_plan(caller, plan_version=2, steps=native_wait(poll_ms=150)))
        self.assertTrue(result["ok"], result)
        self.assertNotIn("ax_wake", result["steps"][0])
        self.assertEqual(2, len(sleeps))
        self.assertTrue(all(abs(value - 0.15) < 0.01 for value in sleeps), sleeps)

    def test_reads_without_an_app_pid_and_browser_waits_never_use_the_watcher(self) -> None:
        caller, _ = observer(ready_on_read=2, pid=0)
        with patch.object(computer_plan.ax_watch, "wait_for_change") as wait:
            self.assertTrue(asyncio.run(execute_computer_plan(
                caller, plan_version=2, steps=native_wait(poll_ms=20)))["ok"])

            async def browser(tool: str, args: dict):
                browser.count += 1
                elements = [{"element_id": "e1", "role": "AXButton", "text": "Allow", "identifier": "allow-button"}]
                return {"ok": True, "observation_id": "b", "app_pid": 99, "elements": elements if browser.count > 1 else []}

            browser.count = 0
            self.assertTrue(asyncio.run(execute_computer_plan(
                browser, plan_version=2, steps=native_wait(poll_ms=20, tool="browser_observe")))["ok"])
        wait.assert_not_called()


class WaitForChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-watch")

    def tearDown(self) -> None:
        reset_state()

    def run_wait(self, helper: FakeHelper, since, readable: bool = True):
        with patch.object(ax_watch.subprocess, "Popen", return_value=helper) as popen, \
             patch.object(ax_watch.select, "select", return_value=([helper.stdout] if readable else [], [], [])) as sel:
            return ax_watch.wait_for_change(42, since, 0.5), popen, sel

    def test_wait_requests_go_to_their_own_helper_and_carry_since(self) -> None:
        helper = FakeHelper({"ok": True, "watching": True, "session": "s", "gen": 3, "changed": True},
                            {"ok": True, "watching": True, "session": "s", "gen": 3, "changed": False})
        with patch.object(ax_watch.subprocess, "Popen", return_value=helper) as popen, \
             patch.object(ax_watch.select, "select", return_value=([helper.stdout], [], [])) as sel:
            first = ax_watch.wait_for_change(42, None, 0.5)
            second = ax_watch.wait_for_change(42, 3, 0.5)
        self.assertEqual({"gen": 3, "changed": True}, first)
        self.assertEqual({"gen": 3, "changed": False}, second)
        self.assertEqual([{"op": "wait", "pid": 42, "timeout_ms": 500},
                          {"op": "wait", "pid": 42, "timeout_ms": 500, "since": 3}], helper.requests)
        popen.assert_called_once()
        # The reply may take the whole wait plus the usual reply margin.
        self.assertAlmostEqual(0.5 + ax_watch._REPLY_TIMEOUT_S, sel.call_args.args[3])
        # change_token keeps its own helper, so a wait never holds it up.
        self.assertIsNot(ax_watch._wait_proc, ax_watch._proc)

    def test_unwatched_app_or_busy_waiter_means_poll_instead(self) -> None:
        result, _, _ = self.run_wait(FakeHelper({"ok": True, "watching": False, "reason": "notification_refused"}), 1)
        self.assertIsNone(result)
        with ax_watch._wait_lock, patch.object(ax_watch.subprocess, "Popen") as popen:
            self.assertIsNone(ax_watch.wait_for_change(42, 1, 0.5))
        popen.assert_not_called()

    def test_a_silent_helper_is_killed_and_counts_as_a_failure(self) -> None:
        helper = FakeHelper(None)
        result, _, _ = self.run_wait(helper, 1, readable=False)
        self.assertIsNone(result)
        self.assertTrue(helper.killed)
        self.assertIsNone(ax_watch._wait_proc)
        self.assertEqual(1, ax_watch._state["failures"])

    def test_disabled_watcher_never_starts_a_wait_helper(self) -> None:
        reset_state(allowed=False)
        with patch.object(ax_watch.subprocess, "Popen") as popen:
            self.assertIsNone(ax_watch.wait_for_change(42, None, 0.5))
            self.assertIsNone(ax_watch.wait_for_change(0, None, 0.5))
        popen.assert_not_called()


@unittest.skipUnless(shutil.which("swiftc"), "macOS Swift toolchain required")
class RealWaitHelperTests(unittest.TestCase):
    def test_wait_times_out_without_a_change_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"MAC_MCP_AX_WATCH_CACHE": td}):
            reset_state(allowed=True)
            try:
                ax_watch.change_token(os.getpid())
                deadline = time.time() + 240
                while ax_watch._state["executable"] is None and not ax_watch._state["disabled"] and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(ax_watch._state["disabled"], "the helper failed to build")
                first = ax_watch.wait_for_change(os.getpid(), None, 0.2)
                if first is None:
                    return  # no Accessibility trust, or this process refuses notifications: callers poll
                self.assertTrue(first["changed"])
                started = time.monotonic()
                quiet = ax_watch.wait_for_change(os.getpid(), first["gen"], 0.2)
                self.assertEqual({"gen": first["gen"], "changed": False}, quiet)
                self.assertGreaterEqual(time.monotonic() - started, 0.15)
            finally:
                reset_state()


if __name__ == "__main__":
    unittest.main()
