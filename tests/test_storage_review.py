"""#72: what takes room in a folder, and moving the items the owner chose to the Trash.

Mac MCP could list and delete files but had no way to say what takes room and why. storage_review scan
reads a folder (default ~/Downloads) and lists large, long-unchanged and duplicate files with reasons,
leaving links, hidden files, packages and system folders alone. move_to_trash takes only ids from a scan,
skips files that changed since, and uses macOS's Trash so Finder's Put Back works; undo moves them back
or, when the Trash cannot be opened, names what to Put Back. The real Trash is never touched here.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import storage_review as review
from mcp_server.change_summary import change_items_for_event
from mcp_server.policy import PolicyContext, RISK_REGISTRY, reset_policy_context, resolve_risk, set_policy_context
from mcp_server.policy_scope import ResourceScope

MB = 1024 * 1024


class StorageReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-storage-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.folder = base / "Downloads"
        self.trash = base / "Trash"
        self.folder.mkdir()
        self.trash.mkdir()
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time()

    def file(self, name: str, size_mb: float, *, days_old: int = 0, fill: bytes = b"x") -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fill * int(size_mb * MB))
        stamp = self.now - days_old * 86400
        os.utime(path, (stamp, stamp))
        return path

    def fake_trash(self, args, **_kwargs):
        source = Path(args[-1])
        dest = self.trash / source.name
        os.rename(source, dest)
        return subprocess.CompletedProcess(args, 0, stdout=f'# Moved "{source}" to "{dest}"\n', stderr="")

    def scan(self, **kwargs):
        return review.scan(str(self.folder), min_size_mb=5, older_than_days=30, now=self.now, **kwargs)

    def test_scan_lists_large_old_and_duplicate_files_with_reasons(self) -> None:
        self.file("movie.mov", 8)
        self.file("installer.dmg", 2, days_old=400)
        first = self.file("report.pdf", 2, days_old=10, fill=b"a")
        self.file("copies/report (1).pdf", 2, fill=b"a")
        self.file("small.txt", 0.1, days_old=900)  # old but tiny: not worth listing
        self.file("same-size.bin", 2, fill=b"b")  # same size, other content: not a duplicate
        result = self.scan()
        reasons = {item["name"]: item["reasons"] for item in result["items"]}
        self.assertEqual({"movie.mov": ["large"], "installer.dmg": ["old"], "report (1).pdf": ["duplicate"]}, reasons)
        self.assertEqual("movie.mov", result["items"][0]["name"])  # largest first
        self.assertEqual(1, len(result["duplicate_groups"]))
        self.assertEqual(str(first), result["duplicate_groups"][0]["keep_suggested"])  # the older copy stays
        self.assertTrue(result["read_only"])
        self.assertEqual(6, result["scanned_files"])
        self.assertTrue((self.folder / "movie.mov").exists())

    def test_links_hidden_files_and_packages_are_left_out(self) -> None:
        self.file(".hidden.iso", 8)
        self.file("Tool.app/Contents/big.bin", 8)
        target = self.file("real.bin", 8)
        (self.folder / "link.bin").symlink_to(target)
        result = self.scan()
        self.assertEqual(["real.bin"], [item["name"] for item in result["items"]])
        self.assertEqual((1, 1, 1), (result["skipped"]["hidden"], result["skipped"]["packages"], result["skipped"]["links"]))

    def test_system_folders_and_library_are_refused(self) -> None:
        for folder in ("/System", "/usr/local", "/Applications", str(Path.home() / "Library"), "/"):
            with self.subTest(folder=folder), self.assertRaises(HTTPException) as refused:
                review.scan(folder)
            self.assertIn(refused.exception.status_code, {400, 404})

    def test_an_agent_scope_limits_the_folder(self) -> None:
        self.file("movie.mov", 8)
        scope = ResourceScope(path_roots=(str(self.folder / "elsewhere"),), tool_families=("files",), access_mode="read_only")
        token = set_policy_context(PolicyContext(profile="developer", actor="agent:t", agent_id="agt_t", scope=scope))
        try:
            with self.assertRaises(HTTPException) as outside:
                self.scan()
        finally:
            reset_policy_context(token)
        self.assertEqual(403, outside.exception.status_code)

    def test_only_chosen_unchanged_items_move_to_the_trash_and_undo_brings_them_back(self) -> None:
        movie = self.file("movie.mov", 8)
        edited = self.file("edited.iso", 6)
        result = self.scan()
        ids = {item["name"]: item["id"] for item in result["items"]}
        edited.write_bytes(b"y" * (7 * MB))  # changed after the scan
        with patch.object(review.subprocess, "run", side_effect=self.fake_trash) as trash:
            moved = review.move_to_trash(result["scan_id"], [ids["movie.mov"], ids["edited.iso"], "it_unknown"],
                                         now=self.now)
        self.assertEqual([[review.TRASH, "-v", str(movie)]], [call.args[0] for call in trash.call_args_list])
        self.assertEqual([str(movie)], [item["path"] for item in moved["moved"]])
        self.assertEqual({"changed_since_scan", "not_in_this_scan"}, {item["reason"] for item in moved["skipped"]})
        self.assertEqual(8 * MB, moved["bytes_moved"])
        self.assertTrue(moved["verified"])
        self.assertFalse(movie.exists())
        self.assertTrue(edited.exists())

        back = review.undo(moved["receipt_id"])
        self.assertEqual(([str(movie)], True), (back["restored"], back["ok"]))
        self.assertTrue(movie.exists())

    def test_undo_without_trash_access_names_what_to_put_back_and_overwrites_nothing(self) -> None:
        movie = self.file("movie.mov", 8)
        other = self.file("other.mov", 8, fill=b"o")
        result = self.scan()
        with patch.object(review.subprocess, "run", side_effect=self.fake_trash):
            moved = review.move_to_trash(result["scan_id"], [item["id"] for item in result["items"]], now=self.now)
        other.write_bytes(b"new")  # a new file took this name
        with patch.object(review.os, "rename", side_effect=PermissionError("Operation not permitted")):
            back = review.undo(moved["receipt_id"])
        self.assertFalse(back["ok"])
        self.assertEqual([str(other)], back["conflicts"])
        self.assertEqual([{"name": "movie.mov", "back_to": str(self.folder)}], back["put_back_in_finder"])
        self.assertIn("Put Back", back["message"])
        self.assertEqual(b"new", other.read_bytes())
        self.assertFalse(movie.exists())

    def test_a_scan_expires_and_ids_must_come_from_it(self) -> None:
        self.file("movie.mov", 8)
        result = self.scan()
        with self.assertRaises(HTTPException) as old:
            review.move_to_trash(result["scan_id"], [result["items"][0]["id"]], now=self.now + review.REPORT_TTL_S + 1)
        self.assertEqual(410, old.exception.status_code)
        for bad in ("../../etc/passwd", "scan_zz"):
            with self.assertRaises(HTTPException):
                review.move_to_trash(bad, ["it_x"], now=self.now)
        with self.assertRaises(HTTPException):
            review.move_to_trash(result["scan_id"], [], now=self.now)

    def test_the_report_is_owner_only(self) -> None:
        self.file("movie.mov", 8)
        scan_id = self.scan()["scan_id"]
        report = Path(os.environ["MAC_MCP_STATE_DIR"]) / "storage-review" / f"{scan_id}.json"
        self.assertEqual(0o600, report.stat().st_mode & 0o777)

    def test_policy_scan_reads_and_trashing_is_destructive(self) -> None:
        self.assertIn("storage_review", RISK_REGISTRY)
        scan = resolve_risk("storage_review", {"action": "scan"})[1]
        self.assertFalse(scan.destructive)
        self.assertEqual("read_only", str(getattr(scan.requested_access_mode, "value", scan.requested_access_mode)))
        for action in ("move_to_trash", "undo"):
            self.assertTrue(resolve_risk("storage_review", {"action": action})[1].destructive)

    def test_changes_list_shows_trashed_files_but_not_scans(self) -> None:
        base = {"tool": "storage_review", "status": "success", "event_id": "e1", "timestamp": "t"}
        self.assertEqual([], change_items_for_event({**base, "arguments": {"action": "scan"}, "result": {}}))
        items = change_items_for_event({**base, "arguments": {"action": "move_to_trash"},
                                        "result": {"moved": [{"path": str(self.folder / "movie.mov")}]}})
        self.assertEqual(["Moved to Trash"], [item["action"] for item in items])


if __name__ == "__main__":
    unittest.main()
