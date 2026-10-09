from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import browser_tabs, tools_browser_agent

ROW = {
    "browser": "Safari", "window_index": 1, "tab_index": 1, "tab_handle": "btab_companion",
    "native_id": "native-1", "url": "https://example.test/", "title": "Example", "active": False,
}


class VisualCompanionConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        tools_browser_agent._VISUAL_ENSURE_CACHE.clear()
        self.addCleanup(tools_browser_agent._VISUAL_ENSURE_CACHE.clear)

    def test_concurrent_setup_for_one_tab_injects_at_most_once(self) -> None:
        loaded = threading.Event()
        injections = []
        release = threading.Event()

        def fake_js(_browser, js, _target, timeout_s=0):
            if js == tools_browser_agent._visual_companion_source():
                injections.append(1)
                release.wait(2)
                loaded.set()
                return "OK"
            return "1" if loaded.is_set() else "0"

        results = []
        settings = MagicMock()
        with patch.object(browser_tabs, "resolve_tab", return_value=(1, 1, dict(ROW))), \
             patch.object(tools_browser_agent, "_execute_js_for_target", side_effect=fake_js):
            threads = [
                threading.Thread(target=lambda: results.append(
                    tools_browser_agent._ensure_visual_companion(settings, "Safari", tab_handle="btab_companion")
                ))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            time.sleep(0.2)
            release.set()
            for thread in threads:
                thread.join(5)
            # Once set up, a later call is served from the cache without browser I/O.
            self.assertTrue(tools_browser_agent._ensure_visual_companion(settings, "Safari", tab_handle="btab_companion"))

        self.assertEqual(1, len(injections))
        self.assertEqual(4, len(results))
        self.assertIn(True, results)
        self.assertEqual(1, len(tools_browser_agent._VISUAL_ENSURE_CACHE))


if __name__ == "__main__":
    unittest.main()
