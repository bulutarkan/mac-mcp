"""`mac-mcp promote`: the committed, pushed HEAD reaches the runtime through the updater's own steps.

Real Git repositories in a temporary folder: a source checkout with a bare origin and a runtime that
was deployed at an older commit. No restart and no menu app build run here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import dev_promote, update_state


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class DevPromoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-promote-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.root / "update"),
                                       "MAC_MCP_SKIP_MENU_APP_INSTALL": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.runtime = self.root / "runtime"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.write("mcp_server/main.py", "VALUE = 'old'\n")
        self.write("mcp_server/gone.py", "GONE = True\n")
        self.write("mcp_server/settings_local.py", "LOCAL = 'shipped'\n")
        self.write("mcp_server/requirements.txt", "fastapi==1.0\n")
        self.write("menu_app/Sources/App.swift", "// old\n")
        self.deployed = self.commit("deployed")
        remote = self.root / "remote.git"
        subprocess.check_call(["git", "clone", "-q", "--bare", str(self.repo), str(remote)])
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
        # The runtime as the last update left it, with one local customization.
        for rel in ("mcp_server/main.py", "mcp_server/gone.py", "mcp_server/settings_local.py",
                    "mcp_server/requirements.txt", "menu_app/Sources/App.swift"):
            target = self.runtime / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repo / rel, target)
        (self.runtime / "mcp_server/settings_local.py").write_text("LOCAL = 'customized'\n", encoding="utf-8")
        update_state.write_deployed_commit(self.deployed)

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def push(self) -> None:
        git(self.repo, "push", "-q", "origin", "main")

    def run_promote(self) -> dict:
        return dev_promote.promote(self.repo, self.runtime, restart=False, refresh_menu_app=False, say=lambda _: None)

    def read(self, rel: str) -> str:
        return (self.runtime / rel).read_text(encoding="utf-8")

    def test_head_reaches_the_runtime_and_becomes_its_deployed_commit(self) -> None:
        self.write("mcp_server/main.py", "VALUE = 'new'\n")
        self.write("mcp_server/added.py", "ADDED = True\n")
        (self.repo / "mcp_server/gone.py").unlink()
        head = self.commit("change")
        self.push()
        result = self.run_promote()
        self.assertTrue(result["promoted"])
        self.assertEqual("VALUE = 'new'\n", self.read("mcp_server/main.py"))
        self.assertEqual("ADDED = True\n", self.read("mcp_server/added.py"))
        self.assertFalse((self.runtime / "mcp_server/gone.py").exists())
        self.assertEqual("LOCAL = 'customized'\n", self.read("mcp_server/settings_local.py"))
        self.assertEqual(head, update_state.read_deployed_commit(self.runtime))
        self.assertTrue(Path(result["backup"]).is_dir())
        # The merge worktree is gone again: only the checkout itself is listed.
        self.assertEqual(1, git(self.repo, "worktree", "list", "--porcelain").count("worktree "))
        # A second run has nothing left to do.
        self.assertFalse(self.run_promote()["promoted"])

    def test_a_file_already_copied_by_hand_is_not_a_conflict(self) -> None:
        self.write("mcp_server/main.py", "VALUE = 'new'\n")
        head = self.commit("change")
        self.push()
        shutil.copy2(self.repo / "mcp_server/main.py", self.runtime / "mcp_server/main.py")
        self.run_promote()
        self.assertEqual("VALUE = 'new'\n", self.read("mcp_server/main.py"))
        self.assertEqual(head, update_state.read_deployed_commit(self.runtime))

    def test_a_customization_the_change_also_touches_stops_everything(self) -> None:
        self.write("mcp_server/settings_local.py", "LOCAL = 'shipped v2'\n")
        self.write("mcp_server/main.py", "VALUE = 'new'\n")
        self.commit("change")
        self.push()
        with self.assertRaisesRegex(dev_promote.PromoteError, "customizations conflict"):
            self.run_promote()
        self.assertEqual("VALUE = 'old'\n", self.read("mcp_server/main.py"))
        self.assertEqual("LOCAL = 'customized'\n", self.read("mcp_server/settings_local.py"))
        self.assertEqual(self.deployed, update_state.read_deployed_commit(self.runtime))

    def test_a_file_that_does_not_compile_restores_the_runtime(self) -> None:
        self.write("mcp_server/main.py", "VALUE = (\n")
        self.write("mcp_server/added.py", "ADDED = True\n")
        self.commit("broken")
        self.push()
        with self.assertRaisesRegex(dev_promote.PromoteError, "does not compile"):
            self.run_promote()
        self.assertEqual("VALUE = 'old'\n", self.read("mcp_server/main.py"))
        self.assertFalse((self.runtime / "mcp_server/added.py").exists())
        self.assertTrue((self.runtime / "mcp_server/gone.py").exists())
        self.assertEqual(self.deployed, update_state.read_deployed_commit(self.runtime))

    def test_refuses_what_a_later_update_could_not_follow(self) -> None:
        self.write("mcp_server/main.py", "VALUE = 'new'\n")
        self.commit("not pushed")
        with self.assertRaisesRegex(dev_promote.PromoteError, "Push it first"):
            self.run_promote()
        self.push()
        self.write("mcp_server/main.py", "VALUE = 'dirty'\n")
        with self.assertRaisesRegex(dev_promote.PromoteError, "uncommitted"):
            self.run_promote()
        git(self.repo, "checkout", "-q", "--", "mcp_server/main.py")
        git(self.repo, "checkout", "-q", "-b", "feature")
        with self.assertRaisesRegex(dev_promote.PromoteError, "must be on 'main'"):
            self.run_promote()
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(self.deployed, update_state.read_deployed_commit(self.runtime))

    def test_a_dependency_change_is_left_to_the_updater(self) -> None:
        self.write("mcp_server/requirements.txt", "fastapi==2.0\n")
        self.commit("deps")
        self.push()
        with self.assertRaisesRegex(dev_promote.PromoteError, "dependencies changed"):
            self.run_promote()
        self.assertEqual("fastapi==1.0\n", self.read("mcp_server/requirements.txt"))
        self.assertEqual(self.deployed, update_state.read_deployed_commit(self.runtime))

    def test_the_plan_names_what_restarts(self) -> None:
        self.write("menu_app/Sources/App.swift", "// new\n")
        self.commit("app only")
        self.push()
        plan = dev_promote.plan_promote(self.repo, self.runtime)
        self.assertEqual(["menu_app/Sources/App.swift"], plan.changed_files)
        self.assertTrue(plan.menu_app_changed)
        self.assertFalse(plan.server_changed)
        self.assertIn("server: unchanged", dev_promote.format_plan(plan))

    def test_the_cli_offers_promote(self) -> None:
        from mcp_server import cli

        parser = cli.build_parser()
        args = parser.parse_args(["promote", "--dry-run", "--no-restart", "--no-app"])
        self.assertIs(cli.promote, args.func)
        self.assertTrue(args.dry_run and args.no_restart and args.no_app)


if __name__ == "__main__":
    unittest.main()
