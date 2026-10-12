from __future__ import annotations

import argparse
import io
import json
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, supervisor


class StartAtLoginPlistTests(unittest.TestCase):
    """The supervisor job lives in ~/Library/LaunchAgents (run at login) unless start at login is off."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-login-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.agents = self.root / "LaunchAgents"
        self.settings = self.root / "settings.json"
        self.launchctl: list[tuple[str, ...]] = []
        for target, value in (
            (patch.object(cli, "STATE_DIR", self.state), None),
            (patch.object(cli, "DEFAULT_STATE_DIR", self.state), None),
            (patch.object(cli, "_launch_agents_dir", return_value=self.agents), None),
            (patch.object(cli, "_supervisor_loaded", return_value=False), None),
            (patch("mcp_server.runtime_settings.settings_path", return_value=self.settings), None),
            (patch.object(cli, "_launchctl_run", side_effect=self.record), None),
        ):
            target.start()
            self.addCleanup(target.stop)

    def record(self, *args, **_kwargs):
        self.launchctl.append(args)
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def write_setting(self, value: bool) -> None:
        self.settings.write_text(json.dumps({"server": {"start_at_login": value}}))

    def test_start_at_login_is_on_by_default(self) -> None:
        self.assertTrue(cli._start_at_login())
        self.assertEqual(self.agents / f"{cli.SUPERVISOR_LABEL}.plist", cli._supervisor_plist_path())
        self.assertTrue(cli._supervisor_payload()["RunAtLoad"])

    def test_off_keeps_the_job_in_the_state_folder_without_run_at_load(self) -> None:
        self.write_setting(False)
        self.assertFalse(cli._start_at_login())
        self.assertEqual(self.state / "supervisor.plist", cli._supervisor_plist_path())
        self.assertFalse(cli._supervisor_payload()["RunAtLoad"])

    def test_ensure_moves_the_job_into_launch_agents_and_removes_the_old_copy(self) -> None:
        old = self.state / "supervisor.plist"
        old.write_bytes(plistlib.dumps({"Label": cli.SUPERVISOR_LABEL}))
        cli._ensure_supervisor()
        agent = self.agents / f"{cli.SUPERVISOR_LABEL}.plist"
        self.assertTrue(agent.is_file())
        self.assertFalse(old.exists())
        self.assertTrue(plistlib.loads(agent.read_bytes())["RunAtLoad"])
        self.assertEqual("bootstrap", self.launchctl[-1][0])
        self.assertEqual(str(agent), self.launchctl[-1][2])

    def test_ensure_with_start_at_login_off_removes_the_launch_agent(self) -> None:
        self.write_setting(False)
        agent = self.agents / f"{cli.SUPERVISOR_LABEL}.plist"
        self.agents.mkdir()
        agent.write_bytes(plistlib.dumps({"Label": cli.SUPERVISOR_LABEL}))
        cli._ensure_supervisor()
        self.assertFalse(agent.exists())
        self.assertTrue((self.state / "supervisor.plist").is_file())

    def test_autostart_command_saves_the_choice_and_reconciles(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.autostart(argparse.Namespace(state="off", json=True))
        self.assertEqual(0, code)
        self.assertFalse(json.loads(self.settings.read_text())["server"]["start_at_login"])
        report = json.loads(out.getvalue())
        self.assertFalse(report["start_at_login"])
        self.assertEqual(str(self.state / "supervisor.plist"), report["job"])
        out = io.StringIO()
        with redirect_stdout(out):
            cli.autostart(argparse.Namespace(state="on", json=True))
        self.assertTrue(json.loads(out.getvalue())["start_at_login"])
        self.assertTrue((self.agents / f"{cli.SUPERVISOR_LABEL}.plist").is_file())

    def test_autostart_status_changes_nothing(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            cli.autostart(argparse.Namespace(state=None, json=True))
        self.assertTrue(json.loads(out.getvalue())["start_at_login"])
        self.assertFalse(self.settings.exists())
        self.assertEqual([], self.launchctl)


class LoginPassTests(unittest.TestCase):
    """The first supervision pass after login starts a server the owner left running, and opens the app."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-login-pass-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.calls: list[tuple[list[str], bool]] = []
        self.health = [False]

    def run_pass(self, *, boot_time: float, now: float):
        def fake_cli(args, open_menu_app=False):
            self.calls.append((args, open_menu_app))
            self.health = [True]
            return 0

        return supervisor.run_once(
            root=self.root, now=now, healthy=lambda port: self.health[-1],
            process_present=lambda root, port: False, cli=fake_cli,
            update_lock_held=lambda: False, boot_time=lambda: boot_time,
        )

    def test_first_pass_after_a_restart_starts_the_server_and_the_app(self) -> None:
        supervisor.write_intent("running", start_args=["--port", "8877"], root=self.root)
        before = time.time() - 3600
        supervisor._write_json(self.root / supervisor.STATE_FILE, {"last_check_at": before})
        state = self.run_pass(boot_time=before + 60, now=before + 120)
        self.assertEqual("started_at_login", state["last_result"])
        self.assertEqual([(["start", "--port", "8877"], True)], self.calls)
        self.assertEqual([], state.get("recovery_attempts") or [])

    def test_a_server_stopped_on_purpose_stays_stopped_after_login(self) -> None:
        supervisor.write_intent("stopped", start_args=[], root=self.root)
        before = time.time() - 3600
        supervisor._write_json(self.root / supervisor.STATE_FILE, {"last_check_at": before})
        state = self.run_pass(boot_time=before + 60, now=before + 120)
        self.assertEqual("idle_stopped", state["last_result"])
        self.assertEqual([], self.calls)

    def test_a_crash_in_the_same_session_is_a_recovery_without_the_app(self) -> None:
        supervisor.write_intent("running", start_args=[], root=self.root)
        boot = time.time() - 3600
        supervisor._write_json(self.root / supervisor.STATE_FILE, {"last_check_at": boot + 60})
        state = self.run_pass(boot_time=boot, now=boot + 90)
        self.assertEqual("recovered", state["last_result"])
        self.assertEqual([(["start"], False)], self.calls)

    def test_boot_time_reads_kern_boottime(self) -> None:
        value = supervisor._boot_time()
        self.assertGreater(value, 0)
        self.assertLessEqual(value, time.time())


if __name__ == "__main__":
    unittest.main()


class DefaultInstallIdentityTests(unittest.TestCase):
    def test_the_default_install_follows_the_account_home_not_the_home_variable(self) -> None:
        # Isolated installs run with a temporary HOME; they must not count as the default
        # install, whose launchd jobs and ~/Applications they share.
        import importlib
        import os
        import pwd

        real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".mac-mcp"
        with tempfile.TemporaryDirectory(prefix="mac-mcp-fake-home-") as fake, \
                patch.dict(os.environ, {"HOME": fake}):
            fresh = importlib.reload(cli)
            try:
                self.assertEqual(real, fresh.DEFAULT_STATE_DIR)
            finally:
                importlib.reload(cli)
