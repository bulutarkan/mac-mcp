from __future__ import annotations

import argparse
import json
import tomllib
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import cli
from mcp_server.connection_config import (
    ConnectionConfigError,
    render_connection_config,
)


ROOT = Path(__file__).resolve().parents[1]


class ConnectionConfigTemplateTests(unittest.TestCase):
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
        for client in ("chatgpt", "codex", "opencode"):
            self.assertIn(f"mac-mcp connect-config --client {client}", readme)
        self.assertIn("never prints the configured API-key value", readme)
        self.assertIn("--endpoint auto", readme)


class ConnectionConfigCLITests(unittest.TestCase):
    def _run(self, argv: list[str], *, settings, public=None) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        patches = [
            patch("mcp_server.cli.load_settings", return_value=settings),
            patch("mcp_server.cli._default_port", return_value=8765),
        ]
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
