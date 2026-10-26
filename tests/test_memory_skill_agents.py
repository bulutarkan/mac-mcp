"""Memory and skills for delegated agents and their use counted (#110, part 2)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import runtime_settings
from mcp_server import tools_agents as agents
from mcp_server import tools_memory
from mcp_server.policy_scope import ResourceScope
from mcp_server.usage_metering import (
    UsageCollector, UsageSample, clear_usage, memory_skill_outcome, query_usage_summary,
)


def scope(**extra) -> ResourceScope:
    return ResourceScope.from_dict({"access_mode": "read_only", "path_roots": ["/tmp"], **extra})


class AgentInstructionTests(unittest.TestCase):
    def test_a_normal_agent_gets_conditional_lookups_and_rare_quiet_saves(self) -> None:
        text = agents._memory_skill_instruction(scope(), provenance_class="local", scoped_mcp=True)
        self.assertIn("call memory_search with a few key words before searching files or the web", text)
        self.assertIn("call skill_search once, even for a short task, and skill_get the best match if one fits", text)
        self.assertIn("'from now on', 'always', 'never'", text)
        self.assertIn("saving it is part of the task", text)
        self.assertIn("A task that needs no owner facts and follows no procedure needs none of this.", text)
        self.assertIn("memory_update the match or memory_add one short sentence", text)
        self.assertIn("save only the safe part once or skip it", text)
        self.assertIn("never mention reading or saving memory", text)
        self.assertLess(len(text.split()), 220)

    def test_work_from_outside_content_reads_but_never_saves(self) -> None:
        text = agents._memory_skill_instruction(scope(), provenance_class="external", scoped_mcp=True)
        self.assertIn("memory_search", text)
        self.assertNotIn("memory_add", text)

    def test_no_block_without_the_tools(self) -> None:
        self.assertEqual("", agents._memory_skill_instruction(
            scope(tool_families=["browser", "interactive"]), provenance_class="local", scoped_mcp=True))
        self.assertEqual("", agents._memory_skill_instruction(scope(), provenance_class="local", scoped_mcp=False))
        skills_only = agents._memory_skill_instruction(scope(tool_families=["skills", "files"]),
                                                       provenance_class="local", scoped_mcp=True)
        self.assertIn("skill_search once, even for a short task", skills_only)
        self.assertNotIn("memory_search", skills_only)

    def test_the_spawned_agents_prompt_carries_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(root / "state"),
                                         "MAC_MCP_LESSON_DIR": str(root / "lessons")}), \
                    patch.object(agents, "AGENTS_DIR", root / "agents"), \
                    patch.object(agents, "provider_enabled", return_value=True), \
                    patch.object(agents, "_find_binary", return_value="/tmp/codex"), \
                    patch.object(agents, "_base_env", return_value={}), \
                    patch.object(agents.subprocess, "Popen", return_value=SimpleNamespace(pid=43210)), \
                    patch.object(agents.threading, "Thread", return_value=SimpleNamespace(start=lambda: None)):
                spawned = agents._spawn_internal(
                    settings=None, provider="codex", prompt="Write the weekly report.", model="gpt-test",
                    reasoning="high", cwd=str(root), timeout_s=600, title="t", result_style="concise",
                    access_mode="full", scope=ResourceScope.from_dict({"access_mode": "full", "path_roots": [str(root)]}),
                    permission_profile="trusted",
                )
                effective = (root / "agents" / spawned["agent_id"] / "effective_prompt.txt").read_text(encoding="utf-8")
            with agents._WORKERS_LOCK:
                agents._WORKERS.clear()
        self.assertIn("Mac MCP memory and skills", effective)
        self.assertLess(effective.index("Mac MCP memory and skills"), effective.index("TASK_RESULT_ENVELOPE_V1"))
        self.assertTrue(effective.startswith("Write the weekly report.\n\nMac MCP memory and skills"))

    def test_read_only_agents_are_told_memory_saves_are_allowed(self) -> None:
        text = agents._access_instruction("read_only", "read_only")
        self.assertIn("This task is read-only.", text)
        self.assertIn("Saving to the owner's Mac MCP memory (memory_add, memory_update) and team messages are allowed", text)

    def test_a_read_only_agent_may_save_through_tool_invoke_too(self) -> None:
        from mcp_server.policy import evaluate_profile, resolve_risk

        for tool, allowed in (("memory_add", True), ("memory_update", True), ("write_file", False), ("memory_delete", False)):
            _, risk = resolve_risk("tool_invoke", {"tool_name": tool, "arguments": {}})
            self.assertEqual(allowed, evaluate_profile("read_only", risk).allowed, tool)

    def test_a_memorys_source_is_text_not_a_path(self) -> None:
        from mcp_server.policy import evaluate_tool_scope

        narrow = ResourceScope.from_dict({"access_mode": "read_only", "path_roots": ["/tmp/project"]})
        self.assertTrue(evaluate_tool_scope(narrow, "memory_add", {"content": "x", "source": "Owner instruction"}).allowed)
        self.assertTrue(evaluate_tool_scope(narrow, "memory_update", {"memory_id": "m", "source": "chat"}).allowed)
        self.assertFalse(evaluate_tool_scope(narrow, "read_file", {"path": "/etc/hosts"}).allowed)

    def test_tool_invoke_accepts_the_name_a_client_shows(self) -> None:
        from mcp_server.policy import RISK_REGISTRY, evaluate_profile, resolve_risk, strip_client_prefix

        known = lambda name: name in RISK_REGISTRY  # noqa: E731
        self.assertEqual("skill_search", strip_client_prefix("mcp__mac_mcp_agent__skill_search", known))
        self.assertEqual("memory_add", strip_client_prefix("mcp__mac-mcp__memory_add", known))
        self.assertEqual("mcp__x__nothing", strip_client_prefix("mcp__x__nothing", known))
        _, risk = resolve_risk("tool_invoke", {"tool_name": "mcp__mac_mcp_agent__skill_search", "arguments": {"query": "x"}})
        self.assertTrue(evaluate_profile("read_only", risk).allowed)
        _, risk = resolve_risk("tool_invoke", {"tool_name": "mcp__mac_mcp_agent__write_file", "arguments": {}})
        self.assertFalse(evaluate_profile("read_only", risk).allowed)

    def test_the_secret_refusal_says_retry_once_without_it(self) -> None:
        self.assertIn("once, or skip saving it. Never retry with the secret.", tools_memory._SECRET_MESSAGE)


class OutcomeTests(unittest.TestCase):
    def test_outcomes(self) -> None:
        cases = [
            (("memory_search", "success", {}, {"ok": True, "count": 3}), ("matched", "")),
            (("memory_search", "success", {}, {"ok": True, "count": 0}), ("no_match", "")),
            (("memory_add", "success", {}, {"ok": True, "action": "added"}), ("saved", "")),
            (("memory_add", "success", {}, {"ok": True, "action": "already_saved"}), ("already_saved", "")),
            (("memory_add", "error", {}, None, "400: MEMORY_CONTAINS_SECRET: no"), ("rejected_secret", "")),
            (("memory_update", "success", {}, {"ok": True, "action": "unchanged"}), ("unchanged", "")),
            (("memory_update", "success", {}, {"ok": True, "action": "updated"}), ("updated", "")),
            (("skill_search", "success", {}, {"ok": True, "count": 1}), ("matched", "")),
            (("skill_get", "success", {"name": "swiftui-pro"}, {"ok": True, "name": "swiftui-pro"}), ("loaded", "swiftui-pro")),
            (("skill_get", "error", {"name": "x"}, None, "404: Skill not found."), ("not_found", "")),
            (("memory_search", "error", {}, None, "boom"), ("error", "")),
            (("memory_search", "success", {}, {"result": {"ok": True, "count": 2}}), ("matched", "")),
            # What MCP hands the meter: (content, structured) and tool_invoke's JSON text.
            (("memory_search", "success", {}, ([{"type": "text", "text": "..."}], {"result": {"ok": True, "count": 1}})),
             ("matched", "")),
            (("memory_search", "success", {}, [{"type": "text", "text": '{"ok": true, "count": 4}'}]), ("matched", "")),
            # tool_invoke is recorded again as the tool that ran, so it never counts itself.
            (("tool_invoke", "success", {"tool_name": "skill_get", "arguments": {"name": "ui-ux-pro-max"}},
              [{"type": "text", "text": '{"ok": true, "name": "ui-ux-pro-max"}'}]), None),
        ]
        for args, expected in cases:
            self.assertEqual(expected, memory_skill_outcome(*args), args)
        self.assertIsNone(memory_skill_outcome("read_file", "success", {}, {}))


class StoredOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"privacy": {"usage_retention_days": 30}}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings)})
        self.env.start()
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
        self.db = self.root / "telemetry.sqlite3"

    def tearDown(self) -> None:
        self.env.stop()
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
        self.temp.cleanup()

    def submit(self, collector, tool, status, arguments, result, error=None, actor="primary", ts=None):
        collector.submit(UsageSample(timestamp=ts or time.time(), tool=tool, actor_class=actor, status=status,
                                     duration_ms=3, arguments=arguments, result=result, error=error))

    def test_counts_reach_the_usage_summary_without_contents(self) -> None:
        collector = UsageCollector(self.db, enabled=True)
        self.submit(collector, "memory_search", "success", {"query": "owner's bank"}, {"ok": True, "count": 2})
        self.submit(collector, "memory_search", "success", {"query": "x"}, {"ok": True, "count": 0}, actor="scoped_subagent")
        self.submit(collector, "memory_add", "error", {"content": "token sk-secret"}, None, "MEMORY_CONTAINS_SECRET")
        self.submit(collector, "memory_add", "success", {"content": "Owner prefers Turkish"}, {"ok": True, "action": "added"})
        self.submit(collector, "skill_get", "success", {"name": "swiftui-pro"}, {"ok": True, "name": "swiftui-pro"})
        self.submit(collector, "skill_get", "success", {"name": "swiftui-pro"}, {"ok": True, "name": "swiftui-pro"})
        self.submit(collector, "read_file", "success", {"path": "/x"}, {"ok": True})
        self.submit(collector, "memory_update", "success", {"memory_id": "m"},
                    ([{"type": "text", "text": "..."}], {"result": {"ok": True, "action": "updated"}}), actor="scoped_subagent")
        self.assertTrue(collector.wait_idle(3.0))
        summary = query_usage_summary(self.db, days=30)["memory_skills"]
        self.assertEqual({"matched": 1, "no_match": 1}, summary["tools"]["memory_search"])
        self.assertEqual({"rejected_secret": 1, "saved": 1}, summary["tools"]["memory_add"])
        self.assertEqual([{"name": "swiftui-pro", "count": 2}], summary["skills_loaded"])
        self.assertNotIn("read_file", summary["tools"])
        agents_only = query_usage_summary(self.db, days=30, actor_class="scoped_subagent")["memory_skills"]
        self.assertEqual({"memory_search": {"no_match": 1}, "memory_update": {"updated": 1}}, agents_only["tools"])
        conn = sqlite3.connect(self.db)
        stored = json.dumps(conn.execute("SELECT * FROM memory_skill_daily").fetchall())
        conn.close()
        for text in ("owner's bank", "sk-secret", "Owner prefers Turkish"):
            self.assertNotIn(text, stored)

    def test_retention_and_clear_cover_it(self) -> None:
        collector = UsageCollector(self.db, enabled=True)
        self.submit(collector, "memory_search", "success", {}, {"ok": True, "count": 1}, ts=time.time() - 60 * 86400)
        self.submit(collector, "memory_search", "success", {}, {"ok": True, "count": 1})
        self.assertTrue(collector.wait_idle(3.0))
        self.assertEqual(1, self.rows())
        clear_usage(self.db)
        self.assertEqual(0, self.rows())

    def rows(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM memory_skill_daily").fetchone()[0]
        finally:
            conn.close()

    def test_metering_off_counts_nothing(self) -> None:
        self.settings.write_text(json.dumps({"privacy": {"usage_metering": False}}), encoding="utf-8")
        runtime_settings._usage_privacy_cache.update({"at": -1.0, "value": None})
        collector = UsageCollector(self.db, enabled=True)
        self.assertFalse(collector.submit(UsageSample(timestamp=time.time(), tool="memory_search", actor_class="primary",
                                                      status="success", duration_ms=1, arguments={}, result={"count": 1})))


class UsageCardSourceTests(unittest.TestCase):
    def test_the_usage_pane_shows_memory_and_skills(self) -> None:
        root = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
        settings = (root / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('Text("Memory and skills")', settings)
        self.assertIn("MemorySkillUsageView(usage: memorySkills)", settings)
        view = (root / "UsageOverview.swift").read_text(encoding="utf-8")
        for words in ("Memory lookups", "Memories saved", "Skills used", "held a secret"):
            self.assertIn(words, view)


if __name__ == "__main__":
    unittest.main()
