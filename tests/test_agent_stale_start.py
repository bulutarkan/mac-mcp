from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_agents as agents


class AgentStaleStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents, self.old_teams = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir(parents=True)
        agents.TEAMS_DIR.mkdir(parents=True)
        self.releases = []
        self.converged = []
        patches = (
            patch.object(agents, "_release_agent_admission", side_effect=lambda agent_id, meta: self.releases.append(agent_id)),
            patch.object(agents, "_converge_workflow_terminal", side_effect=lambda agent_id, meta: self.converged.append(agent_id)),
            patch.object(agents.browser_tabs, "release_agent_leases"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.old_agents, self.old_teams
        self.temp.cleanup()

    def agent(self, agent_id: str, *, age_s: float, worker_pid=None, status: str = "starting", nonce: str = "n1") -> None:
        adir = agents.AGENTS_DIR / agent_id
        adir.mkdir(parents=True)
        (adir / "effective_prompt.txt").write_text("task", encoding="utf-8")
        started = time.time() - age_s
        meta = {
            "agent_id": agent_id, "status": status, "phase": status, "provider": "codex",
            "started_at": started, "spawn_requested_at": started, "updated_at": started,
            "worker_pid": worker_pid, "provider_pid": None, "launch_nonce": nonce,
        }
        (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_start_that_never_launched_fails_once_and_releases_admission(self) -> None:
        self.agent("agt_ghost", age_s=600)
        meta = agents._normalize("agt_ghost", agents._read_meta("agt_ghost"))
        self.assertEqual("failed", meta["status"])
        self.assertEqual("worker_never_started", meta["failure_reason"])
        self.assertIn("never reported", meta["note"])
        self.assertEqual(["agt_ghost"], self.releases)
        self.assertEqual(["agt_ghost"], self.converged)
        # Repeated reads do not repeat the transition or the releases.
        again = agents._normalize("agt_ghost", agents._read_meta("agt_ghost"))
        self.assertEqual(meta["ended_at"], again["ended_at"])
        self.assertEqual(["agt_ghost"], self.releases)

    def test_recent_start_and_spawn_in_progress_are_left_alone(self) -> None:
        self.agent("agt_fresh", age_s=5)
        self.assertEqual("starting", agents._normalize("agt_fresh", agents._read_meta("agt_fresh"))["status"])
        self.agent("agt_spawning", age_s=600)
        with agents._WORKERS_LOCK:
            agents._WORKERS["agt_spawning"] = object()
        try:
            self.assertEqual("starting", agents._normalize("agt_spawning", agents._read_meta("agt_spawning"))["status"])
        finally:
            with agents._WORKERS_LOCK:
                agents._WORKERS.pop("agt_spawning", None)
        self.assertEqual([], self.releases)

    def test_live_worker_that_recorded_itself_is_not_failed(self) -> None:
        # The parent crashed before storing the PID; the worker stored its own.
        self.agent("agt_live", age_s=600, worker_pid=os.getpid(), status="running")
        meta = agents._normalize("agt_live", agents._read_meta("agt_live"))
        self.assertEqual("running", meta["status"])
        self.assertEqual([], self.releases)

    def test_worker_records_its_own_identity_and_refuses_a_foreign_launch(self) -> None:
        self.agent("agt_self", age_s=1, nonce="good")
        # No provider process may ever start from this test.
        with patch.dict(os.environ, {agents._LAUNCH_NONCE_ENV: "good"}), \
             patch.object(agents.subprocess, "Popen", side_effect=RuntimeError("no provider in tests")) as popen:
            try:
                agents._worker("agt_self")
            except Exception:
                pass
        meta = agents._read_meta("agt_self")
        self.assertEqual(os.getpid(), meta["worker_pid"])
        self.assertEqual({"pid": os.getpid(), "nonce": "good"},
                         {k: meta["worker_identity"][k] for k in ("pid", "nonce")})

        self.agent("agt_other", age_s=1, nonce="expected")
        with patch.dict(os.environ, {agents._LAUNCH_NONCE_ENV: "stale"}):
            self.assertEqual(0, agents._worker("agt_other"))
        self.assertIsNone(agents._read_meta("agt_other")["worker_pid"])

    def test_worker_does_not_run_an_agent_already_failed_as_never_started(self) -> None:
        self.agent("agt_late", age_s=600)
        agents._normalize("agt_late", agents._read_meta("agt_late"))
        with patch.dict(os.environ, {agents._LAUNCH_NONCE_ENV: "n1"}):
            self.assertEqual(0, agents._worker("agt_late"))
        meta = agents._read_meta("agt_late")
        self.assertEqual("failed", meta["status"])
        self.assertNotIn("worker_identity", meta)

    def test_spawn_meta_carries_a_launch_nonce_for_the_worker(self) -> None:
        source = Path(agents.__file__).read_text(encoding="utf-8")
        self.assertIn('"launch_nonce": uuid.uuid4().hex,', source)
        self.assertIn('_spawn_worker_process(agent_id, worker_log, str(meta.get("launch_nonce") or "") or None)', source)


if __name__ == "__main__":
    unittest.main()
