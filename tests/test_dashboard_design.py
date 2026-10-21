"""The dashboard's look: a macOS window of glass over a wallpaper, with Light, Dark or Device themes,
a sidebar (a tab bar on phones), motion that respects Reduce Motion, and token colours for both themes.
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

    def test_light_theme_redefines_the_glass_and_labels(self) -> None:
        light = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', CSS, re.S).group(1)
        for token in ("--wall-base", "--label", "--label-3", "--glass", "--glass-strong", "--fill", "--separator",
                      "--tint", "--good-text", "--bad-text", "--well", "color-scheme: light"):
            self.assertIn(token, light)


class WindowTests(unittest.TestCase):
    def test_sidebar_and_tab_bar_point_at_real_sections(self) -> None:
        for nav in ('<nav class="sidebar glass"', '<nav class="tabbar glass"'):
            self.assertIn(nav, HTML)
        for section in set(re.findall(r'data-nav="([a-z]+)"', HTML)):
            self.assertIn(f'id="{section}"', HTML)
        self.assertIn('link.setAttribute("aria-current", "true")', JS)

    def test_glass_has_the_safari_prefix(self) -> None:
        glass = re.search(r"\n\.glass \{(.*?)\n\}", CSS, re.S).group(1)
        self.assertIn("-webkit-backdrop-filter", glass)
        self.assertIn("backdrop-filter", glass)

    def test_no_fonts_or_scripts_from_the_network(self) -> None:
        self.assertNotRegex(HTML + CSS, r"https?://(?!www\.w3\.org/2000/svg)")


class MotionTests(unittest.TestCase):
    def test_reduced_motion_stops_everything(self) -> None:
        reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn("animation-duration: .001ms", reduced)
        self.assertIn(".wallpaper i { animation: none; }", reduced)

    def test_no_scroll_reveals_hide_content(self) -> None:
        self.assertNotIn("data-reveal", HTML + CSS + JS)

    def test_metrics_still_start_empty_and_end_on_the_exact_text(self) -> None:
        self.assertIn('<strong id="metricCalls">—</strong>', HTML)
        self.assertIn("t < 1 ? format.format(from.value + (to.value - from.value) * eased) + to.unit : text", JS)

    def test_focus_returns_to_the_row_when_the_opener_was_the_page(self) -> None:
        self.assertIn("state.restoreFocus !== document.body", JS)


if __name__ == "__main__":
    unittest.main()
