"""The monogram in the native app: menu bar template, menu header and Live Island (#187, #204)."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


class MenuBarMarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")

    def test_the_menu_bar_shows_the_monogram_as_a_template(self) -> None:
        self.assertIn("Image(nsImage: MenuBarMark.image(menuBarLook))", self.app)
        self.assertIn("isTemplate = true", self.app)  # macOS colours it for light, dark and highlighted bars
        self.assertNotIn('"server.rack"', self.app)
        self.assertNotIn('"cpu"', self.app)

    def test_states_use_alpha_and_a_dot_never_colour(self) -> None:
        self.assertIn("if !state.serverRunning { return .stopped }", self.app)
        self.assertIn("fraction: look == .stopped ? 0.4 : 1", self.app)
        self.assertIn("return .working(hollow: state.pulse)", self.app)

    def test_the_dot_sits_clear_of_the_m_and_the_width_never_changes(self) -> None:
        # The dot over the right leg read as "Mi"; it sits to the right, and every look is 22 pt wide
        # so the other menu bar icons never move when work starts or stops.
        self.assertIn("private static let size = NSSize(width: 22, height: 18)", self.app)
        self.assertIn("base.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18)", self.app)
        self.assertIn("let dot = NSRect(x: 18.6, y: 13", self.app)

    def test_both_resolutions_are_loaded(self) -> None:
        self.assertIn('"MenuBarMark"', self.app)
        self.assertIn('"MenuBarMark@2x"', self.app)


class HeaderAndIslandTests(unittest.TestCase):
    def test_menu_header_is_the_bare_mark_in_the_right_tone(self) -> None:
        view = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        header = view[view.index("private var header: some View"):][:900]
        self.assertIn('BrandArt.image(colorScheme == .dark ? "BrandMark" : "BrandMarkLight")', header)
        self.assertNotIn("applicationIconImage", header)
        self.assertNotIn("clipShape", header)

    def test_live_island_uses_the_champagne_mark(self) -> None:
        island = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")
        self.assertIn('static let icon: NSImage = BrandArt.image("BrandMark")', island)

    def test_the_build_bundles_every_mark(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        for name in ("BrandMark.png", "BrandMarkLight.png", "MenuBarMark.png", "MenuBarMark@2x.png"):
            self.assertIn(name, build)


if __name__ == "__main__":
    unittest.main()


class TourMarkTests(unittest.TestCase):
    """The tour's own art uses the monogram; its macOS mock-ups show the app icon, as macOS would."""

    def setUp(self) -> None:
        self.page = (ROOT / "menu_app" / "Onboarding" / "index.html").read_text(encoding="utf-8")

    def test_hero_images_and_the_power_ring_use_the_vector_mark(self) -> None:
        self.assertIn('<img class="hero-icon" src="../Assets/BrandMark.svg" alt="">', self.page)
        self.assertIn('<img class="hero-icon small" src="../Assets/BrandMark.svg" alt="">', self.page)
        ring = self.page[self.page.index('<div class="ring-wrap">'):][:400]
        self.assertIn('src="../Assets/BrandMark.svg"', ring)

    def test_macos_mockups_keep_the_app_icon(self) -> None:
        approval = self.page[self.page.index('<div class="approval">'):][:200]
        self.assertIn('src="../Assets/AppIcon.png"', approval)
        self.assertEqual(2, self.page.count('<img src="../Assets/AppIcon.png" alt=""><span>Mac MCP</span>'))

    def test_the_build_ships_the_vector_mark(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("BrandMark.svg", build)
        self.assertTrue((ROOT / "menu_app" / "Assets" / "BrandMark.svg").is_file())
