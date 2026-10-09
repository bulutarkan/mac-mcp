from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import ax_watch


def reset_state(**overrides) -> None:
    ax_watch.shutdown()
    ax_watch._state.update({"allowed": False, "executable": None, "building": False, "disabled": False, "failures": 0})
    ax_watch._state.update(overrides)


class GateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_state()

    def test_off_unless_the_managed_server_enables_it(self) -> None:
        reset_state()
        with patch.object(ax_watch.threading, "Thread") as thread:
            self.assertIsNone(ax_watch.change_token(123))
        thread.assert_not_called()

    def test_kill_switch_wins_over_enable(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-watch")
        with patch.dict(os.environ, {"MAC_MCP_AX_WATCH": "0"}), patch.object(ax_watch.subprocess, "Popen") as popen:
            self.assertIsNone(ax_watch.change_token(123))
        popen.assert_not_called()

    def test_first_enabled_call_builds_once_in_the_background(self) -> None:
        reset_state(allowed=True)
        with patch.object(ax_watch.threading, "Thread") as thread:
            for _ in range(3):
                self.assertIsNone(ax_watch.change_token(123))
        thread.assert_called_once()

    def test_missing_pid_never_starts_the_helper(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-watch")
        with patch.object(ax_watch.subprocess, "Popen") as popen:
            self.assertIsNone(ax_watch.change_token(None))
            self.assertIsNone(ax_watch.change_token(0))
        popen.assert_not_called()


class FakeHelper:
    """A Popen stand-in whose stdout replies with the queued JSON lines."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.stdin = MagicMock()
        self.stdin.write.side_effect = lambda data: self.requests.append(json.loads(data))
        self.stdout = MagicMock()
        self.stdout.readline.side_effect = self._readline
        self.killed = False

    def _readline(self) -> bytes:
        reply = self.replies.pop(0)
        return b"" if reply is None else (json.dumps(reply) + "\n").encode()

    def poll(self):
        return 0 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        return 0


class ReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state(allowed=True, executable="/fake/ax-watch")

    def tearDown(self) -> None:
        reset_state()

    def run_with(self, helper: FakeHelper, pid: int = 42, readable: bool = True):
        with patch.object(ax_watch.subprocess, "Popen", return_value=helper) as popen, \
             patch.object(ax_watch.select, "select", return_value=([helper.stdout] if readable else [], [], [])):
            return ax_watch.change_token(pid), popen

    def test_token_combines_session_and_generation(self) -> None:
        helper = FakeHelper({"ok": True, "watching": True, "session": "abc", "gen": 5})
        token, _ = self.run_with(helper)
        self.assertEqual("abc:5", token)
        self.assertEqual([{"op": "watch", "pid": 42}], helper.requests)

    def test_unwatched_app_has_no_token(self) -> None:
        token, _ = self.run_with(FakeHelper({"ok": True, "watching": False, "reason": "notification_refused"}))
        self.assertIsNone(token)

    def test_one_helper_serves_many_requests(self) -> None:
        helper = FakeHelper(*[{"ok": True, "watching": True, "session": "s", "gen": n} for n in (1, 1, 2)])
        tokens = []
        with patch.object(ax_watch.subprocess, "Popen", return_value=helper) as popen, \
             patch.object(ax_watch.select, "select", return_value=([helper.stdout], [], [])):
            for _ in range(3):
                tokens.append(ax_watch.change_token(42))
        popen.assert_called_once()
        self.assertEqual(["s:1", "s:1", "s:2"], tokens)

    def test_slow_or_dead_helper_is_killed_and_eventually_disabled(self) -> None:
        for attempt in range(ax_watch._FAILURE_LIMIT):
            helper = FakeHelper(None)
            token, _ = self.run_with(helper, readable=attempt % 2 == 0)
            self.assertIsNone(token)
            self.assertTrue(helper.killed)
        self.assertTrue(ax_watch._state["disabled"])
        with patch.object(ax_watch.subprocess, "Popen") as popen:
            self.assertIsNone(ax_watch.change_token(42))
        popen.assert_not_called()


@unittest.skipUnless(shutil.which("swiftc"), "macOS Swift toolchain required")
class RealHelperTests(unittest.TestCase):
    def test_helper_compiles_and_answers_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"MAC_MCP_AX_WATCH_CACHE": td}):
            reset_state(allowed=True)
            try:
                ax_watch.change_token(os.getpid())
                deadline = time.time() + 240
                while ax_watch._state["executable"] is None and not ax_watch._state["disabled"] and time.time() < deadline:
                    time.sleep(0.2)
                self.assertFalse(ax_watch._state["disabled"], "the helper failed to build")
                # Without Accessibility trust the helper exits and the caller walks the tree.
                token = ax_watch.change_token(os.getpid())
                self.assertTrue(token is None or ":" in token)
                if token is not None:
                    self.assertEqual(token, ax_watch.change_token(os.getpid()))
            finally:
                reset_state()


if __name__ == "__main__":
    unittest.main()
