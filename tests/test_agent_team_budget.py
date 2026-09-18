from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents


class TeamBudgetSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        self.spawned: list[dict] = []
        self.counter = 0
        self.patches = [
            patch.object(agents, "provider_enabled", return_value=True),
            patch.object(agents, "_find_binary", return_value="/tmp/fake-opencode"),
            patch.object(agents, "_validate_provider_access_mode", return_value=None),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    def fake_spawn(self, settings=None, provider="opencode", prompt="", model=None, reasoning=None,
                   cwd=None, timeout_s=None, title=None, result_style="concise", access_mode="read_only",
                   scope=None, permission_profile="read_only", capability_profile="legacy",
                   parent_agent_id=None, resume_session_id=None, attempt=1, team_id=None,
                   team_task_id=None, idle_timeout_s=None, retries=0, project=None, role=None,
                   provenance_class="local", **kwargs):
        self.counter += 1
        agent_id = f"agt_budget_{self.counter}"
        path = agents.AGENTS_DIR / agent_id
        path.mkdir(parents=True)
        (path / "result.txt").write_text("", encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id,
            "title": title, "role": role, "provider": provider, "model": model,
            "cwd": cwd, "access_mode": access_mode, "permission_profile": permission_profile,
            "capability_profile": capability_profile,
            "scope": scope.to_dict() if hasattr(scope, "to_dict") else scope,
            "status": "running", "phase": "running", "started_at": now,
            "spawn_requested_at": now, "last_activity_at": now, "updated_at": now,
            "retries": retries, "retry_count": 0, "tool_call_count": 0,
            "usage": None, "attempt": attempt, "parent_agent_id": parent_agent_id,
        })
        self.spawned.append({"agent_id": agent_id, "task_id": team_task_id})
        return {"ok": True, "agent_id": agent_id, "team_task_id": team_task_id, "status": "running", "title": title}

    def spawn_team(self, tasks, **kwargs):
        return agents.spawn_agents(
            settings=None, tasks=tasks, provider="opencode", cwd=str(self.root),
            access_mode="read_only", **kwargs,
        )

    def complete(self, agent_id: str, *, tool_calls: int = 0, tokens: int = 0) -> None:
        def update(meta):
            meta.update({
                "status": "completed", "phase": "completed", "ended_at": time.time(),
                "updated_at": time.time(), "tool_call_count": tool_calls,
                "usage": {"total": tokens} if tokens else None,
            })
        agents._update_meta(agent_id, update)

    def task(self, team_id: str, task_id: str) -> dict:
        return next(item for item in agents._read_team(team_id)["tasks"] if item["id"] == task_id)

    def test_tool_budget_blocks_new_child_and_parent_sees_reason(self) -> None:
        team = self.spawn_team([
            {"id": "first", "prompt": "one"},
            {"id": "second", "prompt": "two", "depends_on": ["first"]},
        ], max_parallel=1, max_total_tool_calls=2)
        team_id = team["team_id"]
        self.assertEqual(["first"], [item["task_id"] for item in self.spawned])
        self.complete(self.spawned[0]["agent_id"], tool_calls=2)
        agents._team_tick(team_id)
        self.assertEqual(["first"], [item["task_id"] for item in self.spawned])
        self.assertEqual("skipped", self.task(team_id, "second")["state"])
        self.assertEqual("budget_exhausted:tool_call_budget", self.task(team_id, "second")["failure_reason"])
        summary = agents._team_summary(team_id)
        self.assertEqual("budget_exhausted", summary["status"])
        self.assertEqual("tool_call_budget", summary["budget_exhausted_reason"])
        self.assertFalse(summary["budget"]["admission_open"])
        self.assertEqual(0, summary["budget"]["tool_calls_remaining"])

    def test_token_budget_blocks_new_child(self) -> None:
        team = self.spawn_team([
            {"id": "first", "prompt": "one"},
            {"id": "second", "prompt": "two", "depends_on": ["first"]},
        ], max_parallel=1, max_total_tokens=100)
        team_id = team["team_id"]
        self.complete(self.spawned[0]["agent_id"], tokens=100)
        agents._team_tick(team_id)
        self.assertEqual("budget_exhausted:token_budget", self.task(team_id, "second")["failure_reason"])
        self.assertEqual(0, agents._team_summary(team_id)["budget"]["total_tokens_remaining"])

    def test_team_deadline_blocks_new_child(self) -> None:
        team = self.spawn_team([
            {"id": "first", "prompt": "one"},
            {"id": "second", "prompt": "two", "depends_on": ["first"]},
        ], max_parallel=1, team_timeout_s=60)
        team_id = team["team_id"]
        self.complete(self.spawned[0]["agent_id"])
        meta = agents._read_team(team_id)
        with patch.object(agents, "_now", return_value=float(meta["deadline_at"]) + 1.0):
            agents._team_tick(team_id)
            summary = agents._team_summary(team_id)
        self.assertEqual(["first"], [item["task_id"] for item in self.spawned])
        self.assertEqual("budget_exhausted:team_deadline", self.task(team_id, "second")["failure_reason"])
        self.assertEqual("team_deadline", summary["budget_exhausted_reason"])
        self.assertEqual(0.0, summary["budget"]["remaining_s"])

    def test_zero_retry_policy_is_closed_but_not_reported_exhausted_until_blocked(self) -> None:
        team = self.spawn_team([{"id": "a", "prompt": "A"}], retries=0, max_team_retries=0)
        budget = team["budget"]
        self.assertFalse(budget["retry_open"])
        self.assertEqual(0, budget["retries_remaining"])
        self.assertFalse(budget["exhausted"])
        self.assertIsNone(budget["exhausted_reason"])

    def test_retry_budget_is_atomic_and_spaces_retries(self) -> None:
        team = self.spawn_team([{"id": "a", "prompt": "A"}], retries=3, max_team_retries=1)
        team_id = team["team_id"]
        with patch.object(agents, "_now", return_value=1000.0):
            first = agents._reserve_team_retry(team_id, "agt_one", "transient_transport", 2.0)
            second = agents._reserve_team_retry(team_id, "agt_two", "transient_transport", 0.0)
        self.assertTrue(first["allowed"])
        self.assertGreaterEqual(first["delay_s"], 2.0)
        self.assertEqual(0, first["remaining"])
        self.assertFalse(second["allowed"])
        self.assertEqual("retry_budget", second["reason"])
        summary = agents._team_summary(team_id)
        self.assertFalse(summary["budget"]["retry_open"])
        self.assertEqual(1, summary["budget"]["retries_used"])
        self.assertEqual("retry_budget", summary["last_retry_block_reason"])

    def test_budget_validation_and_chatgpt_token_fail_closed(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.spawn_team([{"prompt": "x"}], team_timeout_s=30)
        self.assertEqual(400, ctx.exception.status_code)
        with self.assertRaises(HTTPException) as ctx:
            self.spawn_team([{"prompt": "x"}], max_team_retries=31)
        self.assertEqual(400, ctx.exception.status_code)
        with patch.object(agents, "_find_binary", return_value="/tmp/fake-chatgpt"):
            with self.assertRaises(HTTPException) as ctx:
                agents.spawn_agents(
                    settings=None, tasks=[{"prompt": "x"}], provider="chatgpt", cwd=str(self.root),
                    access_mode="read_only", max_total_tokens=1000,
                )
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("does not expose reliable token usage", str(ctx.exception.detail))


class AdaptiveRetryWorkerTests(unittest.TestCase):
    def _worker_fixture(self, root: Path, agent_id: str, *, retries: int = 1, team_id: str | None = None) -> None:
        adir = root / "agents" / agent_id
        adir.mkdir(parents=True)
        input_hash = agents.workflow_input_hash(
            prompt="retry fixture", provider="opencode", cwd=str(root), access_mode="read_only",
            scope={"access_mode": "read_only", "path_roots": [str(root)]}, role=None,
        )
        checkpoint = agents.create_workflow(agent_id=agent_id, input_hash=input_hash, provider="opencode")
        for name, value in {
            "prompt.txt": "retry fixture", "effective_prompt.txt": "retry fixture",
            "stdout.log": "", "stderr.log": "", "worker.log": "", "result.txt": "",
        }.items():
            (adir / name).write_text(value, encoding="utf-8")
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "provider": "opencode", "binary": "/tmp/opencode", "cwd": str(root),
            "team_id": team_id, "team_task_id": "task_a" if team_id else None,
            "status": "starting", "phase": "starting", "started_at": now, "spawn_requested_at": now,
            "last_activity_at": now, "timeout_s": 1200, "idle_timeout_s": None,
            "retries": retries, "retry_count": 0, "result_style": "concise", "access_mode": "read_only",
            "permission_profile": "read_only", "capability_profile": "legacy",
            "scope": {"access_mode": "read_only", "path_roots": [str(root)]},
            "attempt": 1, "workflow_id": checkpoint["workflow_id"], "workflow_input_hash": input_hash,
            "resume_generation": 0, "session_id": None, "resume_session_id": None,
            "tool_call_count": 0, "step_count": 0,
        })

    def test_worker_retries_transient_error_once(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(agents, "TEAMS_DIR", Path(td) / "teams"):
            root = Path(td); agent_id = "agt_transient_budget"
            self._worker_fixture(root, agent_id, retries=1)
            calls: list[int] = []
            def fake_attempt(_agent_id, _meta, _prompt, attempt_index):
                calls.append(attempt_index)
                if attempt_index == 0:
                    (root / "agents" / agent_id / "stderr.log").write_text("503 Service Unavailable\n", encoding="utf-8")
                    return 1, None
                (root / "agents" / agent_id / "stdout.log").write_text(
                    json.dumps({"type":"text","part":{"text":"done"}}) + "\n" +
                    json.dumps({"type":"step_finish","part":{"reason":"stop","tokens":{"total":12}}}) + "\n",
                    encoding="utf-8",
                )
                return 0, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt), patch.object(agents.time, "sleep", return_value=None):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(0, rc)
            self.assertEqual([0, 1], calls)
            self.assertEqual("completed", saved["status"])
            self.assertEqual("provider_unavailable", saved["last_retry_reason"])
            self.assertTrue(saved["last_retryable"])

    def test_worker_fails_fast_on_auth_error(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(agents, "TEAMS_DIR", Path(td) / "teams"):
            root = Path(td); agent_id = "agt_auth_budget"
            self._worker_fixture(root, agent_id, retries=2)
            calls: list[int] = []
            def fake_attempt(_agent_id, _meta, _prompt, attempt_index):
                calls.append(attempt_index)
                (root / "agents" / agent_id / "stderr.log").write_text("401 Unauthorized: authentication failed\n", encoding="utf-8")
                return 1, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(1, rc)
            self.assertEqual([0], calls)
            self.assertEqual("auth_error", saved["retry_blocked_reason"])
            self.assertFalse(saved["last_retryable"])
            self.assertIn("non-retryable", saved["note"])

    def test_worker_team_retry_budget_blocks_transient_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MAC_MCP_STATE_DIR": str(Path(td) / "state")}, clear=False,
        ), patch.object(agents, "AGENTS_DIR", Path(td) / "agents"), patch.object(agents, "TEAMS_DIR", Path(td) / "teams"):
            root = Path(td); agent_id = "agt_teamretry_budget"; team_id = "team_retry_budget"
            self._worker_fixture(root, agent_id, retries=2, team_id=team_id)
            now = time.time()
            agents._write_team(team_id, {
                "team_id": team_id, "created_at": now, "updated_at": now, "team_timeout_s": 3600,
                "deadline_at": now + 3600, "max_parallel": 1, "max_team_retries": 0, "team_retry_count": 0,
                "max_total_tool_calls": None, "max_total_tokens": None, "agent_ids": [agent_id],
                "tasks": [{"id":"task_a","state":"running","active_agent_id":agent_id}],
            })
            calls: list[int] = []
            def fake_attempt(_agent_id, _meta, _prompt, attempt_index):
                calls.append(attempt_index)
                (root / "agents" / agent_id / "stderr.log").write_text("connection reset by peer\n", encoding="utf-8")
                return 1, None
            with patch.object(agents, "_run_provider_attempt", side_effect=fake_attempt):
                rc = agents._worker(agent_id)
            saved = agents._read_meta(agent_id)
            self.assertEqual(1, rc)
            self.assertEqual([0], calls)
            self.assertEqual("retry_budget", saved["retry_blocked_reason"])
            self.assertEqual(0, saved["team_retry_remaining"])
            self.assertIn("team budget", saved["note"])


