from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, status

from mcp_server import browser_tabs, tools_browser, tools_browser_agent as agent

ROW = {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
       "native_id": "4242", "title": "Fixture", "url": "https://example.com/a"}
SETTINGS = SimpleNamespace(max_wait_s=30, max_js_result_chars=100_000)


def identity_changed() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "Target tab identity changed before the operation; resolve or observe the tab again.")


class _ScopeCase(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._scope_entries().clear()
        self.scans = 0

        def scan(_browser):
            self.scans += 1
            return [dict(ROW)]

        self.patcher = patch.object(browser_tabs, "_scan", side_effect=scan)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
        self.scans = 0


class ScopedTabResolutionTests(_ScopeCase):
    def test_nested_leases_in_one_transaction_scan_once(self) -> None:
        with browser_tabs.tab_lease("Safari", self.handle, mutation=True) as outer:
            with browser_tabs.tab_lease("Safari", self.handle) as inner:
                self.assertEqual((outer.window_index, outer.tab_index, outer.native_id),
                                 (inner.window_index, inner.tab_index, inner.native_id))
            self.assertEqual((1, 2), tools_browser._resolve_tab_target("Safari", self.handle, 1, None))
            with browser_tabs.tab_lease("Safari", self.handle, mutation=True):
                pass
        self.assertEqual(1, self.scans)
        # The scope ends with the outer lease; the next transaction resolves again.
        with browser_tabs.tab_lease("Safari", self.handle):
            pass
        self.assertEqual(2, self.scans)
        self.assertEqual({}, browser_tabs._scope_entries())

    def test_a_mutation_inside_a_read_scope_runs_its_own_checks(self) -> None:
        with patch.object(browser_tabs, "claim_delegated_resource", return_value=None) as claim:
            with browser_tabs.tab_lease("Safari", self.handle):
                with browser_tabs.tab_lease("Safari", self.handle, mutation=True):
                    pass
                with browser_tabs.tab_lease("Safari", self.handle, mutation=True):
                    pass
        self.assertEqual(2, self.scans, "first mutation re-resolves, the second reuses it")
        claim.assert_called_once()

    def test_identity_failure_drops_the_cached_row(self) -> None:
        with browser_tabs.tab_lease("Safari", self.handle):
            with self.assertRaises(HTTPException):
                with browser_tabs.tab_lease("Safari", self.handle):
                    raise identity_changed()
            self.assertIsNone(browser_tabs.scoped_row("Safari", self.handle))
            with browser_tabs.tab_lease("Safari", self.handle):
                pass
        self.assertEqual(2, self.scans)

    def test_other_threads_never_see_this_transaction_cache(self) -> None:
        seen = []
        with browser_tabs.tab_lease("Safari", self.handle):
            worker = threading.Thread(target=lambda: seen.append(browser_tabs.scoped_row("Safari", self.handle)))
            worker.start()
            worker.join()
        self.assertEqual([None], seen)

    def test_identity_failure_markers(self) -> None:
        closed = HTTPException(status.HTTP_409_CONFLICT, {"error": "tab_target_closed"})
        busy = HTTPException(status.HTTP_409_CONFLICT, {"error": "tab_busy"})
        self.assertTrue(browser_tabs.is_tab_identity_failure(identity_changed()))
        self.assertTrue(browser_tabs.is_tab_identity_failure(closed))
        self.assertFalse(browser_tabs.is_tab_identity_failure(busy))
        self.assertFalse(browser_tabs.is_tab_identity_failure(HTTPException(500, "x")))


class JsRetryTests(_ScopeCase):
    def payload(self) -> str:
        import base64, json
        return base64.b64encode(json.dumps({"ok": True}).encode()).decode()

    def test_guard_refusal_retries_once_against_a_fresh_tab(self) -> None:
        calls = []

        def execute(_browser, _js, target, timeout_s):
            calls.append(target.native_id)
            if len(calls) == 1:
                raise identity_changed()
            return self.payload()

        with patch.object(agent, "_execute_js_for_target", side_effect=execute):
            with browser_tabs.tab_lease("Safari", self.handle, mutation=True):
                result = agent._run_json_js(SETTINGS, "Safari", "1", tab_handle=self.handle)
        self.assertEqual({"ok": True}, result)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, self.scans, "the retry resolved the tab again")

    def test_other_errors_and_repeated_refusals_are_not_retried(self) -> None:
        with patch.object(agent, "_execute_js_for_target", side_effect=HTTPException(500, "boom")) as execute:
            with self.assertRaises(HTTPException):
                agent._run_json_js(SETTINGS, "Safari", "1", tab_handle=self.handle)
        self.assertEqual(1, execute.call_count)
        with patch.object(agent, "_execute_js_for_target", side_effect=identity_changed()) as execute:
            with self.assertRaises(HTTPException):
                agent._run_json_js(SETTINGS, "Safari", "1", tab_handle=self.handle)
        self.assertEqual(2, execute.call_count)

    def test_a_prevalidated_delegated_target_is_never_retried(self) -> None:
        target = browser_tabs._target_from_row({**ROW, "tab_handle": self.handle})
        with patch.object(agent, "_execute_js_for_target", side_effect=identity_changed()) as execute:
            with self.assertRaises(HTTPException):
                agent._run_json_js(SETTINGS, "Safari", "1", tab_handle=self.handle, prevalidated_target=target)
        self.assertEqual(1, execute.call_count)


if __name__ == "__main__":
    unittest.main()
