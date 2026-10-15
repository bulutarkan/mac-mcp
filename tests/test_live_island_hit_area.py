"""#203: the island's click-through follows its shape even when the pointer does not move.

Clicks pass through the island's large transparent panel everywhere except the island's
current shape. That used to be decided only on mouse moves, so with a resting pointer a
shape change (a peek opening or closing), the first show, or a display change left the
decision stale: the first click could miss the island, or a click beside a shrunken island
could be swallowed instead of reaching the menu bar.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
ISLAND = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")

HARNESS = r'''
import CoreGraphics

@main
struct HitAreaHarness {
    static func main() {
        // The panel spans 760 x 560 at the top centre of a 1470-wide screen (y grows upwards).
        let panel = CGRect(x: 355, y: 396, width: 760, height: 560)
        let rest = CGSize(width: 260, height: 32)
        let peek = CGSize(width: 420, height: 86)

        // A pointer resting beside the notch, inside the peek's shape but outside the rest shape.
        let pointer = CGPoint(x: 735 + 190, y: 956 - 20)
        precondition(IslandHitArea.takesClick(at: pointer, in: panel, size: peek))
        precondition(!IslandHitArea.takesClick(at: pointer, in: panel, size: rest))

        // The shape hangs from the top centre of its container, in panel or view coordinates alike.
        let shape = IslandHitArea.rect(in: panel, size: rest)
        precondition(shape.midX == panel.midX && shape.maxY == panel.maxY && shape.size == rest)
        let local = IslandHitArea.rect(in: CGRect(origin: .zero, size: panel.size), size: rest)
        precondition(local.midX == 380 && local.maxY == 560)

        // Outside the panel nothing is the island's; the shape's edges are.
        precondition(!IslandHitArea.takesClick(at: CGPoint(x: 10, y: 10), in: panel, size: peek))
        precondition(IslandHitArea.takesClick(at: CGPoint(x: shape.minX, y: shape.minY), in: panel, size: rest))
        print("ISLAND_HIT_AREA_PASS")
    }
}
'''


class IslandHitAreaTests(unittest.TestCase):
    def test_the_decision_depends_on_the_current_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "HitAreaHarness.swift"
            binary = Path(td) / "hit-area-harness"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            compiled = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "IslandHitArea.swift"), str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("ISLAND_HIT_AREA_PASS", ran.stdout)

    def test_hit_test_and_click_through_share_one_rule(self) -> None:
        self.assertIn("IslandHitArea.takesClick(at: point, in: bounds, size: model.size)", ISLAND)
        self.assertIn("IslandHitArea.takesClick(at: NSEvent.mouseLocation, in: panel.frame, size: model.size)", ISLAND)
        self.assertNotIn("NSRect(x: frame.midX - size.width / 2", ISLAND)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Sources/IslandHitArea.swift"', build)

    def test_click_through_is_re_decided_without_a_mouse_move(self) -> None:
        show = ISLAND[ISLAND.index("private func showWindow()"):ISLAND.index("private func hideWindow()")]
        self.assertIn("updateMouseThrough(hover: false)", show)
        observe = ISLAND[ISLAND.index("private func observeSystem()"):ISLAND.index("// MARK: Words")]
        screen = observe[observe.index("didChangeScreenParametersNotification"):observe.index("activeSpaceDidChangeNotification")]
        self.assertIn("self.updateMouseThrough(hover: false)", screen)
        for published in ("model.$mode", "model.$notch", "model.$expandedHeight"):
            self.assertIn(published, observe)
        # @Published emits before the value is stored: read the new shape on the next run loop turn.
        self.assertIn(".receive(on: RunLoop.main)", observe)

    def test_only_mouse_moves_change_the_hover_and_nothing_takes_focus(self) -> None:
        update = ISLAND[ISLAND.index("private func updateMouseThrough("):ISLAND.index("private func observeSystem()")]
        self.assertIn("if hover, inside != model.hovering { hoverChanged(inside) }", update)
        self.assertEqual(1, ISLAND.count("addGlobalMonitorForEvents"), "no new global event monitor")
        self.assertIsNone(re.search(r"Timer\.scheduledTimer|\.publish\(every:", ISLAND), "no polling for the pointer")
        self.assertIn("override var canBecomeKey: Bool { false }", ISLAND)
        self.assertIn("override var canBecomeMain: Bool { false }", ISLAND)
        self.assertIn("panel.hidesOnDeactivate = false", ISLAND)


class IslandAppIconTests(unittest.TestCase):
    def test_an_app_named_by_its_executable_gets_its_icon_not_a_placeholder(self) -> None:
        lookup = ISLAND[ISLAND.index("static func image(for key: String)"):ISLAND.index("private struct IconTile")]
        self.assertIn("workspace.runningApplications.first", lookup)
        self.assertIn("app.executableURL?.lastPathComponent", lookup)
        self.assertIn("app.localizedName", lookup)


if __name__ == "__main__":
    unittest.main()
