"""#74: one tested contract for stopping a tool's child process group."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import process_supervision
from mcp_server.process_supervision import terminate_process_group

ROOT = Path(__file__).resolve().parents[1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); ps says whether it is really running.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


def start(script: str) -> subprocess.Popen:
    return subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)


class TerminateProcessGroupTests(unittest.TestCase):
    def test_an_exited_process_is_left_alone(self) -> None:
        proc = start("exit 0")
        proc.wait(5)
        with patch.object(process_supervision.os, "killpg") as killpg:
            terminate_process_group(proc)
        killpg.assert_not_called()

    def test_a_process_that_honours_sigterm_is_not_killed(self) -> None:
        proc = start("sleep 30")
        with patch.object(process_supervision.os, "killpg", wraps=os.killpg) as killpg:
            started = time.monotonic()
            terminate_process_group(proc, grace_s=2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual([signal.SIGTERM], [c.args[1] for c in killpg.call_args_list])
        self.assertIsNotNone(proc.poll())

    def test_a_process_ignoring_sigterm_is_killed_with_its_descendants(self) -> None:
        # The shell ignores TERM and starts a child that does too; both share the group.
        proc = start("trap '' TERM; (trap '' TERM; sleep 30) & echo $!; wait")
        child = int(proc.stdout.readline())
        terminate_process_group(proc, grace_s=0.3, reap_s=2)
        self.assertIsNotNone(proc.poll(), "the leader was killed and reaped")
        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child), "the descendant in the group was killed too")

    def test_a_vanished_group_is_not_an_error(self) -> None:
        proc = start("sleep 30")
        with patch.object(process_supervision.os, "killpg", side_effect=ProcessLookupError):
            terminate_process_group(proc)
        proc.kill()
        proc.wait(5)

    def test_every_tool_uses_the_shared_supervisor(self) -> None:
        for name in ("tools_terminal", "tools_macos", "tools_ui", "tools_browser", "tools_interactive",
                     "tools_snapshot", "artifact_pipeline"):
            source = (ROOT / "mcp_server" / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertIn("from .process_supervision import", source)
                self.assertNotRegex(source, re.compile(r"os\.killpg\(proc\.pid"))
                # osascript is started only through run_osascript.
                self.assertNotRegex(source, re.compile(r"""\[\s*["']osascript["']"""))


@unittest.skipUnless(Path("/usr/bin/osascript").exists(), "osascript is macOS only")
class RunOsascriptTests(unittest.TestCase):
    """#74: the one osascript runner the tools adapt to their own response shapes."""

    def marked_alive(self, marker: str) -> bool:
        out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False).stdout
        return bool(out.strip())

    def test_success_failure_and_arguments(self) -> None:
        ok = process_supervision.run_osascript('return "hi"', 10)
        self.assertEqual((True, "hi"), (ok.ok, ok.stdout.strip()))
        failed = process_supervision.run_osascript('error "boom" number 7', 10)
        self.assertFalse(failed.ok)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("boom", failed.stderr)
        # Arguments stay data: they never become AppleScript source.
        tricky = 'x" & (do shell script "echo pwned") & "'
        bound = process_supervision.run_osascript("on run argv\nreturn item 1 of argv\nend run", 10,
                                                  args=[tricky], script_on_stdin=False)
        self.assertEqual(tricky, bound.stdout.strip())

    def test_timeout_stops_the_process(self) -> None:
        marker = "mcp74timeout"
        started = time.monotonic()
        result = process_supervision.run_osascript(f"delay 20 -- {marker}", 0.5, script_on_stdin=False)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertLess(time.monotonic() - started, 5)
        deadline = time.monotonic() + 3
        while self.marked_alive(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.marked_alive(marker), subprocess.run(["pgrep", "-fl", marker], capture_output=True, text=True, check=False).stdout)

    def test_a_cancelled_call_stops_the_process_and_raises(self) -> None:
        import threading

        from mcp_server.tool_cancellation import (
            ToolCancellationContext,
            ToolCancelledError,
            reset_tool_cancellation,
            set_tool_cancellation,
        )

        marker = "mcp74cancel"
        outcome: dict = {}

        def worker() -> None:
            cancellation_token = set_tool_cancellation(context)
            try:
                process_supervision.run_osascript(f"delay 20 -- {marker}", 30, script_on_stdin=False)
                outcome["result"] = "finished"
            except ToolCancelledError:
                outcome["result"] = "cancelled"
            finally:
                reset_tool_cancellation(cancellation_token)

        context = ToolCancellationContext(cleanup_wait_s=1.5)
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 5
        while not self.marked_alive(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marked_alive(marker), "osascript did not start")
        context.cancel("test_cancel")
        thread.join(5)
        self.assertEqual("cancelled", outcome.get("result"))
        deadline = time.monotonic() + 3
        while self.marked_alive(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.marked_alive(marker), subprocess.run(["pgrep", "-fl", marker], capture_output=True, text=True, check=False).stdout)


if __name__ == "__main__":
    unittest.main()
