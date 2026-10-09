from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli
from mcp_server.managed_process import ProcessValidation


def _validation(status: str, pid: int = 4242) -> ProcessValidation:
    return ProcessValidation(status, pid, "server", "json", status)


class StopExitingProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-cli-stop-")
        self.pid_file = Path(self.temp.name) / "server.pid"
        self.pid_file.write_text("4242", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _stop(self, *statuses: str, timeout: float = 2.0, force: bool = True) -> tuple[bool, list]:
        sequence = [_validation(s) for s in statuses]

        def validate(*_args, **_kwargs):
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        with patch.object(cli, "_validate_managed_pid", side_effect=validate), \
                patch.object(cli.os, "kill") as kill, \
                patch.object(cli.time, "sleep"):
            stopped = cli._stop_pid(self.pid_file, "mac-mcp", timeout, force)
        return stopped, kill.call_args_list

    def test_exiting_process_without_metadata_is_waited_out_not_aborted(self) -> None:
        # Restart aborted in production: right after SIGTERM, ps returned no
        # metadata for the exiting server, which read as "unverifiable".
        stopped, kills = self._stop("valid", "unverifiable", "unverifiable", "dead")
        self.assertTrue(stopped)
        self.assertEqual(1, len(kills))
        self.assertFalse(self.pid_file.exists())

    def test_process_that_stays_unverifiable_is_never_force_killed(self) -> None:
        stopped, kills = self._stop("valid", "unverifiable", timeout=0.05)
        self.assertFalse(stopped)
        self.assertEqual(1, len(kills))  # only the initial SIGTERM to the verified pid


if __name__ == "__main__":
    unittest.main()
