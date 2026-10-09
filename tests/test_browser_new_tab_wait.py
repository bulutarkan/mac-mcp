from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_browser_agent as agent


def _row(handle, url="https://example.com/x", title="X"):
    return {"tab_handle": handle, "url": url, "title": title, "window_index": 1, "tab_index": 2, "active": False}


class NewTabWaitTests(unittest.TestCase):
    def _wait(self, scans, **action):
        calls = iter(scans)
        with patch.object(agent.browser_tabs, "list_tabs", side_effect=lambda browser: next(calls)), \
                patch.object(agent, "cancellable_sleep", lambda seconds: None):
            return agent._wait_new_tab("Google Chrome", {"_tabs_before": ["own", "other"], "timeout_s": 5, **action}, "own")

    def test_returns_the_tab_the_page_opened_without_activating_it(self) -> None:
        result = self._wait([[_row("own"), _row("other")], [_row("own"), _row("other"), _row("new", "https://idp.example/login")]])
        self.assertTrue(result["matched"])
        self.assertEqual("new", result["new_tab"]["tab_handle"])
        self.assertFalse(result["new_tab"]["active"])
        self.assertFalse(result["url_pending"])
        self.assertEqual(2, result["tab_scans"])

    def test_blank_popup_is_given_time_to_navigate(self) -> None:
        result = self._wait([[_row("own"), _row("new", "about:blank")], [_row("own"), _row("new", "https://idp.example/auth")]])
        self.assertEqual("https://idp.example/auth", result["new_tab"]["url"])

    def test_several_new_tabs_are_ambiguous_unless_narrowed(self) -> None:
        tabs = [_row("own"), _row("a", "https://ads.example/"), _row("b", "https://idp.example/login")]
        ambiguous = self._wait([tabs])
        self.assertFalse(ambiguous["matched"])
        self.assertEqual("ambiguous_new_tabs", ambiguous["error"])
        self.assertEqual({"a", "b"}, {row["tab_handle"] for row in ambiguous["candidates"]})
        narrowed = self._wait([tabs], url_contains="idp.example")
        self.assertEqual("b", narrowed["new_tab"]["tab_handle"])

    def test_no_new_tab_times_out_with_a_hint(self) -> None:
        clock = iter(range(0, 1000, 2))
        with patch.object(agent.time, "perf_counter", side_effect=lambda: float(next(clock))):
            result = self._wait([[_row("own")]] * 10, timeout_s=5)
        self.assertFalse(result["matched"])
        self.assertTrue(result["timed_out"])
        self.assertEqual("NO_NEW_TAB", result["reason_code"])

    def test_batch_snapshots_tabs_before_running_actions(self) -> None:
        captured = {}

        def fake_wait(settings, browser, action, *args, **kwargs):
            captured["before"] = action.get("_tabs_before")
            return {"ok": True, "type": "wait", "matched": True}

        with patch.object(agent.browser_tabs, "list_tabs", return_value=[_row("own"), _row("other")]), \
                patch.object(agent, "_resolve_tab_target", return_value=(1, 1)), \
                patch.object(agent, "_wait_action", side_effect=fake_wait), \
                patch.object(agent, "_run_json_js", return_value={"ok": True, "url": "https://example.com"}):
            try:
                agent._browser_act_locked(None, "Google Chrome", [{"type": "wait", "for": "new_tab"}],
                                          tab_handle="own", return_state="none")
            except Exception:
                pass
        self.assertEqual(["other", "own"], captured.get("before"))


if __name__ == "__main__":
    unittest.main()
