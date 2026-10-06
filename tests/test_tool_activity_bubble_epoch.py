from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUBBLE = ROOT / "menu_app" / "Sources" / "ToolActivityBubbleController.swift"


@unittest.skipUnless(sys.platform == "darwin", "requires macOS AppKit")
class ToolActivityBubbleEpochTests(unittest.TestCase):
    def test_expanded_epoch_stays_stable_until_all_activity_hides(self) -> None:
        harness = r'''
import AppKit
import Foundation

@main
struct BubbleEpochHarness {
    @MainActor
    static func main() async {
        let controller = ToolActivityBubbleController.shared

        func visiblePanel() -> NSWindow? {
            NSApplication.shared.windows.first(where: { $0.isVisible })
        }

        func wait(_ seconds: Double) async {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        }

        func sameSize(_ lhs: NSSize, _ rhs: NSSize) -> Bool {
            abs(lhs.width - rhs.width) < 1 && abs(lhs.height - rhs.height) < 1
        }

        controller.begin(
            eventID: "e1",
            tool: "run_command",
            description: "Checking first session",
            sessionID: "s1"
        )
        await wait(0.20)
        guard let compact = visiblePanel() else {
            fatalError("compact panel not visible")
        }
        let compactSize = compact.frame.size

        controller.begin(
            eventID: "e2",
            tool: "read_file",
            description: "Reviewing second session",
            sessionID: "s2"
        )
        await wait(0.20)
        guard let expanded = visiblePanel() else {
            fatalError("expanded panel not visible")
        }
        let expandedSize = expanded.frame.size
        precondition(expandedSize.width > compactSize.width)
        precondition(expandedSize.height > compactSize.height)

        controller.begin(
            eventID: "e3",
            tool: "browser_observe",
            description: "Updating second session",
            sessionID: "s2"
        )
        controller.finish(eventID: "e2")
        await wait(0.20)
        guard let sameExpanded = visiblePanel() else {
            fatalError("panel disappeared during same-lane update")
        }
        precondition(sameSize(sameExpanded.frame.size, expandedSize))

        controller.finish(eventID: "e1")
        await wait(0.90)
        guard let afterFirstFinish = visiblePanel() else {
            fatalError("panel disappeared while second session remained active")
        }
        precondition(sameSize(afterFirstFinish.frame.size, expandedSize))

        controller.finish(eventID: "e3")
        await wait(1.20)
        guard let lingering = visiblePanel() else {
            fatalError("panel did not honor two-second post-activity linger")
        }
        precondition(sameSize(lingering.frame.size, expandedSize))

        await wait(1.15)
        precondition(visiblePanel() == nil)

        controller.begin(
            eventID: "e4",
            tool: "run_command",
            description: "Starting next epoch",
            sessionID: "s4"
        )
        await wait(0.20)
        guard let nextCompact = visiblePanel() else {
            fatalError("next compact epoch not visible")
        }
        precondition(sameSize(nextCompact.frame.size, compactSize))

        controller.hideImmediately()
        print("BUBBLE_EPOCH_HARNESS_PASS")
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "BubbleEpochHarness.swift"
            binary = Path(td) / "bubble-epoch-harness"
            source.write_text(textwrap.dedent(harness), encoding="utf-8")
            compiled = subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    "-parse-as-library",
                    str(BUBBLE),
                    str(source),
                    "-framework",
                    "AppKit",
                    "-framework",
                    "SwiftUI",
                    "-framework",
                    "Combine",
                    "-o",
                    str(binary),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("BUBBLE_EPOCH_HARNESS_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
