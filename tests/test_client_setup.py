from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import client_setup


class ClientSetupTests(unittest.TestCase):
    """One-click setup writes `mac-mcp stdio` into the app's config: no address, no key."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="mac-mcp-client-setup-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.cli = self.home / ".local" / "bin" / "mac-mcp"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("#!/bin/sh\n")
        self.cli.chmod(0o755)
        env = patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.cli.parent),
                                      "MAC_MCP_CLI": "", "CODEX_HOME": "", "XDG_CONFIG_HOME": "",
                                      "KIMI_CODE_HOME": "", "GROK_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        # Only the temp home: the real /opt/homebrew/bin may hold the apps' CLIs.
        paths = patch.object(client_setup, "EXTRA_PATHS", ("~/.local/bin",))
        paths.start()
        self.addCleanup(paths.stop)
        self.calls: list[list[str]] = []

    def fake_run(self, args, **kwargs):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def tool(self, name: str) -> Path:
        path = self.cli.parent / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_claude_code_is_added_through_its_cli_without_a_key(self) -> None:
        claude = self.tool("claude")
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"mac-mcp": {"type": "http"}}}))
        result = client_setup.connect("claude-code", run=self.fake_run)
        self.assertEqual([str(claude), "mcp", "remove", "mac-mcp", "--scope", "user"], self.calls[0])
        self.assertEqual([str(claude), "mcp", "add", "--scope", "user", "mac-mcp", "--", str(self.cli), "stdio"],
                         self.calls[1])
        self.assertIn("new Claude Code session", result["next"])

    def test_codex_is_added_through_its_cli(self) -> None:
        codex = self.tool("codex")
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()

        def run(args, **kwargs):
            self.calls.append(list(args))
            config.write_text('[mcp_servers.mac-mcp]\ncommand = "x"\n')
            return subprocess.CompletedProcess(args, 0, "", "")

        client_setup.connect("codex", run=run)
        self.assertEqual([str(codex), "mcp", "add", "mac-mcp", "--", str(self.cli), "stdio"], self.calls[-1])

    def test_a_failing_cli_is_reported(self) -> None:
        self.tool("claude")

        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "boom\nError: scope is wrong")

        with self.assertRaisesRegex(client_setup.SetupError, "scope is wrong"):
            client_setup.connect("claude-code", run=run)

    def test_claude_desktop_config_keeps_everything_else(self) -> None:
        config = self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {"other": {"command": "o"}}}))
        config.chmod(0o640)
        with patch.object(client_setup, "_desktop_app", return_value=Path("/Applications/Claude.app")):
            result = client_setup.connect("claude-desktop", run=self.fake_run)
        data = json.loads(config.read_text())
        self.assertEqual({"x": 1}, data["preferences"])
        self.assertEqual({"command": "o"}, data["mcpServers"]["other"])
        self.assertEqual({"command": str(self.cli), "args": ["stdio"]}, data["mcpServers"]["mac-mcp"])
        self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))
        self.assertTrue(config.with_name(config.name + ".before-mac-mcp").exists())
        self.assertIn("Quit Claude", result["next"])
        self.assertEqual([], self.calls)

    def test_opencode_with_comments_is_left_alone(self) -> None:
        folder = self.home / ".config" / "opencode"
        folder.mkdir(parents=True)
        original = '{\n  // mine\n  "model": "x"\n}\n'
        (folder / "opencode.jsonc").write_text(original)
        with self.assertRaisesRegex(client_setup.SetupError, "not plain JSON"):
            client_setup.connect("opencode", run=self.fake_run)
        self.assertEqual(original, (folder / "opencode.jsonc").read_text())

    def test_opencode_gets_a_local_command_server(self) -> None:
        self.tool("opencode")
        client_setup.connect("opencode", run=self.fake_run)
        data = json.loads((self.home / ".config" / "opencode" / "opencode.json").read_text())
        self.assertEqual({"type": "local", "command": [str(self.cli), "stdio"], "enabled": True}, data["mcp"]["mac-mcp"])

    def test_kimi_gets_a_stdio_server_beside_the_owners_own(self) -> None:
        self.tool("kimi")
        config = self.home / ".kimi-code" / "mcp.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"mcpServers": {"github": {"url": "https://example.invalid"}}}))
        result = client_setup.connect("kimi", run=self.fake_run)
        servers = json.loads(config.read_text())["mcpServers"]
        self.assertEqual({"url": "https://example.invalid"}, servers["github"])
        self.assertEqual({"transport": "stdio", "command": str(self.cli), "args": ["stdio"]}, servers["mac-mcp"])
        self.assertEqual([], self.calls)
        self.assertIn("new Kimi Code session", result["next"])

    def test_grok_is_added_through_its_cli(self) -> None:
        grok = self.tool("grok")
        config = self.home / ".grok" / "config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.mac-mcp]\nurl = "http://127.0.0.1:8765/mcp"\n')

        def run(args, **kwargs):
            self.calls.append(list(args))
            config.write_text(f'[mcp_servers.mac-mcp]\ncommand = "{self.cli}"\nargs = ["stdio"]\n')
            return subprocess.CompletedProcess(args, 0, "", "")

        client_setup.connect("grok", run=run)
        self.assertEqual([str(grok), "--no-auto-update", "mcp", "remove", "mac-mcp"], self.calls[0])
        self.assertEqual([str(grok), "--no-auto-update", "mcp", "add", "mac-mcp", "--", str(self.cli), "stdio"],
                         self.calls[1])
        app = {item["id"]: item for item in client_setup.apps_status()}["grok"]
        self.assertEqual((True, True, True), (app["installed"], app["added"], app["keyless"]))

    def test_kimi_and_grok_not_installed_are_reported(self) -> None:
        for app in ("kimi", "grok"):
            with self.assertRaises(client_setup.SetupError):
                client_setup.connect(app, run=self.fake_run)
        apps = {item["id"]: item for item in client_setup.apps_status()}
        self.assertFalse(apps["kimi"]["installed"])
        self.assertFalse(apps["grok"]["installed"])

    def test_status_lists_installed_and_added_apps(self) -> None:
        self.tool("claude")
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"mac-mcp": {}}}))
        with patch.object(client_setup, "_desktop_app", return_value=None):
            apps = {app["id"]: app for app in client_setup.apps_status()}
        self.assertEqual({"installed": True, "added": True}, {k: apps["claude-code"][k] for k in ("installed", "added")})
        self.assertFalse(apps["codex"]["installed"])
        self.assertFalse(apps["claude-desktop"]["installed"])

    def test_no_secret_is_ever_written(self) -> None:
        self.tool("opencode")
        with patch.dict(os.environ, {"MCP_API_KEY": "secret-value-0123456789"}):
            client_setup.connect("opencode", run=self.fake_run)
        self.assertNotIn("secret-value", (self.home / ".config" / "opencode" / "opencode.json").read_text())


class KeylessDetectionTests(ClientSetupTests):
    def test_old_keyed_entries_are_told_apart_from_one_click_setup(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"mac-mcp": {
            "type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer k"}}}}))
        rows = {row["id"]: row for row in client_setup.apps_status()}
        self.assertEqual((True, False), (rows["claude-code"]["added"], rows["claude-code"]["keyless"]))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"mac-mcp": {
            "type": "stdio", "command": str(self.cli), "args": ["stdio"]}}}))
        self.assertTrue({row["id"]: row for row in client_setup.apps_status()}["claude-code"]["keyless"])
        self.assertIsNone({row["id"]: row for row in client_setup.apps_status()}["codex"]["keyless"])

    def test_backups_of_edited_configs_are_owner_only(self) -> None:
        config = self.home / ".config" / "opencode" / "opencode.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"mcp": {"mac-mcp": {"type": "remote", "url": "http://x/mcp?ApiKey=k"}}}))
        config.chmod(0o644)
        self.tool("opencode")
        client_setup.connect("opencode", run=self.fake_run)
        backup = config.with_name(config.name + ".before-mac-mcp")
        self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
