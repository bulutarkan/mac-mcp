"""#101: scoped_move falls back to a plain rename only when the destination is not a directory.

Opening the destination as a directory could fail for any reason; only three reasons were known, a few
more were re-raised, and anything else (permission_denied, say) went on to rename the source onto the
destination path. An empty folder the agent may not open was then replaced by the source folder.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.scoped_fs as scoped_fs
from mcp_server.policy_scope import ResourceScope


class ScopedMoveGuardErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-scoped-move-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.scope = ResourceScope(path_roots=(str(self.root),), tool_families=("files",), access_mode="workspace_write")

    def test_a_folder_it_may_not_open_is_never_replaced(self) -> None:
        source = self.root / "drafts"
        source.mkdir()
        (source / "a.txt").write_text("draft")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        with self.assertRaises(scoped_fs.ScopedFilesystemError) as refused:
            scoped_fs.scoped_move(self.scope, source, locked)
        self.assertEqual("permission_denied", refused.exception.reason)
        locked.chmod(0o755)
        self.assertTrue((source / "a.txt").exists(), "the source stays where it was")
        self.assertEqual([], os.listdir(locked))

    def test_an_unknown_reason_stops_the_move(self) -> None:
        source = self.root / "note.txt"
        source.write_text("x")
        with patch.object(scoped_fs, "guarded_directory",
                          side_effect=scoped_fs.ScopedFilesystemError("something_new", "unexpected")):
            with self.assertRaises(scoped_fs.ScopedFilesystemError):
                scoped_fs.scoped_move(self.scope, source, self.root / "elsewhere.txt")
        self.assertTrue(source.exists())

    def test_moves_still_work(self) -> None:
        folder = self.root / "box"
        folder.mkdir()
        first = self.root / "one.txt"
        first.write_text("1")
        self.assertEqual(folder / "one.txt", scoped_fs.scoped_move(self.scope, first, folder))
        second = self.root / "two.txt"
        second.write_text("2")
        self.assertEqual(self.root / "renamed.txt", scoped_fs.scoped_move(self.scope, second, self.root / "renamed.txt"))
        self.assertEqual("2", (self.root / "renamed.txt").read_text())


if __name__ == "__main__":
    unittest.main()
