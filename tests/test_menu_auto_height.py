"""#200: the menu popover is as tall as its content and does not jump when a section opens."""
from __future__ import annotations

import unittest
from pathlib import Path

MENU = (Path(__file__).resolve().parents[1] / "menu_app" / "Sources" / "MenuBarView.swift").read_text(encoding="utf-8")


class MenuAutoHeightTests(unittest.TestCase):
    def test_height_follows_the_measured_content_up_to_the_screen(self) -> None:
        self.assertNotIn(".frame(width: 400, height: 660)", MENU)
        self.assertIn(".frame(width: 400, height: menuHeight, alignment: .top)", MENU)
        self.assertIn("MenuContentHeight.self", MENU)
        self.assertIn("NSScreen.main?.visibleFrame.height", MENU)

    def test_the_window_follows_with_its_top_edge_fixed_and_no_second_animation(self) -> None:
        fit = MENU.split("private func fitWindow", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("let top = frame.maxY", fit)
        self.assertIn("frame.origin.y = top - height", fit)
        # Animating the window as well as the content made the box jump up and down.
        self.assertIn("fitWindow(animated: false)", MENU)
        self.assertNotIn("fitWindow(animated: !reduceMotion)", MENU)
        change = MENU.split(".onPreferenceChange(MenuContentHeight.self)", 1)[1].split("}\n", 2)[1]
        self.assertNotIn("withAnimation", change)


if __name__ == "__main__":
    unittest.main()