class TeamBudgetRetryPropagationTests(unittest.TestCase):
    def test_manual_team_retry_preserves_budget_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.object(agents, "TEAMS_DIR", Path(td) / "teams"):
            team_id = "team_budget_retry_propagation"
            now = time.time()
            agents._write_team(team_id, {
                "team_id": team_id, "scheduler_version": 1, "provider": "opencode", "model": "test/model",
                "reasoning": "high", "cwd": td, "timeout_s": 900, "idle_timeout_s": 120, "retries": 2,
                "result_style": "concise", "access_mode": "read_only", "scope": {"access_mode":"read_only","path_roots":[td]},
                "role": None, "provenance_class": "local", "project": None, "title": "Budget team",
                "max_parallel": 2, "max_revisions": 1, "team_timeout_s": 5400, "deadline_at": now + 5400,
                "max_team_retries": 3, "team_retry_count": 1, "max_total_tool_calls": 40, "max_total_tokens": 50000,
                "created_at": now, "updated_at": now, "agent_ids": [], "cancelled": False,
                "tasks": [
                    {"id":"a","prompt":"A","title":"A","scope":{"access_mode":"read_only","path_roots":[td]},"project":None,"role":None,"depends_on":[],"review_of":None,"max_revisions":1},
                    {"id":"b","prompt":"B","title":"B","scope":{"access_mode":"read_only","path_roots":[td]},"project":None,"role":None,"depends_on":["a"],"review_of":None,"max_revisions":1},
                ],
            })
            with patch.object(agents, "spawn_agents", return_value={"ok":True,"team_id":"team_new"}) as spawn:
                result = agents.agent_action(None, action="retry", team_id=team_id)
            self.assertTrue(result["ok"])
            kwargs = spawn.call_args.kwargs
            self.assertEqual(2, kwargs["max_parallel"])
            self.assertEqual(1, kwargs["max_revisions"])
            self.assertEqual(5400, kwargs["team_timeout_s"])
            self.assertEqual(3, kwargs["max_team_retries"])
            self.assertEqual(40, kwargs["max_total_tool_calls"])
            self.assertEqual(50000, kwargs["max_total_tokens"])


