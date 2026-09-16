from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.file_transactions as journal
import mcp_server.tools_files as files
from mcp_server.policy import PolicyContext, evaluate_tool_scope, reset_policy_context, resolve_risk, set_policy_context
from mcp_server.policy_scope import ResourceScope


class FileTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-file-tx-")
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_STATE_DIR": str(self.state),
                "MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS": "64",
                "MAC_MCP_FILE_JOURNAL_MAX_BYTES": str(64 * 1024 * 1024),
                "MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES": str(16 * 1024 * 1024),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_write_and_edit_are_reversible_without_content_in_manifest(self) -> None:
        target = self.work / "note.txt"
        target.write_bytes(b"before\n")
        result = files.write_file(None, str(target), "secret replacement\n")
        self.assertTrue(result["undoable"])
        self.assertEqual(b"secret replacement\n", target.read_bytes())
        transaction_id = result["transaction_id"]
        manifest_path = journal.journal_root() / transaction_id / "manifest.json"
        manifest_text = manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("secret replacement", manifest_text)
        self.assertEqual(0o600, stat.S_IMODE(manifest_path.stat().st_mode))
        snapshot = next((journal.journal_root() / transaction_id / "snapshots").glob("*.tar"))
        self.assertEqual(0o600, stat.S_IMODE(snapshot.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(journal.journal_root().stat().st_mode))

        undone = files.undo_file_transaction(None, transaction_id)
        self.assertEqual("undone", undone["transaction_state"])
        self.assertEqual(b"before\n", target.read_bytes())

        edited = files.edit_file(None, str(target), "before", "after")
        self.assertEqual(b"after\n", target.read_bytes())
        files.undo_file_transaction(None, edited["transaction_id"])
        self.assertEqual(b"before\n", target.read_bytes())

    def test_atomic_write_preserves_existing_file_mode(self) -> None:
        target = self.work / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        target.chmod(0o755)
        result = files.write_file(None, str(target), "#!/bin/sh\necho new\n")
        self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))

    def test_new_nested_write_undo_removes_created_parent_tree(self) -> None:
        target = self.work / "new" / "deep" / "file.txt"
        result = files.write_file(None, str(target), "new")
        self.assertTrue(target.exists())
        self.assertTrue((self.work / "new").exists())
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertFalse((self.work / "new").exists())

    def test_move_undo_restores_source_and_existing_destination_bytes(self) -> None:
        src = self.work / "source.bin"
        dst = self.work / "destination.bin"
        src.write_bytes(b"SOURCE-BYTES")
        dst.write_bytes(b"DESTINATION-BYTES")
        result = files.move_file(None, str(src), str(dst))
        self.assertFalse(src.exists())
        self.assertEqual(b"SOURCE-BYTES", dst.read_bytes())
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual(b"SOURCE-BYTES", src.read_bytes())
        self.assertEqual(b"DESTINATION-BYTES", dst.read_bytes())

    def test_delete_file_and_directory_restore_byte_for_byte(self) -> None:
        target = self.work / "delete-me.bin"
        original = bytes(range(256)) * 20
        target.write_bytes(original)
        deleted = files.delete_path(None, str(target))
        self.assertFalse(target.exists())
        files.undo_file_transaction(None, deleted["transaction_id"])
        self.assertEqual(original, target.read_bytes())

        directory = self.work / "tree"
        (directory / "nested").mkdir(parents=True)
        (directory / "a.bin").write_bytes(b"A\x00B\xff")
        (directory / "nested" / "b.bin").write_bytes(b"\x01\x02\x03")
        expected = {
            "a.bin": (directory / "a.bin").read_bytes(),
            "nested/b.bin": (directory / "nested" / "b.bin").read_bytes(),
        }
        removed = files.delete_path(None, str(directory), recursive=True)
        self.assertFalse(directory.exists())
        files.undo_file_transaction(None, removed["transaction_id"])
        self.assertEqual(expected["a.bin"], (directory / "a.bin").read_bytes())
        self.assertEqual(expected["nested/b.bin"], (directory / "nested" / "b.bin").read_bytes())

    def test_atomic_batch_fault_in_second_write_restores_all_preimages(self) -> None:
        first = self.work / "first.txt"
        second = self.work / "second.txt"
        first.write_bytes(b"FIRST-OLD")
        second.write_bytes(b"SECOND-OLD")
        real_write = files._write_text_atomic
        calls = {"n": 0}

        def fail_second(target: Path, content: str) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected second-write failure")
            return real_write(target, content)

        with patch.object(files, "_write_text_atomic", side_effect=fail_second):
            with self.assertRaises(HTTPException) as ctx:
                files.write_files_batch(
                    None,
                    [{"path": str(first), "content": "FIRST-NEW"}, {"path": str(second), "content": "SECOND-NEW"}],
                    atomic=True,
                )
        self.assertEqual(500, ctx.exception.status_code)
        self.assertTrue(ctx.exception.detail["rolled_back"])
        self.assertEqual(b"FIRST-OLD", first.read_bytes())
        self.assertEqual(b"SECOND-OLD", second.read_bytes())
        txid = ctx.exception.detail["transaction_id"]
        self.assertEqual("rolled_back", journal.get_transaction(txid)["state"])

    def test_mixed_write_move_delete_batch_fault_rolls_back_everything(self) -> None:
        write_target = self.work / "write.txt"
        move_source = self.work / "move-source.bin"
        move_destination = self.work / "move-destination.bin"
        delete_target = self.work / "delete.bin"
        write_target.write_bytes(b"WRITE-OLD")
        move_source.write_bytes(b"MOVE-SOURCE-OLD")
        move_destination.write_bytes(b"MOVE-DEST-OLD")
        delete_target.write_bytes(b"DELETE-OLD")

        def fail_delete(target: Path, *, recursive: bool):
            raise OSError("injected delete failure")

        with patch.object(files, "_delete_raw", side_effect=fail_delete):
            with self.assertRaises(HTTPException) as ctx:
                files.file_transaction_batch(
                    None,
                    [
                        {"type": "write", "path": str(write_target), "content": "WRITE-NEW"},
                        {"type": "move", "source": str(move_source), "destination": str(move_destination)},
                        {"type": "delete", "path": str(delete_target)},
                    ],
                )
        self.assertEqual(500, ctx.exception.status_code)
        self.assertTrue(ctx.exception.detail["rolled_back"])
        self.assertEqual(b"WRITE-OLD", write_target.read_bytes())
        self.assertEqual(b"MOVE-SOURCE-OLD", move_source.read_bytes())
        self.assertEqual(b"MOVE-DEST-OLD", move_destination.read_bytes())
        self.assertEqual(b"DELETE-OLD", delete_target.read_bytes())
        self.assertEqual("rolled_back", journal.get_transaction(ctx.exception.detail["transaction_id"])["state"])

    def test_mixed_batch_success_can_be_undone_as_one_transaction(self) -> None:
        write_target = self.work / "batch-write.txt"
        move_source = self.work / "batch-source.txt"
        move_destination = self.work / "batch-destination.txt"
        delete_target = self.work / "batch-delete.txt"
        write_target.write_bytes(b"W0")
        move_source.write_bytes(b"S0")
        move_destination.write_bytes(b"D0")
        delete_target.write_bytes(b"X0")
        result = files.file_transaction_batch(
            None,
            [
                {"type": "write", "path": str(write_target), "content": "W1"},
                {"type": "move", "source": str(move_source), "destination": str(move_destination)},
                {"type": "delete", "path": str(delete_target)},
            ],
        )
        self.assertEqual(3, result["action_count"])
        self.assertEqual(b"W1", write_target.read_bytes())
        self.assertFalse(move_source.exists())
        self.assertEqual(b"S0", move_destination.read_bytes())
        self.assertFalse(delete_target.exists())
        files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual(b"W0", write_target.read_bytes())
        self.assertEqual(b"S0", move_source.read_bytes())
        self.assertEqual(b"D0", move_destination.read_bytes())
        self.assertEqual(b"X0", delete_target.read_bytes())

    def test_undo_refuses_to_overwrite_newer_change_unless_forced(self) -> None:
        target = self.work / "conflict.txt"
        target.write_text("v1", encoding="utf-8")
        result = files.write_file(None, str(target), "v2")
        target.write_text("v3-user-change", encoding="utf-8")
        with self.assertRaises(HTTPException) as ctx:
            files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("transaction_conflict", ctx.exception.detail["error"])
        self.assertEqual("v3-user-change", target.read_text(encoding="utf-8"))
        files.undo_file_transaction(None, result["transaction_id"], force=True)
        self.assertEqual("v1", target.read_text(encoding="utf-8"))

    def test_atomic_batch_refuses_irreversible_snapshot_before_mutation(self) -> None:
        target = self.work / "large.txt"
        target.write_bytes(b"X" * 2048)
        with patch.dict(os.environ, {"MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES": "1024"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                files.write_files_batch(None, [{"path": str(target), "content": "changed"}], atomic=True)
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("transaction_irreversible", ctx.exception.detail["error"])
        self.assertFalse(ctx.exception.detail["action_executed"])
        self.assertEqual(b"X" * 2048, target.read_bytes())

    def test_single_large_action_is_clearly_marked_irreversible(self) -> None:
        target = self.work / "large-single.txt"
        target.write_bytes(b"Y" * 2048)
        with patch.dict(os.environ, {"MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES": "1024"}, clear=False):
            result = files.write_file(None, str(target), "changed")
        self.assertFalse(result["undoable"])
        self.assertEqual("snapshot_limit_exceeded", result["irreversible_reason"])
        with self.assertRaises(HTTPException) as ctx:
            files.undo_file_transaction(None, result["transaction_id"])
        self.assertEqual("transaction_irreversible", ctx.exception.detail["error"])

    def test_scoped_agent_cannot_undo_transaction_outside_scope(self) -> None:
        outside = self.work / "outside.txt"
        outside.write_text("old", encoding="utf-8")
        result = files.write_file(None, str(outside), "new")
        allowed = self.root / "allowed"
        allowed.mkdir()
        token = set_policy_context(
            PolicyContext(
                profile="developer", actor="agent:scoped", agent_id="agt_scoped",
                scope=ResourceScope(path_roots=(str(allowed),), tool_families=("files",), access_mode="workspace_write"),
            )
        )
        try:
            with self.assertRaises(HTTPException) as ctx:
                files.undo_file_transaction(None, result["transaction_id"])
            self.assertEqual(403, ctx.exception.status_code)
            self.assertEqual("scope_denied", ctx.exception.detail["error"])
        finally:
            reset_policy_context(token)
        self.assertEqual("new", outside.read_text(encoding="utf-8"))

    def test_prepared_crash_state_requires_force_to_restore_preimage(self) -> None:
        target = self.work / "crash-window.txt"
        target.write_bytes(b"BEFORE")
        prepared = journal.prepare_transaction("crash-test", [target])
        target.write_bytes(b"AFTER-BUT-NOT-COMMITTED")
        with self.assertRaises(HTTPException) as ctx:
            files.undo_file_transaction(None, prepared["transaction_id"])
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual(b"AFTER-BUT-NOT-COMMITTED", target.read_bytes())
        files.undo_file_transaction(None, prepared["transaction_id"], force=True)
        self.assertEqual(b"BEFORE", target.read_bytes())

    def test_mixed_batch_scope_checks_nested_action_paths(self) -> None:
        allowed = self.work / "allowed"
        outside = self.work / "outside.txt"
        allowed.mkdir()
        scope = ResourceScope(
            path_roots=(str(allowed),), tool_families=("files",), access_mode="workspace_write"
        )
        arguments = {
            "actions": [
                {"type": "write", "path": str(allowed / "ok.txt"), "content": "ok"},
                {"type": "delete", "path": str(outside)},
            ]
        }
        risk = resolve_risk("file_transaction_batch", arguments)[1]
        decision = evaluate_tool_scope(scope, "file_transaction_batch", arguments, risk)
        self.assertFalse(decision.allowed)
        self.assertIn("path_not_allowed", decision.reasons)

    def test_startup_prune_removes_expired_transaction_and_noops_without_store(self) -> None:
        target = self.work / "expired.txt"
        target.write_text("old", encoding="utf-8")
        result = files.write_file(None, str(target), "new")
        manifest = journal.get_transaction(result["transaction_id"])
        manifest["expires_at"] = journal._now() - 1
        journal._write_manifest(manifest)
        journal.prune_transactions()
        self.assertFalse((journal.journal_root() / result["transaction_id"]).exists())

        empty_state = self.root / "empty-state"
        with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(empty_state)}, clear=False):
            journal.prune_transactions()
            self.assertFalse((empty_state / "transactions").exists())

    def test_retention_prunes_oldest_committed_transactions(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS": "2"}, clear=False):
            ids = []
            for index in range(3):
                target = self.work / f"retention-{index}.txt"
                target.write_text(f"before-{index}", encoding="utf-8")
                result = files.write_file(None, str(target), f"after-{index}")
                ids.append(result["transaction_id"])
            tx_dirs = sorted(p.name for p in journal.journal_root().iterdir() if p.is_dir() and p.name.startswith("ftx_"))
        self.assertEqual(2, len(tx_dirs))
        self.assertNotIn(ids[0], tx_dirs)
        self.assertIn(ids[1], tx_dirs)
        self.assertIn(ids[2], tx_dirs)


if __name__ == "__main__":
    unittest.main()
