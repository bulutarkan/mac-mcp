from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerCompletionTests(unittest.TestCase):
    def run_bash(self, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        merged["MAC_MCP_INSTALLER_LIBRARY_ONLY"] = "1"
        for name in ("MAC_MCP_HOST", "MAC_MCP_PORT", "MCP_ALLOW_NO_AUTH", "MCP_API_KEY"):
            merged.pop(name, None)
        if env:
            merged.update(env)
        return subprocess.run(
            ["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'],
            text=True,
            capture_output=True,
            env=merged,
            check=False,
        )

    def fixture(self, root: Path, *, port: int = 8000, env_text: str) -> tuple[Path, Path]:
        state = root / "state"
        runtime = root / "runtime"
        state.mkdir()
        (runtime / "mcp_server").mkdir(parents=True)
        settings = state / "settings.json"
        settings.write_text(json.dumps({"server": {"port": port}}) + "\n", encoding="utf-8")
        env_file = runtime / "mcp_server/.env"
        env_file.write_text(env_text, encoding="utf-8")
        env_file.chmod(0o600)
        return state, runtime

    def completion_body(self, state: Path, runtime: Path) -> str:
        return (
            f'STATE_DIR="{state}"; RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; '
            f'SOURCE_DIR="{runtime}/source"; CLI_PATH="{runtime}/bin/mac-mcp"; '
            f'APP_PATH="{runtime}/Applications/Mac MCP.app"; BIN_DIR="{runtime}/bin"; '
            'VERIFIED_RELEASE_ID=stable-test; VERIFIED_RELEASE_VERSION=2.1.6; '
            'CHATGPT_PROVIDER_ENABLED=0; print_completion'
        )

    def test_completion_hides_secret_and_uses_default_endpoint(self) -> None:
        secret = "installer-secret-must-never-appear"
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-completion-") as td:
            state, runtime = self.fixture(
                Path(td),
                env_text=f"MCP_API_KEY={secret}\nMCP_ALLOW_NO_AUTH=false\n",
            )
            env_file = runtime / "mcp_server/.env"
            before = env_file.read_bytes()
            mode_before = stat.S_IMODE(env_file.stat().st_mode)

            proc = self.run_bash(self.completion_body(state, runtime))

            self.assertEqual(0, proc.returncode, proc.stderr)
            output = proc.stdout + proc.stderr
            self.assertIn("Local MCP endpoint: http://127.0.0.1:8000/mcp", output)
            self.assertIn("Server bind:        127.0.0.1:8000", output)
            self.assertIn("MCP authentication: Bearer token required", output)
            self.assertIn("API key: stored locally (present, not printed)", output)
            self.assertIn("Authorization: Bearer <API_KEY>", output)
            self.assertNotIn(secret, output)
            self.assertIn(
                "ChatGPT / header-limited client URL: http://127.0.0.1:8000/mcp?ApiKey=<API_KEY>",
                output,
            )
            self.assertNotIn("API key: " + secret, output)
            self.assertEqual(before, env_file.read_bytes())
            self.assertEqual(mode_before, stat.S_IMODE(env_file.stat().st_mode))

    def test_completion_uses_custom_port_from_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-port-") as td:
            state, runtime = self.fixture(
                Path(td),
                port=8765,
                env_text="MCP_API_KEY=test-secret\nMCP_ALLOW_NO_AUTH=false\n",
            )
            proc = self.run_bash(self.completion_body(state, runtime))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("Local MCP endpoint: http://127.0.0.1:8765/mcp", proc.stdout)
            self.assertIn("Server bind:        127.0.0.1:8765", proc.stdout)

    def test_completion_process_env_overrides_settings_and_reports_bind(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-env-port-") as td:
            state, runtime = self.fixture(
                Path(td),
                port=8765,
                env_text="MCP_API_KEY=test-secret\nMCP_ALLOW_NO_AUTH=false\n",
            )
            proc = self.run_bash(
                self.completion_body(state, runtime),
                {"MAC_MCP_HOST": "0.0.0.0", "MAC_MCP_PORT": "9123"},
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("Local MCP endpoint: http://127.0.0.1:9123/mcp", proc.stdout)
            self.assertIn("Server bind:        0.0.0.0:9123", proc.stdout)

    def test_completion_reads_port_from_runtime_env_before_settings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-env-file-port-") as td:
            state, runtime = self.fixture(
                Path(td),
                port=8765,
                env_text=(
                    "MAC_MCP_PORT=8456\n"
                    "MCP_API_KEY=test-secret\n"
                    "MCP_ALLOW_NO_AUTH=false\n"
                ),
            )
            proc = self.run_bash(self.completion_body(state, runtime))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("Local MCP endpoint: http://127.0.0.1:8456/mcp", proc.stdout)

    def test_completion_reports_explicit_no_auth_without_revealing_stored_key(self) -> None:
        secret = "stored-but-unused-secret"
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-no-auth-") as td:
            state, runtime = self.fixture(
                Path(td),
                port=8765,
                env_text=f"MCP_API_KEY={secret}\nMCP_ALLOW_NO_AUTH=true\n",
            )
            proc = self.run_bash(self.completion_body(state, runtime))
            self.assertEqual(0, proc.returncode, proc.stderr)
            output = proc.stdout + proc.stderr
            self.assertIn(
                "MCP authentication: disabled by explicit no-auth configuration",
                output,
            )
            self.assertIn(
                "API key: present in local storage but not used in no-auth mode (not printed)",
                output,
            )
            self.assertNotIn(secret, output)
            self.assertNotIn("ChatGPT / header-limited client URL:", output)

    def test_completion_prefers_public_connector_url_for_chatgpt_query_auth(self) -> None:
        secret = "public-connector-secret"
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-public-chatgpt-") as td:
            state, runtime = self.fixture(
                Path(td),
                port=8765,
                env_text=f"MCP_API_KEY={secret}\nMCP_ALLOW_NO_AUTH=false\n",
            )
            settings = state / "settings.json"
            settings.write_text(
                json.dumps({
                    "server": {
                        "port": 8765,
                        "public_endpoint_mode": "cloudflare",
                        "public_url": "https://mac.example.com/mcp",
                    }
                }) + "\n",
                encoding="utf-8",
            )
            proc = self.run_bash(self.completion_body(state, runtime))
            self.assertEqual(0, proc.returncode, proc.stderr)
            output = proc.stdout + proc.stderr
            self.assertIn(
                "ChatGPT / header-limited client URL: https://mac.example.com/mcp?ApiKey=<API_KEY>",
                output,
            )
            self.assertNotIn(secret, output)

    def test_configure_runtime_never_echoes_generated_api_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-generated-secret-") as td:
            runtime = Path(td) / "runtime"
            (runtime / "mcp_server").mkdir(parents=True)
            (runtime / "mcp_server/.env.example").write_text(
                "MCP_API_KEY=\nMCP_ALLOW_NO_AUTH=false\n",
                encoding="utf-8",
            )
            proc = self.run_bash(
                f'RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; configure_runtime'
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            env_file = runtime / "mcp_server/.env"
            content = env_file.read_text(encoding="utf-8")
            key = next(
                line.split("=", 1)[1]
                for line in content.splitlines()
                if line.startswith("MCP_API_KEY=")
            )
            self.assertGreaterEqual(len(key), 32)
            output = proc.stdout + proc.stderr
            self.assertNotIn(key, output)
            self.assertIn("stored it in mcp_server/.env", output)
            self.assertEqual(0o600, stat.S_IMODE(env_file.stat().st_mode))

    def test_installer_source_has_no_completion_secret_or_query_key_output(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("printf '  API key: %s", source)
        self.assertIn("?ApiKey=<API_KEY>", source)
        self.assertNotIn('API_KEY="$api_key"', source)
        self.assertIn("API key: stored locally (present, not printed)", source)
        self.assertIn("ChatGPT / header-limited client URL:", source)


if __name__ == "__main__":
    unittest.main()
