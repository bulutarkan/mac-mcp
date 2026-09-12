from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import browser_tabs
from mcp_server.cli import _resolve_ngrok_binary
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import (
    ApprovalSource,
    PolicyContext,
    RISK_REGISTRY,
    evaluate_profile,
    evaluate_tool_scope,
    permission_semantics,
    resolve_risk,
)
from mcp_server.policy_scope import ResourceScope, child_scope, scope_contains
from mcp_server.scoped_auth import ScopedCredentialStore
from mcp_server.security import load_settings
from mcp_server.tools_agents import (
    _build_provider_command,
    _cleanup_provider_config,
    _provider_env,
    _scope_prompt,
)
from mcp_server.tools_browser import browser_execute_js
from mcp_server.tools_terminal import run_command
import mcp_server.tools_agents as agents


class RiskAndScopeTests(unittest.TestCase):
    def test_registry_covers_current_84_tool_surface(self) -> None:
        self.assertEqual(84, len(RISK_REGISTRY))
        for required in ("run_command", "browser_observe", "browser_do", "tool_discover", "tool_invoke", "spawn_agent", "mac_act", "read_file"):
            self.assertIn(required, RISK_REGISTRY)

    def test_tool_invoke_inherits_target_risk(self) -> None:
        _, read_risk = resolve_risk("tool_invoke", {"tool_name": "read_file", "arguments": {"path": "/tmp/a"}})
        _, shell_risk = resolve_risk("tool_invoke", {"tool_name": "run_command", "arguments": {"command": "pwd"}})
        self.assertTrue(evaluate_profile("read_only", read_risk).allowed)
        self.assertFalse(evaluate_profile("read_only", shell_risk).allowed)

    def test_profiles_are_deterministic(self) -> None:
        _, read_risk = resolve_risk("read_file", {"path": "/tmp/a"})
        _, shell_risk = resolve_risk("run_command", {"command": "pwd"})
        self.assertTrue(evaluate_profile("read_only", read_risk).allowed)
        self.assertFalse(evaluate_profile("read_only", shell_risk).allowed)
        self.assertTrue(evaluate_profile("trusted", shell_risk).allowed)

    def test_permission_semantics_separates_capability_and_approval(self) -> None:
        semantics = permission_semantics("standard")
        self.assertEqual("separate_from_capability_enforcement", semantics["approval_contract"])
        self.assertFalse(semantics["ask_confirmation_is_automatic_gate"])
        self.assertEqual({source.value for source in ApprovalSource}, set(semantics["supported_approval_sources"]))
        profiles = {item["name"]: item for item in semantics["profiles"]}
        self.assertEqual({"trusted", "standard", "read_only"}, set(profiles))
        self.assertTrue(profiles["standard"]["active"])
        for profile in profiles.values():
            self.assertEqual("none", profile["approval_behavior"]["source"])
            self.assertFalse(profile["approval_behavior"]["automatic_confirmation"])

    def test_read_only_denies_write_and_standard_allowed_write_has_no_server_prompt(self) -> None:
        _, file_write = resolve_risk("write_file", {"path": "/tmp/a", "content": "x"})
        _, mkdir_write = resolve_risk("create_directory", {"path": "/tmp/a"})
        self.assertFalse(evaluate_profile("read_only", file_write).allowed)
        self.assertTrue(evaluate_profile("standard", mkdir_write).allowed)
        standard = {item["name"]: item for item in permission_semantics("standard")["profiles"]}["standard"]
        self.assertEqual("none", standard["approval_behavior"]["source"])
        self.assertFalse(standard["approval_behavior"]["automatic_confirmation"])

    def test_active_permission_profile_tracks_environment(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "read_only"}, clear=False):
            semantics = permission_semantics()
        self.assertEqual("read_only", semantics["active_profile"])
        self.assertTrue(semantics["known_profile"])
        self.assertTrue(next(item for item in semantics["profiles"] if item["name"] == "read_only")["active"])

    def test_unknown_profile_is_reported_without_inventing_approval(self) -> None:
        semantics = permission_semantics("approval_heavy")
        self.assertEqual("approval_heavy", semantics["active_profile"])
        self.assertFalse(semantics["known_profile"])
        self.assertFalse(any(item["active"] for item in semantics["profiles"]))

    def test_scope_enforces_path_browser_and_job(self) -> None:
        scope = ResourceScope(
            path_roots=("/tmp/project",), browser_tabs=("tab-a",), job_ids=("job-a",),
            tool_families=("files", "browser", "jobs"), access_mode="read_only",
        )
        self.assertTrue(evaluate_tool_scope(scope, "read_file", {"path": "/tmp/project/a.txt"}).allowed)
        self.assertFalse(evaluate_tool_scope(scope, "read_file", {"path": "/tmp/outside.txt"}).allowed)
        self.assertFalse(evaluate_tool_scope(scope, "browser_observe", {"tab_handle": "tab-b"}).allowed)
        self.assertFalse(evaluate_tool_scope(scope, "wait_jobs", {"job_ids": ["job-a", "job-b"]}).allowed)

    def test_browser_wildcard_scope_allows_list_tabs_without_handle(self) -> None:
        scope = ResourceScope(browser_tabs=("*",), tool_families=("browser",), access_mode="read_only")
        self.assertTrue(evaluate_tool_scope(scope, "browser_list_tabs", {"browser": "Safari"}).allowed)

    def test_child_scope_can_only_narrow_parent(self) -> None:
        parent = ResourceScope(path_roots=("/tmp/project",), browser_tabs=("tab-a", "tab-b"), access_mode="workspace_write")
        child = ResourceScope(path_roots=("/tmp/project/sub",), browser_tabs=("tab-a",), access_mode="read_only")
        widened = ResourceScope(path_roots=("/tmp",), browser_tabs=("tab-a",), access_mode="read_only")
        self.assertTrue(scope_contains(parent, child))
        self.assertFalse(scope_contains(parent, widened))
        self.assertEqual(child_scope(parent, child), child)


