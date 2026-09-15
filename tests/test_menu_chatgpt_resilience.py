from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP_STATE = ROOT / "menu_app/Sources/AppState.swift"
MENU_VIEW = ROOT / "menu_app/Sources/MenuBarView.swift"


class MenuChatGPTResilienceTests(unittest.TestCase):
    def test_source_surfaces_checkpoint_and_throttle_states(self):
        state = APP_STATE.read_text(encoding="utf-8")
        view = MENU_VIEW.read_text(encoding="utf-8")
        self.assertIn('case turnElapsedMS = "turn_elapsed_ms"', state)
        self.assertIn('case checkpointCount = "checkpoint_count"', state)
        self.assertIn('case throttleCount = "throttle_count"', state)
        self.assertIn('case "checkpointing": return "Checkpointing"', view)
        self.assertIn('case "throttled": return "Provider cooldown"', view)
        self.assertIn('agent.checkpointCount', view)
        self.assertIn('agent.throttleCount', view)
        self.assertIn('turn \\(compactDuration(turnElapsed))', view)

    def test_swift_decodes_resilience_fields(self):
        harness = r'''
import Foundation

@main
struct AgentResilienceDecodeHarness {
    static func main() {
        let json = #"{"agent_id":"agt_test","status":"running","phase":"throttled","provider":"chatgpt","model":"GPT-5.6 Sol","reasoning":"high","last_tool":"Search","last_tool_duration_ms":12000,"tool_call_count":4,"retry_count":1,"duration_ms":900000,"turn_count":2,"turn_elapsed_ms":610000,"turn_budget_s":900,"checkpoint_count":1,"checkpoint_pending":false,"throttle_count":2,"last_throttled_at":1000,"last_throttle_reason":"requesting_too_fast","cooldown_until":1090}"#
        let agent = try! JSONDecoder().decode(AgentInfo.self, from: Data(json.utf8))
        precondition(agent.agentID == "agt_test")
        precondition(agent.turnCount == 2)
        precondition(agent.turnElapsedMS == 610000)
        precondition(agent.turnBudgetS == 900)
        precondition(agent.checkpointCount == 1)
        precondition(agent.throttleCount == 2)
        precondition(agent.lastThrottleReason == "requesting_too_fast")
        precondition(agent.lastToolDurationMS == 12000)
        print("AGENT_RESILIENCE_DECODE_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "AgentResilienceDecodeHarness.swift"
            binary = Path(td) / "agent-resilience-decode"
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
            self.assertIn("AGENT_RESILIENCE_DECODE_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
