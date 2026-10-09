from __future__ import annotations

import unittest

from mcp_server import tools_agents as agents
from mcp_server.agent_results import normalize_result_envelope, reduce_task_results
from tests.test_agent_team_outcome import TeamFixture


def envelope(claims=(), *, outcome="success", errors=(), resolutions=None) -> dict:
    raw = {
        "schema_version": 1, "outcome": outcome, "summary": "s", "confidence": 0.8,
        "claims": [{"id": f"c{i}", "statement": "x", "key": key, "value": value} for i, (key, value) in enumerate(claims, 1)],
        "errors": list(errors),
    }
    if resolutions is not None:
        raw["resolutions"] = resolutions
    return normalize_result_envelope(raw)


class EnvelopeAndReducerTests(unittest.TestCase):
    def test_success_with_declared_errors_is_partial_failure(self) -> None:
        row = envelope(errors=[{"code": "test_failed", "message": "one test failed"}])
        self.assertEqual("partial_failure", row["outcome"])
        self.assertIn("success to partial_failure", row["warnings"][-1])

    def test_unresolved_conflict_is_prominent_but_advisory_keeps_success(self) -> None:
        reduced = reduce_task_results([("a", envelope([("port", 8765)])), ("b", envelope([("port", 8000)]))])
        self.assertEqual("success", reduced["outcome"])
        self.assertEqual(1, reduced["unresolved_conflict_count"])
        self.assertTrue(reduced["warnings"][0].startswith("UNRESOLVED: 1 claim key"))
        sources = {tuple(row["source_task_ids"]) for row in reduced["contradictions"][0]["values"]}
        self.assertEqual({("a",), ("b",)}, sources)

    def test_review_and_fail_policies_withhold_success(self) -> None:
        rows = [("a", envelope([("port", 8765)])), ("b", envelope([("port", 8000)]))]
        for policy in ("review", "fail"):
            reduced = reduce_task_results(rows, conflict_policy=policy)
            self.assertEqual("partial_failure", reduced["outcome"], policy)
            self.assertEqual("unresolved_conflict", reduced["errors"][-1]["code"])

    def test_a_reviewer_resolution_settles_the_conflict_with_attribution(self) -> None:
        reviewer = envelope(resolutions=[{"key": "port", "accepted_value": 8765, "reason": "README says 8765"}])
        reduced = reduce_task_results(
            [("a", envelope([("port", 8765)])), ("b", envelope([("port", 8000)])), ("review", reviewer)],
            conflict_policy="review",
        )
        self.assertEqual("success", reduced["outcome"])
        self.assertEqual(0, reduced["unresolved_conflict_count"])
        conflict = reduced["contradictions"][0]
        self.assertTrue(conflict["resolved"])
        self.assertEqual({"key": "port", "accepted_value": 8765, "reason": "README says 8765",
                          "resolved_by_task_id": "review"}, conflict["resolution"])


class TeamConflictOutcomeTests(TeamFixture, unittest.TestCase):
    def conflicting_team(self, policy: str) -> str:
        team_id = f"team_conflict_{policy}"
        a = self.agent(f"agt_a_{policy}", "completed", team_id=team_id, task_id="a")
        b = self.agent(f"agt_b_{policy}", "completed", team_id=team_id, task_id="b")
        agents._write_result_envelope(a, envelope([("answer", "yes")]))
        agents._write_result_envelope(b, envelope([("answer", "no")]))
        self.team(team_id, [
            {"id": "a", "state": "completed", "failure_reason": None, "agent_ids": [a], "latest_agent_id": a},
            {"id": "b", "state": "completed", "failure_reason": None, "agent_ids": [b], "latest_agent_id": b},
        ], [a, b])
        meta = agents._read_team(team_id)
        meta["conflict_policy"] = policy
        agents._write_team(team_id, meta)
        return team_id

    def test_review_policy_withholds_team_success_in_wait_agents(self) -> None:
        result = agents.wait_agents(None, team_id=self.conflicting_team("review"), mode="all", timeout_s=0)
        self.assertFalse(result["success"])
        self.assertEqual("partial_failure", result["outcome"])
        self.assertEqual(1, result["unresolved_conflict_count"])
        self.assertIn("unresolved_conflict", [row.get("reason") for row in result["failure_reasons"]])

    def test_advisory_policy_keeps_success_and_reports_the_conflict(self) -> None:
        result = agents.wait_agents(None, team_id=self.conflicting_team("advisory"), mode="all", timeout_s=0)
        self.assertTrue(result["success"])
        self.assertEqual(1, result["unresolved_conflict_count"])
        self.assertEqual("advisory", result["conflict_policy"])


if __name__ == "__main__":
    unittest.main()
