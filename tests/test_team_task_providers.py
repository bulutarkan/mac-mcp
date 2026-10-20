"""#52: a team task may run on its own provider, model and reasoning; the rest use the team's.

For example the builder on OpenCode and its reviewer on Codex in one DAG. Each override goes through the
same checks as the team's (enabled, installed, compatible, signed in, model valid); access stays one per team.
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


def selection(provider, model=None, reasoning=None):
    defaults = {"opencode": "opencode/default", "codex": "gpt-6-luna", "claude": "claude-sonnet"}
    return {"provider": provider, "model": model or defaults.get(provider), "reasoning": reasoning,
            "selection_validation": {"validated": True, "provider": provider}}


class TeamTaskProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_dirs = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.root / "agents", self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.addCleanup(lambda: setattr(agents, "AGENTS_DIR", self.old_dirs[0]) or setattr(agents, "TEAMS_DIR", self.old_dirs[1]))
        self.spawned: list[dict] = []
        self.admitted: list[str] = []
        self.enabled = {"opencode", "codex", "claude"}
        env = patch.dict(os.environ, {"MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "4", "MAC_MCP_AGENT_PROVIDER_LIMIT": "4"})
        env.start()
        self.addCleanup(env.stop)
        real_request = admission.request_admission

        def request(*args, **kwargs):
            self.admitted.append(kwargs.get("provider"))
            return real_request(*args, **kwargs)

        for item in (
            patch.object(agents, "provider_enabled", side_effect=lambda provider: provider in self.enabled),
            patch.object(agents, "_find_binary", side_effect=lambda provider: f"/tmp/fake-{provider}"),
            patch.object(agents, "_require_provider_compatible"),
            patch.object(agents, "_validate_provider_access_mode"),
            patch.object(agents, "_require_provider_sign_in"),
            patch.object(agents, "_resolve_agent_selection", side_effect=selection),
            patch.object(agents, "_validate_provider_model_selection", return_value={"validated": True}),
            patch.object(agents, "_spawn_internal", side_effect=self.fake_spawn),
            patch.object(agents, "request_admission", side_effect=request),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_spawn(self, *, provider, model=None, reasoning=None, team_id=None, team_task_id=None,
                   selection_validation=None, **kwargs):
        self.counter = getattr(self, "counter", 0) + 1
        agent_id = f"agt_{self.counter}"
        (agents.AGENTS_DIR / agent_id).mkdir()
        now = time.time()
        agents._write_meta(agent_id, {"agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id,
                                      "provider": provider, "model": model, "status": "running", "phase": "running",
                                      "started_at": now, "updated_at": now,
                                      "admission_lease_id": kwargs.get("admission_lease_id")})
        if kwargs.get("admission_lease_id"):
            admission.bind_agent(agents.AGENTS_DIR, kwargs["admission_lease_id"], agent_id)
        self.spawned.append({"task": team_task_id, "provider": provider, "model": model, "reasoning": reasoning,
                             "validation": selection_validation})
        return {"ok": True, "agent_id": agent_id, "status": "running"}

    def spawn(self, tasks, **kwargs):
        cwd = self.root / "work"
        cwd.mkdir(exist_ok=True)
        return agents.spawn_agents(load_settings(), tasks=tasks, provider="opencode", cwd=str(cwd),
                                   access_mode="read_only", git_isolation="auto", retries=0, **kwargs)

    def test_a_reviewer_on_another_provider_beside_the_team_default(self) -> None:
        team = self.spawn([
            {"id": "build", "prompt": "build it"},
            {"id": "check", "prompt": "check it", "provider": "codex", "reasoning": "high"},
            {"id": "tweak", "prompt": "small change", "model": "opencode/fast"},
        ])
        runs = {row["task"]: row for row in self.spawned}
        self.assertEqual(("opencode", "opencode/default"), (runs["build"]["provider"], runs["build"]["model"]))
        # Another provider gets its own defaults, never the team's model.
        self.assertEqual(("codex", "gpt-6-luna", "high"),
                         (runs["check"]["provider"], runs["check"]["model"], runs["check"]["reasoning"]))
        self.assertEqual({"validated": True, "provider": "codex"}, runs["check"]["validation"])
        # Only a model: the team's provider with that model.
        self.assertEqual(("opencode", "opencode/fast"), (runs["tweak"]["provider"], runs["tweak"]["model"]))
        self.assertEqual(["opencode", "codex", "opencode"], self.admitted)
        rows = {row["id"]: row for row in agents._team_summary(team["team_id"])["tasks"]}
        self.assertEqual("codex", rows["check"]["provider"])
        self.assertEqual("opencode", rows["build"]["provider"])

    def test_a_disabled_provider_or_shared_setting_is_refused_before_anything_starts(self) -> None:
        self.enabled.discard("claude")
        with self.assertRaises(HTTPException) as ctx:
            self.spawn([{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y", "provider": "claude"}])
        self.assertEqual(403, ctx.exception.status_code)
        self.assertIn("provider_disabled", str(ctx.exception.detail))
        with self.assertRaises(HTTPException) as ctx:
            self.spawn([{"id": "a", "prompt": "x", "access_mode": "full"}])
        self.assertEqual(400, ctx.exception.status_code)
        self.assertEqual([], self.spawned)

    def test_retry_keeps_each_task_on_its_own_provider(self) -> None:
        team = self.spawn([{"id": "build", "prompt": "build it"},
                           {"id": "check", "prompt": "check it", "provider": "codex"}])
        stored = {task["id"]: task for task in agents._read_team(team["team_id"])["tasks"]}
        self.assertEqual("codex", stored["check"]["provider"])
        self.assertIsNone(stored["build"]["provider"])
        source = Path(agents.__file__).read_text(encoding="utf-8")
        self.assertIn('**{key: task[key] for key in ("provider", "model", "reasoning") if task.get(key)},', source)

    def test_messaging_hint_follows_the_task_provider(self) -> None:
        team = {"provider": "opencode", "messaging": {"enabled": True}, "tasks": [{"id": "a"}, {"id": "b"}]}
        hook_provider = next(iter(agents.agent_message_bridge.HOOK_PROVIDERS))
        on_hook = agents._team_messaging_instruction(team, {"id": "a", "provider": hook_provider})
        self.assertNotIn("cannot show teammates' messages", on_hook)


if __name__ == "__main__":
    unittest.main()


class TeamResumeTests(TeamTaskProviderTests):
    """#51: a stopped team picks up where it stopped; finished work is not done again."""

    def set_agent(self, agent_id, **fields):
        agents._update_meta(agent_id, lambda meta: meta.update(fields))

    def set_task(self, team_id, task_id, **fields):
        def update(team):
            for task in team["tasks"]:
                if task["id"] == task_id:
                    task.update(fields)
        agents._update_team(team_id, update)

    def test_resume_continues_restarts_waits_and_leaves(self) -> None:
        team = self.spawn([
            {"id": "a", "prompt": "done already"},
            {"id": "b", "prompt": "was interrupted"},
            {"id": "c", "prompt": "needs b", "depends_on": ["b"]},
            {"id": "d", "prompt": "sent an email, then failed"},
            {"id": "e", "prompt": "never started"},
        ], max_parallel=4)
        team_id = team["team_id"]
        agent_of = {row["task"]: f"agt_{index}" for index, row in enumerate(self.spawned, start=1)}
        self.set_agent(agent_of["a"], status="completed")
        self.set_agent(agent_of["b"], status="failed", workflow_input_hash="hash_b", session_id="sess_b")
        self.set_agent(agent_of["d"], status="failed")
        self.set_task(team_id, "a", state="completed")
        self.set_task(team_id, "b", state="failed", failure_reason="agent_failed")
        self.set_task(team_id, "c", state="skipped", failure_reason="dependency_failed:b")
        self.set_task(team_id, "d", state="failed", failure_reason="agent_failed")
        self.set_task(team_id, "e", state="cancelled", failure_reason="team_cancelled", latest_agent_id=None)
        for task_id in ("a", "b", "d"):
            self.set_task(team_id, task_id, active_agent_id=None)
        risky = {"safety": "verified", "receipt_count": 1, "resume_generation": 0}
        checkpoint = {"workflow_id": "wf_b", "input_hash": "hash_b", "resume_generation": 1, "resume_token": "tok"}
        self.spawned.clear()
        with patch.object(agents, "workflow_for_agent",
                          side_effect=lambda agent_id: risky if agent_id == agent_of["d"] else None), \
             patch.object(agents, "prepare_resume", return_value=checkpoint) as prepare, \
             patch.object(agents, "durable_resume_prompt", return_value="RESUME b"), \
             patch.object(agents, "update_provider_state"), \
             patch.object(agents, "_spawn_internal", side_effect=self.resume_spawn):
            result = agents.agent_action(load_settings(), "resume", team_id=team_id)
        self.assertEqual((["b"], ["e"], ["c"], ["d"]),
                         (result["continued"], result["restarted"], result["waiting"], result["left"]))
        started = {row["task"]: row for row in self.spawned}
        self.assertNotIn("a", started)  # finished work is not done again
        self.assertEqual("sess_b", started["b"]["resume_session_id"])
        self.assertEqual("RESUME b", started["b"]["prompt"])
        self.assertEqual(agent_of["b"], started["b"]["resume_parent_agent_id"])
        prepare.assert_called_once()
        self.assertIsNone(started["e"]["resume_session_id"])
        states = {task["id"]: task["state"] for task in agents._read_team(team_id)["tasks"]}
        self.assertEqual({"a": "completed", "b": "running", "c": "blocked", "d": "failed", "e": "running"}, states)

    def resume_spawn(self, *, prompt=None, resume_session_id=None, resume_parent_agent_id=None, **kwargs):
        out = self.fake_spawn(**kwargs)
        self.spawned[-1].update({"prompt": prompt, "resume_session_id": resume_session_id,
                                 "resume_parent_agent_id": resume_parent_agent_id})
        return out

    def test_a_running_team_or_an_old_team_is_refused(self) -> None:
        team = self.spawn([{"id": "a", "prompt": "x"}])
        with self.assertRaises(HTTPException) as running:
            agents.agent_action(load_settings(), "resume", team_id=team["team_id"])
        self.assertEqual(409, running.exception.status_code)
        agents._update_team(team["team_id"], lambda current: current.update({"scheduler_version": 0}))
        with self.assertRaises(HTTPException) as old:
            agents.agent_action(load_settings(), "resume", team_id=team["team_id"])
        self.assertEqual(400, old.exception.status_code)

    def test_a_checkpoint_that_cannot_be_prepared_falls_back_only_when_safe(self) -> None:
        team = self.spawn([{"id": "a", "prompt": "x"}])
        team_id, agent_id = team["team_id"], "agt_1"
        self.set_agent(agent_id, status="failed", workflow_input_hash="h", session_id="s")
        self.set_task(team_id, "a", state="failed", active_agent_id=None)
        risky = {"safety": "verified", "receipt_count": 2}
        with patch.object(agents, "workflow_for_agent", return_value=risky), \
             patch.object(agents, "prepare_resume", side_effect=agents.CheckpointConflictError("resume_conflict", "taken")), \
             patch.object(agents, "update_provider_state"):
            agents.agent_action(load_settings(), "resume", team_id=team_id)
        task = agents._read_team(team_id)["tasks"][0]
        self.assertEqual(("failed", "resume_unsafe"), (task["state"], task["failure_reason"]))


class MixedTeamDataNoticeTests(unittest.TestCase):
    def test_every_provider_of_a_mixed_team_is_named(self) -> None:
        from mcp_server import privacy_inventory
        result = privacy_inventory.with_provider_notice({
            "provider": "codex", "tasks": [{"id": "a", "provider": "codex"}, {"id": "b", "provider": "claude"}]})
        self.assertIn("Anthropic (Claude Code) and OpenAI (Codex)", result["data_notice"])
