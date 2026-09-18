from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import tools_agents as agents
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context


class AgentLineageIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_agents = agents.AGENTS_DIR
        self.old_teams = agents.TEAMS_DIR
        agents.AGENTS_DIR = self.root / "agents"
        agents.TEAMS_DIR = self.root / "teams"
        agents.AGENTS_DIR.mkdir(parents=True)
        agents.TEAMS_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        agents.AGENTS_DIR = self.old_agents
        agents.TEAMS_DIR = self.old_teams
        self.temp.cleanup()

    def context(self, agent_id: str | None):
        token = set_policy_context(PolicyContext(
            profile="trusted",
            actor=f"agent:{agent_id}" if agent_id else "authenticated",
            agent_id=agent_id,
        ))
        self.addCleanup(reset_policy_context, token)
        return token

    def agent(self, agent_id: str, *, parent: str | None = None, root: str | None = None,
              ancestors: list[str] | None = None, status: str = "completed", lineage: bool = True) -> None:
        adir = agents.AGENTS_DIR / agent_id
        adir.mkdir(parents=True, exist_ok=True)
        for name in ("result.txt", "stdout.log", "stderr.log", "worker.log", "prompt.txt"):
            (adir / name).write_text(f"{agent_id}:{name}\n", encoding="utf-8")
        meta = {
            "agent_id": agent_id,
            "team_id": None,
            "title": agent_id,
            "provider": "opencode",
            "access_mode": "read_only",
            "permission_profile": "trusted",
            "capability_profile": "legacy",
            "scope": {"access_mode": "read_only", "path_roots": [str(self.root)]},
            "status": status,
            "phase": status,
            "started_at": time.time() - 1,
            "ended_at": time.time() if status in agents.TERMINAL_STATUSES else None,
            "last_activity_at": time.time(),
            "spawn_requested_at": time.time() - 1,
            "parent_agent_id": parent,
            "retries": 0,
            "retry_count": 0,
        }
        if lineage:
            meta.update({
                "lineage_version": agents._LINEAGE_VERSION,
                "lineage_root_agent_id": root or agent_id,
                "lineage_parent_agent_id": parent,
                "lineage_ancestors": list(ancestors or []),
            })
        agents._write_meta(agent_id, meta)

    def team(self, team_id: str, *, owner: str | None, root: str | None,
             ancestors: list[str] | None = None, agent_ids: list[str] | None = None) -> None:
        agents._write_team(team_id, {
            "team_id": team_id,
            "title": team_id,
            "provider": "opencode",
            "access_mode": "read_only",
            "created_at": time.time(),
            "updated_at": time.time(),
            "agent_ids": list(agent_ids or []),
            "tasks": [],
            "scheduler_version": 0,
            "owner_agent_id": owner,
            "lineage_version": agents._LINEAGE_VERSION,
            "lineage_root_agent_id": root,
            "lineage_ancestors": list(ancestors or []),
        })

    def build_tree(self) -> None:
        # root -> child -> grandchild; sibling is another child of root; unrelated is separate root.
        self.agent("agt_root", root="agt_root", ancestors=[])
        self.agent("agt_child", parent="agt_root", root="agt_root", ancestors=["agt_root"])
        self.agent(
            "agt_grandchild", parent="agt_child", root="agt_root",
            ancestors=["agt_root", "agt_child"],
        )
        self.agent("agt_sibling", parent="agt_root", root="agt_root", ancestors=["agt_root"])
        self.agent("agt_other", root="agt_other", ancestors=[])

    # ASSURANCE: SEC-AGENT-001
    def test_sibling_and_unrelated_get_or_logs_are_denied(self) -> None:
        self.build_tree()
        self.context("agt_child")
        for target in ("agt_sibling", "agt_other", "agt_root"):
            with self.subTest(target=target), self.assertRaises(HTTPException) as ctx:
                agents.get_agent(None, target, include_logs=True)
            self.assertEqual(403, ctx.exception.status_code)
            self.assertEqual("agent_control_denied", ctx.exception.detail["error"])
        # Self and descendants remain visible.
        self.assertEqual("agt_child", agents.get_agent(None, "agt_child")["agent_id"])
        self.assertEqual("agt_grandchild", agents.get_agent(None, "agt_grandchild")["agent_id"])

    # ASSURANCE: SEC-AGENT-001
    def test_denied_legacy_target_does_not_trigger_lineage_backfill_write(self) -> None:
        self.agent("agt_actor", root="agt_actor", ancestors=[])
        self.agent("agt_legacy_other", lineage=False)
        before = (agents.AGENTS_DIR / "agt_legacy_other" / "meta.json").read_bytes()
        token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_actor", agent_id="agt_actor"))
        try:
            with self.assertRaises(HTTPException) as ctx:
                agents.get_agent(None, "agt_legacy_other")
            self.assertEqual(403, ctx.exception.status_code)
        finally:
            reset_policy_context(token)
        after = (agents.AGENTS_DIR / "agt_legacy_other" / "meta.json").read_bytes()
        self.assertEqual(before, after)

    # ASSURANCE: SEC-AGENT-001
    def test_sibling_control_actions_are_denied_before_mutation(self) -> None:
        self.build_tree()
        self.context("agt_child")
        before = agents._read_meta("agt_sibling")
        for action in ("cancel", "resume", "despawn"):
            with self.subTest(action=action), self.assertRaises(HTTPException) as ctx:
                agents.agent_action(None, action=action, agent_id="agt_sibling")
            self.assertEqual(403, ctx.exception.status_code)
        after = agents._read_meta("agt_sibling")
        self.assertEqual(before["status"], after["status"])
        self.assertTrue((agents.AGENTS_DIR / "agt_sibling").exists())

    def test_ancestor_can_control_descendant_but_child_cannot_widen_upward(self) -> None:
        self.build_tree()
        root_token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_root", agent_id="agt_root"))
        try:
            result = agents.agent_action(None, action="cancel", agent_id="agt_grandchild")
            self.assertTrue(result["ok"])
            self.assertEqual("completed", result["status"])
        finally:
            reset_policy_context(root_token)

        child_token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_grandchild", agent_id="agt_grandchild"))
        try:
            for target in ("agt_root", "agt_child", "agt_sibling"):
                with self.subTest(target=target), self.assertRaises(HTTPException) as ctx:
                    agents.get_agent(None, target)
                self.assertEqual(403, ctx.exception.status_code)
        finally:
            reset_policy_context(child_token)

    def test_list_agents_returns_only_self_and_descendants(self) -> None:
        self.build_tree()
        self.context("agt_child")
        payload = agents.list_agents(None, limit=20)
        ids = {row["agent_id"] for row in payload["agents"]}
        self.assertEqual({"agt_child", "agt_grandchild"}, ids)

    def test_wait_agents_rejects_mixed_authorized_and_sibling_targets(self) -> None:
        self.build_tree()
        self.context("agt_child")
        with self.assertRaises(HTTPException) as ctx:
            agents.wait_agents(None, agent_ids=["agt_grandchild", "agt_sibling"], timeout_s=0)
        self.assertEqual(403, ctx.exception.status_code)

    # ASSURANCE: SEC-AGENT-001
    def test_team_owner_and_ancestor_can_access_but_sibling_cannot(self) -> None:
        self.build_tree()
        self.team(
            "team_child", owner="agt_child", root="agt_root",
            ancestors=["agt_root"], agent_ids=["agt_grandchild"],
        )
        for actor in ("agt_child", "agt_root"):
            token = set_policy_context(PolicyContext(profile="trusted", actor=f"agent:{actor}", agent_id=actor))
            try:
                payload = agents.list_agents(None, team_id="team_child", limit=20)
                self.assertEqual("team_child", payload["team"]["team_id"])
            finally:
                reset_policy_context(token)

        sibling = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_sibling", agent_id="agt_sibling"))
        try:
            with self.assertRaises(HTTPException) as ctx:
                agents.list_agents(None, team_id="team_child", limit=20)
            self.assertEqual(403, ctx.exception.status_code)
        finally:
            reset_policy_context(sibling)

    # ASSURANCE: SEC-AGENT-001
    def test_legacy_parent_metadata_backfills_and_survives_restart_like_reload(self) -> None:
        self.agent("agt_legacy_root", lineage=False)
        self.agent("agt_legacy_child", parent="agt_legacy_root", lineage=False)
        token = set_policy_context(PolicyContext(
            profile="trusted", actor="agent:agt_legacy_root", agent_id="agt_legacy_root"
        ))
        try:
            agents.get_agent(None, "agt_legacy_child")
        finally:
            reset_policy_context(token)
        persisted = agents._read_meta("agt_legacy_child")
        self.assertEqual(agents._LINEAGE_VERSION, persisted["lineage_version"])
        self.assertEqual("agt_legacy_root", persisted["lineage_root_agent_id"])
        self.assertEqual(["agt_legacy_root"], persisted["lineage_ancestors"])

        # Simulate process restart: in-memory lock registries disappear, disk metadata remains authoritative.
        agents._META_LOCKS.clear()
        token = set_policy_context(PolicyContext(
            profile="trusted", actor="agent:agt_legacy_root", agent_id="agt_legacy_root"
        ))
        try:
            again = agents.get_agent(None, "agt_legacy_child")
            self.assertEqual("agt_legacy_root", again["lineage_root_agent_id"])
        finally:
            reset_policy_context(token)

    def test_new_agent_lineage_inherits_parent_root_and_ancestors(self) -> None:
        self.build_tree()
        lineage = agents._new_agent_lineage("agt_new", "agt_child")
        self.assertEqual("agt_root", lineage["lineage_root_agent_id"])
        self.assertEqual("agt_child", lineage["lineage_parent_agent_id"])
        self.assertEqual(["agt_root", "agt_child"], lineage["lineage_ancestors"])

    def test_delegated_spawn_agent_binds_caller_as_parent(self) -> None:
        self.build_tree()
        token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_child", agent_id="agt_child"))
        try:
            with patch.object(agents, "_spawn_internal", return_value={"ok": True, "agent_id": "agt_new"}) as spawn:
                agents.spawn_agent(
                    None, provider="opencode", prompt="child task", cwd=str(self.root),
                    access_mode="read_only",
                )
            self.assertEqual("agt_child", spawn.call_args.kwargs["parent_agent_id"])
        finally:
            reset_policy_context(token)

    def test_delegated_team_persists_owner_root_and_scheduler_parent(self) -> None:
        self.build_tree()
        token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_child", agent_id="agt_child"))
        try:
            with patch.object(agents, "provider_enabled", return_value=True), \
                 patch.object(agents, "_find_binary", return_value="/tmp/fake-opencode"), \
                 patch.object(agents, "_validate_provider_access_mode", return_value=None), \
                 patch.object(agents, "_team_tick", side_effect=lambda tid: agents._read_team(tid)):
                created = agents.spawn_agents(
                    None,
                    tasks=[{"id": "work", "prompt": "do work"}],
                    provider="opencode", cwd=str(self.root), access_mode="read_only",
                    max_parallel=1,
                )
        finally:
            reset_policy_context(token)
        team_id = created["team_id"]
        persisted = agents._read_team(team_id)
        self.assertEqual("agt_child", persisted["owner_agent_id"])
        self.assertEqual("agt_root", persisted["lineage_root_agent_id"])
        self.assertEqual(["agt_root"], persisted["lineage_ancestors"])

        captured: list[str | None] = []
        def fake_spawn_internal(*args, **kwargs):
            captured.append(kwargs.get("parent_agent_id"))
            return {"ok": True, "agent_id": "agt_team_child", "status": "running"}
        with patch.object(agents, "_spawn_internal", side_effect=fake_spawn_internal):
            agents._team_tick(team_id)
        self.assertEqual(["agt_child"], captured)

    def test_retry_team_inherits_original_owner_even_when_ancestor_triggers(self) -> None:
        self.build_tree()
        self.team("team_owned", owner="agt_child", root="agt_root", ancestors=["agt_root"])
        token = set_policy_context(PolicyContext(profile="trusted", actor="agent:agt_root", agent_id="agt_root"))
        try:
            lineage = agents._team_lineage_for_spawn("team_owned")
        finally:
            reset_policy_context(token)
        self.assertEqual("agt_child", lineage["owner_agent_id"])
        self.assertEqual("agt_root", lineage["lineage_root_agent_id"])
        self.assertEqual(["agt_root"], lineage["lineage_ancestors"])

    def test_root_admin_context_preserves_existing_local_management(self) -> None:
        self.build_tree()
        self.context(None)
        ids = {row["agent_id"] for row in agents.list_agents(None, limit=20)["agents"]}
        self.assertTrue({"agt_root", "agt_child", "agt_sibling", "agt_other"}.issubset(ids))
        self.assertEqual("agt_other", agents.get_agent(None, "agt_other", include_logs=True)["agent_id"])


