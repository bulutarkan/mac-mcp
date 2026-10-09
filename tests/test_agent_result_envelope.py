from __future__ import annotations

import json
import unittest

from mcp_server.agent_results import (
    RESULT_ENVELOPE_MARKER,
    ResultContractError,
    apply_evidence_policy,
    bound_result_envelope,
    evidence_policy_for,
    normalize_result_envelope,
    parse_provider_result,
    reduce_task_results,
)


def marked(payload: dict) -> str:
    return (
        "human preface\n"
        + RESULT_ENVELOPE_MARKER
        + "\n```json\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```"
    )


class AgentResultEnvelopeTests(unittest.TestCase):
    def test_valid_envelope_normalizes_and_preserves_provenance(self) -> None:
        payload = {
            "schema_version": 1,
            "outcome": "success",
            "summary": "Verified result",
            "claims": [{"id": "c1", "statement": "A is true", "key": "answer", "value": 42}],
            "evidence": [{"id": "e1", "ref": "file:report", "summary": "Report", "claim_ids": ["c1"]}],
            "artifacts": [{"id": "a1", "ref": "/tmp/report.txt", "description": "Report"}],
            "warnings": [],
            "confidence": 0.9,
            "errors": [],
            "provenance": {"provider_note": "kept"},
        }
        env, meta = parse_provider_result(
            marked(payload),
            provenance={"agent_id": "agent-1", "task_id": "task-a", "provider": "codex"},
        )
        self.assertTrue(meta["valid"])
        self.assertEqual("valid", env["contract_status"])
        self.assertEqual("Verified result", env["summary"])
        self.assertEqual("agent-1", env["provenance"]["agent_id"])
        self.assertEqual("kept", env["provenance"]["provider_note"])
        self.assertEqual("/tmp/report.txt", env["artifacts"][0]["ref"])

    def test_invalid_marked_envelope_fails_closed(self) -> None:
        payload = {
            "schema_version": 1,
            "outcome": "success",
            "summary": "",
            "claims": [],
            "evidence": [],
            "artifacts": [],
            "warnings": [],
            "confidence": 0.5,
            "errors": [],
        }
        with self.assertRaises(ResultContractError) as ctx:
            parse_provider_result(marked(payload), provenance={"agent_id": "agent-1"})
        self.assertEqual("missing_summary", ctx.exception.code)

    def test_duplicate_ids_and_dangling_claim_references_fail_validation(self) -> None:
        base = {
            "schema_version": 1, "outcome": "success", "summary": "done",
            "claims": [{"id": "c1", "statement": "a"}, {"id": "c2", "statement": "b"}],
            "evidence": [{"id": "e1", "ref": "test:1", "claim_ids": ["c1"]}],
            "artifacts": [], "warnings": [], "confidence": 0.8, "errors": [],
        }
        envelope = normalize_result_envelope(base)
        self.assertEqual(
            {"claim_count": 2, "evidence_count": 1, "unsupported_claim_count": 1, "unsupported_claim_ids": ["c2"]},
            envelope["evidence_coverage"],
        )
        cases = {
            "duplicate_id": [
                {**base, "claims": [{"id": "c1", "statement": "a"}, {"id": "c1", "statement": "b"}]},
                {**base, "evidence": [{"id": "e1", "ref": "x"}, {"id": "e1", "ref": "y"}]},
                {**base, "artifacts": [{"id": "a1", "ref": "p"}, {"id": "a1", "ref": "q"}]},
            ],
            "dangling_claim_reference": [
                {**base, "evidence": [{"id": "e1", "ref": "test:1", "claim_ids": ["c9"]}]},
            ],
        }
        for code, payloads in cases.items():
            for payload in payloads:
                with self.subTest(code=code), self.assertRaises(ResultContractError) as ctx:
                    normalize_result_envelope(payload)
                self.assertEqual(code, ctx.exception.code)

    def test_envelope_closed_by_a_second_marker_still_parses(self) -> None:
        payload = {
            "schema_version": 1, "outcome": "partial_failure", "summary": "tab missing",
            "claims": [], "evidence": [], "artifacts": [], "warnings": [], "confidence": 0.9, "errors": [],
        }
        text = f"{RESULT_ENVELOPE_MARKER} {json.dumps(payload)} {RESULT_ENVELOPE_MARKER}"
        envelope, meta = parse_provider_result(text)
        self.assertEqual("valid", meta["contract_status"])
        self.assertEqual("partial_failure", envelope["outcome"])
        with self.assertRaises(ResultContractError):
            parse_provider_result(f"{RESULT_ENVELOPE_MARKER} {{\"schema_version\": 1, broken {RESULT_ENVELOPE_MARKER}")

    def test_write_task_success_without_evidence_is_downgraded(self) -> None:
        self.assertEqual("required", evidence_policy_for("workspace_write"))
        self.assertEqual("required", evidence_policy_for("full"))
        self.assertEqual("required", evidence_policy_for("read_only", integration_required=True))
        self.assertEqual("optional", evidence_policy_for("read_only"))
        base = {"schema_version": 1, "outcome": "success", "summary": "Changed the parser.",
                "claims": [{"id": "c1", "statement": "Parser handles the new form."}]}
        envelope, _ = parse_provider_result(marked(base))
        apply_evidence_policy(envelope, "required")
        self.assertEqual("partial_failure", envelope["outcome"])
        self.assertEqual("required", envelope["evidence_policy"])
        self.assertEqual(["evidence_required"], [row["code"] for row in envelope["errors"]])

        with_evidence = dict(base, evidence=[{"id": "e1", "ref": "unittest", "summary": "42 tests OK",
                                              "claim_ids": ["c1"]}])
        envelope, _ = parse_provider_result(marked(with_evidence))
        apply_evidence_policy(envelope, "required")
        self.assertEqual("success", envelope["outcome"])
        self.assertEqual([], envelope["errors"])

    def test_read_only_success_without_claims_or_evidence_stays_valid(self) -> None:
        envelope, meta = parse_provider_result(marked(
            {"schema_version": 1, "outcome": "success", "summary": "The page title is Example."}
        ))
        apply_evidence_policy(envelope, evidence_policy_for("read_only"))
        self.assertTrue(meta["valid"])
        self.assertEqual("success", envelope["outcome"])
        self.assertEqual("optional", envelope["evidence_policy"])
        self.assertEqual(0, envelope["evidence_coverage"]["evidence_count"])

    def test_legacy_text_handoff_is_not_held_to_the_evidence_policy(self) -> None:
        envelope, _ = parse_provider_result("Done, updated the file.")
        apply_evidence_policy(envelope, "required")
        self.assertEqual("legacy_fallback", envelope["contract_status"])
        self.assertNotIn("evidence_required", [row.get("code") for row in envelope["errors"]])

    def test_legacy_provider_fallback_is_explicit_and_versioned(self) -> None:
        env, meta = parse_provider_result(
            "Legacy final answer",
            provenance={"agent_id": "legacy-1", "provider": "opencode"},
        )
        self.assertEqual(1, env["schema_version"])
        self.assertEqual("legacy_fallback", env["contract_status"])
        self.assertEqual("Legacy final answer", env["summary"])
        self.assertFalse(meta["marker_present"])
        self.assertTrue(any("Legacy provider fallback" in w for w in env["warnings"]))

    def test_duplicate_evidence_merges_without_losing_task_provenance(self) -> None:
        base = {
            "schema_version": 1,
            "contract_status": "valid",
            "outcome": "success",
            "summary": "ok",
            "claims": [],
            "evidence": [{"id": "e1", "ref": "url:https://example.test/doc", "summary": "Doc", "claim_ids": []}],
            "artifacts": [],
            "warnings": [],
            "confidence": 0.8,
            "errors": [],
            "provenance": {},
            "quality_gate": None,
            "truncation": {"truncated": False, "omitted": {}},
        }
        reduced = reduce_task_results([("a", base), ("b", base)], team_id="team-1")
        self.assertEqual(1, len(reduced["evidence"]))
        self.assertEqual(["a", "b"], reduced["evidence"][0]["source_task_ids"])
        self.assertEqual("team-1", reduced["provenance"]["team_id"])

    def test_conflicting_claims_are_flagged_deterministically(self) -> None:
        def env(task_value: str) -> dict:
            return {
                "schema_version": 1,
                "contract_status": "valid",
                "outcome": "success",
                "summary": task_value,
                "claims": [{"id": "c1", "statement": f"answer={task_value}", "key": "answer", "value": task_value}],
                "evidence": [],
                "artifacts": [],
                "warnings": [],
                "confidence": 0.7,
                "errors": [],
                "provenance": {},
                "quality_gate": None,
                "truncation": {"truncated": False, "omitted": {}},
            }
        reduced = reduce_task_results([("a", env("yes")), ("b", env("no"))])
        self.assertEqual(1, len(reduced["contradictions"]))
        self.assertEqual("answer", reduced["contradictions"][0]["key"])
        self.assertTrue(any("conflicting claim" in w for w in reduced["warnings"]))

    def test_partial_child_failure_is_explicit(self) -> None:
        ok = {
            "schema_version": 1, "contract_status": "valid", "outcome": "success", "summary": "ok",
            "claims": [], "evidence": [], "artifacts": [], "warnings": [], "confidence": 0.8,
            "errors": [], "provenance": {}, "quality_gate": None, "truncation": {"truncated": False, "omitted": {}},
        }
        failed = {
            **ok,
            "outcome": "failure",
            "summary": "failed",
            "errors": [{"code": "child_failed", "message": "boom"}],
        }
        reduced = reduce_task_results([("a", ok), ("b", failed)])
        self.assertEqual("partial_failure", reduced["outcome"])
        self.assertEqual("b", reduced["errors"][0]["source_task_id"])

    def test_large_result_has_explicit_truncation_metadata(self) -> None:
        env = {
            "schema_version": 1, "contract_status": "valid", "outcome": "success",
            "summary": "S" * 2000,
            "claims": [{"id": f"c{i}", "statement": "claim " + ("x" * 300)} for i in range(20)],
            "evidence": [{"id": f"e{i}", "ref": f"ref:{i}", "summary": "e" * 200, "claim_ids": []} for i in range(20)],
            "artifacts": [{"id": f"a{i}", "ref": f"/tmp/{i}", "description": "a" * 100} for i in range(10)],
            "warnings": ["w" * 200 for _ in range(10)],
            "confidence": 0.9, "errors": [], "provenance": {}, "quality_gate": None,
            "truncation": {"truncated": False, "omitted": {}},
        }
        bounded = bound_result_envelope(env, 1800)
        self.assertTrue(bounded["truncation"]["truncated"])
        self.assertTrue(bounded["truncation"]["omitted"])
        self.assertGreater(bounded["truncation"]["original_chars"], bounded["truncation"]["returned_chars"])
        self.assertLessEqual(bounded["truncation"]["returned_chars"], 1800)

    def test_artifact_references_survive_full_fan_in(self) -> None:
        env = {
            "schema_version": 1, "contract_status": "valid", "outcome": "success", "summary": "artifact",
            "claims": [], "evidence": [],
            "artifacts": [{"id": "a1", "ref": "/tmp/output.csv", "description": "CSV", "sha256": "abc"}],
            "warnings": [], "confidence": 1.0, "errors": [], "provenance": {},
            "quality_gate": None, "truncation": {"truncated": False, "omitted": {}},
        }
        reduced = reduce_task_results([("task-x", env)])
        self.assertEqual("/tmp/output.csv", reduced["artifacts"][0]["ref"])
        self.assertEqual(["task-x"], reduced["artifacts"][0]["source_task_ids"])

    def test_reviewer_typed_outcome_is_preserved(self) -> None:
        payload = {
            "schema_version": 1,
            "outcome": "success",
            "summary": "Review passed",
            "claims": [],
            "evidence": [],
            "artifacts": [],
            "warnings": [],
            "confidence": 0.95,
            "errors": [],
            "quality_gate": {"decision": "pass", "feedback": "Looks good"},
        }
        env, _ = parse_provider_result(marked(payload), provenance={"agent_id": "reviewer"})
        self.assertEqual("pass", env["quality_gate"]["decision"])
        self.assertEqual("Looks good", env["quality_gate"]["feedback"])


if __name__ == "__main__":
    unittest.main()
