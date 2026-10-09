from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import audit_chain
from mcp_server.observability import TelemetryManager


class AuditChainTests(unittest.TestCase):
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

    def test_an_untouched_chain_verifies(self) -> None:
        self.record(5)
        report = self.telemetry.verify_security_chain()
        self.assertTrue(report["ok"], report)
        self.assertEqual(5, report["checked"])
        self.assertEqual(5, report["latest_seq"])

    def test_edits_deletions_and_reordering_are_detected(self) -> None:
        self.record(6)
        self.sql("UPDATE security_events SET decision = 'allow' WHERE chain_seq = 3")
        self.assertEqual(("modified_event", 3), self.problem())
        self.sql("UPDATE security_events SET decision = 'deny' WHERE chain_seq = 3")
        self.assertTrue(self.telemetry.verify_security_chain()["ok"])
        self.sql("DELETE FROM security_events WHERE chain_seq = 4")
        self.assertEqual(("missing_events", 4), self.problem())

    def test_swapping_two_events_breaks_the_links(self) -> None:
        self.record(4)
        self.sql("UPDATE security_events SET chain_seq = -2 WHERE chain_seq = 2")
        self.sql("UPDATE security_events SET chain_seq = 2 WHERE chain_seq = 3")
        self.sql("UPDATE security_events SET chain_seq = 3 WHERE chain_seq = -2")
        self.assertFalse(self.telemetry.verify_security_chain()["ok"])

    def test_cutting_off_recent_events_is_caught_by_the_checkpoint(self) -> None:
        with patch.object(audit_chain, "CHECKPOINT_EVERY", 3):
            self.record(7)
        self.assertTrue(audit_chain.checkpoint_path(self.db).exists())
        self.sql("DELETE FROM security_events WHERE chain_seq >= 6")
        self.assertEqual(("truncated_after_checkpoint", 6), self.problem())

    def test_retention_keeps_the_rest_verifiable(self) -> None:
        self.record(10)
        with sqlite3.connect(self.db) as conn:
            removed = audit_chain.prune(conn, cutoff=0.0, max_events=4)
        self.assertEqual(6, removed)
        report = self.telemetry.verify_security_chain()
        self.assertTrue(report["ok"], report)
        self.assertEqual(6, report["anchor_seq"])
        self.assertEqual(4, report["checked"])
        self.record(2, start=10)
        self.assertTrue(self.telemetry.verify_security_chain()["ok"])

    def test_concurrent_writers_produce_one_unbroken_chain(self) -> None:
        threads = [threading.Thread(target=self.record, args=(10, i * 10)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        report = self.telemetry.verify_security_chain()
        self.assertTrue(report["ok"], report)
        self.assertEqual(40, report["checked"])

    def problem(self):
        report = self.telemetry.verify_security_chain()
        self.assertFalse(report["ok"])
        return report["problem"], report["seq"]


if __name__ == "__main__":
    unittest.main()
