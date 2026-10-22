"""Hover captions on the menu chips and rows.

The system tooltip often never appears in the menu-bar panel. A caption we draw sits above the
row and must not change the menu's measured height. The chip and the Conversations row share one
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
        precondition(MenuHoverCopy.toolCalls(count: 0) == "No tool calls in the last hour")
        precondition(MenuHoverCopy.toolCalls(count: 1) == "1 tool call in the last hour")
        precondition(MenuHoverCopy.toolCalls(count: 13) == "13 tool calls in the last hour")

        precondition(MenuHoverCopy.success(calls: 0, rate: 100) == "No tool calls in the last hour")
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
    def test_caption_is_an_overlay_so_the_menu_does_not_grow(self) -> None:
        self.assertIn(".overlay(alignment: .topLeading)", TIP)
        self.assertIn("dimensions[.bottom] + 6", TIP)
        self.assertIn(".allowsHitTesting(false)", TIP)
        self.assertIn("static let delay: TimeInterval = 0.18", TIP)
        self.assertNotIn("@State ", TIP)
        # The measured stack is unchanged: the caption is not a row in it.
        measured = MENU.split(".background(GeometryReader { box in", 1)[1].split("})", 1)[0]
        self.assertIn("MenuContentHeight.self", measured)
        self.assertNotIn("menuHoverTip", measured)

    def test_chips_and_rows_say_the_shared_sentences(self) -> None:
        chips = MENU[MENU.index("private var chipsRow"):MENU.index("private var successTint")]
        self.assertIn("MenuHoverCopy.toolCalls(count: state.totalCalls)", chips)
        self.assertIn("MenuHoverCopy.success(calls: state.totalCalls, rate: state.successRate)", chips)
        self.assertIn("MenuHoverCopy.agents(running: state.activeAgents)", chips)
        self.assertIn("MenuHoverCopy.conversations(", chips)
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
