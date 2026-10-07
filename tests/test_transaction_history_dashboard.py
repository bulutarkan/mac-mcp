from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

import mcp_server.file_transactions as journal
import mcp_server.tools_files as files
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings


TOKEN = "transaction-dashboard-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class TransactionHistoryDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-transaction-dashboard-")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.work = self.root / "private-patient-project"
        self.work.mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_STATE_DIR": str(self.state),
                "MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS": "64",
                "MAC_MCP_FILE_JOURNAL_MAX_BYTES": str(64 * 1024 * 1024),
                "MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES": str(16 * 1024 * 1024),
            },
            clear=False,
        )
        self.env.start()
        telemetry = TelemetryManager(
            db_path=self.root / "telemetry.sqlite3",
            max_events=100,
        )
        self.telemetry = telemetry
        self.app = Starlette(
            routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_recent_history_is_sanitized_and_paginates(self) -> None:
        first = self.work / "patient-secret-name.txt"
        second = self.work / "insurance-secret.txt"
        first.write_text("before", encoding="utf-8")
        second.write_text("before", encoding="utf-8")
        files.write_file(None, str(first), "after")
        files.write_file(None, str(second), "after")

        page = journal.recent_transactions(limit=1, offset=0)
        self.assertEqual(2, page["total"])
        self.assertEqual(1, len(page["transactions"]))
        self.assertEqual(1, page["next_offset"])
        item = page["transactions"][0]
        self.assertTrue(item["can_undo"])
        self.assertEqual("filesystem", item["operation_class"])
        self.assertEqual("1 filesystem target", item["target_summary"])
        self.assertNotIn("path", item)
        serialized = repr(page)
        self.assertNotIn("patient-secret-name", serialized)
        self.assertNotIn("insurance-secret", serialized)
        self.assertNotIn(str(self.root), serialized)

        second_page = journal.recent_transactions(limit=1, offset=1)
        self.assertEqual(1, len(second_page["transactions"]))
        self.assertIsNone(second_page["next_offset"])

    def test_compound_children_are_hidden_and_partial_state_is_truthful(self) -> None:
        one = self.work / "one.txt"
        two = self.work / "two.txt"
        one.write_text("one-before", encoding="utf-8")
        two.write_text("two-before", encoding="utf-8")
        first = files.write_file(None, str(one), "one-after")

        capture = journal.begin_capture_transaction(
            "run_command",
            metadata={"secret": "must-not-leak"},
        )
        before = journal.path_revision(two)
        journal.capture_transaction_snapshot(
            capture["transaction_id"],
            two,
            before_kind="file",
            before_source=two,
            before_fingerprint=before,
        )
        two.write_text("two-after", encoding="utf-8")
        second = journal.finalize_capture_transaction(
            capture["transaction_id"],
            reversibility="partial",
            unsupported=[{"reason": "network_side_effect", "secret": "hidden"}],
        )
        parent = journal.compose_transactions(
            "run_command",
            [first["transaction_id"], second["transaction_id"]],
        )

        history = journal.recent_transactions(limit=10)
        self.assertEqual(1, history["total"])
        item = history["transactions"][0]
        self.assertEqual(parent["transaction_id"], item["transaction_id"])
        self.assertEqual("compound", item["operation_class"])
        self.assertEqual("partial", item["reversibility"])
        self.assertEqual(1, item["unsupported_count"])
        self.assertEqual(2, item["child_count"])
        self.assertTrue(item["can_undo"])
        serialized = repr(history)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("network_side_effect", serialized)
        self.assertNotIn("hidden", serialized)

    def test_newer_change_disables_undo_and_route_fails_closed(self) -> None:
        target = self.work / "conflict.txt"
        target.write_text("before", encoding="utf-8")
        result = files.write_file(None, str(target), "agent-change")
        target.write_text("newer-human-change", encoding="utf-8")

        item = journal.recent_transactions(limit=10)["transactions"][0]
        self.assertFalse(item["can_undo"])
        self.assertEqual("newer_change_conflict", item["undo_blocked_reason"])

        response = self.client.post(
            "/dashboard/api/transactions/undo",
            headers=AUTH,
            json={"transaction_id": result["transaction_id"], "confirm": True, "force": True},
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("transaction_conflict", response.json()["error"])
        self.assertEqual("newer-human-change", target.read_text(encoding="utf-8"))

    def test_dashboard_undo_requires_auth_and_explicit_confirmation(self) -> None:
        target = self.work / "safe.txt"
        target.write_text("before", encoding="utf-8")
        result = files.write_file(None, str(target), "after")

        self.assertEqual(
            401,
            self.client.get("/dashboard/api/transactions").status_code,
        )
        missing_confirm = self.client.post(
            "/dashboard/api/transactions/undo",
            headers=AUTH,
            json={"transaction_id": result["transaction_id"]},
        )
        self.assertEqual(400, missing_confirm.status_code)
        self.assertEqual(
            "undo_confirmation_required",
            missing_confirm.json()["error"],
        )
        self.assertEqual("after", target.read_text(encoding="utf-8"))

        undone = self.client.post(
            "/dashboard/api/transactions/undo",
            headers=AUTH,
            json={"transaction_id": result["transaction_id"], "confirm": True},
        )
        self.assertEqual(200, undone.status_code)
        payload = undone.json()
        self.assertTrue(payload["ok"])
        self.assertEqual("undone", payload["transaction"]["state"])
        self.assertFalse(payload["transaction"]["can_undo"])
        self.assertEqual(
            "already_undone",
            payload["transaction"]["undo_blocked_reason"],
        )
        self.assertEqual("before", target.read_text(encoding="utf-8"))
        event = self.telemetry.query_events(limit=1)[0]
        self.assertEqual("dashboard", event["source"])
        self.assertEqual("file_transaction_undo", event["tool"])
        self.assertEqual(result["transaction_id"], event["arguments"]["transaction_id"])
        serialized_event = repr(event)
        self.assertNotIn(str(target), serialized_event)
        self.assertNotIn("before", serialized_event)
        self.assertNotIn("after", serialized_event)

    def test_dashboard_assets_wire_safe_transaction_history_ui(self) -> None:
        dashboard = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"
        html = (dashboard / "index.html").read_text(encoding="utf-8")
        js = (dashboard / "dashboard.js").read_text(encoding="utf-8")
        css = (dashboard / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn("Transaction history", html)
        self.assertIn('id="transactionList"', html)
        self.assertIn("Paths and file contents stay private", html)
        self.assertIn("/dashboard/api/transactions", js)
        self.assertIn("/dashboard/api/transactions/undo", js)
        self.assertIn("confirm: true", js)
        self.assertIn("Undo files", js)
        self.assertIn("unsupported effects will remain", js)
        self.assertNotIn("force: true", js)
        self.assertIn(".transaction-list", css)
        self.assertIn(".transaction-btn.is-confirm", css)

    def test_dashboard_history_is_bounded_and_contains_no_raw_paths(self) -> None:
        for index in range(4):
            target = self.work / f"secret-{index}.txt"
            target.write_text("before", encoding="utf-8")
            files.write_file(None, str(target), "after")

        response = self.client.get(
            "/dashboard/api/transactions?limit=2&offset=1",
            headers=AUTH,
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(2, payload["limit"])
        self.assertEqual(1, payload["offset"])
        self.assertEqual(2, len(payload["transactions"]))
        serialized = response.text
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("secret-0", serialized)
        self.assertNotIn("secret-1", serialized)
        self.assertNotIn("secret-2", serialized)
        self.assertNotIn("secret-3", serialized)


if __name__ == "__main__":
    unittest.main()
