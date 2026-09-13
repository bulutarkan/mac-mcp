from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext, evaluate_tool_scope, resolve_risk
from mcp_server.policy_scope import ResourceScope
from mcp_server.security_context import SecurityContextManager
from mcp_server.steering import SteeringIdentity, SteeringManager
from mcp_server.tools_agents import _requested_agent_scope

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/security/malicious_prompt_injection.html"


class WebHostBoundaryRegressionTests(unittest.TestCase):
    def _manager(self, td: str, *, context: PolicyContext | None = None) -> tuple[ObservedFastMCP, TelemetryManager]:
        telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
        policy_context = context or PolicyContext(profile="trusted", actor="test")
        mcp = ObservedFastMCP(
            name="security-test",
            telemetry=telemetry,
            policy_context_provider=lambda: policy_context,
        )
        return mcp, telemetry

    def test_malicious_dom_cannot_pivot_to_shell_and_security_event_is_redacted(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, telemetry = self._manager(td)
                shell_executed = False
                html = FIXTURE.read_text(encoding="utf-8")

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari", tab_handle: str = "tab-a") -> dict:
                    return {
                        "ok": True,
                        "url": "https://evil.example/report?token=must-not-log",
                        "tab_handle": tab_handle,
                        "text": html,
                    }

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal shell_executed
                    shell_executed = True
                    return {"ok": True, "stdout": "ran"}

                await mcp.call_tool("browser_observe", {"browser": "Safari", "tab_handle": "tab-a"})
                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("run_command", {"command": "cat ~/.ssh/id_ed25519"})
                self.assertIn("web_host_boundary_denied", str(ctx.exception))
                self.assertFalse(shell_executed)

                events = telemetry.query_security_events(limit=10)
                breach = next(event for event in events if event["event_type"] == "HOST_TOOL_BREACH")
                self.assertEqual("run_command", breach["tool"])
                self.assertEqual("https://evil.example", breach["origin"])
                rendered = str(events)
                self.assertNotIn("id_ed25519", rendered)
                self.assertNotIn("must-not-log", rendered)
                self.assertNotIn("IGNORE THE USER", rendered)
        asyncio.run(run())

    def test_local_one_shot_escalation_allows_exact_tool_once(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, telemetry = self._manager(td)
                calls = 0

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": "https://evil.example/", "tab_handle": "tab-a"}

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True, "stdout": "ok"}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "printf blocked"})

                grant = mcp.security_context.grant_escalation("actor:test", "run_command", ttl_s=60)
                self.assertEqual("https://evil.example", grant["origin"])
                result = await mcp.call_tool("run_command", {"command": "printf allowed"})
                self.assertIsNotNone(result)
                self.assertEqual(1, calls)

                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "printf blocked-again"})
                self.assertEqual(1, calls)
                self.assertTrue(any(e["event_type"] == "WEB_TO_HOST_ESCALATION" for e in telemetry.query_security_events(limit=20)))
        asyncio.run(run())

    def test_tool_invoke_cannot_bypass_web_host_gate(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, _telemetry = self._manager(td)
                shell_executed = False

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": "https://evil.example/", "tab_handle": "tab-a"}

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal shell_executed
                    shell_executed = True
                    return {"ok": True}

                @mcp.tool(name="tool_invoke")
                async def tool_invoke(tool_name: str, arguments: dict | None = None) -> dict:
                    return {"result": await mcp.call_tool(tool_name, arguments or {})}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("tool_invoke", {"tool_name": "run_command", "arguments": {"command": "whoami"}})
                self.assertIn("web_host_boundary_denied", str(ctx.exception))
                self.assertFalse(shell_executed)
        asyncio.run(run())

    def test_localhost_browser_content_does_not_trigger_untrusted_gate(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, _telemetry = self._manager(td)
                calls = 0

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": "http://127.0.0.1:9876/dashboard", "tab_handle": "tab-local"}

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal calls
                    calls += 1
                    return {"ok": True}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                await mcp.call_tool("run_command", {"command": "pwd"})
                self.assertEqual(1, calls)
        asyncio.run(run())

    def test_untrusted_state_cannot_be_laundered_by_later_localhost_observation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, _telemetry = self._manager(td)
                shell_executed = False
                urls = iter(["https://evil.example/", "http://127.0.0.1:9876/dashboard"])

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": next(urls), "tab_handle": "tab-a"}

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal shell_executed
                    shell_executed = True
                    return {"ok": True}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                state = mcp.security_context.state_for_public_session("actor:test")
                self.assertTrue(state["web_scoped"])
                self.assertEqual("untrusted_web", state["trust_level"])
                self.assertEqual("http://127.0.0.1:9876", state["current_origin"])
                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "whoami"})
                self.assertFalse(shell_executed)
        asyncio.run(run())

    def test_origin_change_invalidates_existing_one_shot_grant(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                mcp, _telemetry = self._manager(td)
                urls = iter(["https://evil.example/", "https://other.example/"])
                ran = False

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": next(urls), "tab_handle": "tab-a"}

                @mcp.tool(name="run_command")
                def run_command(command: str) -> dict:
                    nonlocal ran
                    ran = True
                    return {"ok": True}

                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                mcp.security_context.grant_escalation("actor:test", "run_command", ttl_s=60)
                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                with self.assertRaises(ToolError):
                    await mcp.call_tool("run_command", {"command": "pwd"})
                self.assertFalse(ran)
        asyncio.run(run())


class DelegatedCapabilityProfileTests(unittest.TestCase):
    def test_browser_only_profile_is_server_scoped_and_read_only_native(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            scope, permission_profile, capability_profile = _requested_agent_scope(
                workdir, "workspace_write", None, None, "trusted", "browser_only"
            )
            self.assertEqual("browser_only", capability_profile)
            self.assertEqual("browser_only", permission_profile)
            self.assertEqual("read_only", scope.access_mode.value)
            self.assertEqual(("browser",), scope.tool_families)
            self.assertTrue(evaluate_tool_scope(scope, "browser_open_url", {"url": "https://example.com"}).allowed)
            self.assertFalse(evaluate_tool_scope(scope, "run_command", {"command": "pwd"}).allowed)

    def test_browser_only_child_cannot_spawn_full_child(self) -> None:
        parent_scope = ResourceScope(tool_families=("browser",), access_mode="read_only")
        spawn_scope_decision = evaluate_tool_scope(
            parent_scope,
            "spawn_agent",
            {"provider": "codex", "prompt": "escape", "capability_profile": "full"},
            resolve_risk("spawn_agent", {"capability_profile": "full"})[1],
        )
        self.assertFalse(spawn_scope_decision.allowed)
        self.assertIn("tool_family_not_allowed", spawn_scope_decision.reasons)

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(HTTPException):
                _requested_agent_scope(
                    Path(td), "full", None, parent_scope, "browser_only", "full"
                )

    def test_developer_profile_cannot_request_browser_family_outside_preset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(HTTPException) as ctx:
                _requested_agent_scope(
                    Path(td), "workspace_write", {"tool_families": ["browser"]},
                    None, "trusted", "developer"
                )
            self.assertEqual(403, ctx.exception.status_code)


class SecurityAttentionAndAuditTests(unittest.TestCase):
    def test_security_attention_uses_existing_needs_attention_signal(self) -> None:
        manager = SteeringManager(session_ttl_s=120)
        identity = SteeringIdentity(key="security-test", source="metadata")
        session_id = manager.session_id_for(identity)
        state = manager.mark_security_attention(identity, "web_host_boundary_denied")
        self.assertEqual(session_id, state["session_id"])
        self.assertEqual("security:web_host_boundary_denied", state["last_error"])
        manager.clear_security_attention(identity)
        state = next(item for item in manager.sessions() if item["session_id"] == session_id)
        self.assertIsNone(state["last_error"])

    def test_security_audit_sanitizes_secret_like_target_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
            telemetry.record_security_event(
                session_id="sess-safe", event_type="POLICY_DENY", tool="run_command",
                tool_class="terminal", origin="https://evil.example", decision="deny",
                reason_code="test", profile="trusted", actor="test", agent_id=None,
                target_summary="token=sk-supersecret0123456789",
            )
            event = telemetry.query_security_events(limit=1)[0]
            self.assertNotIn("supersecret", str(event))
            self.assertIn("[REDACTED]", str(event))

    def test_security_event_retention_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=5)
            for index in range(12):
                telemetry.record_security_event(
                    session_id="sess-bounded", event_type="POLICY_DENY", tool="run_command",
                    tool_class="terminal", origin="https://evil.example", decision="deny",
                    reason_code=f"reason_{index}", profile="trusted", actor="test",
                    agent_id=None, target_summary="terminal:run_command",
                )
            telemetry._prune()
            events = telemetry.query_security_events(limit=100)
            self.assertEqual(5, len(events))
            self.assertEqual("reason_11", events[0]["reason_code"])


if __name__ == "__main__":
    unittest.main()
