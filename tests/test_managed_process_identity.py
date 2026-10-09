from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli, diagnostics, managed_process, update_helper
from mcp_server.managed_process import (
    ProcessSnapshot,
    ProcessValidation,
    matches_role,
    record_mode,
    validate_process_record,
    write_process_record,
)


def server_snapshot(pid: int, root: Path, *, start: str = "Tue Sep 29 12:00:00 2026") -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        start_time=start,
        executable="/usr/bin/python3",
        command=(
            "/usr/bin/python3 -m uvicorn mcp_server.main:app "
            "--host 127.0.0.1 --port 8765"
        ),
        cwd=str(root),
    )


class ManagedProcessRecordTests(unittest.TestCase):
    def test_record_is_owner_only_and_validates_same_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-process-record-") as td:
            root = Path(td)
            path = root / "mac-mcp.pid"
            snap = server_snapshot(4242, root)
            write_process_record(
                path,
                "server",
                4242,
                metadata={"port": 8765},
                snapshot=snap,
            )
            self.assertEqual(0o600, record_mode(path))
            with patch("mcp_server.managed_process.pid_alive", return_value=True),                  patch("mcp_server.managed_process.process_snapshot", return_value=snap):
                validation = validate_process_record(
                    path,
                    "server",
                    port=8765,
                    project_root=root,
                )
            self.assertTrue(validation.valid)
            self.assertEqual("json", validation.record_format)

    def test_pid_reuse_is_detected_by_start_time_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-process-reuse-") as td:
            root = Path(td)
            path = root / "mac-mcp.pid"
            original = server_snapshot(4242, root, start="Tue Sep 29 12:00:00 2026")
            replacement = server_snapshot(4242, root, start="Tue Sep 29 12:01:00 2026")
            write_process_record(path, "server", 4242, metadata={"port": 8765}, snapshot=original)
            with patch("mcp_server.managed_process.pid_alive", return_value=True),                  patch("mcp_server.managed_process.process_snapshot", return_value=replacement):
                validation = validate_process_record(
                    path, "server", port=8765, project_root=root,
                )
            self.assertEqual("identity_mismatch", validation.status)
            self.assertEqual("process_fingerprint_mismatch", validation.reason)

    def test_legacy_pid_pointing_to_foreign_process_is_not_owned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-process-legacy-") as td:
            root = Path(td)
            path = root / "mac-mcp.pid"
            path.write_text("4242\n", encoding="utf-8")
            foreign = ProcessSnapshot(
                pid=4242,
                start_time="Tue Sep 29 12:00:00 2026",
                executable="/usr/bin/python3",
                command="/usr/bin/python3 -m http.server 8765",
                cwd=str(root),
            )
            with patch("mcp_server.managed_process.pid_alive", return_value=True),                  patch("mcp_server.managed_process.process_snapshot", return_value=foreign):
                validation = validate_process_record(
                    path, "server", port=8765, project_root=root,
                )
            self.assertEqual("role_mismatch", validation.status)

    def test_server_role_requires_expected_cwd_and_port(self) -> None:
        root = Path("/tmp/mac-mcp-runtime")
        good = server_snapshot(111, root)
        self.assertTrue(matches_role(good, "server", port=8765, project_root=root))
        self.assertFalse(matches_role(good, "server", port=8877, project_root=root))
        self.assertFalse(matches_role(good, "server", port=8765, project_root="/tmp/other"))


