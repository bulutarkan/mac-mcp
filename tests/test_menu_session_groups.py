from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MenuSessionGroupingTests(unittest.TestCase):
    def test_swift_grouping_fixtures(self):
        harness = r'''
import Foundation

func decodeSession(_ json: String) -> SteeringSession {
    try! JSONDecoder().decode(SteeringSession.self, from: Data(json.utf8))
}
func decodeRecent(_ json: String) -> SteeringRecent {
    try! JSONDecoder().decode(SteeringRecent.self, from: Data(json.utf8))
}

@main
struct FixtureHarness {
    static func main() {
        let now = 2_000.0
        let failed = decodeSession(#"{"session_id":"s-failed","flow_number":1,"label":"Failed","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"failed","last_transition_at":1995,"last_error":"tool_failed_before_steering_delivery","pending_instruction_count":1,"awaiting_acknowledgement_count":0,"created_at":1800,"last_activity_at":1990,"activity_ms":10000,"queued":1,"active_calls":0}"#)
        let active = decodeSession(#"{"session_id":"s-active","flow_number":2,"label":"Active","detail":"Running","tool":"browser_do","state":"working","activity_state":"working","lifecycle_state":"ready","last_transition_at":1994,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":1800,"last_activity_at":1998,"activity_ms":2000,"queued":0,"active_calls":1}"#)
        let queued = decodeSession(#"{"session_id":"s-queued","flow_number":3,"label":"Queued","detail":"Idle","tool":"search_files","state":"idle","activity_state":"idle","lifecycle_state":"queued","last_transition_at":1993,"last_error":null,"pending_instruction_count":1,"awaiting_acknowledgement_count":0,"created_at":1800,"last_activity_at":1992,"activity_ms":8000,"queued":1,"active_calls":0}"#)
        let idle = decodeSession(#"{"session_id":"s-idle","flow_number":4,"label":"Recent","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"acknowledged","last_transition_at":1980,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":1700,"last_activity_at":1950,"activity_ms":50000,"queued":0,"active_calls":0}"#)
        let tooOld = decodeSession(#"{"session_id":"s-old","flow_number":5,"label":"Old","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"ready","last_transition_at":1500,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":1400,"last_activity_at":1500,"activity_ms":500000,"queued":0,"active_calls":0}"#)
        let disconnected = decodeRecent(#"{"schema_version":1,"kind":"session","id":"se-one","session_id":"s-ended","status":"session_ended","lifecycle_state":"disconnected","transitioned_at":1996,"delivered_at":null,"delivery_mode":null,"last_error":null}"#)
        let expiredOld = decodeRecent(#"{"schema_version":1,"kind":"session","id":"se-old","session_id":"s-expired","status":"session_expired","lifecycle_state":"expired","transitioned_at":1200,"delivered_at":null,"delivery_mode":null,"last_error":null}"#)

        let groups = SteeringSessionGrouping.groups(
            sessions: [idle, queued, failed, active, tooOld],
            recentEvents: [disconnected, expiredOld],
            retentionMinutes: 2,
            now: now
        )
        precondition(groups.needsAttention.map(\.sessionID) == ["s-failed"])
        precondition(groups.active.map(\.sessionID) == ["s-active", "s-queued"])
        precondition(groups.recent.map(\.sessionID) == ["s-idle"])
        precondition(groups.terminalAttention.map(\.sessionID) == ["s-ended"])
        precondition(groups.totalItemCount == 5)
        precondition(groups.sectionCount == 3)

        let shorterRetention = SteeringSessionGrouping.groups(
            sessions: [idle],
            recentEvents: [disconnected],
            retentionMinutes: 1,
            now: 2_100
        )
        precondition(shorterRetention.recent.isEmpty)
        precondition(shorterRetention.terminalAttention.isEmpty)
        print("SESSION_GROUPING_FIXTURES_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "SessionGroupingHarness.swift"
            binary = Path(td) / "session-grouping-harness"
            source.write_text(textwrap.dedent(harness), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                str(ROOT / "menu_app/Sources/AppState.swift"),
                str(ROOT / "menu_app/Sources/SettingsStore.swift"),
                str(ROOT / "menu_app/Sources/KeychainStore.swift"),
                str(source),
                "-framework", "AppKit", "-framework", "Security",
                "-o", str(binary),
            ]
            compile_result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(0, compile_result.returncode, compile_result.stderr)
            run_result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run_result.returncode, run_result.stderr)
            self.assertIn("SESSION_GROUPING_FIXTURES_PASS", run_result.stdout)


if __name__ == "__main__":
    unittest.main()
