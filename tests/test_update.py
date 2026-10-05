from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server.menu_app_bootstrap as menu_bootstrap_module
import mcp_server.release_trust as release_trust
import mcp_server.tools_update as tools_update_module
import mcp_server.update_helper as update_helper_module
from mcp_server.tools_update import mac_mcp_update
from mcp_server.update_helper import UpdateError, apply_update, check_update, format_check, format_check_json
from mcp_server.update_state import migrate_completed_legacy_update


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(list(args), cwd=str(cwd) if cwd else None, text=True).strip()


class SecureBootstrapMigrationTests(unittest.TestCase):
    def _runtime(self, env_text: str) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-secure-migration-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        runtime = root / "runtime"
        (runtime / "mcp_server").mkdir(parents=True)
        (runtime / "mcp_server/.env").write_text(env_text, encoding="utf-8")
        settings = root / "settings.json"
        return runtime, settings

    def test_legacy_no_auth_ngrok_is_blocked_without_exposing_secret(self):
        runtime, settings = self._runtime(
            "MCP_ALLOW_NO_AUTH=true\nMCP_API_KEY=\nNGROK_DOMAIN=legacy.example.com\n"
        )
        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=True):
            blocker = update_helper_module.secure_bootstrap_update_blocker(runtime)
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual("LEGACY_NO_AUTH_PUBLIC_ENDPOINT", blocker["code"])
        self.assertEqual("ngrok", blocker["public_exposure"])
        self.assertFalse(blocker["api_key_configured"])
        rendered = json.dumps(blocker)
        self.assertNotIn("token_urlsafe", rendered)
        self.assertIn("MCP_ALLOW_NO_AUTH=false", blocker["remediation"])
        self.assertIn("Authorization: Bearer <MCP_API_KEY>", blocker["remediation"])

    def test_legacy_no_auth_public_mode_from_settings_is_blocked(self):
        runtime, settings = self._runtime("MCP_ALLOW_NO_AUTH=true\nMCP_API_KEY=\n")
        settings.write_text(json.dumps({"server": {"public_endpoint_mode": "cloudflare"}}), encoding="utf-8")
        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=True):
            blocker = update_helper_module.secure_bootstrap_update_blocker(runtime)
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual("cloudflare", blocker["public_exposure"])
        self.assertTrue(str(blocker["exposure_source"]).startswith("settings:"))

    def test_explicit_local_only_no_auth_remains_compatible(self):
        runtime, settings = self._runtime("MCP_ALLOW_NO_AUTH=true\nMCP_API_KEY=\nNGROK_DOMAIN=\n")
        settings.write_text(json.dumps({"server": {"public_endpoint_mode": "none"}}), encoding="utf-8")
        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=True):
            blocker = update_helper_module.secure_bootstrap_update_blocker(runtime)
        self.assertIsNone(blocker)

    def test_missing_key_with_auth_required_is_blocked_before_update(self):
        runtime, settings = self._runtime("MCP_ALLOW_NO_AUTH=false\nMCP_API_KEY=\n")
        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=True):
            blocker = update_helper_module.secure_bootstrap_update_blocker(runtime)
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertEqual("MISSING_AUTH_CREDENTIAL", blocker["code"])

    def test_authenticated_public_endpoint_is_compatible(self):
        runtime, settings = self._runtime(
            "MCP_ALLOW_NO_AUTH=false\nMCP_API_KEY=this-is-a-long-test-key-not-a-secret\nNGROK_DOMAIN=legacy.example.com\n"
        )
        with patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(settings)}, clear=True):
            blocker = update_helper_module.secure_bootstrap_update_blocker(runtime)
        self.assertIsNone(blocker)

    def test_apply_update_blocks_before_runtime_merge_and_preserves_config(self):
        runtime, settings = self._runtime(
            "MCP_ALLOW_NO_AUTH=true\nMCP_API_KEY=\nNGROK_DOMAIN=legacy.example.com\n"
        )
        repo = runtime.parent / "repo"
        repo.mkdir()
        original_env = (runtime / "mcp_server/.env").read_bytes()
        update_dir = runtime.parent / "update-state"
        info = SimpleNamespace(
            repo=str(repo), runtime=str(runtime), branch="main", remote="origin",
            deployed_commit="a" * 40, repo_commit="a" * 40, target_commit="b" * 40,
            behind_by=1, update_available=True, dirty=False, release_verified=True,
            release_id="test-stable", release_version="1.0.0",
            release_payload_sha256="c" * 64, release_signer_fingerprint="SHA256:test",
            release_file_count=4, release_artifact_count=0,
            branch_tip_commit="b" * 40, unverified_ahead=0,
        )
        verified = SimpleNamespace(
            release_id="test-stable", version="1.0.0", payload_sha256="c" * 64,
            signer_fingerprint="SHA256:test",
        )
        with patch.dict(os.environ, {
            "MAC_MCP_SETTINGS_PATH": str(settings),
            "MAC_MCP_UPDATE_DIR": str(update_dir),
        }, clear=True), \
                patch("mcp_server.update_helper.check_update", return_value=info), \
                patch("mcp_server.update_helper.release_trust.verify_release_commit", return_value=verified), \
                patch("mcp_server.update_helper._prepare_runtime_merge") as prepare_merge:
            with self.assertRaisesRegex(UpdateError, "Update blocked before runtime swap"):
                apply_update(repo=repo, runtime=runtime, skip_restart=True)
        prepare_merge.assert_not_called()
        self.assertEqual(original_env, (runtime / "mcp_server/.env").read_bytes())
        state = json.loads((update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("blocked", state["status"])
        self.assertEqual("secure_bootstrap_migration_required", state["reason"])
        self.assertEqual("LEGACY_NO_AUTH_PUBLIC_ENDPOINT", state["migration"]["code"])

    def test_tool_update_returns_blocker_before_detached_process(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-blocker-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        info = SimpleNamespace(
            repo=str(repo), runtime=str(runtime), branch="main", remote="origin",
            deployed_commit="a" * 40, target_commit="b" * 40, behind_by=1,
            update_available=True, dirty=False, release_verified=True,
            release_id="test-stable", release_version="1.0.0",
            release_payload_sha256="c" * 64, release_signer_fingerprint="SHA256:test",
            release_file_count=4, release_artifact_count=0,
            branch_tip_commit="b" * 40, unverified_ahead=0,
        )
        blocker = {
            "reason": "secure_bootstrap_migration_required",
            "code": "LEGACY_NO_AUTH_PUBLIC_ENDPOINT",
            "summary": "Update blocked before runtime swap.",
            "remediation": "Configure authentication first.",
        }
        with patch("mcp_server.tools_update.resolve_paths", return_value=(repo, runtime)), \
                patch("mcp_server.tools_update.check_update", return_value=info), \
                patch("mcp_server.tools_update.secure_bootstrap_update_blocker", return_value=blocker), \
                patch("mcp_server.tools_update.subprocess.Popen") as popen:
            result = mac_mcp_update(check_only=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual("secure_bootstrap_migration_required", result["reason"])
        popen.assert_not_called()


class MenuAppParityTests(unittest.TestCase):
    def test_updater_reinstalls_missing_menu_app_and_restarts_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-menu-parity-") as td:
            root = Path(td)
            runtime = root / "runtime"
            installer = runtime / "menu_app" / "install_app.sh"
            installer.parent.mkdir(parents=True)
            installer.write_text("#!/bin/sh\n", encoding="utf-8")
            target = root / "Applications" / "Mac MCP.app"
            calls: list[list[str]] = []

            def fake_run(cmd, cwd=None, check=True, timeout=120):
                calls.append(list(cmd))
                if str(installer) in cmd:
                    executable = target / "Contents" / "MacOS" / "MacMCPMenu"
                    executable.parent.mkdir(parents=True, exist_ok=True)
                    executable.write_text("menu", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.dict(os.environ, {"MAC_MCP_APP_PATH": str(target)}, clear=False):
                os.environ.pop("MAC_MCP_SKIP_MENU_APP_INSTALL", None)
                with patch.object(update_helper_module, "_run", side_effect=fake_run), \
                        patch.object(update_helper_module, "_stop_menu_app") as stop, \
                        patch.object(update_helper_module, "_start_menu_app") as start:
                    refreshed = update_helper_module._refresh_installed_menu_app(runtime)

            self.assertTrue(refreshed)
            stop.assert_called_once_with(target)
            start.assert_called_once_with(target)
            self.assertIn(["/usr/bin/env", "MAC_MCP_MENU_APP_LIFECYCLE_EXTERNAL=1", str(installer), str(target)], calls)
            self.assertIn(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)], calls)

    def test_updater_defaults_to_user_app_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-menu-home-") as td:
            with patch.dict(os.environ, {"HOME": td}, clear=False):
                os.environ.pop("MAC_MCP_APP_PATH", None)
                target = update_helper_module._menu_app_target()
            self.assertEqual(Path(td) / "Applications" / "Mac MCP.app", target)

    def test_updater_menu_refresh_honors_explicit_test_skip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-menu-skip-") as td:
            runtime = Path(td) / "runtime"
            installer = runtime / "menu_app" / "install_app.sh"
            installer.parent.mkdir(parents=True)
            installer.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(os.environ, {"MAC_MCP_SKIP_MENU_APP_INSTALL": "1"}, clear=False), \
                    patch.object(update_helper_module, "_run") as run_cmd:
                refreshed = update_helper_module._refresh_installed_menu_app(runtime)
            self.assertFalse(refreshed)
            run_cmd.assert_not_called()

    def test_menu_process_matching_is_exact_to_installed_bundle(self) -> None:
        app = Path("/Users/test/Applications/Mac MCP.app")
        wanted = str(app / "Contents" / "MacOS" / "MacMCPMenu")
        stdout = "\n".join([
            f"123 {wanted}",
            f"124 {wanted} --unexpected-arg",
            "125 /tmp/Other.app/Contents/MacOS/MacMCPMenu",
        ])
        completed = subprocess.CompletedProcess(["ps"], 0, stdout, "")
        with patch.object(update_helper_module, "_run", return_value=completed):
            pids = update_helper_module._menu_app_process_pids(app)
        self.assertEqual([123], pids)

    def test_target_install_script_self_manages_legacy_updater_transition(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "menu_app" / "install_app.sh").read_text(encoding="utf-8")
        self.assertIn("MAC_MCP_MENU_APP_LIFECYCLE_EXTERNAL", source)
        self.assertIn("Previous Mac MCP.app", source)
        self.assertIn("stop_menu", source)
        self.assertIn("start_menu", source)
        self.assertIn('/usr/bin/open -g -n "$DEST"', source)
        self.assertIn("restore_previous", source)

    def test_startup_bootstrap_delegates_launch_to_install_script(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-menu-bootstrap-") as td:
            root = Path(td)
            source = root / "menu_app"
            source.mkdir()
            installer = source / "install_app.sh"
            installer.write_text("#!/bin/sh\n", encoding="utf-8")
            target = root / "Applications" / "Mac MCP.app"
            completed = subprocess.CompletedProcess([str(installer)], 0, "", "")
            with patch.object(menu_bootstrap_module, "_installed_app", side_effect=[None, target]), \
                    patch.object(menu_bootstrap_module, "_menu_source_candidates", return_value=[source]), \
                    patch.object(menu_bootstrap_module.subprocess, "run", return_value=completed) as run_cmd, \
                    patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MAC_MCP_SKIP_MENU_APP_INSTALL", None)
                installed = menu_bootstrap_module.ensure_menu_app_installed(root)
            self.assertTrue(installed)
            run_cmd.assert_called_once()
            self.assertEqual([str(installer)], run_cmd.call_args.args[0])

    def test_installer_contract_restarts_menu_app_and_restores_running_backup(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn("APP_WAS_RUNNING=0", source)
        self.assertIn("stop_menu_app", source)
        self.assertIn('MAC_MCP_MENU_APP_LIFECYCLE_EXTERNAL=1 "$RUNTIME_DIR/menu_app/install_app.sh"', source)
        self.assertIn('/usr/bin/open -g -n "$APP_PATH"', source)
        install_body = source.split("install_menu_app() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(install_body.index("stop_menu_app"), install_body.index('"$RUNTIME_DIR/menu_app/install_app.sh" "$APP_PATH"'))
        self.assertGreater(install_body.index("launch_menu_app"), install_body.index("code-signature verified"))
        cleanup_body = source.split("cleanup() {", 1)[1].split("\n}\ntrap cleanup", 1)[0]
        self.assertIn('if [[ "$APP_WAS_RUNNING" -eq 1 ]]', cleanup_body)
        self.assertIn('/usr/bin/open -g -n "$APP_PATH"', cleanup_body)

class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.update_dir = Path(tempfile.mkdtemp(prefix="mac-mcp-update-state-test-"))
        self.addCleanup(shutil.rmtree, self.update_dir, True)
        self.signing_key = self.update_dir / "release-test-key"
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-q", "-t", "ed25519",
                "-N", "", "-C", "mac-mcp-test-release", "-f", str(self.signing_key),
            ]
        )
        pub_fields = self.signing_key.with_suffix(".pub").read_text(encoding="utf-8").split()
        self.trusted_signers = self.update_dir / "trusted-signers"
        self.trusted_signers.write_text(
            f"{release_trust.SIGNER_IDENTITY} {pub_fields[0]} {pub_fields[1]}\n",
            encoding="utf-8",
        )
        self.env_patcher = patch.dict(
            os.environ,
            {
                "MAC_MCP_UPDATE_DIR": str(self.update_dir),
                "MAC_MCP_RELEASE_TRUSTED_SIGNERS": str(self.trusted_signers),
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def sign_index_release(self, repo: Path, release_id: str = "test-stable") -> None:
        manifest = release_trust.build_manifest_from_index(
            repo,
            release_id=release_id,
            generated_at="2026-09-18T00:00:00Z",
            branch="main",
        )
        manifest_path = repo / release_trust.MANIFEST_RELPATH
        signature_path = repo / release_trust.SIGNATURE_RELPATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(release_trust.canonical_manifest_bytes(manifest))
        signature_path.unlink(missing_ok=True)
        subprocess.check_call(
            [
                "/usr/bin/ssh-keygen", "-Y", "sign",
                "-f", str(self.signing_key),
                "-n", release_trust.SIGNATURE_NAMESPACE,
                str(manifest_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run(
            "git", "add",
            release_trust.MANIFEST_RELPATH,
            release_trust.SIGNATURE_RELPATH,
            cwd=repo,
        )

    def test_cli_update_waits_on_detached_helper_instead_of_running_inline(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-cli-detached-update-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        log_path = root / "update.log"
        log_path.write_text("[mac-mcp update] Update complete: old -> new\n", encoding="utf-8")
        info = SimpleNamespace(
            repo=str(repo), runtime=str(runtime), branch="main", remote="origin",
            deployed_commit="a" * 40, target_commit="b" * 40, behind_by=1,
            update_available=True, dirty=False, release_verified=True,
            release_id="stable-test", release_version="2.1.8",
            release_payload_sha256="c" * 64, release_signer_fingerprint="SHA256:test",
            release_file_count=4, release_artifact_count=0,
            branch_tip_commit="b" * 40, unverified_ahead=0,
        )
        proc = SimpleNamespace(pid=4242, wait=lambda: 0)
        args = SimpleNamespace(
            check=False, json=False, repo=str(repo), runtime=str(runtime),
            branch="main", remote="origin", skip_restart=False, skip_deps=False,
        )
        with patch(
            "mcp_server.cli.resolve_update_paths", return_value=(repo, runtime)
        ), patch(
            "mcp_server.cli.check_update", return_value=info
        ), patch(
            "mcp_server.cli.secure_bootstrap_update_blocker", return_value=None
        ), patch(
            "mcp_server.cli.launch_detached_update",
            return_value=({"updater_pid": 4242, "log_path": str(log_path)}, proc),
        ) as launch:
            from mcp_server import cli as cli_module
            rc = cli_module.update(args)
        self.assertEqual(0, rc)
        launch.assert_called_once()
        self.assertEqual("main", launch.call_args.kwargs["branch"])
        self.assertEqual("origin", launch.call_args.kwargs["remote"])

    def test_updater_adopts_single_verified_listener_when_pid_record_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="mac-mcp-update-adopt-") as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            pid_file = root / "state" / "mac-mcp.pid"
            snapshot = SimpleNamespace(pid=4242)
            valid = SimpleNamespace(valid=True, pid=4242)

            with patch.object(update_helper_module, "listener_pids", return_value=[4242]),                  patch.object(update_helper_module, "process_snapshot", return_value=snapshot),                  patch.object(update_helper_module, "matches_role", return_value=True),                  patch.object(update_helper_module, "port_is_listening", return_value=True),                  patch.object(update_helper_module, "write_process_record") as write_record,                  patch.object(update_helper_module, "validate_process_record", return_value=valid):
                adopted = update_helper_module._adopt_verified_runtime_listener(
                    pid_file, runtime, 8765
                )

            self.assertIs(adopted, valid)
            write_record.assert_called_once()
            self.assertEqual(
                "updater_verified_listener",
                write_record.call_args.kwargs["metadata"]["ownership_source"],
            )

    def test_restart_cli_strips_python_import_overrides_from_server_env(self):
        with tempfile.TemporaryDirectory(prefix="mac-mcp-update-restart-env-") as td:
            root = Path(td)
            runtime = root / "runtime"
            python = runtime / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            pid_file = Path.home() / ".mac-mcp" / "mac-mcp.pid"
            missing = SimpleNamespace(
                legacy_match=False,
                pid=None,
                status="missing",
                valid=False,
                reason="pid_file_missing",
            )
            snapshot = SimpleNamespace(pid=4321)
            proc = SimpleNamespace(pid=4321)
            proc.poll = lambda: None
            proc.terminate = lambda: None

            with patch.dict(
                os.environ,
                {"PYTHONPATH": "/tmp/source-override", "PYTHONHOME": "/tmp/python-home"},
                clear=False,
            ), patch.object(
                update_helper_module.Path, "home", return_value=root / "home"
            ), patch.object(
                update_helper_module, "validate_process_record", return_value=missing
            ), patch.object(
                update_helper_module, "_adopt_verified_runtime_listener", return_value=None
            ), patch.object(
                update_helper_module.subprocess, "Popen", return_value=proc
            ) as popen, patch.object(
                update_helper_module, "process_snapshot", return_value=snapshot
            ), patch.object(
                update_helper_module, "matches_role", return_value=True
            ), patch.object(
                update_helper_module, "write_process_record"
            ), patch.object(
                update_helper_module.time, "sleep"
            ):
                update_helper_module._restart_cli(runtime, "127.0.0.1", 8765)

            env = popen.call_args.kwargs["env"]
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("PYTHONHOME", env)
            self.assertEqual(str(runtime), env["MAC_MCP_RUNTIME_DIR"])

    def test_updater_refuses_foreign_listener_when_pid_record_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="mac-mcp-update-adopt-foreign-") as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            pid_file = root / "state" / "mac-mcp.pid"
            snapshot = SimpleNamespace(pid=9999)

            with patch.object(update_helper_module, "listener_pids", return_value=[9999]),                  patch.object(update_helper_module, "process_snapshot", return_value=snapshot),                  patch.object(update_helper_module, "matches_role", return_value=False),                  patch.object(update_helper_module, "port_is_listening", return_value=True),                  patch.object(update_helper_module, "write_process_record") as write_record:
                with self.assertRaisesRegex(UpdateError, "unmanaged process"):
                    update_helper_module._adopt_verified_runtime_listener(
                        pid_file, runtime, 8765
                    )
            write_record.assert_not_called()

    def test_detached_update_stages_helper_and_keeps_single_checkout_clean(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-detached-bootstrap-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "checkout"
        runtime = repo
        isolated_cwd = root / "isolated-cwd"
        repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=repo)
        run("git", "config", "user.email", "test@example.com", cwd=repo)
        run("git", "config", "user.name", "Test", cwd=repo)
        (repo / "mcp_server").mkdir()
        (repo / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-q", "-m", "old", cwd=repo)
        remote = root / "remote.git"
        run("git", "clone", "-q", "--bare", str(repo), str(remote))
        run("git", "remote", "add", "origin", str(remote), cwd=repo)
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        isolated_cwd.mkdir()
        info = SimpleNamespace(
            repo=str(repo),
            runtime=str(runtime),
            branch="main",
            remote="origin",
            deployed_commit="a" * 40,
            target_commit="b" * 40,
            behind_by=1,
            update_available=True,
            dirty=False,
            release_verified=True,
            release_id="test-stable",
            release_version="1.0.0",
            release_payload_sha256="c" * 64,
            release_signer_fingerprint="SHA256:test",
            release_file_count=4,
            release_artifact_count=0,
            branch_tip_commit="b" * 40,
            unverified_ahead=0,
        )
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            kwargs["stdout"].close()
            return SimpleNamespace(pid=4242)

        with patch.dict(os.environ, {"MAC_MCP_LAUNCHD_LABEL": ""}), \
                patch("mcp_server.tools_update.resolve_paths", return_value=(repo, runtime)), \
                patch("mcp_server.tools_update.check_update", return_value=info), \
                patch("mcp_server.tools_update.subprocess.Popen", side_effect=fake_popen):
            result = mac_mcp_update(check_only=False)

        self.assertTrue(result["update_started"])
        status_path = Path(result["status_path"])
        log_path = Path(result["log_path"])
        self.assertEqual(self.update_dir / "state.json", status_path)
        self.assertTrue(status_path.is_file())
        self.assertNotIn(repo, status_path.parents)
        self.assertNotIn(runtime, status_path.parents)
        self.assertNotIn(repo, log_path.parents)
        self.assertNotIn(runtime, log_path.parents)
        self.assertFalse((repo / ".mac-mcp-update.json").exists())
        self.assertFalse((repo / ".updates").exists())
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        started = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual("starting", started["status"])
        cmd = captured["cmd"]
        helper = Path(cmd[1])
        staged_state = helper.with_name("update_state.py")
        staged_release_trust = helper.with_name("release_trust.py")
        staged_managed_process = helper.with_name("managed_process.py")
        staged_trusted_signers = helper.with_name("release_trusted_signers.txt")
        self.addCleanup(shutil.rmtree, helper.parent, True)
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(helper.is_file())
        self.assertTrue(staged_state.is_file())
        self.assertTrue(staged_release_trust.is_file())
        self.assertTrue(staged_managed_process.is_file())
        self.assertTrue(staged_trusted_signers.is_file())
        self.assertEqual(helper.parent, staged_state.parent)
        self.assertEqual(helper.parent, staged_release_trust.parent)
        self.assertEqual(helper.parent, staged_managed_process.parent)
        self.assertEqual(helper.parent, staged_trusted_signers.parent)
        self.assertNotIn(repo, helper.parents)
        self.assertNotIn(runtime, helper.parents)
        self.assertEqual(
            helper.read_bytes(),
            Path(tools_update_module.__file__).with_name("update_helper.py").read_bytes(),
        )
        self.assertEqual(
            staged_state.read_bytes(),
            Path(tools_update_module.__file__).with_name("update_state.py").read_bytes(),
        )
        self.assertEqual(captured["kwargs"]["cwd"], str(repo))
        self.assertIs(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["kwargs"]["stderr"], subprocess.STDOUT)
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertIn("--deferred-seconds", cmd)
        self.assertEqual(["--cleanup-staging-dir", str(helper.parent)], cmd[-2:])

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        bootstrap = subprocess.run(
            [sys.executable, "-I", str(helper), "--help"],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        self.assertIn("usage:", bootstrap.stdout)
        self.assertNotIn("ImportError", bootstrap.stderr)

        staged_check = subprocess.run(
            [
                sys.executable,
                "-I",
                str(helper),
                "--check",
                "--repo",
                str(repo),
                "--runtime",
                str(runtime),
                "--cleanup-staging-dir",
                str(helper.parent),
            ],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(staged_check.returncode, 0, staged_check.stderr)
        self.assertFalse(helper.parent.exists())

        source_helper = Path(update_helper_module.__file__).resolve()
        source_check = subprocess.run(
            [
                sys.executable,
                str(source_helper),
                "--check",
                "--repo",
                str(repo),
                "--runtime",
                str(runtime),
                "--cleanup-staging-dir",
                str(source_helper.parent),
            ],
            cwd=str(isolated_cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(source_check.returncode, 0, source_check.stderr)
        self.assertTrue(source_helper.exists())
        self.assertTrue(source_helper.parent.is_dir())

    def test_handled_staged_update_failure_cleans_only_its_staging_dir(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-staged-failure-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        staging = Path(tempfile.mkdtemp(prefix="mac-mcp-update-upd_deadbeef00-under_score_"))
        self.addCleanup(shutil.rmtree, staging, True)
        helper = staging / "update_helper.py"
        shutil.copy2(Path(update_helper_module.__file__), helper)
        shutil.copy2(Path(update_helper_module.__file__).with_name("update_state.py"), staging / "update_state.py")
        shutil.copy2(Path(update_helper_module.__file__).with_name("release_trust.py"), staging / "release_trust.py")
        shutil.copy2(Path(update_helper_module.__file__).with_name("managed_process.py"), staging / "managed_process.py")
        shutil.copy2(
            Path(update_helper_module.__file__).with_name("release_trusted_signers.txt"),
            staging / "release_trusted_signers.txt",
        )

        failed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(helper),
                "--repo",
                str(root / "missing-repo"),
                "--runtime",
                str(root / "missing-runtime"),
                "--cleanup-staging-dir",
                str(staging),
            ],
            cwd=str(root),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("mac-mcp update failed", failed.stderr)
        self.assertFalse(staging.exists())

    def make_fixture(self, conflict: bool = False, delete_old: bool = False, deps_change: bool = False):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "source"
        source.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=source)
        run("git", "config", "user.email", "test@example.com", cwd=source)
        run("git", "config", "user.name", "Test", cwd=source)
        (source / "mcp_server").mkdir()
        (source / "mcp_server/main.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (source / "mcp_server/requirements.txt").write_text("", encoding="utf-8")
        (source / "mcp_server/security.py").write_text("SECURITY = True\n", encoding="utf-8")
        (source / "pyproject.toml").write_text(
            '[project]\nname = "mac-mcp-test"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-q", "-m", "old", cwd=source)
        old = run("git", "rev-parse", "HEAD", cwd=source)

        remote = root / "remote.git"
        run("git", "clone", "-q", "--bare", str(source), str(remote))
        (source / "mcp_server/main.py").write_text("VALUE = 'new'\nNEW_FEATURE = True\n", encoding="utf-8")
        (source / "mcp_server/new_tool.py").write_text("ENABLED = True\n", encoding="utf-8")
        if deps_change:
            (source / "mcp_server/requirements.txt").write_text("# dependency change\n", encoding="utf-8")
        if delete_old:
            (source / "mcp_server/security.py").unlink()
        run("git", "add", ".", cwd=source)
        self.sign_index_release(source, "test-stable")
        run("git", "commit", "-q", "-m", "new signed release", cwd=source)
        target = run("git", "rev-parse", "HEAD", cwd=source)
        run("git", "push", "-q", str(remote), "main", cwd=source)

        repo = root / "repo"
        run("git", "clone", "-q", str(remote), str(repo))
        run("git", "reset", "-q", "--hard", old, cwd=repo)
        runtime = root / "runtime"
        shutil.copytree(repo / "mcp_server", runtime / "mcp_server")
        main = runtime / "mcp_server/main.py"
        if conflict:
            main.write_text("VALUE = 'custom'\n", encoding="utf-8")
        elif not delete_old:
            security = runtime / "mcp_server/security.py"
            security.write_text(security.read_text(encoding="utf-8") + "# RUNTIME_CUSTOMIZATION\n", encoding="utf-8")
        (runtime / "mcp_server/.env").write_text("SECRET_SENTINEL=preserve-me\n", encoding="utf-8")
        return root, repo, runtime, old, target

    def test_crash_after_runtime_sync_recovers_previous_checkpoint(self):
        _, repo, runtime, old, target = self.make_fixture()

        def crash(stage: str) -> None:
            if stage == "runtime_synced":
                raise SystemExit("simulated abrupt updater death")

        with patch("mcp_server.update_helper._test_update_checkpoint_hook", side_effect=crash):
            with self.assertRaises(SystemExit):
                apply_update(repo, runtime, skip_restart=True, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertIn("VALUE = 'new'", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("runtime_synced", state["status"])
        self.assertTrue(state["runtime_sync_started"])

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            recovered = update_helper_module.recover_incomplete_update(repo, runtime)

        self.assertIsNotNone(recovered)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        restart.assert_called_once()
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("recovered", state["status"])
        self.assertTrue(state["recovered_after_crash"])
        self.assertEqual("passed", state["recovery"]["health"]["status"])

    def test_sigkill_after_runtime_sync_is_recovered_from_durable_journal(self):
        _, repo, runtime, old, target = self.make_fixture()
        project_root = Path(update_helper_module.__file__).resolve().parents[1]
        script = (
            "import os, signal\n"
            "import mcp_server.update_helper as u\n"
            "def crash(stage):\n"
            "    if stage == 'runtime_synced':\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "u._test_update_checkpoint_hook = crash\n"
            f"u.apply_update({str(repo)!r}, {str(runtime)!r}, skip_restart=True, skip_deps=True)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(project_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        self.assertEqual(-signal.SIGKILL, proc.returncode)
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertIn("VALUE = 'new'", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("runtime_synced", state["status"])

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            recovered = update_helper_module.recover_incomplete_update(repo, runtime)

        self.assertIsNotNone(recovered)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("recovered", state["status"])
        self.assertTrue(state["recovered_after_crash"])

    def test_crash_during_restart_recovers_and_restarts_previous_runtime(self):
        _, repo, runtime, old, target = self.make_fixture()
        with patch("mcp_server.update_helper._restart_service", side_effect=SystemExit("simulated kill after service stop")):
            with self.assertRaises(SystemExit):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restarting", state["status"])

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            update_helper_module.recover_incomplete_update(repo, runtime)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        restart.assert_called_once()
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("recovered", state["status"])

    def test_crash_after_dependency_swap_restores_previous_environment(self):
        _, repo, runtime, old, target = self.make_fixture(deps_change=True)
        venv = runtime / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text("python\n", encoding="utf-8")
        (venv / "state.txt").write_text("old-env\n", encoding="utf-8")

        def fake_prepare(runtime_path: Path, _requirements: Path, _target_commit: str) -> Path:
            staging_root = Path(tempfile.mkdtemp(prefix=f".{runtime_path.name}.venv-update-", dir=str(runtime_path.parent)))
            staged = staging_root / "candidate"
            shutil.copytree(runtime_path / ".venv", staged)
            (staged / "state.txt").write_text("new-env\n", encoding="utf-8")
            return staged

        def crash(stage: str) -> None:
            if stage == "dependencies_activated":
                raise SystemExit("simulated abrupt updater death")

        with patch("mcp_server.update_helper._prepare_dependency_environment", side_effect=fake_prepare), \
                patch("mcp_server.update_helper._test_update_checkpoint_hook", side_effect=crash):
            with self.assertRaises(SystemExit):
                apply_update(repo, runtime)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("new-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("dependencies_activated", state["status"])

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            update_helper_module.recover_incomplete_update(repo, runtime)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("old-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        self.assertFalse((runtime / ".venv/.mac-mcp-update-env").exists())
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("recovered", state["status"])
        self.assertEqual("restored", state["recovery"]["dependency"]["status"])

    def test_crash_after_dependency_commit_is_finalized_by_next_update_invocation(self):
        _, repo, runtime, _old, target = self.make_fixture(deps_change=True)
        venv = runtime / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text("python\n", encoding="utf-8")
        (venv / "state.txt").write_text("old-env\n", encoding="utf-8")

        def fake_prepare(runtime_path: Path, _requirements: Path, _target_commit: str) -> Path:
            staging_root = Path(tempfile.mkdtemp(prefix=f".{runtime_path.name}.venv-update-", dir=str(runtime_path.parent)))
            staged = staging_root / "candidate"
            shutil.copytree(runtime_path / ".venv", staged)
            (staged / "state.txt").write_text("new-env\n", encoding="utf-8")
            return staged

        def crash(stage: str) -> None:
            if stage == "dependency_committed":
                raise SystemExit("simulated abrupt updater death")

        with patch("mcp_server.update_helper._prepare_dependency_environment", side_effect=fake_prepare), \
                patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=True), \
                patch("mcp_server.update_helper._test_update_checkpoint_hook", side_effect=crash):
            with self.assertRaises(SystemExit):
                apply_update(repo, runtime)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(target, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        self.assertEqual("new-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("dependency_committed", state["status"])

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            result = apply_update(repo, runtime, skip_deps=True)

        self.assertFalse(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("new-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        restart.assert_called_once()
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", state["status"])
        self.assertTrue(state["recovered_after_crash"])
        self.assertEqual("finalized_target", state["recovery"]["status"])

    def test_transaction_checkpoint_persistence_is_required_before_repo_mutation(self):
        _, repo, runtime, old, _target = self.make_fixture()
        with patch("mcp_server.update_helper.write_update_state", return_value=None), \
                patch("mcp_server.update_helper.read_update_state", return_value=None):
            with self.assertRaisesRegex(UpdateError, "durably persist updater transaction checkpoint 'prepared'"):
                apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))

    def test_check_and_update_preserve_runtime_overlay_and_env(self):
        _, repo, runtime, old, target = self.make_fixture()
        info = check_update(repo, runtime)
        self.assertTrue(info.update_available)
        self.assertEqual(1, info.behind_by)
        self.assertIn("Verified update available", format_check(info))
        structured = json.loads(format_check_json(info))
        self.assertTrue(structured["ok"])
        self.assertTrue(structured["update_available"])
        self.assertEqual(info.target_commit, structured["target_commit"])

        cli_result = subprocess.run(
            [
                sys.executable, "-m", "mcp_server.cli", "update", "--check", "--json",
                "--repo", str(repo), "--runtime", str(runtime),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, cli_result.returncode, cli_result.stderr)
        cli_payload = json.loads(cli_result.stdout)
        self.assertTrue(cli_payload["ok"])
        self.assertTrue(cli_payload["update_available"])
        self.assertEqual(info.target_commit, cli_payload["target_commit"])

        result = apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        text = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        self.assertIn("VALUE = 'new'", text)
        self.assertNotIn("RUNTIME_CUSTOMIZATION", text)
        self.assertIn("RUNTIME_CUSTOMIZATION", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertTrue((runtime / "mcp_server/new_tool.py").exists())
        self.assertIn("preserve-me", (runtime / "mcp_server/.env").read_text(encoding="utf-8"))
        self.assertEqual(target, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue(Path(result["backup"]).exists())
        self.assertTrue(str(Path(result["backup"])).startswith(str(self.update_dir / "backups")))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups/updates").exists())

    def test_single_checkout_update_stays_clean_and_second_check_works(self):
        root, repo, _runtime, _old, target = self.make_fixture()
        # Public install shape: one Git checkout is both source repo and live runtime.
        single = root / "single"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)

        before = check_update(single, single)
        self.assertTrue(before.update_available)
        result = apply_update(single, single, skip_restart=True, skip_deps=True)
        self.assertTrue(result["updated"])
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=single))

        after = check_update(single, single)
        self.assertFalse(after.dirty)
        self.assertFalse(after.update_available)
        self.assertIn("up to date", format_check(after))
        self.assertTrue((self.update_dir / "deployed-commit").exists())
        self.assertTrue((self.update_dir / "state.json").exists())
        self.assertTrue((self.update_dir / "backups").exists())

    # ASSURANCE: SEC-UPD-001
    def test_health_failure_rolls_back_split_repo_runtime_and_marker(self):
        _, repo, runtime, old, target = self.make_fixture()
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", side_effect=[False, True]):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("main", run("git", "branch", "--show-current", cwd=repo))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("RUNTIME_CUSTOMIZATION", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertTrue(state["repo_head_moved"])
        self.assertEqual(old, state["repo_pre_update_commit"])
        self.assertEqual(target, state["repo_post_merge_commit"])
        self.assertEqual(2, restart.call_count)
        self.assertEqual("restored", state["runtime_rollback"]["status"])
        self.assertEqual("passed", state["rollback_health"]["status"])

    def test_dependency_activation_failure_preserves_existing_environment(self):
        root = Path(tempfile.mkdtemp(prefix="mac-mcp-dependency-activate-failure-"))
        self.addCleanup(shutil.rmtree, root, True)
        runtime = root / "runtime"
        canonical = runtime / ".venv"
        (canonical / "bin").mkdir(parents=True)
        (canonical / "bin/python").write_text("python\n", encoding="utf-8")
        (canonical / "state.txt").write_text("old-env\n", encoding="utf-8")
        staging_root = root / ".runtime.venv-update-test"
        staged = staging_root / "candidate"
        (staged / "bin").mkdir(parents=True)
        (staged / "bin/python").write_text("python\n", encoding="utf-8")
        (staged / "state.txt").write_text("new-env\n", encoding="utf-8")

        with patch.object(update_helper_module.os, "replace", side_effect=OSError("rename blocked")):
            with self.assertRaisesRegex(OSError, "rename blocked"):
                update_helper_module._activate_dependency_environment(runtime, staged)

        self.assertEqual("old-env\n", (canonical / "state.txt").read_text(encoding="utf-8"))
        self.assertFalse(staging_root.exists())

    def test_dependency_health_failure_restores_previous_environment(self):
        _, repo, runtime, old, _target = self.make_fixture(deps_change=True)
        venv = runtime / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text("python\n", encoding="utf-8")
        (venv / "state.txt").write_text("old-env\n", encoding="utf-8")

        def fake_prepare(runtime_path: Path, _requirements: Path, _target_commit: str) -> Path:
            staging_root = Path(tempfile.mkdtemp(prefix=f".{runtime_path.name}.venv-update-", dir=str(runtime_path.parent)))
            staged = staging_root / "candidate"
            shutil.copytree(runtime_path / ".venv", staged)
            (staged / "state.txt").write_text("new-env\n", encoding="utf-8")
            return staged

        with patch("mcp_server.update_helper._prepare_dependency_environment", side_effect=fake_prepare), \
                patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("old-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        self.assertFalse((runtime / ".venv/.mac-mcp-update-env").exists())
        self.assertEqual([], list(runtime.parent.glob(f".{runtime.name}.venv-update-*")))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["dependency_rollback"]["status"])
        self.assertTrue(state["dependency_install_attempted"])

    def test_dependency_health_success_commits_staged_environment(self):
        _, repo, runtime, _old, target = self.make_fixture(deps_change=True)
        venv = runtime / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text("python\n", encoding="utf-8")
        (venv / "state.txt").write_text("old-env\n", encoding="utf-8")

        def fake_prepare(runtime_path: Path, _requirements: Path, _target_commit: str) -> Path:
            staging_root = Path(tempfile.mkdtemp(prefix=f".{runtime_path.name}.venv-update-", dir=str(runtime_path.parent)))
            staged = staging_root / "candidate"
            shutil.copytree(runtime_path / ".venv", staged)
            (staged / "state.txt").write_text("new-env\n", encoding="utf-8")
            return staged

        with patch("mcp_server.update_helper._prepare_dependency_environment", side_effect=fake_prepare), \
                patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=True):
            result = apply_update(repo, runtime)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("new-env\n", (runtime / ".venv/state.txt").read_text(encoding="utf-8"))
        self.assertFalse((runtime / ".venv/.mac-mcp-update-env").exists())
        self.assertEqual([], list(runtime.parent.glob(f".{runtime.name}.venv-update-*")))
        self.assertEqual("activated", result["dependency_environment"]["status"])

    def test_post_update_gate_success_is_required_and_recorded(self):
        _, repo, runtime, _old, target = self.make_fixture()
        gate_report = {
            "ok": True,
            "status": "passed",
            "target_commit": target,
            "duration_ms": 321,
            "critical_failures": [],
            "warnings": ["companion.chrome_files"],
        }
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=True), \
                patch("mcp_server.update_helper._target_supports_health_gate", return_value=True), \
                patch("mcp_server.update_helper._read_health_gate_report", return_value=gate_report):
            result = apply_update(repo, runtime, skip_deps=True)

        self.assertTrue(result["updated"])
        self.assertEqual(target, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        self.assertEqual("passed", result["health_gate"]["status"])
        self.assertEqual(321, result["health_gate"]["duration_ms"])
        self.assertEqual(["companion.chrome_files"], result["health_gate"]["warnings"])
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("completed", state["status"])
        self.assertEqual("passed", state["health_gate"]["status"])

    def test_post_update_gate_failure_rolls_back_and_reports_critical_check(self):
        _, repo, runtime, old, target = self.make_fixture()
        gate_report = {
            "ok": False,
            "status": "failed",
            "target_commit": target,
            "critical_failures": ["public.health"],
            "warnings": [],
        }
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health") as restart, \
                patch("mcp_server.update_helper._health_ok", return_value=False), \
                patch("mcp_server.update_helper._read_health_gate_report", return_value=gate_report):
            with self.assertRaisesRegex(UpdateError, "Post-update health gate failed: public.health"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual("restore_unverified", state["runtime_rollback"]["status"])
        self.assertEqual("failed", state["rollback_health"]["status"])
        self.assertEqual(2, restart.call_count)

    def test_health_failure_rolls_back_single_checkout_and_keeps_it_clean(self):
        root, repo, _runtime, old, _target = self.make_fixture()
        single = root / "single-health-failure"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(single, single, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=single))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual("restore_unverified", state["runtime_rollback"]["status"])
        self.assertEqual("failed", state["rollback_health"]["status"])

    # ASSURANCE: SEC-UPD-001
    def test_health_failure_restores_pre_update_repo_head_not_deployed_marker(self):
        root, repo, runtime, deployed, repo_head = self.make_fixture()
        run("git", "reset", "--hard", "-q", repo_head, cwd=repo)
        (repo / "mcp_server/main.py").write_text("VALUE = 'latest'\n", encoding="utf-8")
        run("git", "add", ".", cwd=repo)
        self.sign_index_release(repo, "test-stable-latest")
        run("git", "commit", "-q", "-m", "latest signed release", cwd=repo)
        target = run("git", "rev-parse", "HEAD", cwd=repo)
        run("git", "push", "-q", "origin", "main", cwd=repo)
        run("git", "reset", "--hard", "-q", repo_head, cwd=repo)
        (self.update_dir / "deployed-commit").write_text(deployed + "\n", encoding="utf-8")

        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(repo_head, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertNotEqual(deployed, run("git", "rev-parse", "HEAD", cwd=repo))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual(repo_head, state["repo_pre_update_commit"])
        self.assertEqual(target, state["repo_post_merge_commit"])

    def test_health_failure_restores_new_and_deleted_runtime_files(self):
        _, repo, runtime, old, _target = self.make_fixture(delete_old=True)
        with patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("SECURITY = True\n", (runtime / "mcp_server/security.py").read_text(encoding="utf-8"))
        self.assertFalse((runtime / "mcp_server/new_tool.py").exists())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("restored", state["repo_rollback"]["status"])
        self.assertEqual("restore_unverified", state["runtime_rollback"]["status"])
        self.assertEqual("failed", state["rollback_health"]["status"])

    def test_health_failure_does_not_reset_when_repo_head_was_already_target(self):
        root, repo, runtime, old, target = self.make_fixture()
        run("git", "reset", "--hard", "-q", target, cwd=repo)
        (self.update_dir / "deployed-commit").write_text(old + "\n", encoding="utf-8")

        with patch.object(update_helper_module, "_run", wraps=update_helper_module._run) as run_command, \
                patch("mcp_server.update_helper._restart_service", return_value="http://127.0.0.1:8000/health"), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        reset_calls = [
            call.args[0]
            for call in run_command.call_args_list
            if call.args and len(call.args[0]) >= 4 and call.args[0][3] == "reset"
        ]
        self.assertEqual([], reset_calls)
        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("did not move", state["repo_rollback"]["reason"])

    # ASSURANCE: SEC-UPD-001
    def test_user_edit_after_merge_skips_repo_rollback_but_restores_runtime(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0

        def restart_with_user_edit(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (repo / "mcp_server/main.py").write_text("USER_EDIT = True\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_edit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertIn("USER_EDIT", (repo / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("mcp_server/main.py", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual("VALUE = 'old'\n", (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("not clean", state["repo_rollback"]["reason"])
        self.assertEqual(2, restart_calls)

    def test_untracked_file_after_merge_skips_repo_rollback(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0

        def restart_with_untracked_file(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (repo / "user-untracked.txt").write_text("preserve-me\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_untracked_file), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual("preserve-me\n", (repo / "user-untracked.txt").read_text(encoding="utf-8"))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("not clean", state["repo_rollback"]["reason"])

    def test_user_commit_after_merge_is_preserved(self):
        _, repo, runtime, old, target = self.make_fixture()
        restart_calls = 0
        user_commit = None

        def restart_with_user_commit(*_args):
            nonlocal restart_calls, user_commit
            restart_calls += 1
            if restart_calls == 1:
                (repo / "user-commit.txt").write_text("preserve-me\n", encoding="utf-8")
                run("git", "add", "user-commit.txt", cwd=repo)
                run("git", "commit", "-q", "-m", "user commit during update", cwd=repo)
                user_commit = run("git", "rev-parse", "HEAD", cwd=repo)
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_commit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(repo, runtime, skip_deps=True)

        self.assertIsNotNone(user_commit)
        self.assertEqual(user_commit, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertNotEqual(target, user_commit)
        self.assertEqual("preserve-me\n", (repo / "user-commit.txt").read_text(encoding="utf-8"))
        self.assertEqual("", run("git", "status", "--porcelain", cwd=repo))
        self.assertEqual(old, (self.update_dir / "deployed-commit").read_text(encoding="utf-8").strip())
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertIn("no longer matches", state["repo_rollback"]["reason"])

    def test_same_checkout_user_edit_is_not_overwritten_when_repo_rollback_skips(self):
        root, repo, _runtime, old, target = self.make_fixture()
        single = root / "single-user-edit"
        shutil.copytree(repo, single)
        run("git", "remote", "set-url", "origin", str(root / "remote.git"), cwd=single)
        restart_calls = 0

        def restart_with_user_edit(*_args):
            nonlocal restart_calls
            restart_calls += 1
            if restart_calls == 1:
                (single / "mcp_server/main.py").write_text("USER_EDIT = True\n", encoding="utf-8")
            return "http://127.0.0.1:8000/health"

        with patch("mcp_server.update_helper._restart_service", side_effect=restart_with_user_edit), \
                patch("mcp_server.update_helper._health_ok", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Health check failed after restart"):
                apply_update(single, single, skip_deps=True)

        self.assertEqual(target, run("git", "rev-parse", "HEAD", cwd=single))
        self.assertEqual("USER_EDIT = True\n", (single / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertIn("mcp_server/main.py", run("git", "status", "--porcelain", cwd=single))
        state = json.loads((self.update_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", state["repo_rollback"]["status"])
        self.assertEqual("skipped", state["runtime_rollback"]["status"])

    def test_completed_legacy_state_moves_out_of_checkout(self):
        runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-legacy-update-test-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        commit = "a" * 40
        (runtime / ".mac-mcp-deployed-commit").write_text(commit + "\n", encoding="utf-8")
        (runtime / ".mac-mcp-update.json").write_text(json.dumps({"status": "completed", "to_commit": commit}) + "\n", encoding="utf-8")
        old_backup = runtime / "backups" / "updates" / "legacy-backup"
        old_backup.mkdir(parents=True)
        (old_backup / "manifest.json").write_text("{}\n", encoding="utf-8")

        self.assertTrue(migrate_completed_legacy_update(runtime))
        self.assertFalse((runtime / ".mac-mcp-deployed-commit").exists())
        self.assertFalse((runtime / ".mac-mcp-update.json").exists())
        self.assertFalse((runtime / "backups").exists())
        self.assertEqual(commit, (self.update_dir / "deployed-commit").read_text().strip())
        self.assertTrue((self.update_dir / "backups" / "legacy-backup" / "manifest.json").exists())

    def test_conflicting_runtime_overlay_aborts_before_repo_or_runtime_change(self):
        _, repo, runtime, old, _ = self.make_fixture(conflict=True)
        before = (runtime / "mcp_server/main.py").read_text(encoding="utf-8")
        with self.assertRaises(UpdateError):
            apply_update(repo, runtime, skip_restart=True, skip_deps=True)
        self.assertEqual(old, run("git", "rev-parse", "HEAD", cwd=repo))
        self.assertEqual(before, (runtime / "mcp_server/main.py").read_text(encoding="utf-8"))
        self.assertFalse((self.update_dir / "backups").exists())
        run("git", "worktree", "prune", cwd=repo)

    def test_dirty_repo_is_reported(self):
        _, repo, runtime, _, _ = self.make_fixture()
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        info = check_update(repo, runtime)
        self.assertTrue(info.dirty)
        self.assertIn("Update blocked", format_check(info))


if __name__ == "__main__":
    unittest.main()
