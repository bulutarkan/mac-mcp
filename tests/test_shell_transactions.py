from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.file_transactions as journal
import mcp_server.tools_files as files
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings
from mcp_server.shell_transactions import begin_shell_capture
from mcp_server.tools_terminal import run_command
from mcp_server import tools_jobs


class ReversibleShellRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-shell-tx-")
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_STATE_DIR": str(self.state),
                "MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS": "128",
                "MAC_MCP_FILE_JOURNAL_MAX_BYTES": str(128 * 1024 * 1024),
                "MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES": str(64 * 1024 * 1024),
                "MAC_MCP_SHELL_CAPTURE_MAX_FILES": "1000",
                "MAC_MCP_SHELL_CAPTURE_MAX_BYTES": str(32 * 1024 * 1024),
                "MAC_MCP_SHELL_CAPTURE_MAX_FILE_BYTES": str(8 * 1024 * 1024),
            },
            clear=False,
        )
        self.env.start()
        self.settings = replace(load_settings(), workdir=self.work)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_shell_create_delete_rename_modify_and_undo(self) -> None:
        modified = self.work / "modified.txt"
        renamed = self.work / "rename-me.txt"
        deleted = self.work / "delete-me.txt"
        modified.write_text("before-modified", encoding="utf-8")
        renamed.write_text("before-rename", encoding="utf-8")
        deleted.write_text("before-delete", encoding="utf-8")

        result = run_command(
            self.settings,
            "printf changed > modified.txt; mv rename-me.txt renamed.txt; rm delete-me.txt; printf created > created.txt",
            reversible=True,
            reversible_root=str(self.work),
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["undoable"])
        self.assertEqual("full", result["reversibility"])
        self.assertGreaterEqual(result["changed_count"], 4)
        self.assertEqual("changed", modified.read_text())
        self.assertTrue((self.work / "renamed.txt").exists())
        self.assertFalse(renamed.exists())
        self.assertFalse(deleted.exists())
        self.assertTrue((self.work / "created.txt").exists())

        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("before-modified", modified.read_text())
        self.assertEqual("before-rename", renamed.read_text())
        self.assertEqual("before-delete", deleted.read_text())
        self.assertFalse((self.work / "renamed.txt").exists())
        self.assertFalse((self.work / "created.txt").exists())

    def test_direct_file_edit_plus_formatter_six_files_is_one_compound_undo(self) -> None:
        paths = [self.work / f"f{i}.txt" for i in range(7)]
        for index, path in enumerate(paths):
            path.write_text(f"old-{index}", encoding="utf-8")

        direct = files.write_file(None, str(paths[0]), "direct-change")
        shell = "; ".join(f"printf shell-{i} > f{i}.txt" for i in range(1, 7))
        result = run_command(
            self.settings,
            shell,
            reversible=True,
            reversible_root=str(self.work),
            join_transaction_ids=[direct["transaction_id"]],
        )
        self.assertEqual("compound", result["transaction_kind"])
        self.assertEqual(2, len(result["child_transaction_ids"]))
        self.assertEqual(7, result["path_count"])

        files.undo_file_transaction(None, result["transaction_id"])
        for index, path in enumerate(paths):
            self.assertEqual(f"old-{index}", path.read_text())

    def test_undo_conflict_preserves_newer_user_edit(self) -> None:
        target = self.work / "note.txt"
        target.write_text("before", encoding="utf-8")
        result = run_command(
            self.settings,
            "printf shell > note.txt",
            reversible=True,
            reversible_root=str(self.work),
        )
        target.write_text("newer-user-edit", encoding="utf-8")
        with self.assertRaises(HTTPException) as ctx:
            files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("transaction_conflict", ctx.exception.detail["error"])
        self.assertEqual("newer-user-edit", target.read_text())
        forced = files.undo_file_transaction(None, result["transaction_id"], force=True)
        self.assertEqual("undone", forced["transaction_state"])
        self.assertEqual("before", target.read_text())

    def test_capture_limit_non_strict_runs_and_reports_partial(self) -> None:
        large = self.work / "large.txt"
        large.write_bytes(b"12345678")
        normal = self.work / "normal.txt"
        normal.write_text("b", encoding="utf-8")
        with patch.dict(os.environ, {"MAC_MCP_SHELL_CAPTURE_MAX_FILE_BYTES": "4"}, clear=False):
            result = run_command(
                self.settings,
                "printf a > normal.txt; printf changed > large.txt",
                reversible=True,
                reversible_root=str(self.work),
                require_full_reversibility=False,
            )
        self.assertTrue(result["ok"])
        self.assertEqual("partial", result["reversibility"])
        self.assertFalse(result["full_reversibility_met"])
        self.assertTrue(any("large.txt" in str(row.get("path")) for row in result["unsupported"]))
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("b", normal.read_text())
        self.assertEqual("changed", large.read_text())

    def test_capture_limit_strict_fails_before_command_runs(self) -> None:
        (self.work / "large.txt").write_bytes(b"12345678")
        with patch.dict(os.environ, {"MAC_MCP_SHELL_CAPTURE_MAX_FILE_BYTES": "4"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                run_command(
                    self.settings,
                    "touch should-not-exist.txt",
                    reversible=True,
                    reversible_root=str(self.work),
                    require_full_reversibility=True,
                )
        self.assertEqual(409, ctx.exception.status_code)
        self.assertFalse((self.work / "should-not-exist.txt").exists())
        self.assertFalse(ctx.exception.detail["action_executed"])

    def test_generated_directory_is_explicit_partial_coverage(self) -> None:
        target = self.work / "tracked.txt"
        target.write_text("before", encoding="utf-8")
        generated = self.work / "node_modules"
        generated.mkdir()
        (generated / "cache.txt").write_text("cache-before", encoding="utf-8")

        result = run_command(
            self.settings,
            "printf after > tracked.txt; printf cache-after > node_modules/cache.txt",
            reversible=True,
            reversible_root=str(self.work),
        )
        self.assertEqual("partial", result["reversibility"])
        self.assertFalse(result["full_reversibility_met"])
        self.assertTrue(result["unsupported"])
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("before", target.read_text())
        self.assertEqual("cache-after", (generated / "cache.txt").read_text())

    def test_git_head_change_is_explicit_partial_and_file_preimage_still_restores(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.work, check=True)
        target = self.work / "tracked.txt"
        target.write_text("base", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.work, check=True)
        base_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.work, text=True).strip()

        result = run_command(
            self.settings,
            "printf after > tracked.txt; git add tracked.txt; git commit -qm inside-run",
            reversible=True,
            reversible_root=str(self.work),
        )
        self.assertEqual("partial", result["reversibility"])
        reasons = {str(row.get("reason")) for row in result["unsupported"]}
        self.assertIn("git_head_changed", reasons)
        new_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.work, text=True).strip()
        self.assertNotEqual(base_head, new_head)

        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("base", target.read_text())
        self.assertEqual(new_head, subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.work, text=True).strip())

    def test_compound_children_are_not_pruned_while_compound_is_live(self) -> None:
        target_a = self.work / "a.txt"
        target_b = self.work / "b.txt"
        target_a.write_text("a0", encoding="utf-8")
        target_b.write_text("b0", encoding="utf-8")
        with patch.dict(os.environ, {"MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS": "2"}, clear=False):
            direct = files.write_file(None, str(target_a), "a1")
            result = run_command(
                self.settings,
                "printf b1 > b.txt",
                reversible=True,
                reversible_root=str(self.work),
                join_transaction_ids=[direct["transaction_id"]],
            )
            manifest = journal.get_transaction(result["transaction_id"])
            children = list(manifest["child_transaction_ids"])
            self.assertEqual(2, len(children))
            for txid in children:
                self.assertEqual("committed", journal.get_transaction(txid)["state"])
            files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("a0", target_a.read_text())
        self.assertEqual("b0", target_b.read_text())

    def test_git_clean_dirty_and_new_files_restore_without_reset_or_clean(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.work, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.work, check=True)
        clean = self.work / "clean.txt"
        dirty = self.work / "dirty.txt"
        clean.write_text("clean-base", encoding="utf-8")
        dirty.write_text("dirty-base", encoding="utf-8")
        subprocess.run(["git", "add", "clean.txt", "dirty.txt"], cwd=self.work, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.work, check=True)
        dirty.write_text("dirty-before-shell", encoding="utf-8")

        result = run_command(
            self.settings,
            "printf clean-after > clean.txt; printf dirty-after > dirty.txt; printf new > new.txt",
            reversible=True,
            reversible_root=str(self.work),
        )
        self.assertEqual("git", result["capture_mode"])
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("clean-base", clean.read_text())
        self.assertEqual("dirty-before-shell", dirty.read_text())
        self.assertFalse((self.work / "new.txt").exists())
        self.assertEqual("dirty-before-shell", dirty.read_text())

    def test_scoped_symlink_escape_is_not_journaled(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside-before", encoding="utf-8")
        link = self.work / "escape"
        link.symlink_to(outside)
        context = PolicyContext(
            profile="developer",
            actor="agent:test",
            agent_id="agt-test",
            scope=ResourceScope(
                path_roots=(str(self.work),),
                tool_families=("terminal", "files"),
                access_mode="workspace_write",
            ),
        )
        token = set_policy_context(context)
        try:
            result = run_command(
                self.settings,
                "printf inside > local.txt",
                reversible=True,
                reversible_root=str(self.work),
            )
        finally:
            reset_policy_context(token)
        manifest = journal.get_transaction(result["transaction_id"])
        journal_paths = [str(row.get("path")) for row in manifest.get("snapshots") or []]
        self.assertNotIn(str(link), journal_paths)
        self.assertEqual("outside-before", outside.read_text())

    def test_background_job_finalizes_reversible_capture_and_undo(self) -> None:
        target = self.work / "job.txt"
        target.write_text("before", encoding="utf-8")
        old_jobs_dir = tools_jobs.JOBS_DIR
        tools_jobs.JOBS_DIR = self.root / "jobs"
        try:
            started = tools_jobs.start_background_job(
                self.settings,
                "printf after > job.txt",
                cwd=str(self.work),
                timeout_s=20,
                reversible=True,
                reversible_root=str(self.work),
            )
            waited = tools_jobs.wait_jobs(
                self.settings, [started["job_id"]], timeout_s=20, return_output=False,
            )
            row = waited["jobs"][0]
            self.assertEqual("completed", row["status"])
            self.assertEqual("captured", row["filesystem_outcome"])
            self.assertTrue(row["undoable"])
            self.assertIn(row["reversibility"], {"full", "partial"})
            self.assertTrue(row["transaction_id"])
            files.undo_file_transaction(None, row["transaction_id"])
            self.assertEqual("before", target.read_text())
        finally:
            tools_jobs.JOBS_DIR = old_jobs_dir

    def test_background_job_dead_pid_recovery_finalizes_durable_capture(self) -> None:
        target = self.work / "recover.txt"
        target.write_text("before", encoding="utf-8")
        old_jobs_dir = tools_jobs.JOBS_DIR
        tools_jobs.JOBS_DIR = self.root / "jobs-recover"
        try:
            capture = begin_shell_capture(self.work)
            target.write_text("after", encoding="utf-8")
            job_id = "deadjob12345"
            job_dir = tools_jobs.JOBS_DIR / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "stdout.log").write_text("", encoding="utf-8")
            (job_dir / "stderr.log").write_text("", encoding="utf-8")
            now = tools_jobs._now()
            tools_jobs._write_meta(job_id, {
                "job_id": job_id,
                "command": "simulated",
                "cwd": str(self.work),
                "pid": 99999999,
                "status": "running",
                "exit_code": None,
                "started_at": now - 1,
                "updated_at": now - 1,
                "last_output_at": now - 1,
                "ended_at": None,
                "timeout_s": 20,
                "no_output_timeout_s": None,
                "sandboxed": False,
                "reversible_capture": True,
                "capture_transaction_id": capture["transaction_id"],
                "join_transaction_ids": [],
                "require_full_reversibility": False,
                "transaction": None,
                "transaction_id": None,
                "undoable": None,
                "reversibility": None,
                "filesystem_outcome": "capturing",
                "reversibility_error": None,
            })
            row = tools_jobs.get_job_status(self.settings, job_id)
            self.assertEqual("failed", row["status"])
            self.assertEqual("captured", row["filesystem_outcome"])
            self.assertTrue(row["transaction_id"])
            files.undo_file_transaction(None, row["transaction_id"])
            self.assertEqual("before", target.read_text())
        finally:
            tools_jobs.JOBS_DIR = old_jobs_dir

    def test_empty_directory_create_delete_rename_round_trips(self) -> None:
        old_empty = self.work / "old-empty"
        rename_empty = self.work / "rename-empty"
        old_empty.mkdir()
        rename_empty.mkdir()
        result = run_command(
            self.settings,
            "rmdir old-empty; mv rename-empty renamed-empty; mkdir created-empty",
            reversible=True,
            reversible_root=str(self.work),
        )
        self.assertFalse(old_empty.exists())
        self.assertFalse(rename_empty.exists())
        self.assertTrue((self.work / "renamed-empty").is_dir())
        self.assertTrue((self.work / "created-empty").is_dir())
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertTrue(old_empty.is_dir())
        self.assertTrue(rename_empty.is_dir())
        self.assertFalse((self.work / "renamed-empty").exists())
        self.assertFalse((self.work / "created-empty").exists())

    def test_compound_overlap_chain_undoes_same_file_across_file_and_shell_transactions(self) -> None:
        target = self.work / "overlap.txt"
        target.write_text("original", encoding="utf-8")
        direct = files.write_file(None, str(target), "direct")
        result = run_command(
            self.settings,
            "printf shell > overlap.txt",
            reversible=True,
            reversible_root=str(self.work),
            join_transaction_ids=[direct["transaction_id"]],
        )
        self.assertEqual("shell", target.read_text())
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("original", target.read_text())

    def test_incomplete_capture_state_is_explicit_unknown_not_false_undo_success(self) -> None:
        target = self.work / "a.txt"
        target.write_text("before", encoding="utf-8")
        capture = begin_shell_capture(self.work)
        target.write_text("after-crash", encoding="utf-8")
        manifest = journal.get_transaction(capture["transaction_id"])
        self.assertEqual("capturing", manifest["state"])
        with self.assertRaises(HTTPException) as ctx:
            files.undo_file_transaction(None, capture["transaction_id"])
        self.assertEqual(409, ctx.exception.status_code)
        self.assertIn("outcome is unknown", ctx.exception.detail["message"])
        with self.assertRaises(HTTPException) as force_ctx:
            files.undo_file_transaction(None, capture["transaction_id"], force=True)
        self.assertEqual(409, force_ctx.exception.status_code)
        self.assertEqual("transaction_irreversible", force_ctx.exception.detail["error"])
        self.assertEqual("after-crash", target.read_text())


if __name__ == "__main__":
    unittest.main()
