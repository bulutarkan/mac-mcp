from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from mcp_server import browser_tabs
from mcp_server.computer_plan import execute_computer_plan
from mcp_server.security import load_settings
from mcp_server.tools_browser_agent import _browser_act_locked, _select_action, _verified_dom_action


_HUMAN_TAKEOVER = {
    "ok": False,
    "error": "human_active_resource",
    "reason_code": "HUMAN_ACTIVE_RESOURCE",
    "retryable": True,
    "human_priority": True,
    "yielded": True,
    "human_takeover_during_action": True,
    "resource_kind": "browser_tab",
}


def _ready() -> dict:
    return {
        "ready": True,
        "element_id": "e1",
        "stable_for_ms": 300,
        "dom_revision": 1,
        "rect": {"x": 10, "y": 10, "w": 40, "h": 20},
        "_js_calls": 1,
    }


def _target(
    handle: str = "tab-one",
    generation: int = 7,
    browser: str = "Safari",
) -> browser_tabs.TabTarget:
    return browser_tabs.TabTarget(
        browser=browser,
        window_index=1,
        tab_index=1,
        tab_handle=handle,
        native_id="42",
        title="App",
        url="https://example.test/app",
        active=False,
        lease_generation=generation,
        logical_owner="agent:agt_one",
    )


