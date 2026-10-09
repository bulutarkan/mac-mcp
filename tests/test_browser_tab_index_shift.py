from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server import tools_browser
from mcp_server import tools_browser_agent as agent
from mcp_server.tools_browser_agent import _browser_act_locked


def _target(browser: str = "Safari", native_id: str = "4005", tab_index: int = 5) -> browser_tabs.TabTarget:
    return browser_tabs.TabTarget(
        browser=browser, window_index=1, tab_index=tab_index, tab_handle="btab_safari_shift",
        native_id=native_id, title="Checkout", url="https://example.test/checkout",
    )


class TabIdentityGuardScriptTests(unittest.TestCase):
    def test_safari_guard_resolves_by_pid_not_leased_index(self) -> None:
        script = tools_browser._tab_identity_guard(_target())
        first_line = script.splitlines()[0]
        self.assertEqual('set targetMatches to (every tab whose pid is "4005")', first_line)
        self.assertIn('if (count of targetMatches) is 0 then error "MAC_MCP_TAB_TARGET_MISSING"', script)
        self.assertIn("set targetTab to item 1 of targetMatches", script)
        self.assertNotIn("set targetTab to tab 5", script)

    def test_shared_pid_falls_back_to_leased_index_only_when_it_still_matches(self) -> None:
        script = tools_browser._tab_identity_guard(_target())
        self.assertIn("set leasedTab to tab 5", script)
        self.assertIn('if ((pid of leasedTab) as text) is "4005" then set targetTab to leasedTab', script)
        self.assertIn('if targetTab is missing value then error "MAC_MCP_TAB_IDENTITY_CHANGED"', script)

    def test_chrome_guard_resolves_by_tab_id(self) -> None:
        script = tools_browser._tab_identity_guard(_target("Google Chrome", native_id="812"))
        self.assertIn('every tab whose id is "812"', script)
        self.assertNotIn("pid", script)

    def test_guard_without_native_identity_matches_url_and_title(self) -> None:
        script = tools_browser._tab_identity_guard(_target(native_id="0"))
        self.assertIn(
            'every tab whose URL is "https://example.test/checkout" and name is "Checkout"', script,
        )

    def test_chrome_activate_uses_resolved_tab_not_leased_index(self) -> None:
        target = _target("Google Chrome", native_id="812")
        with patch.object(tools_browser, "_tab_lease", return_value=MagicMock(__enter__=lambda s: target, __exit__=lambda *a: False)), \
                patch.object(tools_browser, "require_foreground_authorization"), \
                patch.object(tools_browser, "_require_stable_handle_for_mutation"), \
                patch.object(tools_browser, "_run_osascript", return_value="") as osa:
            tools_browser.browser_activate_tab(None, "chrome", tab_handle=target.tab_handle)
        script = osa.call_args.args[0]
        self.assertIn("if (id of tab candidateIndex) is targetTabId then", script)
        self.assertNotIn("set active tab index to 5", script)


class MissingTabMarkerTests(unittest.TestCase):
    def test_missing_target_marker_maps_to_structured_409(self) -> None:
        proc = MagicMock(returncode=1)
        proc.communicate.return_value = ("", "execution error: MAC_MCP_TAB_TARGET_MISSING (-2700)")
        with patch.object(tools_browser.subprocess, "Popen", return_value=proc):
            with self.assertRaises(HTTPException) as ctx:
                tools_browser._run_osascript("return 1")
        self.assertEqual(409, ctx.exception.status_code)
        self.assertEqual("tab_target_closed", ctx.exception.detail["error"])
        self.assertTrue(ctx.exception.detail["do_not_fallback_to_active_tab"])


def _closed_tab_error() -> HTTPException:
    return HTTPException(409, {"ok": False, "error": "tab_target_closed", "reason_code": "TAB_TARGET_CLOSED", "message": "gone"})


