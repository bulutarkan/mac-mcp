"""Native scroll uses the AXScroll<Direction>ByPage action that scroll areas expose."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_ui


class NativeScrollActionNameTests(unittest.TestCase):
    def test_scroll_prefers_the_by_page_action_the_element_offers(self) -> None:
        scripts = []

        def run(script, timeout_s=None):
            scripts.append(script)
            return True, "", ""

        with patch.object(tools_ui, "_run_osascript", side_effect=run):
            ok, _ = tools_ui._scroll("MacMCPMenu", {"direction": "down", "element_id": "w2/1/16", "pages": 2},
                                     None, app_pid=4578, activate_target=False)
        self.assertTrue(ok)
        script = scripts[0]
        self.assertIn('contains "AXScrollDownByPage" then set scrollAction to "AXScrollDownByPage"', script)
        self.assertIn('set scrollAction to "AXScrollDown"', script)
        self.assertIn("perform action scrollAction of targetElement", script)
        self.assertIn("repeat 2 times", script)


class SameAppNameTests(unittest.TestCase):
    record = {"app_name": "Mac MCP", "bundle_id": "com.bulutarkan.mac-mcp.menu", "pid": 4578}

    def test_displayed_name_bundle_id_and_executable_all_name_the_app(self) -> None:
        def ps(argv, **kwargs):
            return type("R", (), {"stdout": "/Users/x/Applications/Mac MCP.app/Contents/MacOS/MacMCPMenu\n"})()

        self.assertTrue(tools_ui._same_app("mac mcp", self.record))
        self.assertTrue(tools_ui._same_app("com.bulutarkan.mac-mcp.menu", self.record))
        with patch.object(tools_ui.subprocess, "run", side_effect=ps):
            self.assertTrue(tools_ui._same_app("MacMCPMenu", self.record))
            self.assertFalse(tools_ui._same_app("Safari", self.record))


if __name__ == "__main__":
    unittest.main()
