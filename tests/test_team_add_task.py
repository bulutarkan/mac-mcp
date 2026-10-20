"""#74: the lead adds a task to a team that is running or has finished, without starting a new team.

The new task is checked like a spawn_agents task, waits for its depends_on and never makes finished work run
again. The plan has a revision, so two changes made at the same time cannot both win.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import team_messages as tm
from mcp_server import tools_agents as agents
from mcp_server.security import load_settings
from tests.test_team_task_providers import TeamTaskProviderTests


class TeamAddTaskTests(TeamTaskProviderTests):
    def add(self, team_id, task, **kwargs):
        return agents.agent_action(load_settings(), "add_task", team_id=team_id, task=task, **kwargs)

    def finish(self, team_id, agent_id):
        agents._update_meta(agent_id, lambda meta: meta.update(status="completed"))
        return agents._team_tick(team_id)

    def states(self, team_id):
        return {task["id"]: task["state"] for task in agents._read_team(team_id)["tasks"]}

    def test_a_task_that_depends_on_running_work_starts_when_it_completes(self) -> None:
        team_id = self.spawn([{"id": "api", "prompt": "build the api"}])["team_id"]
        added = self.add(team_id, {"id": "ui", "prompt": "build the ui on it", "depends_on": ["api"]})
        self.assertEqual(("ui", "blocked", 1), (added["task_id"], added["task_state"], added["plan_revision"]))
        self.assertEqual(1, added["team"]["plan_revision"])
        self.assertEqual(["api"], [row["task"] for row in self.spawned])
        self.finish(team_id, "agt_1")
        self.assertEqual(["api", "ui"], [row["task"] for row in self.spawned])  # api is not run again
        self.assertEqual({"api": "completed", "ui": "running"}, self.states(team_id))

    def test_an_independent_task_runs_beside_a_team_that_ran_everything_at_once(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        added = self.add(team_id, {"id": "b", "prompt": "y", "provider": "codex"})
        self.assertEqual("running", added["task_state"])
        self.assertEqual([("a", "opencode"), ("b", "codex")], [(row["task"], row["provider"]) for row in self.spawned])
        self.assertEqual(2, agents._read_team(team_id)["max_parallel"])

    def test_a_team_held_to_fewer_parallel_tasks_stays_held(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}], max_parallel=1)["team_id"]
        self.assertEqual("ready", self.add(team_id, {"id": "c", "prompt": "z"})["task_state"])
        self.assertEqual(1, agents._read_team(team_id)["max_parallel"])

    def test_a_finished_team_takes_a_follow_up_task(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        self.finish(team_id, "agt_1")
        added = self.add(team_id, {"id": "follow_up", "prompt": "use a", "depends_on": ["a"]})
        self.assertEqual("running", added["task_state"])
        self.assertEqual(["a", "follow_up"], [row["task"] for row in self.spawned])

    def test_a_stale_plan_revision_is_a_conflict(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        self.add(team_id, {"id": "b", "prompt": "y"}, expected_revision=0)
        with self.assertRaises(HTTPException) as stale:
            self.add(team_id, {"id": "c", "prompt": "z"}, expected_revision=0)
        self.assertEqual(409, stale.exception.status_code)
        self.assertEqual(("plan_conflict", 1), (stale.exception.detail["error"], stale.exception.detail["plan_revision"]))
        self.assertEqual(["a", "b"], list(self.states(team_id)))
        self.assertEqual(2, self.add(team_id, {"id": "c", "prompt": "z"}, expected_revision=1)["plan_revision"])

    def test_bad_tasks_are_refused_and_change_nothing(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        self.enabled.discard("claude")
        cases = [
            ({"id": "a", "prompt": "same id"}, 400),
            ({"id": "b", "prompt": "y", "depends_on": ["nope"]}, 400),
            ({"id": "b", "prompt": "y", "depends_on": ["b"]}, 400),
            ({"id": "bad id!", "prompt": "y"}, 400),
            ({"id": "b"}, 400),
            ({"id": "b", "prompt": "y", "review_of": "a"}, 400),
            ({"id": "b", "prompt": "y", "access_mode": "full"}, 400),
            ({"id": "b", "prompt": "y", "provider": "claude"}, 403),
            ({"id": "b", "prompt": "y", "scope": {"path_roots": ["/"]}}, 403),
            (None, 400),
        ]
        for task, code in cases:
            with self.subTest(task=task), self.assertRaises(HTTPException) as ctx:
                self.add(team_id, task)
            self.assertEqual(code, ctx.exception.status_code)
        team = agents._read_team(team_id)
        self.assertEqual((["a"], 0), ([task["id"] for task in team["tasks"]], int(team.get("plan_revision") or 0)))

    def test_a_full_cancelled_or_old_team_is_refused(self) -> None:
        full = self.spawn([{"id": f"t{index}", "prompt": "x"} for index in range(agents.MAX_TEAM_SIZE)], max_parallel=1)
        with self.assertRaises(HTTPException) as too_many:
            self.add(full["team_id"], {"id": "extra", "prompt": "x"})
        self.assertEqual(409, too_many.exception.status_code)
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        agents._update_team(team_id, lambda current: current.update({"cancelled": True}))
        with self.assertRaises(HTTPException) as cancelled:
            self.add(team_id, {"id": "b", "prompt": "y"})
        self.assertEqual(409, cancelled.exception.status_code)
        agents._update_team(team_id, lambda current: current.update({"cancelled": False, "deadline_at": 1.0}))
        with self.assertRaises(HTTPException) as late:
            self.add(team_id, {"id": "b", "prompt": "y"})
        self.assertIn("team_deadline", str(late.exception.detail))
        agents._update_team(team_id, lambda current: current.update({"scheduler_version": 0}))
        with self.assertRaises(HTTPException) as old:
            self.add(team_id, {"id": "b", "prompt": "y"})
        self.assertEqual(400, old.exception.status_code)

    def test_a_sibling_agent_cannot_change_the_plan(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
        context = agents.PolicyContext(agent_id="agt_stranger") if hasattr(agents, "PolicyContext") else None
        if context is None:
            self.skipTest("PolicyContext is not importable from tools_agents")
        with self.assertRaises(HTTPException) as denied:
            agents._authorize_team_control(team_id, "agent_action:add_task", context=context)
        self.assertEqual(403, denied.exception.status_code)

    def test_teammates_can_message_the_new_task(self) -> None:
        store = tm.Store(Path(tempfile.mkdtemp()) / "team_messages.sqlite3")
        with patch.object(agents.agent_message_bridge, "enabled", return_value=True), \
             patch.object(agents.team_messages, "store", return_value=store):
            team_id = self.spawn([{"id": "a", "prompt": "x"}])["team_id"]
            self.add(team_id, {"id": "late", "prompt": "y"})
        room = agents._read_team(team_id)["messaging"]["room"]
        sender = tm.Caller(member_id="agent:agt_1", label="a", agent_id="agt_1", team_id=team_id)
        store.join(room, sender.member_id, label="a", kind="agent", task_id="a")
        sent = store.send(room, sender, "the schema changed", to="late")
        self.assertTrue(sent["message_id"])


if __name__ == "__main__":
    unittest.main()
