from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from mcp_server.agent_admission import release as admission_release, request_resource_lease, snapshot as admission_snapshot
from mcp_server.computer_plan import ComputerPlanError, execute_computer_plan
from mcp_server import tools_ui


class ComputerPlanRecoveryTests(unittest.TestCase):
    # ASSURANCE: SEC-COMP-001
    def test_spa_stale_node_reobserves_semantically_rebinds_and_finishes(self) -> None:
        async def run() -> None:
            calls: list[tuple[str, dict]] = []
            act_count = 0

            async def caller(tool: str, args: dict):
                nonlocal act_count
                calls.append((tool, args))
                if tool == "browser_observe":
                    return {
                        "ok": True, "observation_id": "obs_old", "url": "https://x.test",
                        "elements": [{
                            "element_id": "e_old", "role": "button", "text": "Save",
                            "aria_label": "Save", "actionable": True,
                        }],
                    }
                if tool == "browser_act":
                    act_count += 1
                    if act_count == 1:
                        return {
                            "ok": False, "error": "stale_observation", "observe_again": True, "retryable": True,
                            "actions": [{"ok": False, "error": "stale_observation", "element_id": "e_old"}],
                        }
                    self.assertEqual("obs_new", args["observation_id"])
                    self.assertEqual("e_new", args["actions"][0]["element_id"])
                    return {"ok": True, "actions": [{"ok": True, "type": "click", "effect_observed": True}]}
                if tool == "browser_find":
                    self.assertEqual("Save", args["query"])
                    match = {"element_id": "e_new", "confidence": 0.97, "role": "button", "text": "Save"}
                    return {"ok": True, "observation_id": "obs_new", "best_match": match, "matches": [match]}
                raise AssertionError(tool)

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                steps=[
                    {"id": "observe", "tool": "browser_observe", "arguments": {"browser": "Safari", "tab_handle": "tab_a"}},
                    {
                        "id": "click", "tool": "browser_act",
                        "arguments": {
                            "browser": "Safari", "tab_handle": "tab_a",
                            "observation_id": {"$ref": "observe.observation_id"},
                            "actions": [{"type": "click", "element_id": "e_old"}],
                        },
                    },
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(["browser_observe", "browser_act", "browser_find", "browser_act"], [c[0] for c in calls])
            self.assertEqual(1, result["plan_stats"]["recoveries_used"])
            self.assertTrue(result["steps"][1]["recovered"])
            self.assertEqual("browser_semantic_rebind", result["steps"][1]["attempts"][0]["recovery"]["kind"])

        asyncio.run(run())

    def test_native_sibling_insertion_rebinds_by_axidentifier(self) -> None:
        async def run() -> None:
            calls: list[tuple[str, dict]] = []
            act_count = 0

            async def caller(tool: str, args: dict):
                nonlocal act_count
                calls.append((tool, args))
                if tool == "mac_observe" and len([c for c in calls if c[0] == "mac_observe"]) == 1:
                    return {
                        "ok": True, "observation_id": "native_old", "active_app": "DemoApp",
                        "window_index": 1, "app_handle": "app_old", "window_handle": "win_old",
                        "nodes": [
                            {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                            {
                                "element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "subrole": "",
                                "title": "Continue", "description": "Continue", "value": "", "identifier": "continue-button",
                            },
                        ],
                    }
                if tool == "mac_act":
                    act_count += 1
                    if act_count == 1:
                        return {
                            "ok": False, "reason_code": "STALE_ELEMENT_PATH", "error": "element_not_ready",
                            "retryable": True, "observe_again": True,
                            "actions": [{"ok": False, "reason_code": "STALE_ELEMENT_PATH", "element_id": "w1/2"}],
                        }
                    self.assertEqual("native_new", args["observation_id"])
                    self.assertEqual("w1/3", args["actions"][0]["element_id"])
                    self.assertEqual("app_new", args["app_handle"])
                    self.assertEqual("win_new", args["window_handle"])
                    return {"ok": True, "actions": [{"ok": True, "effect_observed": True}]}
                if tool == "mac_observe":
                    # Recovery must rediscover from app/window_index, not feed stale handles back in.
                    self.assertEqual("DemoApp", args["app"])
                    self.assertEqual(1, args["window_index"])
                    self.assertNotIn("app_handle", args)
                    self.assertNotIn("window_handle", args)
                    return {
                        "ok": True, "observation_id": "native_new", "active_app": "DemoApp",
                        "window_index": 1, "app_handle": "app_new", "window_handle": "win_new",
                        "nodes": [
                            {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                            {"element_id": "w1/1", "parent_id": "w1", "role": "AXStaticText", "title": "Inserted"},
                            {
                                "element_id": "w1/3", "parent_id": "w1", "role": "AXButton", "subrole": "",
                                "title": "Continue", "description": "Continue", "value": "", "identifier": "continue-button",
                            },
                        ],
                    }
                raise AssertionError(tool)

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                steps=[
                    {"id": "observe", "tool": "mac_observe", "arguments": {"app": "DemoApp", "window_index": 1, "include_screenshot": False}},
                    {
                        "id": "act", "tool": "mac_act",
                        "arguments": {
                            "app": "DemoApp", "app_handle": {"$ref": "observe.app_handle"},
                            "window_handle": {"$ref": "observe.window_handle"}, "window_index": 1,
                            "observation_id": {"$ref": "observe.observation_id"},
                            "actions": [{"type": "click", "element_id": "w1/2"}],
                            "state_mode": "none",
                        },
                    },
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["plan_stats"]["recoveries_used"])
            self.assertEqual(2, act_count)

        asyncio.run(run())

    def test_wait_until_handles_delayed_native_dialog_without_consuming_recovery_budget(self) -> None:
        async def run() -> None:
            observe_count = 0
            calls: list[str] = []

            async def caller(tool: str, args: dict):
                nonlocal observe_count
                calls.append(tool)
                if tool == "mac_observe":
                    observe_count += 1
                    if observe_count == 1:
                        return {"ok": True, "observation_id": "o1", "nodes": []}
                    return {
                        "ok": True, "observation_id": "o2", "app_handle": "app", "window_handle": "win",
                        "nodes": [{
                            "element_id": "w1/4", "role": "AXButton", "title": "Allow",
                            "description": "", "identifier": "allow-button",
                        }],
                    }
                if tool == "mac_act":
                    self.assertEqual("o2", args["observation_id"])
                    self.assertEqual("w1/4", args["actions"][0]["element_id"])
                    return {"ok": True, "actions": [{"ok": True, "effect_observed": True}]}
                raise AssertionError(tool)

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                max_recoveries=0,
                steps=[
                    {
                        "id": "wait_dialog", "type": "wait_until", "tool": "mac_observe",
                        "arguments": {"app": "DemoApp", "include_screenshot": False},
                        "target": {"identifier": "allow-button", "role": "AXButton", "title": "Allow"},
                        "timeout_s": 1.0, "poll_ms": 20,
                    },
                    {
                        "id": "click", "tool": "mac_act",
                        "arguments": {
                            "app": "DemoApp", "observation_id": {"$ref": "wait_dialog.observation_id"},
                            "app_handle": {"$ref": "wait_dialog.app_handle"},
                            "window_handle": {"$ref": "wait_dialog.window_handle"},
                            "actions": [{"type": "click", "element_id": {"$ref": "wait_dialog.best_match.element_id"}}],
                            "state_mode": "none",
                        },
                    },
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(0, result["plan_stats"]["recoveries_used"])
            self.assertEqual(["mac_observe", "mac_observe", "mac_act"], calls)
            self.assertEqual(2, result["steps"][0]["attempts"])

        asyncio.run(run())

    # ASSURANCE: SEC-COMP-001
    def test_ambiguous_native_rebind_fails_closed_without_second_mutation(self) -> None:
        async def run() -> None:
            act_count = 0

            async def caller(tool: str, args: dict):
                nonlocal act_count
                if tool == "mac_observe" and not args.get("include_screenshot") is False:
                    pass
                if tool == "mac_observe" and act_count == 0:
                    # Initial observation.
                    return {
                        "ok": True, "observation_id": "old", "active_app": "DemoApp", "window_index": 1,
                        "nodes": [
                            {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                            {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                        ],
                    }
                if tool == "mac_act":
                    act_count += 1
                    return {
                        "ok": False, "reason_code": "STALE_ELEMENT_PATH", "retryable": True, "observe_again": True,
                        "actions": [{"ok": False, "reason_code": "STALE_ELEMENT_PATH", "element_id": "w1/2"}],
                    }
                if tool == "mac_observe":
                    return {
                        "ok": True, "observation_id": "new", "active_app": "DemoApp", "window_index": 1,
                        "nodes": [
                            {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                            {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                            {"element_id": "w1/3", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                        ],
                    }
                raise AssertionError(tool)

            # Need a stateful initial/recovery distinction independent of args.
            observe_calls = 0
            async def stateful(tool: str, args: dict):
                nonlocal observe_calls, act_count
                if tool == "mac_observe":
                    observe_calls += 1
                    if observe_calls == 1:
                        return {
                            "ok": True, "observation_id": "old", "active_app": "DemoApp", "window_index": 1,
                            "nodes": [
                                {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                                {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                            ],
                        }
                    return {
                        "ok": True, "observation_id": "new", "active_app": "DemoApp", "window_index": 1,
                        "nodes": [
                            {"element_id": "w1", "role": "AXWindow", "title": "Demo"},
                            {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                            {"element_id": "w1/3", "parent_id": "w1", "role": "AXButton", "title": "Continue", "description": "Continue", "identifier": ""},
                        ],
                    }
                if tool == "mac_act":
                    act_count += 1
                    return {"ok": False, "reason_code": "STALE_ELEMENT_PATH", "retryable": True, "observe_again": True,
                            "actions": [{"ok": False, "reason_code": "STALE_ELEMENT_PATH", "element_id": "w1/2"}]}
                raise AssertionError(tool)

            result = await execute_computer_plan(
                stateful, plan_version=2,
                steps=[
                    {"id": "obs", "tool": "mac_observe", "arguments": {"app": "DemoApp", "include_screenshot": False}},
                    {"id": "act", "tool": "mac_act", "arguments": {
                        "app": "DemoApp", "observation_id": {"$ref": "obs.observation_id"},
                        "actions": [{"type": "click", "element_id": "w1/2"}], "state_mode": "none",
                    }},
                ],
            )
            self.assertFalse(result["ok"])
            self.assertEqual("RECOVERY_AMBIGUOUS_TARGET", result["reason_code"])
            self.assertEqual(1, act_count)

        asyncio.run(run())

    # ASSURANCE: SEC-COMP-001
    def test_action_no_effect_and_outcome_unknown_are_never_replayed(self) -> None:
        async def case(payload: dict) -> tuple[dict, int]:
            calls = 0
            async def caller(tool: str, args: dict):
                nonlocal calls
                calls += 1
                return payload
            result = await execute_computer_plan(
                caller, plan_version=2,
                steps=[{"id": "act", "tool": "browser_act", "arguments": {
                    "browser": "Safari", "actions": [{"type": "click", "element_id": "e1"}],
                }}],
            )
            return result, calls

        no_effect, no_effect_calls = asyncio.run(case({
            "ok": False, "reason_code": "ACTION_NO_EFFECT", "error": "action_no_effect",
            "automatic_retry": False, "observe_again": True,
            "actions": [{"ok": False, "reason_code": "ACTION_NO_EFFECT"}],
        }))
        self.assertFalse(no_effect["ok"])
        self.assertEqual("ACTION_NO_EFFECT", no_effect["reason_code"])
        self.assertEqual(1, no_effect_calls)

        unknown, unknown_calls = asyncio.run(case({
            "ok": False, "reason_code": "outcome_unknown", "error": "opaque side effect",
            "outcome_unknown": True, "retryable": True, "observe_again": True,
        }))
        self.assertFalse(unknown["ok"])
        self.assertEqual("outcome_unknown", unknown["reason_code"])
        self.assertEqual(1, unknown_calls)

    # ASSURANCE: SEC-COMP-001
    def test_unknown_retryable_mutation_and_partial_batch_are_not_replayed(self) -> None:
        async def run() -> None:
            async def run_case(payload: dict) -> tuple[dict, int]:
                calls = 0
                async def caller(tool: str, args: dict):
                    nonlocal calls
                    calls += 1
                    return payload
                result = await execute_computer_plan(
                    caller, plan_version=2,
                    steps=[{"id":"act","tool":"mac_act","arguments":{
                        "actions":[{"type":"click","element_id":"w1/1"}], "state_mode":"none"
                    }}],
                )
                return result, calls

            unknown, unknown_calls = await run_case({
                "ok": False, "reason_code": "SOME_NEW_RETRYABLE_FAILURE",
                "retryable": True, "observe_again": True,
            })
            self.assertFalse(unknown["ok"])
            self.assertEqual(1, unknown_calls)

            partial, partial_calls = await run_case({
                "ok": False, "reason_code": "ELEMENT_NOT_READY", "retryable": True, "observe_again": True,
                "actions": [
                    {"ok": True, "type": "click", "effect_observed": True},
                    {"ok": False, "reason_code": "ELEMENT_NOT_READY", "type": "click"},
                ],
            })
            self.assertFalse(partial["ok"])
            self.assertEqual(1, partial_calls)
        asyncio.run(run())

    def test_branch_local_output_cannot_be_referenced_after_branch(self) -> None:
        async def caller(tool: str, args: dict):
            return {"ok": True}
        with self.assertRaises(ComputerPlanError) as ctx:
            asyncio.run(execute_computer_plan(
                caller, plan_version=2,
                steps=[
                    {"id":"probe","tool":"mac_observe","arguments":{}},
                    {"id":"branch","type":"branch","condition":{"ref":"probe.ok","equals":True},
                     "then":[{"id":"only_then","tool":"mac_observe","arguments":{}}]},
                    {"id":"later","tool":"mac_observe","arguments":{"app":{"$ref":"only_then.active_app"}}},
                ],
            ))
        self.assertIn("unknown or future step", str(ctx.exception))

    # ASSURANCE: SEC-COMP-001
    def test_recovery_budget_exceeded_stops_before_fresh_observe(self) -> None:
        async def run() -> None:
            calls: list[str] = []
            async def caller(tool: str, args: dict):
                calls.append(tool)
                if tool == "browser_observe":
                    return {"ok": True, "observation_id": "o", "elements": [{"element_id":"e1","role":"button","text":"Save"}]}
                if tool == "browser_act":
                    return {"ok": False, "error": "stale_element", "retryable": True, "observe_again": True,
                            "actions": [{"ok": False, "error": "stale_element", "element_id": "e1"}]}
                raise AssertionError(tool)
            result = await execute_computer_plan(
                caller, plan_version=2, max_recoveries=0,
                steps=[
                    {"id":"obs","tool":"browser_observe","arguments":{"browser":"Safari"}},
                    {"id":"act","tool":"browser_act","arguments":{
                        "browser":"Safari","observation_id":{"$ref":"obs.observation_id"},
                        "actions":[{"type":"click","element_id":"e1"}],
                    }},
                ],
            )
            self.assertFalse(result["ok"])
            self.assertEqual("RECOVERY_BUDGET_EXCEEDED", result["reason_code"])
            self.assertEqual(["browser_observe", "browser_act"], calls)
        asyncio.run(run())

    def test_conditional_branch_and_safe_fallback(self) -> None:
        async def run() -> None:
            calls: list[str] = []
            observe_attempt = 0
            async def caller(tool: str, args: dict):
                nonlocal observe_attempt
                calls.append(tool)
                if tool == "mac_observe":
                    return {"ok": True, "ready": False}
                if tool == "browser_observe":
                    observe_attempt += 1
                    return {"ok": False, "error": "render_not_ready", "retryable": True, "observe_again": True}
                if tool == "browser_list_tabs":
                    return {"ok": True, "tabs": []}
                raise AssertionError(tool)

            result = await execute_computer_plan(
                caller, plan_version=2,
                steps=[
                    {"id":"probe","tool":"mac_observe","arguments":{"include_screenshot":False}},
                    {"id":"branch","type":"branch","condition":{"ref":"probe.ready","equals":True},
                     "then":[{"id":"then_tabs","tool":"browser_list_tabs","arguments":{"browser":"Safari"}}],
                     "else":[{
                         "id":"fallback_source","tool":"browser_observe","arguments":{"browser":"Safari"},
                         "retry":{"max_attempts":1},
                         "fallback":{"id":"fallback_tabs","tool":"browser_list_tabs","arguments":{"browser":"Safari"}},
                     }]},
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(["mac_observe", "browser_observe", "browser_list_tabs"], calls)
            self.assertEqual("else", result["steps"][1]["branch"])
            self.assertTrue(result["steps"][2]["fallback_used"])
            self.assertEqual(1, result["plan_stats"]["recoveries_used"])
        asyncio.run(run())

    # ASSURANCE: SEC-COMP-001
    def test_recovery_observe_exception_becomes_controlled_failure(self) -> None:
        async def run() -> None:
            calls: list[str] = []
            async def caller(tool: str, args: dict):
                calls.append(tool)
                if tool == "browser_observe":
                    return {"ok": True, "observation_id": "old", "elements": [{"element_id":"e1","role":"button","text":"Save"}]}
                if tool == "browser_act":
                    return {"ok": False, "error": "stale_element", "retryable": True, "observe_again": True,
                            "actions": [{"ok": False, "error": "stale_element", "element_id": "e1"}]}
                if tool == "browser_find":
                    raise RuntimeError("fixture find failed")
                raise AssertionError(tool)
            result = await execute_computer_plan(
                caller, plan_version=2,
                steps=[
                    {"id":"obs","tool":"browser_observe","arguments":{"browser":"Safari"}},
                    {"id":"act","tool":"browser_act","arguments":{
                        "browser":"Safari","observation_id":{"$ref":"obs.observation_id"},
                        "actions":[{"type":"click","element_id":"e1"}],
                    }},
                ],
            )
            self.assertFalse(result["ok"])
            self.assertEqual("RECOVERY_OBSERVE_FAILED", result["reason_code"])
            self.assertEqual("act", result["step_id"])
            self.assertEqual(["browser_observe", "browser_act", "browser_find"], calls)
        asyncio.run(run())

    # ASSURANCE: SEC-COMP-001
    def test_resource_busy_preflight_stops_before_any_nested_tool(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "agents"
                root.mkdir()
                held = request_resource_lease(
                    root, owner_id="other", resources=[{"kind":"browser_tab","id":"tab_busy","mode":"write"}], ttl_s=60,
                )
                self.assertTrue(held["admitted"])
                called = False
                async def caller(tool: str, args: dict):
                    nonlocal called
                    called = True
                    return {"ok": True}
                result = await execute_computer_plan(
                    caller, plan_version=2, admission_root=root,
                    resources=[{"kind":"browser_tab","id":"tab_busy","mode":"write"}],
                    steps=[{"id":"tabs","tool":"browser_list_tabs","arguments":{"browser":"Safari"}}],
                )
                self.assertFalse(result["ok"])
                self.assertEqual("RESOURCE_BUSY", result["reason_code"])
                self.assertFalse(called)
                self.assertEqual(0, admission_snapshot(root)["global_active"])
                admission_release(root, lease_id=held["lease_id"])
        asyncio.run(run())

    def test_five_step_v2_fixture_stays_one_model_call(self) -> None:
        async def run() -> None:
            calls = 0
            async def caller(tool: str, args: dict):
                nonlocal calls
                calls += 1
                return {"ok": True, "value": calls}
            result = await execute_computer_plan(
                caller, plan_version=2,
                steps=[{"id":f"s{i}","tool":"mac_observe","arguments":{"include_screenshot":False}} for i in range(5)],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(5, calls)
            self.assertEqual(1, result["plan_stats"]["model_tool_calls"])
            self.assertEqual(5, result["plan_stats"]["steps_executed"])
        asyncio.run(run())


class NativeSemanticIdentityTests(unittest.TestCase):
    def test_native_observation_script_and_parser_include_axidentifier(self) -> None:
        script = tools_ui._observation_script("DemoApp", 1, 2, 10)
        self.assertIn('value of attribute "AXIdentifier" of nodeRef', script)
        fs = tools_ui._FIELD_SEPARATOR
        rs = tools_ui._RECORD_SEPARATOR
        fields = [
            "__NODE__", "w1/2", "w1", "AXButton", "", "Continue", "Continue", "", "10", "20", "100", "30",
            "true", "false", "AXPress", "0", "continue-button",
        ]
        _, nodes = tools_ui._parse_observation(fs.join(fields) + rs)
        self.assertEqual("continue-button", nodes[0]["identifier"])


if __name__ == "__main__":
    unittest.main()
