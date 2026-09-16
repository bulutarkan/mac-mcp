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


class InstallerPublicEndpointTests(unittest.TestCase):
    def run_bash(self, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        merged["MAC_MCP_INSTALLER_LIBRARY_ONLY"] = "1"
        if env:
            merged.update(env)
        return subprocess.run(
            ["/bin/bash", "-c", f'source "{INSTALLER}"; {body}'],
            text=True,
            capture_output=True,
            env=merged,
            check=False,
        )

    def test_public_mode_overrides_map_to_canonical_modes(self) -> None:
        expected = {
            "local": "none",
            "cloudflare": "cloudflare",
            "ngrok": "ngrok",
            "custom": "custom",
        }
        for requested, canonical in expected.items():
            with self.subTest(requested=requested):
                proc = self.run_bash(
                    'TTY_AVAILABLE=0; choose_public_endpoint_mode >/dev/null; printf "%s" "$PUBLIC_ENDPOINT_MODE"',
                    {"MAC_MCP_INSTALL_PUBLIC_MODE": requested},
                )
                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertEqual(canonical, proc.stdout)

    def test_existing_cloudflared_is_accepted_without_package_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-bin-") as td:
            fake = Path(td) / "cloudflared"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            proc = self.run_bash(
                'PUBLIC_ENDPOINT_MODE=cloudflare; install_selected_public_provider >/dev/null; '
                'printf "%s|%s" "$PUBLIC_PROVIDER_AVAILABLE" "$PUBLIC_PROVIDER_BIN"',
                {"PATH": td + ":/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            available, binary = proc.stdout.split("|", 1)
            self.assertEqual("1", available)
            self.assertEqual(str(fake), binary)

    def test_persist_cloudflare_config_normalizes_url_and_keeps_owner_only_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-state-") as td:
            state = Path(td) / "state"
            runtime = Path(td) / "runtime"
            state.mkdir()
            (runtime / "mcp_server").mkdir(parents=True)
            env_file = runtime / "mcp_server/.env"
            env_file.write_text("NGROK_DOMAIN=old.example\n", encoding="utf-8")
            proc = self.run_bash(
                f'STATE_DIR="{state}"; RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; '
                'persist_public_endpoint_config cloudflare https://mac.example.com ""',
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            data = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("cloudflare", data["server"]["public_endpoint_mode"])
            self.assertEqual("https://mac.example.com/mcp", data["server"]["public_url"])
            self.assertEqual("NGROK_DOMAIN=\n", env_file.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE((state / "settings.json").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(env_file.stat().st_mode))

    def test_insecure_public_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-invalid-") as td:
            state = Path(td) / "state"
            runtime = Path(td) / "runtime"
            state.mkdir()
            (runtime / "mcp_server").mkdir(parents=True)
            (runtime / "mcp_server/.env").write_text("NGROK_DOMAIN=\n", encoding="utf-8")
            proc = self.run_bash(
                f'STATE_DIR="{state}"; RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; '
                'persist_public_endpoint_config custom http://insecure.example.com ""',
            )
            self.assertNotEqual(0, proc.returncode)

    def test_cloudflare_token_is_delivered_only_over_stdin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-token-") as td:
            root = Path(td)
            state = root / "state"
            runtime = root / "runtime"
            state.mkdir()
            (runtime / "mcp_server").mkdir(parents=True)
            (runtime / "mcp_server/.env").write_text("NGROK_DOMAIN=\n", encoding="utf-8")
            capture = root / "capture"
            fake_cli = root / "mac-mcp"
            fake_cli.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$CAPTURE.args\"\ncat > \"$CAPTURE.stdin\"\n",
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            secret = "test-cloudflare-secret-value"
            body = (
                f'STATE_DIR="{state}"; RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; '
                f'CLI_PATH="{fake_cli}"; PUBLIC_ENDPOINT_MODE=cloudflare; PUBLIC_PROVIDER_AVAILABLE=1; '
                'TTY_AVAILABLE=0; '
                f'CAPTURE="{capture}"; export CAPTURE; '
                f'read_secret() {{ SECRET_REPLY="{secret}"; }}; '
                'configure_public_endpoint >/dev/null'
            )
            proc = self.run_bash(
                body,
                {
                    "MAC_MCP_INSTALL_PUBLIC_URL": "https://mac.example.com",
                    "MAC_MCP_INSTALL_CLOUDFLARE_TOKEN_NOW": "yes",
                },
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            args = (root / "capture.args").read_text(encoding="utf-8")
            stdin = (root / "capture.stdin").read_text(encoding="utf-8")
            self.assertEqual("credential cloudflare save\n", args)
            self.assertEqual(secret + "\n", stdin)
            self.assertNotIn(secret, (state / "settings.json").read_text(encoding="utf-8"))
            self.assertNotIn(secret, (runtime / "mcp_server/.env").read_text(encoding="utf-8"))
            settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("cloudflare", settings["server"]["public_endpoint_mode"])

    def test_skipping_cloudflare_token_falls_back_to_local_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-defer-") as td:
            root = Path(td)
            state = root / "state"
            runtime = root / "runtime"
            state.mkdir()
            (runtime / "mcp_server").mkdir(parents=True)
            (runtime / "mcp_server/.env").write_text("NGROK_DOMAIN=\n", encoding="utf-8")
            proc = self.run_bash(
                f'STATE_DIR="{state}"; RUNTIME_DIR="{runtime}"; PYTHON_BIN=/usr/bin/python3; '
                'PUBLIC_ENDPOINT_MODE=cloudflare; PUBLIC_PROVIDER_AVAILABLE=1; TTY_AVAILABLE=0; '
                'configure_public_endpoint >/dev/null',
                {
                    "MAC_MCP_INSTALL_PUBLIC_URL": "https://mac.example.com",
                    "MAC_MCP_INSTALL_CLOUDFLARE_TOKEN_NOW": "no",
                },
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("none", settings["server"]["public_endpoint_mode"])
            self.assertEqual("", settings["server"]["public_url"])

    def test_cloudflare_installer_contract_is_secret_safe_and_defer_safe(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('Install $package with Homebrew?', source)
        self.assertIn('MAC_MCP_INSTALL_CLOUDFLARED', source)
        self.assertIn('read -r -s SECRET_REPLY', source)
        self.assertIn('credential cloudflare save', source)
        self.assertIn("printf '%s\\n' \"$SECRET_REPLY\" | \"$CLI_PATH\" credential cloudflare save", source)
        self.assertIn('Published application', source)
        self.assertIn('http://localhost:$port', source)
        self.assertIn('Local only will remain active', source)
        self.assertNotIn('MAC_MCP_CLOUDFLARE_TOKEN=', source)
        self.assertNotIn('CLOUDFLARE_TUNNEL_TOKEN=', source)


if __name__ == "__main__":
    unittest.main()
