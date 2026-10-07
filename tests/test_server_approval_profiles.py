from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import (
    PolicyContext,
    permission_semantics,
    resolve_risk,
    server_approval_requirement,
)
from mcp_server.runtime_settings import (
    server_approval_profile_setting,
    update_runtime_setting,
)
from mcp_server.security import load_settings
from mcp_server.security_context import SecurityContextManager


TOKEN = "server-approval-test-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class ServerApprovalPolicyTests(unittest.TestCase):
    def test_risk_profiles_map_real_tools_without_prompting_safe_reads(self) -> None:
        _declared, run_risk = resolve_risk("run_command", {"command": "echo ok"})
        _declared, read_risk = resolve_risk("read_file", {"path": "/tmp/example"})
        _declared, browser_risk = resolve_risk(
            "browser_act",
            {"actions": [{"type": "click", "element_id": "e1"}]},
        )
        _declared, observe_risk = resolve_risk("browser_observe", {"browser": "Safari"})
        _declared, update_risk = resolve_risk("mac_mcp_update", {"check_only": False})

        self.assertTrue(server_approval_requirement("critical", run_risk).required)
        self.assertTrue(server_approval_requirement("critical", update_risk).required)
        self.assertFalse(server_approval_requirement("critical", browser_risk).required)
        self.assertFalse(server_approval_requirement("critical", read_risk).required)

        self.assertTrue(server_approval_requirement("high_risk", run_risk).required)
        self.assertTrue(server_approval_requirement("high_risk", browser_risk).required)
        self.assertFalse(server_approval_requirement("high_risk", read_risk).required)
        self.assertFalse(server_approval_requirement("high_risk", observe_risk).required)

    def test_invalid_profile_fails_closed_only_for_high_risk_calls(self) -> None:
        _declared, run_risk = resolve_risk("run_command", {"command": "echo ok"})
        _declared, read_risk = resolve_risk("read_file", {"path": "/tmp/example"})

        blocked = server_approval_requirement("__invalid__", run_risk)
        safe = server_approval_requirement("__invalid__", read_risk)
        self.assertTrue(blocked.blocked)
        self.assertEqual("server_approval_config_invalid", blocked.reason_code)
        self.assertFalse(safe.blocked)
        self.assertFalse(safe.required)

    def test_semantics_are_explicit_about_remote_headless_and_client_prompts(self) -> None:
        semantics = permission_semantics(
            "standard",
            server_approval_profile="high_risk",
        )
        approval = semantics["server_approval"]
        self.assertTrue(approval["enabled"])
        self.assertEqual("server", approval["source"])
        self.assertEqual("deny", approval["headless_behavior"])
        self.assertEqual("deny", approval["timeout_behavior"])
        self.assertEqual(60, approval["approval_timeout_s"])
        self.assertEqual(
            "approval_must_be_granted_on_server_mac",
            approval["remote_session_behavior"],
        )
        self.assertFalse(approval["client_attestation_accepted"])
        self.assertEqual(
            "not_available_without_trusted_attestation",
            approval["client_prompt_deduplication"],
        )
        self.assertIn(
            "mandatory_trust_boundary_approval_satisfies_optional_server_risk_gate",
            approval["same_call_deduplication"],
        )


