from pathlib import Path
import os
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = ROOT / "menu_app" / "Sources" / "AppState.swift"


class MenuSteeringGenerationRecoveryTests(unittest.TestCase):
    def test_source_sends_generation_and_handles_unknown_outcome(self):
        source = APP_STATE.read_text(encoding="utf-8")
        self.assertIn('"generation_id": generationID', source)
        self.assertIn('case "stale_generation":', source)
        self.assertIn('case "idempotency_expired":', source)
        self.assertIn('pendingSteeringGenerationID == generationID', source)
        self.assertIn('Delivery outcome is unknown', source)

    def test_relaunch_marker_is_invalidated_when_daemon_generation_changes(self):
        harness = r'''
import Foundation

func decodeSession(_ id: String) -> SteeringSession {
    let json = #"{"session_id":"\#(id)","flow_number":1,"label":"Agent session","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"ready","last_transition_at":1000,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":900,"last_activity_at":1000,"activity_ms":1000,"queued":0,"active_calls":0}"#
    return try! JSONDecoder().decode(SteeringSession.self, from: Data(json.utf8))
}

@main
struct GenerationRecoveryHarness {
    @MainActor static func main() async {
        let now = Date().timeIntervalSince1970
        let file = PendingSteeringSubmissionStore.stateURL()
        PendingSteeringSubmissionStore.clear()
        try! PendingSteeringSubmissionStore.save(.init(
            schemaVersion: PendingSteeringSubmissionStore.schemaVersion,
            clientInstructionID: "client-old",
            sessionID: "sess-old",
            textHash: PendingSteeringSubmissionStore.textHash("uncertain prompt"),
            generationID: "gen-old",
            createdAt: now
        ))

        let app = AppState(startBackgroundTasks: false)
        let live = decodeSession("sess-new")
        app.applySteeringSnapshot(.init(schemaVersion: 1, generationID: "gen-new", sessions: [live], recent: []))
        precondition(app.steeringGenerationID == "gen-new")
        precondition(app.steeringStatus.contains("Delivery outcome is unknown"))
        precondition(!FileManager.default.fileExists(atPath: file.path))
        print("STEERING_GENERATION_RECOVERY_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "GenerationRecoveryHarness.swift"
            binary = Path(td) / "generation-recovery-harness"
            state_file = Path(td) / "state" / "pending-steering.json"
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
            compiled = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            run_env = {
                **os.environ,
                "MAC_MCP_PENDING_STEERING_STATE_FILE": str(state_file),
                "MAC_MCP_SETTINGS_PATH": str(Path(td) / "settings.json"),
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.generation-test",
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test",
            }
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20, env=run_env)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("STEERING_GENERATION_RECOVERY_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
