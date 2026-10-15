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
                                      "KIMI_CODE_HOME": "", "GROK_HOME": "", "PI_CODING_AGENT_DIR": "", "CLINE_DIR": ""})
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

    def test_antigravity_and_pi_are_added_through_their_cli(self) -> None:
        for app, binary, config in (("antigravity", "agy", self.home / ".gemini" / "config" / "mcp_config.json"),
                                    ("pi", "pi", self.home / ".pi" / "agent" / "mcp.json")):
            with self.subTest(app=app):
                tool = self.tool(binary)
                self.calls = []

                def run(args, config=config, **kwargs):
                    self.calls.append(list(args))
                    if "add" in args:
                        config.parent.mkdir(parents=True, exist_ok=True)
                        config.write_text(json.dumps({"mcpServers": {"mac-mcp": {"command": str(self.cli), "args": ["stdio"]}}}))
                    return subprocess.CompletedProcess(args, 0, "", "")

                client_setup.connect(app, run=run)
                self.assertEqual([str(tool), "mcp", "add", "mac-mcp", "--", str(self.cli), "stdio"], self.calls[-1])
                status = {item["id"]: item for item in client_setup.apps_status()}[app]
                self.assertEqual((True, True, True), (status["installed"], status["added"], status["keyless"]))

    def test_cline_kilo_and_goose_keep_the_owners_own_servers(self) -> None:
        cline = self.home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
        kilo = self.home / ".config" / "kilo" / "kilo.json"
        goose = self.home / ".config" / "goose" / "config.yaml"
        for path in (cline, kilo, goose):
            path.parent.mkdir(parents=True)
        cline.write_text(json.dumps({"mcpServers": {"github": {"url": "https://example.invalid"}}}))
        kilo.write_text(json.dumps({"model": "openrouter/x", "mcp": {"github": {"type": "remote"}}}))
        goose.write_text("GOOSE_PROVIDER: openrouter\nextensions:\n  developer:\n    enabled: true\n    type: platform\n")
        for app in ("cline", "kilo", "goose"):
            self.tool(app)
            result = client_setup.connect(app, run=self.fake_run)
            self.assertIn("ask it something about your Mac", result["next"])
        self.assertEqual([], self.calls)
        servers = json.loads(cline.read_text())["mcpServers"]
        self.assertEqual({"url": "https://example.invalid"}, servers["github"])
        self.assertEqual({"command": str(self.cli), "args": ["stdio"]}, servers["mac-mcp"])
        data = json.loads(kilo.read_text())
        self.assertEqual(("openrouter/x", {"type": "remote"}), (data["model"], data["mcp"]["github"]))
        self.assertEqual({"type": "local", "command": [str(self.cli), "stdio"], "enabled": True}, data["mcp"]["mac-mcp"])
        import yaml
        config = yaml.safe_load(goose.read_text())
        self.assertEqual("openrouter", config["GOOSE_PROVIDER"])
        self.assertEqual("platform", config["extensions"]["developer"]["type"])
        self.assertEqual((str(self.cli), ["stdio"], "stdio"),
                         tuple(config["extensions"]["mac-mcp"][key] for key in ("cmd", "args", "type")))
        self.assertTrue(goose.with_name("config.yaml.before-mac-mcp").exists())
        apps = {item["id"]: item for item in client_setup.apps_status()}
        for app in ("cline", "kilo", "goose"):
            self.assertEqual((True, True, True), (apps[app]["installed"], apps[app]["added"], apps[app]["keyless"]), app)

    def test_an_unreadable_goose_config_is_left_alone(self) -> None:
        self.tool("goose")
        goose = self.home / ".config" / "goose" / "config.yaml"
        goose.parent.mkdir(parents=True)
        goose.write_text("extensions: [unclosed\n")
        with self.assertRaisesRegex(client_setup.SetupError, "could not be read"):
            client_setup.connect("goose", run=self.fake_run)
        self.assertEqual("extensions: [unclosed\n", goose.read_text())

    def test_the_new_apps_not_installed_are_reported(self) -> None:
        for app in ("antigravity", "pi", "cline", "kilo", "goose"):
            with self.assertRaises(client_setup.SetupError):
                client_setup.connect(app, run=self.fake_run)
        apps = {item["id"]: item for item in client_setup.apps_status()}
        self.assertFalse(any(apps[app]["installed"] for app in ("antigravity", "pi", "cline", "kilo", "goose")))

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
