from __future__ import annotations

import argparse
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, diagnostics
_ORIGINAL_CLI_PATHS = {
    "STATE_DIR": cli.STATE_DIR,
    "PID_FILE": cli.PID_FILE,
    "NGROK_PID_FILE": cli.NGROK_PID_FILE,
    "CLOUDFLARE_PID_FILE": cli.CLOUDFLARE_PID_FILE,
    "LOG_FILE": cli.LOG_FILE,
    "NGROK_LOG_FILE": cli.NGROK_LOG_FILE,
    "CLOUDFLARE_LOG_FILE": cli.CLOUDFLARE_LOG_FILE,
}
_ORIGINAL_CLOUDFLARE_LAUNCHD_LABEL = cli.CLOUDFLARE_LAUNCHD_LABEL
_ORIGINAL_CLOUDFLARE_LAUNCHD_PLIST_ENV = os.environ.get("MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST")
_TEST_CLI_STATE = None


def setUpModule() -> None:
    global _TEST_CLI_STATE
    _TEST_CLI_STATE = tempfile.TemporaryDirectory(prefix="mac-mcp-public-module-")
    state = Path(_TEST_CLI_STATE.name)
    cli.STATE_DIR = state
    cli.PID_FILE = state / "mac-mcp.pid"
    cli.NGROK_PID_FILE = state / "ngrok.pid"
    cli.CLOUDFLARE_PID_FILE = state / "cloudflared.pid"
    cli.LOG_FILE = state / "mac-mcp.log"
    cli.NGROK_LOG_FILE = state / "ngrok.log"
    cli.CLOUDFLARE_LOG_FILE = state / "cloudflared.log"
    cli.CLOUDFLARE_LAUNCHD_LABEL = f"mac-mcp-cloudflared-test-{os.getpid()}"
    os.environ["MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST"] = str(state / "cloudflared-launchd.plist")


def tearDownModule() -> None:
    global _TEST_CLI_STATE
    for name, value in _ORIGINAL_CLI_PATHS.items():
        setattr(cli, name, value)
    cli.CLOUDFLARE_LAUNCHD_LABEL = _ORIGINAL_CLOUDFLARE_LAUNCHD_LABEL
    if _ORIGINAL_CLOUDFLARE_LAUNCHD_PLIST_ENV is None:
        os.environ.pop("MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST", None)
    else:
        os.environ["MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST"] = _ORIGINAL_CLOUDFLARE_LAUNCHD_PLIST_ENV
    if _TEST_CLI_STATE is not None:
        _TEST_CLI_STATE.cleanup()
        _TEST_CLI_STATE = None


from mcp_server.public_endpoint import (
    PublicEndpointError,
    cloudflare_token_path,
    inspect_cloudflare_credential,
    normalize_custom_public_url,
    public_health_url,
    resolve_public_endpoint,
    write_cloudflare_token,
)


class PublicEndpointConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-public-endpoint-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
                "NGROK_DOMAIN": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def write_settings(self, server: dict) -> None:
        self.settings.write_text(json.dumps({"server": server}), encoding="utf-8")

    def test_default_is_local_only(self) -> None:
        config = resolve_public_endpoint()
        self.assertEqual("none", config.mode)
        self.assertIsNone(config.endpoint_url)

    def test_legacy_ngrok_on_start_migrates_behavior(self) -> None:
        self.write_settings({"ngrok_on_start": True})
        with patch.dict(os.environ, {"NGROK_DOMAIN": "example.ngrok-free.dev"}, clear=False):
            config = resolve_public_endpoint()
        self.assertEqual("ngrok", config.mode)
        self.assertEqual("https://example.ngrok-free.dev/mcp", config.endpoint_url)
        self.assertEqual("settings_legacy", config.source)

    def test_canonical_custom_mode_uses_settings_url_and_adds_mcp_path(self) -> None:
        self.write_settings({
            "ngrok_on_start": False,
            "public_endpoint_mode": "custom",
            "public_url": "https://mac.example.com/",
        })
        config = resolve_public_endpoint()
        self.assertEqual("custom", config.mode)
        self.assertEqual("https://mac.example.com/mcp", config.endpoint_url)
        self.assertEqual("https://mac.example.com/health", public_health_url(config))

    def test_explicit_url_implies_custom_mode(self) -> None:
        config = resolve_public_endpoint(public_url_override="https://edge.example.com/mcp")
        self.assertEqual("custom", config.mode)
        self.assertEqual("https://edge.example.com/mcp", config.endpoint_url)

    def test_env_mode_overrides_settings(self) -> None:
        self.write_settings({"public_endpoint_mode": "none", "public_url": ""})
        with patch.dict(
            os.environ,
            {
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "custom",
                "MAC_MCP_PUBLIC_URL": "https://env.example.com/mcp",
            },
            clear=False,
        ):
            config = resolve_public_endpoint()
        self.assertEqual("custom", config.mode)
        self.assertEqual("https://env.example.com/mcp", config.endpoint_url)
        self.assertEqual("env", config.source)

    def test_custom_url_rejects_insecure_or_secret_bearing_forms(self) -> None:
        bad = [
            "http://example.com/mcp",
            "https://user:pass@example.com/mcp",
            "https://example.com/mcp?ApiKey=secret",
            "https://example.com/mcp#token",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(PublicEndpointError):
                normalize_custom_public_url(value)

    def test_ngrok_alias_conflicts_with_custom_mode(self) -> None:
        with self.assertRaises(PublicEndpointError):
            resolve_public_endpoint(mode_override="custom", force_ngrok=True, public_url_override="https://x.example/mcp")

    def test_cli_default_port_reads_runtime_settings(self) -> None:
        self.write_settings({"port": 8765, "public_endpoint_mode": "none"})
        with patch.dict(os.environ, {"MAC_MCP_PORT": ""}, clear=False):
            self.assertEqual(8765, cli._default_port())


class PublicEndpointCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-public-cli-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({
            "server": {
                "port": 8765,
                "ngrok_on_start": False,
                "public_endpoint_mode": "custom",
                "public_url": "https://mac.example.com/mcp",
            }
        }), encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
                "NGROK_DOMAIN": "example.ngrok-free.dev",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def args(self, **overrides):
        data = {
            "public_mode": None,
            "public_url": None,
            "ngrok": False,
            "ngrok_domain": None,
            "ngrok_bin": None,
            "host": "127.0.0.1",
            "port": 8765,
            "reload": False,
        }
        data.update(overrides)
        return argparse.Namespace(**data)

    def test_start_custom_does_not_start_ngrok(self) -> None:
        out = StringIO()
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_start_ngrok") as start_ngrok, \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", return_value=None), \
             redirect_stdout(out):
            code = cli.start(self.args())
        self.assertEqual(0, code)
        start_ngrok.assert_not_called()
        self.assertIn("custom public endpoint configured: https://mac.example.com/mcp", out.getvalue())

    def test_legacy_ngrok_flag_still_starts_ngrok(self) -> None:
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_start_ngrok", return_value=0) as start_ngrok:
            code = cli.start(self.args(ngrok=True))
        self.assertEqual(0, code)
        start_ngrok.assert_called_once()

    def test_custom_mode_stops_managed_ngrok_if_running(self) -> None:
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", side_effect=lambda path: 4242 if path == cli.NGROK_PID_FILE else None), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid:
            code = cli.start(self.args())
        self.assertEqual(0, code)
        stop_pid.assert_called_once_with(cli.NGROK_PID_FILE, "ngrok", 3.0, True)

    def test_start_adopts_existing_mac_mcp_listener_when_pid_file_is_missing(self) -> None:
        args = self.args()
        with patch.object(cli, "_read_pid", return_value=None), \
             patch.object(cli, "_adopt_server_listener", return_value=4321) as adopt, \
             patch.object(cli, "_launch_menu_app"), \
             patch.object(cli.subprocess, "Popen") as popen:
            code = cli._start_server(args)
        self.assertEqual(0, code)
        adopt.assert_called_once_with(8765)
        popen.assert_not_called()

    def test_status_adopts_existing_mac_mcp_listener_when_pid_file_is_missing(self) -> None:
        with patch.object(cli, "_read_pid", return_value=None), \
             patch.object(cli, "_adopt_server_listener", return_value=4321), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "resolve_public_endpoint", return_value=type("Public", (), {"mode": "none", "endpoint_url": None})()), \
             redirect_stdout(StringIO()):
            code = cli.status(argparse.Namespace())
        self.assertEqual(0, code)

    def test_stop_adopts_existing_mac_mcp_listener_before_stopping(self) -> None:
        args = argparse.Namespace(timeout=5, force=True)
        with patch.object(cli, "_read_pid", return_value=None), \
             patch.object(cli, "_adopt_server_listener", return_value=4321) as adopt, \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid, \
             patch.object(cli, "_stop_cloudflare", return_value=True):
            code = cli.stop(args)
        self.assertEqual(0, code)
        adopt.assert_called_once_with(8765)
        self.assertTrue(any(call.args[0] == cli.PID_FILE for call in stop_pid.call_args_list))

    def test_status_prints_selected_custom_mcp_url(self) -> None:
        out = StringIO()
        with patch.object(cli, "_read_pid", return_value=None), \
             patch.object(cli, "_adopt_server_listener", return_value=None), \
             redirect_stdout(out):
            code = cli.status(argparse.Namespace())
        self.assertEqual(1, code)
        rendered = out.getvalue()
        self.assertIn("public endpoint mode: custom", rendered)
        self.assertIn("MCP URL: https://mac.example.com/mcp", rendered)


class PublicEndpointDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-public-doctor-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_doctor_reports_custom_endpoint_health(self) -> None:
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "custom",
            "public_url": "https://mac.example.com/mcp",
            "ngrok_on_start": False,
        }}), encoding="utf-8")
        with patch.object(diagnostics, "_request_json", return_value=(200, {"ok": True, "server": "mac-mcp"})) as request:
            row = diagnostics._check_public_endpoint()
        self.assertEqual("pass", row.status)
        self.assertEqual("PUBLIC_ENDPOINT_HEALTHY", row.reason_code)
        request.assert_called_once_with(
            "https://mac.example.com/health",
            headers={"User-Agent": f"Mac-MCP-Doctor/{diagnostics.__version__}"},
            timeout=3.0,
        )

    def test_doctor_local_only_does_not_probe_network(self) -> None:
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "none",
            "ngrok_on_start": False,
        }}), encoding="utf-8")
        with patch.object(diagnostics, "_request_json") as request:
            row = diagnostics._check_public_endpoint()
        self.assertEqual("info", row.status)
        self.assertEqual("PUBLIC_ENDPOINT_LOCAL_ONLY", row.reason_code)
        request.assert_not_called()


class PublicEndpointSourceContractTests(unittest.TestCase):
    def test_menu_settings_exposes_four_way_switch_and_cloudflare_fields(self) -> None:
        root = Path(__file__).resolve().parents[1]
        settings = (root / "menu_app/Sources/SettingsStore.swift").read_text(encoding="utf-8")
        view = (root / "menu_app/Sources/SettingsView.swift").read_text(encoding="utf-8")
        app_state = (root / "menu_app/Sources/AppState.swift").read_text(encoding="utf-8")
        self.assertIn('public_endpoint_mode', settings)
        self.assertIn('@Published var publicEndpointMode = "none"', settings)
        self.assertIn('Text("Cloudflare").tag("cloudflare")', view)
        self.assertIn('Text("Custom HTTPS").tag("custom")', view)
        self.assertIn('cloudflare_tunnel', settings)
        self.assertIn('@Published var cloudflareTunnel = ""', settings)
        self.assertIn('"--public-mode", settings.publicEndpointMode', app_state)
        self.assertIn('"--public-url", url', app_state)
        self.assertIn('"--cloudflare-tunnel", tunnel', app_state)
        self.assertIn('SecureField("Paste once; it is never written to settings.json"', view)
        self.assertIn('Save credential', view)
        self.assertIn('Replace credential', view)
        self.assertIn('cloudflareCredentialConfigured', app_state)
        self.assertIn('deepMerge', settings)
        self.assertNotIn('cloudflare_token', settings)

    def test_installer_migrates_server_public_endpoint_fields_without_overwrite(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "install.sh").read_text(encoding="utf-8")
        self.assertIn('server.setdefault("public_endpoint_mode"', source)
        self.assertIn('server.setdefault("public_url", "")', source)
        self.assertIn('server.setdefault("cloudflare_tunnel", "")', source)
        self.assertIn('Custom HTTPS', source)


class CloudflarePublicEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-cloudflare-endpoint-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
                "CLOUDFLARE_TUNNEL": "",
                "CLOUDFLARE_TUNNEL_TOKEN_FILE": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_cloudflare_mode_resolves_named_tunnel(self) -> None:
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "cloudflare",
            "public_url": "https://mac.example.com/mcp",
            "cloudflare_tunnel": "mac-mcp-home",
        }}), encoding="utf-8")
        config = resolve_public_endpoint()
        self.assertEqual("cloudflare", config.mode)
        self.assertEqual("https://mac.example.com/mcp", config.endpoint_url)
        self.assertEqual("mac-mcp-home", config.cloudflare_tunnel)

    def test_cloudflare_mode_requires_tunnel_or_token_file(self) -> None:
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "cloudflare",
            "public_url": "https://mac.example.com/mcp",
        }}), encoding="utf-8")
        with self.assertRaises(PublicEndpointError):
            resolve_public_endpoint()

    def _cloudflare_launchd_patches(self, pid: int = 4444):
        root = Path(self.tmp.name)
        plist = root / "cloudflared-launchd.plist"
        return plist, (
            patch.object(cli, "CLOUDFLARE_PID_FILE", root / "cloudflared.pid"),
            patch.object(cli, "CLOUDFLARE_LOG_FILE", root / "cloudflared.log"),
            patch.dict(os.environ, {"MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST": str(plist)}, clear=False),
            patch.object(cli, "_resolve_cloudflared_binary", return_value="/opt/homebrew/bin/cloudflared"),
            patch.object(cli, "_launchctl_pid", return_value=None),
            patch.object(cli, "_cloudflare_launchd_loaded", return_value=False),
            patch.object(cli, "_set_cloudflare_launchd_enabled", return_value=True),
            patch.object(cli, "_bootstrap_cloudflare_launchd", return_value=(True, "")),
            patch.object(cli, "_wait_for_cloudflare_launchd", return_value=pid),
        )

    def test_cloudflare_start_uses_launchd_keepalive_without_secret_args(self) -> None:
        public = type("Public", (), {
            "endpoint_url": "https://mac.example.com/mcp",
            "cloudflare_token_file": None,
            "cloudflare_tunnel": "mac-mcp-home",
        })()
        args = argparse.Namespace(port=8765, cloudflared_bin=None)
        plist, patches = self._cloudflare_launchd_patches(4444)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            code = cli._start_cloudflare(args, public)
        self.assertEqual(0, code)
        payload = plistlib.loads(plist.read_bytes())
        cmd = payload["ProgramArguments"]
        self.assertEqual("/opt/homebrew/bin/cloudflared", cmd[0])
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual("Background", payload["ProcessType"])
        self.assertEqual(5, payload["ThrottleInterval"])
        self.assertIn("--no-autoupdate", cmd)
        self.assertEqual("fatal", cmd[cmd.index("--loglevel") + 1])
        self.assertLess(cmd.index("--loglevel"), cmd.index("run"))
        self.assertIn("http://127.0.0.1:8765", cmd)
        self.assertEqual("mac-mcp-home", cmd[-1])
        self.assertNotIn("--token", cmd)
        self.assertEqual(0o600, stat.S_IMODE(plist.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((Path(self.tmp.name) / "cloudflared.log").stat().st_mode))

    def test_cloudflare_start_accepts_token_file_not_token_value(self) -> None:
        token_file = Path(self.tmp.name) / "tunnel-token"
        token_file.write_text("super-secret-token", encoding="utf-8")
        token_file.chmod(0o600)
        public = type("Public", (), {
            "endpoint_url": "https://mac.example.com/mcp",
            "cloudflare_token_file": str(token_file),
            "cloudflare_tunnel": None,
        })()
        args = argparse.Namespace(port=8765, cloudflared_bin=None)
        plist, patches = self._cloudflare_launchd_patches(5555)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            code = cli._start_cloudflare(args, public)
        self.assertEqual(0, code)
        payload = plistlib.loads(plist.read_bytes())
        cmd = payload["ProgramArguments"]
        self.assertIn("--token-file", cmd)
        self.assertIn(str(token_file), cmd)
        self.assertNotIn("super-secret-token", cmd)
        self.assertEqual("fatal", cmd[cmd.index("--loglevel") + 1])
        self.assertLess(cmd.index("--loglevel"), cmd.index("run"))
        log_path = Path(self.tmp.name) / "cloudflared.log"
        self.assertNotIn("super-secret-token", log_path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(log_path.stat().st_mode))

    def test_cloudflare_start_reuses_existing_launchd_job(self) -> None:
        public = type("Public", (), {
            "endpoint_url": "https://mac.example.com/mcp",
            "cloudflare_token_file": None,
            "cloudflare_tunnel": "mac-mcp-home",
        })()
        args = argparse.Namespace(port=8765, cloudflared_bin=None)
        pid_file = Path(self.tmp.name) / "cloudflared.pid"
        with patch.object(cli, "CLOUDFLARE_PID_FILE", pid_file), \
             patch.object(cli, "_launchctl_pid", return_value=7777), \
             patch.object(cli, "_resolve_cloudflared_binary") as resolve_binary:
            code = cli._start_cloudflare(args, public)
        self.assertEqual(0, code)
        self.assertEqual("7777", pid_file.read_text(encoding="utf-8"))
        resolve_binary.assert_not_called()

    def test_stop_cloudflare_boots_out_and_disables_launchd_job(self) -> None:
        pid_file = Path(self.tmp.name) / "cloudflared.pid"
        pid_file.write_text("8888", encoding="utf-8")
        with patch.object(cli, "CLOUDFLARE_PID_FILE", pid_file), \
             patch.object(cli, "_launchctl_pid", return_value=8888), \
             patch.object(cli, "_bootout_cloudflare_launchd", return_value=True) as bootout, \
             patch.object(cli, "_set_cloudflare_launchd_enabled", return_value=True) as enabled, \
             patch.object(cli, "_pid_alive", return_value=False):
            ok = cli._stop_cloudflare(1.0, True)
        self.assertTrue(ok)
        bootout.assert_called_once_with()
        enabled.assert_called_once_with(False)
        self.assertFalse(pid_file.exists())


class CloudflareCredentialLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-cloudflare-credential-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
                "CLOUDFLARE_TUNNEL_TOKEN_FILE": "",
                "CLOUDFLARE_TUNNEL": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_default_token_path_is_owner_state_file(self) -> None:
        self.assertEqual(Path(self.tmp.name) / "cloudflare-tunnel-token", cloudflare_token_path())

    def test_write_token_is_atomic_owner_only_and_replaceable(self) -> None:
        path = write_cloudflare_token("first-secret")
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(os.getuid(), path.stat().st_uid)
        self.assertEqual("first-secret", path.read_text(encoding="utf-8").strip())
        first_inode = path.stat().st_ino
        replaced = write_cloudflare_token("second-secret")
        self.assertEqual(path, replaced)
        self.assertEqual("second-secret", path.read_text(encoding="utf-8").strip())
        self.assertNotEqual(first_inode, path.stat().st_ino)
        state = inspect_cloudflare_credential(path)
        self.assertTrue(state.configured)
        self.assertTrue(state.secure)
        self.assertEqual("ok", state.reason)

    def test_credential_rejects_group_or_world_permissions(self) -> None:
        path = Path(self.tmp.name) / "cloudflare-tunnel-token"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o644)
        state = inspect_cloudflare_credential(path)
        self.assertTrue(state.configured)
        self.assertFalse(state.secure)
        self.assertEqual("permissions_not_0600", state.reason)

    def test_credential_rejects_symlink(self) -> None:
        real = Path(self.tmp.name) / "real-token"
        real.write_text("secret", encoding="utf-8")
        real.chmod(0o600)
        link = Path(self.tmp.name) / "cloudflare-tunnel-token"
        link.symlink_to(real)
        state = inspect_cloudflare_credential(link)
        self.assertFalse(state.secure)
        self.assertEqual("symlink_not_allowed", state.reason)

    def test_cloudflare_mode_automatically_uses_default_secure_token_file(self) -> None:
        write_cloudflare_token("secret-value")
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "cloudflare",
            "public_url": "https://mac.example.com/mcp",
            "cloudflare_tunnel": "",
        }}), encoding="utf-8")
        config = resolve_public_endpoint()
        self.assertEqual("cloudflare", config.mode)
        self.assertEqual(str(Path(self.tmp.name) / "cloudflare-tunnel-token"), config.cloudflare_token_file)
        self.assertIsNone(config.cloudflare_tunnel)

    def test_cli_credential_save_reads_stdin_and_never_writes_secret_to_settings_or_output(self) -> None:
        sentinel = {"server": {"public_endpoint_mode": "none", "sentinel": "keep"}, "future": {"value": 7}}
        self.settings.write_text(json.dumps(sentinel), encoding="utf-8")
        secret = "cli-super-secret"
        out = StringIO()
        with patch("sys.stdin", StringIO(secret + "\n")), redirect_stdout(out):
            code = cli.credential(argparse.Namespace(provider="cloudflare", action="save"))
        self.assertEqual(0, code)
        self.assertNotIn(secret, out.getvalue())
        self.assertEqual(sentinel, json.loads(self.settings.read_text(encoding="utf-8")))
        token_path = Path(self.tmp.name) / "cloudflare-tunnel-token"
        self.assertEqual(secret, token_path.read_text(encoding="utf-8").strip())
        self.assertEqual(0o600, stat.S_IMODE(token_path.stat().st_mode))

    def test_cloudflare_start_rejects_unsafe_token_permissions_before_popen(self) -> None:
        token_file = Path(self.tmp.name) / "unsafe-token"
        token_file.write_text("secret", encoding="utf-8")
        token_file.chmod(0o644)
        public = type("Public", (), {
            "endpoint_url": "https://mac.example.com/mcp",
            "cloudflare_token_file": str(token_file),
            "cloudflare_tunnel": None,
        })()
        args = argparse.Namespace(port=8765, cloudflared_bin=None)
        with patch.object(cli, "CLOUDFLARE_PID_FILE", Path(self.tmp.name) / "cloudflared.pid"), \
             patch.object(cli, "CLOUDFLARE_LOG_FILE", Path(self.tmp.name) / "cloudflared.log"), \
             patch.object(cli, "_resolve_cloudflared_binary", return_value="/opt/homebrew/bin/cloudflared"), \
             patch.object(cli, "_launchctl_pid", return_value=None), \
             patch.object(cli, "_write_cloudflare_launchd_plist") as write_plist:
            code = cli._start_cloudflare(args, public)
        self.assertEqual(2, code)
        write_plist.assert_not_called()

    def test_doctor_reports_secure_credential_without_secret_value(self) -> None:
        secret = "doctor-secret-must-not-leak"
        write_cloudflare_token(secret)
        self.settings.write_text(json.dumps({"server": {
            "public_endpoint_mode": "cloudflare",
            "public_url": "https://mac.example.com/mcp",
            "cloudflare_tunnel": "",
        }}), encoding="utf-8")
        row = diagnostics._check_cloudflare_credential()
        rendered = json.dumps(row.to_dict(), sort_keys=True)
        self.assertEqual("pass", row.status)
        self.assertEqual("CLOUDFLARE_CREDENTIAL_SECURE", row.reason_code)
        self.assertNotIn(secret, rendered)


class PublicEndpointProviderSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-provider-switch-")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(
            os.environ,
            {
                "MAC_MCP_SETTINGS_PATH": str(self.settings),
                "MAC_MCP_STATE_DIR": self.tmp.name,
                "MAC_MCP_PUBLIC_ENDPOINT_MODE": "",
                "MAC_MCP_PUBLIC_URL": "",
                "NGROK_DOMAIN": "switch.ngrok-free.dev",
                "CLOUDFLARE_TUNNEL_TOKEN_FILE": "",
                "CLOUDFLARE_TUNNEL": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def args(self, mode: str, url: str | None = None):
        return argparse.Namespace(
            public_mode=mode, public_url=url, ngrok=False, ngrok_domain=None, ngrok_bin=None,
            cloudflare_tunnel=None, cloudflare_token_file=None, cloudflared_bin=None,
            host="127.0.0.1", port=8765, reload=False,
        )

    def test_ngrok_mode_stops_managed_cloudflared(self) -> None:
        def read_pid(path):
            return 2222 if path == cli.CLOUDFLARE_PID_FILE else None
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_start_ngrok", return_value=0) as start_ngrok, \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", side_effect=read_pid), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid:
            code = cli.start(self.args("ngrok"))
        self.assertEqual(0, code)
        start_ngrok.assert_called_once()
        stop_pid.assert_called_once_with(cli.CLOUDFLARE_PID_FILE, "cloudflared", 3.0, True)

    def test_cloudflare_mode_stops_managed_ngrok(self) -> None:
        token = write_cloudflare_token("switch-secret")
        def read_pid(path):
            return 3333 if path == cli.NGROK_PID_FILE else None
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_start_cloudflare", return_value=0) as start_cloudflare, \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", side_effect=read_pid), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid:
            code = cli.start(self.args("cloudflare", "https://mac.example.com/mcp"))
        self.assertEqual(0, code)
        start_cloudflare.assert_called_once()
        self.assertEqual(str(token), start_cloudflare.call_args.args[1].cloudflare_token_file)
        stop_pid.assert_called_once_with(cli.NGROK_PID_FILE, "ngrok", 3.0, True)

    def test_local_mode_stops_both_managed_providers(self) -> None:
        def read_pid(path):
            if path == cli.NGROK_PID_FILE: return 4444
            if path == cli.CLOUDFLARE_PID_FILE: return 5555
            return None
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", side_effect=read_pid), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid:
            code = cli.start(self.args("none"))
        self.assertEqual(0, code)
        stopped = {(call.args[0], call.args[1]) for call in stop_pid.call_args_list}
        self.assertEqual({(cli.NGROK_PID_FILE, "ngrok"), (cli.CLOUDFLARE_PID_FILE, "cloudflared")}, stopped)

    def test_custom_mode_stops_both_managed_providers_and_starts_no_tunnel(self) -> None:
        def read_pid(path):
            if path == cli.NGROK_PID_FILE: return 6666
            if path == cli.CLOUDFLARE_PID_FILE: return 7777
            return None
        with patch.object(cli, "_start_server", return_value=0), \
             patch.object(cli, "_start_ngrok") as start_ngrok, \
             patch.object(cli, "_start_cloudflare") as start_cloudflare, \
             patch.object(cli, "_remove_stale_pid"), \
             patch.object(cli, "_read_pid", side_effect=read_pid), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli, "_stop_pid", return_value=True) as stop_pid:
            code = cli.start(self.args("custom", "https://external.example.com/mcp"))
        self.assertEqual(0, code)
        start_ngrok.assert_not_called()
        start_cloudflare.assert_not_called()
        self.assertEqual(2, stop_pid.call_count)


class InstallerPublicEndpointMigrationTests(unittest.TestCase):
    def test_installer_migration_preserves_unrelated_and_legacy_ngrok(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "install.sh").read_text(encoding="utf-8")
        marker = "<<'PYSETTINGS'\n"
        script = source.split(marker, 1)[1].split("\nPYSETTINGS", 1)[0]
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-migrate-") as td:
            settings = Path(td) / "settings.json"
            original = {
                "server": {"port": 8765, "ngrok_on_start": True, "sentinel": "preserve"},
                "future_section": {"nested": {"value": 42}},
                "subagents": {"providers": {"future-provider": {"enabled": False, "extra": "keep"}}},
            }
            settings.write_text(json.dumps(original), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-", str(settings), "0", ""],
                input=script, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            migrated = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual("ngrok", migrated["server"]["public_endpoint_mode"])
            self.assertEqual(8765, migrated["server"]["port"])
            self.assertEqual("preserve", migrated["server"]["sentinel"])
            self.assertEqual(42, migrated["future_section"]["nested"]["value"])
            self.assertEqual("keep", migrated["subagents"]["providers"]["future-provider"]["extra"])
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
