from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from mcp_server import tools_agents as agents


class FailureAwareTeamOutcomeTests(unittest.TestCase):
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

    def agent(self, agent_id: str, status: str, *, team_id: str | None = None, task_id: str | None = None) -> str:
        adir = agents.AGENTS_DIR / agent_id
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "result.txt").write_text("result\n", encoding="utf-8")
        (adir / "prompt.txt").write_text("prompt\n", encoding="utf-8")
        now = time.time()
        meta = {
            "agent_id": agent_id,
            "team_id": team_id,
            "team_task_id": task_id,
            "title": agent_id,
            "provider": "opencode",
            "access_mode": "read_only",
            "permission_profile": "trusted",
            "status": status,
            "phase": status,
            "started_at": now - 1,
            "spawn_requested_at": now - 1,
            "last_activity_at": now,
            "updated_at": now,
            "ended_at": now if status in agents.TERMINAL_STATUSES else None,
            "retry_count": 0,
            "retries": 0,
        }
        agents._write_meta(agent_id, meta)
        return agent_id

    def team(self, team_id: str, tasks: list[dict], agent_ids: list[str], *, cancelled: bool = False,
             budget_exhausted_reason: str | None = None) -> None:
        now = time.time()
        agents._write_team(team_id, {
            "team_id": team_id,
            "title": team_id,
            "provider": "opencode",
            "access_mode": "read_only",
            "permission_profile": "trusted",
            "scope": None,
            "created_at": now - 1,
            "updated_at": now,
            "agent_ids": list(agent_ids),
            "scheduler_version": 1,
            "tasks": tasks,
            "cancelled": cancelled,
            "max_parallel": max(1, len(tasks)),
            "max_revisions": 0,
            "retries": 0,
            "team_retry_count": 0,
            "team_retry_reservations": [],
            "budget_exhausted_reason": budget_exhausted_reason,
        })

    def test_any_failed_plus_running_does_not_form_success_quorum(self) -> None:
        failed = self.agent("agt_failed", "failed")
        running = self.agent("agt_running", "running")
        result = agents.wait_agents(None, agent_ids=[failed, running], mode="any", timeout_s=0)
        self.assertFalse(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["quorum_possible"])
        self.assertEqual(0, result["successful_count"])
        self.assertEqual(1, result["failure_count"])
        self.assertEqual(1, result["pending_count"])
        self.assertEqual("running", result["outcome"])

    def test_any_completed_result_forms_success_quorum(self) -> None:
        completed = self.agent("agt_completed", "completed")
        running = self.agent("agt_running", "running")
        result = agents.wait_agents(None, agent_ids=[completed, running], mode="any", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertTrue(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(1, result["successful_count"])
        self.assertEqual("running", result["outcome"])

    def test_majority_two_failed_one_running_is_impossible_not_timed_out(self) -> None:
        ids = [
            self.agent("agt_failed_1", "failed"),
            self.agent("agt_failed_2", "failed"),
            self.agent("agt_running", "running"),
        ]
        result = agents.wait_agents(None, agent_ids=ids, mode="majority", timeout_s=30)
        self.assertFalse(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["quorum_possible"])
        self.assertEqual(2, result["required_successes"])
        self.assertEqual(2, result["failure_count"])
        self.assertEqual(1, result["pending_count"])
        self.assertEqual("running", result["outcome"])

    def test_majority_two_success_one_failure_succeeds_with_partial_failure_outcome(self) -> None:
        ids = [
            self.agent("agt_ok_1", "completed"),
            self.agent("agt_ok_2", "completed"),
            self.agent("agt_failed", "failed"),
        ]
        result = agents.wait_agents(None, agent_ids=ids, mode="majority", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertTrue(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual("partial_failure", result["outcome"])
        self.assertEqual(2, result["successful_count"])
        self.assertEqual(1, result["failure_count"])

    def test_all_completion_is_separate_from_success_for_partial_failure(self) -> None:
        ids = [self.agent("agt_ok", "completed"), self.agent("agt_failed", "failed")]
        result = agents.wait_agents(None, agent_ids=ids, mode="all", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["partial_failure"])
        self.assertEqual("partial_failure", result["outcome"])
        self.assertEqual(2, result["quorum_terminal_count"])
        self.assertEqual("failed", result["failure_reasons"][0]["reason"])

    def test_agent_timeout_is_failed_work_not_waiter_timeout(self) -> None:
        ids = [self.agent("agt_timeout", "timeout"), self.agent("agt_failed", "failed")]
        result = agents.wait_agents(None, agent_ids=ids, mode="all", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertEqual("failed", result["outcome"])
        self.assertEqual({"timeout", "failed"}, {row["reason"] for row in result["failure_reasons"]})

    def test_all_cancelled_agents_normalize_to_cancelled(self) -> None:
        ids = [self.agent("agt_cancel_1", "cancelled"), self.agent("agt_cancel_2", "cancelled")]
        result = agents.wait_agents(None, agent_ids=ids, mode="all", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertFalse(result["timed_out"])
        self.assertEqual("cancelled", result["outcome"])

    def test_dag_all_partial_failure_uses_task_quorum_and_task_failure_reason(self) -> None:
        team_id = "team_partial"
        a = self.agent("agt_task_ok", "completed", team_id=team_id, task_id="ok")
        b = self.agent("agt_task_fail", "failed", team_id=team_id, task_id="bad")
        self.team(team_id, [
            {"id": "ok", "state": "completed", "failure_reason": None, "agent_ids": [a], "latest_agent_id": a},
            {"id": "bad", "state": "failed", "failure_reason": "agent_failed", "agent_ids": [b], "latest_agent_id": b},
        ], [a, b])
        result = agents.wait_agents(None, team_id=team_id, mode="all", timeout_s=0)
        self.assertTrue(result["condition_met"])
        self.assertFalse(result["success"])
        self.assertEqual("partial_failure", result["outcome"])
        self.assertEqual(2, result["quorum_total"])
        self.assertEqual(1, result["successful_count"])
        self.assertEqual(1, result["failure_count"])
        self.assertEqual([{"task_id": "bad", "state": "failed", "reason": "agent_failed"}], result["failure_reasons"])
        self.assertEqual("completed_with_failures", result["team"]["status"])
        self.assertEqual("partial_failure", result["team"]["outcome"])
        self.assertFalse(result["team"]["success"])

    def test_dag_majority_uses_task_results_not_historical_agent_attempts(self) -> None:
        team_id = "team_revision_history"
        old_failed = self.agent("agt_old_failed", "failed", team_id=team_id, task_id="a")
        new_ok = self.agent("agt_new_ok", "completed", team_id=team_id, task_id="a")
        b_ok = self.agent("agt_b_ok", "completed", team_id=team_id, task_id="b")
        c_failed = self.agent("agt_c_failed", "failed", team_id=team_id, task_id="c")
        self.team(team_id, [
            {
                "id": "a", "state": "completed", "failure_reason": None,
                "agent_ids": [old_failed, new_ok], "latest_agent_id": new_ok,
            },
            {
                "id": "b", "state": "completed", "failure_reason": None,
                "agent_ids": [b_ok], "latest_agent_id": b_ok,
            },
            {
                "id": "c", "state": "failed", "failure_reason": "agent_failed",
                "agent_ids": [c_failed], "latest_agent_id": c_failed,
            },
        ], [old_failed, new_ok, b_ok, c_failed])
        result = agents.wait_agents(None, team_id=team_id, mode="majority", timeout_s=0)
        # Agent-attempt majority would be 3/4 and fail here. Task majority is 2/3 and succeeds.
        self.assertEqual(4, result["count"])
        self.assertEqual(3, result["quorum_total"])
        self.assertEqual(2, result["required_successes"])
        self.assertEqual(2, result["successful_count"])
        self.assertTrue(result["condition_met"])
        self.assertTrue(result["success"])
        self.assertEqual("partial_failure", result["outcome"])

    def test_team_statuses_remain_backward_compatible_while_outcome_is_normalized(self) -> None:
        base = {
            "team_id": "team_fixture", "title": "fixture", "provider": "opencode",
            "access_mode": "read_only", "permission_profile": "trusted", "scope": None,
            "created_at": time.time(), "updated_at": time.time(), "agent_ids": [],
            "scheduler_version": 1, "max_parallel": 2, "max_revisions": 0,
            "retries": 0, "team_retry_count": 0, "team_retry_reservations": [],
        }

        quality = dict(base, tasks=[
            {"id": "coder", "state": "completed", "failure_reason": None},
            {"id": "review", "state": "quality_failed", "failure_reason": "revision_limit_exhausted"},
        ], cancelled=False, budget_exhausted_reason=None)
        quality_summary = agents._team_summary("team_quality", quality)
        self.assertEqual("quality_failed", quality_summary["status"])
        self.assertEqual("partial_failure", quality_summary["outcome"])
        self.assertFalse(quality_summary["success"])

        budget = dict(base, tasks=[
            {"id": "first", "state": "completed", "failure_reason": None},
            {"id": "second", "state": "skipped", "failure_reason": "budget_exhausted:tool_call_budget"},
        ], cancelled=False, budget_exhausted_reason="tool_call_budget")
        budget_summary = agents._team_summary("team_budget", budget)
        self.assertEqual("budget_exhausted", budget_summary["status"])
        self.assertEqual("partial_failure", budget_summary["outcome"])
        self.assertEqual("budget_exhausted:tool_call_budget", budget_summary["failure_reasons"][0]["reason"])

        cancelled = dict(base, tasks=[
            {"id": "first", "state": "completed", "failure_reason": None},
            {"id": "second", "state": "cancelled", "failure_reason": "team_cancelled"},
        ], cancelled=True, budget_exhausted_reason=None)
        cancelled_summary = agents._team_summary("team_cancelled", cancelled)
        self.assertEqual("cancelled", cancelled_summary["status"])
        self.assertEqual("cancelled", cancelled_summary["outcome"])
        self.assertFalse(cancelled_summary["success"])
        self.assertTrue(cancelled_summary["partial_failure"])

    def test_budget_exhaustion_with_no_success_normalizes_to_failed(self) -> None:
        meta = {
            "team_id": "team_budget_fail", "title": "fixture", "provider": "opencode",
            "access_mode": "read_only", "permission_profile": "trusted", "scope": None,
            "created_at": time.time(), "updated_at": time.time(), "agent_ids": [],
            "scheduler_version": 1, "max_parallel": 2, "max_revisions": 0,
            "retries": 0, "team_retry_count": 0, "team_retry_reservations": [],
            "cancelled": False, "budget_exhausted_reason": "team_deadline",
            "tasks": [
                {"id": "a", "state": "skipped", "failure_reason": "budget_exhausted:team_deadline"},
                {"id": "b", "state": "skipped", "failure_reason": "budget_exhausted:team_deadline"},
            ],
        }
        summary = agents._team_summary("team_budget_fail", meta)
        self.assertEqual("budget_exhausted", summary["status"])
        self.assertEqual("failed", summary["outcome"])
        self.assertFalse(summary["partial_failure"])
        self.assertEqual(0, summary["successful_count"])
        self.assertEqual(2, summary["failure_count"])


if __name__ == "__main__":
    unittest.main()
