from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, the declared floor
    tomllib = None
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import cli
from mcp_server.connection_config import (
    ConnectionConfigError,
    SUPPORTED_CLIENTS,
    UNGUIDED_CLIENTS,
    render_connection_config,
)


ROOT = Path(__file__).resolve().parents[1]


class ConnectionConfigTemplateTests(unittest.TestCase):
    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_codex_toml_is_parseable_and_uses_env_token(self) -> None:
        rendered = render_connection_config(
            client="codex",
            endpoint_url="http://127.0.0.1:8765/mcp",
            auth_required=True,
            server_name="mac-mcp",
            auth_env="MAC_MCP_API_KEY",
        )
        parsed = tomllib.loads(rendered.snippet)
        row = parsed["mcp_servers"]["mac-mcp"]
        self.assertEqual("http://127.0.0.1:8765/mcp", row["url"])
        self.assertEqual("MAC_MCP_API_KEY", row["bearer_token_env_var"])
        self.assertNotIn("secret", rendered.snippet.lower())

    def test_opencode_json_is_parseable_and_uses_env_header(self) -> None:
        rendered = render_connection_config(
            client="opencode",
            endpoint_url="https://mac.example.com/mcp",
            auth_required=True,
        )
        parsed = json.loads(rendered.snippet)
        server = parsed["mcp"]["mac-mcp"]
        self.assertEqual("remote", server["type"])
        self.assertEqual("https://mac.example.com/mcp", server["url"])
        self.assertTrue(server["enabled"])
        self.assertFalse(server["oauth"])
        self.assertEqual(
            "Bearer {env:MAC_MCP_API_KEY}",
            server["headers"]["Authorization"],
        )

    def test_no_auth_omits_all_credential_placeholders(self) -> None:
        codex = render_connection_config(
            client="codex",
            endpoint_url="http://127.0.0.1:8765/mcp",
            auth_required=False,
        )
        opencode = render_connection_config(
            client="opencode",
            endpoint_url="http://127.0.0.1:8765/mcp",
            auth_required=False,
        )
        chatgpt = render_connection_config(
            client="chatgpt",
            endpoint_url="https://mac.example.com/mcp",
            auth_required=False,
        )
        self.assertNotIn("bearer_token_env_var", codex.snippet)
        self.assertNotIn("headers", json.loads(opencode.snippet)["mcp"]["mac-mcp"])
        self.assertEqual("https://mac.example.com/mcp", chatgpt.snippet)
        self.assertIsNone(codex.secret_instruction)
        self.assertIsNone(opencode.secret_instruction)
        self.assertIsNone(chatgpt.secret_instruction)

    def test_chatgpt_uses_safe_query_placeholder_and_rejects_local_url(self) -> None:
        rendered = render_connection_config(
            client="chatgpt",
            endpoint_url="https://mac.example.com/mcp",
            auth_required=True,
        )
        self.assertEqual(
            "https://mac.example.com/mcp?ApiKey=<API_KEY>",
            rendered.snippet,
        )
        with self.assertRaises(ConnectionConfigError):
            render_connection_config(
                client="chatgpt",
                endpoint_url="http://127.0.0.1:8765/mcp",
                auth_required=True,
            )

    def test_name_env_and_endpoint_validation_block_injection(self) -> None:
        for name in ("bad name", "x]\nurl=\"https://evil\"", "../bad"):
            with self.assertRaises(ConnectionConfigError):
                render_connection_config(
                    client="codex",
                    endpoint_url="http://127.0.0.1:8765/mcp",
                    auth_required=True,
                    server_name=name,
                )
        with self.assertRaises(ConnectionConfigError):
            render_connection_config(
                client="codex",
                endpoint_url="http://127.0.0.1:8765/mcp",
                auth_required=True,
                auth_env="BAD;ENV",
            )
        with self.assertRaises(ConnectionConfigError):
            render_connection_config(
                client="codex",
                endpoint_url="https://mac.example.com/mcp?ApiKey=x",
                auth_required=True,
            )


    def test_readme_uses_generator_as_source_of_truth(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for client in SUPPORTED_CLIENTS:
            self.assertIn(f"mac-mcp connect-config --client {client}", readme)
        self.assertIn("Claude Desktop", readme)
        self.assertIn("never prints the configured API-key value", readme)
        self.assertIn("--endpoint auto", readme)


class ClaudeCodeConfigTests(unittest.TestCase):
    """#55: Claude Code gets a named, tested setup path; unverified Claude clients get guidance only."""

    def render(self, auth_required: bool = True, endpoint: str = "http://127.0.0.1:8765/mcp"):
        return render_connection_config(client="claude-code", endpoint_url=endpoint, auth_required=auth_required)

    def payload(self, snippet: str) -> dict:
        argv = shlex.split(snippet)
        self.assertEqual(["claude", "mcp", "add-json", "--scope", "user", "mac-mcp"], argv[:6])
        self.assertEqual(7, len(argv))
        return json.loads(argv[6])

    def test_command_keeps_the_key_as_an_unexpanded_env_reference(self) -> None:
        rendered = self.render()
        server = self.payload(rendered.snippet)
        self.assertEqual({"type": "http", "url": "http://127.0.0.1:8765/mcp",
                          "headers": {"Authorization": "Bearer ${MAC_MCP_API_KEY}"}}, server)
        # Single quotes: a shell must not expand ${MAC_MCP_API_KEY} into the stored config.
        self.assertIn("'{", rendered.snippet)
        self.assertIn("MAC_MCP_API_KEY", rendered.secret_instruction)
        self.assertIn("claude mcp get mac-mcp", rendered.verify_instruction)

    def test_no_auth_and_custom_names(self) -> None:
        server = self.payload(self.render(auth_required=False).snippet)
        self.assertNotIn("headers", server)
        rendered = render_connection_config(client="claude-code", endpoint_url="https://mac.example.com/mcp",
                                            auth_required=True, server_name="mac_2", auth_env="MY_KEY")
        argv = shlex.split(rendered.snippet)
        self.assertEqual("mac_2", argv[5])
        self.assertEqual("Bearer ${MY_KEY}", json.loads(argv[6])["headers"]["Authorization"])

    @unittest.skipUnless(shutil.which("sh"), "sh is required")
    def test_a_shell_passes_the_placeholder_through_unchanged(self) -> None:
        rendered = self.render()
        argv_json = subprocess.run(
            ["sh", "-c", rendered.snippet.replace("claude mcp add-json --scope user mac-mcp", "printf %s"),],
            capture_output=True, text=True, env={"MAC_MCP_API_KEY": "real-secret-value"}, check=True,
        ).stdout
        self.assertNotIn("real-secret-value", argv_json)
        self.assertEqual("Bearer ${MAC_MCP_API_KEY}", json.loads(argv_json)["headers"]["Authorization"])

    def test_claude_desktop_is_not_given_an_unverified_snippet(self) -> None:
        with self.assertRaises(ConnectionConfigError) as ctx:
            render_connection_config(client="claude-desktop", endpoint_url="http://127.0.0.1:8765/mcp",
                                     auth_required=True)
        self.assertIn("not guided yet", str(ctx.exception))
        self.assertIn("--client claude-code", str(ctx.exception))
        self.assertNotIn("claude-desktop", SUPPORTED_CLIENTS)

    @unittest.skipUnless(shutil.which("claude"), "Claude Code CLI not installed")
    def test_installed_claude_code_accepts_the_generated_command(self) -> None:
        rendered = self.render(endpoint="http://127.0.0.1:9/mcp")
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin:/opt/homebrew/bin:" + str(Path(shutil.which("claude")).parent)}
            added = subprocess.run(["sh", "-c", rendered.snippet], cwd=home, env=env,
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(0, added.returncode, added.stderr)
            stored = json.loads((Path(home) / ".claude.json").read_text(encoding="utf-8"))
        server = stored["mcpServers"]["mac-mcp"]
        self.assertEqual("Bearer ${MAC_MCP_API_KEY}", server["headers"]["Authorization"])
        self.assertEqual("http", server["type"])


class SettingsClientSetupTests(unittest.TestCase):
    def test_connections_pane_offers_every_client_through_the_cli(self) -> None:
        view = (ROOT / "menu_app/Sources/SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn('case .clients: return "AI Clients"', view)
        self.assertIn('state.runCLICommand(["connect-config", "--client", requested])', view)
        for client in (*SUPPORTED_CLIENTS, *UNGUIDED_CLIENTS):
            self.assertIn(f'("{client}", ', view)


class ConnectionConfigCLITests(unittest.TestCase):
    public_health = "ok"

    def _run(self, argv: list[str], *, settings, public=None) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        patches = [
            patch("mcp_server.cli.load_settings", return_value=settings),
            patch("mcp_server.cli._default_port", return_value=8765),
        ]
        probe = patch("mcp_server.cli._probe_public_route", return_value=self.public_health)
        probe.start()
        self.addCleanup(probe.stop)
        if public is not None:
            patches.append(
                patch(
                    "mcp_server.cli.resolve_public_endpoint",
                    return_value=SimpleNamespace(endpoint_url=public),
                )
            )
        with patches[0], patches[1]:
            if len(patches) == 3:
                with patches[2], redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(argv)
            else:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_codex_auto_prefers_local_and_never_prints_configured_secret(self) -> None:
        secret = "configured-value-that-must-never-print"
        settings = SimpleNamespace(allow_no_auth=False, api_key=secret)
        with patch("mcp_server.cli.resolve_public_endpoint") as public:
            code, output, error = self._run(
                ["connect-config", "--client", "codex"],
                settings=settings,
            )
        self.assertEqual(0, code, error)
        public.assert_not_called()
        self.assertIn("Endpoint: local (http://127.0.0.1:8765/mcp)", output)
        self.assertIn("[mcp_servers.mac-mcp]", output)
        self.assertIn('bearer_token_env_var = "MAC_MCP_API_KEY"', output)
        self.assertNotIn(secret, output + error)

    def test_codex_public_endpoint_is_selectable(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="hidden-value")
        code, output, error = self._run(
            ["connect-config", "--client", "codex", "--endpoint", "public"],
            settings=settings,
            public="https://mac.example.com/mcp",
        )
        self.assertEqual(0, code, error)
        self.assertIn('url = "https://mac.example.com/mcp"', output)
        self.assertIn("Endpoint: public", output)
        self.assertNotIn("hidden-value", output + error)

    def test_opencode_no_auth_output_has_no_header_or_secret_instruction(self) -> None:
        settings = SimpleNamespace(allow_no_auth=True, api_key="")
        code, output, error = self._run(
            ["connect-config", "--client", "opencode"],
            settings=settings,
        )
        self.assertEqual(0, code, error)
        self.assertIn('"type": "remote"', output)
        self.assertNotIn('"headers"', output)
        self.assertNotIn("Secret setup:", output)

    def test_chatgpt_auto_uses_public_placeholder_without_real_secret(self) -> None:
        secret = "chatgpt-real-secret-never-print"
        settings = SimpleNamespace(allow_no_auth=False, api_key=secret)
        code, output, error = self._run(
            ["connect-config", "--client", "chatgpt"],
            settings=settings,
            public="https://mac.example.com/mcp",
        )
        self.assertEqual(0, code, error)
        self.assertIn("https://mac.example.com/mcp?ApiKey=<API_KEY>", output)
        self.assertIn("Target: ChatGPT remote MCP app/connector URL field", output)
        self.assertNotIn(secret, output + error)

    def test_chatgpt_without_public_endpoint_fails_closed(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="hidden")
        code, output, error = self._run(
            ["connect-config", "--client", "chatgpt"],
            settings=settings,
            public="",
        )
        self.assertEqual("", output)
        self.assertEqual(1, code)
        self.assertIn("needs a public HTTPS address", error)
        # Other clients asking for --endpoint public keep the generic message.
        code, output, error = self._run(
            ["connect-config", "--client", "codex", "--endpoint", "public"], settings=settings, public="",
        )
        self.assertEqual(1, code)
        self.assertIn("no public MCP endpoint is configured", error)

    def test_required_auth_without_server_key_fails_closed(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="")
        code, output, error = self._run(
            ["connect-config", "--client", "codex"],
            settings=settings,
        )
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("MCP_API_KEY is not configured", error)

    def test_claude_code_auto_uses_local_and_prints_a_verify_step(self) -> None:
        secret = "configured-value-that-must-never-print"
        settings = SimpleNamespace(allow_no_auth=False, api_key=secret)
        with patch("mcp_server.cli.resolve_public_endpoint") as public:
            code, output, error = self._run(["connect-config", "--client", "claude-code"], settings=settings)
        self.assertEqual(0, code, error)
        public.assert_not_called()
        self.assertIn("claude mcp add-json --scope user mac-mcp", output)
        self.assertIn("Verify:\nRun: claude mcp get mac-mcp", output)
        self.assertNotIn(secret, output + error)

    def test_claude_desktop_gets_guidance_even_without_a_server_key(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="")
        code, output, error = self._run(["connect-config", "--client", "claude-desktop"], settings=settings)
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn(UNGUIDED_CLIENTS["claude-desktop"], error)

    def test_chatgpt_on_local_only_gets_next_steps_instead_of_a_bare_error(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="hidden")
        code, output, error = self._run(["connect-config", "--client", "chatgpt"], settings=settings, public="")
        self.assertEqual(1, code)
        self.assertEqual("", output)
        for expected in ("needs a public HTTPS address", "Settings > Connections > Public Endpoint",
                         "mac-mcp status", "--client claude-code"):
            self.assertIn(expected, error)

    def test_public_endpoint_reports_its_health(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="hidden")
        code, output, _ = self._run(["connect-config", "--client", "chatgpt"], settings=settings,
                                    public="https://mac.example.com/mcp")
        self.assertEqual(0, code)
        self.assertIn("Public endpoint check: reachable", output)
        self.public_health = "unreachable"
        code, output, _ = self._run(["connect-config", "--client", "chatgpt"], settings=settings,
                                    public="https://mac.example.com/mcp")
        self.assertEqual(0, code)
        self.assertIn("Public endpoint check: NOT reachable", output)
        local = self._run(["connect-config", "--client", "codex"], settings=settings)[1]
        self.assertNotIn("Public endpoint check", local)

    def test_invalid_cli_name_and_env_fail_without_shell_escaping_risk(self) -> None:
        settings = SimpleNamespace(allow_no_auth=False, api_key="hidden")
        for extra in (
            ["--name", "bad name"],
            ["--auth-env", "BAD;ENV"],
        ):
            code, output, error = self._run(
                ["connect-config", "--client", "codex", *extra],
                settings=settings,
            )
            self.assertEqual(1, code)
            self.assertEqual("", output)
            self.assertTrue(error.startswith("mac-mcp connect-config:"))


if __name__ == "__main__":
    unittest.main()