class AdaptiveRetryClassifierTests(unittest.TestCase):
    def classify(self, *, stderr: str = "", stdout: str = "", exit_code: int = 1, stop_reason=None, meta=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "stdout.log"; err = root / "stderr.log"
            out.write_text(stdout, encoding="utf-8"); err.write_text(stderr, encoding="utf-8")
            return agents._adaptive_retry_decision(meta or {}, exit_code, stop_reason, out, err)

    def test_timeout_and_transient_provider_errors_retry(self) -> None:
        self.assertEqual("timeout", self.classify(stop_reason="timeout")["reason"])
        self.assertTrue(self.classify(stop_reason="timeout")["retryable"])
        decision = self.classify(stderr="upstream 503 Service Unavailable")
        self.assertTrue(decision["retryable"])
        self.assertEqual("provider_unavailable", decision["reason"])

    def test_classifier_ignores_previous_attempt_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); out = root / "stdout.log"; err = root / "stderr.log"
            out.write_text("", encoding="utf-8")
            previous = "503 Service Unavailable\n"
            err.write_text(previous + "provider exited unexpectedly\n", encoding="utf-8")
            decision = agents._adaptive_retry_decision(
                {}, 1, None, out, err, 0, len(previous.encode("utf-8")),
            )
        self.assertFalse(decision["retryable"])
        self.assertEqual("provider_error_nonretryable", decision["reason"])

    def test_auth_invalid_and_unknown_errors_fail_fast(self) -> None:
        auth = self.classify(stderr="401 Unauthorized: authentication failed")
        self.assertFalse(auth["retryable"])
        self.assertEqual("auth_error", auth["reason"])
        invalid = self.classify(stderr="400 Bad Request: invalid model")
        self.assertFalse(invalid["retryable"])
        unknown = self.classify(stderr="provider exited unexpectedly")
        self.assertFalse(unknown["retryable"])
        self.assertEqual("provider_error_nonretryable", unknown["reason"])


if __name__ == "__main__":
    unittest.main()
