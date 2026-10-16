"""#211: an optional usage limit stops a scheduled AI run at N tokens or $N, where the agent reports usage live."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import run_budget
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests.test_scheduled_tasks import StoreCase


def claude_message(message_id: str, output: int, cache_read: int = 0) -> dict:
    return {"type": "assistant", "message": {"id": message_id, "usage": {
        "input_tokens": 10, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": 0,
        "output_tokens": output}}}


class LimitRulesTests(unittest.TestCase):
    def test_no_limit_unless_one_is_set(self) -> None:
        for empty in (None, {}, {"max_tokens": None, "max_cost_usd": 0}):
            self.assertIsNone(run_budget.validate(empty))
        self.assertEqual({"max_tokens": 200_000, "max_cost_usd": 1.5},
                         run_budget.validate({"max_tokens": 200_000, "max_cost_usd": 1.499}))
        for bad in ({"max_tokens": 10}, {"max_tokens": 2.5}, {"max_tokens": True}, {"max_cost_usd": -1},
                    {"max_cost_usd": "5"}, "200000"):
            with self.subTest(bad=bad), self.assertRaises(run_budget.BudgetError):
                run_budget.validate(bad)

    def test_a_limit_the_agent_cannot_keep_is_refused_with_the_reason(self) -> None:
        tokens, cost = {"max_tokens": 50_000}, {"max_cost_usd": 2.0}
        self.assertIsNone(run_budget.unenforceable(tokens, "claude"))
        self.assertIsNone(run_budget.unenforceable({**tokens, **cost}, "opencode"))
        self.assertIsNone(run_budget.unenforceable(cost, "kilo"))
        self.assertIn("only when it finishes", run_budget.unenforceable(tokens, "codex", "Codex"))
        self.assertIn("price", run_budget.unenforceable(cost, "claude", "Claude Code"))
        self.assertIn("Choose an agent", run_budget.unenforceable(tokens, None))
        self.assertIsNone(run_budget.unenforceable(None, "codex"))


class MeterTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.path = Path(tempfile.mkdtemp(prefix="mac-mcp-meter-")) / "stdout.log"

    def write(self, *events, partial: str = "") -> None:
        with self.path.open("a") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
            handle.write(partial)

    def test_claude_counts_each_message_once_and_takes_the_final_result(self) -> None:
        meter = run_budget.Meter(self.path, "claude")
        self.assertEqual({"tokens": 0, "cost_usd": None, "cost_unknown": False}, meter.read())  # nothing written yet
        self.write(claude_message("msg_1", 5), claude_message("msg_1", 40), {"type": "user", "message": {}})
        self.assertEqual(50, meter.read()["tokens"])  # 10 input + 40 output, the second event replaces the first
        line = json.dumps(claude_message("msg_2", 100, cache_read=1_000))
        self.write(partial=line[:30])
        self.assertEqual(50, meter.read()["tokens"])  # half a line is not counted yet
        self.write(partial=line[30:] + "\n")
        self.assertEqual(1_160, meter.read()["tokens"])
        self.write({"type": "result", "total_cost_usd": 0.04, "usage": {
            "input_tokens": 20, "cache_read_input_tokens": 1_000, "output_tokens": 150}})
        self.assertEqual({"tokens": 1_170, "cost_usd": 0.04, "cost_unknown": False}, meter.read())

    def test_opencode_and_kilo_steps_carry_tokens_and_price(self) -> None:
        meter = run_budget.Meter(self.path, "opencode")
        self.write({"type": "step_finish", "part": {"id": "p1", "cost": 0.5, "tokens": {"total": 900}}},
                   {"type": "step_finish", "part": {"id": "p2", "cost": 0.75, "tokens": {"total": 1_100}}})
        self.assertEqual({"tokens": 2_000, "cost_usd": 1.25, "cost_unknown": False}, meter.read())
        kilo = Path(str(self.path) + ".kilo")
        kilo.write_text(json.dumps({"type": "step_finish", "part": {
            "id": "k1", "tokens": {"input": 10, "output": 2, "cache": {"read": 4}}}}) + "\n")
        self.assertEqual({"tokens": 12, "cost_usd": None, "cost_unknown": True}, run_budget.Meter(kilo, "kilo").read())

    def test_a_step_without_a_price_makes_the_cost_unknown_and_stops_a_cost_limit(self) -> None:
        # Some OpenCode models report tokens but no price. A $ limit would then never trigger, so the
        # run is stopped instead of spending past what the owner set.
        meter = run_budget.Meter(self.path, "opencode")
        self.write({"type": "step_finish", "part": {"id": "p1", "cost": 0.2, "tokens": {"total": 900}}},
                   {"type": "step_finish", "part": {"id": "p2", "tokens": {"total": 1_100}}})
        used = meter.read()
        self.assertTrue(used["cost_unknown"])
        self.assertEqual("cost_unknown", run_budget.over({"max_cost_usd": 100.0}, used))
        self.assertIsNone(run_budget.over({"max_tokens": 50_000}, used))  # a token limit is unaffected
        reason = run_budget.stop_reason({"max_cost_usd": 100.0}, "cost_unknown", used, "OpenCode")
        self.assertIn("didn't report a price", reason)
        free = run_budget.Meter(Path(str(self.path) + ".free"), "opencode")
        Path(str(self.path) + ".free").write_text(json.dumps(
            {"type": "step_finish", "part": {"id": "p1", "cost": 0, "tokens": {"total": 900}}}) + "\n")
        self.assertIsNone(run_budget.over({"max_cost_usd": 1.0}, free.read()))  # a free model costs $0

    def test_pi_counts_each_turn(self) -> None:
        meter = run_budget.Meter(self.path, "pi")
        self.write({"type": "turn_end", "message": {"role": "assistant", "responseId": "r1",
                                                     "usage": {"input": 2_480, "output": 298, "totalTokens": 2_778}}})
        self.assertEqual(2_778, meter.read()["tokens"])

    def test_over(self) -> None:
        self.assertEqual("tokens", run_budget.over({"max_tokens": 1_000}, {"tokens": 1_000, "cost_usd": None}))
        self.assertEqual("cost", run_budget.over({"max_cost_usd": 1.0}, {"tokens": 5, "cost_usd": 1.2}))
        self.assertIsNone(run_budget.over({"max_cost_usd": 1.0}, {"tokens": 5, "cost_usd": None}))
        self.assertIsNone(run_budget.over(None, {"tokens": 10**9}))


class LimitDefinitionTests(StoreCase):
    def test_ai_tasks_keep_an_optional_limit_their_agent_can_enforce(self) -> None:
        definition = store.validate_definition(self.ai(provider="claude", usage_limit={"max_tokens": 300_000}))
        self.assertEqual({"max_tokens": 300_000}, definition["usage_limit"])
        self.assertNotIn("usage_limit", store.validate_definition(self.ai(provider="codex")))
        self.assertNotIn("usage_limit", store.validate_definition(self.ai(provider="claude", usage_limit={})))
        with self.assertRaises(store.TaskError) as refused:
            store.validate_definition(self.ai(provider="codex", usage_limit={"max_tokens": 300_000}))
        self.assertEqual("usage_limit_unenforceable", refused.exception.code)
        self.assertIn("Codex", str(refused.exception))
        with self.assertRaises(store.TaskError) as script:
            store.validate_definition(self.sh(usage_limit={"max_tokens": 300_000}))
        self.assertEqual("usage_limit_ai_only", script.exception.code)

    def test_the_default_agent_is_the_one_checked(self) -> None:
        self.settings_file.write_text(json.dumps({"privacy": {}, "subagents": {"default": {"provider": "codex"}}}))
        with self.assertRaises(store.TaskError):
            store.validate_definition(self.ai(usage_limit={"max_tokens": 300_000}))
        self.settings_file.write_text(json.dumps({"privacy": {}, "subagents": {"default": {"provider": "opencode"}}}))
        self.assertEqual({"max_cost_usd": 3.0},
                         store.validate_definition(self.ai(usage_limit={"max_cost_usd": 3}))["usage_limit"])


class LimitRunTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent_dir = self.dir / "agents" / "agent_abc"
        self.agent_dir.mkdir(parents=True)
        self.log = self.agent_dir / "stdout.log"
        self.log.write_text("")
        self.calls = {"spawn": [], "cancel": [], "polls": 0}
        from mcp_server import tools_agents

        def spawn(settings, provider, prompt, **kwargs):
            self.calls["spawn"].append(provider)
            return {"agent_id": "agent_abc"}

        def get(settings, agent_id, **kwargs):
            self.calls["polls"] += 1
            if self.calls["cancel"]:
                return {"status": "cancelled", "provider": "claude", "result_full": {"text": "Partial report"}}
            if self.calls["polls"] == 2:  # the agent works: two messages, 2,050 tokens
                with self.log.open("a") as handle:
                    for event in (claude_message("m1", 990), claude_message("m2", 1_040)):
                        handle.write(json.dumps(event) + "\n")
            if self.calls["polls"] > 6:
                return {"status": "completed", "provider": "claude", "result_envelope": {"outcome": "success"},
                        "result_full": {"text": "All done"}}
            return {"status": "running"}

        def action(settings, action, agent_id, requested_by):
            self.calls["cancel"].append(agent_id)
            return {"ok": True}

        for item in (patch.object(tools_agents, "spawn_agent", spawn), patch.object(tools_agents, "get_agent", get),
                     patch.object(tools_agents, "agent_action", action),
                     patch.object(tools_agents, "_agent_dir", lambda agent_id: self.agent_dir),
                     patch.object(runner, "AGENT_POLL_S", 0), patch.object(runner, "LIMIT_POLL_S", 0)):
            item.start()
            self.addCleanup(item.stop)

    def run_task(self, **extra):
        definition = store.validate_definition(self.ai(provider="claude", cwd=str(self.folder), **extra))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        return runner.execute(task["id"], run_id, definition, settings=None)

    def test_the_agent_is_stopped_at_the_limit_and_the_run_says_so(self) -> None:
        result = self.run_task(usage_limit={"max_tokens": 2_000})
        self.assertEqual(["agent_abc"], self.calls["cancel"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("Stopped at its 2,000-token limit (used 2,050)", result["reason"])
        self.assertEqual({"max_tokens": 2_000, "used_tokens": 2_050, "used_cost_usd": None, "stopped": "tokens"},
                         result["detail"]["usage_limit"])
        self.assertEqual("Partial report", result["output"])  # what it did before the stop is kept

    def test_a_run_under_its_limit_finishes_and_shows_what_it_used(self) -> None:
        result = self.run_task(usage_limit={"max_tokens": 50_000})
        self.assertEqual(([], "done"), (self.calls["cancel"], result["status"]))
        self.assertEqual({"max_tokens": 50_000, "used_tokens": 2_050, "used_cost_usd": None, "stopped": None},
                         result["detail"]["usage_limit"])

    def test_no_limit_no_meter(self) -> None:
        result = self.run_task()
        self.assertEqual("done", result["status"])
        self.assertNotIn("usage_limit", result["detail"])

    def test_an_agent_that_cannot_keep_the_limit_is_not_started(self) -> None:
        self.settings_file.write_text(json.dumps({"privacy": {}, "subagents": {"default": {"provider": "opencode"}}}))
        definition = store.validate_definition(self.ai(cwd=str(self.folder), usage_limit={"max_cost_usd": 1}))
        task = store.create_task(definition)
        # The owner later makes Codex the Default Agent: the cost limit can no longer be kept.
        self.settings_file.write_text(json.dumps({"privacy": {}, "subagents": {"default": {"provider": "codex"}}}))
        result = runner.execute(task["id"], store.start_run(task["id"], trigger="manual"), definition, settings=None)
        self.assertEqual(([], "failed"), (self.calls["spawn"], result["status"]))
        self.assertIn("The agent was not started", result["reason"])


class LimitSurfaceTests(StoreCase):
    def test_agents_can_suggest_a_limit_and_the_app_learns_what_each_agent_keeps(self) -> None:
        import asyncio

        from mcp_server.main import _unwrap_tool_result
        from tests.test_core_catalog_descriptions import _build_mcp
        mcp = _build_mcp()
        created = _unwrap_tool_result(asyncio.run(mcp.call_tool("scheduled_tasks", {
            "action": "create", "name": "Weekly ad report", "kind": "ai", "prompt": "Summarize it.", "agent": "claude",
            "schedule": {"type": "weekly", "time": "09:00", "weekdays": [0]}, "usage_limit": {"max_tokens": 400_000}})))
        self.assertEqual({"max_tokens": 400_000}, created["task"]["proposal"]["usage_limit"])
        refused = _unwrap_tool_result(asyncio.run(mcp.call_tool("scheduled_tasks", {
            "action": "create", "name": "Weekly ad report", "kind": "ai", "prompt": "Summarize it.", "agent": "codex",
            "schedule": {"type": "weekly", "time": "09:00", "weekdays": [0]}, "usage_limit": {"max_tokens": 400_000}})))
        self.assertEqual("usage_limit_unenforceable", refused["error"])

        from tests.test_scheduled_tasks import AUTH, DashboardApiTests
        self.settings_file.write_text(json.dumps({"privacy": {}, "subagents": {
            "default": {"provider": "claude"}, "providers": {"claude": {"enabled": True}, "codex": {"enabled": True}}}}))
        listed = DashboardApiTests.client(self).get("/dashboard/api/scheduled-tasks", headers=AUTH).json()
        self.assertEqual("claude", listed["default_agent"])
        limits = {agent["id"]: agent["limits"] for agent in listed["agents"]}
        self.assertEqual({"tokens": True, "cost": False}, limits.get("claude"))
        self.assertEqual({"tokens": False, "cost": False}, limits.get("codex"))


if __name__ == "__main__":
    unittest.main()