class BrowserMutationRevalidationTests(unittest.TestCase):
    def _batch_result(self, typ: str) -> dict:
        return {
            "ok": True,
            "actions": [{
                "ok": True,
                "type": typ,
                "element_id": "e1",
                "effect_observed": True,
                "verification": "state_changed",
            }],
            "state": {
                "ok": True,
                "url": "https://example.test/app",
                "title": "App",
                "dom_revision": 2,
            },
        }

    def test_click_then_human_takeover_blocks_type_before_second_mutation(self) -> None:
        js_types = iter(("click", "type"))
        with patch(
            "mcp_server.tools_browser_agent.delegated_agent_identity",
            return_value={"agent_id": "agt_one", "team_id": "team_one", "actor": "agent:agt_one"},
        ), patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.revalidate_mutation_lease",
            side_effect=[(_target("tab-one", 7), None), (None, dict(_HUMAN_TAKEOVER))],
        ) as revalidate, patch(
            "mcp_server.tools_browser_agent._run_json_js",
            side_effect=lambda *args, **kwargs: self._batch_result(next(js_types)),
        ) as run_js:
            result = _browser_act_locked(
                load_settings(),
                "Safari",
                [
                    {"type": "click", "element_id": "e1"},
                    {"type": "type", "element_id": "e1", "text": "agent text"},
                ],
                window_index=1,
                tab_index=1,
                tab_handle="tab-one",
                lease_generation=7,
                return_state="compact",
            )

        self.assertFalse(result["ok"])
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", result["reason_code"])
        self.assertTrue(result["human_priority"])
        self.assertTrue(result["yielded"])
        self.assertTrue(result["human_takeover_during_action"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(2, len(result["actions"]))
        self.assertTrue(result["actions"][0]["ok"])
        self.assertFalse(result["actions"][1]["ok"])
        self.assertEqual("type", result["actions"][1]["type"])
        self.assertEqual(2, revalidate.call_count)
        self.assertEqual(1, run_js.call_count, "type mutation must not reach the browser")
        self.assertIs(
            _target("tab-one", 7).__class__,
            run_js.call_args_list[0].kwargs["prevalidated_target"].__class__,
        )
        self.assertEqual("tab-one", run_js.call_args_list[0].kwargs["prevalidated_target"].tab_handle)

    def test_long_wait_then_human_takeover_blocks_later_click(self) -> None:
        with patch(
            "mcp_server.tools_browser_agent.delegated_agent_identity",
            return_value={"agent_id": "agt_wait", "team_id": "team_one", "actor": "agent:agt_wait"},
        ), patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent._wait_action",
            return_value={
                "ok": True,
                "type": "wait",
                "for": "dom_stable",
                "matched": True,
                "_js_calls": 0,
                "_compact_state": {
                    "ok": True,
                    "url": "https://example.test/app",
                    "title": "App",
                    "dom_revision": 3,
                },
            },
        ), patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.revalidate_mutation_lease",
            return_value=(None, dict(_HUMAN_TAKEOVER)),
        ) as revalidate, patch(
            "mcp_server.tools_browser_agent._run_json_js",
        ) as run_js:
            result = _browser_act_locked(
                load_settings(),
                "Safari",
                [
                    {"type": "wait", "for": "dom_stable", "timeout_s": 10},
                    {"type": "click", "element_id": "e1"},
                ],
                window_index=1,
                tab_index=1,
                tab_handle="tab-wait",
                lease_generation=4,
                return_state="compact",
            )

        self.assertFalse(result["ok"])
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", result["reason_code"])
        self.assertEqual(1, revalidate.call_count)
        run_js.assert_not_called()

    def test_no_takeover_allows_multiple_mutations(self) -> None:
        type_result = self._batch_result("type")
        type_result["actions"][0]["_input_expected"] = "agent text"
        # Two read-only polls verify typing without acquiring another mutation
        # lease. The click and type still each revalidate immediately before input.
        js_results = iter((
            self._batch_result("click"), type_result,
            {"connected": True, "matches": True, "value": "agent text"},
            {"connected": True, "matches": True, "value": "agent text"},
        ))
        with patch(
            "mcp_server.tools_browser_agent.delegated_agent_identity",
            return_value={"agent_id": "agt_ok", "team_id": "team_one", "actor": "agent:agt_ok"},
        ), patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.revalidate_mutation_lease",
            return_value=(_target("tab-ok", 2), None),
        ) as revalidate, patch(
            "mcp_server.tools_browser_agent._run_json_js",
            side_effect=lambda *args, **kwargs: next(js_results),
        ) as run_js:
            result = _browser_act_locked(
                load_settings(),
                "Safari",
                [
                    {"type": "click", "element_id": "e1"},
                    {"type": "type", "element_id": "e1", "text": "agent text"},
                ],
                window_index=1,
                tab_index=1,
                tab_handle="tab-ok",
                lease_generation=2,
                return_state="compact",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(2, revalidate.call_count)
        self.assertEqual(4, run_js.call_count)

    def test_custom_select_revalidates_again_before_option_click(self) -> None:
        revalidation = [
            (_target("tab-select", 1), None),
            (None, dict(_HUMAN_TAKEOVER)),
        ]
        with patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent._run_json_js",
            return_value={"ok": True, "native": False, "needs_option_wait": True},
        ) as run_js:
            result = _select_action(
                load_settings(),
                "Safari",
                {"type": "select", "element_id": "e1", "option": "Turkey"},
                None,
                1,
                1,
                "tab-select",
                mutation_revalidator=lambda action_type: revalidation.pop(0),
            )

        self.assertFalse(result["ok"])
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", result["reason_code"])
        self.assertEqual(1, run_js.call_count, "option activation must not run after takeover")

    def test_trusted_pointer_revalidates_after_readiness_before_dispatch(self) -> None:
        before = {
            "ok": True,
            "connected": True,
            "element_id": "e1",
            "rect": {"x": 10, "y": 10, "w": 40, "h": 20},
            "stable_for_ms": 300,
            "dom_revision": 2,
            "url": "https://example.test/app",
            "title": "App",
            "activation_network_count": 0,
        }
        with patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent.chrome_background_bridge.is_connected",
            return_value=True,
        ), patch(
            "mcp_server.tools_browser_agent._run_json_js",
            return_value=before,
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.resolve_tab",
            return_value=(1, 1, {"native_id": "42"}),
        ), patch(
            "mcp_server.tools_browser_agent.chrome_background_bridge.request_dispatch_mouse",
        ) as dispatch:
            result = _verified_dom_action(
                load_settings(),
                "Google Chrome",
                {"type": "click", "element_id": "e1", "input_mode": "trusted"},
                None,
                1,
                1,
                "tab-trusted",
                mutation_revalidator=lambda action_type: (None, dict(_HUMAN_TAKEOVER)),
            )

        self.assertFalse(result["ok"])
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", result["reason_code"])
        dispatch.assert_not_called()

    def test_read_only_extract_does_not_revalidate_human_ownership(self) -> None:
        with patch(
            "mcp_server.tools_browser_agent.delegated_agent_identity",
            return_value={"agent_id": "agt_read", "team_id": "team_one", "actor": "agent:agt_read"},
        ), patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent._extract_action",
            return_value={"ok": True, "type": "extract", "data": {"title": "App"}, "_js_calls": 0},
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.revalidate_mutation_lease",
        ) as revalidate, patch(
            "mcp_server.tools_browser_agent._run_json_js",
            return_value={"ok": True, "url": "https://example.test/app", "title": "App", "dom_revision": 1},
        ):
            result = _browser_act_locked(
                load_settings(),
                "Safari",
                [{"type": "extract", "fields": [{"name": "title", "selector": "h1"}]}],
                window_index=1,
                tab_index=1,
                tab_handle="tab-read",
                lease_generation=9,
                return_state="none",
            )

        self.assertTrue(result["ok"])
        revalidate.assert_not_called()

    def test_root_local_mutation_has_no_extra_revalidation_cost(self) -> None:
        with patch(
            "mcp_server.tools_browser_agent.delegated_agent_identity",
            return_value=None,
        ), patch(
            "mcp_server.tools_browser_agent._resolve_tab_target",
            return_value=(1, 1),
        ), patch(
            "mcp_server.tools_browser_agent._wait_for_element_readiness",
            side_effect=lambda *args, **kwargs: _ready(),
        ), patch(
            "mcp_server.tools_browser_agent.browser_tabs.revalidate_mutation_lease",
        ) as revalidate, patch(
            "mcp_server.tools_browser_agent._run_json_js",
            return_value=self._batch_result("click"),
        ):
            result = _browser_act_locked(
                load_settings(),
                "Safari",
                [{"type": "click", "element_id": "e1"}],
                window_index=1,
                tab_index=1,
                tab_handle="tab-local",
                lease_generation=0,
                return_state="compact",
            )

        self.assertTrue(result["ok"])
        revalidate.assert_not_called()


class BrowserLeaseGenerationRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._LOGICAL_LEASES.clear()
        browser_tabs._LEASE_HISTORY.clear()

    def _row(self) -> dict:
        return {
            "browser": "Safari",
            "window_index": 1,
            "tab_index": 1,
            "tab_handle": "tab-one",
            "native_id": "7001",
            "title": "App",
            "url": "https://example.test/app",
            "active": False,
        }

    def _install_lease(self, owner: str = "agent:agt_one", generation: int = 3) -> None:
        browser_tabs._LOGICAL_LEASES["tab-one"] = {
            "owner": owner,
            "agent_id": owner.removeprefix("agent:"),
            "generation": generation,
            "created_at": time.time(),
            "last_seen_at": time.time(),
            "expires_at": time.time() + 60,
        }

    def test_generation_change_fails_closed_before_mutation(self) -> None:
        self._install_lease(generation=3)
        with browser_tabs.logical_owner_scope("agent:agt_one", agent_id="agt_one", profile="trusted"), patch(
            "mcp_server.browser_tabs.resolve_tab",
            return_value=(1, 1, self._row()),
        ), patch(
            "mcp_server.browser_tabs.browser_human_takeover",
            return_value=None,
        ):
            target, blocked = browser_tabs.revalidate_mutation_lease("Safari", "tab-one", 2)

        self.assertIsNone(target)
        self.assertEqual("STALE_TAB_LEASE", blocked["reason_code"])
        self.assertEqual(2, blocked["expected_lease_generation"])
        self.assertEqual(3, blocked["actual_lease_generation"])
        self.assertTrue(blocked["observe_again"])

    def test_two_agents_cannot_continue_same_generation(self) -> None:
        self._install_lease(owner="agent:agt_other", generation=5)
        with browser_tabs.logical_owner_scope("agent:agt_one", agent_id="agt_one", profile="trusted"), patch(
            "mcp_server.browser_tabs.resolve_tab",
            return_value=(1, 1, self._row()),
        ), patch(
            "mcp_server.browser_tabs.browser_human_takeover",
            return_value=None,
        ):
            target, blocked = browser_tabs.revalidate_mutation_lease("Safari", "tab-one", 5)

        self.assertIsNone(target)
        self.assertEqual("TAB_OWNED_BY_OTHER_AGENT", blocked["reason_code"])
        self.assertTrue(blocked["yielded"])

    def test_human_takeover_metadata_is_resource_bound_and_user_can_leave(self) -> None:
        self._install_lease(generation=6)
        human = {
            "reason_code": "HUMAN_ACTIVE_RESOURCE",
            "retryable": True,
            "human_priority": True,
            "yielded": True,
        }
        with browser_tabs.logical_owner_scope("agent:agt_one", agent_id="agt_one", profile="trusted"), patch(
            "mcp_server.browser_tabs.resolve_tab",
            return_value=(1, 1, self._row()),
        ), patch(
            "mcp_server.browser_tabs.browser_human_takeover",
            side_effect=[human, None],
        ), patch(
            "mcp_server.browser_tabs.recent_user_input",
            return_value=(True, None, 0.125),
        ):
            blocked_target, blocked = browser_tabs.revalidate_mutation_lease("Safari", "tab-one", 6)
            released_target, released = browser_tabs.revalidate_mutation_lease("Safari", "tab-one", 6)

        self.assertIsNone(blocked_target)
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", blocked["reason_code"])
        self.assertTrue(blocked["human_takeover_during_action"])
        self.assertEqual(125, blocked["human_input_age_ms"])
        self.assertIsNone(released)
        self.assertIsNotNone(released_target)
        self.assertEqual(6, released_target.lease_generation)


