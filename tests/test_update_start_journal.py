"""#95: the updater's start journal is written like every other journal write: atomically.

The launcher wrote state.json with write_text, which empties the file before writing. A write that
stopped half way (a crash, a full disk) left a journal the next update refuses as corrupt, so updates
stayed locked until someone repaired it by hand.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_update
from mcp_server.update_state import read_update_state, update_state_path


def _info(**overrides):
    base = dict(
        repo="/tmp/repo", runtime="/tmp/runtime", remote="origin", branch="main",
        deployed_commit="a" * 40, target_commit="b" * 40, behind_by=1, update_available=True, dirty=False,
        release_verified=True, release_id="stable-2.3.0-r3", release_version="2.3.0",
        release_payload_sha256="c" * 64, release_signer_fingerprint="d" * 64, release_file_count=1,
        release_artifact_count=0, branch_tip_commit="b" * 40, unverified_ahead=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class UpdateStartJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-update-journal-")
        self.env = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": self.temp.name})
        self.env.start()
        self.previous = {"status": "completed", "update_id": "upd_previous", "release_id": "stable-2.3.0-r2"}
        update_state_path().write_text(json.dumps(self.previous), encoding="utf-8")

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def launch(self, info):
        with patch.object(tools_update.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            return tools_update._launch_locked_update(
                info, Path("/tmp/repo"), Path("/tmp/runtime"), 99,
                branch="main", remote="origin", launchd_label="", skip_restart=True, skip_deps=True,
            ), popen

    def test_a_write_that_fails_half_way_leaves_the_last_journal_whole(self) -> None:
        # Text that cannot be encoded stops the write after it began, like a crash would.
        with patch.object(tools_update.json, "dumps", return_value='{"status": "starting", "x": "\ud800"}'), \
             self.assertRaises(UnicodeEncodeError):
            self.launch(_info())
        self.assertEqual(self.previous, read_update_state())
        self.assertEqual([], [p.name for p in update_state_path().parent.glob(".state.json.*")])

    def test_a_normal_start_writes_the_starting_journal(self) -> None:
        (payload, _proc), popen = self.launch(_info())
        state = read_update_state()
        self.assertEqual(("starting", payload["update_id"]), (state["status"], state["update_id"]))
        self.assertEqual(0o600, update_state_path().stat().st_mode & 0o777)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
