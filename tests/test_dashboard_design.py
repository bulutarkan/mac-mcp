"""The dashboard's look: Light, Dark or Device themes, motion that respects Reduce Motion, and
token-based colours so both themes stay whole.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"
HTML = (DASHBOARD / "index.html").read_text(encoding="utf-8")
CSS = (DASHBOARD / "dashboard.css").read_text(encoding="utf-8")
JS = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")


class ThemeTests(unittest.TestCase):
    def test_three_choices_and_a_choice_made_before_the_first_paint(self) -> None:
        for choice in ("light", "dark", "auto"):
            self.assertIn(f'data-theme-choice="{choice}" aria-pressed="false"', HTML)
        head = HTML[:HTML.index("</head>")]
        self.assertLess(head.index('localStorage.getItem("mac_mcp_dashboard_theme")'), head.index("dashboard.css"))
        self.assertIn('<meta name="color-scheme" content="light dark">', head)
        # The page root carries the mode under another name, so it is never taken for a switch button.
        self.assertIn("dataset.themeMode = choice", head)
        self.assertNotIn("documentElement.dataset.themeChoice", HTML + JS)
        self.assertNotIn("root.dataset.themeChoice", JS)

    def test_storage_failures_do_not_break_the_page(self) -> None:
        self.assertIn('try { choice = window.localStorage.getItem("mac_mcp_dashboard_theme") || "auto"; } catch {}', HTML)
        self.assertIn("try { window.localStorage.setItem(THEME_KEY, choice); } catch", JS)

    def test_device_mode_follows_macos_and_the_sweep_is_optional(self) -> None:
        self.assertIn('systemLight.addEventListener("change"', JS)
        self.assertIn("!document.startViewTransition || reducedMotion.matches", JS)

    def test_light_theme_redefines_the_colours(self) -> None:
        light = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', CSS, re.S).group(1)
        for token in ("--text", "--muted", "--surface", "--line", "--canvas", "--card-top", "--code-bg", "--drawer", "color-scheme: light"):
            self.assertIn(token, light)


class MotionTests(unittest.TestCase):
    def test_reveals_are_shown_without_motion(self) -> None:
        reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn("[data-reveal] { opacity: 1;", reduced)
        self.assertIn('revealTargets.forEach((el) => el.classList.add("is-in"))', JS)

    def test_scroll_linked_css_has_a_fallback(self) -> None:
        self.assertIn("@supports (animation-timeline: scroll())", CSS)
        self.assertIn("if (!scrollLinkedCSS && scrollProgress)", JS)

    def test_metrics_still_start_empty_and_end_on_the_exact_text(self) -> None:
        self.assertIn('<strong id="metricCalls">—</strong>', HTML)
        self.assertIn("t < 1 ? format.format(from.value + (to.value - from.value) * eased) + to.unit : text", JS)


if __name__ == "__main__":
    unittest.main()