class AgentLineageObservedMCPTests(unittest.TestCase):
    # ASSURANCE: SEC-AGENT-001
    def test_direct_and_tool_invoke_denials_match_and_emit_security_audit(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                agents_root = root / "agents"
                teams_root = root / "teams"
                agents_root.mkdir(); teams_root.mkdir()
                def write_agent(agent_id: str, parent: str | None, lineage_root: str, ancestors: list[str]) -> None:
                    adir = agents_root / agent_id; adir.mkdir()
                    (adir / "meta.json").write_text(__import__("json").dumps({
                        "agent_id": agent_id, "parent_agent_id": parent,
                        "lineage_version": agents._LINEAGE_VERSION,
                        "lineage_root_agent_id": lineage_root,
                        "lineage_parent_agent_id": parent,
                        "lineage_ancestors": ancestors,
                        "status": "completed", "provider": "opencode", "access_mode": "read_only",
                        "started_at": time.time(), "ended_at": time.time(), "last_activity_at": time.time(),
                    }), encoding="utf-8")
                    for name in ("result.txt", "stdout.log", "stderr.log", "worker.log"):
                        (adir / name).write_text("", encoding="utf-8")
                write_agent("agt_root", None, "agt_root", [])
                write_agent("agt_left", "agt_root", "agt_root", ["agt_root"])
                write_agent("agt_right", "agt_root", "agt_root", ["agt_root"])

                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
                context = PolicyContext(profile="trusted", actor="agent:agt_left", agent_id="agt_left")
                mcp = ObservedFastMCP(
                    name="lineage-test", telemetry=telemetry,
                    policy_context_provider=lambda: context,
                )
                body_calls = 0

                @mcp.tool(name="get_agent", structured_output=False)
                def fake_get_agent(agent_id: str, include_logs: bool = False, tail_lines: int = 40):
                    nonlocal body_calls
                    body_calls += 1
                    return {"ok": True, "agent_id": agent_id}

                @mcp.tool(name="tool_invoke", structured_output=False)
                async def fake_tool_invoke(tool_name: str, arguments: dict | None = None):
                    return {"result": await mcp.call_tool(tool_name, arguments or {})}

                with patch.object(agents, "AGENTS_DIR", agents_root), patch.object(agents, "TEAMS_DIR", teams_root):
                    with self.assertRaises(ToolError) as direct:
                        await mcp.call_tool("get_agent", {"agent_id": "agt_right", "include_logs": True})
                    self.assertIn("agent_control_denied", str(direct.exception))
                    with self.assertRaises(ToolError) as nested:
                        await mcp.call_tool(
                            "tool_invoke",
                            {"tool_name": "get_agent", "arguments": {"agent_id": "agt_right", "include_logs": True}},
                        )
                    self.assertIn("agent_control_denied", str(nested.exception))
                self.assertEqual(0, body_calls)
                events = telemetry.query_security_events(hours=1, limit=20)
                denies = [row for row in events if row.get("event_type") == "AGENT_CONTROL_DENY"]
                self.assertEqual(2, len(denies))
                self.assertTrue(all(row.get("agent_id") == "agt_left" for row in denies))
                self.assertTrue(all("agt_right" in str(row.get("target_summary")) for row in denies))

        asyncio.run(run())

    def test_denied_mutating_control_is_blocked_before_side_effect_intent(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                agents_root = root / "agents"; teams_root = root / "teams"
                agents_root.mkdir(); teams_root.mkdir()
                for agent_id, parent, ancestors in (
                    ("agt_root", None, []),
                    ("agt_left", "agt_root", ["agt_root"]),
                    ("agt_right", "agt_root", ["agt_root"]),
                ):
                    adir = agents_root / agent_id; adir.mkdir()
                    (adir / "meta.json").write_text(__import__("json").dumps({
                        "agent_id": agent_id, "parent_agent_id": parent,
                        "lineage_version": agents._LINEAGE_VERSION,
                        "lineage_root_agent_id": "agt_root",
                        "lineage_parent_agent_id": parent,
                        "lineage_ancestors": ancestors,
                        "status": "running" if agent_id == "agt_right" else "completed",
                        "provider": "opencode", "access_mode": "read_only",
                        "started_at": time.time(), "last_activity_at": time.time(),
                    }), encoding="utf-8")
                    for name in ("result.txt", "stdout.log", "stderr.log", "worker.log"):
                        (adir / name).write_text("", encoding="utf-8")

                telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3", max_events=100)
                context = PolicyContext(profile="trusted", actor="agent:agt_left", agent_id="agt_left")
                mcp = ObservedFastMCP(name="lineage-intent-test", telemetry=telemetry, policy_context_provider=lambda: context)
                body_calls = 0

                @mcp.tool(name="agent_action", structured_output=False)
                def fake_agent_action(action: str, agent_id: str | None = None, team_id: str | None = None,
                                      message: str | None = None, signal: str = "TERM"):
                    nonlocal body_calls
                    body_calls += 1
                    return {"ok": True}

                with patch.object(agents, "AGENTS_DIR", agents_root), patch.object(agents, "TEAMS_DIR", teams_root), \
                     patch("mcp_server.observability.begin_side_effect") as begin_intent:
                    with self.assertRaises(ToolError) as denied:
                        await mcp.call_tool("agent_action", {"action": "cancel", "agent_id": "agt_right"})
                self.assertIn("agent_control_denied", str(denied.exception))
                self.assertEqual(0, body_calls)
                begin_intent.assert_not_called()
                denies = [row for row in telemetry.query_security_events(hours=1, limit=20)
                          if row.get("event_type") == "AGENT_CONTROL_DENY"]
                self.assertEqual(1, len(denies))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
