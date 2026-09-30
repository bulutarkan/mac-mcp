from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mcp_server.agent_admission import release as admission_release, request_resource_lease
from mcp_server.computer_plan import ComputerPlanError, derive_computer_plan_resources, execute_computer_plan
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext, resolve_risk


class ComputerPlanExecutorTests(unittest.TestCase):
    def test_five_steps_are_one_model_round_trip_and_refs_conditions_work(self) -> None:
        async def run() -> None:
            calls: list[tuple[str, dict]] = []

            async def caller(tool: str, arguments: dict):
                calls.append((tool, arguments))
                if tool == "open_app":
                    return {"ok": True, "app": arguments["app"]}
                if tool == "mac_observe":
                    return {"ok": True, "observation_id": "obs-1", "active_app": "Notes"}
                if tool == "mac_act":
                    return {
                        "ok": True,
                        "actions": [{"ok": True, "type": "key"}],
                        "observation_id": arguments.get("observation_id", "obs-2"),
                    }
                raise AssertionError(tool)

            result = await execute_computer_plan(
                caller,
                steps=[
                    {"id": "open", "tool": "open_app", "arguments": {"app": "Notes"}},
                    {
                        "id": "observe",
                        "tool": "mac_observe",
                        "arguments": {"app": "Notes"},
                        "preconditions": [{"ref": "open.ok", "equals": True}],
                        "postconditions": [{"path": "observation_id", "exists": True}],
                    },
                    {
                        "id": "click",
                        "tool": "mac_act",
                        "arguments": {
                            "observation_id": {"$ref": "observe.observation_id"},
                            "actions": [{"type": "key", "key": "n"}],
                            "state_mode": "none",
                        },
                    },
                    {
                        "id": "type",
                        "tool": "mac_act",
                        "arguments": {
                            "observation_id": {"$ref": "observe.observation_id"},
                            "actions": [{"type": "type", "text": "Task 29 fixture"}],
                            "state_mode": "none",
                        },
                    },
                    {
                        "id": "verify",
                        "tool": "mac_observe",
                        "arguments": {"app": "Notes"},
                        "postconditions": [{"path": "ok", "equals": True}],
                    },
                ],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(5, len(calls))
            self.assertEqual("obs-1", calls[2][1]["observation_id"])
            self.assertEqual(1, result["plan_stats"]["model_tool_calls"])
            self.assertEqual(5, result["plan_stats"]["standalone_equivalent_tool_calls"])
            self.assertGreaterEqual(result["plan_stats"]["model_round_trip_reduction_percent"], 50.0)

        asyncio.run(run())

    def test_failure_stops_later_steps(self) -> None:
        async def run() -> None:
            calls: list[str] = []

            async def caller(tool: str, arguments: dict):
                calls.append(tool)
                if len(calls) == 2:
                    return {"ok": False, "reason_code": "ACTION_NO_EFFECT", "error": "no effect"}
                return {"ok": True}

            result = await execute_computer_plan(
                caller,
                steps=[
                    {"id": "one", "tool": "mac_observe", "arguments": {}},
                    {"id": "two", "tool": "mac_act", "arguments": {"actions": [{"type": "key", "key": "x"}]}},
                    {"id": "three", "tool": "mac_observe", "arguments": {}},
                ],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(["mac_observe", "mac_act"], calls)
            self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
            self.assertEqual(2, result["plan_stats"]["steps_executed"])

        asyncio.run(run())

    def test_allowlist_step_and_action_budgets(self) -> None:
        async def caller(tool: str, arguments: dict):
            return {"ok": True}

        with self.assertRaises(ComputerPlanError):
            asyncio.run(execute_computer_plan(caller, steps=[
                {"id": "shell", "tool": "run_command", "arguments": {"command": "pwd"}},
            ]))
        with self.assertRaises(ComputerPlanError):
            asyncio.run(execute_computer_plan(caller, steps=[
                {"id": f"s{i}", "tool": "mac_observe", "arguments": {}} for i in range(9)
            ]))
        with self.assertRaises(ComputerPlanError):
            asyncio.run(execute_computer_plan(caller, steps=[
                {
                    "id": "too_many",
                    "tool": "browser_act",
                    "arguments": {"actions": [{"type": "wait"} for _ in range(25)]},
                }
            ]))

    def test_clipboard_backed_native_actions_preclaim_global_clipboard(self) -> None:
        paste_claims = derive_computer_plan_resources([
            {"id": "paste", "tool": "mac_act", "arguments": {
                "app": "TextEdit",
                "actions": [{"type": "paste", "element_id": "w1/1", "text": "hello"}],
            }},
        ])
        type_claims = derive_computer_plan_resources([
            {"id": "type", "tool": "mac_act", "arguments": {
                "app": "TextEdit",
                "actions": [{"type": "type", "element_id": "w1/1", "text": "hello"}],
            }},
        ])
        key_claims = derive_computer_plan_resources([
            {"id": "key", "tool": "mac_act", "arguments": {
                "app": "TextEdit",
                "actions": [{"type": "key", "key": "return"}],
            }},
        ])
        clipboard = {"kind": "clipboard", "id": "system", "mode": "write"}
        self.assertIn(clipboard, paste_claims)
        self.assertIn(clipboard, type_claims)
        self.assertNotIn(clipboard, key_claims)

    def test_clipboard_resource_preflight_blocks_conflicting_plan_before_action(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                holder = request_resource_lease(
                    root,
                    owner_id="holder",
                    resources=[{"kind": "clipboard", "id": "system", "mode": "write"}],
                    ttl_s=30,
                )
                calls: list[str] = []

                async def caller(tool: str, arguments: dict):
                    calls.append(tool)
                    return {"ok": True}

                steps = [{
                    "id": "paste",
                    "tool": "mac_act",
                    "arguments": {
                        "app": "TextEdit",
                        "actions": [{"type": "paste", "element_id": "w1/1", "text": "hello"}],
                    },
                }]
                try:
                    result = await execute_computer_plan(
                        caller,
                        steps=steps,
                        resources=derive_computer_plan_resources(steps),
                        admission_root=root,
                    )
                finally:
                    admission_release(root, lease_id=str(holder.get("lease_id") or ""))

                self.assertFalse(result["ok"])
                self.assertEqual("RESOURCE_BUSY", result["reason_code"])
                self.assertEqual([], calls)

        asyncio.run(run())

    def test_dynamic_ref_resource_conflict_blocks_before_mutation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                holder = request_resource_lease(
                    root,
                    owner_id="holder",
                    resources=[{"kind": "browser_tab", "id": "tab-dynamic", "mode": "write"}],
                    ttl_s=30,
                )
                calls: list[str] = []

                async def caller(tool: str, arguments: dict):
                    calls.append(tool)
                    if tool == "browser_list_tabs":
                        return {"ok": True, "tab_handle": "tab-dynamic"}
                    return {"ok": True, "actions": [{"ok": True}]}

                steps = [
                    {"id": "discover", "tool": "browser_list_tabs", "arguments": {"browser": "Safari"}},
                    {
                        "id": "act",
                        "tool": "browser_act",
                        "arguments": {
                            "browser": "Safari",
                            "tab_handle": {"$ref": "discover.tab_handle"},
                            "actions": [{"type": "click", "element_id": "e1"}],
                        },
                    },
                ]
                try:
                    result = await execute_computer_plan(
                        caller,
                        steps=steps,
                        plan_version=2,
                        resources=derive_computer_plan_resources(steps),
                        admission_root=root,
                    )
                finally:
                    admission_release(root, lease_id=str(holder.get("lease_id") or ""))

                self.assertFalse(result["ok"])
                self.assertEqual("RESOURCE_BUSY", result["reason_code"])
                self.assertEqual("act", result["step_id"])
                self.assertTrue(result["dynamic_resource_claim"])
                self.assertEqual(["browser_list_tabs"], calls)

        asyncio.run(run())

    def test_dynamic_ref_resource_claim_extends_and_releases(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                calls: list[tuple[str, dict]] = []

                async def caller(tool: str, arguments: dict):
                    calls.append((tool, arguments))
                    if tool == "browser_list_tabs":
                        return {"ok": True, "tab_handle": "tab-dynamic"}
                    return {"ok": True, "actions": [{"ok": True}]}

                steps = [
                    {"id": "discover", "tool": "browser_list_tabs", "arguments": {"browser": "Safari"}},
                    {
                        "id": "act",
                        "tool": "browser_act",
                        "arguments": {
                            "browser": "Safari",
                            "tab_handle": {"$ref": "discover.tab_handle"},
                            "actions": [{"type": "click", "element_id": "e1"}],
                        },
                    },
                ]
                result = await execute_computer_plan(
                    caller,
                    steps=steps,
                    plan_version=2,
                    resources=derive_computer_plan_resources(steps),
                    admission_root=root,
                )
                self.assertTrue(result["ok"])
                self.assertEqual("tab-dynamic", calls[-1][1]["tab_handle"])
                self.assertGreaterEqual(result["resource_preflight"]["dynamic_extensions"], 1)
                self.assertEqual(1, result["resource_preflight"]["resource_count"])

                after = request_resource_lease(
                    root,
                    owner_id="after",
                    resources=[{"kind": "browser_tab", "id": "tab-dynamic", "mode": "write"}],
                    ttl_s=30,
                )
                self.assertTrue(after["admitted"])
                admission_release(root, lease_id=str(after.get("lease_id") or ""))

        asyncio.run(run())

    def test_future_reference_is_rejected_before_any_side_effect(self) -> None:
        async def run() -> None:
            calls: list[str] = []

            async def caller(tool: str, arguments: dict):
                calls.append(tool)
                return {"ok": True}

            with self.assertRaises(ComputerPlanError):
                await execute_computer_plan(
                    caller,
                    steps=[
                        {
                            "id": "first",
                            "tool": "mac_act",
                            "arguments": {
                                "observation_id": {"$ref": "later.observation_id"},
                                "actions": [{"type": "key", "key": "x"}],
                            },
                        },
                        {"id": "later", "tool": "mac_observe", "arguments": {}},
                    ],
                )
            self.assertEqual([], calls)

        asyncio.run(run())

    def test_failed_precondition_skips_tool(self) -> None:
        async def run() -> None:
            calls: list[str] = []

            async def caller(tool: str, arguments: dict):
                calls.append(tool)
                return {"ok": True, "ready": False}

            result = await execute_computer_plan(
                caller,
                steps=[
                    {"id": "one", "tool": "mac_observe", "arguments": {}},
                    {
                        "id": "two",
                        "tool": "mac_act",
                        "arguments": {"actions": [{"type": "key", "key": "x"}]},
                        "preconditions": [{"ref": "one.ready", "equals": True}],
                    },
                ],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(["mac_observe"], calls)
            self.assertEqual("PRECONDITION_FAILED", result["reason_code"])

        asyncio.run(run())


class ComputerPlanPolicyTests(unittest.TestCase):
    def test_wrapper_risk_is_read_only_but_declared_annotation_is_mutating(self) -> None:
        declared, effective = resolve_risk("computer_plan", {"steps": []})
        self.assertTrue(declared.destructive)
        self.assertFalse(effective.destructive)
        self.assertEqual({"read"}, {item.value for item in effective.capabilities})

    def test_nested_dispatch_reapplies_web_to_host_security_gate_and_stops(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = PolicyContext(profile="standard", actor="test")
                mcp = ObservedFastMCP(
                    name="computer-plan-security",
                    telemetry=telemetry,
                    policy_context_provider=lambda: context,
                )
                executed: list[str] = []

                @mcp.tool(name="browser_observe", structured_output=False)
                def browser_observe(browser: str = "Safari") -> dict:
                    executed.append("browser_observe")
                    return {
                        "ok": True,
                        "url": "https://untrusted.example/page",
                        "tab_handle": "tab-test",
                    }

                @mcp.tool(name="mac_act", structured_output=False)
                def mac_act(actions: list[dict]) -> dict:
                    executed.append("mac_act")
                    return {"ok": True, "actions": [{"ok": True}]}

                @mcp.tool(name="computer_plan", structured_output=False)
                async def computer_plan(steps: list[dict], max_seconds: float = 45.0) -> dict:
                    return await execute_computer_plan(mcp.call_tool, steps=steps, max_seconds=max_seconds)

                result_blocks = await mcp.call_tool("computer_plan", {
                    "steps": [
                        {"id": "web", "tool": "browser_observe", "arguments": {"browser": "Safari"}},
                        {"id": "host", "tool": "mac_act", "arguments": {"actions": [{"type": "key", "key": "x"}]}},
                        {"id": "never", "tool": "mac_observe", "arguments": {}},
                    ]
                })
                payload = json.loads(result_blocks[-1].text)
                self.assertFalse(payload["ok"])
                self.assertEqual("STEP_CALL_FAILED", payload["reason_code"])
                self.assertEqual(["browser_observe"], executed)
                self.assertIn("approval", payload["error"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