class ComputerPlanHumanTakeoverTests(unittest.TestCase):
    def test_partial_browser_mutation_then_human_takeover_is_not_replayed(self) -> None:
        async def run() -> None:
            calls: list[str] = []

            async def caller(tool: str, arguments: dict):
                calls.append(tool)
                if tool == "browser_observe":
                    return {
                        "ok": True,
                        "observation_id": "obs-one",
                        "tab_handle": "tab-one",
                        "url": "https://example.test/app",
                    }
                if tool == "browser_act":
                    return {
                        "ok": False,
                        "reason_code": "HUMAN_ACTIVE_RESOURCE",
                        "error": "human_active_resource",
                        "human_priority": True,
                        "yielded": True,
                        "automatic_retry": False,
                        "actions": [
                            {"ok": True, "type": "click"},
                            {
                                "ok": False,
                                "type": "type",
                                "reason_code": "HUMAN_ACTIVE_RESOURCE",
                                "error": "human_active_resource",
                                "human_priority": True,
                                "yielded": True,
                                "automatic_retry": False,
                            },
                        ],
                    }
                raise AssertionError(f"unexpected tool: {tool}")

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                steps=[
                    {
                        "id": "observe",
                        "tool": "browser_observe",
                        "arguments": {"browser": "Safari", "tab_handle": "tab-one"},
                    },
                    {
                        "id": "mutate",
                        "tool": "browser_act",
                        "arguments": {
                            "browser": "Safari",
                            "tab_handle": "tab-one",
                            "observation_id": {"$ref": "observe.observation_id"},
                            "actions": [
                                {"type": "click", "element_id": "e1"},
                                {"type": "type", "element_id": "e2", "text": "agent text"},
                            ],
                        },
                    },
                    {
                        "id": "later",
                        "tool": "browser_observe",
                        "arguments": {"browser": "Safari", "tab_handle": "tab-one"},
                    },
                ],
            )

            self.assertFalse(result["ok"])
            self.assertEqual("HUMAN_ACTIVE_RESOURCE", result["reason_code"])
            self.assertEqual("mutate", result["step_id"])
            self.assertEqual(["browser_observe", "browser_act"], calls)
            self.assertEqual(0, result["plan_stats"]["recoveries_used"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
