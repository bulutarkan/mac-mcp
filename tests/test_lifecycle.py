"""#132: preview-first uninstall and a repair that touches only managed parts."""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, lifecycle


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="mac-mcp-lifecycle-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.runtime = self.home / "mac-mcp"
        (self.runtime / "mcp_server").mkdir(parents=True)
        (self.runtime / "mcp_server" / ".env").write_text("MCP_API_KEY=x\n")
        self.state = self.home / ".mac-mcp"
        self.state.mkdir()
        (self.state / "settings.json").write_text("{}\n")
        self.app = self.home / "Applications" / "Mac MCP.app"
        (self.app / "Contents").mkdir(parents=True)
        self.cli_path = self.home / ".local" / "bin" / "mac-mcp"
        self.cli_path.parent.mkdir(parents=True)
        self.cli_path.write_text("#!/bin/sh\n")
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        (self.agents / "com.macmcp.supervisor.plist").write_text("x")
        self.source = self.home / "Projects" / "mac-mcp"
        (self.source / ".git").mkdir(parents=True)
        self.trash = self.home / ".Trash"
        self.trash.mkdir()

    def plan(self, **flags):
        return lifecycle.uninstall_plan(
            runtime=self.runtime, state=self.state, app=self.app, cli=self.cli_path,
            launch_agents_dir=self.agents, source=self.source, **flags,
        )


class UninstallPlanTests(Fixture):
    def test_default_plan_keeps_state_and_source(self) -> None:
        items = {item.key: item for item in self.plan()}
        for key in ("app", "runtime", "cli", "supervisor_job"):
            self.assertTrue(items[key].remove, key)
        self.assertFalse(items["state"].remove)
        self.assertFalse(items["source"].remove)
        self.assertTrue(items["state"].data_bearing)
        self.assertFalse(items["tunnel_job"].exists)
        self.assertIn(".env", items["runtime"].note)

    def test_state_and_source_move_only_when_asked(self) -> None:
        items = {item.key: item for item in self.plan(include_state=True, include_source=True)}
        self.assertTrue(items["state"].remove)
        self.assertTrue(items["source"].remove)

    def test_a_source_that_is_the_runtime_or_not_a_checkout_is_not_listed(self) -> None:
        keys = [item.key for item in lifecycle.uninstall_plan(
            runtime=self.runtime, state=self.state, app=self.app, cli=self.cli_path,
            launch_agents_dir=self.agents, source=self.runtime,
        )]
        self.assertNotIn("source", keys)
        shutil.rmtree(self.source / ".git")
        self.assertNotIn("source", [item.key for item in self.plan()])

    def test_apply_moves_to_the_trash_and_deletes_nothing(self) -> None:
        results = lifecycle.apply_uninstall(self.plan(), trash_root=self.trash, stamp="20261012-120000")
        folder = self.trash / "Mac MCP uninstall 20261012-120000"
        self.assertTrue((folder / "Mac MCP.app" / "Contents").is_dir())
        self.assertEqual("MCP_API_KEY=x\n", (folder / "mac-mcp" / "mcp_server" / ".env").read_text())
        self.assertTrue((folder / "mac-mcp-cli").is_file())
        self.assertFalse(self.app.exists())
        self.assertFalse(self.runtime.exists())
        self.assertTrue((self.state / "settings.json").is_file(), "state is kept by default")
        self.assertTrue(self.source.is_dir(), "source is kept by default")
        moved = {row["key"] for row in results if row["result"] == "moved"}
        self.assertEqual({"app", "runtime", "cli", "supervisor_job"}, moved)

    def test_preview_text_says_what_stays(self) -> None:
        text = lifecycle.format_plan(self.plan())
        self.assertIn("Kept", text)
        self.assertIn("settings, keys and tokens", text)
        self.assertIn("--include-state", text)
        self.assertIn("Nothing is deleted", text)


