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
        for name in ("tools_terminal", "tools_macos", "tools_ui", "tools_browser", "tools_interactive"):
            source = (ROOT / "mcp_server" / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertIn("from .process_supervision import terminate_process_group", source)
                self.assertNotRegex(source, re.compile(r"os\.killpg\(proc\.pid"))


if __name__ == "__main__":
    unittest.main()
