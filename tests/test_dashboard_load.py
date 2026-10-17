"""#254: an open dashboard costs the server little. The summary is recomputed at most every few
seconds and its cache is bounded; the security chain is re-walked whole only now and then, while
checks in between cover new events and still catch any edit or delete at once; the dashboard
refreshes the attention card from stream events and polls it often only while work runs."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import audit_chain
from mcp_server import observability
from mcp_server.observability import TelemetryManager

ROOT = Path(__file__).resolve().parents[1]


class ChainCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.db = Path(self.td.name) / "telemetry.sqlite3"
        self.telemetry = TelemetryManager(db_path=self.db, usage_enabled=False)

    def record(self, n: int, start: int = 0) -> None:
        for i in range(start, start + n):
            self.telemetry.record_security_event(
                session_id="s", event_type="POLICY_DENY", tool="run_command", tool_class="terminal",
                origin=None, decision="deny", reason_code=f"r{i}", profile="trusted", actor="owner",
                agent_id=None, target_summary=f"target {i}",
            )

    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(statement, args)

    def walked(self) -> list:
        calls = []
        real = audit_chain._walk

        def spy(conn, db_path, anchor_seq, after_seq, expected_prev):
            calls.append(after_seq)
            return real(conn, db_path, anchor_seq, after_seq, expected_prev)
        patcher = patch.object(audit_chain, "_walk", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_checks_between_full_walks_read_only_new_events(self) -> None:
        self.record(30)
        walked = self.walked()
        first = self.telemetry.verify_security_chain()
        self.assertEqual(("full", 30, True), (first["mode"], first["checked"], first["ok"]))
        self.record(5, start=30)
        second = self.telemetry.verify_security_chain()
        self.assertEqual(("incremental", 35, 35, True), (second["mode"], second["checked"], second["latest_seq"],
                                                          second["ok"]))
        self.assertEqual([0, 30], walked)  # the second check started after the 30 already verified
        self.assertNotIn("latest_hash", second)

    def test_an_edit_or_delete_after_a_check_is_still_caught_at_once(self) -> None:
        self.record(10)
        self.assertTrue(self.telemetry.verify_security_chain()["ok"])
        self.sql("UPDATE security_events SET target_summary = 'changed' WHERE chain_seq = 4")
        report = self.telemetry.verify_security_chain()
        self.assertEqual(("full", False, "modified_event", 4), (report["mode"], report["ok"], report["problem"],
                                                                report["seq"]))
        self.sql("UPDATE security_events SET target_summary = 'target 3' WHERE chain_seq = 4")
        self.assertTrue(self.telemetry.verify_security_chain()["ok"])
        self.sql("DELETE FROM security_events WHERE chain_seq = 7")
        report = self.telemetry.verify_security_chain()
        self.assertEqual(("missing_events", 7), (report["problem"], report["seq"]))

    def test_removing_the_change_triggers_forces_a_full_walk_and_restores_them(self) -> None:
        self.record(6)
        self.telemetry.verify_security_chain()
        self.sql("DROP TRIGGER security_events_edited")
        self.sql("UPDATE security_events SET decision = 'allow' WHERE chain_seq = 2")
        report = self.telemetry.verify_security_chain()
        self.assertEqual(("full", "modified_event"), (report["mode"], report["problem"]))
        with sqlite3.connect(self.db) as conn:
            self.assertIsNotNone(audit_chain.change_mark(conn))

    def test_pruning_and_the_full_interval_bring_back_a_full_walk(self) -> None:
        self.record(10)
        self.telemetry.verify_security_chain()
        with sqlite3.connect(self.db) as conn:
            audit_chain.prune(conn, cutoff=0.0, max_events=4)
        report = self.telemetry.verify_security_chain()
        self.assertEqual(("full", True, 4), (report["mode"], report["ok"], report["checked"]))
        self.assertEqual("incremental", self.telemetry.verify_security_chain()["mode"])
        with patch.object(observability, "CHAIN_FULL_VERIFY_S", 0.0):
            self.assertEqual("full", self.telemetry.verify_security_chain()["mode"])
        self.assertEqual("full", self.telemetry.verify_security_chain(full=True)["mode"])

    def test_a_checkpoint_cut_is_caught_between_full_walks(self) -> None:
        with patch.object(audit_chain, "CHECKPOINT_EVERY", 3):
            self.record(7)
            self.assertTrue(self.telemetry.verify_security_chain()["ok"])
            self.record(3, start=7)
        self.sql("DELETE FROM security_events WHERE chain_seq >= 9")
        report = self.telemetry.verify_security_chain()
        self.assertFalse(report["ok"])
        self.assertIn(report["problem"], {"truncated_after_checkpoint", "missing_events"})

    def test_a_long_chain_check_between_walks_is_quick(self) -> None:
        with sqlite3.connect(self.db) as conn:
            prev, rows = audit_chain.GENESIS_HASH, []
            for seq in range(1, 20_001):
                event = {"event_id": f"sec_{seq}", "timestamp": 1.7e9 + seq, "event_type": "POLICY_DENY",
                         "decision": "deny", "reason_code": "x"}
                digest = audit_chain.record_hash(event, seq, prev)
                rows.append([event.get(f) for f in audit_chain.HASHED_FIELDS] + [seq, prev, digest])
                prev = digest
            fields = list(audit_chain.HASHED_FIELDS) + ["chain_seq", "prev_hash", "record_hash"]
            conn.executemany(f"INSERT INTO security_events ({','.join(fields)}) VALUES "
                             f"({','.join('?' * len(fields))})", rows)
        started = time.perf_counter()
        self.assertEqual(20_000, self.telemetry.verify_security_chain()["checked"])
        full = time.perf_counter() - started
        started = time.perf_counter()
        self.assertEqual("incremental", self.telemetry.verify_security_chain()["mode"])
        between = time.perf_counter() - started
        self.assertLess(between, full / 5)


class SummaryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.telemetry = TelemetryManager(db_path=Path(self.td.name) / "t.sqlite3", usage_enabled=False)

    def call(self, ms: int) -> None:
        self.n = getattr(self, "n", 0) + 1
        now = time.time()
        self.telemetry._insert_event({"event_id": f"evt_{self.n}", "timestamp": now, "source": "mcp",
                                      "tool": "read_file", "status": "success", "started_at": now - ms / 1000,
                                      "ended_at": now, "duration_ms": ms})

    def test_a_busy_server_recomputes_the_summary_at_most_every_few_seconds(self) -> None:
        self.call(10)
        first = self.telemetry.summary(24)
        self.call(20)
        self.assertEqual(first["total_calls"], self.telemetry.summary(24)["total_calls"])
        with patch.object(observability, "SUMMARY_MIN_REFRESH_S", 0.0):
            self.assertEqual(2, self.telemetry.summary(24)["total_calls"])

    def test_the_cache_keeps_a_few_windows_only(self) -> None:
        for hours in range(1, 40):
            self.telemetry.summary(hours)
        self.assertEqual(observability.SUMMARY_CACHE_MAX, len(self.telemetry._summary_cache))

    def test_p95_is_the_nearest_rank_duration(self) -> None:
        for ms in range(1, 21):
            self.call(ms * 10)
        self.assertEqual(190, self.telemetry.summary(1)["p95_duration_ms"])


class DashboardPollingTests(unittest.TestCase):
    def test_attention_and_summary_follow_the_stream_instead_of_fixed_polls(self) -> None:
        source = (ROOT / "mcp_server" / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn("refreshAttention(); }, 4000)", source)
        self.assertIn("pollAdaptive(refreshAttention, 4000, 30000);", source)
        finished = source[source.index('event.kind === "call_finished"'):]
        finished = finished[:finished.index("\n    }\n")]
        self.assertIn("scheduleSummaryRefresh();", finished)
        self.assertNotIn("refreshSummary();", finished)
        self.assertIn('event.kind === "security_event"', source)


if __name__ == "__main__":
    unittest.main()
