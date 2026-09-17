from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mcp_server import policy
from mcp_server import cli
from mcp_server.security import load_settings, validate_bootstrap_security


_SECURITY_ENV = {
    "MCP_API_KEY",
    "MCP_ALLOW_NO_AUTH",
    "MCP_ALLOW_SHELL",
    "HTTP_ALLOWLIST",
    "HTTP_PRIVATE_ALLOWLIST",
    "BROWSER_ALLOWLIST",
    "BROWSER_PRIVATE_ALLOWLIST",
    "MAC_MCP_PERMISSION_PROFILE",
    "MAC_MCP_HOST",
    "MAC_MCP_PUBLIC_ENDPOINT_MODE",
    "MAC_MCP_SETTINGS_PATH",
}


def clean_env(**values: str):
    env = {key: value for key, value in __import__("os").environ.items() if key not in _SECURITY_ENV}
    env.update(values)
    return patch.dict("os.environ", env, clear=True)


class SecureBootstrapTests(unittest.TestCase):
    def test_missing_security_config_defaults_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json")):
            settings = load_settings()
            self.assertFalse(settings.allow_no_auth)
            self.assertFalse(settings.allow_shell)
            self.assertEqual([], settings.http_allowlist)
            self.assertEqual([], settings.browser_allowlist)
            self.assertEqual("standard", policy.permission_profile_name())

    def test_auth_enabled_without_key_refuses_startup(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json")):
            settings = load_settings()
            with self.assertRaisesRegex(RuntimeError, "secure_bootstrap_missing_api_key"):
                validate_bootstrap_security(settings)

    def test_explicit_authenticated_configuration_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_API_KEY="test-key-value",
            MCP_ALLOW_NO_AUTH="false",
            MCP_ALLOW_SHELL="true",
            HTTP_ALLOWLIST="*",
            BROWSER_ALLOWLIST="*",
            MAC_MCP_PERMISSION_PROFILE="trusted",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ):
            settings = load_settings()
            validate_bootstrap_security(settings, host="0.0.0.0")
            self.assertTrue(settings.allow_shell)
            self.assertEqual(["*"], settings.http_allowlist)
            self.assertEqual(["*"], settings.browser_allowlist)
            self.assertEqual("trusted", policy.permission_profile_name())

    def test_no_auth_non_loopback_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_ALLOW_NO_AUTH="true",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ):
            settings = replace(load_settings(), api_key="")
            with self.assertRaisesRegex(RuntimeError, "secure_bootstrap_no_auth_non_loopback"):
                validate_bootstrap_security(settings, host="0.0.0.0")

    def test_no_auth_public_endpoint_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_ALLOW_NO_AUTH="true",
            MAC_MCP_PUBLIC_ENDPOINT_MODE="cloudflare",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ):
            settings = replace(load_settings(), api_key="")
            with self.assertRaisesRegex(RuntimeError, "secure_bootstrap_no_auth_public_endpoint"):
                validate_bootstrap_security(settings, host="127.0.0.1")


    def test_direct_uvicorn_non_loopback_host_is_detected_from_argv(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_ALLOW_NO_AUTH="true",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ), patch("sys.argv", ["uvicorn", "mcp_server.main:app", "--host", "0.0.0.0", "--port", "8877"]):
            settings = replace(load_settings(), api_key="")
            with self.assertRaisesRegex(RuntimeError, "secure_bootstrap_no_auth_non_loopback"):
                validate_bootstrap_security(settings)

    def test_cli_public_mode_override_is_checked_before_server_start(self) -> None:
        args = SimpleNamespace(
            host="127.0.0.1", port=8877, reload=False, public_mode="custom",
            public_url="https://example.com/mcp", cloudflare_tunnel=None,
            cloudflare_token_file=None, cloudflared_bin=None, ngrok=False,
            ngrok_domain=None, ngrok_bin=None,
        )
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_ALLOW_NO_AUTH="true",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ), patch.object(cli, "_start_server") as start_server:
            self.assertEqual(2, cli.start(args))
            start_server.assert_not_called()

    def test_env_example_does_not_ship_a_placeholder_api_key(self) -> None:
        env_example = Path(__file__).resolve().parents[1] / "mcp_server" / ".env.example"
        lines = env_example.read_text(encoding="utf-8").splitlines()
        key_line = next(line for line in lines if line.startswith("MCP_API_KEY="))
        self.assertEqual("MCP_API_KEY=", key_line)

    def test_security_regression_workflow_declares_explicit_ci_bootstrap(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "security-regression.yml").read_text(encoding="utf-8")
        self.assertIn('MCP_API_KEY: "ci-regression-only-', workflow)
        self.assertIn('MCP_ALLOW_NO_AUTH: "false"', workflow)
        self.assertIn('MCP_ALLOW_SHELL: "true"', workflow)
        self.assertNotIn('HTTP_ALLOWLIST: "*"', workflow)
        self.assertNotIn('BROWSER_ALLOWLIST: "*"', workflow)

    def test_explicit_no_auth_loopback_warns_but_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td, clean_env(
            MCP_ALLOW_NO_AUTH="true",
            MAC_MCP_SETTINGS_PATH=str(Path(td) / "missing.json"),
        ):
            settings = replace(load_settings(), api_key="")
            with self.assertLogs("mac_mcp.security", level="WARNING") as logs:
                validate_bootstrap_security(settings, host="127.0.0.1")
            self.assertTrue(any("secure_bootstrap_no_auth_loopback" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
