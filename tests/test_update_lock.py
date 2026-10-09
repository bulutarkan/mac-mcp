from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import tools_update, update_helper, update_state
from mcp_server.update_helper import UpdateError


def update_info(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo=str(root / "repo"), runtime=str(root / "runtime"), branch="main", remote="origin",
        deployed_commit="a" * 40, target_commit="b" * 40, behind_by=1,
        update_available=True, dirty=False, release_verified=True,
        release_id="test-stable", release_version="1.0.0",
        release_payload_sha256="c" * 64, release_signer_fingerprint="SHA256:test",
        release_file_count=4, release_artifact_count=0,
        branch_tip_commit="b" * 40, unverified_ahead=0,
    )


class UpdateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-lock-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "repo").mkdir()
        (self.root / "runtime").mkdir()
        env = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.root / "update")})
        env.start()
        self.addCleanup(env.stop)
        self.workers: list[subprocess.Popen] = []
        self.addCleanup(self._stop_workers)

    def _stop_workers(self) -> None:
        for proc in self.workers:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def launch(self):
        """Launch an update whose 'worker' is a real sleeping process holding the handed-over lock."""
        captured = {}
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            captured["cmd"], captured["pass_fds"] = cmd, kwargs.get("pass_fds")
            kwargs["stdout"].close()
            worker = real_popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], pass_fds=kwargs["pass_fds"],
            )
            self.workers.append(worker)
            return worker

        info = update_info(self.root)
        with patch("mcp_server.tools_update.subprocess.Popen", side_effect=fake_popen):
            payload, proc = tools_update.launch_detached_update(
                info, self.root / "repo", self.root / "runtime", branch="main", remote="origin"
            )
        self.addCleanup(shutil.rmtree, Path(captured["cmd"][1]).parent, True)
        return payload, proc, captured

    def test_second_update_is_refused_while_the_first_worker_runs(self) -> None:
        first, _, captured = self.launch()
        lock_fd = captured["pass_fds"][0]
        self.assertEqual(str(lock_fd), captured["cmd"][captured["cmd"].index("--lock-fd") + 1])
        with self.assertRaises(update_state.UpdateInProgress) as ctx:
            self.launch()
        self.assertEqual(first["update_id"], ctx.exception.state.get("update_id"))
        self.assertIn(first["update_id"], str(ctx.exception))

    def test_a_killed_worker_releases_the_lock(self) -> None:
        self.launch()
        self.workers[0].send_signal(signal.SIGKILL)
        self.workers[0].wait()
        payload, _, _ = self.launch()
        self.assertTrue(payload["update_started"])

    def test_launcher_failure_releases_the_lock(self) -> None:
        info = update_info(self.root)
        with patch("mcp_server.tools_update.subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(OSError):
                tools_update.launch_detached_update(info, self.root / "repo", self.root / "runtime",
                                                    branch="main", remote="origin")
        os.close(update_state.acquire_update_lock())

    def test_mcp_tool_reports_the_running_update(self) -> None:
        first, _, _ = self.launch()
        info = update_info(self.root)
        with patch("mcp_server.tools_update.validate_update_state"), \
             patch("mcp_server.tools_update.resolve_paths", return_value=(self.root / "repo", self.root / "runtime")), \
             patch("mcp_server.tools_update.check_update", return_value=info), \
             patch("mcp_server.tools_update.secure_bootstrap_update_blocker", return_value=None):
            payload = tools_update.mac_mcp_update(check_only=False)
        self.assertFalse(payload["ok"])
        self.assertEqual("update_in_progress", payload["reason"])
        self.assertEqual(first["update_id"], payload["update_id"])


class WorkerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-lock-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {"MAC_MCP_UPDATE_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_inherited_lock_is_accepted(self) -> None:
        fd = update_state.acquire_update_lock()
        self.addCleanup(os.close, fd)
        self.assertEqual(fd, update_helper._hold_update_lock(fd))

    def test_a_descriptor_that_does_not_hold_the_lock_is_refused(self) -> None:
        holder = update_state.acquire_update_lock()
        self.addCleanup(os.close, holder)
        stranger = os.open(update_state.update_lock_path(), os.O_RDWR)
        self.addCleanup(os.close, stranger)
        with self.assertRaises(UpdateError):
            update_helper._hold_update_lock(stranger)

    def test_direct_run_takes_the_lock_or_refuses(self) -> None:
        fd = update_helper._hold_update_lock(None)
        try:
            (self.root / "state.json").write_text(json.dumps({"update_id": "upd_x", "status": "starting"}))
            with self.assertRaises(UpdateError) as ctx:
                update_helper._hold_update_lock(None)
            self.assertIn("upd_x", str(ctx.exception))
        finally:
            os.close(fd)
        os.close(update_helper._hold_update_lock(None))


if __name__ == "__main__":
    unittest.main()
