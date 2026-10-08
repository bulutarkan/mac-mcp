from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_jobs
from mcp_server.security import load_settings


class JobStorageBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-job-bounds-")
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.jobs = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        self.jobs.start()
        tools_jobs._last_prune_at = 0.0
        self.settings = replace(load_settings(), workdir=self.work, allow_shell=True, max_output_chars=2000)

    def tearDown(self) -> None:
        self.jobs.stop()
        self.temp.cleanup()

    def _wait_done(self, job_id: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = tools_jobs.get_job_status(self.settings, job_id)
            if status["status"] not in {"starting", "running", "stopping"}:
                return status
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish")

    def _fake_job(self, job_id: str, *, status: str = "completed", ended_ago_days: float = 0.0,
                  size: int = 10) -> Path:
        path = tools_jobs.JOBS_DIR / job_id
        path.mkdir(parents=True)
        ended = time.time() - ended_ago_days * 86400
        meta = {"job_id": job_id, "status": status, "pid": None, "started_at": ended - 1,
                "ended_at": None if status == "running" else ended, "updated_at": ended}
        (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (path / "stdout.log").write_bytes(b"x" * size)
        (path / "stderr.log").write_bytes(b"")
        return path

    def test_output_beyond_the_cap_is_drained_but_not_stored(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_JOB_STREAM_MAX_BYTES": str(64 * 1024)}):
            job = tools_jobs.start_background_job(
                self.settings, "python3 -c \"import sys\nfor i in range(20000): print('line %06d ' % i + 'y'*20)\"",
                cwd=str(self.work), timeout_s=60,
            )
            status = self._wait_done(job["job_id"])
        self.assertEqual("completed", status["status"], status)
        log = tools_jobs.JOBS_DIR / job["job_id"] / "stdout.log"
        self.assertLessEqual(log.stat().st_size, 64 * 1024 + 200)
        self.assertIn("truncated after", log.read_text(encoding="utf-8")[-200:])
        self.assertTrue(status.get("stdout_truncated"))

    def test_output_reads_only_the_requested_slice(self) -> None:
        path = self._fake_job("slicejob", size=0)
        lines = [f"line {i:05d}" for i in range(20000)]
        (path / "stdout.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        size = (path / "stdout.log").stat().st_size
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")):
            tail = tools_jobs.get_job_output(self.settings, "slicejob", tail_lines=3, stream="stdout")
            self.assertEqual("line 19997\nline 19998\nline 19999", tail["stdout"])
            self.assertEqual(size, tail["offsets"]["stdout"])

            first = tools_jobs.get_job_output(self.settings, "slicejob", since_offset=0, stream="stdout")
            self.assertTrue(first["stdout"].startswith("line 00000"))
            self.assertTrue(first["stdout_truncated"])
            self.assertLess(first["offsets"]["stdout"], size)
            second = tools_jobs.get_job_output(self.settings, "slicejob", since_offset=first["offsets"]["stdout"], stream="stdout")
            self.assertGreater(second["offsets"]["stdout"], first["offsets"]["stdout"])
            end = tools_jobs.get_job_output(self.settings, "slicejob", since_offset=size, stream="stdout")
            self.assertEqual("", end["stdout"])
            self.assertFalse(end["stdout_truncated"])

    def test_retention_drops_old_and_excess_finished_jobs_but_keeps_active(self) -> None:
        self._fake_job("old", ended_ago_days=30)
        for index in range(15):
            self._fake_job(f"recent{index:02d}", ended_ago_days=index * 0.01)
        active = self._fake_job("active", status="running", ended_ago_days=40)
        with patch.dict(os.environ, {"MAC_MCP_JOB_RETENTION_COUNT": "10"}), \
             patch.object(tools_jobs, "_is_pid_alive", return_value=True), \
             patch.object(tools_jobs, "_surviving_group", return_value=False):
            report = tools_jobs.prune_jobs(force=True)
        remaining = sorted(p.name for p in tools_jobs.JOBS_DIR.iterdir())
        self.assertEqual(6, report["pruned"])
        self.assertIn("active", remaining)
        self.assertNotIn("old", remaining)
        self.assertEqual([f"recent{i:02d}" for i in range(10)], [n for n in remaining if n.startswith("recent")])
        self.assertTrue(active.exists())

    def test_total_size_limit_removes_oldest_finished_jobs(self) -> None:
        for index in range(4):
            self._fake_job(f"big{index}", ended_ago_days=index * 0.1, size=10 * 1024 * 1024)
        with patch.dict(os.environ, {"MAC_MCP_JOB_RETENTION_BYTES": str(25 * 1024 * 1024)}):
            tools_jobs.prune_jobs(force=True)
        self.assertEqual(["big0", "big1"], sorted(p.name for p in tools_jobs.JOBS_DIR.iterdir()))

    def test_list_is_bounded_and_reports_total(self) -> None:
        for index in range(12):
            self._fake_job(f"job{index:02d}", ended_ago_days=index * 0.001)
        listed = tools_jobs.list_jobs(self.settings, limit=5)
        self.assertEqual(5, listed["count"])
        self.assertEqual(12, listed["total"])
        self.assertTrue(listed["truncated"])
        self.assertIn("days", listed["retention"])

    def test_delete_removes_finished_job_and_refuses_running(self) -> None:
        done = self._fake_job("donejob")
        result = tools_jobs.delete_job(self.settings, "donejob")
        self.assertTrue(result["deleted"])
        self.assertFalse(done.exists())
        self._fake_job("livejob", status="running")
        with patch.object(tools_jobs, "_is_pid_alive", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                tools_jobs.delete_job(self.settings, "livejob")
        self.assertEqual(409, ctx.exception.status_code)
        with self.assertRaises(HTTPException) as missing:
            tools_jobs.delete_job(self.settings, "donejob")
        self.assertEqual(404, missing.exception.status_code)

    def test_job_files_are_owner_only(self) -> None:
        job = tools_jobs.start_background_job(self.settings, "echo hi", cwd=str(self.work), timeout_s=30)
        self._wait_done(job["job_id"])
        path = tools_jobs.JOBS_DIR / job["job_id"]
        self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode))
        for name in ("meta.json", "stdout.log", "stderr.log"):
            self.assertEqual(0o600, stat.S_IMODE((path / name).stat().st_mode), name)

    def test_job_that_never_launched_is_failed_after_a_grace_period(self) -> None:
        path = tools_jobs.JOBS_DIR / "ghost"
        path.mkdir(parents=True)
        meta = {"job_id": "ghost", "status": "starting", "pid": None, "started_at": time.time() - 120,
                "ended_at": None, "updated_at": time.time() - 120}
        (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        status = tools_jobs.get_job_status(self.settings, "ghost")
        self.assertEqual("failed", status["status"])
        self.assertIn("never started", status["note"])
        fresh = dict(meta, job_id="fresh", started_at=time.time())
        (tools_jobs.JOBS_DIR / "fresh").mkdir()
        (tools_jobs.JOBS_DIR / "fresh" / "meta.json").write_text(json.dumps(fresh), encoding="utf-8")
        self.assertEqual("starting", tools_jobs.get_job_status(self.settings, "fresh")["status"])


if __name__ == "__main__":
    unittest.main()
