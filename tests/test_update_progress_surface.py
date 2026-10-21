"""#78: an update the owner starts from the menu is visible from start to end.

Before it starts the menu says Mac MCP will close and open again by itself; while it runs it shows a spinner and
the step; the app that opens again keeps following an update still under way; and once it ends, that app says
how (updated to which release, or went back to the version you had), once, by notice and notification.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

func snapshot(_ json: String) -> UpdateStateSnapshot {
    try! JSONDecoder().decode(UpdateStateSnapshot.self, from: Data(json.utf8))
}

@main
struct UpdateSurfaceHarness {
    static func main() {
        let done = snapshot(#"{"status":"completed","release_id":"stable-2.3.0-r2","release_version":"2.3.0","updated_at":1}"#)
        precondition(done.isFinal && done.releaseLabel == "2.3.0 r2", "\(String(describing: done.releaseLabel))")
        let said = done.resultAnnouncement!
        precondition(said.title == "Mac MCP updated to 2.3.0 r2" && said.success, said.title)

        let back = snapshot(#"{"status":"failed","runtime_rollback":{"status":"restored"},"rollback_health":{"status":"passed"}}"#)
        precondition(back.resultAnnouncement!.title == "Update didn’t finish" && !back.resultAnnouncement!.success)
        precondition(back.resultAnnouncement!.body.contains("went back to the version you had"))
        precondition(back.resultAnnouncement!.body.contains("~/.mac-mcp/update/logs"))

        let running = snapshot(#"{"status":"runtime_syncing","release_id":"stable-2.3.0-r2"}"#)
        precondition(!running.isFinal && running.resultAnnouncement == nil)
        let step = running.stepPosition!
        precondition(step.current == 4 && step.total == 7, "\(step)")
        precondition(snapshot(#"{"status":"completed","release_version":"2.3.1"}"#).releaseLabel == "2.3.1")
        print("UPDATE_SURFACE_PASS")
    }
}
'''


class UpdateResultModelTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xcrun"), "Xcode command line tools are not installed")
    def test_the_relaunched_app_knows_what_to_say(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "main.swift"
            binary = Path(td) / "update-surface"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            cmd = ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "AppModels+Updates.swift"), str(harness),
                   "-o", str(binary)]
            built = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr[-2000:])
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, ran.returncode, ran.stderr[-2000:])
            self.assertIn("UPDATE_SURFACE_PASS", ran.stdout)


class UpdateFlowWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        cls.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")

    def test_the_reopened_app_follows_or_reports_the_update(self) -> None:
        self.assertIn("if updateTransactionActive { startUpdateStatePolling() } else { announceUpdateResultIfNew() }",
                      self.state)
        polling = self.state[self.state.index("private func startUpdateStatePolling"):]
        polling = polling[:polling.index("\n    }\n")]
        self.assertIn("self.announceUpdateResultIfNew()", polling)

    def test_the_result_is_said_once_and_only_while_it_is_news(self) -> None:
        announce = self.state[self.state.index("func announceUpdateResultIfNew"):]
        announce = announce[:announce.index("\n    }\n")]
        self.assertIn("announcedUpdateResultKey", announce)
        self.assertIn("updateResultFreshSeconds", announce)
        self.assertIn("scheduleUpdateResult", announce)

    def test_closing_is_announced_before_it_happens(self) -> None:
        install = self.state[self.state.index("    func installUpdate()"):]
        self.assertLess(install.index("closes and opens again by itself"), install.index('args: ["update"]'))
        self.assertIn("Mac MCP closes and opens again by itself during the update.", self.menu)
        self.assertIn("ProgressView().controlSize(.mini)", self.menu)
        self.assertIn("Step \\(position.current) of \\(position.total)", self.menu)


if __name__ == "__main__":
    unittest.main()
