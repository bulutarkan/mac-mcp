from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import result_pages, tools_files, tools_jobs


class CursorTests(unittest.TestCase):
    def test_round_trip_and_binding(self) -> None:
        cursor = result_pages.encode_cursor(("a", "/x"), ["dir", "file"])
        self.assertEqual(["dir", "file"], result_pages.decode_cursor(cursor, ("a", "/x")))
        self.assertIsNone(result_pages.decode_cursor(None, ("a", "/x")))
        for bad, request in ((cursor, ("a", "/y")), ("not-base64!!", ("a", "/x"))):
            with self.assertRaises(HTTPException) as ctx:
                result_pages.decode_cursor(bad, request)
            self.assertEqual(400, ctx.exception.status_code)


class ReadMultipleBudgetTests(unittest.TestCase):
    def test_aggregate_budget_skips_and_lists_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for index in range(5):
                path = Path(td) / f"f{index}.txt"
                path.write_text(("line\n" * 9_000), encoding="utf-8")  # 45,000 chars each
                paths.append(str(path))
            result = tools_files.read_multiple_files(None, paths, max_total_chars=100_000)
        statuses = [item["status"] for item in result["files"]]
        self.assertEqual(["ok", "ok", "ok", "skipped", "skipped"], statuses)
        self.assertEqual(paths[3:], result["not_read"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(sum(len(item.get("content", "")) for item in result["files"]), 100_000)
        self.assertEqual(100_000, result["budget"]["used_chars"])
        third = result["files"][2]
        self.assertEqual("aggregate_budget", third["truncation"]["reason"])
        # The continuation offset is the first line not returned in full.
        self.assertEqual("read_file", third["continue"]["tool"])
        self.assertEqual(third["content"].count("\n") - 1, third["continue"]["offset"])

    def test_default_budget_is_bounded(self) -> None:
        self.assertEqual(120_000, tools_files.read_multiple_files(None, [])["budget"]["limit_chars"])
        self.assertEqual(400_000, tools_files.read_multiple_files(None, [], max_total_chars=10**9)["budget"]["limit_chars"])


class ReadFileContinuationTests(unittest.TestCase):
    def test_truncated_file_reports_next_offset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.txt"
            path.write_text("".join(f"{i:09d}\n" for i in range(30_000)), encoding="utf-8")  # 300,000 chars
            first = tools_files.read_file(None, str(path))
            self.assertTrue(first["truncated"])
            self.assertEqual(30_000, first["total_lines"])
            nxt = first["next_offset"]
            self.assertTrue(first["content"].startswith("000000000\n"))
            second = tools_files.read_file(None, str(path), offset=nxt)
            self.assertTrue(second["content"].startswith(f"{nxt:09d}\n"))
            self.assertIsNone(second["next_offset"])
            # No line is skipped at the boundary.
            self.assertTrue(first["content"].split("\n... [truncated]")[0].split("\n")[-2].endswith(f"{nxt - 1:09d}"))

    def test_small_file_keeps_the_plain_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.txt"
            path.write_text("a\nb\n", encoding="utf-8")
            result = tools_files.read_file(None, str(path))
            self.assertNotIn("next_offset", result)
            page = tools_files.read_file(None, str(path), offset=0, length=1)
            self.assertEqual("a\n", page["content"])
            self.assertEqual(1, page["next_offset"])
            self.assertEqual(2, page["total_lines"])


class ListingPagingTests(unittest.TestCase):
    def test_list_directory_pages_stably_across_inserts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for index in range(25):
                (Path(td) / f"n{index:02d}").write_text("x")
            first = tools_files.list_directory(None, td, limit=10)
            self.assertEqual(25, first["total"])
            self.assertEqual(["n00", "n09"], [first["entries"][0]["name"], first["entries"][-1]["name"]])
            # A new entry sorting before the cursor does not shift the next page.
            (Path(td) / "a-new").write_text("x")
            second = tools_files.list_directory(None, td, limit=10, cursor=first["page"]["next_cursor"])
            self.assertEqual("n10", second["entries"][0]["name"])
            third = tools_files.list_directory(None, td, limit=10, cursor=second["page"]["next_cursor"])
            self.assertEqual(["n20", "n21", "n22", "n23", "n24"], [e["name"] for e in third["entries"]])
            self.assertFalse(third["page"]["has_more"])
            with self.assertRaises(HTTPException):
                tools_files.list_directory(None, str(Path(td) / ".."), cursor=first["page"]["next_cursor"])

    def test_find_files_continues_beyond_a_page_without_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            expected = set()
            for d in ("a", "a-b", "b"):
                (root / d).mkdir()
                for index in range(260):
                    (root / d / f"f{index:03d}.txt").write_text("x")
                    expected.add(str(root / d / f"f{index:03d}.txt"))
            seen, cursor = [], None
            while True:
                result = tools_files.find_files(None, "*.txt", path=td, file_type="file", limit=200, cursor=cursor)
                seen.extend(item["path"] for item in result["results"])
                cursor = result["page"]["next_cursor"]
                self.assertEqual(result["truncated"], result["page"]["has_more"])
                if not cursor:
                    break
            self.assertEqual(780, len(seen))
            self.assertEqual(expected, set(seen))
            self.assertEqual(len(seen), len(set(seen)))

    def test_scoped_find_resumes_in_the_same_order(self) -> None:
        from mcp_server import scoped_fs
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            for d in ("a", "a-b", "b"):
                (root / d).mkdir()
                for index in range(5):
                    (root / d / f"f{index}.txt").write_text("x")

            def fake_guarded(scope, path):
                class Ctx:
                    def __enter__(self_inner):
                        self_inner.fd = os.open(str(path), os.O_RDONLY)
                        return self_inner.fd, path
                    def __exit__(self_inner, *exc):
                        os.close(self_inner.fd)
                return Ctx()

            with patch.object(scoped_fs, "guarded_directory", fake_guarded):
                full = [item["path"] for item in scoped_fs.scoped_find(None, root, "*.txt", "file", 100)]
                head = scoped_fs.scoped_find(None, root, "*.txt", "file", 7)
                after = Path(head[-1]["path"]).relative_to(root).parts
                rest = scoped_fs.scoped_find(None, root, "*.txt", "file", 100, after)
            self.assertEqual(15, len(full))
            self.assertEqual(full, [item["path"] for item in head] + [item["path"] for item in rest])
            self.assertEqual(sorted(full, key=lambda p: Path(p).relative_to(root).parts), full)


class ListJobsPagingTests(unittest.TestCase):
    def test_cursor_walks_jobs_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            jobs_dir = Path(td)
            for index in range(7):
                job = jobs_dir / f"job{index}"
                job.mkdir()
                (job / "meta.json").write_text("{}")
                os.utime(job, (time.time() - 100 + index, time.time() - 100 + index))
            with patch.object(tools_jobs, "JOBS_DIR", jobs_dir), \
                    patch.object(tools_jobs, "prune_jobs", lambda: None), \
                    patch.object(tools_jobs, "_normalize_status", lambda job_id, meta: meta), \
                    patch.object(tools_jobs, "_public_meta", lambda job_id, meta: {"job_id": job_id, "status": "completed"}):
                first = tools_jobs.list_jobs(None, limit=3)
                second = tools_jobs.list_jobs(None, limit=3, cursor=first["page"]["next_cursor"])
                third = tools_jobs.list_jobs(None, limit=3, cursor=second["page"]["next_cursor"])
        ids = [job["job_id"] for page in (first, second, third) for job in page["jobs"]]
        self.assertEqual([f"job{i}" for i in range(6, -1, -1)], ids)
        self.assertEqual(7, first["total"])
        self.assertTrue(first["truncated"])
        self.assertFalse(third["page"]["has_more"])


if __name__ == "__main__":
    unittest.main()
