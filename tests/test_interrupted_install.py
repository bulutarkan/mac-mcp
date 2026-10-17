"""#241: an install or update cut off half way is put right by the next attempt, with no files
moved by hand, and the app and the runtime end on the same version."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_pkg_install import BASE_FILES, FakeRun, make_staging, make_staging_again, pkg
from tests.test_pkg_scripts import run_script


class Killed(BaseException):
    """The installer process dying: nothing after it runs, not even except blocks for Exception."""


class InterruptedUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-cut-")
        self.addCleanup(self.td.cleanup)
        self.home = Path(self.td.name)
        self.layout = pkg.Layout(self.home)
        make_staging(self.home, dict(BASE_FILES, OLD_ONLY="r1\n"))
        pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 1000)
        (self.layout.runtime / "mcp_server/agents").mkdir(parents=True)
        (self.layout.runtime / "mcp_server/agents/team.json").write_text('{"local": true}\n')
        self.files_before = self.layout.files_list.read_text()
        make_staging_again(self.home, dict(BASE_FILES, VERSION_MARK="B\n"))

    def test_cut_off_right_after_the_runtime_moved_is_recovered_by_the_next_run(self) -> None:
        with patch.object(pkg, "_lay_out_runtime", side_effect=Killed), self.assertRaises(Killed):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        self.assertFalse(self.layout.runtime.exists())  # the state a killed installer leaves
        journal = json.loads(self.layout.journal.read_text())
        self.assertEqual("upgrade", journal["mode"])
        self.assertTrue(Path(journal["backup"]).is_dir())
        self.assertFalse(pkg.is_upgrade(self.layout))  # before #241 the retry refused to run here

        run = FakeRun(self.layout)
        result = pkg.install(self.layout, run=run, health=lambda p: True, clock=lambda: 3000)
        rt = self.layout.runtime
        self.assertEqual("upgrade", result["mode"])
        self.assertEqual("B\n", (rt / "VERSION_MARK").read_text())
        self.assertEqual('{"local": true}\n', (rt / "mcp_server/agents/team.json").read_text())
        self.assertFalse(self.layout.journal.exists())
        self.assertEqual(["pkg-3000"], sorted(p.name for p in self.layout.backups_root.iterdir()))
        self.assertIn("interrupted upgrade", self.layout.log_file.read_text())

    def test_cut_off_half_way_through_the_new_runtime(self) -> None:
        real = pkg._lay_out_runtime

        def half(layout, release):
            real(layout, release)
            raise Killed

        with patch.object(pkg, "_lay_out_runtime", half), self.assertRaises(Killed):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        recovered = pkg.recover_interrupted(self.layout, pkg._Log(self.layout.log_file))
        self.assertEqual("upgrade", recovered)
        rt = self.layout.runtime
        self.assertEqual("A\n", (rt / "VERSION_MARK").read_text())  # the previous runtime, whole
        self.assertTrue((rt / "OLD_ONLY").exists())
        self.assertEqual(self.files_before, self.layout.files_list.read_text())
        self.assertFalse((self.layout.backups_root / "pkg-2000").exists())

    def test_cut_off_after_a_healthy_start_still_ends_on_the_new_release(self) -> None:
        with patch.object(pkg, "_prune_package_backups", side_effect=Killed), self.assertRaises(Killed):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        run = FakeRun(self.layout)
        pkg.install(self.layout, run=run, health=lambda p: True, clock=lambda: 3000)
        self.assertEqual("B\n", (self.layout.runtime / "VERSION_MARK").read_text())
        self.assertEqual([str(self.layout.cli), "stop"], run.calls[0])  # the cut-off run's server first
        self.assertFalse(self.layout.journal.exists())

    def test_an_ordinary_failure_leaves_no_journal(self) -> None:
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout, fail_on="pip check"), health=lambda p: True,
                        clock=lambda: 2000)
        self.assertFalse(self.layout.journal.exists())

    def test_an_unreadable_journal_stops_instead_of_guessing(self) -> None:
        self.layout.journal.write_text("{not json")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        self.assertEqual("A\n", (self.layout.runtime / "VERSION_MARK").read_text())


class InterruptedFreshInstallTests(unittest.TestCase):
    def test_a_cut_off_fresh_install_is_redone_by_the_next_run(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-cut-fresh-")
        self.addCleanup(td.cleanup)
        home = Path(td.name)
        layout = pkg.Layout(home)
        make_staging(home)
        with patch.object(pkg, "_ensure_settings", side_effect=Killed), self.assertRaises(Killed):
            pkg.install(layout, run=FakeRun(layout), health=lambda p: True)
        self.assertTrue(layout.runtime.exists())
        self.assertEqual("fresh", json.loads(layout.journal.read_text())["mode"])
        result = pkg.install(layout, run=FakeRun(layout), health=lambda p: True)
        self.assertEqual("fresh", result["mode"])
        self.assertEqual("pkg\n", layout.kind_file.read_text())
        self.assertFalse(layout.journal.exists())


class PreinstallJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-cut-scripts-")
        self.addCleanup(td.cleanup)
        self.home = Path(td.name)
        (self.home / ".mac-mcp/pkg").mkdir(parents=True)

    def test_a_cut_off_fresh_install_is_not_mistaken_for_another_install(self) -> None:
        (self.home / "mac-mcp").mkdir()
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/mac-mcp").write_text("#!/bin/sh\n")
        self.assertEqual(1, run_script("preinstall", self.home).returncode)
        (self.home / ".mac-mcp/pkg/transaction.json").write_text('{"mode": "fresh"}')
        proc = run_script("preinstall", self.home)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_the_previous_app_of_a_cut_off_upgrade_is_kept(self) -> None:
        (self.home / ".mac-mcp/install-kind").write_text("pkg\n")
        previous = self.home / ".mac-mcp/pkg/previous-app/Mac MCP.app/Contents"
        previous.mkdir(parents=True)
        (previous / "Info.plist").write_text("old app")
        app = self.home / "Applications/Mac MCP.app/Contents"
        app.mkdir(parents=True)
        (app / "Info.plist").write_text("new app from the cut-off run")
        (self.home / ".mac-mcp/pkg/transaction.json").write_text('{"mode": "upgrade"}')
        self.assertEqual(0, run_script("preinstall", self.home).returncode)
        self.assertEqual("old app", (previous / "Info.plist").read_text())


if __name__ == "__main__":
    unittest.main()
