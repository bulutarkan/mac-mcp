"""Hover captions on the menu chips and rows.

The system tooltip often never appears in the menu-bar panel. One caption is drawn over the whole
panel, below (or near the bottom, above) the hovered chip or row, kept inside the panel, animated,
and it must not change the menu's measured height. The chip and the Conversations row share one
count, so an idle conversation is not labeled ended on one of them.
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
MENU = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
TIP = (SOURCES / "MenuHoverTip.swift").read_text(encoding="utf-8")

HARNESS = r'''
import Foundation

@main
struct MenuHoverHarness {
    static func main() {
        precondition(MenuHoverCopy.toolCalls(count: 0) == "No actions in the last hour")
        precondition(MenuHoverCopy.toolCalls(count: 1) == "1 action in the last hour")
        precondition(MenuHoverCopy.toolCalls(count: 13) == "13 actions in the last hour")

        precondition(MenuHoverCopy.success(calls: 0, rate: 100) == "No actions in the last hour")
        precondition(MenuHoverCopy.success(calls: 1, rate: 100) == "The call succeeded")
        precondition(MenuHoverCopy.success(calls: 13, rate: 100) == "All 13 succeeded")
        precondition(MenuHoverCopy.success(calls: 13, rate: 99.6) == "All 13 succeeded", "rounded percent is 100")
        precondition(MenuHoverCopy.success(calls: 10, rate: 80) == "8 of 10 succeeded")
        precondition(MenuHoverCopy.success(calls: 4, rate: 0) == "None of 4 succeeded")
        precondition(MenuHoverCopy.success(calls: 1, rate: 0) == "The call failed")

        precondition(MenuHoverCopy.agents(running: 0) == "No agents working")
        precondition(MenuHoverCopy.agents(running: 1) == "1 agent working")
        precondition(MenuHoverCopy.agents(running: 3) == "3 agents working")

        precondition(MenuHoverCopy.conversationCount(active: 2, idle: 1, ended: 1) == "2 active")
        precondition(MenuHoverCopy.conversationCount(active: 0, idle: 1, ended: 1) == "1 idle")
        precondition(MenuHoverCopy.conversationCount(active: 0, idle: 0, ended: 4) == "4 ended")
        precondition(MenuHoverCopy.conversationCount(active: 0, idle: 0, ended: 0) == "0 ended")

        precondition(MenuHoverCopy.conversations(active: 0, idle: 1, ended: 1, reliable: true) == "1 conversation idle")
        precondition(MenuHoverCopy.conversations(active: 2, idle: 0, ended: 0, reliable: true) == "2 conversations active")
        precondition(MenuHoverCopy.conversations(active: 1, idle: 0, ended: 0, reliable: true) == "1 conversation active")
        precondition(MenuHoverCopy.conversations(active: 0, idle: 0, ended: 1, reliable: true) == "1 conversation ended")
        precondition(MenuHoverCopy.conversations(active: 0, idle: 0, ended: 3, reliable: true) == "3 conversations ended")
        precondition(MenuHoverCopy.conversations(active: 0, idle: 0, ended: 0, reliable: true) == "No conversations")
        precondition(MenuHoverCopy.conversations(active: 1, idle: 1, ended: 1, reliable: false) == "Conversations unavailable")
        print("MENU_HOVER_PASS")
    }
}
'''


class MenuHoverTipTests(unittest.TestCase):
    def test_one_caption_is_drawn_over_the_whole_panel(self) -> None:
        # Targets only report where they are; the caption is drawn once, over the panel, outside the
        # scroll view and the measured stack, so it is never clipped, hidden or squeezed to a chip.
        self.assertIn(".anchorPreference(key: MenuHoverTipKey.self, value: .bounds)", TIP)
        self.assertNotIn(".overlay(alignment: .topLeading)", TIP)
        panel = MENU[MENU.index(".frame(width: 400, height: menuHeight, alignment: .top)"):]
        self.assertIn(".overlayPreferenceValue(MenuHoverTipKey.self) { target in MenuHoverTipLayer(target: target) }",
                      panel.split(".background(HostWindowReader", 1)[0])
        measured = MENU.split(".background(GeometryReader { box in", 1)[1].split("})", 1)[0]
        self.assertIn("MenuContentHeight.self", measured)
        self.assertNotIn("menuHoverTip", measured)
        self.assertNotIn("@State ", TIP)

    def test_caption_is_placed_inside_the_panel_and_takes_no_clicks(self) -> None:
        layer = TIP[TIP.index("struct MenuHoverTipLayer"):TIP.index("struct MenuHoverCaption")]
        self.assertIn(".allowsHitTesting(false)", layer)
        self.assertIn("static func placesBelow", layer)
        self.assertIn("min(max(centred, margin), max(margin, panelWidth - width - margin))", layer)
        caption = TIP[TIP.index("struct MenuHoverCaption"):]
        self.assertIn("text.lineLimit(1).fixedSize()", caption)
        self.assertIn(".frame(width: Self.wrapWidth, alignment: .leading)", caption)

    def test_caption_moves_and_respects_reduce_motion(self) -> None:
        self.assertIn("static let delay: TimeInterval = 0.3", TIP)
        self.assertIn("Date().timeIntervalSince(lastHidden) < 0.45 ? 0 : MenuHoverTip.delay", TIP)
        layer = TIP[TIP.index("struct MenuHoverTipLayer"):TIP.index("struct MenuHoverCaption")]
        self.assertIn("accessibilityReduceMotion", layer)
        self.assertIn(".transition(transition(below: below))", layer)
        self.assertIn("guard !reduceMotion else { return .opacity }", layer)
        self.assertIn("value: target?.id", layer)

    def test_chips_and_rows_say_the_shared_sentences(self) -> None:
        chips = MENU[MENU.index("private var chipsRow"):MENU.index("private var successTint")]
        self.assertIn("MenuHoverCopy.toolCalls(count: state.totalCalls)", chips)
        self.assertIn("MenuHoverCopy.success(calls: state.totalCalls, rate: state.successRate)", chips)
        # #123: agents and conversations are counted in their own rows, not repeated in chips.
        self.assertNotIn("MenuHoverCopy.agents(running:", chips)
        self.assertNotIn("MenuHoverCopy.conversations(", chips)
        chip = MENU[MENU.index("private struct MenuChip"):MENU.index("private struct QuotaBadges")]
        self.assertIn(".menuHoverTip(caption)", chip)
        self.assertNotIn(".help(", chip)

        count = MENU[MENU.index("private var sessionsChipText"):MENU.index("private var actionBar")]
        self.assertIn("MenuHoverCopy.conversationCount(", count)
        label = MENU[MENU.index('Label("Conversations"'):MENU.index(".disclosureGroupStyle(MenuDisclosureStyle())")]
        self.assertIn("sessionsChipText", label)
        self.assertNotIn("sessionEndedCount > 0", label)
        self.assertIn("Mac MCP logical session", MENU)
        self.assertNotIn("pending approval", MENU.lower())

    def test_build_compiles_the_caption_before_the_menu(self) -> None:
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        tip = '"${SCRIPT_DIR}/Sources/MenuHoverTip.swift"'
        menu = '"${SCRIPT_DIR}/Sources/MenuBarView.swift"'
        self.assertIn(tip, script)
        self.assertLess(script.index(tip), script.index(menu))


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class MenuHoverCopyTests(unittest.TestCase):
    def test_sentences_match_the_chip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "MenuHoverHarness.swift"
            binary = Path(td) / "menu-hover"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                [
                    "xcrun", "swiftc", "-parse-as-library",
                    str(SOURCES / "SwiftUICompat.swift"),
                    str(SOURCES / "MenuHoverTip.swift"),
                    str(harness),
                    "-framework", "SwiftUI", "-framework", "AppKit",
                    "-o", str(binary),
                ],
                cwd=ROOT, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("MENU_HOVER_PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
