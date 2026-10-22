"""ask_user in the menu app: where the question shows follows Settings › Activity & Notifications.

The notch island style grows the question out of the island; every other choice, Off included,
shows it as a card at the top right. The rule and the small helpers around it run in a harness.
"""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
PROMPT = (SOURCES / "OwnerPrompt.swift").read_text(encoding="utf-8")
APP_STATE = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
ISLAND = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")

HARNESS = r'''
import CoreGraphics

@main
struct PlacementHarness {
    static func main() {
        precondition(OwnerPromptPlacement.style(showLiveIsland: true) == .island)
        precondition(OwnerPromptPlacement.style(showLiveIsland: false) == .card)

        // A 1470 x 956 screen whose menu bar is 33 points tall (y grows upwards).
        let screen = CGRect(x: 0, y: 0, width: 1470, height: 956)
        let visible = CGRect(x: 0, y: 0, width: 1470, height: 923)
        let island = OwnerPromptPlacement.frame(style: .island, contentHeight: 220, screen: screen, visible: visible)
        precondition(island.midX == screen.midX && island.maxY == screen.maxY, "hangs from the notch")
        let card = OwnerPromptPlacement.frame(style: .card, contentHeight: 220, screen: screen, visible: visible)
        let m = OwnerPromptPlacement.margin
        precondition(card.maxX - m <= visible.maxX && card.maxX - m > visible.maxX - 20, "at the right edge")
        precondition(card.maxY - m <= visible.maxY, "below the menu bar")
        precondition(card.minX > screen.midX, "on the right half")

        precondition(OwnerPromptPlacement.uniqueName("shot.png", taken: []) == "shot.png")
        precondition(OwnerPromptPlacement.uniqueName("shot.png", taken: ["shot.png", "shot 2.png"]) == "shot 3.png")
        precondition(OwnerPromptPlacement.uniqueName("a/b", taken: []) == "a-b")
        precondition(OwnerPromptPlacement.uniqueName("  ", taken: []) == "File")

        precondition(OwnerPromptPlacement.providerID(sender: "Claude Code", agentProvider: nil) == "claude")
        precondition(OwnerPromptPlacement.providerID(sender: "Codex", agentProvider: nil) == "openai")
        precondition(OwnerPromptPlacement.providerID(sender: "x", agentProvider: "codex") == "openai")
        precondition(OwnerPromptPlacement.providerID(sender: "Someone", agentProvider: nil) == nil)
        precondition(OwnerPromptPlacement.remainingText(75) == "1:15" && OwnerPromptPlacement.remainingText(9) == "9")
        print("OWNER_PROMPT_PLACEMENT_PASS")
    }
}
'''


class OwnerPromptPlacementTests(unittest.TestCase):
    def test_placement_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "PlacementHarness.swift"
            binary = Path(td) / "placement-harness"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            compiled = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "OwnerPromptPlacement.swift"), str(source),
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("OWNER_PROMPT_PLACEMENT_PASS", ran.stdout)

    def test_the_style_follows_the_live_activity_setting(self) -> None:
        self.assertIn("OwnerPromptPlacement.style(showLiveIsland: state?.settings.showLiveIsland == true)", PROMPT)
        self.assertIn("state.settings.$showLiveIsland", PROMPT)

    def test_it_sits_above_the_island_and_the_island_stays_at_rest_beneath(self) -> None:
        self.assertIn("NSWindow.Level.mainMenu.rawValue + 3", PROMPT)
        self.assertIn("NSWindow.Level.mainMenu.rawValue + 2", ISLAND)
        self.assertIn("if promptCovering { go(.rest) }", ISLAND)
        self.assertIn("LiveIslandController.shared.setPromptCovering(model.style == .island)", PROMPT)

    def test_the_app_names_its_stream_and_claims_questions(self) -> None:
        self.assertIn("/dashboard/events?client=menu-app", APP_STATE)
        self.assertIn('kind == "owner_prompt" || kind == "owner_prompt_closed"', APP_STATE)
        self.assertIn('"dashboard/api/owner-prompts/claim"', PROMPT)
        self.assertIn('"dashboard/api/owner-prompts/answer"', PROMPT)
        # It never takes focus by itself: shown with orderFrontRegardless, key only after a click.
        self.assertIn("panel.orderFrontRegardless()", PROMPT)
        self.assertNotIn("makeKeyAndOrderFront", PROMPT)

    def test_build_script_compiles_both_files(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Sources/OwnerPromptPlacement.swift"', build)
        self.assertIn('"${SCRIPT_DIR}/Sources/OwnerPrompt.swift"', build)


if __name__ == "__main__":
    unittest.main()
