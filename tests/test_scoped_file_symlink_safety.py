from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import mcp_server.scoped_fs as scoped_fs
import mcp_server.tools_files as tools_files
import mcp_server.tools_search as tools_search
from mcp_server.policy import PolicyContext, evaluate_tool_scope, reset_policy_context, set_policy_context
from mcp_server.policy_scope import ResourceScope


class ScopedFileSymlinkSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-scope-race-")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.outside = self.root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        self.state = self.root / "state"
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state)}, clear=False)
        self.env.start()
        self.scope = ResourceScope(
            path_roots=(str(self.workspace),),
            tool_families=("files", "search"),
            access_mode="workspace_write",
        )

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    @contextmanager
    def scoped(self):
        token = set_policy_context(
            PolicyContext(profile="developer", actor="agent:race-test", agent_id="agt_race", scope=self.scope)
        )
        try:
            yield
        finally:
            reset_policy_context(token)

    def _make_slot(self, name: str = "slot") -> tuple[Path, Path]:
        slot = self.workspace / name
        parked = self.workspace / f"{name}-parked"
        slot.mkdir()
        return slot, parked

    def _swap_to_outside(self, slot: Path, parked: Path) -> None:
        os.rename(slot, parked)
        os.symlink(self.outside, slot)

    def test_normal_scoped_read_write_delete_search_and_undo(self) -> None:
        target = self.workspace / "note.txt"
        target.write_text("old needle\n", encoding="utf-8")
        with self.scoped():
            read = tools_files.read_file(None, str(target))
            self.assertEqual("old needle\n", read["content"])
            write = tools_files.write_file(None, str(target), "new needle\n")
            self.assertTrue(write["undoable"])
            self.assertEqual("new needle\n", target.read_text(encoding="utf-8"))
            search = tools_search.search_files(None, "needle", path=str(self.workspace))
            self.assertEqual(1, search["match_count"])
            self.assertIn("note.txt", search["results"])
            tools_files.undo_file_transaction(None, write["transaction_id"])
            self.assertEqual("old needle\n", target.read_text(encoding="utf-8"))
            deleted = tools_files.delete_path(None, str(target))
            self.assertFalse(target.exists())
            tools_files.undo_file_transaction(None, deleted["transaction_id"])
            self.assertEqual("old needle\n", target.read_text(encoding="utf-8"))

    def test_scoped_search_never_follows_symlink_child_to_outside_secret(self) -> None:
        (self.workspace / "inside.txt").write_text("public needle\n", encoding="utf-8")
        secret = self.outside / "secret.txt"
        secret.write_text("SENTINEL_SECRET needle\n", encoding="utf-8")
        os.symlink(self.outside, self.workspace / "linked-outside")
        with self.scoped():
            result = tools_search.search_files(None, "needle", path=str(self.workspace))
        self.assertEqual(1, result["match_count"])
        self.assertIn("inside.txt", result["results"])
        self.assertNotIn("SENTINEL_SECRET", result["results"])
        self.assertNotIn(str(secret), result["results"])

    def test_read_check_then_parent_symlink_swap_is_blocked(self) -> None:
        slot, parked = self._make_slot("read-slot")
        target = slot / "sentinel.txt"
        target.write_text("SAFE", encoding="utf-8")
        outside = self.outside / "sentinel.txt"
        outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
        self.assertTrue(evaluate_tool_scope(self.scope, "read_file", {"path": str(target)}).allowed)
        real_resolve = tools_files.resolve_path
        swapped = False

        def resolve_then_swap(value: str):
            nonlocal swapped
            resolved = real_resolve(value)
            if not swapped:
                self._swap_to_outside(slot, parked)
                swapped = True
            return resolved

        with self.scoped(), patch.object(tools_files, "resolve_path", side_effect=resolve_then_swap):
            with self.assertRaises(HTTPException) as ctx:
                tools_files.read_file(None, str(target))
        self.assertEqual(403, ctx.exception.status_code)
        self.assertEqual("OUTSIDE_SECRET", outside.read_text(encoding="utf-8"))

    def test_search_check_then_root_symlink_swap_is_blocked(self) -> None:
        slot, parked = self._make_slot("search-slot")
        (slot / "inside.txt").write_text("needle safe", encoding="utf-8")
        outside = self.outside / "outside.txt"
        outside.write_text("needle OUTSIDE_SECRET", encoding="utf-8")
        self.assertTrue(evaluate_tool_scope(self.scope, "search_files", {"path": str(slot)}).allowed)
        real_resolve = tools_search.resolve_path
        swapped = False

        def resolve_then_swap(value: str):
            nonlocal swapped
            resolved = real_resolve(value)
            if not swapped:
                self._swap_to_outside(slot, parked)
                swapped = True
            return resolved

        with self.scoped(), patch.object(tools_search, "resolve_path", side_effect=resolve_then_swap):
            with self.assertRaises(HTTPException) as ctx:
                tools_search.search_files(None, "needle", path=str(slot))
        self.assertEqual(403, ctx.exception.status_code)
        self.assertEqual("needle OUTSIDE_SECRET", outside.read_text(encoding="utf-8"))

    def test_write_swap_after_safe_snapshot_never_writes_outside(self) -> None:
        slot, parked = self._make_slot("write-slot")
        target = slot / "sentinel.txt"
        target.write_text("INSIDE_OLD", encoding="utf-8")
        outside = self.outside / "sentinel.txt"
        outside.write_text("OUTSIDE_OLD", encoding="utf-8")
        original_prepare = tools_files._prepare_file_transaction
        swapped = False

        def prepare_then_swap(*args, **kwargs):
            nonlocal swapped
            result = original_prepare(*args, **kwargs)
            if not swapped:
                self._swap_to_outside(slot, parked)
                swapped = True
            return result

        with self.scoped(), patch.object(tools_files, "_prepare_file_transaction", side_effect=prepare_then_swap):
            with self.assertRaises(HTTPException) as ctx:
                tools_files.write_file(None, str(target), "ATTACK")
        self.assertIn(ctx.exception.status_code, {403, 500})
        self.assertEqual("OUTSIDE_OLD", outside.read_text(encoding="utf-8"))
        self.assertFalse((self.outside / ".sentinel.txt.mac-mcp").exists())

    def test_delete_swap_after_safe_snapshot_never_deletes_outside(self) -> None:
        slot, parked = self._make_slot("delete-slot")
        target = slot / "sentinel.txt"
        target.write_text("INSIDE_OLD", encoding="utf-8")
        outside = self.outside / "sentinel.txt"
        outside.write_text("OUTSIDE_KEEP", encoding="utf-8")
        original_prepare = tools_files._prepare_file_transaction
        swapped = False

        def prepare_then_swap(*args, **kwargs):
            nonlocal swapped
            result = original_prepare(*args, **kwargs)
            if not swapped:
                self._swap_to_outside(slot, parked)
                swapped = True
            return result

        with self.scoped(), patch.object(tools_files, "_prepare_file_transaction", side_effect=prepare_then_swap):
            with self.assertRaises(HTTPException) as ctx:
                tools_files.delete_path(None, str(target))
        self.assertIn(ctx.exception.status_code, {403, 500})
        self.assertTrue(outside.exists())
        self.assertEqual("OUTSIDE_KEEP", outside.read_text(encoding="utf-8"))

    def test_scoped_directory_copy_keeps_copytree_existing_destination_failure(self) -> None:
        source = self.workspace / "source-dir"
        source.mkdir()
        (source / "a.txt").write_text("A", encoding="utf-8")
        destination = self.workspace / "existing-dir"
        destination.mkdir()
        with self.scoped():
            with self.assertRaises(HTTPException) as ctx:
                tools_files.copy_file(None, str(source), str(destination))
        self.assertEqual(409, ctx.exception.status_code)
        self.assertFalse((destination / "a.txt").exists())

    def test_scoped_copy_rejects_symlink_source_and_move_rejects_symlink_destination(self) -> None:
        source = self.workspace / "source-link"
        outside_file = self.outside / "outside.txt"
        outside_file.write_text("OUTSIDE", encoding="utf-8")
        os.symlink(outside_file, source)
        with self.scoped():
            with self.assertRaises(HTTPException):
                tools_files.copy_file(None, str(source), str(self.workspace / "copy.txt"))

        real_source = self.workspace / "real.txt"
        real_source.write_text("REAL", encoding="utf-8")
        dest_link = self.workspace / "dest-link"
        os.symlink(self.outside, dest_link)
        with self.scoped():
            with self.assertRaises(HTTPException):
                tools_files.move_file(None, str(real_source), str(dest_link))
        self.assertTrue(real_source.exists())
        self.assertEqual("OUTSIDE", outside_file.read_text(encoding="utf-8"))

    def test_descriptor_guard_blocks_swap_after_operation_time_revalidation(self) -> None:
        slot, parked = self._make_slot("fd-race-slot")
        target = slot / "sentinel.txt"
        target.write_text("SAFE", encoding="utf-8")
        outside = self.outside / "sentinel.txt"
        outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
        real_matching = scoped_fs._matching_root
        swapped = False

        def validate_then_swap(scope, path, access_mode):
            nonlocal swapped
            root = real_matching(scope, path, access_mode)
            if not swapped:
                self._swap_to_outside(slot, parked)
                swapped = True
            return root

        with self.scoped(), patch.object(scoped_fs, "_matching_root", side_effect=validate_then_swap):
            with self.assertRaises(HTTPException) as ctx:
                tools_files.read_file(None, str(target))
        self.assertEqual(403, ctx.exception.status_code)
        self.assertEqual("OUTSIDE_SECRET", outside.read_text(encoding="utf-8"))

    def test_read_multiple_fails_closed_instead_of_leaking_swapped_secret(self) -> None:
        safe = self.workspace / "safe.txt"
        safe.write_text("SAFE", encoding="utf-8")
        slot, parked = self._make_slot("multi-slot")
        target = slot / "secret.txt"
        target.write_text("INSIDE", encoding="utf-8")
        outside = self.outside / "secret.txt"
        outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
        real_matching = scoped_fs._matching_root
        calls = 0

        def swap_on_target(scope, path, access_mode):
            nonlocal calls
            root = real_matching(scope, path, access_mode)
            calls += 1
            if calls == 2:
                self._swap_to_outside(slot, parked)
            return root

        with self.scoped(), patch.object(scoped_fs, "_matching_root", side_effect=swap_on_target):
            with self.assertRaises(HTTPException) as ctx:
                tools_files.read_multiple_files(None, [str(safe), str(target)])
        self.assertEqual(403, ctx.exception.status_code)
        self.assertNotIn("OUTSIDE_SECRET", str(ctx.exception.detail))

    def test_find_tree_list_and_info_do_not_traverse_outside_symlink(self) -> None:
        inside_dir = self.workspace / "inside"
        inside_dir.mkdir()
        (inside_dir / "visible.txt").write_text("visible", encoding="utf-8")
        secret = self.outside / "secret.txt"
        secret.write_text("OUTSIDE_SECRET", encoding="utf-8")
        os.symlink(self.outside, self.workspace / "outside-link")
        with self.scoped():
            found = tools_files.find_files(None, "*.txt", str(self.workspace), "file")
            tree = tools_files.directory_tree(None, str(self.workspace), depth=3)
            listing = tools_files.list_directory(None, str(self.workspace))
            with self.assertRaises(HTTPException) as info_ctx:
                tools_files.get_file_info(None, str(self.workspace / "outside-link"))
        self.assertEqual(1, found["count"])
        self.assertIn("visible.txt", str(found))
        self.assertNotIn(str(secret), str(found))
        self.assertNotIn("OUTSIDE_SECRET", str(tree))
        link_rows = [row for row in listing["entries"] if row["name"] == "outside-link"]
        self.assertEqual("symlink", link_rows[0]["type"])
        self.assertEqual(403, info_ctx.exception.status_code)

    def test_scoped_copy_preserves_existing_directory_and_file_destination_semantics(self) -> None:
        source = self.workspace / "copy-source.txt"
        source.write_text("NEW", encoding="utf-8")
        dest_dir = self.workspace / "dest"
        dest_dir.mkdir()
        (dest_dir / source.name).write_text("OLD", encoding="utf-8")
        with self.scoped():
            result = tools_files.copy_file(None, str(source), str(dest_dir))
        self.assertEqual(str((dest_dir / source.name).resolve()), str(Path(result["destination"]).resolve()))
        self.assertEqual("NEW", (dest_dir / source.name).read_text(encoding="utf-8"))

    def test_move_destination_parent_swap_after_revalidation_never_moves_outside(self) -> None:
        source = self.workspace / "move-source.txt"
        source.write_text("SOURCE", encoding="utf-8")
        dest_slot, parked = self._make_slot("move-dest")
        destination = dest_slot / "moved.txt"
        outside_destination = self.outside / "moved.txt"
        real_matching = scoped_fs._matching_root
        swapped = False

        def validate_then_swap(scope, path, access_mode):
            nonlocal swapped
            root = real_matching(scope, path, access_mode)
            if str(path).endswith("/move-dest/moved.txt") and not swapped:
                self._swap_to_outside(dest_slot, parked)
                swapped = True
            return root

        with self.scoped(), patch.object(scoped_fs, "_matching_root", side_effect=validate_then_swap):
            with self.assertRaises(HTTPException):
                tools_files.move_file(None, str(source), str(destination))
        self.assertTrue(source.exists())
        self.assertFalse(outside_destination.exists())

    def test_unscoped_historical_symlink_read_behavior_is_unchanged(self) -> None:
        outside = self.outside / "legacy.txt"
        outside.write_text("legacy", encoding="utf-8")
        link = self.workspace / "legacy-link.txt"
        os.symlink(outside, link)
        result = tools_files.read_file(None, str(link))
        self.assertEqual("legacy", result["content"])


if __name__ == "__main__":
    unittest.main()
