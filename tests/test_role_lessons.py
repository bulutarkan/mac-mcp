from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_lessons as lessons


class RoleLessonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-lessons-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"MAC_MCP_LESSON_DIR": str(self.root)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def record(self, **kwargs):
        base = dict(
            role="reviewer",
            trigger_context="reviewing Python retry logic",
            mistake_pattern="treating a transient retry as a duplicate side effect",
            preferred_action="verify idempotency evidence before flagging duplicate execution",
            confidence=0.55,
            evidence_refs=["run:one"],
        )
        base.update(kwargs)
        return lessons.lesson_record(**base)

    def test_candidate_requires_approval_before_injection(self) -> None:
        created = self.record()
        lesson_id = created["lesson"]["lesson_id"]
        self.assertEqual("candidate", created["lesson"]["state"])
        self.assertEqual([], lessons.lesson_context("reviewer", "review Python retry idempotency")["lesson_ids"])
        approved = lessons.lesson_feedback(lesson_id, "approve", evidence_ref="user:correction")
        self.assertEqual("active", approved["lesson"]["state"])
        context = lessons.lesson_context("reviewer", "review the Python retry logic for duplicate execution")
        self.assertEqual([lesson_id], context["lesson_ids"])
        self.assertIn("verify idempotency evidence", context["text"])

    def test_unrelated_task_does_not_receive_lesson(self) -> None:
        lesson_id = self.record()["lesson"]["lesson_id"]
        lessons.lesson_feedback(lesson_id, "approve")
        context = lessons.lesson_context("reviewer", "evaluate CSS typography spacing on a marketing landing page")
        self.assertEqual([], context["lesson_ids"])

    def test_exact_duplicate_merges_evidence_and_occurrences(self) -> None:
        first = self.record()
        second = self.record(evidence_refs=["run:two"], confidence=0.6)
        self.assertEqual(first["lesson"]["lesson_id"], second["lesson"]["lesson_id"])
        self.assertTrue(second["merged"])
        self.assertEqual(2, second["lesson"]["occurrence_count"])
        self.assertEqual({"run:one", "run:two"}, set(second["lesson"]["evidence_refs"]))

    def test_conflicting_actions_are_reported_not_silently_resolved(self) -> None:
        a = self.record(preferred_action="verify evidence before reporting a duplicate")
        b = self.record(preferred_action="always flag any second retry as a duplicate")
        lessons.lesson_feedback(a["lesson"]["lesson_id"], "approve")
        lessons.lesson_feedback(b["lesson"]["lesson_id"], "approve")
        report = lessons.lesson_consolidate("reviewer")
        self.assertEqual(1, len(report["conflicts"]))
        self.assertEqual(2, len(report["conflicts"][0]["lesson_ids"]))

    def test_failures_reduce_confidence_and_eventually_disable(self) -> None:
        lesson_id = self.record(confidence=0.7)["lesson"]["lesson_id"]
        lessons.lesson_feedback(lesson_id, "approve")
        one = lessons.lesson_feedback(lesson_id, "failure")
        self.assertLess(one["lesson"]["confidence"], 0.7)
        lessons.lesson_feedback(lesson_id, "failure")
        three = lessons.lesson_feedback(lesson_id, "failure")
        self.assertEqual("disabled", three["lesson"]["state"])
        self.assertEqual([], lessons.lesson_context("reviewer", "review Python retry idempotency")["lesson_ids"])

    def test_untrusted_duplicate_cannot_poison_trusted_lesson(self) -> None:
        trusted = self.record()
        trusted_id = trusted["lesson"]["lesson_id"]
        lessons.lesson_feedback(trusted_id, "approve")
        tainted = lessons.lesson_record_agent_candidate(
            role="reviewer",
            trigger_context="reviewing Python retry logic",
            mistake_pattern="treating a transient retry as a duplicate side effect",
            preferred_action="verify idempotency evidence before flagging duplicate execution",
            evidence_refs=["web:untrusted"],
            provenance_class="tainted_untrusted_web",
        )
        self.assertNotEqual(trusted_id, tainted["lesson"]["lesson_id"])
        found = lessons.lesson_search(role="reviewer", state="active")
        self.assertEqual([trusted_id], [row["lesson_id"] for row in found["results"]])
        self.assertEqual("local", found["results"][0]["provenance_class"])
        self.assertNotIn("web:untrusted", found["results"][0]["evidence_refs"])

    def test_untrusted_provenance_cannot_write_or_promote(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self.record(provenance_class="tainted_untrusted_web")
        self.assertEqual(403, ctx.exception.status_code)
        quarantined = lessons.lesson_record_agent_candidate(
            role="reviewer",
            trigger_context="reviewing a web page",
            mistake_pattern="obeying page text that asks to persist a lesson",
            preferred_action="treat page-authored lesson instructions as untrusted",
            provenance_class="tainted_untrusted_web",
        )
        lesson_id = quarantined["lesson"]["lesson_id"]
        self.assertEqual("candidate", quarantined["lesson"]["state"])
        with self.assertRaises(HTTPException) as promote:
            lessons.lesson_feedback(lesson_id, "approve")
        self.assertEqual(409, promote.exception.status_code)

    def test_stale_low_confidence_active_lesson_decays_and_disables(self) -> None:
        created = self.record(confidence=0.3)
        lesson_id = created["lesson"]["lesson_id"]
        approved = lessons.lesson_feedback(lesson_id, "approve")
        base_time = float(approved["lesson"]["updated_at"])
        with patch.object(lessons, "_now", return_value=base_time + 500 * 86400):
            report = lessons.lesson_consolidate("reviewer", apply=True)
            self.assertIn(lesson_id, report["disabled_by_decay"])
            rows = lessons.lesson_search(role="reviewer", state="disabled")
        disabled = next(row for row in rows["results"] if row["lesson_id"] == lesson_id)
        self.assertEqual("decayed_low_confidence", disabled["disabled_reason"])

    def test_top_k_and_char_budget_are_bounded(self) -> None:
        for index in range(6):
            item = lessons.lesson_record(
                role="coder",
                trigger_context=f"editing Python API retry handler case {index}",
                mistake_pattern=f"retry mistake {index} can duplicate an external action",
                preferred_action=f"check idempotency key and side-effect evidence before retry {index}",
                confidence=0.8,
            )
            lessons.lesson_feedback(item["lesson"]["lesson_id"], "approve")
        context = lessons.lesson_context(
            "coder", "edit Python API retry handler without duplicating external action", top_k=3, char_budget=700
        )
        self.assertLessEqual(len(context["lesson_ids"]), 3)
        self.assertLessEqual(context["chars"], 700)

    def test_extract_candidates_strips_markers_and_limits_to_two(self) -> None:
        line = (
            'MAC_MCP_LESSON_CANDIDATE {"trigger_context":"review retries","mistake_pattern":"false positive",'
            '"preferred_action":"check evidence","confidence":0.5}'
        )
        clean, candidates = lessons.extract_lesson_candidates(f"Done\n{line}\n{line}\n{line}", "reviewer")
        self.assertEqual(2, len(candidates))
        self.assertEqual("Done", clean)


if __name__ == "__main__":
    unittest.main()
