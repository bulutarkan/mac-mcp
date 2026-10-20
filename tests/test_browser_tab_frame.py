"""#79: the tab pulse finds the agent's tab button, or draws nothing.

The server answers where the tab sits in the tab bar; the tab on show, a closed tab, or a button
whose title does not agree all give no frame, so the glow never lands on the wrong tab.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import browser_tab_frame as frame


def button(title, x, y=85, width=290, height=32, subrole="AXTabButton", role="AXRadioButton"):
    return {"role": role, "subrole": subrole, "title": title, "description": title,
            "position": {"x": x, "y": y, "width": width, "height": height}}


TABS = [button("Meta Business Suite", 8), button("bulutarkan/mac-mcp: Let your AI apps", 299),
        button("Kod Ölçeğini Değerlendir", 590), button("bulutarkan/mac-mcp | ghloc", 881)]


class TabButtonTests(unittest.TestCase):
    def test_tab_buttons_are_the_strip_left_to_right(self) -> None:
        nodes = list(reversed(TABS)) + [
            button("Learn more", 600, y=700, subrole="", role="AXLink"),          # page content
            button("Pick", 100, y=600, subrole=""),                                # a radio button in the page
            button("", 1200),                                                      # no label
        ]
        self.assertEqual([tab["title"] for tab in TABS], [node["title"] for node in frame.tab_buttons(nodes, 33)])

    def test_the_tab_at_its_place_when_the_title_agrees(self) -> None:
        self.assertEqual(881, frame.pick_tab_button(TABS, 4, "bulutarkan/mac-mcp | ghloc")["position"]["x"])
        # A shortened button title still agrees with the full one.
        long = [button("bulutarkan/mac-mcp: Let your AI apps use your Mac: a local…", 299)]
        self.assertIsNotNone(frame.pick_tab_button(long, 1, "bulutarkan/mac-mcp: Let your AI apps use your Mac: a local server"))

    def test_moved_or_unknown_tabs(self) -> None:
        # The tab moved: found by its title when only one button has it.
        self.assertEqual(8, frame.pick_tab_button(TABS, 3, "Meta Business Suite")["position"]["x"])
        # Two tabs with that title, or none: nothing.
        twins = TABS + [button("Meta Business Suite", 1172)]
        self.assertIsNone(frame.pick_tab_button(twins, 2, "Meta Business Suite"))
        self.assertIsNone(frame.pick_tab_button(TABS, 2, "Somewhere else"))
        self.assertIsNone(frame.pick_tab_button(TABS, 2, ""))


class TabFrameTests(unittest.TestCase):
    def run_frame(self, row, windows=None, observed=(True, "raw", "")):
        windows = windows if windows is not None else [{"index": 1, "position": {"y": 33}, "on_shown_desktop": True, "window_id": 4242}]
        with patch.object(frame.browser_tabs, "resolve_tab", return_value=(1, row.get("tab_index", 4), row)), \
             patch.object(frame.ax_native, "observe", return_value=observed) as observe, \
             patch("mcp_server.tools_ui._parse_observation", return_value=({"windows": windows, "pid": 98854}, TABS)):
            return frame.tab_frame("safari", "btab_safari_x"), observe

    def test_a_background_tab_gets_its_button_frame_and_window(self) -> None:
        result, observe = self.run_frame({"title": "bulutarkan/mac-mcp | ghloc", "active": False, "tab_index": 4})
        self.assertEqual({"ok": True, "active": False, "frame": {"x": 881.0, "y": 85.0, "width": 290.0, "height": 32.0},
                          "window_id": 4242, "app_pid": 98854}, result)
        self.assertEqual("Safari", observe.call_args.kwargs["app"])

    def test_the_tab_on_show_draws_nothing_and_reads_no_tree(self) -> None:
        result, observe = self.run_frame({"title": "Kod Ölçeğini Değerlendir", "active": True, "tab_index": 3})
        self.assertEqual({"ok": True, "active": True}, result)
        observe.assert_not_called()

    def test_unsure_cases_give_no_frame(self) -> None:
        self.assertEqual("tab_button_not_matched",
                         self.run_frame({"title": "Another page", "active": False, "tab_index": 2})[0]["error"])
        self.assertEqual("window_not_shown", self.run_frame(
            {"title": "bulutarkan/mac-mcp | ghloc", "active": False},
            windows=[{"index": 1, "position": {"y": 33}, "on_shown_desktop": False, "window_id": 1}])[0]["error"])
        self.assertEqual("tab_bar_unavailable", self.run_frame(
            {"title": "bulutarkan/mac-mcp | ghloc", "active": False}, observed=None)[0]["error"])
        with patch.object(frame.browser_tabs, "resolve_tab", side_effect=KeyError("closed")):
            self.assertEqual({"ok": False, "error": "tab_not_found"}, frame.tab_frame("safari", "btab_gone"))
        self.assertEqual({"ok": False, "error": "unsupported_browser"}, frame.tab_frame("firefox", "btab"))


class TabFrameRouteTests(unittest.TestCase):
    def test_route_is_guarded_read_only_and_registered(self) -> None:
        from pathlib import Path

        source = (Path(frame.__file__).resolve().parent / "dashboard_routes.py").read_text(encoding="utf-8")
        route = source[source.index("async def browser_tab_frame("):source.index("async def providers(")]
        self.assertIn("_dashboard_guard(request, dashboard_token)", route)
        self.assertNotIn("foreground_authorization", route)
        self.assertIn('Route("/dashboard/api/browser/tab-frame", browser_tab_frame, methods=["POST"])', source)


if __name__ == "__main__":
    unittest.main()
