from __future__ import annotations

import os
import signal
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_jobs
from mcp_server.security import load_settings


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobProcessGroupTests(unittest.TestCase):
    """A job is done only when every process in its group has exited (roadmap #105)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-job-pgroup-")
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.jobs = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        self.jobs.start()
        self.settings = replace(load_settings(), workdir=self.work, allow_shell=True)
        self.child_pids: list[int] = []

    def tearDown(self) -> None:
        for pid in self.child_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.jobs.stop()
        self.temp.cleanup()

    def _start(self, command: str) -> str:
        return tools_jobs.start_background_job(self.settings, command, cwd=str(self.work))["job_id"]

    def _status(self, job_id: str) -> dict:
        return tools_jobs.get_job_status(self.settings, job_id)

    def _child_pid(self, name: str = "child.pid") -> int:
        path = self.work / name
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text = path.read_text().strip() if path.exists() else ""
            if text.isdigit():
                pid = int(text)
                self.child_pids.append(pid)
                return pid
            time.sleep(0.02)
        self.fail(f"{name} was not written")

    def _wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def test_job_stays_running_while_child_outlives_shell(self) -> None:
        job_id = self._start("sleep 30 >/dev/null 2>&1 & echo $! > child.pid")
        child = self._child_pid()
        self.assertTrue(self._wait_for(lambda: tools_jobs._PROCS.get(job_id) is None
                                       or tools_jobs._PROCS[job_id].poll() is not None))
        time.sleep(1.0)  # past the watcher's poll interval
        self.assertTrue(_alive(child))
        self.assertEqual("running", self._status(job_id)["status"])

    def test_job_completes_once_surviving_child_exits(self) -> None:
        job_id = self._start("sleep 1.2 >/dev/null 2>&1 & echo $! > child.pid")
        child = self._child_pid()
        self.assertEqual("running", self._status(job_id)["status"])
        self.assertTrue(self._wait_for(lambda: not _alive(child), timeout=5))
        self.assertTrue(self._wait_for(lambda: self._status(job_id)["status"] == "completed", timeout=5))
        status = self._status(job_id)
        self.assertEqual(0, status["exit_code"])
        self.assertTrue(status.get("ended_at"))

    def test_stop_after_shell_exit_terminates_surviving_children(self) -> None:
        job_id = self._start(
            "sleep 30 >/dev/null 2>&1 & echo $! > child.pid; "
            "sleep 30 >/dev/null 2>&1 & echo $! > child2.pid"
        )
        children = [self._child_pid(), self._child_pid("child2.pid")]
        self.assertTrue(self._wait_for(lambda: tools_jobs._PROCS.get(job_id) is None
                                       or tools_jobs._PROCS[job_id].poll() is not None))
        result = tools_jobs.stop_job(self.settings, job_id)
        self.assertEqual("killed", result["status"])
        for pid in children:
            self.assertTrue(self._wait_for(lambda pid=pid: not _alive(pid), timeout=5), f"child {pid} survived stop")
        self.assertTrue(self._wait_for(lambda: self._status(job_id).get("ended_at"), timeout=5))
        self.assertEqual("killed", self._status(job_id)["status"])

    def test_short_job_without_children_completes_normally(self) -> None:
        job_id = self._start("echo hello")
        self.assertTrue(self._wait_for(lambda: self._status(job_id)["status"] == "completed", timeout=5))
        self.assertEqual("hello", tools_jobs.get_job_output(self.settings, job_id)["stdout"].strip())

    def test_failed_leader_with_surviving_child_reports_failure_after_child(self) -> None:
        job_id = self._start("sleep 1 >/dev/null 2>&1 & echo $! > child.pid; exit 3")
        child = self._child_pid()
        time.sleep(0.6)
        self.assertEqual("running", self._status(job_id)["status"])
        self.assertTrue(self._wait_for(lambda: not _alive(child), timeout=5))
        self.assertTrue(self._wait_for(lambda: self._status(job_id)["status"] == "failed", timeout=5))
        self.assertEqual(3, self._status(job_id)["exit_code"])

    def test_detached_child_in_new_session_is_not_tracked(self) -> None:
        # setsid/daemonized children leave the job's process group on purpose; the
        # job finishes with its group and never signals the detached process.
        job_id = self._start(
            "python3 -c 'import os,subprocess,sys; "
            "p=subprocess.Popen([\"sleep\",\"30\"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "open(\"child.pid\",\"w\").write(str(p.pid))'"
        )
        child = self._child_pid()
        self.assertTrue(self._wait_for(lambda: self._status(job_id)["status"] == "completed", timeout=5))
        self.assertTrue(_alive(child))

    def test_timeout_applies_to_children_that_outlive_the_shell(self) -> None:
        job_id = tools_jobs.start_background_job(
            self.settings, "sleep 30 >/dev/null 2>&1 & echo $! > child.pid", cwd=str(self.work), timeout_s=2,
        )["job_id"]
        child = self._child_pid()
        self.assertTrue(self._wait_for(lambda: not _alive(child), timeout=6), "timeout left the child running")
        self.assertTrue(self._wait_for(lambda: self._status(job_id)["status"] == "timeout", timeout=5))

    def test_group_id_held_by_a_live_process_is_never_claimed(self) -> None:
        # After the leader is reaped its pid may be reused; a live process holding
        # that pid means the group id is no longer the job's.
        with patch.object(tools_jobs, "_is_pid_alive", return_value=True), \
                patch.object(tools_jobs.os, "killpg") as killpg:
            self.assertFalse(tools_jobs._surviving_group(4242))
            tools_jobs._terminate_process(type("Done", (), {"pid": 4242, "poll": lambda self: 0})())
        killpg.assert_not_called()

    def test_job_adopted_from_metadata_after_restart_tracks_and_stops_survivors(self) -> None:
        # The server restarted: no Popen handle, only meta.json with the leader pid.
        leader = __import__("subprocess").Popen(
            ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > orphan.pid"],
            cwd=str(self.work), start_new_session=True,
        )
        leader.wait()
        child = self._child_pid("orphan.pid")
        job_id = "adopted0001"
        tools_jobs._write_meta(job_id, {"status": "running", "pid": leader.pid, "started_at": time.time()})
        self.assertEqual("running", self._status(job_id)["status"])
        self.assertEqual("killed", tools_jobs.stop_job(self.settings, job_id)["status"])
        self.assertTrue(self._wait_for(lambda: not _alive(child), timeout=5))
        self.assertTrue(self._wait_for(lambda: self._status(job_id).get("ended_at"), timeout=5))
        self.assertEqual("killed", self._status(job_id)["status"])


if __name__ == "__main__":
    unittest.main()
