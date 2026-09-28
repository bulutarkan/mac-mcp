from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import native_targets, tools_ui
from mcp_server.diagnostics import PASS, WARN, _check_permission_coherence
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import (
    Capability,
    PolicyContext,
    evaluate_profile,
    resolve_risk,
)
from mcp_server.security import load_settings


class TerminalUiPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        native_targets.reset_registries_for_tests()

    def _app_handle(self, app_name: str, bundle_id: str) -> str:
        meta = native_targets.decorate_metadata({
            "active_app": app_name,
            "pid": os.getpid(),
            "bundle_id": bundle_id,
            "frontmost": False,
            "window_count": 0,
            "window_names": [],
            "windows": [],
        })
        return str(meta["app_handle"])

    def test_terminal_text_input_inherits_raw_execution_policy(self) -> None:
        handle = self._app_handle("Terminal", "com.apple.Terminal")
        _, risk = resolve_risk("mac_act", {
            "app_handle": handle,
            "actions": [{"type": "type", "text": "pwd"}],
        })
        self.assertIn(Capability.RAW_EXECUTION, risk.capabilities)
        self.assertFalse(evaluate_profile("standard", risk).allowed)
        self.assertTrue(evaluate_profile("trusted", risk).allowed)

    def test_terminal_bundle_identity_wins_over_misleading_app_label(self) -> None:
        handle = self._app_handle("Terminal", "com.apple.Terminal")
        _, risk = resolve_risk("mac_act", {
            "app": "Notes",
            "app_handle": handle,
            "actions": [{"type": "paste", "text": "whoami"}],
        })
        self.assertIn(Capability.RAW_EXECUTION, risk.capabilities)

    def test_normal_text_editor_input_is_not_raw_execution(self) -> None:
        handle = self._app_handle("TextEdit", "com.apple.TextEdit")
        _, risk = resolve_risk("mac_act", {
            "app_handle": handle,
            "actions": [{"type": "type", "text": "hello"}],
        })
        self.assertNotIn(Capability.RAW_EXECUTION, risk.capabilities)
        self.assertTrue(evaluate_profile("standard", risk).allowed)


class NativeTargetRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        native_targets.reset_registries_for_tests()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()
        self.settings = load_settings()

    def test_mutation_without_stable_target_fails_before_frontmost_resolution(self) -> None:
        with patch.object(tools_ui, "_resolve_app") as resolve_app, \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                app="Microsoft Excel",
                return_state=False,
                allow_risky=True,
                preserve_focus=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("NATIVE_TARGET_REQUIRED", result["reason_code"])
        resolve_app.assert_not_called()
        perform.assert_not_called()

    def test_target_bundle_mismatch_fails_before_action(self) -> None:
        target = {
            "app": "DemoApp",
            "pid": 123,
            "window_index": 1,
            "app_handle": "mapp_demo",
            "window_handle": "mwin_demo",
            "bundle_id": "com.example.demo",
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                target_bundle_id="com.example.other",
                return_state=False,
                allow_risky=True,
                preserve_focus=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("TARGET_BUNDLE_MISMATCH", result["reason_code"])
        perform.assert_not_called()

    def test_action_result_exposes_factual_target_and_effect_metadata(self) -> None:
        target = {
            "app": "DemoApp",
            "pid": 123,
            "window_index": 1,
            "app_handle": "mapp_demo",
            "window_handle": None,
            "bundle_id": "com.example.demo",
        }
        ready = {
            "ready": True,
            "state": {
                "connected": True,
                "role": "AXButton",
                "subrole": "",
                "title": "Button",
                "value": "",
                "character_count": 0,
                "selected": False,
                "enabled": True,
                "position": {"x": 1, "y": 1, "width": 20, "height": 20},
                "window_position": {"x": 0, "y": 0, "width": 100, "height": 100},
                "window_title": "Demo",
                "window_count": 1,
                "window_child_count": 1,
                "sheet_count": 0,
                "popover_count": 0,
                "menu_count": 0,
            },
        }
        effect = {"effect_observed": True, "verification": "target_state_changed", "attempts": 1}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "ok")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                target_bundle_id="com.example.demo",
                return_state=False,
                allow_risky=True,
                preserve_focus=False,
            )
        self.assertTrue(result["ok"])
        action = result["actions"][0]
        self.assertEqual("DemoApp", action["target_app"])
        self.assertEqual("com.example.demo", action["target_bundle_id"])
        self.assertEqual("activation", action["effect_kind"])


class EffectiveToolAvailabilityTests(unittest.TestCase):
    def test_dynamic_terminal_mac_act_is_denied_before_tool_body(self) -> None:
        async def run() -> None:
            native_targets.reset_registries_for_tests()
            meta = native_targets.decorate_metadata({
                "active_app": "Terminal",
                "pid": os.getpid(),
                "bundle_id": "com.apple.Terminal",
                "frontmost": False,
                "window_count": 0,
                "window_names": [],
                "windows": [],
            })
            called = []
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                mcp = ObservedFastMCP(
                    name="test", telemetry=manager,
                    policy_context_provider=lambda: PolicyContext(profile="standard"),
                )

                @mcp.tool(name="mac_act")
                def mac_act(actions: list[dict], app_handle: str | None = None) -> dict:
                    called.append(True)
                    return {"ok": True}

                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("mac_act", {
                        "app_handle": meta["app_handle"],
                        "actions": [{"type": "type", "text": "pwd"}],
                    })
                self.assertIn("profile_denied", str(ctx.exception))
                self.assertFalse(called)

        asyncio.run(run())

    def test_list_tools_tracks_active_permission_profile(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = [PolicyContext(profile="standard")]
                mcp = ObservedFastMCP(
                    name="test",
                    telemetry=manager,
                    policy_context_provider=lambda: context[0],
                )

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    return {"ok": True}

                @mcp.tool(name="mac_act")
                def mac_act(actions: list[dict]) -> dict:
                    return {"ok": True}

                with patch.dict(os.environ, {"MAC_MCP_TOOL_PROFILE": "full"}, clear=False):
                    standard_names = {tool.name for tool in await mcp.list_tools()}
                    self.assertNotIn("run_command", standard_names)
                    self.assertIn("mac_act", standard_names)

                    context[0] = PolicyContext(profile="trusted")
                    trusted_names = {tool.name for tool in await mcp.list_tools()}
                    self.assertIn("run_command", trusted_names)
                    self.assertIn("mac_act", trusted_names)

        asyncio.run(run())


class PermissionCoherenceDoctorTests(unittest.TestCase):
    def test_doctor_warns_when_shell_flag_and_profile_conflict(self) -> None:
        with patch.dict(os.environ, {
            "MCP_ALLOW_SHELL": "true",
            "MAC_MCP_PERMISSION_PROFILE": "standard",
        }, clear=False):
            row = _check_permission_coherence()
        self.assertEqual(WARN, row.status)
        self.assertEqual("SHELL_FLAG_PROFILE_DENY", row.reason_code)

    def test_doctor_passes_when_shell_is_intentionally_trusted(self) -> None:
        with patch.dict(os.environ, {
            "MCP_ALLOW_SHELL": "true",
            "MAC_MCP_PERMISSION_PROFILE": "trusted",
        }, clear=False):
            row = _check_permission_coherence()
        self.assertEqual(PASS, row.status)
        self.assertEqual("SHELL_EFFECTIVELY_AVAILABLE", row.reason_code)


if __name__ == "__main__":
    unittest.main()
