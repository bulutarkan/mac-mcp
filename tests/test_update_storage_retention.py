from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import update_helper, update_state


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def age(path: Path, seconds: float = 3600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class StorageRetentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-retention-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.root / "update"), "MAC_MCP_UPDATE_BACKUPS_KEPT": "3"})
        env.start()
        self.addCleanup(env.stop)

    def journal(self, **payload) -> None:
        (update_state.update_root() / "state.json").write_text(json.dumps(payload), encoding="utf-8")

    def backup(self, name: str, manifest: bool = True) -> Path:
        path = update_state.backups_root() / name
        (path / "mcp_server").mkdir(parents=True)
        (path / "mcp_server" / "main.py").write_text("x" * 1000, encoding="utf-8")
        if manifest:
            (path / "manifest.json").write_text("{}", encoding="utf-8")
        return path


class BackupRetentionTests(StorageRetentionTestCase):
    def test_keeps_the_newest_and_the_journal_backup_and_never_touches_other_folders(self) -> None:
        names = [f"2026100{day}-120000-{day}aaaaaaa" for day in range(1, 7)]
        for name in names:
            self.backup(name)
        manual = self.backup("manual-auth-query-20260910-135640")
        unknown = self.backup("20260101-000000-abcdef12", manifest=False)
        oldest = update_state.backups_root() / names[0]
        self.journal(status="completed", backup=str(oldest))

        result = update_helper.prune_update_backups()

        remaining = sorted(path.name for path in update_state.backups_root().iterdir())
        self.assertEqual(sorted([names[0], *names[-3:], manual.name, unknown.name]), remaining)
        self.assertEqual(2, result["removed"])
        self.assertGreater(result["bytes_freed"], 2000)

    def test_an_incomplete_or_unreadable_transaction_blocks_pruning(self) -> None:
        for day in range(1, 7):
            self.backup(f"2026100{day}-120000-{day}aaaaaaa")
        self.journal(status="runtime_syncing", transaction_version=1, backup="")
        self.assertEqual("skipped", update_helper.prune_update_backups()["status"])
        (update_state.update_root() / "state.json").write_text("{not json", encoding="utf-8")
        self.assertEqual("skipped", update_helper.prune_update_backups()["status"])
        self.assertEqual(6, len(list(update_state.backups_root().iterdir())))

    def test_missing_backup_folder_is_not_created(self) -> None:
        self.assertEqual(0, update_helper.prune_update_backups()["removed"])
        self.assertFalse((update_state.update_root() / "backups").exists())


class OrphanedStagingTests(StorageRetentionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        git("add", "a.txt", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=self.repo)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()

    def staged_worktree(self, name: str) -> Path:
        temp_root = update_state.staging_root() / name
        temp_root.mkdir()
        stage = temp_root / "runtime-merge"
        git("worktree", "add", "--quiet", "--detach", str(stage), "HEAD", cwd=self.repo)
        return temp_root

    def registered(self) -> list[str]:
        return [line.split(" ", 1)[1] for line in git("worktree", "list", "--porcelain", cwd=self.repo).splitlines()
                if line.startswith("worktree ")]

    def test_orphans_of_a_killed_updater_are_removed_and_nothing_else(self) -> None:
        orphan = self.staged_worktree("mac-mcp-update-orphan")
        vanished = self.staged_worktree("mac-mcp-update-vanished")
        shutil.rmtree(vanished)
        own = update_state.staging_root() / "mac-mcp-update-upd_x-own"
        own.mkdir()
        fresh = update_state.staging_root() / "mac-mcp-update-fresh"
        fresh.mkdir()
        venv_orphan = self.root / ".runtime.venv-update-abc"
        (venv_orphan / "candidate").mkdir(parents=True)
        unrelated = self.root / "agent-worktree"
        git("worktree", "add", "--quiet", "--detach", str(unrelated), "HEAD", cwd=self.repo)
        unrelated_missing = self.root / "gone-agent-worktree"
        git("worktree", "add", "--quiet", "--detach", str(unrelated_missing), "HEAD", cwd=self.repo)
        shutil.rmtree(unrelated_missing)
        for path in (orphan, own, venv_orphan):
            age(path)

        result = update_helper.cleanup_orphaned_staging(self.repo, self.runtime, exclude=[own])

        self.assertEqual("ok", result["status"], result)
        self.assertFalse(orphan.exists())
        self.assertFalse(venv_orphan.exists())
        self.assertTrue(own.exists(), "the running updater's own folder is excluded")
        self.assertTrue(fresh.exists(), "recent staging is left alone")
        registered = [str(Path(path).resolve()) for path in self.registered()]
        self.assertFalse(any("mac-mcp-update-" in path for path in registered), registered)
        self.assertIn(str(unrelated.resolve()), registered)
        self.assertIn(str(unrelated_missing.resolve()), registered, "stale registrations it does not own stay")

    def test_incomplete_transaction_blocks_cleanup(self) -> None:
        orphan = self.staged_worktree("mac-mcp-update-orphan")
        age(orphan)
        self.journal(status="repo_updating", transaction_version=1)
        self.assertEqual("skipped", update_helper.cleanup_orphaned_staging(self.repo, self.runtime)["status"])
        self.assertTrue(orphan.exists())


class UpdateStorageDoctorTests(StorageRetentionTestCase):
    def test_reports_size_and_flags_failed_cleanup(self) -> None:
        from mcp_server import diagnostics

        BackupRetentionTests.backup(self, "20261001-120000-1aaaaaaa")
        healthy = diagnostics._check_update_storage()
        self.assertEqual(diagnostics.PASS, healthy.status)
        self.assertEqual(1, healthy.details["backups"])
        self.journal(status="completed", backup_retention={"failures": ["20260901-000000-abcdef12: PermissionError"]})
        failed = diagnostics._check_update_storage()
        self.assertEqual(diagnostics.WARN, failed.status)
        self.assertEqual("UPDATE_CLEANUP_FAILED", failed.reason_code)


if __name__ == "__main__":
    unittest.main()
