"""Team flow for the lead (#129): retry one task in place, follow a long team by cursor, apply a team at once.

Retrying used to rebuild the whole team; now chosen failed tasks run again in the same team, optionally on
another provider, while finished tasks keep their results. wait_agents(since=cursor) returns as soon as a
task changes, with only the changes. Team apply copies the finished results into the source checkout in
order, only the last of each dependent chain, and leaves results that touch the same files listed.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import agent_admission as admission
from mcp_server import tools_agents as agents
from mcp_server.security import load_settings
from tests.test_team_presets import write_settings


class TeamFlowCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        old_dirs = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.root / "agents", self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.addCleanup(lambda: setattr(agents, "AGENTS_DIR", old_dirs[0]) or setattr(agents, "TEAMS_DIR", old_dirs[1]))
        settings_path = self.root / "settings.json"
        write_settings(settings_path, None)
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings_path),
                                      "MAC_MCP_STATE_DIR": str(self.root / "state"),
                                      "MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "8", "MAC_MCP_AGENT_PROVIDER_LIMIT": "8"})
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[dict] = []
        for item in (
            patch.object(agents, "_find_binary", side_effect=lambda provider: f"/tmp/fake-{provider}"),
            patch.object(agents, "_require_provider_compatible"),
            patch.object(agents, "_validate_provider_access_mode"),
            patch.object(agents, "_require_provider_sign_in"),
            patch.object(agents, "_validate_provider_model_selection", return_value={"validated": True}),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
            patch.object(agents, "_open_team_room", return_value={"enabled": False}),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_spawn(self, *, provider, model=None, reasoning=None, team_id=None, team_task_id=None, title=None,
                   prompt=None, **kwargs):
        self.counter = getattr(self, "counter", 0) + 1
        agent_id = f"agt_{self.counter}"
        (agents.AGENTS_DIR / agent_id).mkdir()
        now = time.time()
        agents._write_meta(agent_id, {"agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id,
                                      "provider": provider, "model": model, "status": "running", "phase": "running",
                                      "title": title, "started_at": now, "updated_at": now,
                                      "admission_lease_id": kwargs.get("admission_lease_id")})
        if kwargs.get("admission_lease_id"):
            admission.bind_agent(agents.AGENTS_DIR, kwargs["admission_lease_id"], agent_id)
        self.spawned.append({"task": team_task_id, "agent": agent_id, "provider": provider, "model": model,
                             "reuse": kwargs.get("reuse_worktree_agent_id")})
        return {"ok": True, "agent_id": agent_id, "status": "running"}

    def spawn(self, tasks, **kwargs):
        cwd = self.root / "work"
        cwd.mkdir(exist_ok=True)
        return agents.spawn_agents(load_settings(), tasks=tasks, provider="codex", cwd=str(cwd),
                                   access_mode="read_only", git_isolation="auto", retries=0, **kwargs)

    def latest(self, task_id: str) -> str:
        return [row["agent"] for row in self.spawned if row["task"] == task_id][-1]

    def finish(self, team_id: str, task_id: str, status: str) -> None:
        agent_id = self.latest(task_id)
        agents._update_meta(agent_id, lambda meta: meta.update(status=status, phase=status, ended_at=time.time()))
        agents._team_tick(team_id)

    def states(self, team_id: str) -> dict:
        return {task["id"]: task["state"] for task in agents._read_team(team_id)["tasks"]}


class RetryInPlaceTests(TeamFlowCase):
    def team_with_a_failed_build(self) -> str:
        team = self.spawn([{"id": "build", "prompt": "build"}, {"id": "docs", "prompt": "docs"},
                           {"id": "test", "prompt": "test", "depends_on": ["build"]}])
        team_id = team["team_id"]
        self.finish(team_id, "docs", "completed")
        self.finish(team_id, "build", "failed")
        self.assertEqual({"build": "failed", "docs": "completed", "test": "skipped"}, self.states(team_id))
        return team_id

    def test_a_failed_task_runs_again_on_another_provider_and_its_dependent_waits_for_it(self) -> None:
        team_id = self.team_with_a_failed_build()
        # The failed attempt left a worktree; the retry starts clean instead of from its partial edits.
        agents._update_meta(self.latest("build"), lambda meta: meta.update(worktree={"enabled": True}))
        before = len(self.spawned)
        result = agents.agent_action(load_settings(), "retry", team_id=team_id, task_ids=["build"],
                                     provider="claude", model="opus")
        self.assertEqual((["build"], ["test"]), (result["retried"], result["waiting"]))
        self.assertEqual({"provider": "claude", "model": "opus", "reasoning": None}, result["retried_with"])
        again = self.spawned[before:]
        # Only the retried task started; docs kept its result and the team is the same team.
        self.assertEqual([("build", "claude", "opus", None)],
                         [(row["task"], row["provider"], row["model"], row["reuse"]) for row in again])
        self.assertEqual("blocked", self.states(team_id)["test"])
        self.finish(team_id, "build", "completed")
        self.assertEqual("test", self.spawned[-1]["task"])
        self.assertEqual({"build": "completed", "docs": "completed", "test": "running"}, self.states(team_id))
        build = next(task for task in agents._read_team(team_id)["tasks"] if task["id"] == "build")
        self.assertEqual(1, build["manual_retry_count"])

    def test_refusals(self) -> None:
        team_id = self.team_with_a_failed_build()
        cases = [
            ("finished task", ["docs"], {}, 409),
            ("unknown task", ["deploy"], {}, 404),
            ("disabled provider", ["build"], {"provider": "kimi"}, 403),
        ]
        for name, task_ids, override, code in cases:
            with self.subTest(name), self.assertRaises(HTTPException) as ctx:
                agents.agent_action(load_settings(), "retry", team_id=team_id, task_ids=task_ids, **override)
            self.assertEqual(code, ctx.exception.status_code)
        with patch.object(agents, "_team_task_rerun_safe", return_value=False), \
             self.assertRaises(HTTPException) as unsafe:
            agents.agent_action(load_settings(), "retry", team_id=team_id, task_ids=["build"])
        self.assertIn("retry_replay_unsafe", str(unsafe.exception.detail))
        self.assertEqual("failed", self.states(team_id)["build"])


    def test_a_retried_read_only_task_does_not_look_for_a_worktree_its_last_agent_never_had(self) -> None:
        # Live: the scheduler asked to reuse the last agent's worktree and failed with
        # git_worktree_reuse_unavailable when that agent (read-only) had none.
        team_id = self.team_with_a_failed_build()
        agents.agent_action(load_settings(), "retry", team_id=team_id, task_ids=["build"])
        self.assertEqual(("build", None), (self.spawned[-1]["task"], self.spawned[-1]["reuse"]))
        self.assertEqual("running", self.states(team_id)["build"])


class FollowByCursorTests(TeamFlowCase):
    def test_wait_returns_when_a_task_changes_with_only_the_changes(self) -> None:
        team_id = self.spawn([{"id": "a", "prompt": "a"}, {"id": "b", "prompt": "b"}])["team_id"]
        first = agents.wait_agents(load_settings(), team_id=team_id, timeout_s=0, include_results=False)
        cursor = first["cursor"]
        self.assertGreater(cursor, 0)
        self.assertIn(f"since={cursor}", first["next"])
        quiet = agents.wait_agents(load_settings(), team_id=team_id, timeout_s=0, since=cursor, include_results=False)
        self.assertEqual(([], cursor), (quiet["changes"], quiet["cursor"]))
        agents._update_meta(self.latest("a"), lambda meta: meta.update(status="completed", phase="completed",
                                                                       ended_at=time.time()))
        started = time.monotonic()
        moved = agents.wait_agents(load_settings(), team_id=team_id, timeout_s=20, since=cursor, include_results=False)
        self.assertLess(time.monotonic() - started, 10)  # back on the change, not at the deadline
        self.assertFalse(moved["timed_out"])
        self.assertEqual([("a", "running", "completed")],
                         [(change["task_id"], change["from"], change["to"]) for change in moved["changes"]])
        self.assertGreater(moved["cursor"], cursor)
        with self.assertRaises(HTTPException):
            agents.wait_agents(load_settings(), agent_ids=[self.latest("a")], since=0)

    def test_the_event_log_stays_bounded(self) -> None:
        meta = {"tasks": [{"id": "a", "state": "ready"}]}
        path = self.root / "team.json"
        for index in range(agents._TEAM_EVENT_KEEP + 30):
            meta["tasks"][0]["state"] = "running" if index % 2 else "ready"
            agents._record_team_events(path, meta)
        self.assertEqual(agents._TEAM_EVENT_KEEP, len(meta["events"]))
        self.assertEqual(agents._TEAM_EVENT_KEEP + 30, meta["event_seq"])


class TeamApplyTests(TeamFlowCase):
    def test_the_last_of_each_chain_is_applied_and_same_file_results_are_left_listed(self) -> None:
        tasks = [
            {"id": "model", "state": "completed", "latest_agent_id": "agt_model", "title": "Atlas"},
            {"id": "api", "state": "completed", "latest_agent_id": "agt_api", "depends_on": ["model"]},
            {"id": "styles", "state": "completed", "latest_agent_id": "agt_styles"},
            {"id": "readme", "state": "completed", "latest_agent_id": "agt_readme"},
            {"id": "broken", "state": "failed", "latest_agent_id": "agt_broken"},
        ]
        worktrees = {
            "agt_model": {"enabled": True, "pending_changes": True, "changed_files": ["model.py"]},
            # api started from model's worktree: it carries model.py and adds its own files.
            "agt_api": {"enabled": True, "pending_changes": True, "changed_files": ["model.py", "api.py", "app.css"],
                        "seeded_from": [{"source_agent_id": "agt_model", "source_task_id": "model"}]},
            "agt_styles": {"enabled": True, "pending_changes": True, "changed_files": ["app.css"]},
            "agt_readme": {"enabled": True, "pending_changes": True, "changed_files": ["README.md"]},
            "agt_broken": {"enabled": True, "pending_changes": True, "changed_files": ["x.py"]},
        }
        agents._write_team("team_apply", {"team_id": "team_apply", "scheduler_version": 1, "tasks": tasks,
                                          "agent_ids": [task["latest_agent_id"] for task in tasks]})
        team = agents._read_team("team_apply")
        applied: list[str] = []

        def single(_settings, agent_id, action, **_kwargs):
            applied.append(agent_id)
            return {"ok": True, "apply": {"status": "applied"}}

        with patch.object(agents, "_refresh_agent_worktree", side_effect=lambda agent_id: worktrees[agent_id]), \
             patch.object(agents, "_agent_action_single", side_effect=single):
            plan = agents._apply_team(load_settings(), "team_apply", team, dry_run=True)
            self.assertEqual([], applied)
            self.assertEqual(["readme"], [row["task_id"] for row in plan["will_apply"]])
            self.assertEqual([{"task_id": "model", "agent_id": "agt_model", "carried_by": ["api"]}], plan["carried"])
            self.assertEqual({"api": {"app.css": ["styles"]}, "styles": {"app.css": ["api"]}},
                             {row["task_id"]: row["shared_files"] for row in plan["overlapping"]})
            done = agents._apply_team(load_settings(), "team_apply", team, dry_run=False)
        self.assertEqual(["agt_readme"], applied)
        self.assertEqual((True, ["readme"]), (done["ok"], [row["task_id"] for row in done["applied"]]))


if __name__ == "__main__":
    unittest.main()
