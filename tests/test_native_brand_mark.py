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
        # #123: the look comes from the shared live state; anything but a running server is dimmed.
        live = (SOURCES / "MenuLiveStatus.swift").read_text(encoding="utf-8")
        self.assertIn("guard server == .running else { return .stopped }", live)
        self.assertIn("case .stopped: return .stopped", self.app)
        self.assertIn("fraction: look == .stopped ? 0.4 : 1", self.app)
        self.assertIn("return .working(hollow: state.pulse)", self.app)

    def test_the_dot_sits_clear_of_the_m_and_the_width_never_changes(self) -> None:
        # The dot over the right leg read as "Mi"; it sits to the right, and every look is 22 pt wide
        # so the other menu bar icons never move when work starts or stops.
        self.assertIn("private static let size = NSSize(width: 22, height: 18)", self.app)
        self.assertIn("base.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18)", self.app)
        self.assertIn("let dot = NSRect(x: 18.2, y: 12.6", self.app)

    def test_a_waiting_question_and_paused_observation_show_beside_the_m(self) -> None:
        # One look at a time, most important first; the glyphs sit in the column right of the M.
        look = self.app[self.app.index("private var menuBarLook: MenuBarMark.Look"):][:700]
        order = [look.index(s) for s in ("return .stopped", "return .needsYou", "return .paused", "return .working")]
        self.assertEqual(sorted(order), order)
        # #123: the order lives in the shared live state the popup header also reads.
        live = (SOURCES / "MenuLiveStatus.swift").read_text(encoding="utf-8")
        icon = live[live.index("var iconLook: IconLook"):][:400]
        shared = [icon.index(s) for s in ("return .stopped", "return .needsYou", "return .paused", "return .working")]
        self.assertEqual(sorted(shared), shared)
        self.assertIn("if questionsWaiting > 0 { return .needsYou }", icon)
        self.assertIn("if paused { return .paused }", icon)
        self.assertIn("state.liveStatus(questionsWaiting: prompts.queue.count", self.app)
        self.assertIn("MenuBarLabel(state: state, prompts: OwnerPromptController.shared.model)", self.app)
        self.assertIn("case stopped, idle, needsYou, paused", self.app)

    def test_voiceover_names_every_mode(self) -> None:
        label = self.app[self.app.index("private var menuBarAccessibilityLabel: String"):][:1200]
        for words in ("needs you", "observation paused", "active work", "private mode", "partly working"):
            self.assertIn(f'"{words}"', label)

    def test_both_resolutions_are_loaded(self) -> None:
        self.assertIn('"MenuBarMark"', self.app)
        self.assertIn('"MenuBarMark@2x"', self.app)


class HeaderAndIslandTests(unittest.TestCase):
    def test_menu_header_is_the_bare_mark_in_the_right_tone(self) -> None:
        view = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        header = view[view.index("private var header: some View"):][:900]
        self.assertIn("colorScheme == .dark ? Self.markDark : Self.markLight", header)
        self.assertNotIn("applicationIconImage", header)
        self.assertNotIn("clipShape", header)

    def test_live_island_uses_the_champagne_mark(self) -> None:
        island = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")
        self.assertIn('static let icon: NSImage = BrandArt.image("BrandMark")', island)

    def test_the_build_bundles_every_mark(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        for name in ("BrandMark.png", "BrandMarkLight.png", "MenuBarMark.png", "MenuBarMark@2x.png"):
            self.assertIn(name, build)



class TourMarkTests(unittest.TestCase):
    """The tour's own art uses the monogram; its macOS mock-ups show the app icon, as macOS would."""

    def setUp(self) -> None:
        self.page = (ROOT / "menu_app" / "Onboarding" / "index.html").read_text(encoding="utf-8")

    def test_hero_images_and_the_power_ring_use_the_vector_mark(self) -> None:
        self.assertIn('<img class="hero-icon" src="../Assets/BrandMark.svg" alt="">', self.page)
        self.assertIn('<img class="hero-icon small" src="../Assets/BrandMark.svg" alt="">', self.page)
        ring = self.page[self.page.index('<div class="ring-wrap">'):][:500]
        self.assertIn('src="../Assets/BrandMark.svg"', ring)

    def test_macos_mockups_keep_the_app_icon(self) -> None:
        approval = self.page[self.page.index('<div class="approval">'):][:200]
        self.assertIn('src="../Assets/AppIcon.png"', approval)
        self.assertEqual(2, self.page.count('<img src="../Assets/AppIcon.png" alt=""><span>Mac MCP</span>'))

    def test_the_build_ships_the_vector_mark(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("BrandMark.svg", build)
        self.assertTrue((ROOT / "menu_app" / "Assets" / "BrandMark.svg").is_file())


class ReviewFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        self.view = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")

    def test_the_header_reads_its_marks_once(self) -> None:
        self.assertIn('private static let markDark = BrandArt.image("BrandMark")', self.view)
        self.assertIn('private static let markLight = BrandArt.image("BrandMarkLight")', self.view)
        self.assertIn("Image(nsImage: colorScheme == .dark ? Self.markDark : Self.markLight)", self.view)

    def test_the_hollow_dot_reads_on_a_1x_display(self) -> None:
        self.assertIn("let dot = NSRect(x: 18.2, y: 12.6, width: 3.8, height: 3.8)", self.app)
        self.assertIn("ring.lineWidth = 0.9", self.app)

    def test_the_menu_bar_item_never_vanishes_without_its_art(self) -> None:
        self.assertIn('if image.representations.isEmpty, let symbol = NSImage(systemSymbolName: "cursorarrow"', self.app)

    def test_the_tour_ring_uses_bronze_on_a_light_page(self) -> None:
        page = (ROOT / "menu_app" / "Onboarding" / "index.html").read_text(encoding="utf-8")
        ring = page[page.index('<div class="ring-wrap">'):][:500]
        self.assertIn('<source srcset="../Assets/BrandMarkBronze.svg" media="(prefers-color-scheme: light)">', ring)
        self.assertIn("BrandMarkBronze.svg", (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
