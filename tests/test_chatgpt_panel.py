"""ChatGPT sidebar protocol, least-privilege data and settings regression tests."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from mcp.types import InitializeRequestParams
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient
from mcp.server.fastmcp.exceptions import ResourceError, ToolError

from mcp_server.chatgpt_client_gate import CHATGPT_PANEL_LEGACY_URIS, is_chatgpt_client
from mcp_server.chatgpt_panel import (
    PANEL_URI, panel_model_catalog, panel_snapshot, register_chatgpt_panel, save_panel_preference,
)
from mcp_server.policy import PolicyContext, annotations_for_tool, tool_availability
from mcp_server.policy_scope import ResourceScope
from mcp_server.observability import ObservedFastMCP, TelemetryManager


class FakeTelemetry:
    def summary(self, _hours):
        return {"total_calls": 23, "error_calls": 2, "avg_duration_ms": 41, "uptime_seconds": 100}

    def active_calls(self):
        return []


class ChatGPTPanelTests(unittest.TestCase):
    @staticmethod
    def session_context(client_name, *, ui_capable=False):
        capabilities = ({"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}} if ui_capable else {})
        params = InitializeRequestParams.model_validate({"protocolVersion": "2025-11-25", "clientInfo": {"name": client_name, "version": "1.0"}, "capabilities": capabilities})
        session = SimpleNamespace(client_params=params)
        return SimpleNamespace(request_context=SimpleNamespace(session=session))

    def test_global_and_thread_entrypoints_and_renderable_resource(self):
        async def run():
            mcp = FastMCP("control-center-test")
            register_chatgpt_panel(mcp, FakeTelemetry(), Mock())
            tools = {tool.name: tool for tool in await mcp.list_tools()}
            entry = tools["open_mac_mcp_panel"]
            self.assertEqual(PANEL_URI, entry.meta["ui"]["resourceUri"])
            self.assertEqual([{"type": "global"}, {"type": "thread"}], entry.meta["openai/ui"]["entrypoints"])
            self.assertEqual([], entry.inputSchema.get("required", []))
            self.assertTrue(entry.icons)
            contents = list(await mcp.read_resource(PANEL_URI))[0]
            self.assertEqual("text/html;profile=mcp-app", contents.mime_type)
            self.assertIn('id="overview-agents"', contents.content)
            self.assertIn('id="usage-providers"', contents.content)
            self.assertIn('id="agent-edit"', contents.content)
            self.assertNotIn('setInterval', contents.content)
            self.assertIn("ui/initialize", contents.content)  # Bundled official MCP Apps bridge.
            self.assertNotIn("MCP_API_KEY", contents.content)
            self.assertNotIn("dashboard-token", contents.content)
            self.assertEqual("fullscreen", contents.meta["openai/ui"]["preferredDisplayMode"])
        asyncio.run(run())

    @patch("mcp_server.chatgpt_panel.provider_usage_summary")
    @patch("mcp_server.chatgpt_panel.list_agents")
    def test_snapshot_projects_no_agent_prompts_or_secret_settings(self, get_agents, provider_usage):
        get_agents.return_value = {"agents": [{
            "agent_id": "agent-a123456789", "status": "running", "provider": "codex",
            "model": "gpt-6-luna", "role": "reviewer", "prompt": "private task details", "token": "secret-token",
        }]}
        provider_usage.return_value = {"providers": {"codex": {
            "turns": 3, "input_tokens": 98, "output_tokens": 15, "total_tokens": 113,
        }}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({
                "private": {"token": "secret-value"},
                "server": {"public_url": "https://mac.example.test/mcp?ApiKey=secret-key", "public_endpoint_mode": "cloudflare"},
                "subagents": {"default": {"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"},
                              "providers": {"codex": {"enabled": True, "binary_path": "/secret/path"},
                                            "opencode": {"enabled": False}}},
                "notifications": {"agent_completion": True},
                "tool_activity": {"show_bubble": True, "require_descriptions": False},
            }))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                result = panel_snapshot(FakeTelemetry(), Mock())
        self.assertEqual(23, result["stats"]["calls"])
        self.assertEqual(1, result["active_agents"])
        view = result["settings"]
        self.assertEqual({"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"}, view["default_agent"])
        self.assertEqual(["codex"], view["enabled_providers"])
        self.assertEqual({"agent_completion": True, "activity_bubble": False}, view["notifications"])
        self.assertEqual("mac.example.test", view["connection"]["public_host"])
        self.assertEqual("cloudflare", view["connection"]["endpoint_mode"])
        self.assertNotIn("session_ttl_minutes", str(result))
        self.assertEqual(113, result["providers"][0]["total_tokens"])
        self.assertNotIn("private task details", str(result))
        self.assertNotIn("secret-token", str(result))
        self.assertNotIn("secret-value", str(result))
        self.assertNotIn("secret-key", str(result))
        self.assertNotIn("/secret/path", str(result))

    @patch("mcp_server.chatgpt_panel.agent_catalog")
    def test_default_agent_is_validated_and_preserves_unrelated_config(self, catalog):
        catalog.return_value = {"providers": {"codex": {
            "available": True, "reasoning_values": ["low", "high", "max"],
            "model_items": [{"id": "gpt-6-luna", "reasoning_values": ["high", "max"]},
                            {"id": "gpt-6.1-sol", "reasoning_values": ["low"]}],
        }}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            original = {"steering": {"session_ttl_minutes": 9},
                        "subagents": {"providers": {"codex": {"enabled": True}, "opencode": {"enabled": False}}},
                        "private": {"token": "keep-secret"}}
            path.write_text(json.dumps(original))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                rejected = (
                    ("session_ttl_minutes", 35),
                    ("permissions", {"provider": "codex"}),
                    ("default_agent", "codex"),
                    ("default_agent", {"provider": "codex", "binary_path": "/tmp/x"}),
                    ("default_agent", {"provider": "opencode"}),  # disabled
                    ("default_agent", {"provider": "unknown"}),
                    ("default_agent", {"provider": "codex", "model": "not-in-catalog"}),
                    ("default_agent", {"provider": "codex", "model": "gpt-6.1-sol", "reasoning": "max"}),
                    ("default_agent", {"provider": "codex", "reasoning": "ultra"}),
                    ("default_agent", {"provider": "codex", "model": 5}),
                )
                for name, value in rejected:
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        save_panel_preference(name, value, Mock())
                self.assertEqual(original, json.loads(path.read_text()))
                save_panel_preference("default_agent", {"provider": "Codex", "model": "gpt-6-luna", "reasoning": "MAX"}, Mock())
                self.assertEqual("gpt-6-luna", catalog.call_args.kwargs["model_filter"])
                updated = json.loads(path.read_text())
                self.assertEqual({"provider": "codex", "model": "gpt-6-luna", "reasoning": "max"}, updated["subagents"]["default"])
                self.assertEqual(original["subagents"]["providers"], updated["subagents"]["providers"])
                self.assertEqual(original["steering"], updated["steering"])
                self.assertEqual(original["private"], updated["private"])
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                save_panel_preference("default_agent", {"provider": "codex", "model": None, "reasoning": ""}, Mock())
                self.assertEqual({"provider": "codex"}, json.loads(path.read_text())["subagents"]["default"])

    @patch("mcp_server.chatgpt_panel.agent_catalog")
    def test_model_catalog_is_bounded_and_only_for_enabled_providers(self, catalog):
        catalog.return_value = {"providers": {"codex": {"available": True, "reasoning_values": ["high"],
            "model_items": [{"id": "m" * 300, "display_name": "M", "reasoning_values": ["high"] * 40,
                             "binary_path": "/secret"}]}}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"subagents": {"providers": {"codex": {"enabled": True}}}}))
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                result = panel_model_catalog(Mock(), "codex")
                for provider in ("opencode", "chatgpt", "nope"):
                    with self.assertRaises(ValueError):
                        panel_model_catalog(Mock(), provider)
        self.assertEqual(120, len(result["models"][0]["id"]))
        self.assertEqual(12, len(result["models"][0]["reasoning"]))
        self.assertNotIn("/secret", str(result))

    def test_corrupted_settings_reject_write_without_repairing_by_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text("broken")
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}):
                with self.assertRaises((RuntimeError, ValueError)):
                    save_panel_preference("default_agent", {"provider": "codex"}, Mock())
                self.assertEqual("broken", path.read_text())

    def test_read_only_can_open_panel_but_not_change_settings(self):
        for tool_name in ("open_mac_mcp_panel", "mac_mcp_panel_state"):
            self.assertTrue(tool_availability("read_only", tool_name)["available"])
            self.assertTrue(annotations_for_tool(tool_name).readOnlyHint)
        self.assertFalse(tool_availability("read_only", "mac_mcp_panel_setting")["available"])
        self.assertFalse(annotations_for_tool("mac_mcp_panel_setting").readOnlyHint)

    def test_actual_mcp_tool_call_denied_under_read_only(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                mcp = ObservedFastMCP(
                    name="panel-policy-test", telemetry=telemetry,
                    policy_context_provider=lambda: PolicyContext(profile="read_only", actor="test"),
                )
                register_chatgpt_panel(mcp, telemetry, Mock())
                # No caller identity means no ChatGPT extension advertised.
                offered = {tool.name for tool in await mcp.list_tools()}
                self.assertNotIn("open_mac_mcp_panel", offered)
                self.assertNotIn("mac_mcp_panel_state", offered)
                with self.assertRaisesRegex(ToolError, "ChatGPT-only"):
                    await mcp.call_tool("mac_mcp_panel_state", {})

                # ChatGPT gets read tools, but the normal read-only policy still
                # blocks the setting write.
                with patch.object(mcp, "get_context", return_value=self.session_context("ChatGPT")):
                    offered = {tool.name for tool in await mcp.list_tools()}
                    self.assertIn("open_mac_mcp_panel", offered)
                    self.assertIn("mac_mcp_panel_state", offered)
                    self.assertNotIn("mac_mcp_panel_setting", offered)
                    with self.assertRaisesRegex(ToolError, "profile_denied"):
                        await mcp.call_tool("mac_mcp_panel_setting", {
                            "name": "default_agent", "value": {"provider": "codex"},
                        })
        asyncio.run(run())

    def test_other_clients_never_see_chatgpt_ui_tools_or_resource(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                mcp = ObservedFastMCP(name="isolation-test", telemetry=telemetry)
                register_chatgpt_panel(mcp, telemetry, Mock())
                allowed = ("ChatGPT", "ChatGPT Desktop", "com.openai.chatgpt.desktop")
                denied = ("Claude Desktop", "Claude Code", "Codex CLI", "OpenCode",
                          "Cursor", "unknown", "", "not-chatgpt", "Gemini", "chatgpt-web-cli", "ChatGPT CLI", "openai-mcp", "openai-mcp (codex)")
                with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(td) / 'config.json')}):
                    for client in denied:
                        with self.subTest(client=client), patch.object(mcp, "get_context", return_value=self.session_context(client)):
                            self.assertFalse(is_chatgpt_client(mcp))
                            tools = {t.name for t in await mcp.list_available_tools(compact=False)}
                            self.assertTrue(tools.isdisjoint({"open_mac_mcp_panel", "mac_mcp_panel_state", "mac_mcp_panel_setting"}))
                            resources = {str(r.uri) for r in await mcp.list_resources()}
                            self.assertNotIn(PANEL_URI, resources)
                            with self.assertRaisesRegex(ToolError, "ChatGPT-only"):
                                await mcp.call_tool("open_mac_mcp_panel", {})
                            for uri in (PANEL_URI, *CHATGPT_PANEL_LEGACY_URIS):
                                with self.assertRaisesRegex(ResourceError, "ChatGPT-only"):
                                    await mcp.read_resource(uri)
                    # ChatGPT's plugin runtime may suffix the label, e.g. "openai-mcp (codex)".
                    for client in ("openai-mcp", "openai-mcp (codex)"):
                        with self.subTest(client=client), patch.object(mcp, "get_context", return_value=self.session_context(client, ui_capable=True)):
                            self.assertTrue(is_chatgpt_client(mcp))
                            self.assertIn("open_mac_mcp_panel", {tool.name for tool in await mcp.list_tools()})
                            self.assertIn('class="tabs"', list(await mcp.read_resource(PANEL_URI))[0].content)
                    for client in ("openai-mcp-codex", "openai-mcp (codex) extra", "codex-mcp-client"):
                        with self.subTest(client=client), patch.object(mcp, "get_context", return_value=self.session_context(client, ui_capable=True)):
                            self.assertFalse(is_chatgpt_client(mcp))
                    for client in allowed:
                        with self.subTest(client=client), patch.object(mcp, "get_context", return_value=self.session_context(client)):
                            self.assertTrue(is_chatgpt_client(mcp))
                            tools = {t.name for t in await mcp.list_available_tools(compact=False)}
                            self.assertIn("open_mac_mcp_panel", tools)
                            resources = {str(r.uri) for r in await mcp.list_resources()}
                            self.assertIn(PANEL_URI, resources)
                            # Hosts that cached an earlier resourceUri still get the
                            # current UI, but legacy URIs are never advertised.
                            self.assertTrue(resources.isdisjoint(CHATGPT_PANEL_LEGACY_URIS))
                            for uri in CHATGPT_PANEL_LEGACY_URIS:
                                legacy = list(await mcp.read_resource(uri))[0]
                                self.assertEqual("text/html;profile=mcp-app", legacy.mime_type)
                                self.assertIn('class="tabs"', legacy.content)
        asyncio.run(run())

    def test_isolated_http_mcp_initialize_exposes_ui_only_to_chatgpt(self):
        """Verify actual stateful HTTP sessions, not just mocked clientInfo."""
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            mcp = ObservedFastMCP(
                name="panel-http-isolation-test", telemetry=telemetry,
                policy_context_provider=lambda: PolicyContext(profile="read_only", actor="test"),
                transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]),
            )
            @mcp.tool(name="search_files")
            async def _common_tool() -> dict:
                return {"ok": True}

            register_chatgpt_panel(mcp, telemetry, Mock())
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(Path(td) / "settings.json")}), \
                 TestClient(mcp.streamable_http_app(), base_url="http://localhost") as client:
                offered = {}
                resources = {}
                for name in ("Claude Desktop", "ChatGPT", "openai-mcp"):
                    headers = {
                        "accept": "application/json, text/event-stream",
                        "content-type": "application/json",
                    }
                    initialize = client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25", "capabilities": ({"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}} if name == "openai-mcp" else {}),
                            "clientInfo": {"name": name, "version": "1.0"},
                        },
                    })
                    self.assertEqual(200, initialize.status_code)
                    self.assertTrue(initialize.headers.get("mcp-session-id"))
                    headers["mcp-session-id"] = initialize.headers["mcp-session-id"]
                    initialized = client.post(
                        "/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                    self.assertEqual(202, initialized.status_code)
                    tool_list = client.post(
                        "/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    )
                    resource_list = client.post(
                        "/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
                    )
                    self.assertEqual(200, tool_list.status_code)
                    self.assertEqual(200, resource_list.status_code)
                    offered[name] = tool_list.text
                    resources[name] = resource_list.text
                    ui_response = client.post(
                        "/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": PANEL_URI}},
                    )
                    if name == "Claude Desktop":
                        self.assertNotIn('<!doctype html>', ui_response.text)
                    else:
                        self.assertEqual(200, ui_response.status_code)
                        self.assertIn('<!doctype html>', ui_response.text)
                        self.assertIn('text/html;profile=mcp-app', ui_response.text)
                    client.delete("/mcp", headers=headers)
                self.assertNotIn("open_mac_mcp_panel", offered["Claude Desktop"])
                self.assertNotIn(PANEL_URI, resources["Claude Desktop"])
                self.assertIn("open_mac_mcp_panel", offered["ChatGPT"])
                self.assertIn(PANEL_URI, resources["ChatGPT"])
                self.assertIn("open_mac_mcp_panel", offered["openai-mcp"])
                self.assertIn(PANEL_URI, resources["openai-mcp"])
                self.assertIn("search_files", offered["ChatGPT"])
                self.assertIn("search_files", offered["Claude Desktop"])

    def test_optional_chatgpt_extension_toggle_and_invalid_config_fail_closed(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                config = Path(td) / 'settings.json'
                telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
                mcp = ObservedFastMCP(name="toggle-test", telemetry=telemetry)
                register_chatgpt_panel(mcp, telemetry, Mock())
                with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(config)}), patch.object(mcp, "get_context", return_value=self.session_context("ChatGPT")):
                    self.assertIn("open_mac_mcp_panel", {t.name for t in await mcp.list_tools()})
                    for body in ({"chatgpt_extensions": {"enabled": False}},
                                 {"chatgpt_extensions": {"enabled": "true"}},
                                 {"chatgpt_extensions": []}):
                        config.write_text(json.dumps(body))
                        self.assertNotIn("open_mac_mcp_panel", {t.name for t in await mcp.list_tools()})
                    config.write_text('invalid json')
                    self.assertNotIn("open_mac_mcp_panel", {t.name for t in await mcp.list_tools()})
                    config.write_text(json.dumps({"chatgpt_extensions": {"enabled": True}}))
                    self.assertIn("open_mac_mcp_panel", {t.name for t in await mcp.list_tools()})
        asyncio.run(run())

    @patch("mcp_server.chatgpt_panel.provider_usage_summary", return_value={"providers": {}})
    @patch("mcp_server.chatgpt_panel.list_agents", return_value={"agents": []})
    def test_scoped_agent_cannot_see_write_control(self, _agents, _usage):
        context = PolicyContext(
            profile="trusted", actor="test",
            scope=ResourceScope(tool_families=("browser",)),
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text('{}')
            with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)}), \
                 patch("mcp_server.chatgpt_panel.current_policy_context", return_value=context):
                snapshot = panel_snapshot(FakeTelemetry(), Mock())
        self.assertFalse(snapshot["settings_writable"])


    @patch("mcp_server.chatgpt_panel.provider_usage_summary", return_value={"providers": {}})
    @patch("mcp_server.chatgpt_panel.list_agents", return_value={"agents": []})
    def test_embedded_ui_intent_description_works_with_toggle_on_or_off(self, _agents, _usage):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                settings_path = Path(td) / "settings.json"
                for enabled in (True, False):
                    telemetry = TelemetryManager(db_path=Path(td) / f"telemetry-{enabled}.db")
                    mcp = ObservedFastMCP(
                        name="panel-intent-test", telemetry=telemetry,
                        policy_context_provider=lambda: PolicyContext(profile="trusted", actor="test"),
                        intent_descriptions_provider=lambda: enabled,
                    )
                    register_chatgpt_panel(mcp, telemetry, Mock())
                    with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings_path)}), \
                         patch.object(mcp, "get_context", return_value=self.session_context("ChatGPT")):
                        entry = next(tool for tool in await mcp.list_tools() if tool.name == "open_mac_mcp_panel")
                        self.assertNotIn("description", (entry.inputSchema.get("properties") or {}))
                        opened = await mcp.call_tool("open_mac_mcp_panel", {})
                        self.assertIn("ok", str(opened))
                        payload = {
                            "description": "Refresh Mac MCP Control Center status and usage",
                        }
                        result = await mcp.call_tool("mac_mcp_panel_state", payload)
                        self.assertIn("ok", str(result))
                        if enabled:
                            with self.assertRaisesRegex(ToolError, "intent_description_required"):
                                await mcp.call_tool("mac_mcp_panel_state", {})
                        else:
                            result = await mcp.call_tool("mac_mcp_panel_state", {})
                            self.assertIn("ok", str(result))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