class ServerApprovalPipelineTests(unittest.TestCase):
    def _mcp(
        self,
        td: str,
        *,
        profile: str | None,
        approval_provider=None,
        policy_profile: str = "trusted",
    ) -> tuple[ObservedFastMCP, TelemetryManager]:
        telemetry = TelemetryManager(
            db_path=Path(td) / "telemetry.sqlite3",
            max_events=100,
        )
        manager = SecurityContextManager(server_approval_profile=profile)
        mcp = ObservedFastMCP(
            name="server-approval-test",
            telemetry=telemetry,
            security_context=manager,
            policy_context_provider=lambda: PolicyContext(
                profile=policy_profile,
                actor="server-approval-test",
            ),
            security_approval_provider=approval_provider,
        )
        return mcp, telemetry

    def test_disabled_profile_preserves_existing_behavior(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": False, "decision": "unexpected"}

                mcp, _ = self._mcp(td, profile="off", approval_provider=provider)
                calls: list[str] = []

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    calls.append(command)
                    return {"ok": True}

                await mcp.call_tool("run_command", {"command": "echo allowed"})
                self.assertEqual(["echo allowed"], calls)
                self.assertEqual([], approvals)

        asyncio.run(run())

    def test_critical_profile_requires_allow_once_before_run_command_executes(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": True, "decision": "confirmed"}

                mcp, telemetry = self._mcp(
                    td,
                    profile="critical",
                    approval_provider=provider,
                )
                calls: list[str] = []

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    calls.append(command)
                    return {"ok": True}

                await mcp.call_tool("run_command", {"command": "echo approved"})
                self.assertEqual(["echo approved"], calls)
                self.assertEqual(1, len(approvals))
                self.assertEqual("server_risk_profile", approvals[0]["reason_code"])
                rendered = repr(approvals[0])
                self.assertNotIn("echo approved", rendered)
                events = telemetry.query_security_events(limit=20)
                self.assertTrue(
                    any(e["event_type"] == "SERVER_RISK_APPROVAL" for e in events)
                )
                self.assertTrue(
                    any(e["event_type"] == "SERVER_RISK_ESCALATION" for e in events)
                )

        asyncio.run(run())

    def test_headless_deny_timeout_and_user_deny_never_execute_side_effect(self) -> None:
        async def scenario(provider, expected_fragment: str) -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, _ = self._mcp(
                    td,
                    profile="critical",
                    approval_provider=provider,
                )
                calls = 0

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True}

                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("run_command", {"command": "echo blocked"})
                self.assertIn(expected_fragment, str(ctx.exception))
                self.assertEqual(0, calls)

        asyncio.run(scenario(None, "server_risk_approval_required"))
        asyncio.run(
            scenario(
                lambda _payload: {
                    "confirmed": False,
                    "decision": "timed_out",
                    "timed_out": True,
                },
                "security_approval_rejected",
            )
        )
        asyncio.run(
            scenario(
                lambda _payload: {
                    "confirmed": False,
                    "decision": "denied",
                },
                "security_approval_rejected",
            )
        )

    def test_allow_once_is_single_use_and_exact_action_bound(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                decisions = iter([True, False, False])
                approvals: list[str] = []

                def provider(payload):
                    approvals.append(str(payload["request_id"]))
                    confirmed = next(decisions)
                    return {
                        "confirmed": confirmed,
                        "decision": "confirmed" if confirmed else "denied",
                    }

                mcp, _ = self._mcp(
                    td,
                    profile="critical",
                    approval_provider=provider,
                )
                calls: list[str] = []

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    calls.append(command)
                    return {"ok": True}

                await mcp.call_tool("run_command", {"command": "echo one"})
                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "echo one"})
                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "echo two"})

                self.assertEqual(["echo one"], calls)
                self.assertEqual(3, len(approvals))
                self.assertNotEqual(approvals[0], approvals[1])
                self.assertNotEqual(approvals[1], approvals[2])

        asyncio.run(run())

    def test_safe_read_never_prompts_even_in_high_risk_profile(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": False, "decision": "unexpected"}

                mcp, _ = self._mcp(
                    td,
                    profile="high_risk",
                    approval_provider=provider,
                )
                calls = 0

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True, "content": "safe"}

                await mcp.call_tool("read_file", {"path": "/tmp/example"})
                self.assertEqual(1, calls)
                self.assertEqual([], approvals)

        asyncio.run(run())

    def test_browser_mutation_prompts_only_in_high_risk_profile(self) -> None:
        async def scenario(profile: str, expected_prompts: int) -> None:
            with tempfile.TemporaryDirectory() as td:
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": True, "decision": "confirmed"}

                mcp, _ = self._mcp(
                    td,
                    profile=profile,
                    approval_provider=provider,
                )
                calls = 0

                @mcp.tool(name="browser_act")
                def browser_act(actions: list[dict]) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True}

                await mcp.call_tool(
                    "browser_act",
                    {"actions": [{"type": "click", "element_id": "e1"}]},
                )
                self.assertEqual(1, calls)
                self.assertEqual(expected_prompts, len(approvals))

        asyncio.run(scenario("critical", 0))
        asyncio.run(scenario("high_risk", 1))

    def test_web_host_boundary_and_server_risk_do_not_double_prompt_same_call(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": True, "decision": "confirmed"}

                mcp, _ = self._mcp(
                    td,
                    profile="high_risk",
                    approval_provider=provider,
                    policy_profile="developer",
                )
                shell_calls = 0

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {
                        "ok": True,
                        "url": "https://evil.example/",
                        "tab_handle": "tab-a",
                    }

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal shell_calls
                    shell_calls += 1
                    return {"ok": True}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                await mcp.call_tool("run_command", {"command": "echo once"})
                self.assertEqual(1, shell_calls)
                self.assertEqual(1, len(approvals))
                self.assertEqual("web_host_boundary", approvals[0]["reason_code"])

        asyncio.run(run())

    def test_live_settings_switch_changes_existing_manager_without_restart(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                settings_path = Path(td) / "settings.json"
                settings_path.write_text(
                    json.dumps({"unrelated": {"keep": True}}),
                    encoding="utf-8",
                )
                os.chmod(settings_path, 0o600)
                approvals: list[dict] = []

                def provider(payload):
                    approvals.append(dict(payload))
                    return {"confirmed": True, "decision": "confirmed"}

                with patch.dict(
                    os.environ,
                    {"MAC_MCP_SETTINGS_PATH": str(settings_path)},
                    clear=False,
                ):
                    self.assertEqual("off", server_approval_profile_setting())
                    telemetry = TelemetryManager(
                        db_path=Path(td) / "telemetry.sqlite3",
                        max_events=100,
                    )
                    manager = SecurityContextManager()
                    mcp = ObservedFastMCP(
                        name="server-approval-live-settings",
                        telemetry=telemetry,
                        security_context=manager,
                        policy_context_provider=lambda: PolicyContext(
                            profile="trusted",
                            actor="settings-test",
                        ),
                        security_approval_provider=provider,
                    )
                    calls = 0

                    @mcp.tool(name="run_command")
                    def run_command(command: str) -> dict:
                        nonlocal calls
                        calls += 1
                        return {"ok": True}

                    await mcp.call_tool("run_command", {"command": "echo before"})
                    self.assertEqual(0, len(approvals))

                    update_runtime_setting(
                        "security",
                        "server_approval_profile",
                        "critical",
                        path=settings_path,
                    )
                    self.assertEqual("critical", server_approval_profile_setting())
                    await mcp.call_tool("run_command", {"command": "echo after"})
                    self.assertEqual(1, len(approvals))
                    self.assertEqual(2, calls)
                    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
                    self.assertTrue(persisted["unrelated"]["keep"])

        asyncio.run(run())


class ServerApprovalDashboardTests(unittest.TestCase):
    def test_local_authenticated_profile_switch_is_persistent_and_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings_path = root / "settings.json"
            settings_path.write_text(
                json.dumps({"subagents": {"keep": "yes"}}),
                encoding="utf-8",
            )
            os.chmod(settings_path, 0o600)
            telemetry = TelemetryManager(
                db_path=root / "telemetry.sqlite3",
                max_events=100,
            )
            manager = SecurityContextManager()
            with patch.dict(
                os.environ,
                {"MAC_MCP_SETTINGS_PATH": str(settings_path)},
                clear=False,
            ):
                app = Starlette(
                    routes=create_dashboard_routes(
                        telemetry,
                        load_settings(),
                        TOKEN,
                        security_context=manager,
                    )
                )
                client = TestClient(app)

                unauth = client.post(
                    "/dashboard/api/security/server-approval",
                    json={"profile": "critical"},
                )
                self.assertEqual(401, unauth.status_code)

                invalid = client.post(
                    "/dashboard/api/security/server-approval",
                    headers=AUTH,
                    json={"profile": "everything"},
                )
                self.assertEqual(400, invalid.status_code)

                changed = client.post(
                    "/dashboard/api/security/server-approval",
                    headers=AUTH,
                    json={"profile": "high_risk"},
                )
                self.assertEqual(200, changed.status_code)
                payload = changed.json()
                self.assertFalse(payload["restart_required"])
                self.assertEqual(
                    "high_risk",
                    payload["server_approval"]["active_profile"],
                )
                self.assertEqual("high_risk", manager.server_approval_profile)

                semantics = client.get(
                    "/dashboard/api/security/semantics",
                    headers=AUTH,
                )
                self.assertEqual(200, semantics.status_code)
                approval = semantics.json()["server_approval"]
                self.assertEqual("high_risk", approval["active_profile"])
                self.assertEqual("deny", approval["headless_behavior"])

                persisted = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    "high_risk",
                    persisted["security"]["server_approval_profile"],
                )
                self.assertEqual("yes", persisted["subagents"]["keep"])
                self.assertEqual(0o600, settings_path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