class UninstallCommandTests(Fixture):
    def args(self, **extra):
        values = dict(yes=False, include_state=False, include_source=False)
        values.update(extra)
        return argparse.Namespace(**values)

    def run_cli(self, args, default_install=True):
        out = io.StringIO()
        default_state = self.state if default_install else self.home / "elsewhere"
        with patch.object(cli, "STATE_DIR", self.state), patch.object(cli, "PROJECT_ROOT", self.runtime), \
                patch.object(cli, "DEFAULT_STATE_DIR", default_state), \
                patch.object(cli, "_launch_agents_dir", return_value=self.agents), \
                patch.object(cli, "default_cli_path", return_value=self.cli_path), \
                patch.object(cli, "_uninstall_app_path", return_value=self.app), \
                patch.object(cli, "_uninstall_source_path", return_value=self.source), \
                patch.object(cli, "_trash_root", return_value=self.trash), \
                patch.object(cli, "_stop_for_uninstall") as stop, \
                patch.object(cli, "_launchctl_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as launchctl, \
                patch.object(cli, "_forget_package_receipt") as forget, redirect_stdout(out):
            code = cli.uninstall(args)
        return code, out.getvalue(), stop, launchctl, forget

    def test_without_yes_it_only_previews(self) -> None:
        code, text, stop, launchctl, _ = self.run_cli(self.args())
        self.assertEqual(0, code)
        self.assertIn("mac-mcp uninstall --yes", text)
        stop.assert_not_called()
        launchctl.assert_not_called()
        self.assertTrue(self.app.exists() and self.runtime.exists())

    def test_yes_stops_unloads_and_moves(self) -> None:
        code, text, stop, launchctl, forget = self.run_cli(self.args(yes=True))
        self.assertEqual(0, code)
        stop.assert_called_once()
        self.assertTrue(any(call.args[0] == "bootout" for call in launchctl.call_args_list))
        forget.assert_called_once()
        self.assertFalse(self.app.exists())
        self.assertTrue(self.state.exists())
        self.assertIn("Moved to the Trash", text)


    def test_an_isolated_install_never_touches_the_shared_launchd_jobs(self) -> None:
        # launchd labels are shared with the default install: stopping or unloading them
        # from an isolated install would take down the owner's real server and tunnel.
        code, text, stop, launchctl, _ = self.run_cli(self.args(yes=True), default_install=False)
        self.assertEqual(0, code)
        stop.assert_not_called()
        launchctl.assert_not_called()
        self.assertFalse(self.app.exists())
        self.assertIn("not the default install", text)


class RepairChecksTests(Fixture):
    def test_missing_launcher_and_job_are_reported_with_fixes(self) -> None:
        self.cli_path.unlink()
        (self.agents / "com.macmcp.supervisor.plist").unlink()
        with patch.object(lifecycle, "inspect_venv", return_value={"status": "ok", "version": "3.14.7"}):
            checks = {check.key: check for check in lifecycle.repair_checks(
                runtime=self.runtime, cli=self.cli_path, app=self.app,
                supervisor_job=self.agents / "com.macmcp.supervisor.plist", start_at_login=True,
            )}
        self.assertTrue(checks["venv"].ok)
        self.assertFalse(checks["cli"].ok)
        self.assertFalse(checks["supervisor"].ok)
        self.assertTrue(checks["app"].ok)

    def test_a_broken_venv_is_reported(self) -> None:
        with patch.object(lifecycle, "inspect_venv", return_value={"status": "base_missing"}):
            checks = {check.key: check for check in lifecycle.repair_checks(
                runtime=self.runtime, cli=self.cli_path, app=self.app,
                supervisor_job=self.agents / "com.macmcp.supervisor.plist", start_at_login=True,
            )}
        self.assertFalse(checks["venv"].ok)
        self.assertIn("base_missing", checks["venv"].detail)


if __name__ == "__main__":
    unittest.main()
