from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import agent_admission as admission
from mcp_server import tools_agents as agents
from mcp_server import tools_files


class CrossTeamGlobalSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.spawned: list[dict] = []
        self.counter = 0
        self.env = patch.dict(os.environ, {
            "MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT": "4",
            "MAC_MCP_AGENT_PROVIDER_LIMIT": "4",
            "MAC_MCP_AGENT_PROVIDER_LIMIT_OPENCODE": "2",
            "MAC_MCP_AGENT_ADMISSION_TTL_S": "60",
        }, clear=False)
        self.env.start()
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
        self.env.stop()
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    def fake_spawn(self, settings=None, provider="opencode", prompt="", model=None, reasoning=None,
                   cwd=None, timeout_s=None, title=None, result_style="concise", access_mode="workspace_write",
                   scope=None, permission_profile="developer", capability_profile="legacy",
                   parent_agent_id=None, resume_session_id=None, attempt=1, team_id=None,
                   team_task_id=None, idle_timeout_s=None, retries=0, project=None, role=None,
                   provenance_class="local", **kwargs):
        self.counter += 1
        agent_id = f"agt_global_{self.counter}"
        path = agents.AGENTS_DIR / agent_id
        path.mkdir(parents=True)
        for name in ("result.txt", "prompt.txt", "stdout.log", "stderr.log", "worker.log"):
            (path / name).write_text("", encoding="utf-8")
        now = time.time()
        meta = {
            "agent_id": agent_id, "team_id": team_id, "team_task_id": team_task_id,
            "title": title, "role": role, "provider": provider, "model": model,
            "cwd": cwd, "source_cwd": cwd, "access_mode": access_mode,
            "permission_profile": permission_profile, "capability_profile": capability_profile,
            "scope": scope.to_dict() if hasattr(scope, "to_dict") else scope,
            "status": "running", "phase": "running", "started_at": now,
            "spawn_requested_at": now, "last_activity_at": now, "updated_at": now,
            "retries": retries, "retry_count": 0, "attempt": attempt,
            "parent_agent_id": parent_agent_id,
            "admission_lease_id": kwargs.get("admission_lease_id"),
            "admission_resources": list(kwargs.get("admission_resources") or []),
        }
        agents._write_meta(agent_id, meta)
        lease_id = kwargs.get("admission_lease_id")
        if lease_id:
            admission.bind_agent(agents.AGENTS_DIR, lease_id, agent_id)
        self.spawned.append({
            "agent_id": agent_id, "team_id": team_id, "task_id": team_task_id,
            "lease_id": lease_id, "resources": list(kwargs.get("admission_resources") or []),
        })
        return {"ok": True, "agent_id": agent_id, "team_task_id": team_task_id, "status": "running", "title": title}

    def workspace(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def spawn_team(self, name: str, *, cwd: Path, resources=None, scope=None, provider="opencode"):
        return agents.spawn_agents(
            settings=None,
            tasks=[{"id":"task", "prompt":name, "resources":resources or []}],
            provider=provider,
            cwd=str(cwd),
            access_mode="workspace_write",
            scope=scope,
            retries=0,
            max_parallel=1,
            git_isolation="auto",
            title=name,
        )

    def task(self, team_id: str) -> dict:
        return agents._read_team(team_id)["tasks"][0]

    def complete(self, agent_id: str) -> None:
        def update(meta):
            meta.update({"status":"completed", "phase":"completed", "ended_at":time.time(), "updated_at":time.time()})
        agents._update_meta(agent_id, update)

    def test_three_independent_teams_obey_shared_provider_capacity_and_wakeup(self) -> None:
        one = self.spawn_team("one", cwd=self.workspace("one"))
        two = self.spawn_team("two", cwd=self.workspace("two"))
        three = self.spawn_team("three", cwd=self.workspace("three"))
        self.assertEqual(2, len(self.spawned))
        self.assertEqual("queued", self.task(three["team_id"])["state"])
        self.assertEqual("provider_capacity", self.task(three["team_id"])["queued_reason"])
        self.assertEqual(1, three["global_admission"]["queued_count"])

        first_agent = self.spawned[0]["agent_id"]
        self.complete(first_agent)
        agents._team_tick(one["team_id"])
        agents._wake_global_admission_queue(exclude_team_id=one["team_id"])
        self.assertEqual(3, len(self.spawned))
        self.assertEqual(three["team_id"], self.spawned[-1]["team_id"])
        self.assertEqual("running", self.task(three["team_id"])["state"])

    # ASSURANCE: SEC-SCHED-001
    def test_overlapping_cross_team_workspace_serializes_but_distinct_workspace_runs(self) -> None:
        shared = self.workspace("shared")
        other = self.workspace("other")
        one = self.spawn_team("one", cwd=shared)
        two = self.spawn_team("two", cwd=shared)
        three = self.spawn_team("three", cwd=other, provider="codex")
        self.assertEqual("running", self.task(one["team_id"])["state"])
        self.assertEqual("queued", self.task(two["team_id"])["state"])
        self.assertEqual("resource_busy", self.task(two["team_id"])["queued_reason"])
        self.assertEqual("running", self.task(three["team_id"])["state"])

    def test_chatgpt_reduced_window_admits_only_one_cross_team(self) -> None:
        now = time.time()
        state_path = agents.AGENTS_DIR / ".chatgpt-provider-state.json"
        state_path.write_text(
            '{"cooldown_until":0,"reduced_until":' + str(now + 120) + ',"next_allowed_at":0}',
            encoding="utf-8",
        )
        one = self.spawn_team("chat-one", cwd=self.workspace("chat-one"), provider="chatgpt")
        two = self.spawn_team("chat-two", cwd=self.workspace("chat-two"), provider="chatgpt")
        self.assertEqual("running", self.task(one["team_id"])["state"])
        self.assertEqual("queued", self.task(two["team_id"])["state"])
        self.assertEqual("provider_capacity", self.task(two["team_id"])["queued_reason"])
        self.assertEqual(1, self.task(two["team_id"])["queued_details"]["provider_limit"])

    def test_native_window_and_browser_tab_claims_match_global_ownership(self) -> None:
        one = self.spawn_team(
            "native-one", cwd=self.workspace("n1"), provider="opencode",
            resources=[{"kind":"native_window","id":"TextEdit:win_1","mode":"write"}],
        )
        two = self.spawn_team(
            "native-two", cwd=self.workspace("n2"), provider="codex",
            resources=[{"kind":"native_window","id":"TextEdit:win_1","mode":"write"}],
        )
        self.assertEqual("resource_busy", self.task(two["team_id"])["queued_reason"])

        tab_scope = {"browser_tabs":["tab_51"], "path_roots":[str(self.workspace("browser"))]}
        three = self.spawn_team(
            "browser-one", cwd=self.workspace("browser"), provider="codex", scope=tab_scope,
            resources=[{"kind":"browser_tab","id":"tab_51","mode":"write"}],
        )
        four = self.spawn_team(
            "browser-two", cwd=self.workspace("browser2"), provider="chatgpt",
            scope={"browser_tabs":["tab_51"], "path_roots":[str(self.workspace("browser2"))]},
            resources=[{"kind":"browser_tab","id":"tab_51","mode":"write"}],
        )
        self.assertEqual("running", self.task(three["team_id"])["state"])
        self.assertEqual("resource_busy", self.task(four["team_id"])["queued_reason"])

    # ASSURANCE: SEC-SCHED-001
    def test_team_cancel_removes_queued_request_without_releasing_other_active_lease(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_AGENT_PROVIDER_LIMIT_OPENCODE":"1"}, clear=False):
            one = self.spawn_team("one", cwd=self.workspace("one"))
            two = self.spawn_team("two", cwd=self.workspace("two"))
            self.assertEqual("queued", self.task(two["team_id"])["state"])
            before = admission.snapshot(agents.AGENTS_DIR)
            self.assertEqual(1, before["global_active"])
            self.assertEqual(1, before["queued_count"])
            result = agents.agent_action(None, action="cancel", team_id=two["team_id"])
            self.assertTrue(result["ok"])
            after = admission.snapshot(agents.AGENTS_DIR)
            self.assertEqual(1, after["global_active"])
            self.assertEqual(0, after["queued_count"])
            self.assertEqual("cancelled", self.task(two["team_id"])["state"])
            self.assertEqual("running", self.task(one["team_id"])["state"])

    # ASSURANCE: SEC-SCHED-001
    def test_file_expected_revision_conflict_fails_before_spawn(self) -> None:
        workspace = self.workspace("cas")
        target = workspace / "value.txt"
        target.write_text("v1\n", encoding="utf-8")
        read = tools_files.read_file(None, str(target))
        self.assertEqual(64, len(read["revision"]))
        target.write_text("v2\n", encoding="utf-8")
        team = self.spawn_team(
            "cas-conflict", cwd=workspace,
            resources=[{
                "kind":"file", "id":str(target), "mode":"write",
                "expected_revision":read["revision"],
            }],
        )
        task = self.task(team["team_id"])
        self.assertEqual("failed", task["state"])
        self.assertEqual("admission_failed:file_revision_conflict", task["failure_reason"])
        self.assertEqual(0, len(self.spawned))
        self.assertEqual("v2\n", target.read_text())

    def test_file_expected_revision_match_admits(self) -> None:
        workspace = self.workspace("cas-ok")
        target = workspace / "value.txt"
        target.write_text("v1\n", encoding="utf-8")
        revision = tools_files.read_file(None, str(target))["revision"]
        team = self.spawn_team(
            "cas-ok", cwd=workspace,
            resources=[{
                "kind":"file", "id":str(target), "mode":"write",
                "expected_revision":revision,
            }],
        )
        self.assertEqual("running", self.task(team["team_id"])["state"])
        self.assertEqual(1, len(self.spawned))


if __name__ == "__main__":
    unittest.main()
