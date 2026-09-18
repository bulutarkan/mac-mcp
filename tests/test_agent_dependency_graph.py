from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents


class DependencyGraphSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        self.spawned: list[dict] = []
        self.prompts: dict[str, str] = {}
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
        agent_id = f"agt_fake_{self.counter}"
        path = agents.AGENTS_DIR / agent_id
        path.mkdir(parents=True)
        (path / "result.txt").write_text("", encoding="utf-8")
        (path / "prompt.txt").write_text(prompt, encoding="utf-8")
        now = time.time()
        meta = {
            "agent_id": agent_id,
            "team_id": team_id,
            "team_task_id": team_task_id,
            "title": title,
            "role": role,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "project": project,
            "cwd": cwd,
            "access_mode": access_mode,
            "permission_profile": permission_profile,
            "capability_profile": capability_profile,
            "scope": scope.to_dict() if hasattr(scope, "to_dict") else scope,
            "status": "running",
            "phase": "running",
            "started_at": now,
            "spawn_requested_at": now,
            "last_activity_at": now,
            "updated_at": now,
            "retries": retries,
            "retry_count": 0,
            "attempt": attempt,
            "parent_agent_id": parent_agent_id,
        }
        agents._write_meta(agent_id, meta)
        self.spawned.append({"agent_id": agent_id, "task_id": team_task_id, "parent_agent_id": parent_agent_id})
        self.prompts[agent_id] = prompt
        return {"ok": True, "agent_id": agent_id, "team_task_id": team_task_id, "status": "running", "title": title}

    def complete(self, agent_id: str, result: str = "done", status: str = "completed") -> None:
        (agents.AGENTS_DIR / agent_id / "result.txt").write_text(result, encoding="utf-8")
        def update(meta):
            meta.update({"status": status, "phase": status, "ended_at": time.time(), "updated_at": time.time()})
        agents._update_meta(agent_id, update)

    def task(self, team_id: str, task_id: str) -> dict:
        team = agents._read_team(team_id)
        return next(item for item in team["tasks"] if item["id"] == task_id)

    def spawn_team(self, tasks, **kwargs):
        return agents.spawn_agents(
            settings=None,
            tasks=tasks,
            provider="opencode",
            cwd=str(self.root),
            access_mode="read_only",
            **kwargs,
        )

    def test_dag_runs_roots_in_parallel_then_dependent(self) -> None:
        team = self.spawn_team([
            {"id": "a", "prompt": "A"},
            {"id": "b", "prompt": "B"},
            {"id": "c", "prompt": "C", "depends_on": ["a", "b"]},
        ], max_parallel=2)
        team_id = team["team_id"]
        self.assertEqual(["a", "b"], [item["task_id"] for item in self.spawned])
        self.assertEqual("blocked", self.task(team_id, "c")["state"])

        self.complete(self.spawned[0]["agent_id"], "A result")
        agents._team_tick(team_id)
        self.assertEqual(["a", "b"], [item["task_id"] for item in self.spawned])

        self.complete(self.spawned[1]["agent_id"], "B result")
        agents._team_tick(team_id)
        self.assertEqual(["a", "b", "c"], [item["task_id"] for item in self.spawned])
        self.assertIn("A result", self.prompts[self.spawned[2]["agent_id"]])
        self.assertIn("B result", self.prompts[self.spawned[2]["agent_id"]])

        self.complete(self.spawned[2]["agent_id"], "C result")
        agents._team_tick(team_id)
        self.assertEqual("completed", agents._team_summary(team_id)["status"])

    def test_max_parallel_one_serializes_independent_tasks(self) -> None:
        team = self.spawn_team([
            {"id": "a", "prompt": "A"},
            {"id": "b", "prompt": "B"},
            {"id": "c", "prompt": "C"},
        ], max_parallel=1)
        team_id = team["team_id"]
        self.assertEqual(["a"], [item["task_id"] for item in self.spawned])
        self.complete(self.spawned[-1]["agent_id"])
        agents._team_tick(team_id)
        self.assertEqual(["a", "b"], [item["task_id"] for item in self.spawned])
        self.complete(self.spawned[-1]["agent_id"])
        agents._team_tick(team_id)
        self.assertEqual(["a", "b", "c"], [item["task_id"] for item in self.spawned])

    def test_cycle_is_rejected_before_team_creation(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.spawn_team([
                {"id": "a", "prompt": "A", "depends_on": ["b"]},
                {"id": "b", "prompt": "B", "depends_on": ["a"]},
            ])
        self.assertEqual(400, ctx.exception.status_code)
        self.assertIn("cycle", str(ctx.exception.detail).lower())
        self.assertFalse(agents.TEAMS_DIR.exists())

    def test_failed_dependency_skips_downstream(self) -> None:
        team = self.spawn_team([
            {"id": "a", "prompt": "A"},
            {"id": "b", "prompt": "B", "depends_on": ["a"]},
        ])
        team_id = team["team_id"]
        self.complete(self.spawned[0]["agent_id"], "boom", status="failed")
        agents._team_tick(team_id)
        self.assertEqual("skipped", self.task(team_id, "b")["state"])
        self.assertEqual("completed_with_failures", agents._team_summary(team_id)["status"])
        self.assertEqual(["a"], [item["task_id"] for item in self.spawned])

    def test_reviewer_fail_triggers_one_revision_then_pass(self) -> None:
        team = self.spawn_team([
            {"id": "coder", "prompt": "Implement feature", "role": "coder"},
            {"id": "review", "prompt": "Review carefully", "review_of": "coder"},
            {"id": "ship", "prompt": "Prepare final handoff", "depends_on": ["coder"]},
        ], max_parallel=2, max_revisions=1)
        team_id = team["team_id"]
        self.assertEqual(["coder"], [item["task_id"] for item in self.spawned])

        self.complete(self.spawned[-1]["agent_id"], "candidate-v1")
        agents._team_tick(team_id)
        self.assertEqual(["coder", "review"], [item["task_id"] for item in self.spawned])
        self.assertEqual("blocked", self.task(team_id, "ship")["state"])
        review1 = self.spawned[-1]["agent_id"]
        self.assertIn("QUALITY_GATE: PASS", self.prompts[review1])
        self.assertIn("candidate-v1", self.prompts[review1])

        self.complete(review1, "Fix the edge case.\nQUALITY_GATE: FAIL")
        agents._team_tick(team_id)
        self.assertEqual(["coder", "review", "coder"], [item["task_id"] for item in self.spawned])
        coder2 = self.spawned[-1]["agent_id"]
        self.assertIn("Fix the edge case", self.prompts[coder2])
        self.assertIn("candidate-v1", self.prompts[coder2])
        self.assertEqual(1, self.task(team_id, "coder")["revision_count"])
        self.assertEqual(self.spawned[0]["agent_id"], self.spawned[-1]["parent_agent_id"])

        self.complete(coder2, "candidate-v2")
        agents._team_tick(team_id)
        self.assertEqual(["coder", "review", "coder", "review"], [item["task_id"] for item in self.spawned])
        review2 = self.spawned[-1]["agent_id"]
        self.assertIn("candidate-v2", self.prompts[review2])

        self.complete(review2, "Looks correct.\nQUALITY_GATE: PASS")
        agents._team_tick(team_id)
        # Gate PASS now allows the dependent ship task to launch.
        self.assertEqual("ship", self.spawned[-1]["task_id"])
        self.complete(self.spawned[-1]["agent_id"], "shipped")
        agents._team_tick(team_id)
        summary = agents._team_summary(team_id)
        self.assertEqual("completed", summary["status"])
        self.assertEqual("pass", self.task(team_id, "review")["gate_result"])

    def test_revision_limit_blocks_completion(self) -> None:
        team = self.spawn_team([
            {"id": "coder", "prompt": "Implement"},
            {"id": "review", "prompt": "Review", "review_of": "coder"},
        ], max_revisions=1)
        team_id = team["team_id"]
        self.complete(self.spawned[-1]["agent_id"], "v1")
        agents._team_tick(team_id)
        self.complete(self.spawned[-1]["agent_id"], "needs change\nQUALITY_GATE: FAIL")
        agents._team_tick(team_id)
        self.complete(self.spawned[-1]["agent_id"], "v2")
        agents._team_tick(team_id)
        self.complete(self.spawned[-1]["agent_id"], "still wrong\nQUALITY_GATE: FAIL")
        agents._team_tick(team_id)
        summary = agents._team_summary(team_id)
        self.assertEqual("quality_failed", summary["status"])
        review = self.task(team_id, "review")
        self.assertEqual("quality_failed", review["state"])
        self.assertEqual("revision_limit_exhausted", review["failure_reason"])

    def test_zero_revision_gate_fails_without_retry(self) -> None:
        team = self.spawn_team([
            {"id": "coder", "prompt": "Implement"},
            {"id": "review", "prompt": "Review", "review_of": "coder", "max_revisions": 0},
        ], max_revisions=2)
        team_id = team["team_id"]
        self.complete(self.spawned[-1]["agent_id"], "v1")
        agents._team_tick(team_id)
        self.complete(self.spawned[-1]["agent_id"], "reject\nQUALITY_GATE: FAIL")
        agents._team_tick(team_id)
        self.assertEqual(["coder", "review"], [item["task_id"] for item in self.spawned])
        self.assertEqual("quality_failed", agents._team_summary(team_id)["status"])
        self.assertEqual(0, self.task(team_id, "review")["max_revisions"])

    def test_invalid_reviewer_contract_fails_closed(self) -> None:
        team = self.spawn_team([
            {"id": "coder", "prompt": "Implement"},
            {"id": "review", "prompt": "Review", "review_of": "coder"},
        ])
        team_id = team["team_id"]
        self.complete(self.spawned[-1]["agent_id"], "candidate")
        agents._team_tick(team_id)
        self.complete(self.spawned[-1]["agent_id"], "Looks okay but no structured marker")
        agents._team_tick(team_id)
        summary = agents._team_summary(team_id)
        self.assertEqual("quality_failed", summary["status"])
        self.assertEqual("invalid_quality_gate_contract", self.task(team_id, "review")["failure_reason"])


if __name__ == "__main__":
    unittest.main()
