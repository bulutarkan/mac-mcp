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
        // Let a same-lane update settle; a slow CI runner once checked mid-resize at 0.20 s.
        await wait(0.50)
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

        // The fade-out ends 2.24 s after the last finish; a slow CI runner may finish it later.
        var hideWaited = 0.0
        while visiblePanel() != nil && hideWaited < 3.0 {
            await wait(0.05)
            hideWaited += 0.05
        }
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
        self._run_harness(harness, "BUBBLE_EPOCH_HARNESS_PASS")

    def test_work_that_starts_while_the_bubble_fades_keeps_it(self) -> None:
        # Roadmap #24: the fade's completion used to hide the panel and reset the new work.
        harness = r'''
import AppKit
import Foundation

@main
struct BubbleFadeHarness {
    @MainActor
    static func main() async {
        let controller = ToolActivityBubbleController.shared
        func visiblePanel() -> NSWindow? { NSApplication.shared.windows.first(where: { $0.isVisible }) }
        func wait(_ seconds: Double) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }

        controller.begin(eventID: "f1", tool: "run_command", description: "First batch", sessionID: "s1")
        await wait(0.30)
        controller.finish(eventID: "f1")
        // The bubble lingers 2 s, then fades for 0.24 s: start new work inside that fade.
        var waited = 0.0
        while (visiblePanel()?.alphaValue ?? 0) > 0.85 && waited < 4.0 {
            await wait(0.01)
            waited += 0.01
        }
        precondition(visiblePanel() != nil, "panel was gone before the fade could be caught")
        controller.begin(eventID: "f2", tool: "read_file", description: "Second batch", sessionID: "s2")
        await wait(0.60)
        guard let panel = visiblePanel() else { fatalError("new work was hidden by the old fade") }
        precondition(panel.alphaValue > 0.95, "panel stayed faded: \(panel.alphaValue)")
        await wait(1.00)
        precondition(visiblePanel() != nil, "panel hid while work was still running")
        controller.hideImmediately()
        print("BUBBLE_FADE_HARNESS_PASS")
    }
}
'''
        self._run_harness(harness, "BUBBLE_FADE_HARNESS_PASS")

    def test_lanes_are_named_by_their_source_and_more_than_three_all_show(self) -> None:
        # Roadmap #26: every lane said "Agent N", the header counted sessions as agents, and lanes past
        # the third were only a "+N more" count.
        harness = r'''
import AppKit
import Foundation

@main
struct BubbleLaneHarness {
    @MainActor
    static func main() async {
        typealias B = ToolActivityBubbleController
        precondition(B.laneLabel(source: "ChatGPT", laneKey: "session:s1", ordinal: 1) == "ChatGPT")
        precondition(B.laneLabel(source: nil, laneKey: "agent:a1", ordinal: 2) == "Agent 2")
        precondition(B.laneLabel(source: "", laneKey: "session:s9", ordinal: 3) == "Session 3")
        precondition(B.headerText(laneKeys: ["session:s1"]) == "1 session")
        precondition(B.headerText(laneKeys: ["session:s1", "agent:a1", "agent:a2"]) == "1 session · 2 agents")
        precondition(B.headerText(laneKeys: ["event:e1", "agent:a1"]) == "1 session · 1 agent")

        let controller = B.shared
        func visiblePanel() -> NSWindow? { NSApplication.shared.windows.first(where: { $0.isVisible }) }
        func wait(_ seconds: Double) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }
        let sources = ["ChatGPT", "Claude Code", "Codex · Fix the tests", "Codex · Review", "Goose agent"]
        for (index, source) in sources.enumerated() {
            controller.begin(eventID: "l\\(index)", tool: "read_file", description: "Lane \\(index) work",
                             sessionID: index < 2 ? "s\\(index)" : nil, agentID: index >= 2 ? "a\\(index)" : nil,
                             source: source)
        }
        await wait(0.6)
        guard let panel = visiblePanel() else { fatalError("panel not visible") }
        let screenHeight = NSScreen.main?.visibleFrame.height ?? 900
        precondition(panel.frame.height < 360 && panel.frame.height < screenHeight, "five lanes made the panel \\(panel.frame.height) tall")
        controller.hideImmediately()
        print("BUBBLE_LANE_HARNESS_PASS")
    }
}
'''
        self._run_harness(harness, "BUBBLE_LANE_HARNESS_PASS")
        source = BUBBLE.read_text(encoding="utf-8")
        self.assertIn("ForEach(model.lanes) { lane in", source)
        self.assertIn("ScrollView(.vertical, showsIndicators: true)", source)
        self.assertNotIn("more working", source)
        self.assertNotIn("agents active", source)
        self.assertIn('model.lanes.map { "\\($0.label): \\($0.description)" }', source)
        state = (ROOT / "menu_app" / "Sources" / "AppState.swift").read_text(encoding="utf-8")
        self.assertIn("source: bubbleSource(agentID: payload.agentID, client: payload.client)", state)

    def _run_harness(self, harness: str, marker: str) -> None:
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
            self.assertIn(marker, ran.stdout)


if __name__ == "__main__":
    unittest.main()