class ScopedCredentialTests(unittest.TestCase):
    def test_issue_resolve_hash_only_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "creds.sqlite3"
            store = ScopedCredentialStore(db)
            scope = ResourceScope(path_roots=("/tmp/project",), access_mode="read_only")
            token, token_id = store.issue(
                agent_id="agt_test", team_id="team_test", profile="read_only", scope=scope, ttl_s=120,
            )
            resolved = store.resolve(token)
            self.assertIsNotNone(resolved)
            self.assertEqual("agt_test", resolved.agent_id)
            con = sqlite3.connect(db)
            columns = [row[1] for row in con.execute("PRAGMA table_info(scoped_credentials)")]
            self.assertNotIn("token", columns)
            stored = con.execute("SELECT token_hash FROM scoped_credentials WHERE token_id=?", (token_id,)).fetchone()[0]
            con.close()
            self.assertNotEqual(token, stored)
            self.assertNotIn(token, db.read_bytes().decode("latin1", errors="ignore"))
            store.revoke_token_id(token_id)
            self.assertIsNone(store.resolve(token))


class CompactToolSurfaceTests(unittest.TestCase):
    def test_core_is_default_and_full_is_opt_in(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                mcp = ObservedFastMCP(name="test", telemetry=manager)

                @mcp.tool(name="run_command")
                def core_tool(command: str) -> dict:
                    return {"ok": True}

                @mcp.tool(name="browser_find")
                def browser_find_tool(query: str = "", role: str | None = None) -> dict:
                    return {"ok": True}

                @mcp.tool(name="browser_act")
                def browser_act_tool(actions: list[dict] | None = None) -> dict:
                    return {"ok": True}

                @mcp.tool(name="process_list")
                def hidden_tool(filter: str | None = None) -> dict:
                    return {"ok": True}

                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("MAC_MCP_TOOL_PROFILE", None)
                    names = {tool.name for tool in await mcp.list_tools()}
                self.assertIn("run_command", names)
                self.assertIn("browser_find", names)
                self.assertIn("browser_act", names)
                self.assertNotIn("process_list", names)

                with patch.dict(os.environ, {"MAC_MCP_TOOL_PROFILE": "full"}, clear=False):
                    names = {tool.name for tool in await mcp.list_tools()}
                self.assertIn("run_command", names)
                self.assertIn("process_list", names)

        asyncio.run(run())


class DispatchAndTelemetryTests(unittest.TestCase):
    def test_scope_denial_is_tool_error_and_telemetry_is_denied(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                manager = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                context = PolicyContext(
                    profile="read_only", actor="agent:agt_test", agent_id="agt_test",
                    scope=ResourceScope(path_roots=("/tmp/allowed",), access_mode="read_only"),
                )
                mcp = ObservedFastMCP(name="test", telemetry=manager, policy_context_provider=lambda: context)

                @mcp.tool(name="read_file")
                def read_file(path: str) -> dict:
                    return {"ok": True, "result": "should not run"}

                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("read_file", {"path": "/tmp/outside"})
                self.assertIn("scope_denied", str(ctx.exception))
                event = manager.query_events(limit=1)[0]
                self.assertEqual("denied", event["status"])
                self.assertEqual("scope_denied", event["policy_decision"])
                self.assertEqual("agt_test", event["agent_id"])

        asyncio.run(run())


class ShellSecurityTests(unittest.TestCase):
    def test_shell_disabled_fails_before_process_creation(self) -> None:
        settings = replace(load_settings(), allow_shell=False)
        with patch("mcp_server.tools_terminal.subprocess.Popen") as popen:
            with self.assertRaises(HTTPException) as ctx:
                run_command(settings, "touch /tmp/should-never-run")
        self.assertEqual(403, ctx.exception.status_code)
        popen.assert_not_called()


class DelegatedProviderTests(unittest.TestCase):
    def test_codex_resume_reapplies_sandbox_and_scoped_mcp(self) -> None:
        meta = {
            "provider": "codex", "binary": "/opt/homebrew/bin/codex", "cwd": "/tmp",
            "model": "gpt-5.6-luna", "reasoning": "high", "resume_session_id": "thread_123",
            "access_mode": "read_only", "scoped_mcp": True,
            "mcp_endpoint": "http://127.0.0.1:8765/mcp",
        }
        cmd = _build_provider_command(meta, "PROMPT", Path("/tmp/result.txt"))
        joined = " ".join(cmd)
        self.assertIn('sandbox_mode="read-only"', joined)
        self.assertIn('approval_policy="never"', joined)
        self.assertIn("bearer_token_env_var", joined)
        self.assertIn('model_reasoning_effort="high"', joined)

    def test_opencode_prompt_keeps_native_tool_guard(self) -> None:
        scope = ResourceScope(path_roots=("/tmp/project",), browser_tabs=("tab-a",), access_mode="workspace_write")
        prompt = _scope_prompt(scope, "standard", "opencode")
        self.assertIn("native bash/filesystem tools are not constrained", prompt)
        self.assertIn("mandatory behavioral boundary", prompt)
        self.assertIn("Mac MCP tool calls are enforced server-side", prompt)

    def test_opencode_openrouter_config_uses_env_key_without_secret_in_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_agents = agents.AGENTS_DIR
            agents.AGENTS_DIR = Path(td)
            self.addCleanup(setattr, agents, "AGENTS_DIR", old_agents)
            agent_id = "agt_cfg"
            (agents.AGENTS_DIR / agent_id).mkdir(parents=True)
            fake_env = {"PATH": os.environ.get("PATH", ""), "OPENROUTER_API_KEY": "secret-value"}
            with patch("mcp_server.tools_agents._base_env", return_value=dict(fake_env)):
                env, cleanup = _provider_env(
                    agent_id,
                    {"provider": "opencode", "model": "openrouter/nex-agi/nex-n2.5-pro:free"},
                    "mcpagt_scoped-test",
                )
            try:
                config = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.jsonc"
                text = config.read_text(encoding="utf-8")
                payload = json.loads(text)
                self.assertNotIn("secret-value", text)
                self.assertEqual("{env:OPENROUTER_API_KEY}", payload["provider"]["openrouter"]["options"]["apiKey"])
                self.assertIn("nex-agi/nex-n2.5-pro:free", payload["provider"]["openrouter"]["models"])
                self.assertEqual("openrouter/nex-agi/nex-n2.5-mini:free", payload["small_model"])
            finally:
                _cleanup_provider_config(cleanup)


class BrowserConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_tabs._REGISTRY.clear()
        browser_tabs._RESOURCE_LOCKS.clear()

    def test_same_tab_reuses_lock_and_different_tabs_do_not(self) -> None:
        a1 = browser_tabs._resource_lock("Safari", "tab-a")
        a2 = browser_tabs._resource_lock("Safari", "tab-a")
        b = browser_tabs._resource_lock("Safari", "tab-b")
        self.assertIs(a1, a2)
        self.assertIsNot(a1, b)

    def test_tab_lease_reresolves_handle_after_index_shift(self) -> None:
        first = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "3001", "title": "A", "url": "https://example.com/a"},
            {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "3002", "title": "B", "url": "https://example.com/b"},
        ]
        shifted = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "9999", "title": "New", "url": "https://example.com/new"},
            {**first[0], "tab_index": 2},
            {**first[1], "tab_index": 3},
        ]
        with patch("mcp_server.browser_tabs._scan", side_effect=[first, shifted]):
            handle = browser_tabs.list_tabs("Safari")[1]["tab_handle"]
            with browser_tabs.tab_lease("Safari", tab_handle=handle) as target:
                self.assertEqual(3, target.tab_index)
                self.assertEqual("3002", target.native_id)

    def test_busy_tab_fails_fast_with_retryable_409_and_recovers(self) -> None:
        rows = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "4001", "title": "Busy", "url": "https://example.com/busy",
        }]
        entered = threading.Event()
        release = threading.Event()

        with patch("mcp_server.browser_tabs._scan", return_value=rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]

            def holder() -> None:
                with browser_tabs.tab_lease("Safari", tab_handle=handle):
                    entered.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(timeout=1))

            started = time.perf_counter()
            with self.assertRaises(HTTPException) as ctx:
                with browser_tabs.tab_lease("Safari", tab_handle=handle):
                    pass
            elapsed = time.perf_counter() - started

            self.assertLess(elapsed, 0.25)
            self.assertEqual(409, ctx.exception.status_code)
            self.assertEqual("tab_busy", ctx.exception.detail["error"])
            self.assertTrue(ctx.exception.detail["retryable"])
            self.assertEqual(1000, ctx.exception.detail["retry_after_ms"])
            self.assertEqual(handle, ctx.exception.detail["tab_handle"])
            self.assertEqual("1", ctx.exception.headers["Retry-After"])

            release.set()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

            with browser_tabs.tab_lease("Safari", tab_handle=handle) as target:
                self.assertEqual(handle, target.tab_handle)

    def test_same_thread_reentry_and_different_tab_remain_allowed(self) -> None:
        rows = [
            {"browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
             "native_id": "5001", "title": "A", "url": "https://example.com/a"},
            {"browser": "Safari", "window_index": 1, "tab_index": 2, "active": False,
             "native_id": "5002", "title": "B", "url": "https://example.com/b"},
        ]
        with patch("mcp_server.browser_tabs._scan", return_value=rows):
            handles = [row["tab_handle"] for row in browser_tabs.list_tabs("Safari")]
            with browser_tabs.tab_lease("Safari", tab_handle=handles[0]):
                with browser_tabs.tab_lease("Safari", tab_handle=handles[0]) as nested:
                    self.assertEqual(handles[0], nested.tab_handle)
                with browser_tabs.tab_lease("Safari", tab_handle=handles[1]) as other:
                    self.assertEqual(handles[1], other.tab_handle)

    def test_browser_execute_js_propagates_tab_busy_before_browser_execution(self) -> None:
        rows = [{
            "browser": "Safari", "window_index": 1, "tab_index": 1, "active": True,
            "native_id": "6001", "title": "Busy", "url": "https://example.com/busy",
        }]
        errors = []
        with patch("mcp_server.browser_tabs._scan", return_value=rows):
            handle = browser_tabs.list_tabs("Safari")[0]["tab_handle"]
            with browser_tabs.tab_lease("Safari", tab_handle=handle):
                def contender() -> None:
                    try:
                        browser_execute_js(load_settings(), "Safari", "location.href", tab_handle=handle)
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=contender)
                thread.start()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())

        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], HTTPException)
        self.assertEqual(409, errors[0].status_code)
        self.assertEqual("tab_busy", errors[0].detail["error"])


class NgrokDiscoveryTests(unittest.TestCase):
    def test_apple_silicon_fallback_is_used_when_path_is_minimal(self) -> None:
        def fake_is_file(path: Path) -> bool:
            return str(path) == "/opt/homebrew/bin/ngrok"

        with patch("mcp_server.cli.shutil.which", return_value=None), \
             patch("pathlib.Path.is_file", fake_is_file), \
             patch("os.access", return_value=True), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual("/opt/homebrew/bin/ngrok", _resolve_ngrok_binary())


if __name__ == "__main__":
    unittest.main()
