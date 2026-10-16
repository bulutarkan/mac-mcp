"""#198 in the native app: a run that finished but failed its check reads differently from one that passed."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._menu_sources import feature_files

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

@main struct ChecksHarness {
    static func main() {
        let home = NSHomeDirectory()
        let failed = TaskRun(["status": "done", "started_at": Date().timeIntervalSince1970,
                              "verification": ["state": "failed", "checks": [
                                  ["path": home + "/Desktop/report.csv", "ok": false, "problem": "missing"]]]])
        precondition(failed.checkFailed)
        precondition(failed.symbol == "exclamationmark.triangle.fill")
        precondition(failed.shortLine.hasSuffix(", but report.csv is missing"), failed.shortLine)
        precondition(TaskRun.displayPath(home + "/Desktop/report.csv") == "~/Desktop/report.csv")

        let verified = TaskRun(["status": "done", "verification": ["state": "verified", "checks": [
            ["path": "/tmp/out.csv", "ok": true, "size": 2048]]]])
        precondition(!verified.checkFailed && verified.symbol == "checkmark.seal.fill")
        precondition(TaskRun.checkLine(verified.checks[0]).hasPrefix("out.csv is there ("))

        let privateRun = TaskRun(["status": "done", "verification": ["state": "failed"]])
        precondition(privateRun.checkProblem == "its check failed")

        let plain = TaskRun(["status": "done"])
        precondition(!plain.checkFailed && plain.symbol == "checkmark.circle.fill" && plain.checkProblem == nil)

        let crashed = TaskRun(["status": "failed", "reason": "Exited with code 3"])
        precondition(!crashed.checkFailed && crashed.symbol == "xmark.circle.fill")
        print("CHECKS_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class NativeOutputCheckTests(unittest.TestCase):
    def test_run_lines_symbols_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "Harness.swift"
            binary = Path(td) / "harness"
            source.write_text(HARNESS, encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", *map(str, feature_files("AppState")),
                 *(str(SOURCES / name) for name in ("AgentNotificationController.swift", "SettingsStore.swift",
                                                    "ToolActivityBubbleController.swift", "KeychainStore.swift")),
                 str(source), "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                 "-framework", "UserNotifications", "-framework", "Combine", "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("CHECKS_PASS", ran.stdout)

    def test_the_editor_sends_its_checks_and_clearing_removes_them(self) -> None:
        pane = (SOURCES / "ScheduledTasksPane.swift").read_text(encoding="utf-8")
        self.assertIn('out["verify"] = checks  // empty removes the checks on an edit', pane)
        self.assertIn('field("Check the result", optional: true)', pane)
        self.assertIn('Row(title: "Checks"', pane)  # an AI app's suggested checks show in Review
        state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        self.assertIn('case "done" where run.checkFailed:', state)
        report = (SOURCES / "RunReportView.swift").read_text(encoding="utf-8")
        self.assertIn("checksBox", report)


if __name__ == "__main__":
    unittest.main()
