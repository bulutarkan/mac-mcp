from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, supervisor


class SupervisorPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-supervisor-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.calls: list[list[str]] = []
        self.health = [False]

    def run_pass(self, *, present=False, update=False, cli_codes=None, now=None):
        codes = iter(cli_codes or [0, 0])

        def fake_cli(args):
            self.calls.append(args)
            code = next(codes)
            if args[0] == "start" and code == 0:
                self.health = [True]
            return code

        return supervisor.run_once(
            root=self.root, now=now if now is not None else time.time(),
            healthy=lambda port: self.health[-1],
            process_present=lambda root, port: present,
            cli=fake_cli, update_lock_held=lambda: update,
        )

    def intend(self, desired: str) -> None:
        supervisor.write_intent(desired, start_args=["--host", "127.0.0.1", "--port", "8877"], root=self.root)

    def test_an_intentional_stop_is_never_undone(self) -> None:
        self.intend("stopped")
        self.assertEqual("idle_stopped", self.run_pass()["last_result"])
        self.assertEqual([], self.calls)

    def test_no_intent_means_no_action(self) -> None:
        self.assertEqual("idle_stopped", self.run_pass()["last_result"])
        self.assertEqual([], self.calls)

    def test_a_crashed_server_is_started_with_the_recorded_flags(self) -> None:
        self.intend("running")
        state = self.run_pass()
        self.assertEqual("recovered", state["last_result"])
        self.assertEqual([["start", "--host", "127.0.0.1", "--port", "8877"]], self.calls)
        self.assertEqual("process_exited", state["last_recovery"]["reason"])
        self.assertEqual("recovered", state["last_recovery"]["result"])
        self.assertTrue((self.root / "supervisor-state.json").exists())

    def test_update_and_restart_in_progress_are_left_alone(self) -> None:
        self.intend("running")
        self.assertEqual("skipped_update", self.run_pass(update=True)["last_result"])
        (self.root / "restart-status.json").write_text(json.dumps({"state": "running", "updated_at": time.time()}))
        self.assertEqual("skipped_restart", self.run_pass()["last_result"])
        self.assertEqual([], self.calls)

    def test_an_unresponsive_server_is_restarted_only_after_several_failed_passes(self) -> None:
        self.intend("running")
        for _ in range(supervisor.UNHEALTHY_PASSES_BEFORE_RESTART - 1):
            self.assertEqual("unhealthy", self.run_pass(present=True)["last_result"])
        self.assertEqual([], self.calls)
        state = self.run_pass(present=True)
        self.assertEqual("recovered", state["last_result"])
        self.assertEqual(["stop", "--force", "--keep-intent"], self.calls[0])
        self.assertEqual("start", self.calls[1][0])
        self.assertEqual("unresponsive", state["last_recovery"]["reason"])

    def test_repeated_failures_back_off(self) -> None:
        self.intend("running")
        now = 10_000.0
        for attempt in range(supervisor.RECOVERY_LIMIT):
            state = self.run_pass(cli_codes=[1], now=now + attempt)
            self.assertEqual("recovery_failed", state["last_result"])
        state = self.run_pass(cli_codes=[1], now=now + 60)
        self.assertEqual("backoff", state["last_result"])
        self.assertEqual(supervisor.RECOVERY_LIMIT, len(self.calls))
        state = self.run_pass(cli_codes=[0], now=now + supervisor.RECOVERY_WINDOW_S + 5)
        self.assertEqual("recovered", state["last_result"], "the window passed, so it tries again")

    def test_kill_switch(self) -> None:
        self.intend("running")
        with patch.dict(os.environ, {"MAC_MCP_SUPERVISOR": "0"}):
            self.assertEqual("disabled", self.run_pass()["last_result"])
        self.assertEqual([], self.calls)


class IntentAndStartLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="mac-mcp-intent-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        for name, value in (("STATE_DIR", self.state), ("PID_FILE", self.state / "mac-mcp.pid")):
            patcher = patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **extra) -> argparse.Namespace:
        values = dict(host="127.0.0.1", port=8877, reload=False, public_mode="none", public_url=None,
                      cloudflare_tunnel=None, cloudflare_token_file=None, cloudflared_bin=None,
                      ngrok=False, ngrok_domain=None, ngrok_bin=None)
        values.update(extra)
        return argparse.Namespace(**values)

    def test_start_records_running_and_stop_records_stopped_before_stopping(self) -> None:
        seen_at_stop = []
        with patch.object(cli, "_load_env"), patch.object(cli, "validate_bootstrap_security"), \
             patch.object(cli, "load_settings"), patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_stop_unselected_public_processes"):
            self.assertEqual(0, cli.start(self.args()))
        intent = supervisor.read_intent(self.state)
        self.assertEqual("running", intent["desired"])
        self.assertIn("8877", intent["start_args"])

        def stop_pid(*_args, **_kwargs):
            seen_at_stop.append(supervisor.read_intent(self.state)["desired"])
            return True

        with patch.object(cli, "_load_env"), patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_stop_pid", side_effect=stop_pid), patch.object(cli, "_stop_cloudflare", return_value=True):
            cli.stop(argparse.Namespace(timeout=1, force=True))
        self.assertEqual("stopped", seen_at_stop[0], "intent is recorded before the server is stopped")
        self.assertEqual(intent["start_args"], supervisor.read_intent(self.state)["start_args"],
                         "stop keeps the flags the supervisor would start with")

    def test_keep_intent_stop_leaves_running(self) -> None:
        supervisor.write_intent("running", start_args=[], root=self.state)
        with patch.object(cli, "_load_env"), patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")), \
             patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True):
            cli.stop(argparse.Namespace(timeout=1, force=True, keep_intent=True))
        self.assertEqual("running", supervisor.read_intent(self.state)["desired"])

    def test_concurrent_starts_run_one_at_a_time(self) -> None:
        active, overlaps = [0], []

        def locked(_args):
            active[0] += 1
            overlaps.append(active[0])
            time.sleep(0.2)
            active[0] -= 1
            return 0

        with patch.object(cli, "_start_server_locked", side_effect=locked), \
             patch.object(cli, "_ensure_supervisor") as ensure:
            threads = [threading.Thread(target=cli._start_server, args=(self.args(),)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual([1, 1, 1], overlaps)
        self.assertEqual(3, ensure.call_count)

    def test_supervisor_is_only_installed_for_the_default_state_folder(self) -> None:
        with patch.object(cli, "_launchctl_run") as launchctl:
            cli._ensure_supervisor()
        launchctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
