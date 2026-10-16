"""Session detail metrics: logical-call accounting and backwards-compatible native display."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.steering import SteeringIdentity, SteeringManager

ROOT = Path(__file__).resolve().parents[1]


class SessionMetricTests(unittest.TestCase):
    def setUp(self):
        self.manager = SteeringManager(session_ttl_s=60)
        self.identity = SteeringIdentity(key="detail-a", source="client_id")

    def test_counts_calls_not_parallel_commands_and_keeps_failed_calls(self):
        self.manager.prepare_call(self.identity, tool="read_file")
        self.assertEqual(0, self.manager.sessions()[0].get("tool_call_count"))
        self.manager.begin_call(self.identity, "one", tool="run_commands_parallel",
                                arguments={"commands": ["pwd", "ls"]})
        self.manager.begin_call(self.identity, "two", tool="read_file")
        row = self.manager.sessions()[0]
        self.assertEqual(2, row["tool_call_count"])
        self.assertEqual(2, row["active_calls"])
        self.manager.finish_call(self.identity, "one", delivered=False)
        self.manager.finish_call(self.identity, "two", delivered=True)
        row = self.manager.sessions()[0]
        self.assertEqual(2, row["tool_call_count"])
        self.assertEqual(0, row["active_calls"])
        self.assertIsNone(row["activity_started_at"])

    def test_repeated_event_does_not_count_or_restart_the_call(self):
        with patch("mcp_server.steering.time.time", return_value=1000):
            self.manager.begin_call(self.identity, "same", tool="read_file")
        with patch("mcp_server.steering.time.time", return_value=1005):
            self.manager.begin_call(self.identity, "same", tool="read_file")
            row = self.manager.sessions()[0]
            self.assertEqual(1, row.get("tool_call_count"))
            self.assertEqual(1000, row["activity_started_at"])
            self.assertEqual(5000, row["activity_ms"])
        with patch("mcp_server.steering.time.time", return_value=1010):
            self.manager.finish_call(self.identity, "same", delivered=True)
            self.manager.begin_call(self.identity, "same", tool="read_file")
            self.assertEqual(1, self.manager.sessions()[0]["tool_call_count"])
            self.assertEqual(0, self.manager.sessions()[0]["active_calls"])

    def test_metrics_are_isolated_and_reset_for_a_new_session(self):
        with patch("mcp_server.steering.time.time", return_value=1000):
            old_id = self.manager.session_id_for(self.identity)
            self.manager.begin_call(self.identity, "one", tool="read_file")
            self.manager.finish_call(self.identity, "one", delivered=True)
            other = SteeringIdentity(key="detail-b", source="client_id")
            self.manager.session_id_for(other)
            counts = {s["session_id"]: s["tool_call_count"] for s in self.manager.sessions()}
            self.assertEqual(1, counts[old_id])
            self.assertEqual(0, counts[self.manager.session_id_for(other)])
        with patch("mcp_server.steering.time.time", return_value=1061):
            new_id = self.manager.session_id_for(self.identity)
            self.assertNotEqual(old_id, new_id)
            self.assertEqual(0, self.manager.sessions()[0]["tool_call_count"])


class NativeSessionDetailTests(unittest.TestCase):
    def test_decode_diff_and_duration_semantics(self):
        # A missing count must stay unknown; call age must not become session age.
        harness = r'''
import Foundation

@main struct SessionDetailHarness {
    static func main() throws {
        let legacy = #"{"session_id":"sess-a","flow_number":1,"label":"Terminal","detail":"run_command","tool":"run_command","state":"working","created_at":1000,"last_activity_at":1700,"activity_ms":33000,"queued":0,"active_calls":2}"#
        func decode(_ extra: String = "") throws -> SteeringSession {
            let json = String(legacy.dropLast()) + extra + "}"
            return try JSONDecoder().decode(SteeringSession.self, from: Data(json.utf8))
        }
        let old = try decode()
        precondition(old.toolCallCount == nil)
        let fresh = try decode(#", "tool_call_count":24, "activity_started_at":1700"#)
        let presentation = SteeringSessionPresentation(session: fresh, now: 1733, isStale: false)
        precondition(presentation.sessionDuration == "12m 13s")
        precondition(presentation.activityDuration == "33s")
        precondition(presentation.callCount == "24")
        precondition(presentation.summary == "12m · 24 calls")
        precondition(SteeringSessionPresentation(session: old, now: 1733, isStale: false).callCount == "—")
        // A queued or undelivered prompt stays readable on the row, not only as an icon tooltip.
        let queued = try decode(#", "tool_call_count":24, "lifecycle_state":"queued""#)
        precondition(SteeringSessionPresentation(session: queued, now: 1733, isStale: false).summary
                     == "12m · 24 calls · Steering queued")
        let failed = try decode(#", "tool_call_count":1, "lifecycle_state":"failed""#)
        precondition(SteeringSessionPresentation(session: failed, now: 1733, isStale: false).summary
                     == "12m · 1 call · Delivery not completed")
        let ended = try decode(#", "tool_call_count":24, "lifecycle_state":"disconnected""#)
        precondition(SteeringSessionPresentation(session: ended, now: 1733, isStale: false).summary == "12m · 24 calls")
        let changed = try decode(#", "tool_call_count":25, "activity_started_at":1700"#)
        precondition(!fresh.isPresentationEquivalent(to: changed))
        let stale = SteeringSessionPresentation(session: fresh, now: 2000, isStale: true)
        precondition(stale.activityDuration == "33s")
        precondition(stale.status == "Last known: Working")
        let earlier = SteeringSessionPresentation(session: fresh, now: 900, isStale: false)
        precondition(earlier.sessionDuration == "0s")
        let idleJSON = legacy.replacingOccurrences(of: #""working""#, with: #""idle""#)
        let idle = try JSONDecoder().decode(SteeringSession.self, from: Data(idleJSON.utf8))
        let idlePresentation = SteeringSessionPresentation(session: idle, now: 1733, isStale: false)
        precondition(idlePresentation.status == "Idle")
        precondition(idlePresentation.activityDuration == nil)
        print("SESSION_DETAIL_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "Harness.swift"
            binary = Path(td) / "harness"
            source.write_text(harness)
            compile_result = subprocess.run([
                "xcrun", "swiftc", "-parse-as-library",
                str(ROOT / "menu_app/Sources/AppModels+Steering.swift"),
                str(ROOT / "menu_app/Sources/SessionDetailPresentation.swift"), str(source),
                "-o", str(binary),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(0, compile_result.returncode, compile_result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("SESSION_DETAIL_PASS", result.stdout)
