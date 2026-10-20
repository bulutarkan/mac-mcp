"""Roadmap #47-#50 and the provider_state cleanup.

#47  a team task left "spawning" by a server stop takes over the agent that did start.
#48  a dead worker's provider is stopped (TERM, then KILL) before the agent counts as failed.
#49  an agent that fails before writing a typed result says its contract is missing, not pending.
#50  a team queued by memory pressure is woken again when the pressure hold runs out.
     Finished agents drop the caches in their private provider home, but not their sessions.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_admission as admission
from mcp_server import tools_agents as agents


class _TempAgents(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents, self.old_teams = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()

    def tearDown(self) -> None:
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.old_agents, self.old_teams
        self.temp.cleanup()


class TeamSchedulingTests(_TempAgents):
    def setUp(self) -> None:
        super().setUp()
        self.counter = 0
        env = patch.dict(os.environ, {
            "MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "4", "MAC_MCP_AGENT_PROVIDER_LIMIT": "4",
            "MAC_MCP_AGENT_ADMISSION_TTL_S": "60",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for item in (
            patch.object(agents, "provider_enabled", return_value=True),
            patch.object(agents, "_find_binary", return_value="/tmp/fake-opencode"),
            patch.object(agents, "_validate_provider_access_mode", return_value=None),
            patch.object(agents, "_require_provider_sign_in", return_value=None),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_spawn(self, *, team_id=None, team_task_id=None, title=None, **kwargs):
        self.counter += 1
        agent_id = f"agt_recover_{self.counter}"
        (agents.AGENTS_DIR / agent_id).mkdir()
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id, "title": title,
            "provider": "opencode", "status": "running", "phase": "running", "started_at": now,
            "spawn_requested_at": now, "updated_at": now,
            "admission_lease_id": kwargs.get("admission_lease_id"),
        })
        if kwargs.get("admission_lease_id"):
            admission.bind_agent(agents.AGENTS_DIR, kwargs["admission_lease_id"], agent_id)
        return {"ok": True, "agent_id": agent_id, "status": "running"}

    def spawn_team(self) -> str:
        cwd = self.root / "work"
        cwd.mkdir(exist_ok=True)
        return agents.spawn_agents(
            settings=None, tasks=[{"id": "task", "prompt": "do it"}], provider="opencode", cwd=str(cwd),
            access_mode="workspace_write", retries=0, max_parallel=1, git_isolation="auto", title="t",
        )["team_id"]

    def forget_agent(self, team_id: str) -> None:
        """What the team file looks like when the server stopped inside _spawn_internal."""
        def update(team):
            task = team["tasks"][0]
            task.update({"state": "spawning", "active_agent_id": None, "latest_agent_id": None, "agent_ids": [],
                         "spawn_started_at": time.time() - 5})
            team["agent_ids"] = []
        with agents._locked_team(team_id):
            team = agents._read_team_unlocked(team_id)
            update(team)
            agents._write_team_unlocked(team_id, team)

    def test_a_task_left_spawning_takes_over_the_agent_that_started(self) -> None:
        team_id = self.spawn_team()
        self.forget_agent(team_id)
        task = agents._team_tick(team_id)["tasks"][0]
        self.assertEqual("running", task["state"])
        self.assertEqual("agt_recover_1", task["active_agent_id"])
        self.assertEqual(["agt_recover_1"], task["agent_ids"])
        self.assertIn("agt_recover_1", agents._read_team(team_id)["agent_ids"])
        self.assertEqual(1, self.counter)

    def test_a_task_left_spawning_without_an_agent_still_fails(self) -> None:
        team_id = self.spawn_team()
        self.forget_agent(team_id)
        (agents.AGENTS_DIR / "agt_recover_1" / "meta.json").unlink()
        task = agents._team_tick(team_id)["tasks"][0]
        self.assertEqual("failed", task["state"])
        self.assertEqual("missing_active_agent", task["failure_reason"])

    def test_spawn_records_when_it_started(self) -> None:
        source = Path(agents.__file__).read_text(encoding="utf-8")
        spawning = source[source.index('task["state"] = "spawning"'):source.index("prompt = _team_task_prompt(")]
        self.assertIn('task["spawn_started_at"] = _now()', spawning)

    def test_memory_pressure_queue_schedules_a_wake(self) -> None:
        wakes: list[tuple[str, float]] = []
        with patch.object(admission, "memory_pressure", return_value="critical"), \
             patch.object(agents, "_schedule_team_wake", side_effect=lambda team_id, delay: wakes.append((team_id, delay))):
            team_id = self.spawn_team()
        task = agents._read_team(team_id)["tasks"][0]
        self.assertEqual("queued", task["state"])
        self.assertEqual("memory_pressure", task["queued_reason"])
        self.assertTrue(wakes)
        self.assertEqual({(team_id, admission.MEMORY_PRESSURE_HOLD_S + 1.0)}, set(wakes))
        self.assertEqual(0, self.counter)
        # Once the pressure is gone, the wake starts the task.
        agents._team_tick(team_id)
        self.assertEqual("running", agents._read_team(team_id)["tasks"][0]["state"])


class TeamWakeTimerTests(unittest.TestCase):
    def test_one_pending_wake_per_team_and_it_ticks(self) -> None:
        ticked: list[str] = []
        done = threading.Event()

        def tick(team_id):
            ticked.append(team_id)
            done.set()

        with patch.object(agents, "_team_tick", side_effect=tick):
            agents._schedule_team_wake("team_wake_test", 0.2)
            agents._schedule_team_wake("team_wake_test", 0.2)
            self.assertTrue(done.wait(3))
            time.sleep(0.3)
        self.assertEqual(["team_wake_test"], ticked)
        self.assertNotIn("team_wake_test", agents._TEAM_WAKE_TIMERS)


class DeadWorkerTests(_TempAgents):
    def setUp(self) -> None:
        super().setUp()
        self.releases: list[str] = []
        for item in (
            patch.object(agents, "_release_agent_admission", side_effect=lambda agent_id, meta=None: self.releases.append(agent_id)),
            patch.object(agents, "_converge_workflow_terminal"),
            patch.object(agents, "_refresh_agent_worktree"),
            patch.object(agents.browser_tabs, "release_agent_leases"),
        ):
            item.start()
            self.addCleanup(item.stop)
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        self.dead_pid = gone.pid

    def provider(self, ignore_term: bool) -> subprocess.Popen:
        code = "import signal, sys, time\n"
        if ignore_term:
            code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        code += "print('ready', flush=True)\ntime.sleep(30)\n"
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        self.assertEqual("ready", proc.stdout.readline().strip())
        return proc

    def agent(self, provider: subprocess.Popen) -> str:
        agent_id = "agt_orphan"
        (agents.AGENTS_DIR / agent_id).mkdir()
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "status": "running", "phase": "running", "provider": "codex",
            "started_at": now, "updated_at": now, "worker_pid": self.dead_pid, "worker_started_at": now,
            "provider_pid": provider.pid, "provider_started_at": now, "result_contract_status": "pending",
        })
        return agent_id

    def read(self, agent_id: str) -> dict:
        return agents._normalize(agent_id, agents._read_meta(agent_id))

    def test_provider_is_stopped_before_the_agent_fails(self) -> None:
        proc = self.provider(ignore_term=False)
        agent_id = self.agent(proc)
        meta = self.read(agent_id)
        self.assertEqual("running", meta["status"])
        self.assertTrue(meta["orphan_stop_requested_at"])
        self.assertEqual([], self.releases)
        self.assertEqual(-signal.SIGTERM, proc.wait(timeout=5))
        meta = self.read(agent_id)
        self.assertEqual("failed", meta["status"])
        self.assertEqual("missing", meta["result_contract_status"])
        self.assertEqual("failure", meta["result_outcome"])
        self.assertEqual([agent_id], self.releases)

    def test_a_provider_that_ignores_term_is_killed_after_the_grace(self) -> None:
        proc = self.provider(ignore_term=True)
        agent_id = self.agent(proc)
        self.assertEqual("running", self.read(agent_id)["status"])
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        self.assertEqual("running", self.read(agent_id)["status"])  # still inside the grace period
        agents._update_meta(agent_id, lambda current: current.update(
            {"orphan_stop_requested_at": time.time() - agents._CANCEL_GRACE_S - 1}))
        self.assertEqual("running", self.read(agent_id)["status"])
        self.assertEqual(-signal.SIGKILL, proc.wait(timeout=5))
        self.assertEqual("failed", self.read(agent_id)["status"])
        self.assertEqual([agent_id], self.releases)

    def test_never_started_agent_has_a_missing_contract(self) -> None:
        agent_id = "agt_never"
        (agents.AGENTS_DIR / agent_id).mkdir()
        old = time.time() - 600
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "status": "starting", "phase": "starting", "provider": "codex",
            "started_at": old, "spawn_requested_at": old, "updated_at": old, "worker_pid": None,
            "result_contract_status": "pending",
        })
        meta = self.read(agent_id)
        self.assertEqual("failed", meta["status"])
        self.assertEqual("missing", meta["result_contract_status"])

    def test_worker_exception_closes_the_contract(self) -> None:
        source = Path(agents.__file__).read_text(encoding="utf-8")
        branch = source[source.index("def record_worker_failure("):source.index("meta = _update_meta(agent_id, record_worker_failure)")]
        self.assertIn("_close_pending_contract(current)", branch)
        meta = {"result_contract_status": "valid"}
        agents._close_pending_contract(meta)
        self.assertEqual({"result_contract_status": "valid"}, meta)


class ProviderStateTrimTests(_TempAgents):
    def make(self, agent_id: str, *, status: str, ended_ago: float = 3600, **extra) -> Path:
        root = agents.AGENTS_DIR / agent_id
        state = root / "provider_state"
        for rel in ("home/.npm/_cacache", "cache/x", "tmp", "codex/codex-home/cache", "codex/codex-home/plugins/p",
                    "codex/codex-home/sessions/2026", "codex/tmp", "claude/tmp"):
            (state / rel).mkdir(parents=True, exist_ok=True)
        (state / "home/.npm/_cacache/blob").write_bytes(b"x" * 1000)
        (state / "codex/codex-home/plugins/p/plugin.js").write_bytes(b"y" * 500)
        (state / "codex/codex-home/sessions/2026/rollout.jsonl").write_text("{}", encoding="utf-8")
        (state / "codex/codex-home/state.sqlite").write_bytes(b"db")
        (state / "codex/codex-home/config.toml").write_text("model = 'x'", encoding="utf-8")
        (root / "provider_secrets/codex").mkdir(parents=True)
        (root / "provider_secrets/codex/auth.json").write_text("{}", encoding="utf-8")
        (state / "codex/codex-home/auth.json").symlink_to(root / "provider_secrets/codex/auth.json")
        now = time.time()
        meta = {"agent_id": agent_id, "status": status, "provider": "codex", "started_at": now - ended_ago - 60,
                "updated_at": now - ended_ago, **extra}
        if status in agents.TERMINAL_STATUSES:
            meta["ended_at"] = now - ended_ago
        (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return root

    def test_finished_agents_lose_caches_and_keep_sessions(self) -> None:
        done = self.make("agt_done", status="completed")
        result = agents.trim_finished_provider_state()
        self.assertEqual(["agt_done"], result["trimmed"])
        self.assertGreaterEqual(result["freed_bytes"], 1500)
        state = done / "provider_state"
        for rel in ("home/.npm", "cache", "tmp", "codex/codex-home/cache", "codex/codex-home/plugins", "codex/tmp",
                    "claude/tmp"):
            self.assertFalse((state / rel).exists(), rel)
        self.assertFalse((done / "provider_secrets").exists())
        for rel in ("codex/codex-home/sessions/2026/rollout.jsonl", "codex/codex-home/state.sqlite",
                    "codex/codex-home/config.toml"):
            self.assertTrue((state / rel).exists(), rel)
        self.assertEqual([], agents.trim_finished_provider_state()["trimmed"])

    def test_running_recent_and_shared_states_are_left_alone(self) -> None:
        self.make("agt_running", status="running")
        self.make("agt_recent", status="completed", ended_ago=60)
        self.make("agt_first", status="completed")
        self.make("agt_resumed", status="running", parent_agent_id="agt_first", resume_session_id="sess")
        self.assertEqual([], agents.trim_finished_provider_state()["trimmed"])
        for agent_id in ("agt_running", "agt_recent", "agt_first"):
            self.assertTrue((agents.AGENTS_DIR / agent_id / "provider_secrets/codex/auth.json").exists(), agent_id)

    def test_the_history_sweep_runs_the_trim(self) -> None:
        source = Path(agents.__file__).read_text(encoding="utf-8")
        expiry = source[source.index("def _maybe_expire_agent_history() -> None:"):]
        sweep = expiry[expiry.index("    def sweep() -> None:"):]
        sweep = sweep[:sweep.index("finally:")]
        self.assertIn("expire_agent_history()", sweep)
        self.assertIn("trim_finished_provider_state()", sweep)


if __name__ == "__main__":
    unittest.main()
