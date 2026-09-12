from pathlib import Path
import os
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = ROOT / "menu_app" / "Sources" / "AppState.swift"


class MenuStatePerformanceTests(unittest.TestCase):
    def test_app_state_is_main_actor_and_uses_change_guard(self):
        source = APP_STATE.read_text(encoding="utf-8")
        self.assertIn("@MainActor\nfinal class AppState: ObservableObject", source)
        self.assertIn("private func setIfChanged<Value: Equatable>", source)
        self.assertIn("SteeringSessionGrouping.diff(current: steeringSessions, incoming: envelope.sessions)", source)
        self.assertIn("func applySteeringSnapshot(_ envelope: SteeringEnvelope)", source)

    def test_stable_session_diff_handles_identical_update_add_remove_at_scale(self):
        harness = r'''
import Combine
import Foundation

func session(_ id: Int, label: String? = nil, activityMS: Int = 1000) -> SteeringSession {
    let value = label ?? "Session \(id)"
    let json = #"{"session_id":"sess_\#(id)","flow_number":\#(id + 1),"label":"\#(value)","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"ready","last_transition_at":1000,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":900,"last_activity_at":1000,"activity_ms":\#(activityMS),"queued":0,"active_calls":0}"#
    return try! JSONDecoder().decode(SteeringSession.self, from: Data(json.utf8))
}

func envelope(_ sessions: [SteeringSession]) -> SteeringEnvelope {
    SteeringEnvelope(schemaVersion: 1, sessions: sessions, recent: [])
}

@main
struct PerfDiffHarness {
    @MainActor static func main() async {
        let base = (0..<120).map { session($0) }
        let identical = SteeringSessionGrouping.diff(current: base, incoming: base)
        precondition(!identical.hasChanges)

        let oneMinute = [session(0, activityMS: 61_000)]
        let sameVisibleMinute = [session(0, activityMS: 63_000)]
        let nextVisibleMinute = [session(0, activityMS: 120_000)]
        precondition(!SteeringSessionGrouping.diff(current: oneMinute, incoming: sameVisibleMinute).hasChanges)
        precondition(SteeringSessionGrouping.diff(current: oneMinute, incoming: nextVisibleMinute).updatedIDs == ["sess_0"])
        precondition(SteeringSessionGrouping.diff(current: [session(0, activityMS: 1_000)], incoming: [session(0, activityMS: 3_000)]).updatedIDs == ["sess_0"])

        var changed = base
        changed[57] = session(57, label: "Session 57 changed")
        let changedDiff = SteeringSessionGrouping.diff(current: base, incoming: changed)
        precondition(changedDiff.addedIDs.isEmpty)
        precondition(changedDiff.updatedIDs == ["sess_57"])
        precondition(changedDiff.removedIDs.isEmpty)

        let added = changed + [session(120)]
        let addDiff = SteeringSessionGrouping.diff(current: changed, incoming: added)
        precondition(addDiff.addedIDs == ["sess_120"])
        precondition(addDiff.updatedIDs.isEmpty)
        precondition(addDiff.removedIDs.isEmpty)

        let removed = Array(added.dropFirst())
        let removeDiff = SteeringSessionGrouping.diff(current: added, incoming: removed)
        precondition(removeDiff.addedIDs.isEmpty)
        precondition(removeDiff.updatedIDs.isEmpty)
        precondition(removeDiff.removedIDs == ["sess_0"])

        let state = AppState(startBackgroundTasks: false)
        var publishes = 0
        let cancellable = state.objectWillChange.sink { publishes += 1 }
        _ = cancellable
        state.applySteeringSnapshot(envelope(base))

        publishes = 0
        state.applySteeringSnapshot(envelope(base))
        precondition(publishes == 0)

        publishes = 0
        state.applySteeringSnapshot(envelope(changed))
        precondition(publishes == 1)

        publishes = 0
        state.applySteeringSnapshot(envelope(added))
        precondition(publishes == 1)

        publishes = 0
        state.applySteeringSnapshot(envelope(removed))
        precondition(publishes == 1)
        print("SESSION_DIFF_120_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "PerfDiffHarness.swift"
            binary = Path(td) / "perf-diff-harness"
            source.write_text(textwrap.dedent(harness), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                str(APP_STATE),
                str(ROOT / "menu_app/Sources/SettingsStore.swift"),
                str(ROOT / "menu_app/Sources/KeychainStore.swift"),
                str(source),
                "-framework", "AppKit", "-framework", "Security", "-framework", "Combine",
                "-o", str(binary),
            ]
            compile_result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(0, compile_result.returncode, compile_result.stderr)
            run_env = {**os.environ, "MAC_MCP_SETTINGS_PATH": str(Path(td) / "settings.json"), "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.perf-test", "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test"}
            run_result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20, env=run_env)
            self.assertEqual(0, run_result.returncode, run_result.stderr)
            self.assertIn("SESSION_DIFF_120_PASS", run_result.stdout)


if __name__ == "__main__":
    unittest.main()