class SafeLifecycleTests(unittest.TestCase):
    def test_stop_refuses_mismatched_live_pid_without_signal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-stop-mismatch-") as td:
            path = Path(td) / "mac-mcp.pid"
            path.write_text("{}\n", encoding="utf-8")
            mismatch = ProcessValidation(
                "identity_mismatch", 7777, "server", "json",
                "process_fingerprint_mismatch",
            )
            with patch.object(cli, "_validate_managed_pid", return_value=mismatch),                  patch.object(cli.os, "kill") as kill:
                ok = cli._stop_pid(path, "mac-mcp", 0.1, True)
            self.assertFalse(ok)
            kill.assert_not_called()
            self.assertFalse(path.exists())

    def test_force_stop_never_kills_reused_pid_after_term(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-stop-reuse-") as td:
            root = Path(td)
            path = root / "mac-mcp.pid"
            snap = server_snapshot(8888, root)
            valid = ProcessValidation(
                "valid", 8888, "server", "json", "record_matches_process", snap,
            )
            reused = ProcessValidation(
                "identity_mismatch", 8888, "server", "json",
                "process_fingerprint_mismatch",
            )
            with patch.object(cli, "_validate_managed_pid", side_effect=[valid, reused]),                  patch.object(cli.os, "kill") as kill:
                ok = cli._stop_pid(path, "mac-mcp", 1.0, True)
            self.assertTrue(ok)
            kill.assert_called_once_with(8888, signal.SIGTERM)

    def test_start_refuses_real_foreign_listener_and_leaves_it_alive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-foreign-listener-") as td:
            state = Path(td)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            foreign = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                cwd=td,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            def cleanup_foreign() -> None:
                if foreign.poll() is None:
                    foreign.terminate()
                    try:
                        foreign.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        foreign.kill()
                        foreign.wait(timeout=2)
            self.addCleanup(cleanup_foreign)
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)

            args = argparse.Namespace(host="127.0.0.1", port=port, reload=False)
            pid_file = state / "mac-mcp.pid"
            log_file = state / "mac-mcp.log"
            with patch.object(cli, "listener_pids", return_value=[]), \
                 patch.object(cli, "port_is_listening", return_value=True):
                owned, foreign_pids = cli._server_listener_state(port)
                self.assertEqual([], owned)
                self.assertEqual(
                    [0],
                    foreign_pids,
                    "occupied port must remain foreign when PID discovery is unavailable",
                )

                with patch.object(cli, "STATE_DIR", state), \
                     patch.object(cli, "PID_FILE", pid_file), \
                     patch.object(cli, "LOG_FILE", log_file), \
                     patch.object(cli, "_launch_menu_app"):
                    code = cli._start_server(args)

            self.assertEqual(1, code)
            self.assertIsNone(foreign.poll(), "foreign listener must remain untouched")
            self.assertFalse(pid_file.exists())
            self.assertFalse(log_file.exists(), "mac-mcp must not attempt a competing spawn")


    def test_port_listener_probe_detects_active_listener(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        try:
            self.assertTrue(managed_process.port_is_listening(port))
        finally:
            server.close()

    def test_port_listener_probe_ignores_time_wait_after_listener_closes(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)

        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        conn, _ = server.accept()
        client.close()
        conn.close()
        server.close()

        # The teardown can leave TIME_WAIT sockets on this local port. That
        # must not be interpreted as a live LISTEN socket.
        self.assertEqual([], managed_process.listener_pids(port))
        self.assertFalse(managed_process.port_is_listening(port))

    def test_listener_state_fails_closed_when_pid_discovery_is_empty(self) -> None:
        with patch.object(cli, "listener_pids", return_value=[]), \
             patch.object(cli, "port_is_listening", return_value=True):
            owned, foreign = cli._server_listener_state(8765)
        self.assertEqual([], owned)
        self.assertEqual([0], foreign)

    def test_doctor_fails_closed_when_port_is_occupied_but_pid_unknown(self) -> None:
        with patch("mcp_server.diagnostics.listener_pids", return_value=[]), \
             patch("mcp_server.diagnostics.port_is_listening", return_value=True), \
             patch("mcp_server.diagnostics._launchctl_pid", return_value=None), \
             patch("mcp_server.diagnostics._local_host_port", return_value=("127.0.0.1", 8765)), \
             patch("mcp_server.diagnostics.state_dir", return_value=Path("/nonexistent/mac-mcp-test-state")):
            row = diagnostics._check_managed_process("server")
        self.assertEqual("fail", row.status)
        self.assertEqual("SERVER_PORT_FOREIGN_LISTENER", row.reason_code)
        self.assertEqual("unavailable", row.details["listener_pid_resolution"])
        self.assertEqual([], row.details["foreign_listener_pids"])

    def test_start_refuses_second_server_when_verified_record_uses_old_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-old-port-") as td:
            state = Path(td)
            path = state / "mac-mcp.pid"
            snap = server_snapshot(4242, Path("/tmp/runtime"))
            write_process_record(
                path,
                "server",
                4242,
                metadata={"port": 8000, "ownership_source": "spawn"},
                snapshot=snap,
            )
            valid = ProcessValidation(
                "valid", 4242, "server", "json", "record_matches_process", snap,
            )
            args = argparse.Namespace(host="127.0.0.1", port=8765, reload=False)
            with patch.object(cli, "STATE_DIR", state), \
                 patch.object(cli, "PID_FILE", path), \
                 patch.object(cli, "_validate_managed_pid", return_value=valid), \
                 patch.object(cli.subprocess, "Popen") as popen:
                code = cli._start_server(args)
            self.assertEqual(1, code)
            popen.assert_not_called()

    def test_restart_parent_always_handoffs_to_launchd(self) -> None:
        args = argparse.Namespace(timeout=1.0)
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}, clear=False), \
             patch.object(cli, "_load_env"), \
             patch.object(cli, "_spawn_detached_restart", return_value=0) as handoff, \
             patch.object(cli, "stop") as stop, \
             patch.object(cli, "start") as start:
            code = cli.restart(args)
        self.assertEqual(0, code)
        handoff.assert_called_once_with(args)
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_handoff_child_aborts_when_safe_stop_fails(self) -> None:
        args = argparse.Namespace(timeout=1.0)
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: "1"}, clear=False), \
             patch.object(cli, "_load_env"), \
             patch.object(cli, "_restart_preflight", return_value=None), \
             patch.object(cli, "_write_restart_status") as status_write, \
             patch.object(cli.time, "sleep"), \
             patch.object(cli, "stop", return_value=1) as stop, \
             patch.object(cli, "start") as start:
            code = cli.restart(args)
        self.assertEqual(1, code)
        stop.assert_called_once()
        start.assert_not_called()
        status_write.assert_any_call("failed", stage="stop", exit_code=1, helper_pid=os.getpid())

    def test_restart_handoff_child_clears_worker_identity_before_start(self) -> None:
        args = argparse.Namespace(timeout=1.0, host="127.0.0.1", port=8765)
        inherited: list[tuple[str | None, str | None]] = []

        def start_side_effect(_args):
            inherited.append((
                os.environ.get(cli.RESTART_HANDOFF_ENV),
                os.environ.get(cli.RESTART_REQUESTER_ENV),
            ))
            return 0

        with patch.dict(
            os.environ,
            {cli.RESTART_HANDOFF_ENV: "1", cli.RESTART_REQUESTER_ENV: "4242"},
            clear=False,
        ),              patch.object(cli, "_load_env"),              patch.object(cli, "_restart_preflight", return_value=None),              patch.object(cli, "_write_restart_status"),              patch.object(cli, "_wait_for_restart_requester_exit", return_value=True),              patch.object(cli, "stop", return_value=0),              patch.object(cli, "start", side_effect=start_side_effect),              patch.object(cli, "_restart_health_ok", return_value=True),              patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")):
            code = cli.restart(args)

        self.assertEqual(0, code)
        self.assertEqual([(None, None)], inherited)

    def test_start_server_strips_restart_coordination_from_uvicorn_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-restart-env-") as td:
            state = Path(td)
            args = argparse.Namespace(host="127.0.0.1", port=8765, reload=False)
            missing = ProcessValidation(
                "missing", None, "server", None, "pid_file_missing",
            )
            snap = server_snapshot(4321, cli.PROJECT_ROOT)
            with patch.dict(
                os.environ,
                {cli.RESTART_HANDOFF_ENV: "1", cli.RESTART_REQUESTER_ENV: "4242"},
                clear=False,
            ),                  patch.object(cli, "STATE_DIR", state),                  patch.object(cli, "PID_FILE", state / "mac-mcp.pid"),                  patch.object(cli, "LOG_FILE", state / "mac-mcp.log"),                  patch.object(cli, "_validate_managed_pid", return_value=missing),                  patch.object(cli, "_server_listener_state", return_value=([], [])),                  patch.object(cli.subprocess, "Popen") as popen,                  patch.object(cli, "process_snapshot", return_value=snap),                  patch.object(cli, "matches_role", return_value=True),                  patch.object(cli, "write_process_record"),                  patch.object(cli, "_restart_health_ok", return_value=True),                  patch.object(cli, "_launch_menu_app"),                  patch.object(cli.time, "sleep"):
                popen.return_value.pid = 4321
                popen.return_value.poll.return_value = None
                code = cli._start_server(args)

        self.assertEqual(0, code)
        spawn_env = popen.call_args.kwargs["env"]
        self.assertNotIn(cli.RESTART_HANDOFF_ENV, spawn_env)
        self.assertNotIn(cli.RESTART_REQUESTER_ENV, spawn_env)

    def test_start_server_health_failure_terminates_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-start-health-") as td:
            state = Path(td)
            args = argparse.Namespace(host="127.0.0.1", port=8765, reload=False)
            missing = ProcessValidation(
                "missing", None, "server", None, "pid_file_missing",
            )
            snap = server_snapshot(4321, cli.PROJECT_ROOT)
            with patch.object(cli, "STATE_DIR", state), \
                 patch.object(cli, "PID_FILE", state / "mac-mcp.pid"), \
                 patch.object(cli, "LOG_FILE", state / "mac-mcp.log"), \
                 patch.object(cli, "_validate_managed_pid", return_value=missing), \
                 patch.object(cli, "_server_listener_state", return_value=([], [])), \
                 patch.object(cli.subprocess, "Popen") as popen, \
                 patch.object(cli, "process_snapshot", return_value=snap), \
                 patch.object(cli, "matches_role", return_value=True), \
                 patch.object(cli, "write_process_record"), \
                 patch.object(cli, "_restart_health_ok", return_value=False) as health, \
                 patch.object(cli, "_launch_menu_app") as launch, \
                 patch.object(cli.time, "sleep"):
                popen.return_value.pid = 4321
                popen.return_value.poll.return_value = None
                popen.return_value.wait.return_value = 0
                code = cli._start_server(args)

        self.assertEqual(1, code)
        health.assert_called_once_with(
            args,
            timeout_s=cli.DEFAULT_STARTUP_HEALTH_TIMEOUT_S,
            expected_pid=4321,
        )
        popen.return_value.terminate.assert_called_once()
        launch.assert_not_called()

    def test_start_health_probe_fails_fast_when_expected_process_exits(self) -> None:
        args = argparse.Namespace(host="127.0.0.1", port=8765)
        with patch.object(cli, "_pid_alive", return_value=False), \
             patch.object(cli, "urlopen") as urlopen:
            self.assertFalse(cli._restart_health_ok(args, timeout_s=10, expected_pid=4321))
        urlopen.assert_not_called()

    def test_startup_health_timeout_is_bounded_and_configurable(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_STARTUP_HEALTH_TIMEOUT_S": "2.5"}, clear=False):
            self.assertEqual(2.5, cli._startup_health_timeout_s())
        with patch.dict(os.environ, {"MAC_MCP_STARTUP_HEALTH_TIMEOUT_S": "120"}, clear=False):
            self.assertEqual(60.0, cli._startup_health_timeout_s())
        with patch.dict(os.environ, {"MAC_MCP_STARTUP_HEALTH_TIMEOUT_S": "invalid"}, clear=False):
            self.assertEqual(cli.DEFAULT_STARTUP_HEALTH_TIMEOUT_S, cli._startup_health_timeout_s())

    def test_restart_handoff_child_requires_health_before_success(self) -> None:
        args = argparse.Namespace(timeout=1.0, host="127.0.0.1", port=8765)
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: "1"}, clear=False), \
             patch.object(cli, "_load_env"), \
             patch.object(cli, "_restart_preflight", return_value=None), \
             patch.object(cli, "_write_restart_status") as status_write, \
             patch.object(cli.time, "sleep"), \
             patch.object(cli, "stop", return_value=0), \
             patch.object(cli, "start", return_value=0), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")), \
             patch.object(cli, "_restart_health_ok", return_value=False):
            code = cli.restart(args)
        self.assertEqual(1, code)
        final = status_write.call_args_list[-1]
        self.assertEqual(("failed",), final.args)
        self.assertEqual("health", final.kwargs["stage"])
        self.assertTrue(final.kwargs["server_down"])

    def test_launchd_handoff_bootstraps_one_shot_job(self) -> None:
        args = argparse.Namespace(
            host="127.0.0.1", port=8765, timeout=5.0, reload=False,
            public_mode=None, public_url=None, cloudflare_tunnel=None,
            cloudflare_token_file=None, cloudflared_bin=None, ngrok=False,
            ngrok_domain=None, ngrok_bin=None,
        )
        with tempfile.TemporaryDirectory(prefix="mac-mcp-restart-handoff-") as td:
            root = Path(td)
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with patch.object(cli, "STATE_DIR", root), \
                 patch.object(cli, "LOG_FILE", root / "mac-mcp.log"), \
                 patch.object(cli, "PROJECT_ROOT", root), \
                 patch.object(cli, "_restart_handoff_job", side_effect=[(False, None), (True, 7777)]), \
                 patch.object(cli, "_resolve_server_identity", return_value=(4242, "pid_record")), \
                 patch.object(cli, "_launchctl_run", return_value=completed) as launchctl, \
                 patch.object(cli.time, "sleep"):
                code = cli._spawn_detached_restart(args)
                status_payload = (root / "restart-status.json").read_text(encoding="utf-8")
                with (root / "restart-handoff.plist").open("rb") as handle:
                    payload = __import__("plistlib").load(handle)
        self.assertEqual(0, code)
        launchctl.assert_called_once_with(
            "bootstrap", f"gui/{os.getuid()}", str(root / "restart-handoff.plist"),
            capture=True, timeout=5.0,
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertFalse(payload["KeepAlive"])
        self.assertIn("/usr/bin/env", payload["ProgramArguments"])
        self.assertIn(f"{cli.RESTART_HANDOFF_ENV}=1", payload["ProgramArguments"])
        self.assertIn(f"MAC_MCP_STATE_DIR={root}", payload["ProgramArguments"])
        self.assertIn(f"PYTHONPATH={root}", payload["ProgramArguments"])
        self.assertTrue(any(item.startswith(f"{cli.RESTART_REQUESTER_ENV}=") for item in payload["ProgramArguments"]))
        self.assertIn('"state": "requested"', status_payload)

    def test_restart_worker_waits_for_requester_exit_then_grace(self) -> None:
        with patch.dict(os.environ, {cli.RESTART_REQUESTER_ENV: "4242"}, clear=False), \
             patch.object(cli, "_pid_alive", side_effect=[True, False, False]), \
             patch.object(cli.time, "sleep") as sleep:
            self.assertTrue(cli._wait_for_restart_requester_exit())
        sleep.assert_any_call(0.05)
        sleep.assert_any_call(cli.RESTART_RESPONSE_GRACE_S)

    def test_restart_handoff_job_parses_live_launchd_pid(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="gui/501/com.macmcp.restart-handoff = {\n\tstate = running\n\tpid = 7777\n}\n",
            stderr="",
        )
        with patch.object(cli, "_launchctl_run", return_value=completed):
            loaded, pid = cli._restart_handoff_job()
        self.assertTrue(loaded)
        self.assertEqual(7777, pid)

    def test_active_launchd_handoff_is_idempotent(self) -> None:
        args = argparse.Namespace(timeout=1.0)
        with patch.object(cli, "_restart_handoff_job", return_value=(True, 7777)), \
             patch.object(cli, "_pid_alive", return_value=True), \
             patch.object(cli.subprocess, "run") as run:
            code = cli._spawn_detached_restart(args)
        self.assertEqual(0, code)
        run.assert_not_called()

    def test_stale_launchd_handoff_is_removed_before_submit(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory(prefix="mac-mcp-restart-stale-") as td:
            root = Path(td)
            plist = root / "restart-handoff.plist"
            plist.write_text("stale", encoding="utf-8")
            with patch.object(cli, "STATE_DIR", root), \
                 patch.object(cli, "_restart_handoff_job", return_value=(True, None)), \
                 patch.object(cli, "_launchctl_run", return_value=completed) as launchctl:
                self.assertTrue(cli._remove_stale_restart_handoff())
                self.assertFalse(plist.exists())
        launchctl.assert_called_once_with(
            "bootout", cli._launchctl_target(cli.RESTART_HANDOFF_LABEL),
            capture=True, timeout=3.0,
        )

    def test_cloudflare_launchd_role_mismatch_is_not_booted_out(self) -> None:
        with patch.object(cli, "_launchctl_pid", return_value=7777),              patch.object(cli, "_cloudflare_launchd_identity", return_value=(None, "role_mismatch")),              patch.object(cli, "_bootout_cloudflare_launchd") as bootout:
            ok = cli._stop_cloudflare(0.1, True)
        self.assertFalse(ok)
        bootout.assert_not_called()

    def test_updater_refuses_mismatched_pid_without_signal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-update-pid-") as td:
            root = Path(td)
            state = root / ".mac-mcp"
            state.mkdir()
            (state / "mac-mcp.pid").write_text("{}\n", encoding="utf-8")
            runtime = root / "runtime"
            runtime.mkdir()
            mismatch = ProcessValidation(
                "identity_mismatch", 9090, "server", "json",
                "process_fingerprint_mismatch",
            )
            with patch.object(update_helper.Path, "home", return_value=root),                  patch.object(update_helper, "validate_process_record", return_value=mismatch),                  patch.object(update_helper.os, "kill") as kill:
                with self.assertRaises(update_helper.UpdateError):
                    update_helper._restart_cli(runtime, "127.0.0.1", 8765)
            kill.assert_not_called()

    def test_doctor_reports_foreign_listener_as_failure(self) -> None:
        with patch("mcp_server.diagnostics.validate_process_record") as validate,              patch("mcp_server.diagnostics._launchctl_pid", return_value=None),              patch("mcp_server.diagnostics.listener_pids", return_value=[5151]),              patch("mcp_server.diagnostics.process_snapshot", return_value=ProcessSnapshot(
                 5151,
                 "Tue Sep 29 12:00:00 2026",
                 "/usr/bin/python3",
                 "/usr/bin/python3 -m http.server 8765",
                 "/tmp",
             )),              patch("mcp_server.diagnostics._local_host_port", return_value=("127.0.0.1", 8765)),              patch("mcp_server.diagnostics.state_dir", return_value=Path("/nonexistent/mac-mcp-test-state")):
            row = diagnostics._check_managed_process("server")
        validate.assert_not_called()
        self.assertEqual("fail", row.status)
        self.assertEqual("SERVER_PORT_FOREIGN_LISTENER", row.reason_code)


if __name__ == "__main__":
    unittest.main()
