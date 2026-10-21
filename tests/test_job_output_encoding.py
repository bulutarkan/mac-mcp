"""#83: output that is not UTF-8 never loses a job's output or a command's result.

Jobs read their pipes as strict UTF-8: one 0xff byte raised UnicodeDecodeError in the reader thread,
which died silently; the job then showed completed, exit 0, with empty stdout and stderr, and a pipe
no one read could block a chatty process. run_command raised the same error instead of a result.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_jobs, tools_terminal
from mcp_server.security import load_settings


class JobOutputEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-job-encoding-")
        self.root = Path(self.temp.name).resolve()
        self.jobs = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        self.jobs.start()
        self.settings = replace(load_settings(), workdir=self.root, allow_shell=True)

    def tearDown(self) -> None:
        self.jobs.stop()
        self.temp.cleanup()

    def run_job(self, command: str, timeout: float = 20) -> tuple[dict, dict]:
        job_id = tools_jobs.start_background_job(self.settings, command, cwd=str(self.root), timeout_s=60)["job_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = tools_jobs.get_job_status(self.settings, job_id)
            if status.get("ended_at"):
                return status, tools_jobs.get_job_output(self.settings, job_id)
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish")

    def test_a_byte_that_is_not_utf8_keeps_the_rest_of_the_output(self) -> None:
        status, output = self.run_job(r"printf 'before\n\377bad\nafter\ndone\n'; printf 'err\377\nerr-done\n' >&2")
        self.assertEqual(("completed", 0), (status["status"], status["exit_code"]))
        self.assertEqual("before\n�bad\nafter\ndone\n", output["stdout"])
        self.assertEqual("err�\nerr-done\n", output["stderr"])
        self.assertNotIn("stdout_reader_error", status)

    def test_lots_of_binary_output_never_blocks_the_job(self) -> None:
        status, output = self.run_job(
            "python3 -c \"import sys\nfor i in range(4000): sys.stdout.buffer.write(b'\\xff\\xfe line %d\\n' % i)\"")
        self.assertEqual("completed", status["status"])
        self.assertTrue(output["stdout"].rstrip().endswith("line 3999"))

    def test_a_reader_that_fails_says_so(self) -> None:
        job_id = "reader0001"
        (tools_jobs.JOBS_DIR / job_id).mkdir(parents=True)
        tools_jobs._write_meta(job_id, {"status": "running", "started_at": time.time()})

        class Broken:
            closed = False

            def readline(self):
                raise OSError("pipe gone")

            def close(self):
                self.closed = True

        stream = Broken()
        tools_jobs._append_stream(job_id, stream, "stdout.log")
        self.assertEqual("OSError", tools_jobs._read_meta(job_id)["stdout_reader_error"])
        self.assertTrue(stream.closed)

    def test_run_command_returns_output_that_is_not_utf8(self) -> None:
        result = tools_terminal.run_command(self.settings, r"printf 'a\377b\n'", timeout_s=10)
        self.assertEqual((0, "a�b\n"), (result["exit_code"], result["stdout"]))


if __name__ == "__main__":
    unittest.main()
