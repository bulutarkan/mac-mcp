from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import observability
from mcp_server.observability import TelemetryManager


class AsyncTelemetryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.manager = TelemetryManager(self.tmp / "t.sqlite3", usage_enabled=False, async_writes=True)

    def tearDown(self) -> None:
        self.manager.wait_events_idle(5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, result, tool: str = "read_file") -> str:
        event_id = self.manager.start_call("mcp", tool, {"path": "/tmp/x"})
        self.manager.finish_call(event_id, result=result)
        return event_id

    def test_finished_calls_are_queued_then_written_and_published(self) -> None:
        published = []
        original = self.manager._publish
        self.manager._publish = lambda event: (published.append(event.get("kind")), original(event))
        event_id = self.manager.start_call("mcp", "read_file", {"path": "/tmp/x"})
        receipt = self.manager.finish_call(event_id, result={"ok": True, "content": "hello"})
        self.assertEqual({"event_id": event_id, "status": "success", "queued": True}, receipt)
        self.assertTrue(self.manager.wait_events_idle(5))
        rows = self.manager.query_events(hours=1)
        self.assertEqual([event_id], [row["event_id"] for row in rows])
        self.assertEqual(event_id, self.manager.recent_events(1)[0]["event_id"])
        self.assertIn("call_finished", published)

    def test_secrets_are_still_redacted_on_the_writer_thread(self) -> None:
        secret = "sk-" + "a" * 48
        event_id = self.call({"ok": True, "content": f"token {secret}"})
        self.manager.wait_events_idle(5)
        row = next(row for row in self.manager.query_events(hours=1) if row["event_id"] == event_id)
        self.assertNotIn(secret, str(row["result"]))

    def test_result_changed_after_return_is_recorded_as_returned(self) -> None:
        result = {"ok": True, "items": ["before"]}
        with patch.object(self.manager, "_ensure_finish_worker"):
            event_id = self.call(result)
            result["items"].append("after")
            result["extra"] = "late"
            self.manager._ensure_finish_worker.assert_called_once()
        self.manager._ensure_finish_worker()
        self.manager.wait_events_idle(5)
        row = next(row for row in self.manager.query_events(hours=1) if row["event_id"] == event_id)
        # Stored as metadata: one item of six characters, and nothing added after the return.
        self.assertEqual({"ok": True, "items": ["[text · 6 chars]"]}, row["result"])

    def test_a_full_queue_writes_inline_instead_of_dropping(self) -> None:
        with patch.object(self.manager, "_ensure_finish_worker"):
            queued = [self.call({"ok": True, "n": n}) for n in range(observability.FINISH_QUEUE_SIZE)]
            event_id = self.manager.start_call("mcp", "read_file", {})
            inline = self.manager.finish_call(event_id, result={"ok": True})
            self.assertEqual("call_finished", inline.get("kind"), "the caller wrote the event itself")
        self.manager._ensure_finish_worker()
        self.assertTrue(self.manager.wait_events_idle(5))
        written = {row["event_id"] for row in self.manager.query_events(hours=1, limit=100)}
        self.assertEqual(set(queued) | {event_id}, written)

    def test_security_events_stay_synchronous(self) -> None:
        with patch.object(self.manager, "_ensure_finish_worker"):
            self.manager.record_security_event(
                session_id="s", event_type="POLICY_DENY", tool="run_command", tool_class="shell",
                origin=None, decision="deny", reason_code="fixture", profile="default", actor=None, agent_id=None,
            )
            self.assertEqual(1, len(self.manager.query_security_events(hours=1)))

    def test_explicit_db_path_defaults_to_synchronous_writes(self) -> None:
        manager = TelemetryManager(self.tmp / "sync.sqlite3", usage_enabled=False)
        event_id = manager.start_call("mcp", "read_file", {})
        event = manager.finish_call(event_id, result={"ok": True})
        self.assertEqual("call_finished", event["kind"])
        self.assertEqual([event_id], [row["event_id"] for row in manager.query_events(hours=1)])


if __name__ == "__main__":
    unittest.main()
