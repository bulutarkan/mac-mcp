from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = ROOT / "menu_app" / "Sources" / "AppState.swift"


class MenuSteeringIdempotencyTests(unittest.TestCase):
    def test_source_has_stable_id_bounded_retry_and_distinct_error_paths(self) -> None:
        source = APP_STATE.read_text(encoding="utf-8")
        self.assertIn('"client_instruction_id": clientInstructionID', source)
        self.assertIn("pendingSteeringClientInstructionID", source)
        self.assertIn("pendingSteeringSessionID == sessionID", source)
        self.assertIn("pendingSteeringText == text", source)
        self.assertIn("for attempt in 0..<2", source)
        self.assertIn("recoverSteeringMessageID", source)
        self.assertIn("Network result is uncertain. Send again to retry safely without duplicating the instruction.", source)
        self.assertIn("That agent session ended before the prompt could be queued.", source)
        self.assertIn("The agent session ended after the steering instruction was accepted.", source)
        self.assertIn('case "idempotency_conflict":', source)

    def test_swift_decodes_correlation_and_replay_fields(self) -> None:
        harness = r'''
import Foundation

@main
struct SteeringDecodeHarness {
    static func main() {
        let recentJSON = #"{"schema_version":1,"kind":"instruction","id":"st_one","session_id":"sess_one","status":"queued","lifecycle_state":"queued","transitioned_at":1000,"delivered_at":null,"delivery_mode":null,"last_error":null,"client_instruction_id":"client-one"}"#
        let recent = try! JSONDecoder().decode(SteeringRecent.self, from: Data(recentJSON.utf8))
        precondition(recent.clientInstructionID == "client-one")
        precondition(recent.id == "st_one")

        let sendJSON = #"{"ok":true,"status":"queued","message":{"id":"st_one","client_instruction_id":"client-one","idempotent_replay":true,"session_state":"idle","activity_state":"idle","lifecycle_state":"queued"}}"#
        let sent = try! JSONDecoder().decode(SteeringSendEnvelope.self, from: Data(sendJSON.utf8))
        precondition(sent.message?.id == "st_one")
        precondition(sent.message?.clientInstructionID == "client-one")
        precondition(sent.message?.idempotentReplay == true)
        print("STEERING_IDEMPOTENCY_DECODE_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "SteeringDecodeHarness.swift"
            binary = Path(td) / "steering-decode-harness"
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
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("STEERING_IDEMPOTENCY_DECODE_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