class BrowserActTabLossTests(unittest.TestCase):
    def _act(self, actions, dom_effects, state=None, return_state="compact"):
        effects = list(dom_effects)

        def dom_action(*args, **kwargs):
            effect = effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return dict(effect)

        state_mock = MagicMock(side_effect=state) if isinstance(state, Exception) else MagicMock(return_value=state or {"ok": True, "url": "https://example.test"})
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 5)), \
                patch.object(agent, "_verified_dom_action", side_effect=dom_action) as dom, \
                patch.object(agent, "_run_json_js", state_mock):
            result = _browser_act_locked(
                MagicMock(), "Safari", actions,
                window_index=1, tab_index=5, tab_handle="btab_safari_shift", return_state=return_state,
            )
        return result, dom

    def test_tab_closed_mid_batch_reports_completed_actions_instead_of_failing(self) -> None:
        clicks = [{"type": "click", "element_id": f"e{i}"} for i in range(1, 4)]
        result, dom = self._act(clicks, [
            {"ok": True, "type": "click", "element_id": "e1"},
            {"ok": True, "type": "click", "element_id": "e2"},
            _closed_tab_error(),
        ])
        self.assertFalse(result["ok"])
        self.assertEqual("tab_target_closed", result["error"])
        self.assertEqual(2, result["completed_action_count"])
        self.assertTrue(result["outcome_unknown"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual("tab_target_closed", result["state_error"])
        self.assertNotIn("state", result)
        self.assertEqual([True, True, False], [item["ok"] for item in result["actions"]])
        self.assertEqual("click", result["actions"][2]["type"])
        self.assertEqual(3, dom.call_count)

    def test_raw_invalid_index_after_mutation_is_not_a_500(self) -> None:
        raw = HTTPException(500, "execution error: Safari got an error: Can’t get tab 5 of window 1. Invalid index. (-1719)")
        result, _ = self._act(
            [{"type": "click", "element_id": "e1"}, {"type": "click", "element_id": "e2"}],
            [{"ok": True, "type": "click"}, raw],
        )
        self.assertFalse(result["ok"])
        self.assertEqual("TAB_TARGET_CLOSED", result["reason_code"])
        self.assertEqual(1, result["completed_action_count"])

    def test_tab_lost_before_mutation_dispatch_is_not_outcome_unknown(self) -> None:
        stale = HTTPException(404, {"ok": False, "error": "stale_tab_handle", "message": "closed"})
        with patch.object(agent, "_resolve_tab_target", return_value=(1, 5)), \
                patch.object(agent, "browser_find", side_effect=stale), \
                patch.object(agent, "_verified_dom_action") as dom, \
                patch.object(agent, "_run_json_js") as state:
            result = _browser_act_locked(
                MagicMock(), "Safari", [{"type": "click", "query": "Continue"}],
                window_index=1, tab_index=5, tab_handle="btab_safari_shift",
            )
        self.assertFalse(result["outcome_unknown"])
        self.assertEqual(0, result["completed_action_count"])
        dom.assert_not_called()
        state.assert_not_called()

    def test_last_action_closing_its_tab_keeps_successful_result(self) -> None:
        result, _ = self._act(
            [{"type": "click", "element_id": "close"}],
            [{"ok": True, "type": "click"}],
            state=_closed_tab_error(),
            return_state="none",
        )
        self.assertTrue(result["ok"])
        self.assertEqual("tab_target_closed", result["state_error"])

    def test_unrelated_errors_still_propagate(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._act(
                [{"type": "click", "element_id": "e1"}],
                [HTTPException(500, "AppleScript error: something else")],
            )
        self.assertEqual(500, ctx.exception.status_code)


class TabScanRaceTests(unittest.TestCase):
    _ROW = "1\t1\ttrue\t4005\tCheckout\thttps://example.test/checkout"

    def test_scan_rereads_when_a_tab_vanishes_mid_scan(self) -> None:
        race = RuntimeError("execution error: Safari got an error: Can’t get tab 4 of window 1. Invalid index. (-1719)")
        with patch.object(browser_tabs, "_osascript", side_effect=[race, self._ROW]) as osa:
            rows = browser_tabs._scan("Safari")
        self.assertEqual(2, osa.call_count)
        self.assertEqual("4005", rows[0]["native_id"])

    def test_scan_does_not_retry_unrelated_errors(self) -> None:
        with patch.object(browser_tabs, "_osascript", side_effect=RuntimeError("Not authorized to send Apple events (-1743)")) as osa:
            with self.assertRaises(RuntimeError):
                browser_tabs._scan("Safari")
        self.assertEqual(1, osa.call_count)

    def test_scan_tolerates_windows_without_a_current_tab(self) -> None:
        for browser, accessor in (("Safari", "index of current tab"), ("Google Chrome", "active tab index")):
            with patch.object(browser_tabs, "_osascript", return_value="") as osa:
                browser_tabs._scan(browser)
            script = osa.call_args.args[0]
            self.assertIn(f"try\n                set cur to {accessor}\n            end try", script)


if __name__ == "__main__":
    unittest.main()
