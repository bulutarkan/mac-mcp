from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import external_mcp
from mcp_server.policy import RISK_REGISTRY, tool_availability
from tests.test_core_catalog_descriptions import _build_mcp

FAKE_SERVER = textwrap.dedent('''
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("fake-tools")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def echo(text: str, times: int = 1) -> str:
        """Repeat text."""
        return " ".join([text] * times)

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    def delete_thing(thing_id: str) -> str:
        """Delete one thing."""
        return "deleted " + thing_id

    @server.tool()
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    server.run()
''')


def _fake_entry(directory: Path) -> dict:
    script = directory / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return {"command": sys.executable, "args": [str(script)]}


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "mcp_servers.json"

    def test_claude_desktop_shapes_parse(self) -> None:
        stdio = external_mcp.parse_server("fs", {"command": "npx", "args": ["-y", "x"], "env": {"K": "v"}})
        self.assertEqual(("stdio", "npx", ("-y", "x")), (stdio.transport, stdio.command, stdio.args))
        remote = external_mcp.parse_server("exa", {"type": "streamable-http", "url": "https://mcp.exa.ai/mcp"})
        self.assertEqual("http", remote.transport)
        self.assertFalse(external_mcp.parse_server("x", {"url": "https://a", "disabled": True}).enabled)
        for name, raw in (("Bad Name", {"url": "https://a"}), ("ok", {}), ("ok", {"url": "ftp://a"}),
                          ("ok", {"command": "x", "args": "not-a-list"})):
            with self.subTest(name=name, raw=raw), self.assertRaises(external_mcp.ConfigError):
                external_mcp.parse_server(name, raw)

    def test_public_view_hides_secret_values(self) -> None:
        server = external_mcp.parse_server("exa", {"url": "https://mcp.exa.ai/mcp?exaApiKey=SECRET",
                                                   "headers": {"Authorization": "Bearer SECRET"}})
        dumped = json.dumps(server.public())
        self.assertNotIn("SECRET", dumped)
        self.assertIn("Authorization", dumped)
        stdio = external_mcp.parse_server("a", {"command": "x", "env": {"API_KEY": "SECRET"}})
        self.assertNotIn("SECRET", json.dumps(stdio.public()))

    def test_writes_are_owner_only_and_keep_other_servers(self) -> None:
        self.path.write_text(json.dumps({"mcpServers": {"keep": {"url": "https://k"}}, "other": 1}))
        external_mcp.upsert_server("exa", {"url": "https://mcp.exa.ai/mcp"}, self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual({"keep", "exa"}, set(data["mcpServers"]))
        self.assertEqual(1, data["other"])
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        external_mcp.set_server_fields("exa", self.path, enabled=False)
        self.assertFalse(external_mcp.load_servers(self.path)[0]["exa"].enabled)
        self.assertTrue(external_mcp.remove_server("exa", self.path))
        self.assertFalse(external_mcp.remove_server("exa", self.path))

    def test_import_is_all_or_nothing(self) -> None:
        with self.assertRaises(external_mcp.ConfigError):
            external_mcp.import_servers({"mcpServers": {"good": {"url": "https://g"}, "bad": {}}}, self.path)
        self.assertFalse(self.path.exists())
        names = external_mcp.import_servers('{"mcpServers": {"good": {"url": "https://g"}}}', self.path)
        self.assertEqual(["good"], names)

    def test_tool_names_are_namespaced_and_bounded(self) -> None:
        self.assertEqual("exa__web_search_exa", external_mcp.tool_name("exa", "web_search_exa"))
        self.assertEqual("gh__repo_list", external_mcp.tool_name("gh", "repo.list"))
        long_name = external_mcp.tool_name("server", "x" * 100)
        self.assertLessEqual(len(long_name), external_mcp.MAX_TOOL_NAME)


class LiveRegistrationTests(unittest.TestCase):
    """A real stdio MCP server, connected through the manager into a real Mac MCP registry."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp())
        cls.path = cls.dir / "mcp_servers.json"
        external_mcp.upsert_server("fake", _fake_entry(cls.dir), cls.path)
        with patch.object(external_mcp, "config_path", return_value=cls.path):
            cls.mcp = _build_mcp()
        cls.manager = external_mcp.manager()
        cls.manager._path = cls.path
        cls.manager.start()
        cls.manager.reload()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.manager.stop()

    def _profile(self, name: str) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": name}, clear=False)
        patcher.start()
        os.environ.pop("MAC_MCP_TOOL_PROFILE", None)
        self.addCleanup(patcher.stop)

    def _call(self, name: str, arguments: dict) -> dict:
        result = asyncio.run(self.mcp.call_tool(name, {"description": "Test external tool", **arguments}))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_status_reports_connected_tools(self) -> None:
        server = self.manager.status()["servers"][0]
        self.assertEqual(("fake", "connected"), (server["name"], server["status"]))
        self.assertEqual({"fake__echo", "fake__delete_thing", "fake__boom"}, {tool["name"] for tool in server["tools"]})

    def test_tools_are_registered_with_central_risk(self) -> None:
        self.assertEqual("external_mcp", RISK_REGISTRY["fake__echo"].family)
        self.assertTrue(tool_availability("read_only", "fake__echo")["available"])
        self.assertFalse(tool_availability("read_only", "fake__boom")["available"])
        self.assertTrue(tool_availability("standard", "fake__boom")["available"])
        # An explicitly destructive remote tool follows the profile's destructive-family rules.
        self.assertFalse(tool_availability("standard", "fake__delete_thing")["available"])
        self.assertTrue(tool_availability("trusted", "fake__delete_thing")["available"])

    def test_call_goes_through_mac_mcp_and_returns_text(self) -> None:
        self._profile("standard")
        payload = self._call("fake__echo", {"text": "hi", "times": 2})
        self.assertEqual({"ok": True, "server": "fake", "tool": "echo"}, {k: payload[k] for k in ("ok", "server", "tool")})
        self.assertEqual("hi hi", payload["text"])

    def test_remote_error_is_a_tool_error(self) -> None:
        self._profile("standard")
        with self.assertRaises(ToolError) as caught:
            self._call("fake__boom", {})
        self.assertIn("kaboom", str(caught.exception))

    def test_profile_denies_before_forwarding(self) -> None:
        self._profile("standard")
        with self.assertRaises(ToolError) as caught:
            self._call("fake__delete_thing", {"thing_id": "1"})
        self.assertIn("profile_denied", str(caught.exception))

    def test_discover_and_invoke_reach_external_tools(self) -> None:
        self._profile("standard")
        found = self._call("tool_discover", {"query": "repeat text echo"})
        self.assertEqual("fake__echo", found["tools"][0]["name"])
        invoked = self._call("tool_invoke", {"tool_name": "fake__echo", "arguments": {"text": "yo"}})
        self.assertEqual("yo", invoked["result"]["text"])

    def test_listed_tools_show_in_the_core_list(self) -> None:
        self._profile("standard")
        names = {tool.name for tool in asyncio.run(self.mcp.list_tools())}
        self.assertIn("fake__echo", names)
        self.assertNotIn("fake__delete_thing", names)  # hidden by the profile, not by listing

    def test_config_change_is_applied_without_restart(self) -> None:
        external_mcp.set_server_fields("fake", self.path, listed=False)
        try:
            self.manager.reload()
            self._profile("standard")
            names = {tool.name for tool in asyncio.run(self.mcp.list_tools())}
            self.assertNotIn("fake__echo", names)
            self.assertIn("fake__echo", {t["name"] for t in self._call("tool_discover", {"query": "echo"})["tools"]})
        finally:
            external_mcp.set_server_fields("fake", self.path, listed=True)
            self.manager.reload()

    def test_disabling_unregisters_tools(self) -> None:
        external_mcp.set_server_fields("fake", self.path, enabled=False)
        try:
            self.manager.reload()
            self.assertIsNone(self.mcp._tool_manager.get_tool("fake__echo"))
            self.assertNotIn("fake__echo", RISK_REGISTRY)
            self.assertEqual("disabled", self.manager.status()["servers"][0]["status"])
        finally:
            external_mcp.set_server_fields("fake", self.path, enabled=True)
            self.manager.reload()
        deadline = time.time() + 30
        while self.mcp._tool_manager.get_tool("fake__echo") is None and time.time() < deadline:
            time.sleep(0.1)
        self.assertIsNotNone(self.mcp._tool_manager.get_tool("fake__echo"))


class ProbeTests(unittest.TestCase):
    def test_probe_reports_a_server_that_cannot_start(self) -> None:
        server = external_mcp.parse_server("broken", {"command": "/nonexistent/mcp-server"})
        result = asyncio.run(external_mcp.probe(server, timeout=10))
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_probe_lists_tools(self) -> None:
        server = external_mcp.parse_server("fake", _fake_entry(Path(tempfile.mkdtemp())))
        result = asyncio.run(external_mcp.probe(server))
        self.assertTrue(result["ok"], result)
        self.assertIn("fake__echo", {tool["name"] for tool in result["tools"]})


@unittest.skipUnless(os.getenv("MAC_MCP_LIVE_EXA") == "1", "set MAC_MCP_LIVE_EXA=1 to call the hosted Exa MCP server")
class LiveExaTests(unittest.TestCase):
    def test_exa_search(self) -> None:
        server = external_mcp.parse_server("exa", {"url": "https://mcp.exa.ai/mcp"})
        result = asyncio.run(external_mcp.probe(server))
        self.assertTrue(result["ok"], result)
        self.assertIn("exa__web_search_exa", {tool["name"] for tool in result["tools"]})


if __name__ == "__main__":
    unittest.main()
