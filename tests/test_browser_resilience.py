from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server import browser_tabs
from mcp_server.policy import PolicyContext, reset_policy_context, resolve_risk, set_policy_context
from mcp_server.security_context import SecurityContextManager
from mcp_server.tools_browser_agent import _browser_state_bootstrap, browser_observe
from mcp_server.browser_tabs import TabTarget
from mcp_server import tools_agents
from mcp_server.security import load_settings


class BrowserNoProgressTests(unittest.TestCase):
    def _risk(self, args):
        return resolve_risk("browser_act", args)[1]

    def _observe(self, manager: SecurityContextManager, revision: int = 1) -> None:
        manager.observe_browser_result(
            key="session:test",
            public_session_id="sess-test",
            tool="browser_observe",
            arguments={"browser": "Safari", "tab_handle": "tab-a"},
            result={
                "ok": True,
                "tab_handle": "tab-a",
                "url": "https://example.com/app",
                "title": "SPA",
                "dom_revision": revision,
                "observation_id": f"obs-{revision}",
            },
        )

    def _stale_result(self, revision: int = 1):
        return {
            "ok": False,
            "actions": [{"ok": False, "type": "click", "error": "stale_element"}],
            "state": {
                "url": "https://example.com/app",
                "title": "SPA",
                "dom_revision": revision,
            },
        }

    def test_same_stale_click_is_blocked_before_fifth_execution(self) -> None:
        manager = SecurityContextManager(no_progress_threshold=4)
        self._observe(manager, 1)
        args = {
            "browser": "Safari",
            "tab_handle": "tab-a",
            "actions": [{"type": "click", "element_id": "e_page_1"}],
        }
        for _ in range(4):
            decision = manager.evaluate(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", risk=self._risk(args), arguments=args,
            )
            self.assertTrue(decision.allowed, decision)
            manager.observe_browser_result(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", arguments=args, result=self._stale_result(1),
            )
        blocked = manager.evaluate(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", risk=self._risk(args), arguments=args,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual("browser_no_progress", blocked.code)
        self.assertIn("4 attempts", blocked.target_summary or "")

    def test_dom_revision_progress_resets_breaker_budget(self) -> None:
        manager = SecurityContextManager(no_progress_threshold=3)
        self._observe(manager, 1)
        args = {
            "browser": "Safari", "tab_handle": "tab-a",
            "actions": [{"type": "click", "element_id": "e_page_1"}],
        }
        for _ in range(2):
            self.assertTrue(manager.evaluate(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", risk=self._risk(args), arguments=args,
            ).allowed)
            manager.observe_browser_result(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", arguments=args, result=self._stale_result(1),
            )

        # Same action now changes the DOM; that is progress and clears the budget.
        self.assertTrue(manager.evaluate(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", risk=self._risk(args), arguments=args,
        ).allowed)
        manager.observe_browser_result(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", arguments=args,
            result={"ok": True, "actions": [{"ok": True, "type": "click"}],
                    "state": {"url": "https://example.com/app", "title": "SPA", "dom_revision": 2}},
        )
        for _ in range(2):
            self.assertTrue(manager.evaluate(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", risk=self._risk(args), arguments=args,
            ).allowed)
            manager.observe_browser_result(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", arguments=args, result=self._stale_result(2),
            )
        self.assertTrue(manager.evaluate(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", risk=self._risk(args), arguments=args,
        ).allowed)

    def test_wait_only_actions_never_consume_no_progress_budget(self) -> None:
        manager = SecurityContextManager(no_progress_threshold=2)
        self._observe(manager, 1)
        args = {
            "browser": "Safari", "tab_handle": "tab-a",
            "actions": [{"type": "wait", "for": "selector", "selector": "#ready"}],
        }
        for _ in range(8):
            decision = manager.evaluate(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", risk=self._risk(args), arguments=args,
            )
            self.assertTrue(decision.allowed, decision)
            manager.observe_browser_result(
                key="session:test", public_session_id="sess-test", tool="browser_act",
                arguments=args,
                result={"ok": False, "actions": [{"ok": False, "type": "wait", "timed_out": True}],
                        "state": {"url": "https://example.com/app", "title": "SPA", "dom_revision": 1}},
            )

    def test_different_action_remains_available_after_breaker(self) -> None:
        manager = SecurityContextManager(no_progress_threshold=2)
        self._observe(manager, 1)
        first = {
            "browser": "Safari", "tab_handle": "tab-a",
            "actions": [{"type": "click", "element_id": "e_page_1"}],
        }
        for _ in range(2):
            self.assertTrue(manager.evaluate(
                key="session:test", public_session_id="sess-test",
                tool="browser_act", risk=self._risk(first), arguments=first,
            ).allowed)
            manager.observe_browser_result(
                key="session:test", public_session_id="sess-test", tool="browser_act",
                arguments=first, result=self._stale_result(1),
            )
        self.assertFalse(manager.evaluate(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", risk=self._risk(first), arguments=first,
        ).allowed)
        second = {
            "browser": "Safari", "tab_handle": "tab-a",
            "actions": [{"type": "click", "element_id": "e_page_2"}],
        }
        self.assertTrue(manager.evaluate(
            key="session:test", public_session_id="sess-test",
            tool="browser_act", risk=self._risk(second), arguments=second,
        ).allowed)


class BrowserLogicalLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()
        self.rows = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "7001", "title": "Account", "url": "https://example.com/account",
        }]

    def _context(self, agent_id: str):
        return set_policy_context(PolicyContext(
            profile="browser_only", actor=f"agent:{agent_id}", agent_id=agent_id,
        ))

    def test_release_requires_fresh_rebind_and_preserves_user_tab(self) -> None:
        with patch("mcp_server.browser_tabs._scan", return_value=self.rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            token = self._context("agt_a")
            try:
                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True) as target:
                    self.assertEqual(1, target.lease_generation)
                with browser_tabs.tab_lease("Safari", tab_handle=handle) as target:
                    self.assertEqual("agent:agt_a", target.logical_owner)
            finally:
                reset_policy_context(token)

            self.assertEqual(1, browser_tabs.release_agent_leases("agt_a"))
            # Ownership release must never close/forget a user-owned physical tab.
            self.assertIn(handle, browser_tabs.registry_snapshot())

            token = self._context("agt_b")
            try:
                with self.assertRaises(HTTPException) as ctx:
                    with browser_tabs.tab_lease("Safari", tab_handle=handle):
                        pass
                self.assertEqual(409, ctx.exception.status_code)
                self.assertEqual("tab_rebind_required", ctx.exception.detail["error"])
                self.assertEqual("browser_observe", ctx.exception.detail["required_action"])

                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True) as target:
                    self.assertTrue(target.lease_rebound)
                    self.assertEqual(2, target.lease_generation)
                    self.assertEqual("https://example.com", target.previous_origin)
                with browser_tabs.tab_lease("Safari", tab_handle=handle) as target:
                    self.assertEqual("agent:agt_b", target.logical_owner)
            finally:
                reset_policy_context(token)

    def test_active_agent_lease_cannot_be_stolen(self) -> None:
        with patch("mcp_server.browser_tabs._scan", return_value=self.rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            token = self._context("agt_a")
            try:
                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True):
                    pass
            finally:
                reset_policy_context(token)

            token = self._context("agt_b")
            try:
                with self.assertRaises(HTTPException) as ctx:
                    with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True):
                        pass
                self.assertEqual("tab_owned_by_other_agent", ctx.exception.detail["error"])
            finally:
                reset_policy_context(token)

    def test_expired_lease_behaves_like_orphan_and_needs_rebind(self) -> None:
        with patch("mcp_server.browser_tabs._scan", return_value=self.rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            token = self._context("agt_a")
            try:
                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True):
                    pass
            finally:
                reset_policy_context(token)
            browser_tabs._LOGICAL_LEASES[handle]["expires_at"] = 0

            token = self._context("agt_a")
            try:
                with self.assertRaises(HTTPException) as ctx:
                    with browser_tabs.tab_lease("Safari", tab_handle=handle):
                        pass
                self.assertEqual("tab_rebind_required", ctx.exception.detail["error"])
                with browser_tabs.tab_lease("Safari", tab_handle=handle, allow_rebind=True) as target:
                    self.assertTrue(target.lease_rebound)
                    self.assertEqual(2, target.lease_generation)
            finally:
                reset_policy_context(token)


    def test_rebind_resets_page_side_state_before_fresh_observe(self) -> None:
        target = TabTarget(
            browser="Safari", window_index=1, tab_index=1, tab_handle="tab-a",
            native_id="7001", title="Account", url="https://example.com/account",
            lease_generation=2, logical_owner="agent:agt_b", lease_rebound=True,
            previous_origin="https://example.com",
        )

        @contextmanager
        def fake_lease(*args, **kwargs):
            self.assertTrue(kwargs.get("allow_rebind"))
            yield target

        with patch("mcp_server.tools_browser_agent._ensure_visual_companion", return_value=True), \
             patch("mcp_server.tools_browser_agent._tab_lease", side_effect=fake_lease), \
             patch("mcp_server.tools_browser_agent._execute_js_for_target", return_value="OK") as reset_js, \
             patch("mcp_server.tools_browser_agent._browser_observe_locked", return_value=json.dumps({
                 "ok": True, "url": target.url, "title": target.title,
                 "observation_id": "bobs_new", "dom_revision": 0, "elements": [],
             })):
            result = browser_observe(load_settings(), "Safari", tab_handle="tab-a")
        reset_js.assert_called_once()
        payload = json.loads(result)
        self.assertTrue(payload["lease_rebound"])
        self.assertEqual(2, payload["lease_generation"])
        self.assertEqual("https://example.com", payload["previous_origin"])

    def test_worker_reaper_releases_agent_leases(self) -> None:
        proc = MagicMock()
        proc.wait.return_value = 0
        with patch("mcp_server.tools_agents.browser_tabs.release_agent_leases") as release:
            tools_agents._reap_worker("agt_done", proc)
        proc.wait.assert_called_once()
        release.assert_called_once_with("agt_done")

    def test_element_ids_include_page_token_for_generation_safety(self) -> None:
        bootstrap = _browser_state_bootstrap()
        self.assertIn("'e_'+s.pageToken+'_'", bootstrap)


if __name__ == "__main__":
    unittest.main()
