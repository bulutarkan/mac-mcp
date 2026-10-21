"""#97: memory day files and job metadata reach the disk before a write returns.

Both were written to a temp file and renamed into place, but neither the file nor its folder was
synced, so a power cut right after could lose or empty the last write. A job folder whose metadata
was unreadable was also skipped by every prune, so it was never cleaned up.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_jobs, tools_memory


class DurableWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-durable-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def synced(self, write) -> list:
        synced = []
        real = os.fsync

        def record(fd):
            synced.append(os.fstat(fd).st_ino)
            return real(fd)

        with patch.object(os, "fsync", side_effect=record):
            write()
        return synced

    def test_a_memory_day_file_and_its_folder_are_synced(self) -> None:
        path = self.root / "2026" / "10" / "2026-10-21.md"
        synced = self.synced(lambda: tools_memory._atomic_write(path, "# 21 October\n"))
        self.assertEqual("# 21 October\n", path.read_text(encoding="utf-8"))
        self.assertIn(path.stat().st_ino, synced, "the file")
        self.assertIn(path.parent.stat().st_ino, synced, "its folder")
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual([], [p.name for p in path.parent.iterdir() if p.name.startswith(".")])

    def test_job_metadata_and_its_folder_are_synced(self) -> None:
        with patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs"):
            synced = self.synced(lambda: tools_jobs._write_meta("job0001", {"status": "running"}))
            path = tools_jobs._meta_path("job0001")
            self.assertEqual({"status": "running"}, tools_jobs._read_meta("job0001"))
            self.assertIn(path.stat().st_ino, synced)
            self.assertIn(path.parent.stat().st_ino, synced)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_a_job_with_unreadable_metadata_is_pruned_once_it_is_old(self) -> None:
        with patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs"):
            old = tools_jobs.JOBS_DIR / "broken_old"
            fresh = tools_jobs.JOBS_DIR / "broken_new"
            for folder in (old, fresh):
                folder.mkdir(parents=True)
                (folder / "meta.json").write_text("{not json", encoding="utf-8")
            long_ago = time.time() - (tools_jobs.job_retention()["days"] + 1) * 86400
            os.utime(old, (long_ago, long_ago))
            os.utime(old / "meta.json", (long_ago, long_ago))
            report = tools_jobs.prune_jobs(force=True)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists(), "a recent one may still be being written")
            self.assertEqual(1, report["unreadable_pruned"])


if __name__ == "__main__":
    unittest.main()
