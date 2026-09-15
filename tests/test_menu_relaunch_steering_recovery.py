from pathlib import Path
import os
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = ROOT / "menu_app" / "Sources" / "AppState.swift"


class MenuRelaunchSteeringRecoveryTests(unittest.TestCase):
    def test_source_persists_only_correlation_metadata_and_reuses_hash(self):
        source = APP_STATE.read_text(encoding="utf-8")
        self.assertIn('MAC_MCP_PENDING_STEERING_STATE_FILE', source)
        self.assertIn('pending-steering.json', source)
        self.assertIn('pendingSteeringTextHash == textHash', source)
        self.assertIn('Recovered steering after the menu app relaunched.', source)
        self.assertIn('Previous steering outcome is uncertain after relaunch.', source)
        self.assertIn('[.posixPermissions: 0o600]', source)
        persisted_block = source[source.index('struct PersistedPendingSteeringSubmission'):source.index('enum PendingSteeringSubmissionStore')]
        self.assertNotIn('text: String', persisted_block)
        self.assertIn('let textHash: String', persisted_block)

    def test_swift_store_and_relaunch_recovery(self):
        harness = r'''
import Foundation

func decodeSession(_ id: String) -> SteeringSession {
    let json = #"{"session_id":"\#(id)","flow_number":1,"label":"Agent session","detail":"Idle","tool":"read_file","state":"idle","activity_state":"idle","lifecycle_state":"ready","last_transition_at":1000,"last_error":null,"pending_instruction_count":0,"awaiting_acknowledgement_count":0,"created_at":900,"last_activity_at":1000,"activity_ms":1000,"queued":0,"active_calls":0}"#
    return try! JSONDecoder().decode(SteeringSession.self, from: Data(json.utf8))
}

func decodeRecent(_ id: String, sessionID: String, clientID: String) -> SteeringRecent {
    let json = #"{"schema_version":1,"kind":"instruction","id":"\#(id)","session_id":"\#(sessionID)","status":"queued","lifecycle_state":"queued","transitioned_at":1000,"delivered_at":null,"delivery_mode":null,"last_error":null,"client_instruction_id":"\#(clientID)"}"#
    return try! JSONDecoder().decode(SteeringRecent.self, from: Data(json.utf8))
}

@main
struct RelaunchRecoveryHarness {
    @MainActor static func main() async {
        let now = Date().timeIntervalSince1970
        let prompt = "keep checking the browser but do not touch checkout"
        let clientID = "client-relaunch-1"
        let sessionID = "sess_relaunch"
        let file = PendingSteeringSubmissionStore.stateURL()
        PendingSteeringSubmissionStore.clear()

        let hash = PendingSteeringSubmissionStore.textHash(prompt)
        precondition(hash.count == 64)
        precondition(hash == PendingSteeringSubmissionStore.textHash(prompt))
        precondition(hash != PendingSteeringSubmissionStore.textHash(prompt + "!"))

        try! PendingSteeringSubmissionStore.save(.init(
            schemaVersion: PendingSteeringSubmissionStore.schemaVersion,
            clientInstructionID: clientID,
            sessionID: sessionID,
            textHash: hash,
            createdAt: now
        ))
        let raw = try! String(contentsOf: file, encoding: .utf8)
        precondition(!raw.contains(prompt))
        precondition(raw.contains(clientID))
        let attrs = try! FileManager.default.attributesOfItem(atPath: file.path)
        let perms = (attrs[.posixPermissions] as? NSNumber)?.intValue ?? 0
        precondition((perms & 0o777) == 0o600)

        let restored = PendingSteeringSubmissionStore.load(now: now)
        precondition(restored?.clientInstructionID == clientID)
        precondition(restored?.sessionID == sessionID)
        precondition(restored?.textHash == hash)

        let app = AppState(startBackgroundTasks: false)
        precondition(app.steeringStatus.contains("previous menu-app run"))
        let live = decodeSession(sessionID)
        app.applySteeringSnapshot(.init(schemaVersion: 1, sessions: [live], recent: []))
        precondition(app.steeringStatus.contains("uncertain after relaunch"))
        precondition(FileManager.default.fileExists(atPath: file.path))

        let accepted = decodeRecent("st_recovered", sessionID: sessionID, clientID: clientID)
        app.applySteeringSnapshot(.init(schemaVersion: 1, sessions: [live], recent: [accepted]))
        precondition(app.steeringStatus == "Recovered steering after the menu app relaunched.")
        precondition(!FileManager.default.fileExists(atPath: file.path))

        try! PendingSteeringSubmissionStore.save(.init(
            schemaVersion: PendingSteeringSubmissionStore.schemaVersion,
            clientInstructionID: "client-stale",
            sessionID: sessionID,
            textHash: hash,
            createdAt: now - 7200
        ))
        precondition(PendingSteeringSubmissionStore.load(now: now, maxAgeSeconds: 3600) == nil)
        precondition(!FileManager.default.fileExists(atPath: file.path))
        print("MENU_RELAUNCH_STEERING_RECOVERY_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "RelaunchRecoveryHarness.swift"
            binary = Path(td) / "relaunch-recovery-harness"
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
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.relaunch-test",
                "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test",
            }
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20, env=run_env)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("MENU_RELAUNCH_STEERING_RECOVERY_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
