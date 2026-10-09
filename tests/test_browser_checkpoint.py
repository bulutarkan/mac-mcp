from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from mcp_server import browser_checkpoint as bc


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        bc._CHECKPOINTS.clear()

    @staticmethod
    def _probe(*states):
        seq = iter(states)
        last = {}

        def probe():
            nonlocal last
            last = next(seq, last)
            return last
        return probe

    def test_create_records_only_non_sensitive_fields_and_notifies(self) -> None:
        sent = []
        result = bc.create(
            "Google Chrome", "btab_1",
            self._probe({"url": "https://login.example.com/mfa?token=SECRET", "title": "Verify", "challenge": "otp"}),
            notify=lambda title, text: sent.append((title, text)), completed_actions=3,
        )
        self.assertEqual("awaiting_human", result["status"])
        self.assertEqual("otp", result["category"])
        self.assertEqual("https://login.example.com", result["origin"])
        self.assertTrue(result["notified"])
        self.assertEqual(1, len(sent))
        stored = bc._CHECKPOINTS[result["checkpoint_id"]]
        self.assertNotIn("SECRET", json.dumps(stored))
        self.assertEqual({"checkpoint_id", "browser", "tab_handle", "origin", "title", "category",
                          "completed_actions", "created_at"}, set(stored))

    def test_wait_resolves_when_the_challenge_is_gone(self) -> None:
        created = bc.create("Google Chrome", "btab_1", self._probe({"url": "https://a.example/login", "challenge": "password"}))
        clock = iter([0.0, 0.0, 1.0, 2.0, 3.0])
        result = bc.check(
            created["checkpoint_id"],
            self._probe({"url": "https://a.example/login", "challenge": "password"},
                        {"url": "https://app.example/home", "challenge": None}),
            tab_exists=lambda: True, wait_s=60, sleep=lambda s: None, clock=lambda: next(clock),
        )
        self.assertEqual("resolved", result["status"])
        self.assertTrue(result["origin_changed"])
        self.assertTrue(result["observe_again"])
        self.assertNotIn(created["checkpoint_id"], bc._CHECKPOINTS)

    def test_status_keeps_waiting_and_a_closed_tab_no_longer_matches(self) -> None:
        created = bc.create("Safari", "btab_2", self._probe({"url": "https://a.example", "challenge": "otp"}))
        still = bc.check(created["checkpoint_id"], self._probe({"challenge": "otp"}), tab_exists=lambda: True)
        self.assertEqual(("awaiting_human", "otp"), (still["status"], still["challenge_now"]))
        gone = bc.check(created["checkpoint_id"], self._probe({"challenge": "otp"}), tab_exists=lambda: False)
        self.assertEqual("tab_closed", gone["status"])

    def test_unknown_expired_and_cancelled_checkpoints(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            bc.check("bck_missing", self._probe({}), tab_exists=lambda: True)
        self.assertEqual(404, ctx.exception.status_code)
        created = bc.create("Safari", "btab_3", self._probe({"url": "https://a.example", "challenge": None}),
                            category="push")
        self.assertEqual("push", created["category"])
        bc._CHECKPOINTS[created["checkpoint_id"]]["created_at"] -= bc.CHECKPOINT_TTL_S + 1
        with self.assertRaises(HTTPException):
            bc.check(created["checkpoint_id"], self._probe({}), tab_exists=lambda: True)
        again = bc.create("Safari", "btab_3", self._probe({"url": "https://a.example"}))
        self.assertEqual("cancelled", bc.cancel(again["checkpoint_id"])["status"])
        with self.assertRaises(HTTPException):
            bc.create("Safari", "", self._probe({}))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_detection_script_is_valid_and_reads_no_values(self) -> None:
        self.assertNotIn(".value", bc.CHALLENGE_JS)
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "probe.js"
            script.write_text(bc.CHALLENGE_JS, encoding="utf-8")
            completed = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
