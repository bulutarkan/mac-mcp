from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.update_helper import UpdateError, apply_update, check_update, format_check
from mcp_server.update_state import migrate_completed_legacy_update


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.update_dir = Path(tempfile.mkdtemp(prefix="mac-mcp-update-state-test-"))
        self.addCleanup(shutil.rmtree, self.update_dir, True)
        self.env_patcher = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.update_dir)})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def make_fixture(self, conflict: bool = False):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "source"
        source.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=source)
        run("git", "config", "user.email", "test@example.com", cwd=source)
        run("git", "config", "user.name", "Test", cwd=source)
        (source / "mcp_server").mkdir()
        (source / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (source / "mcp_server/requirements.txt").write_text("", encoding="utf-8")
        (source / "mcp_server/security.py").write_text("SECURITY = True\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "old", cwd=source)
        old = run("git", "rev-parse", "HEAD", cwd=source)

        remote = root / "remote.git"
        run("git", "clone", "-q", "--bare", str(source), str(remote))
        (source / "mcp_server/main.py").write_text("VALUE = 'new'\nNEW_FEATURE = True\n", encoding="utf-8")
        (source / "mcp_server/new_tool.py").write_text("ENABLED = True\n", encoding="utf-8")
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "new", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        run("git", "push", "-q", str(remote), "main", cwd=source)

        repo = root / "repo"
        run("git", "clone", "-q", str(remote), str(repo))
        run("git", "reset", "-q", "--hard", old, cwd=repo)
        runtime = root / "runtime"
        shutil.copytree(repo / "mcp_server", runtime / "mcp_server")
        main = runtime / "mcp_server/main.py"
        if conflict:
            main.write_text("VALUE = 'custom'\n", encoding="utf-8")
        else:
            security = runtime / "mcp_server/security.py"
            security.write_text(security.read_text(encoding="utf-8") + "# RUNTIME_CUSTOMIZATION\n", encoding="utf-8")
        (runtime / "mcp_server/.env").write_text("SECRET_SENTINEL=preserve-me\n", encoding="utf-8")
        return root, repo, runtime, old, target

    def test_check_and_update_preserve_runtime_overlay_and_env(self):
        _, repo, runtime, old, target = self.make_fixture()
        info = check_update(repo, runtime)
        self.assertTrue(info.update_available)
        self.assertEqual(1, info.behind_by)
        self.assertIn("Update available", format_check(info))

        result = apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        text = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        self.assertIn("VALUE = 'new'", text)
        self.assertNotIn("RUNTIME_CUSTOMIZATION", text)
        self.assertIn("RUNTIME_CUSTOMIZATION", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertTrue((runtime / "mcp_server/new_tool.py").exists())
        self.assertIn("preserve-me", (runtime / "mcp_server/.env").read_text(encoding="utf-8"))
        self.assertEqual(target, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue(Path(result["backup"]).exists())
        self.assertTrue(str(Path(result["backup"])).startswith(str(self.update_dir / "backups")))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups/updates").exists())

    def test_single_checkout_update_stays_clean_and_second_check_works(self):
        root, repo, _runtime, _old, target = self.make_fixture()
        # Public install shape: one Git checkout is both source repo and live runtime.
        single = root / "single"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)

        before = check_update(single, single)
        self.assertTrue(before.update_available)
        result = apply_update(single, single, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=single))

        after = check_update(single, single)
        self.assertFalse(after.dirty)
        self.assertFalse(after.update_available)
        self.assertIn("up to date", format_check(after))
        self.assertTrue((self.update_dir / "deployed-commit").exists())
        self.assertTrue((self.update_dir / "state.json").exists())
        self.assertTrue((self.update_dir / "backups").exists())

    def test_completed_legacy_state_moves_out_of_checkout(self):
        runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-legacy-update-test-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        commit = "a" * 40
        (runtime / ".mac-mcp-deployed-commit").write_text(commit + "\n", encoding="utf-8")
        (runtime / ".mac-mcp-update.json").write_text(json.dumps({"status": "completed", "to_commit": commit}) + "\n", encoding="utf-8")
        old_backup = runtime / "backups" / "updates" / "legacy-backup"
        old_backup.mkdir(parents=True)
        (old_backup / "manifest.json").write_text("{}\n", encoding="utf-8")

        self.assertTrue(migrate_completed_legacy_update(runtime))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups").exists())
        self.assertEqual(commit, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue((self.update_dir / "backups" / "legacy-backup" / "manifest.json").exists())

    def test_conflicting_runtime_overlay_aborts_before_repo_or_runtime_change(self):
        _, repo, runtime, old, _ = self.make_fixture(conflict=True)
        before = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        with self.assertRaises(UpdateError):
            apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(before, (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertFalse((self.update_dir / "backups").exists())
        run("git", "worktree", "prune", cwd=repo)

    def test_dirty_repo_is_reported(self):
        _, repo, runtime, _, _ = self.make_fixture()
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        info = check_update(repo, runtime)
        self.assertTrue(info.dirty)
        self.assertIn("Update blocked", format_check(info))


if __name__ == "__main__":
    unittest.main()
