from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli


def worker_args(**extra) -> argparse.Namespace:
    values = {"timeout": 1.0, "host": "127.0.0.1", "port": 8765, "reload": False}
    values.update(extra)
    return argparse.Namespace(**values)


class RestartWorkerRecoveryTests(unittest.TestCase):
    def run_worker(self, *, preflight=None, start_codes=(0,), health=(True,)):
        statuses: list[tuple[tuple, dict]] = []
        health_results = iter(health)
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: "1"}, clear=False), \
             patch.object(cli, "_load_env"), \
             patch.object(cli, "_restart_preflight", return_value=preflight), \
             patch.object(cli, "_wait_for_restart_requester_exit", return_value=True), \
             patch.object(cli, "_write_restart_status", side_effect=lambda *a, **k: statuses.append((a, k))), \
             patch.object(cli.time, "sleep"), \
             patch.object(cli, "stop", return_value=0) as stop, \
             patch.object(cli, "start", side_effect=list(start_codes)) as start, \
             patch.object(cli, "_restart_health_ok", side_effect=lambda *a, **k: next(health_results)), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")):
            code = cli.restart(worker_args())
        return code, statuses, stop, start

    def test_failed_preflight_keeps_the_running_server(self) -> None:
        code, statuses, stop, start = self.run_worker(preflight="cloudflared is not installed")
        self.assertEqual(1, code)
        stop.assert_not_called()
        start.assert_not_called()
        state, details = statuses[-1]
        self.assertEqual(("failed",), state)
        self.assertEqual("preflight", details["stage"])
        self.assertTrue(details["server_left_running"])

    def test_tunnel_failure_with_a_healthy_server_is_degraded_not_retried(self) -> None:
        code, statuses, _stop, start = self.run_worker(start_codes=(1,), health=(True,))
        self.assertEqual(1, code)
        self.assertEqual(1, start.call_count)
        state, details = statuses[-1]
        self.assertEqual(("degraded",), state)
        self.assertTrue(details["health"])

    def test_one_retry_recovers_a_failed_start(self) -> None:
        code, statuses, _stop, start = self.run_worker(start_codes=(1, 0), health=(False, True))
        self.assertEqual(0, code)
        self.assertEqual(2, start.call_count)
        state, details = statuses[-1]
        self.assertEqual(("succeeded",), state)
        self.assertTrue(details["recovered_after_retry"])

    def test_a_server_still_down_after_the_retry_is_reported_down(self) -> None:
        code, statuses, _stop, start = self.run_worker(start_codes=(1, 1), health=(False, False))
        self.assertEqual(1, code)
        self.assertEqual(2, start.call_count, "exactly one bounded retry")
        state, details = statuses[-1]
        self.assertEqual(("failed",), state)
        self.assertTrue(details["server_down"])
        self.assertIn("mac-mcp start", details["repair_command"])

    def test_retry_with_only_the_tunnel_failing_is_degraded(self) -> None:
        code, statuses, _stop, _start = self.run_worker(start_codes=(1, 1), health=(False, True))
        self.assertEqual(1, code)
        self.assertEqual(("degraded",), statuses[-1][0])


class RestartWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="mac-mcp-restart-wait-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        patcher = patch.object(cli, "STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def outcome(self, state: str, *, updated_at: float | None = None, **extra) -> int:
        (self.state / "restart-status.json").write_text(json.dumps({
            "state": state, "updated_at": time.time() if updated_at is None else updated_at, **extra,
        }), encoding="utf-8")
        with patch.object(cli, "_startup_health_timeout_s", return_value=0.0), \
             patch.object(cli, "RESTART_REQUESTER_WAIT_S", 0.0), \
             patch.object(cli.time, "sleep"):
            return cli._wait_for_restart_outcome(worker_args(), since=time.time() - 5)

    def test_exit_code_follows_the_final_state(self) -> None:
        self.assertEqual(0, self.outcome("succeeded", server_pid=1))
        self.assertEqual(1, self.outcome("degraded"))
        self.assertEqual(1, self.outcome("failed", stage="start", server_down=True))

    def test_an_older_outcome_is_not_mistaken_for_this_restart(self) -> None:
        with patch.object(cli.time, "time", side_effect=[1000.0, 1000.0, *[1000.0 + 120 * i for i in range(1, 50)]]):
            (self.state / "restart-status.json").write_text(json.dumps({"state": "succeeded", "updated_at": 10.0}))
            with patch.object(cli, "_startup_health_timeout_s", return_value=0.0), patch.object(cli.time, "sleep"):
                self.assertEqual(3, cli._wait_for_restart_outcome(worker_args(), since=999.0))

    def test_waiting_requester_does_not_block_the_worker(self) -> None:
        handoff_args = worker_args(public_mode=None, public_url=None, cloudflare_tunnel=None,
                                   cloudflare_token_file=None, cloudflared_bin=None, ngrok_domain=None,
                                   ngrok_bin=None, ngrok=False)
        log = self.state / "mac-mcp.log"
        with patch.object(cli, "LOG_FILE", log):
            waiting = plistlib.loads(cli._write_restart_handoff_plist(argparse.Namespace(**vars(handoff_args), wait=True)).read_bytes())
            plain = plistlib.loads(cli._write_restart_handoff_plist(argparse.Namespace(**vars(handoff_args), wait=False)).read_bytes())
        self.assertIn(f"{cli.RESTART_REQUESTER_ENV}=0", waiting["ProgramArguments"])
        self.assertIn(f"{cli.RESTART_REQUESTER_ENV}={os.getpid()}", plain["ProgramArguments"])
        self.assertNotIn("--wait", waiting["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
