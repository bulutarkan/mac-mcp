"""#34: a click with expect={download: ...} catches the file it started, even one that finished at once.

browser_wait_for_download used to note the download folder when it was called; a file that finished
before that (a fast download, a separate wait call) counted as old and was never found.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_browser_agent as agent
from mcp_server.security import load_settings
from mcp_server.tools_browser import browser_wait_for_download


def fake_artifact(path, source=None):
    return {"artifact_id": "art_" + Path(path).stem, "path": str(path), "filename": Path(path).name}


class DownloadExpectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.settings = dataclasses.replace(load_settings(), download_dir=self.dir)
        registered = patch("mcp_server.tools_browser.register_artifact", side_effect=fake_artifact)
        registered.start()
        self.addCleanup(registered.stop)

    def test_expect_download_becomes_a_download_wait(self) -> None:
        wait = agent._expect_wait(0, {"download": "report"})
        self.assertEqual(("wait", "download", "report", 60.0), (wait["type"], wait["for"], wait["filename_contains"], wait["timeout_s"]))
        self.assertNotIn("filename_contains", agent._expect_wait(0, {"download": True}))
        self.assertEqual(120.0, agent._expect_wait(0, {"download": True, "timeout_s": 999})["timeout_s"])
        for bad in (False, "", "  ", 3):
            with self.assertRaises(HTTPException):
                agent._expect_wait(0, {"download": bad})

    def test_a_download_that_finished_before_the_wait_is_found(self) -> None:
        (self.dir / "old.pdf").write_bytes(b"x")
        os.utime(self.dir / "old.pdf", (time.time() - 86400, time.time() - 86400))
        time.sleep(0.05)
        clicked_ms = int(time.time() * 1000)
        time.sleep(0.02)
        (self.dir / "report.pdf").write_bytes(b"%PDF fast")
        # Dated by the server's Last-Modified: only its local creation time is recent.
        os.utime(self.dir / "report.pdf", (time.time() - 3600, time.time() - 3600))
        result = agent._download_wait(self.settings, {"for": "download", "timeout_s": 3}, clicked_ms)
        self.assertTrue(result["matched"])
        self.assertEqual("report.pdf", result["download"]["filename"])
        self.assertEqual("art_report", result["download"]["artifact_id"])
        # Without the action's start time the same file looks old: what the separate wait used to do.
        missed = browser_wait_for_download(self.settings, timeout_s=1, stable_ms=100)
        self.assertFalse(missed["completed"])
        self.assertTrue(missed["timed_out"])
        self.assertEqual("download_timeout", missed["error"])

    def test_nothing_downloaded_times_out_clearly(self) -> None:
        result = agent._download_wait(self.settings, {"for": "download", "timeout_s": 1}, int(time.time() * 1000))
        self.assertFalse(result["matched"])
        self.assertTrue(result["timed_out"])

    def test_the_check_lands_on_the_click(self) -> None:
        folded = agent._fold_expectations([
            {"ok": True, "type": "click"},
            {"ok": True, "type": "wait", "for": "download", "matched": True, "duration_ms": 40,
             "download": {"filename": "a.csv", "artifact_id": "art_a", "path": "/x/a.csv"},
             "_expect_for": 0, "_expect": {"download": True}},
        ])
        self.assertEqual(1, len(folded))
        self.assertTrue(folded[0]["expect"]["met"])
        self.assertEqual("art_a", folded[0]["expect"]["download"]["artifact_id"])

    def test_browser_act_waits_from_the_start_of_the_click(self) -> None:
        source = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertIn("previous_started_ms, action_started_ms = action_started_ms, int(time.time() * 1000)", source)
        self.assertIn("wait_result = _download_wait(settings, action, previous_started_ms)", source)


if __name__ == "__main__":
    unittest.main()
