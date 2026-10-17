"""#226: a run stopped at its time limit keeps what was seen so far and says what is unknown."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import time
from unittest.mock import patch

from mcp_server import scheduled_task_runner as runner
from tests.test_scheduled_tasks import StoreCase

EVENTS = [
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/tmp/report.csv"}}],
        "usage": {"input_tokens": 40, "cache_read_input_tokens": 0, "output_tokens": 60}}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "written"}]}},
]


class DeadlineEvidenceTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent_dir = self.dir / "agents" / "agent_abc"
        self.agent_dir.mkdir(parents=True)
        (self.agent_dir / "stdout.log").write_text("".join(json.dumps(event) + "\n" for event in EVENTS))
        self.cancelled = []
        from mcp_server import tools_agents
        self.terminal_after_cancel = True

        def get(settings, agent_id, **kwargs):
            if self.cancelled and self.terminal_after_cancel:
                return {"status": "cancelled", "provider": "claude", "result_full": {"text": "Wrote half the report"}}
            return {"status": "running", "provider": "claude"}

        def action(settings, action, agent_id, requested_by):
            self.cancelled.append(agent_id)
            return {"ok": True}

        for item in (patch.object(tools_agents, "get_agent", get), patch.object(tools_agents, "agent_action", action),
                     patch.object(tools_agents, "_agent_dir", lambda agent_id: self.agent_dir),
                     patch.object(runner, "AGENT_POLL_S", 0), patch.object(runner, "CANCEL_SETTLE_S", 0.2)):
            item.start()
            self.addCleanup(item.stop)

    def check(self, result):
        self.assertEqual(["agent_abc"], self.cancelled)
        self.assertEqual("timed_out", result["status"])
        detail = result["detail"]
        self.assertTrue(detail["steps"], "the steps seen before the stop are kept")
        self.assertIn("report.csv", json.dumps(detail["steps"]))
        # Claude totals its usage only in its last event, which a stopped run never writes: the tokens
        # seen message by message are kept as observed, never as a complete count.
        self.assertEqual({"steps": "partial", "usage": "partial", "observed_tokens": 100, "external_effects": "unknown"},
                         detail["evidence"])

    def test_the_steps_and_usage_before_the_time_limit_are_kept(self) -> None:
        result = runner.wait_for_agent("agent_abc", None, deadline=time.time() - 1)
        self.check(result)
        self.assertEqual("Wrote half the report", result["output"])

    def test_they_are_kept_even_when_the_agent_never_reports_it_stopped(self) -> None:
        self.terminal_after_cancel = False
        started = time.time()
        result = runner.wait_for_agent("agent_abc", None, deadline=time.time() - 1)
        self.assertLess(time.time() - started, 5)  # bounded wait, then the run ends all the same
        self.check(result)

    def test_a_finished_run_carries_no_partial_mark(self) -> None:
        from mcp_server import tools_agents
        with patch.object(tools_agents, "get_agent", lambda *a, **k: {
                "status": "completed", "provider": "claude", "result_envelope": {"outcome": "success"},
                "result_full": {"text": "Done"}}):
            result = runner.wait_for_agent("agent_abc", None, deadline=time.time() + 60)
        self.assertEqual("done", result["status"])
        self.assertNotIn("evidence", result["detail"])


class SkeletonTests(StoreCase):
    def test_how_a_run_ended_outlives_its_contents(self) -> None:
        from mcp_server import run_records
        detail = {"agent_id": "a", "output": "secret text", "usage_limit": {"max_tokens": 5000, "used_tokens": 5100,
                                                                             "stopped": "tokens"},
                  "evidence": {"steps": "partial", "usage": "partial", "observed_tokens": 100,
                               "external_effects": "unknown"}}
        kept = run_records.skeleton(detail)
        self.assertEqual(detail["evidence"], kept["evidence"])
        self.assertEqual(detail["usage_limit"], kept["usage_limit"])
        self.assertNotIn("output", kept)
