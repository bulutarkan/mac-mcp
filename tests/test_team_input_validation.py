"""spawn_agents checks every setting before it creates a team (#105).

A bad value never leaves an empty failed team behind: one 400 lists every problem with what is
allowed. A task that still cannot start settles the tasks that depend on it at once.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_agents as agents


class TeamInputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents, self.old_teams = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        self.fail_tasks: set[str] = set()
        self.spawned: list[str] = []
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
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.old_agents, self.old_teams
        self.temp.cleanup()

    def fake_spawn(self, *, team_id=None, team_task_id=None, title=None, **_kwargs):
        if team_task_id in self.fail_tasks:
            raise HTTPException(400, "model must be one of: a, b (got 'zzz').")
        agent_id = f"agt_fake_{len(self.spawned) + 1}"
        (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
        now = time.time()
        agents._write_meta(agent_id, {
            "agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id, "title": title,
            "provider": "opencode", "status": "running", "phase": "running", "started_at": now,
            "spawn_requested_at": now, "last_activity_at": now, "updated_at": now,
        })
        self.spawned.append(team_task_id)
        return {"ok": True, "agent_id": agent_id, "team_task_id": team_task_id, "status": "running", "title": title}

    def spawn(self, tasks, **kwargs):
        return agents.spawn_agents(settings=None, tasks=tasks, provider=kwargs.pop("provider", "opencode"),
                                   cwd=str(self.root), access_mode=kwargs.pop("access_mode", "read_only"), **kwargs)

    def teams_on_disk(self) -> list:
        return list(agents.TEAMS_DIR.glob("team_*")) if agents.TEAMS_DIR.exists() else []

    def test_every_problem_comes_back_in_one_400_and_no_team_is_created(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.spawn(
                [
                    {"id": "a", "prompt": "A", "role": "boss"},
                    {"id": "b", "prompt": "", "provider": "nope", "max_revisions": 9},
                    {"id": "c", "prompt": "C", "depends_on": "a"},
                ],
                result_style="brief", conflict_policy="maybe", git_isolation="sometimes",
                max_parallel=0, team_timeout_s=5, access_mode="writeish",
            )
        self.assertEqual(400, caught.exception.status_code)
        detail = str(caught.exception.detail)
        self.assertIn("nothing was started", detail)
        for expected in (
            "result_style must be one of: concise, detailed (got 'brief')",
            "conflict_policy must be one of:",
            "git_isolation must be one of:",
            "max_parallel must be between 1 and 10 (got 0)",
            "team_timeout_s must be between 60 and",
            "access_mode must be one of:",
            "tasks[0].role must be one of:",
            "tasks[1].prompt is required.",
            "tasks[1].provider must be one of:",
            "tasks[1].max_revisions must be between 0 and 3 (got 9)",
        ):
            self.assertIn(expected, detail)
        self.assertEqual([], self.teams_on_disk())
        self.assertEqual([], self.spawned)

    def test_a_single_problem_reads_as_before(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.spawn([{"id": "a", "prompt": "A"}], result_style="brief")
        self.assertEqual("result_style must be one of: concise, detailed (got 'brief').", caught.exception.detail)
        self.assertEqual([], self.teams_on_disk())

    def test_non_integer_numbers_are_a_400_not_a_crash(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.spawn([{"id": "a", "prompt": "A"}], timeout_s="soon", retries="x")
        self.assertEqual(400, caught.exception.status_code)
        self.assertIn("timeout_s must be an integer (got 'soon')", caught.exception.detail)
        self.assertIn("retries must be an integer (got 'x')", caught.exception.detail)

    def test_graph_problems_are_checked_before_creation_too(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.spawn([{"id": "a", "prompt": "A", "depends_on": ["ghost"]}])
        self.assertIn("depends on unknown task: ghost", caught.exception.detail)
        self.assertEqual([], self.teams_on_disk())

    def test_valid_team_still_starts(self) -> None:
        team = self.spawn([{"id": "a", "prompt": "A"}, {"id": "b", "prompt": "B", "depends_on": ["a"]}],
                          result_style="detailed")
        self.assertEqual(["a"], self.spawned)
        self.assertEqual("detailed", agents._read_team(team["team_id"])["result_style"])

    def test_a_task_that_cannot_start_skips_its_dependents_at_once(self) -> None:
        self.fail_tasks = {"a"}
        team = self.spawn([
            {"id": "a", "prompt": "A"},
            {"id": "b", "prompt": "B", "depends_on": ["a"]},
            {"id": "c", "prompt": "C", "depends_on": ["b"]},
        ])
        stored = {task["id"]: task for task in agents._read_team(team["team_id"])["tasks"]}
        self.assertEqual("failed", stored["a"]["state"])
        self.assertTrue(stored["a"]["failure_reason"].startswith("spawn_failed:model must be one of"))
        self.assertEqual(("skipped", "dependency_failed:a"), (stored["b"]["state"], stored["b"]["failure_reason"]))
        self.assertEqual(("skipped", "dependency_failed:b"), (stored["c"]["state"], stored["c"]["failure_reason"]))
        self.assertIn(team["status"], {"failed", "completed_with_failures"})  # settled, not running

    def test_spawn_internal_uses_the_same_rule(self) -> None:
        self.assertEqual(
            agents._choice_problem("result_style", "brief", agents._RESULT_STYLES),
            "result_style must be one of: concise, detailed (got 'brief').",
        )


if __name__ == "__main__":
    unittest.main()
