"""#59: the Get Started checklist, compiled and run with the real Swift source."""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menu_app" / "Sources" / "OnboardingChecklist.swift"
VIEW = ROOT / "menu_app" / "Sources" / "SettingsView.swift"
MENU = ROOT / "menu_app" / "Sources" / "MenuBarView.swift"

HARNESS = r'''
import Foundation

func check(_ condition: Bool, _ message: String) {
    if !condition { print("FAIL: \(message)"); exit(1) }
}

func status(_ json: String) -> OnboardingStatus {
    try! JSONDecoder().decode(OnboardingStatus.self, from: json.data(using: .utf8)!)
}

@main
struct Harness {
    static func main() {
        // Server down: nothing is known yet, and the first step says so.
        let down = OnboardingChecklist.make(serverRunning: false, status: nil)
        check(down.steps.map(\.id) == [.server, .client, .firstRequest], "step order")
        check(down.optional.map(\.id) == [.appControl, .screenCapture], "permissions are optional")
        check(down.completed == 0, "nothing done while the server is down")
        check(down.nextStep?.id == .server, "start the server first")
        check(OnboardingChecklist.shouldShowReminder(down, dismissed: false), "reminder shows while unfinished")
        check(!OnboardingChecklist.shouldShowReminder(down, dismissed: true), "hidden once dismissed")

        // Fresh install, server up, no client yet.
        let fresh = status("""
        {"ok": true, "first_call": null, "first_success": null,
         "accessibility": "not_determined", "screen_recording": "denied", "public_endpoint_mode": "none"}
        """)
        let started = OnboardingChecklist.make(serverRunning: true, status: fresh)
        check(started.completed == 1, "only the server step is done")
        check(started.nextStep?.id == .client, "connect a client next")
        check(started.steps[1].detail.contains("ChatGPT needs a public HTTPS"), "local-only explains ChatGPT")

        // A client called but the call failed: connected, first request still open.
        let failed = status("""
        {"ok": true, "first_call": {"at": 1791800000, "tool": "browser_act", "source": "mcp", "status": "error"},
         "first_success": null, "accessibility": "granted", "screen_recording": "granted",
         "public_endpoint_mode": "cloudflare"}
        """)
        let partial = OnboardingChecklist.make(serverRunning: true, status: failed)
        check(partial.steps.filter(\.done).map(\.id) == [.server, .client], "connected")
        check(partial.optional.allSatisfy(\.done), "permissions granted")
        check(partial.nextStep?.id == .firstRequest, "first request is next")
        check(!partial.steps[1].detail.contains("public HTTPS"), "no ChatGPT note once a public endpoint is set")

        // Everything done: complete, and the reminder disappears for returning users.
        let done = status("""
        {"ok": true, "first_call": {"at": 1791800000, "tool": "system_info", "source": "mcp", "status": "success"},
         "first_success": {"at": 1791800000, "tool": "system_info", "source": "mcp", "status": "success"},
         "accessibility": "granted", "screen_recording": "granted", "public_endpoint_mode": "none"}
        """)
        let complete = OnboardingChecklist.make(serverRunning: true, status: done)
        check(complete.isComplete && complete.nextStep == nil, "complete")
        check(!OnboardingChecklist.shouldShowReminder(complete, dismissed: false), "no reminder when complete")

        // #247: a first request that worked finishes setup even with no Mac app permissions.
        let noPermissions = status("""
        {"ok": true, "first_call": {"at": 1791800000, "tool": "system_info", "source": "mcp", "status": "success"},
         "first_success": {"at": 1791800000, "tool": "system_info", "source": "mcp", "status": "success"},
         "accessibility": "not_determined", "screen_recording": "denied", "public_endpoint_mode": "none"}
        """)
        let filesOnly = OnboardingChecklist.make(serverRunning: true, status: noPermissions)
        check(filesOnly.isComplete && filesOnly.completed == 3, "setup complete without permissions")
        check(!OnboardingChecklist.shouldShowReminder(filesOnly, dismissed: false), "no reminder without permissions")
        check(!filesOnly.optional.contains { $0.done }, "permissions stay offered")
        check(filesOnly.optional[0].detail.contains("Files, the terminal and the browser work without it"), "says what it is for")
        check(OnboardingChecklist.firstRequestPrompt.contains("macOS version"), "safe read-only prompt")
        print("ONBOARDING_PASS")
    }
}
'''


class OnboardingChecklistTests(unittest.TestCase):
    def test_checklist_logic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "Harness.swift"
            binary = Path(td) / "onboarding-harness"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCE), str(harness), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("ONBOARDING_PASS", run.stdout)

    def test_app_wires_the_checklist(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("Sources/OnboardingChecklist.swift", build)
        view = VIEW.read_text(encoding="utf-8")
        self.assertIn("case start", view)
        self.assertIn("private var startPane", view)
        self.assertIn("Hide Get Started", view)
        self.assertIn("Remote access is set up, so ChatGPT can reach this Mac over HTTPS.", view)
        menu = MENU.read_text(encoding="utf-8")
        self.assertIn("openSettings(.start)", menu)
        self.assertIn("OnboardingChecklist.shouldShowReminder", menu)


if __name__ == "__main__":
    unittest.main()
