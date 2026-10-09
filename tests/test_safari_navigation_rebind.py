from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from mcp_server import browser_tabs
from mcp_server import tools_browser_agent as agent


def _row(tab_index, pid, url, window_index=1, title="t"):
    return {
        "browser": "Safari", "window_index": window_index, "tab_index": tab_index,
        "active": tab_index == 1, "native_id": str(pid), "title": title, "url": url,
    }


class SafariNavigationRebindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._registry = dict(browser_tabs._REGISTRY)
        self._expected = dict(browser_tabs._EXPECTED_NAVIGATIONS)
        browser_tabs._REGISTRY.clear()
        browser_tabs._EXPECTED_NAVIGATIONS.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._REGISTRY.update(self._registry)
        browser_tabs._EXPECTED_NAVIGATIONS.clear()
        browser_tabs._EXPECTED_NAVIGATIONS.update(self._expected)

    def _scan(self, rows):
        with patch.object(browser_tabs, "_scan", return_value=[dict(r) for r in rows]):
            return {r["tab_index"]: r["tab_handle"] for r in browser_tabs.list_tabs("Safari")}

    def _start(self):
        handles = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 200, "https://example.com/")])
        return handles[1], handles[2]

    def test_process_swap_after_own_navigation_keeps_the_same_handle(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target, expected_url="https://www.wikipedia.org/")
        after = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://www.wikipedia.org/")])
        self.assertEqual(target, after[2])
        self.assertEqual("300", browser_tabs._REGISTRY[target]["native_id"])

    def test_without_own_navigation_a_new_pid_is_still_a_new_identity(self) -> None:
        _, target = self._start()
        after = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://www.wikipedia.org/")])
        self.assertNotEqual(target, after[2])
        self.assertNotIn(target, browser_tabs._REGISTRY)

    def test_unexpected_site_is_not_adopted(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target, expected_url="https://www.wikipedia.org/")
        after = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://evil.test/")])
        self.assertNotEqual(target, after[2])

    def test_click_navigation_without_known_url_is_adopted_by_position(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target)
        after = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://news.example.net/")])
        self.assertEqual(target, after[2])

    def test_changed_tab_count_or_position_is_not_adopted(self) -> None:
        first, target = self._start()
        browser_tabs.expect_safari_navigation(target)
        opened_tab = self._scan([
            _row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://www.wikipedia.org/"),
            _row(3, 400, "https://other.test/"),
        ])
        self.assertNotIn(target, opened_tab.values())

    def test_old_pid_still_alive_elsewhere_is_not_a_swap(self) -> None:
        first, target = self._start()
        browser_tabs.expect_safari_navigation(target)
        reordered = self._scan([_row(1, 200, "https://example.com/"), _row(2, 300, "https://www.wikipedia.org/")])
        self.assertEqual(target, reordered[1])
        self.assertNotEqual(target, reordered[2])

    def test_expired_marker_is_ignored(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target)
        browser_tabs._EXPECTED_NAVIGATIONS[target]["expires_at"] = 0
        after = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://www.wikipedia.org/")])
        self.assertNotEqual(target, after[2])

    def test_redirect_chain_swaps_twice_within_ttl(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target, expected_url="https://wikipedia.org/")
        self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://wikipedia.org/")])
        again = self._scan([_row(1, 100, "https://docs.example.org/a"), _row(2, 301, "https://www.wikipedia.org/wiki")])
        self.assertEqual(target, again[2])

    def test_resolve_tab_after_swap_succeeds(self) -> None:
        _, target = self._start()
        browser_tabs.expect_safari_navigation(target, expected_url="https://www.wikipedia.org/")
        with patch.object(browser_tabs, "_scan", return_value=[
            _row(1, 100, "https://docs.example.org/a"), _row(2, 300, "https://www.wikipedia.org/"),
        ]):
            window_index, tab_index, row = browser_tabs.resolve_tab("Safari", target)
        self.assertEqual((1, 2, "300"), (window_index, tab_index, row["native_id"]))

    def test_site_keeps_country_code_second_level_domains_apart(self) -> None:
        self.assertEqual("example.com", browser_tabs._site("https://www.example.com/x"))
        self.assertEqual("ckhealth.com.tr", browser_tabs._site("https://a.ckhealth.com.tr/"))
        self.assertNotEqual(browser_tabs._site("https://a.com.tr/"), browser_tabs._site("https://b.com.tr/"))
        self.assertIsNone(browser_tabs._site("favorites://"))


class BrowserActMarksPossibleNavigationTests(unittest.TestCase):
    def test_marker_is_set_before_the_click_is_dispatched(self) -> None:
        order = []
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_run_json_js", return_value={"ok": True}), \
                patch.object(agent, "_verified_dom_action",
                             side_effect=lambda *a, **k: order.append("click") or {"ok": True, "type": "click", "_js_calls": 1}), \
                patch.object(agent.browser_tabs, "expect_safari_navigation", side_effect=lambda *a, **k: order.append("mark")):
            agent._browser_act_locked(
                MagicMock(), "Safari", [{"type": "click", "element_id": "e1"}, {"type": "type", "element_id": "e2", "text": "x"}],
                window_index=1, tab_index=1, tab_handle="tab-1", return_state="none",
            )
        self.assertEqual(["mark", "click", "click"], order)

    def test_click_marks_safari_tab_before_dispatch_even_if_it_fails(self) -> None:
        for ok, expected_calls in ((True, 1), (False, 1)):
            with self.subTest(ok=ok), \
                    patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                    patch.object(agent, "_run_json_js", return_value={"ok": True}), \
                    patch.object(agent, "_verified_dom_action", return_value={"ok": ok, "type": "click", "_js_calls": 1}), \
                    patch.object(agent.browser_tabs, "expect_safari_navigation") as expect:
                agent._browser_act_locked(
                    MagicMock(), "Safari", [{"type": "click", "element_id": "e1"}],
                    window_index=1, tab_index=1, tab_handle="tab-1", return_state="none",
                )
            self.assertEqual(expected_calls, expect.call_count)

    def test_chrome_tabs_are_not_marked(self) -> None:
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_run_json_js", return_value={"ok": True}), \
                patch.object(agent, "_verified_dom_action", return_value={"ok": True, "type": "click", "_js_calls": 1}), \
                patch.object(agent.browser_tabs, "expect_safari_navigation") as expect:
            agent._browser_act_locked(
                MagicMock(), "Google Chrome", [{"type": "click", "element_id": "e1"}],
                window_index=1, tab_index=1, tab_handle="tab-1", return_state="none",
            )
        expect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
