from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

from tests._menu_sources import feature_files, feature_source

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


class MenuAgentFailureAgingTests(unittest.TestCase):
    def test_menu_uses_a_bounded_failure_window_and_neutral_history_summary(self):
        models = feature_source("AppState")
        menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")

        self.assertIn("static let recentFailureWindow: TimeInterval = 4 * 60 * 60", models)
        self.assertIn('case endedAt = "ended_at"', models)
        self.assertIn("var recentAgentFailureCount", models)
        self.assertIn("state.recentAgentFailureCount", menu)
        self.assertIn('parts.append("Last run \\(relativeTime(lastRunAt))")', menu)
        self.assertIn("agentFailureDisplay(agent)", menu)
        self.assertIn("agent.isRecentFailure(at: state.agentFailureReferenceTime) ? Color.red : Color.secondary", menu)

    def test_swift_failure_aging_uses_terminal_time(self):
        harness = r'''
import Foundation

func decodeAgent(_ json: String) -> AgentInfo {
    try! JSONDecoder().decode(AgentInfo.self, from: Data(json.utf8))
}

@main
struct AgentFailureAgingHarness {
    static func main() {
        let now = 100_000.0
        let recent = decodeAgent(#"{"agent_id":"agt_recent","status":"failed","phase":"failed","ended_at":99999}"#)
        let old = decodeAgent(#"{"agent_id":"agt_old","status":"timeout","phase":"timeout","ended_at":85600}"#)
        let completed = decodeAgent(#"{"agent_id":"agt_completed","status":"completed","phase":"completed","ended_at":99999}"#)
        let missingTime = decodeAgent(#"{"agent_id":"agt_legacy","status":"failed","phase":"failed"}"#)

        precondition(recent.isFailure)
        precondition(recent.isRecentFailure(at: now))
        precondition(!old.isRecentFailure(at: now))
        precondition(!completed.isRecentFailure(at: now))
        precondition(!missingTime.isRecentFailure(at: now))
        print("AGENT_FAILURE_AGING_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "AgentFailureAgingHarness.swift"
            binary = Path(td) / "agent-failure-aging"
            source.write_text(textwrap.dedent(harness), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                *map(str, feature_files("AppState")),
                str(SOURCES / "AgentNotificationController.swift"),
                str(SOURCES / "SettingsStore.swift"),
                str(SOURCES / "ToolActivityBubbleController.swift"),
                str(SOURCES / "KeychainStore.swift"),
                str(source),
                "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                "-framework", "UserNotifications", "-framework", "Combine",
                "-o", str(binary),
            ]
            compiled = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("AGENT_FAILURE_AGING_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
