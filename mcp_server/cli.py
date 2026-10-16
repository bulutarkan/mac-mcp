from __future__ import annotations

import argparse
import json
import os
import pwd
import fcntl
import plistlib
import re
import signal
import shutil
import subprocess
import sys
import textwrap
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional
from urllib.error import URLError
from types import SimpleNamespace
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen

from dotenv import load_dotenv

from . import paths
from .cli_style import INDENT, Style, ThemedArgumentParser, home_path
from .security import dashboard_token_path, load_settings, validate_bootstrap_security
from .public_endpoint import (
    PublicEndpointError,
    inspect_cloudflare_credential,
    public_health_url,
    remove_cloudflare_token,
    resolve_public_endpoint,
    write_cloudflare_token,
)
from .runtime_settings import server_setting, update_runtime_setting
from .managed_process import (
    listener_owner,
    listener_pids,
    matches_role,
    migrate_legacy_record,
    pid_alive,
    port_conflict_advice,
    port_is_listening,
    process_snapshot,
    read_pid,
    read_process_record,
    validate_process_record,
    write_process_record,
)
from .runtime_resolver import (
    ngrok_http_endpoint_flag,
    resolve_cloudflared_binary,
    resolve_ngrok_binary,
)
from .update_helper import (
    UpdateError, check_update, format_check, format_check_json,
    resolve_paths as resolve_update_paths, secure_bootstrap_update_blocker,
    validate_update_state,
)
from . import supervisor
from .venv_repair import inspect_venv
from .tools_update import launch_detached_update
from .install_kind import install_kind, package_update_payload
from .cli_bootstrap import default_cli_path
from .update_state import UpdateInProgress
from .log_retention import log_limits, managed_logs, rotate_copy_truncate, tail_log, update_logs_dir
from .version import __version__
from .connection_config import (
    ConnectionConfigError,
    DEFAULT_AUTH_ENV,
    DEFAULT_SERVER_NAME,
    SUPPORTED_CLIENTS,
    UNGUIDED_CLIENTS,
    SUPPORTED_ENDPOINTS,
    render_connection_config,
)

APP_MODULE = "mcp_server.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "mcp_server" / ".env"
STATE_DIR = paths.state_dir()
PID_FILE = STATE_DIR / "mac-mcp.pid"
NGROK_PID_FILE = STATE_DIR / "ngrok.pid"
CLOUDFLARE_PID_FILE = STATE_DIR / "cloudflared.pid"
LOG_FILE = STATE_DIR / "mac-mcp.log"
NGROK_LOG_FILE = STATE_DIR / "ngrok.log"
CLOUDFLARE_LOG_FILE = STATE_DIR / "cloudflared.log"
CLOUDFLARE_LAUNCHD_LABEL = os.getenv("MAC_MCP_CLOUDFLARE_LAUNCHD_LABEL", "mac-mcp-cloudflared")
RESTART_HANDOFF_ENV = "MAC_MCP_RESTART_HANDOFF_CHILD"
RESTART_REQUESTER_ENV = "MAC_MCP_RESTART_REQUESTER_PID"
RESTART_HANDOFF_LABEL = "com.macmcp.restart-handoff"
RESTART_REQUESTER_WAIT_S = 5.0
RESTART_RESPONSE_GRACE_S = 3.0
RESTART_RECOVERY_DELAY_S = 2.0
# Kept well under the stop/restart wait (STOP_TIMEOUT_S): once uvicorn stops listening, the exiting
# process can no longer be verified as the server, so the wait must outlast the whole shutdown.
GRACEFUL_SHUTDOWN_S = 3
STOP_TIMEOUT_S = 10.0
RESTART_FINAL_STATES = frozenset({"succeeded", "failed", "degraded"})
DEFAULT_STARTUP_HEALTH_TIMEOUT_S = 10.0


def _load_env() -> None:
    load_dotenv(ENV_FILE)


def _startup_health_timeout_s() -> float:
    raw = os.getenv("MAC_MCP_STARTUP_HEALTH_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_STARTUP_HEALTH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STARTUP_HEALTH_TIMEOUT_S
    return max(1.0, min(value, 60.0))


def _menu_app_candidates() -> list[Path]:
    configured = os.getenv("MAC_MCP_MENU_APP", "").strip()
    items = [Path(configured).expanduser()] if configured else []
    items += [Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")]
    return items


def _launch_menu_app() -> None:
    if os.getenv("MAC_MCP_SKIP_MENU_APP", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    app = next((item for item in _menu_app_candidates() if item.exists()), None)
    if app is None:
        return
    try:
        command = ["/usr/bin/open", "-g"]
        for name in ("MAC_MCP_SETTINGS_PATH", "MAC_MCP_STATE_DIR", "MAC_MCP_DASHBOARD_TOKEN_FILE", "MAC_MCP_CLI_PATH", "MAC_MCP_PORT", "MAC_MCP_PUBLIC_ENDPOINT_MODE", "MAC_MCP_PUBLIC_URL", "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE", "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT"):
            value = os.getenv(name)
            if value is not None:
                command.extend(["--env", f"{name}={value}"])
        command.append(str(app))
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _read_pid(path: Path) -> int | None:
    return read_pid(path)


def _remove_stale_pid(path: Path) -> None:
    """Compatibility helper: only remove definitely dead/invalid PID state."""
    pid = _read_pid(path)
    if pid is None or not _pid_alive(pid):
        path.unlink(missing_ok=True)


def _role_for_name(name: str) -> str:
    return {
        "mac-mcp": "server",
        "server": "server",
        "ngrok": "ngrok",
        "cloudflared": "cloudflared",
        "cloudflare": "cloudflared",
    }.get(str(name or "").strip().lower(), str(name or "").strip().lower())


def _validate_managed_pid(
    path: Path,
    name: str,
    *,
    port: int | None = None,
    binary: str | Path | None = None,
    migrate_legacy: bool = True,
):
    role = _role_for_name(name)
    if port is None:
        record = read_process_record(path)
        recorded_port = (record.metadata or {}).get("port") if record is not None else None
        try:
            effective_port = int(recorded_port)
        except (TypeError, ValueError):
            effective_port = _default_port()
    else:
        effective_port = int(port)
    kwargs = {
        "port": effective_port,
        "project_root": PROJECT_ROOT if role == "server" else None,
        "binary": binary,
    }
    validation = validate_process_record(path, role, **kwargs)
    if migrate_legacy and validation.legacy_match:
        validation = migrate_legacy_record(
            path,
            role,
            metadata={"port": effective_port, "migrated_from": "legacy_pid"},
            **kwargs,
        )
    return validation


def _stop_pid(
    path: Path,
    name: str,
    timeout: float,
    force: bool,
    *,
    port: int | None = None,
    binary: str | Path | None = None,
) -> bool:
    validation = _validate_managed_pid(path, name, port=port, binary=binary)
    if validation.status in {"missing", "dead", "invalid_record"}:
        path.unlink(missing_ok=True)
        print(f"{name} is not running.")
        return True

    if not validation.valid or validation.pid is None:
        if validation.safe_to_remove_record:
            path.unlink(missing_ok=True)
        print(
            f"Refusing to signal {name}: recorded PID ownership could not be verified "
            f"({validation.reason})."
        )
        return False

    pid = validation.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        print(f"{name} stopped.")
        return True
    except PermissionError:
        print(f"Refusing to signal {name}: permission denied for verified pid {pid}.")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _validate_managed_pid(
            path, name, port=port, binary=binary, migrate_legacy=False,
        )
        if current.status in {"missing", "dead"}:
            path.unlink(missing_ok=True)
            print(f"{name} stopped.")
            return True
        if current.status in {"identity_mismatch", "role_mismatch"}:
            # The original process exited and the PID was reused. Never signal the replacement.
            path.unlink(missing_ok=True)
            print(f"{name} stopped; PID {pid} was reused by another process.")
            return True
        if current.status == "unverifiable":
            # An exiting process briefly stays alive with no readable metadata. Never
            # signal it again, but keep waiting for it to disappear.
            time.sleep(0.2)
            continue
        time.sleep(0.2)

    if force:
        current = _validate_managed_pid(
            path, name, port=port, binary=binary, migrate_legacy=False,
        )
        if current.valid and current.pid == pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"Refusing to force-stop {name}: permission denied for verified pid {pid}.")
                return False
            path.unlink(missing_ok=True)
            print(f"{name} force-stopped.")
            return True
        if current.status in {"missing", "dead", "identity_mismatch", "role_mismatch"}:
            path.unlink(missing_ok=True)
            print(f"{name} stopped without signaling a reused PID.")
            return True
        print(f"Refusing to force-stop {name}: process identity is not verifiable.")
        return False

    print(f"{name} did not stop within {timeout}s. Run: mac-mcp stop --force")
    return False


def _local_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _server_listener_state(port: int) -> tuple[list[int], list[int]]:
    owned: list[int] = []
    foreign: list[int] = []
    for pid in listener_pids(int(port)):
        snapshot = process_snapshot(pid)
        if snapshot and matches_role(
            snapshot,
            "server",
            port=int(port),
            project_root=PROJECT_ROOT,
        ):
            owned.append(pid)
        else:
            foreign.append(pid)

    # If PID discovery is unavailable/empty but the TCP port is occupied, we
    # still fail closed. A positive socket probe proves only occupancy, never
    # ownership; 0 is an internal sentinel for "foreign listener, PID unknown".
    if not owned and not foreign and port_is_listening(int(port)):
        foreign.append(0)
    return owned, foreign


def _server_listener_pid(port: int) -> int | None:
    owned, foreign = _server_listener_state(port)
    if foreign or len(owned) != 1:
        return None
    return owned[0]


def _server_listener_conflicts(port: int) -> list[int]:
    _owned, foreign = _server_listener_state(port)
    return foreign


def _adopt_server_listener(port: int) -> int | None:
    owned, foreign = _server_listener_state(port)
    if foreign or len(owned) != 1:
        return None
    pid = owned[0]
    snapshot = process_snapshot(pid)
    if snapshot is None:
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        write_process_record(
            PID_FILE,
            "server",
            pid,
            metadata={"port": int(port), "ownership_source": "verified_listener"},
            snapshot=snapshot,
        )
    except (OSError, RuntimeError):
        return None
    return pid


def _resolve_server_identity(
    port: int,
    *,
    adopt_listener: bool = True,
) -> tuple[int | None, str]:
    # A fingerprinted managed server remains owned even if the configured port
    # changed after it was started. Validate against the record's captured port.
    validation = _validate_managed_pid(PID_FILE, "mac-mcp")
    if validation.valid and validation.pid is not None:
        return validation.pid, "pid_record"
    if validation.status == "unverifiable":
        return None, "pid_unverifiable"
    if validation.safe_to_remove_record:
        PID_FILE.unlink(missing_ok=True)

    owned, foreign = _server_listener_state(int(port))
    if foreign:
        return None, "foreign_listener"
    if len(owned) > 1:
        return None, "ambiguous_listener"
    if len(owned) == 1:
        if not adopt_listener:
            return owned[0], "verified_listener"
        adopted = _adopt_server_listener(int(port))
        return (adopted, "adopted_listener") if adopted else (None, "adoption_failed")
    return None, "not_running"


def _start_server(args: argparse.Namespace) -> int:
    """Start (or adopt) the server under a start lock, so concurrent starts yield one server."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_DIR / "server-start.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _startup_health_timeout_s() + 15
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("Another mac-mcp start is still in progress; not starting a second server.")
                    return 1
                time.sleep(0.2)
        code = _start_server_locked(args)
    finally:
        os.close(fd)
    if code == 0:
        _ensure_supervisor()
    return code


def _server_command(args: argparse.Namespace, port: int) -> list[str]:
    cmd = [
        sys.executable, "-m", "uvicorn", APP_MODULE, "--host", args.host, "--port", str(port),
        # Open MCP streams would otherwise hold a stop or restart open indefinitely.
        "--timeout-graceful-shutdown", str(GRACEFUL_SHUTDOWN_S),
    ]
    if args.reload:
        cmd.append("--reload")
    return cmd


def _start_server_locked(args: argparse.Namespace) -> int:
    port = int(args.port)

    validation = _validate_managed_pid(PID_FILE, "mac-mcp")
    if validation.valid and validation.pid is not None:
        record = read_process_record(PID_FILE)
        recorded_port = (record.metadata or {}).get("port") if record is not None else None
        try:
            owned_port = int(recorded_port)
        except (TypeError, ValueError):
            owned_port = port
        if owned_port != port:
            print(
                f"mac-mcp is already running as a verified managed process on port {owned_port} "
                f"(pid {validation.pid}); refusing to start a second server on port {port}. "
                "Stop or restart Mac MCP first."
            )
            return 1
        if not _restart_health_ok(
            args,
            timeout_s=_startup_health_timeout_s(),
            expected_pid=validation.pid,
        ):
            print(f"mac-mcp process {validation.pid} is owned but not ready. See log: {LOG_FILE}")
            return 1
        print(f"mac-mcp is already running (pid {validation.pid}; identity verified and healthy).")
        _launch_menu_app()
        return 0
    if validation.status == "unverifiable":
        print(
            f"Cannot verify recorded mac-mcp pid {validation.pid}; refusing to start or overwrite ownership state."
        )
        return 1
    if validation.safe_to_remove_record:
        PID_FILE.unlink(missing_ok=True)

    owned, foreign = _server_listener_state(port)
    if foreign:
        owners = [listener_owner(pid) for pid in foreign if pid > 0]
        print(f"Port {port} is already in use, so mac-mcp did not start.")
        print(port_conflict_advice(port, owners))
        return 1
    if len(owned) > 1:
        print(
            f"Port {port} has multiple mac-mcp-like listeners; refusing ambiguous ownership."
        )
        return 1
    if owned:
        pid = _adopt_server_listener(port)
        if not pid:
            print("Verified mac-mcp listener could not be recorded safely; refusing adoption.")
            return 1
        if not _restart_health_ok(
            args,
            timeout_s=_startup_health_timeout_s(),
            expected_pid=pid,
        ):
            print(f"mac-mcp listener {pid} was adopted but is not ready. See log: {LOG_FILE}")
            return 1
        print(f"mac-mcp is already running (pid {pid}; verified listener adopted and healthy).")
        _launch_menu_app()
        return 0

    env = os.environ.copy()
    # Restart handoff/requester identity belongs only to the one-shot lifecycle
    # worker. Never leak it into the long-lived uvicorn process.
    env.pop(RESTART_HANDOFF_ENV, None)
    env.pop(RESTART_REQUESTER_ENV, None)
    env.setdefault("MAC_MCP_HOST", args.host)
    env.setdefault("MAC_MCP_PORT", str(port))
    # Only the CLI-managed server rotates the shared logs in the background.
    env["MAC_MCP_MANAGED_SERVER"] = "1"
    cmd = _server_command(args, port)

    # Rotate the file this start is about to append to (the module path, so tests
    # that point LOG_FILE elsewhere never touch the real log).
    rotate_copy_truncate(LOG_FILE)
    helper = _server_helper_for_start(args)
    if helper is not None:
        return _start_server_via_helper(args, port, helper, cmd, env)
    # The child keeps its own copy of the log descriptor; close ours right away.
    with LOG_FILE.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(PROJECT_ROOT),
            start_new_session=True,
        )
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"mac-mcp failed to start. See log: {LOG_FILE}")
        PID_FILE.unlink(missing_ok=True)
        return proc.returncode or 1

    snapshot = process_snapshot(proc.pid)
    if snapshot is None or not matches_role(
        snapshot, "server", port=port, project_root=PROJECT_ROOT,
    ):
        try:
            proc.terminate()
        except OSError:
            pass
        print("mac-mcp started a process whose identity could not be verified; it was not adopted.")
        return 1
    try:
        write_process_record(
            PID_FILE,
            "server",
            proc.pid,
            metadata={"port": port, "ownership_source": "spawn"},
            snapshot=snapshot,
        )
    except (OSError, RuntimeError):
        try:
            proc.terminate()
        except OSError:
            pass
        print("mac-mcp could not persist verified process identity; started process was terminated.")
        return 1

    if not _restart_health_ok(
        args,
        timeout_s=_startup_health_timeout_s(),
        expected_pid=proc.pid,
    ):
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        PID_FILE.unlink(missing_ok=True)
        print(f"mac-mcp process started but did not become ready. See log: {LOG_FILE}")
        return 1

    print(f"mac-mcp started on {_local_url(args.host, port)} (pid {proc.pid}; health verified).")
    print("dashboard: run 'mac-mcp dashboard' for an authenticated local launch")
    print(f"mac-mcp log: {LOG_FILE}")
    _launch_menu_app()
    return 0


SUPERVISOR_LABEL = "com.macmcp.supervisor"
SUPERVISOR_INTERVAL_S = 30
# The account's real home, not $HOME: an isolated install run with a temporary HOME must not
# count as the default install, whose launchd jobs and ~/Applications it would share.
DEFAULT_STATE_DIR = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".mac-mcp"


def _supervisor_enabled() -> bool:
    return os.getenv("MAC_MCP_SUPERVISOR", "1").strip().lower() not in {"0", "false", "no", "off"}


def _record_intent(desired: str, args: argparse.Namespace | None) -> None:
    try:
        supervisor.write_intent(
            desired, start_args=_lifecycle_flags(args) if args is not None else None, root=STATE_DIR,
        )
    except OSError as exc:
        print(f"Warning: could not record the server intent ({type(exc).__name__}).")


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _start_at_login() -> bool:
    """Start Mac MCP at login (settings server.start_at_login, on unless set to false)."""
    return server_setting("start_at_login", True) is not False


def _supervisor_plist_path() -> Path:
    # In ~/Library/LaunchAgents launchd loads the job at login, so a server the owner left
    # running comes back after a restart; in the state folder it only runs once loaded.
    if _start_at_login():
        return _launch_agents_dir() / f"{SUPERVISOR_LABEL}.plist"
    return STATE_DIR / "supervisor.plist"


def _stale_supervisor_plists() -> list[Path]:
    current = _supervisor_plist_path()
    candidates = (STATE_DIR / "supervisor.plist", _launch_agents_dir() / f"{SUPERVISOR_LABEL}.plist")
    return [path for path in candidates if path != current and path.exists()]


def _supervisor_payload() -> dict:
    return {
        "Label": SUPERVISOR_LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            f"MAC_MCP_STATE_DIR={STATE_DIR}",
            f"PYTHONPATH={PROJECT_ROOT}",
            f"PATH={os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}",
            sys.executable, "-m", "mcp_server.supervisor",
        ],
        "StartInterval": SUPERVISOR_INTERVAL_S,
        "RunAtLoad": _start_at_login(),
        "ProcessType": "Background",
        "WorkingDirectory": str(PROJECT_ROOT),
        "StandardOutPath": str(STATE_DIR / "supervisor.log"),
        "StandardErrorPath": str(STATE_DIR / "supervisor.log"),
        "Umask": 0o077,
    }


def _supervisor_loaded() -> bool:
    try:
        return _launchctl_run("print", _launchctl_target(SUPERVISOR_LABEL), capture=True, timeout=3.0).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _ensure_supervisor() -> None:
    """Load the crash supervisor for the default installation (idempotent).

    With start at login on (the default) the job lives in ~/Library/LaunchAgents and
    runs at login, where it starts a server the owner left running; with it off the
    job lives in the state folder and only `mac-mcp start` loads it.
    """
    if STATE_DIR.resolve() != DEFAULT_STATE_DIR.resolve():
        return
    if not _supervisor_enabled():
        if _supervisor_loaded():
            _launchctl_run("bootout", _launchctl_target(SUPERVISOR_LABEL), capture=True)
        return
    payload = _supervisor_payload()
    path = _supervisor_plist_path()
    stale = _stale_supervisor_plists()
    try:
        current = plistlib.loads(path.read_bytes()) if path.exists() else None
    except (OSError, plistlib.InvalidFileException):
        current = None
    loaded = _supervisor_loaded()
    if loaded and current == payload and not stale:
        return
    try:
        if loaded:
            _launchctl_run("bootout", _launchctl_target(SUPERVISOR_LABEL), capture=True)
        for old in stale:
            old.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with tmp.open("wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        proc = _launchctl_run("bootstrap", f"gui/{os.getuid()}", str(path), capture=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Warning: crash supervisor could not be loaded ({type(exc).__name__}).")
        return
    if proc.returncode != 0:
        print(f"Warning: crash supervisor could not be loaded: {(proc.stderr or proc.stdout or '').strip()}")


def _server_helper_for_start(args: argparse.Namespace) -> Path | None:
    """Mac MCP Server, when this start should run under it (default install only).

    Isolated installs and --reload keep the direct spawn; MAC_MCP_SERVER_HELPER=0 turns
    the helper off. A missing or unsigned helper also means a direct spawn.
    """
    from . import server_helper

    if getattr(args, "reload", False) or STATE_DIR.resolve() != DEFAULT_STATE_DIR.resolve():
        return None
    if os.getenv("MAC_MCP_SERVER_HELPER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    return server_helper.helper_executable(PROJECT_ROOT)


def _server_job_plist_path() -> Path:
    return STATE_DIR / "server.plist"


def _unload_server_job() -> None:
    from . import server_helper

    _launchctl_run("bootout", _launchctl_target(server_helper.SERVER_LABEL), capture=True)
    _server_job_plist_path().unlink(missing_ok=True)


def _start_server_via_helper(
    args: argparse.Namespace, port: int, helper: Path, cmd: list[str], env: dict[str, str],
) -> int:
    """Start the server as a launchd job under Mac MCP Server and record its Python child."""
    from . import server_helper

    payload = server_helper.server_job_payload(
        helper=helper, command=cmd, env=server_helper.job_environment(env), cwd=PROJECT_ROOT, log=LOG_FILE,
    )
    path = _server_job_plist_path()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _launchctl_run("bootout", _launchctl_target(server_helper.SERVER_LABEL), capture=True)
    proc = _launchctl_run("bootstrap", f"gui/{os.getuid()}", str(path), capture=True)
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        print(f"mac-mcp could not start under Mac MCP Server: {(proc.stderr or proc.stdout or '').strip()}")
        return 1

    deadline = time.time() + _startup_health_timeout_s()
    child = None
    while time.time() < deadline:
        helper_pid = _launchctl_pid(server_helper.SERVER_LABEL)
        if helper_pid:
            child = server_helper.server_child_pid(helper_pid, port)
            if child:
                break
        time.sleep(0.25)
    snapshot = process_snapshot(child) if child else None
    if snapshot is None or not matches_role(snapshot, "server", port=port, project_root=PROJECT_ROOT):
        _unload_server_job()
        print(f"mac-mcp did not come up under Mac MCP Server. See log: {LOG_FILE}")
        return 1
    try:
        write_process_record(
            PID_FILE, "server", child, metadata={"port": port, "ownership_source": "helper"}, snapshot=snapshot,
        )
    except (OSError, RuntimeError):
        _unload_server_job()
        print("mac-mcp could not persist verified process identity; started process was stopped.")
        return 1
    if not _restart_health_ok(args, timeout_s=_startup_health_timeout_s(), expected_pid=child):
        _unload_server_job()
        PID_FILE.unlink(missing_ok=True)
        print(f"mac-mcp process started but did not become ready. See log: {LOG_FILE}")
        return 1
    print(f"mac-mcp started on {_local_url(args.host, port)} (pid {child}; health verified).")
    print("macOS permissions belong to Mac MCP (the server runs under Mac MCP Server).")
    print("dashboard: run 'mac-mcp dashboard' for an authenticated local launch")
    print(f"mac-mcp log: {LOG_FILE}")
    _launch_menu_app()
    return 0


def autostart(args: argparse.Namespace) -> int:
    desired = getattr(args, "state", None)
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if desired in {"on", "off"}:
        try:
            update_runtime_setting("server", "start_at_login", desired == "on")
        except RuntimeError as exc:
            if style is not None:
                from .cli_views import render_error
                render_error(style, "autostart", f"Could not save the setting: {exc}")
                return 1
            print(f"mac-mcp autostart: could not save the setting: {exc}", file=sys.stderr)
            return 1
        _ensure_supervisor()
    report = {
        "start_at_login": _start_at_login(),
        "job": str(_supervisor_plist_path()),
        "loaded": _supervisor_loaded(),
    }
    if style is not None:
        from .cli_views import render_autostart
        render_autostart(style, report["start_at_login"])
        return 0
    if getattr(args, "json", False):
        print(json.dumps(report, sort_keys=True))
    elif report["start_at_login"]:
        print("Start at login: on (a server you left running starts again when you log in).")
    else:
        print("Start at login: off (run 'mac-mcp start' or use the menu bar app after you log in).")
    return 0


def _uninstall_app_path() -> Path:
    configured = os.getenv("MAC_MCP_MENU_APP", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Applications" / "Mac MCP.app"


def _uninstall_source_path() -> Path:
    return Path(os.getenv("MAC_MCP_SOURCE_DIR", str(Path.home() / "Projects" / "mac-mcp"))).expanduser()


def _trash_root() -> Path:
    return Path.home() / ".Trash"


def _stop_for_uninstall(app: Path) -> None:
    try:
        # The plain stop: uninstall prints its own report, without a second header in it.
        _stop_plain(argparse.Namespace(timeout=10, force=True, keep_intent=False))
    except Exception as exc:  # keep going: the parts are moved to the Trash either way
        print(f"Warning: Mac MCP did not stop cleanly ({type(exc).__name__}).")
    from .update_helper import _stop_menu_app
    try:
        _stop_menu_app(app)
    except Exception:
        pass


PACKAGE_RECEIPT_ID = "com.bulutarkan.mac-mcp.pkg"


def _forget_package_receipt() -> None:
    """A package install leaves an Installer receipt in the home domain; drop it with the install."""
    home = str(Path.home())
    try:
        listed = subprocess.run(["/usr/sbin/pkgutil", "--volume", home, "--pkgs"],
                                capture_output=True, text=True, timeout=20).stdout.split()
        if PACKAGE_RECEIPT_ID in listed:
            subprocess.run(["/usr/sbin/pkgutil", "--volume", home, "--forget", PACKAGE_RECEIPT_ID],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def uninstall(args: argparse.Namespace) -> int:
    from . import lifecycle

    app = _uninstall_app_path()
    items = lifecycle.uninstall_plan(
        runtime=PROJECT_ROOT, state=STATE_DIR, app=app, cli=default_cli_path(),
        launch_agents_dir=_launch_agents_dir(), source=_uninstall_source_path(),
        include_state=bool(getattr(args, "include_state", False)),
        include_source=bool(getattr(args, "include_source", False)),
    )
    style = Style.for_stream()
    if style is not None:
        return _uninstall_themed(args, style, items, app)
    if not getattr(args, "yes", False):
        print(lifecycle.format_plan(items))
        extra = "".join(
            f" --{name.replace('_', '-')}" for name in ("include_state", "include_source") if getattr(args, name, False)
        )
        print(f"\nNothing was changed. To do this, run: mac-mcp uninstall --yes{extra}")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash_root = _trash_root()
    summary = lifecycle.format_plan(items, trash_folder=trash_root / f"Mac MCP uninstall {stamp}")
    if STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve():
        _stop_for_uninstall(app)
        for label in (SUPERVISOR_LABEL, CLOUDFLARE_LAUNCHD_LABEL):
            _launchctl_run("bootout", _launchctl_target(label), capture=True)
    else:
        # launchd labels are shared with the default install; unloading them from here
        # would stop the owner's real server and tunnel.
        print("This is not the default install, so its server and login jobs were not stopped; stop them yourself.")
    results = lifecycle.apply_uninstall(items, trash_root=trash_root, stamp=stamp)
    _forget_package_receipt()
    failed = [row for row in results if row["result"] == "failed"]
    print(summary)
    for row in failed:
        print(f"Could not move {row['path']} ({row.get('error')}); remove it by hand if you want it gone.")
    return 1 if failed else 0


def _uninstall_command(args: argparse.Namespace) -> str:
    extra = "".join(
        f" --{name.replace('_', '-')}" for name in ("include_state", "include_source") if getattr(args, name, False)
    )
    return f"mac-mcp uninstall --yes{extra}"


def _uninstall_themed(args: argparse.Namespace, style, items: list, app: Path) -> int:
    """uninstall in a terminal. Everything the screen needs is imported first: the move takes
    away the runtime this command runs from."""
    from . import lifecycle
    from .cli_steps import Steps
    from .cli_views import UNINSTALL_NOTES, dim_lines, note_lines, render_uninstall_plan

    if not getattr(args, "yes", False):
        render_uninstall_plan(style, items)
        dim_lines(style, UNINSTALL_NOTES[1])
        style.summary("warn", "Nothing was changed", next_command=_uninstall_command(args))
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash_root = _trash_root()
    folder = trash_root / f"Mac MCP uninstall {stamp}"
    style.header("uninstall")
    steps = Steps(style)
    if STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve():
        with steps.step("Stopped Mac MCP", failed_label="Stop Mac MCP"):
            _stop_for_uninstall(app)
            for label in (SUPERVISOR_LABEL, CLOUDFLARE_LAUNCHD_LABEL):
                _launchctl_run("bootout", _launchctl_target(label), capture=True)
    else:
        # launchd labels are shared with the default install; unloading them from here
        # would stop the owner's real server and tunnel.
        note_lines(style, "This is not the default install, so its server and login jobs were not stopped; "
                          "stop them yourself.", "warn")
        style.write()
    failed: list = []
    with steps.step("Moved Mac MCP to the Trash", failed_label="Move Mac MCP to the Trash") as state:
        results = lifecycle.apply_uninstall(items, trash_root=trash_root, stamp=stamp)
        _forget_package_receipt()
        failed = [row for row in results if row["result"] == "failed"]
        moved = sum(1 for row in results if row["result"] == "moved")
        state.label = f"Moved {_plural(moved, 'part')} to the Trash"
        for row in failed:
            print(f"{home_path(row['path'])}: {row.get('error')}")
        state.ok = not failed
    style.write()
    dim_lines(style, UNINSTALL_NOTES[1])
    if failed:
        count = len(failed)
        style.summary("error", f"{_plural(count, 'part')} could not move — remove {'it' if count == 1 else 'them'} by hand")
        return 1
    style.summary("ok", "Mac MCP is removed", note=home_path(folder))
    return 0


def privacy_delete(args: argparse.Namespace) -> int:
    """Preview, or move chosen Mac MCP data stores to the Trash (Settings > Privacy)."""
    from . import privacy_inventory

    keys = list(privacy_inventory.DELETABLE) if args.all else list(args.store or [])
    unknown = [key for key in keys if key not in privacy_inventory.DELETABLE]
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if style is not None:
        return _privacy_delete_themed(args, style, keys, unknown)
    if not keys or unknown:
        print(f"Choose --all or --store from: {', '.join(privacy_inventory.DELETABLE)}", file=sys.stderr)
        return 2
    plan = privacy_inventory.delete_plan(keys)
    if not args.yes:
        for store in plan:
            size = f"{store['bytes'] / 1_048_576:.1f} MB" if store.get("exists") else "empty"
            print(f"  {store['name']}: {size} ({', '.join(store['paths'])})")
        print("\nNothing was changed. Mac MCP stops while the files move to the Trash, then starts again.")
        print(f"To do this, run: mac-mcp privacy delete {'--all' if args.all else ' '.join('--store ' + k for k in keys)} --yes")
        return 0
    default_install = STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve()
    was_running = default_install and _resolve_server_identity(_default_port(), adopt_listener=False)[0] is not None
    if was_running:
        # Open databases and running agents must not keep writing into what moves.
        stop(argparse.Namespace(timeout=STOP_TIMEOUT_S, force=True, keep_intent=True))
    result = privacy_inventory.trash_stores(keys, trash_root=_trash_root(), stamp=time.strftime("%Y%m%d-%H%M%S"))
    started = _start_again_as_before() if was_running else None
    result.update({"ok": not result["failed"], "restarted": started})
    if args.json:
        print(json.dumps(result))
    else:
        for row in result["moved"]:
            print(f"Moved {row['path']} -> {row['to']}")
        for row in result["failed"]:
            print(f"Could not move {row['path']} ({row['error']})")
    return 0 if result["ok"] else 1


def _privacy_delete_themed(args: argparse.Namespace, style, keys: list, unknown: list) -> int:
    """privacy delete in a terminal: a preview that changes nothing, or the move as steps."""
    from . import privacy_inventory
    from .cli_steps import Steps
    from .cli_views import render_error

    if not keys or unknown:
        render_error(style, "privacy delete", f"Choose --all or --store from: {', '.join(privacy_inventory.DELETABLE)}")
        return 2
    plan = privacy_inventory.delete_plan(keys)
    command = f"mac-mcp privacy delete {'--all' if args.all else ' '.join('--store ' + k for k in keys)} --yes"
    if not args.yes:
        style.header("privacy delete")
        style.rows([(None, store["name"], f"{store['bytes'] / 1_048_576:.1f} MB" if store.get("exists") else "empty",
                     ", ".join(home_path(path) for path in store["paths"])) for store in plan])
        style.write()
        for line in textwrap.wrap("Mac MCP stops while these move to the Trash, then starts again.", style.width - len(INDENT)):
            style.write(INDENT + style.paint(line, dim=True))
        style.summary("warn", "Nothing was changed", next_command=command)
        return 0
    style.header("privacy delete")
    steps = Steps(style)
    default_install = STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve()
    was_running = default_install and _resolve_server_identity(_default_port(), adopt_listener=False)[0] is not None
    if was_running:
        # Open databases and running agents must not keep writing into what moves.
        with steps.step("Stopped Mac MCP", failed_label="Stop Mac MCP") as state:
            state.ok = _stop_plain(argparse.Namespace(timeout=STOP_TIMEOUT_S, force=True, keep_intent=True)) == 0
    result = {"moved": [], "failed": []}
    with steps.step("Moved the stores to the Trash", failed_label="Move the stores to the Trash") as state:
        result = privacy_inventory.trash_stores(keys, trash_root=_trash_root(), stamp=time.strftime("%Y%m%d-%H%M%S"))
        # One row per moved path; stores are what was asked for (backups move file by file).
        moved_keys = list(dict.fromkeys(str(row.get("store") or row["path"]) for row in result["moved"]))
        moved = len(moved_keys)
        state.label = f"Moved {moved} store{'s' if moved != 1 else ''} to the Trash"
        for row in result["failed"]:
            print(f"{home_path(row['path'])}: {row['error']}")
        state.ok = not result["failed"]
    started = True
    if was_running:
        with steps.step("Started Mac MCP again", failed_label="Start Mac MCP again") as start_state:
            started = start_state.ok = _start_again_as_before()
    if result["failed"]:
        style.summary("error", f"{len(result['failed'])} could not move", next_command=command)
        return 1
    names = {store["key"]: store["name"] for store in plan}
    moved_names = ", ".join(names.get(key, key) for key in moved_keys) or "nothing"
    folder = home_path(result["folder"]) if result.get("folder") else ""
    if not started:
        style.summary("warn", "Moved to the Trash, but Mac MCP did not start again", next_command="mac-mcp start")
        return 0
    style.summary("ok", f"Moved to the Trash: {moved_names}", note=folder)
    return 0


def _start_again_as_before() -> bool:
    """Start the server with the flags it last ran with, the way the supervisor does."""
    start_args = [str(item) for item in supervisor.read_intent(STATE_DIR).get("start_args") or []]
    proc = subprocess.run([sys.executable, "-m", "mcp_server.cli", "start", *start_args],
                          cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=180, check=False)
    return proc.returncode == 0


def repair(args: argparse.Namespace) -> int:
    from . import lifecycle
    from .cli_bootstrap import ensure_cli_launcher

    def checks():
        return lifecycle.repair_checks(
            runtime=PROJECT_ROOT, cli=default_cli_path(), app=_uninstall_app_path(),
            supervisor_job=_supervisor_plist_path(),
            # Only the default install has the login job (see _ensure_supervisor).
            start_at_login=_start_at_login() and STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve(),
        )

    def show(rows) -> None:
        for row in rows:
            mark = "ok " if row.ok else "fix"
            print(f"  [{mark}] {row.label}: {row.detail}" + ("" if row.ok else f" -> {row.fix}"))

    style = Style.for_stream()
    if style is not None:
        return _repair_themed(args, style, checks)
    found = checks()
    show(found)
    broken = [row for row in found if not row.ok]
    if not broken:
        print("Nothing to repair.")
        return 0
    if not getattr(args, "yes", False):
        print("Nothing was changed. To repair these, run: mac-mcp repair --yes")
        return 0
    for row in broken:
        if row.key == "venv":
            from .venv_repair import repair as repair_venv
            repair_venv(PROJECT_ROOT)
        elif row.key == "cli":
            ensure_cli_launcher(PROJECT_ROOT, default_cli_path(), strict=True)
        elif row.key == "supervisor":
            _ensure_supervisor()
        elif row.key == "app":
            installer = PROJECT_ROOT / "menu_app" / "install_app.sh"
            if STATE_DIR.resolve() != DEFAULT_STATE_DIR.resolve():
                print("Not the default install: the menu bar app is not reinstalled from here.")
            elif installer.is_file() and shutil.which("xcrun") and subprocess.run(
                ["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True).returncode == 0:
                subprocess.run([str(installer)], check=False)
            else:
                print("The menu bar app cannot be rebuilt here; install the latest package from "
                      "https://github.com/bulutarkan/mac-mcp/releases to put it back.")
    after = checks()
    print("After repair:")
    show(after)
    return 0 if all(row.ok for row in after) else 1


_REPAIR_STEPS = {
    "venv": ("Rebuilt the Python environment", "Rebuild the Python environment"),
    "cli": ("Recreated the mac-mcp command", "Recreate the mac-mcp command"),
    "supervisor": ("Reinstalled the start-at-login job", "Reinstall the start-at-login job"),
    "app": ("Reinstalled the menu bar app", "Reinstall the menu bar app"),
}


def _repair_rows(rows) -> list:
    return [("ok" if row.ok else "error", row.label, home_path(row.detail), "" if row.ok else f"fix: {row.fix}")
            for row in rows]


def _repair_one(key: str) -> bool:
    """One fix, quietly: what it prints (also on stderr, also its commands) stays in the step."""
    import contextlib

    with contextlib.redirect_stderr(sys.stdout):
        if key == "venv":
            from .venv_repair import repair as repair_venv
            try:
                return repair_venv(PROJECT_ROOT, quiet=True) == 0
            except SystemExit as exc:  # no usable Python on this Mac
                print(exc)
                return False
        if key == "cli":
            from .cli_bootstrap import ensure_cli_launcher
            try:
                ensure_cli_launcher(PROJECT_ROOT, default_cli_path(), strict=True)
            except Exception as exc:  # the step shows why; the checks after say what is left
                print(f"{type(exc).__name__}: {exc}")
                return False
            return True
        if key == "supervisor":
            _ensure_supervisor()
            return True
        if key == "app":
            installer = PROJECT_ROOT / "menu_app" / "install_app.sh"
            if STATE_DIR.resolve() != DEFAULT_STATE_DIR.resolve():
                print("Not the default install: the menu bar app is not reinstalled from here.")
                return False
            if installer.is_file() and shutil.which("xcrun") and subprocess.run(
                    ["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True).returncode == 0:
                proc = subprocess.run([str(installer)], check=False, capture_output=True, text=True)
                if proc.returncode != 0:
                    print("\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-15:]))
                return proc.returncode == 0
            print("The menu bar app cannot be rebuilt here; install the latest package from "
                  "https://github.com/bulutarkan/mac-mcp/releases to put it back.")
            return False
    return False


def _repair_themed(args: argparse.Namespace, style, checks) -> int:
    from .cli_steps import Steps

    found = checks()
    broken = [row for row in found if not row.ok]
    style.header("repair")
    if not getattr(args, "yes", False) or not broken:
        style.rows(_repair_rows(found))
        if not broken:
            style.summary("ok", "Nothing to repair")
        else:
            style.summary("warn", f"{_plural(len(broken), 'part')} need{'s' if len(broken) == 1 else ''} repair",
                          next_command="mac-mcp repair --yes")
        return 0
    steps = Steps(style)
    for row in broken:
        done, running = _REPAIR_STEPS.get(row.key, (f"Repaired {row.label}", f"Repair {row.label}"))
        with steps.step(done, failed_label=running) as state:
            state.ok = _repair_one(row.key)
    after = checks()
    style.write()
    style.rows(_repair_rows(after))
    left = [row for row in after if not row.ok]
    if left:
        style.summary("error", f"{_plural(len(left), 'part')} still need{'s' if len(left) == 1 else ''} repair",
                      next_command="mac-mcp doctor")
        return 1
    # A rebuilt environment is used once the server starts on it again.
    rebuilt = any(row.key == "venv" for row in broken)
    style.summary("ok", f"Repaired {_plural(len(broken), 'part')}", next_command="mac-mcp restart" if rebuilt else None)
    return 0


def supervisor_report() -> dict:
    state = supervisor.read_state(STATE_DIR)
    intent = supervisor.read_intent(STATE_DIR)
    return {
        "enabled": _supervisor_enabled(),
        "loaded": _supervisor_loaded(),
        "intent": intent.get("desired"),
        "last_check_at": state.get("last_check_at"),
        "last_result": state.get("last_result"),
        "last_recovery": {
            key: value for key, value in (state.get("last_recovery") or {}).items() if key != "log_tail"
        } or None,
        "last_tunnel_recovery": state.get("last_tunnel_recovery"),
        "backoff_until": state.get("backoff_until") if state.get("last_result") in {"backoff", "tunnel_backoff"} else None,
    }


def _format_supervision(report: dict) -> str:
    if not report.get("enabled"):
        return "crash supervisor: disabled (MAC_MCP_SUPERVISOR=0)"
    if not report.get("loaded"):
        return "crash supervisor: not loaded (mac-mcp start loads it)"
    line = f"crash supervisor: watching (intent {report.get('intent') or 'unknown'})"
    recovery = report.get("last_recovery") or {}
    if recovery:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(recovery.get("at") or 0)))
        line += f"; last recovery {when}: {recovery.get('reason')} -> {recovery.get('result')}"
    if report.get("backoff_until"):
        line += "; backing off after repeated failures"
    return line


def _default_port() -> int:
    raw = os.getenv("MAC_MCP_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    configured = server_setting("port", int(DEFAULT_PORT))
    try:
        return int(configured)
    except (TypeError, ValueError):
        return int(DEFAULT_PORT)


def _public_endpoint_config(args: argparse.Namespace):
    return resolve_public_endpoint(
        mode_override=getattr(args, "public_mode", None),
        public_url_override=getattr(args, "public_url", None),
        force_ngrok=bool(getattr(args, "ngrok", False)),
        cloudflare_tunnel_override=getattr(args, "cloudflare_tunnel", None),
        cloudflare_token_file_override=getattr(args, "cloudflare_token_file", None),
    )


def _resolve_ngrok_binary(configured: str | None = None) -> str | None:
    return resolve_ngrok_binary(configured).path


def _resolve_cloudflared_binary(configured: str | None = None) -> str | None:
    return resolve_cloudflared_binary(configured).path


def _launchctl_binary() -> str:
    return shutil.which("launchctl") or "/bin/launchctl"


def _cloudflare_launchd_plist() -> Path:
    override = os.getenv("MAC_MCP_CLOUDFLARE_LAUNCHD_PLIST", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents" / "com.macmcp.cloudflared.plist"


def _launchctl_target(label: str | None = None) -> str:
    resolved = label or CLOUDFLARE_LAUNCHD_LABEL
    return f"gui/{os.getuid()}/{resolved}"


def _launchctl_run(*args: str, capture: bool = False, timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )
    if capture:
        kwargs.update(capture_output=True)
    else:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run([_launchctl_binary(), *args], **kwargs)


def _launchctl_pid(label: str | None = None) -> int | None:
    try:
        proc = _launchctl_run("print", _launchctl_target(label), capture=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    running = False
    pid = None
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line == "state = running":
            running = True
        elif line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
    return pid if running and pid and _pid_alive(pid) else None


def _cloudflare_launchd_identity(
    *,
    port: int | None = None,
    binary: str | Path | None = None,
) -> tuple[int | None, str]:
    pid = _launchctl_pid()
    if not pid:
        return None, "not_running"
    snapshot = process_snapshot(pid)
    if snapshot is None:
        return None, "metadata_unavailable"
    expected_port = int(port) if port is not None else None
    if not matches_role(
        snapshot,
        "cloudflared",
        port=expected_port,
        binary=binary,
    ):
        return None, "role_mismatch"
    return pid, "verified"


def _cloudflare_launchd_loaded() -> bool:
    try:
        return _launchctl_run("print", _launchctl_target(), timeout=3.0).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _write_cloudflare_launchd_plist(cmd: list[str]) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CLOUDFLARE_LOG_FILE.touch(mode=0o600, exist_ok=True)
    os.chmod(CLOUDFLARE_LOG_FILE, 0o600)
    plist_path = _cloudflare_launchd_plist()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": CLOUDFLARE_LAUNCHD_LABEL,
        "ProgramArguments": cmd,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(CLOUDFLARE_LOG_FILE),
        "StandardErrorPath": str(CLOUDFLARE_LOG_FILE),
        "Umask": 0o077,
    }
    tmp = plist_path.with_name(f".{plist_path.name}.tmp")
    with tmp.open("wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, plist_path)
    os.chmod(plist_path, 0o600)
    return plist_path


def _set_cloudflare_launchd_enabled(enabled: bool) -> bool:
    action = "enable" if enabled else "disable"
    try:
        return _launchctl_run(action, _launchctl_target()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _bootout_cloudflare_launchd() -> bool:
    if not _cloudflare_launchd_loaded():
        return True
    try:
        return _launchctl_run("bootout", _launchctl_target()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _bootstrap_cloudflare_launchd(plist_path: Path) -> tuple[bool, str]:
    try:
        proc = _launchctl_run("bootstrap", f"gui/{os.getuid()}", str(plist_path), capture=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, detail


def _wait_for_cloudflare_launchd(
    timeout: float = 8.0,
    *,
    port: int | None = None,
    binary: str | Path | None = None,
) -> int | None:
    deadline = time.time() + timeout
    effective_port = int(port if port is not None else _default_port())
    while time.time() < deadline:
        pid, identity = _cloudflare_launchd_identity(port=effective_port, binary=binary)
        if pid:
            snapshot = process_snapshot(pid)
            if snapshot is None:
                return None
            try:
                write_process_record(
                    CLOUDFLARE_PID_FILE,
                    "cloudflared",
                    pid,
                    metadata={
                        "port": effective_port,
                        "launchd_label": CLOUDFLARE_LAUNCHD_LABEL,
                        "ownership_source": "launchd",
                    },
                    snapshot=snapshot,
                )
            except (OSError, RuntimeError):
                return None
            return pid
        if identity == "role_mismatch":
            return None
        time.sleep(0.2)
    return None


def _stop_cloudflare(timeout: float, force: bool) -> bool:
    port = _default_port()
    launchd_pid = _launchctl_pid()
    if launchd_pid:
        verified_pid, identity = _cloudflare_launchd_identity()
        if not verified_pid:
            print(
                f"Refusing to stop cloudflared launchd job: process identity could not be verified "
                f"({identity})."
            )
            return False
        snapshot = process_snapshot(verified_pid)
        if snapshot is None:
            print("Refusing to stop cloudflared launchd job: process metadata is unavailable.")
            return False
        try:
            write_process_record(
                CLOUDFLARE_PID_FILE,
                "cloudflared",
                verified_pid,
                metadata={
                    "port": port,
                    "launchd_label": CLOUDFLARE_LAUNCHD_LABEL,
                    "ownership_source": "launchd",
                },
                snapshot=snapshot,
            )
        except (OSError, RuntimeError):
            print("Refusing to stop cloudflared: verified launchd identity could not be recorded.")
            return False

    launchd_ok = _bootout_cloudflare_launchd()
    disabled_ok = _set_cloudflare_launchd_enabled(False)

    manual_ok = True
    recorded_pid = _read_pid(CLOUDFLARE_PID_FILE)
    if recorded_pid and (not launchd_pid or recorded_pid != launchd_pid):
        manual_ok = _stop_pid(
            CLOUDFLARE_PID_FILE,
            "cloudflared",
            timeout,
            force,
        )

    if launchd_ok:
        deadline = time.time() + timeout
        while launchd_pid and _pid_alive(launchd_pid) and time.time() < deadline:
            time.sleep(0.2)
        # A reused PID must not be signaled; launchctl already targeted only our label.
        if launchd_pid and _pid_alive(launchd_pid):
            current = process_snapshot(launchd_pid)
            if current and matches_role(current, "cloudflared"):
                launchd_ok = False

    if launchd_ok and disabled_ok and manual_ok:
        CLOUDFLARE_PID_FILE.unlink(missing_ok=True)
        print("cloudflared stopped.")
        return True
    print("cloudflared did not stop cleanly.")
    return False


def _start_cloudflare(args: argparse.Namespace, public) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    port = int(args.port)

    launchd_pid = _launchctl_pid()
    if launchd_pid:
        verified_pid, identity = _cloudflare_launchd_identity(port=port)
        if not verified_pid:
            print(
                f"Refusing to reuse cloudflared launchd job: process identity could not be verified "
                f"({identity})."
            )
            return 1
        snapshot = process_snapshot(verified_pid)
        if snapshot is None:
            print("Refusing to reuse cloudflared launchd job: process metadata is unavailable.")
            return 1
        try:
            write_process_record(
                CLOUDFLARE_PID_FILE,
                "cloudflared",
                verified_pid,
                metadata={
                    "port": port,
                    "launchd_label": CLOUDFLARE_LAUNCHD_LABEL,
                    "ownership_source": "launchd",
                },
                snapshot=snapshot,
            )
        except (OSError, RuntimeError):
            print("Verified cloudflared launchd process could not be recorded safely.")
            return 1
        print(f"cloudflared is already running under launchd for mac-mcp (pid {verified_pid}; identity verified).")
        return 0

    cloudflared = _resolve_cloudflared_binary(getattr(args, "cloudflared_bin", None))
    if cloudflared is None:
        print("cloudflared was not found. Install it with Homebrew or set CLOUDFLARED_BIN.")
        return 2

    target = f"http://127.0.0.1:{port}"
    cmd = [cloudflared, "tunnel", "--no-autoupdate", "--loglevel", "fatal", "run", "--url", target]
    token_file = public.cloudflare_token_file
    if token_file:
        token_path = Path(token_file).expanduser()
        credential = inspect_cloudflare_credential(token_path)
        if not credential.configured:
            print("Cloudflare Tunnel credential is not configured. Open Mac MCP Settings > Advanced and save the tunnel token.")
            return 2
        if not credential.secure:
            print(f"Cloudflare Tunnel credential file is unsafe ({credential.reason}); it must be a regular owner-owned 0600 file.")
            return 2
        cmd += ["--token-file", str(token_path)]
    elif public.cloudflare_tunnel:
        cmd.append(public.cloudflare_tunnel)
    else:
        print("Cloudflare Tunnel mode is missing a tunnel name/UUID or token file.")
        return 2

    # A stale manual PID record is never signaled here. _stop_pid validates it first.
    if CLOUDFLARE_PID_FILE.exists() and not _cloudflare_launchd_loaded():
        validation = _validate_managed_pid(
            CLOUDFLARE_PID_FILE,
            "cloudflared",
            port=port,
            binary=cloudflared,
        )
        if validation.valid:
            if not _stop_pid(
                CLOUDFLARE_PID_FILE,
                "cloudflared",
                3.0,
                True,
                port=port,
                binary=cloudflared,
            ):
                return 1
        elif validation.status == "unverifiable":
            print("Refusing to replace cloudflared: existing PID ownership is unverifiable.")
            return 1
        elif validation.safe_to_remove_record:
            CLOUDFLARE_PID_FILE.unlink(missing_ok=True)

    plist_path = _write_cloudflare_launchd_plist(cmd)
    if _cloudflare_launchd_loaded():
        existing_pid, identity = _cloudflare_launchd_identity(port=port, binary=cloudflared)
        if _launchctl_pid() and not existing_pid:
            print(f"Refusing to replace cloudflared launchd job with unverified identity ({identity}).")
            return 1
        if not _bootout_cloudflare_launchd():
            print("Could not replace the existing cloudflared launchd job.")
            return 1
    if not _set_cloudflare_launchd_enabled(True):
        print("Could not enable the cloudflared launchd job.")
        return 1
    ok, detail = _bootstrap_cloudflare_launchd(plist_path)
    if not ok:
        print(f"cloudflared failed to register with launchd{': ' + detail if detail else '.'}")
        return 1
    pid = _wait_for_cloudflare_launchd(port=port, binary=cloudflared)
    if not pid:
        # Only boot out a job we can still identify by the configured label.
        verified_pid, _identity = _cloudflare_launchd_identity(port=port, binary=cloudflared)
        if verified_pid:
            _bootout_cloudflare_launchd()
        print(f"cloudflared failed to become healthy under launchd. See log: {CLOUDFLARE_LOG_FILE}")
        return 1

    print(f"Cloudflare Tunnel started under launchd: {public.endpoint_url} -> {target} (pid {pid}).")
    print(f"cloudflared log: {CLOUDFLARE_LOG_FILE}")
    return 0


def _stop_unselected_public_processes(selected_mode: str) -> None:
    if selected_mode != "ngrok" and NGROK_PID_FILE.exists():
        _stop_pid(
            NGROK_PID_FILE,
            "ngrok",
            3.0,
            True,
        )

    if selected_mode != "cloudflare" and (
        _cloudflare_launchd_loaded() or CLOUDFLARE_PID_FILE.exists()
    ):
        _stop_cloudflare(3.0, True)


def _start_ngrok(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    port = int(args.port)

    domain = (args.ngrok_domain or os.getenv("NGROK_DOMAIN", "")).strip()
    if domain.startswith("https://") or domain.startswith("http://"):
        print("NGROK_DOMAIN should contain only the domain, for example: your-domain.ngrok-free.dev")
        return 2
    if not domain:
        print("NGROK_DOMAIN is not set. Add it to mcp_server/.env or pass --ngrok-domain your-domain.ngrok-free.dev")
        return 2

    ngrok_path = _resolve_ngrok_binary(args.ngrok_bin)
    if ngrok_path is None:
        print("ngrok was not found. Install it with Homebrew or set NGROK_BIN in mcp_server/.env")
        return 2

    validation = _validate_managed_pid(
        NGROK_PID_FILE,
        "ngrok",
        port=port,
        binary=ngrok_path,
    )
    if validation.valid and validation.pid is not None:
        print(f"ngrok is already running for mac-mcp (pid {validation.pid}; identity verified).")
        return 0
    if validation.status == "unverifiable":
        print("Refusing to start ngrok: existing PID ownership is unverifiable.")
        return 1
    if validation.safe_to_remove_record:
        NGROK_PID_FILE.unlink(missing_ok=True)

    public_url = f"https://{domain}"
    target = str(port)
    endpoint_flag = ngrok_http_endpoint_flag(ngrok_path)
    if endpoint_flag == "--url":
        cmd = [ngrok_path, "http", "--url", public_url, target]
    else:
        cmd = [ngrok_path, "http", f"--domain={domain}", target]
    with NGROK_LOG_FILE.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(0.8)
    if proc.poll() is not None:
        print(f"ngrok failed to start. See log: {NGROK_LOG_FILE}")
        NGROK_PID_FILE.unlink(missing_ok=True)
        return proc.returncode or 1

    snapshot = process_snapshot(proc.pid)
    if snapshot is None or not matches_role(
        snapshot,
        "ngrok",
        port=port,
        binary=ngrok_path,
    ):
        try:
            proc.terminate()
        except OSError:
            pass
        print("ngrok started a process whose identity could not be verified; it was not adopted.")
        return 1
    try:
        write_process_record(
            NGROK_PID_FILE,
            "ngrok",
            proc.pid,
            metadata={"port": port, "ownership_source": "spawn"},
            snapshot=snapshot,
        )
    except (OSError, RuntimeError):
        try:
            proc.terminate()
        except OSError:
            pass
        print("ngrok process identity could not be persisted; started process was terminated.")
        return 1

    print(f"ngrok tunnel started: {public_url} -> {_local_url(args.host, port)} (pid {proc.pid}).")
    print(f"ngrok log: {NGROK_LOG_FILE}")
    # A live ngrok process is not yet a working public endpoint.
    if _public_route_ready(f"{public_url}/health", timeout_s=NGROK_READY_TIMEOUT_S):
        print(f"public endpoint verified: {public_url}/health answers.")
        return 0
    print(
        f"ngrok is running, but {public_url}/health does not answer yet (public endpoint degraded). "
        "Check the ngrok log and your network; mac-mcp status rechecks it."
    )
    return 3


NGROK_READY_TIMEOUT_S = 15.0


def _probe_public_route(url: str, timeout_s: float = 4.0) -> str:
    """'ok' when the public /health answers 200, else 'unreachable'."""
    request = UrlRequest(url, headers={"User-Agent": "mac-mcp-status"})
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return "ok" if response.status == 200 else "unreachable"
    except Exception:
        return "unreachable"


def _public_route_ready(url: str, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.5, timeout_s)
    while True:
        if _probe_public_route(url, timeout_s=min(4.0, max(0.5, deadline - time.monotonic()))) == "ok":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _start_plain(args: argparse.Namespace) -> int:
    _load_env()
    try:
        public = _public_endpoint_config(args)
    except PublicEndpointError as exc:
        print(f"Public endpoint configuration error: {exc}")
        return 2
    try:
        validate_bootstrap_security(
            load_settings(), host=args.host, public_endpoint_mode=public.mode,
        )
    except RuntimeError as exc:
        print(f"Security bootstrap error: {exc}")
        return 2
    _record_intent("running", args)
    server_code = _start_server(args)
    if server_code != 0:
        return server_code
    _stop_unselected_public_processes(public.mode)
    if public.mode == "ngrok":
        return _start_ngrok(args)
    if public.mode == "cloudflare":
        return _start_cloudflare(args, public)
    if public.mode == "custom":
        print(f"custom public endpoint configured: {public.endpoint_url}")
    else:
        print("public endpoint mode: local only")
    return 0


_TUNNEL_STEP = {"cloudflare": ("Connected the Cloudflare tunnel", "Connect the Cloudflare tunnel"),
                "ngrok": ("Started ngrok", "Start ngrok")}


def start(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if style is None:
        return _start_plain(args)
    _load_env()
    style.header("start")
    from .cli_steps import Steps
    steps = Steps(style)
    with steps.step("Checked the settings", failed_label="Check the settings") as state:
        try:
            public = _public_endpoint_config(args)
            validate_bootstrap_security(load_settings(), host=args.host, public_endpoint_mode=public.mode)
        except (PublicEndpointError, RuntimeError) as exc:
            print(str(exc))
            state.ok = False
    if not state.ok:
        style.summary("error", "Mac MCP's settings need a fix first", next_command="mac-mcp doctor")
        return 2
    _record_intent("running", args)
    with steps.step("Started the server", failed_label="Start the server") as state:
        code = _start_server(args)
        state.ok = code == 0
        if code == 0 and "already running" in state.printed:
            state.label = "The server was already running"
    if code != 0:
        style.summary("error", "Mac MCP did not start", next_command="mac-mcp doctor")
        return code
    _stop_unselected_public_processes(public.mode)
    if public.mode in _TUNNEL_STEP:
        done, failed = _TUNNEL_STEP[public.mode]
        with steps.step(done, failed_label=failed) as state:
            code = _start_cloudflare(args, public) if public.mode == "cloudflare" else _start_ngrok(args)
            state.ok = code == 0
        if code != 0:
            style.summary("warn", "Mac MCP is running, but its public address is not", next_command="mac-mcp doctor")
            return code
    pid = _resolve_server_identity(int(getattr(args, "port", _default_port())))[0]
    where = urlparse(public.endpoint_url).netloc if getattr(public, "endpoint_url", None) else "this Mac only"
    style.summary("ok", f"Started — pid {pid} · {where}")
    return 0


def _stop_plain(args: argparse.Namespace) -> int:
    _load_env()
    if not getattr(args, "keep_intent", False):
        # Before stopping anything, so the supervisor never undoes this stop.
        _record_intent("stopped", None)
    port = _default_port()
    server_pid, server_source = _resolve_server_identity(port)
    if server_pid:
        server_ok = _stop_pid(
            PID_FILE,
            "mac-mcp",
            args.timeout,
            args.force,
        )
    elif server_source in {
        "pid_unverifiable",
        "foreign_listener",
        "ambiguous_listener",
        "adoption_failed",
    }:
        print(
            f"Refusing to stop mac-mcp: server ownership is not verifiable "
            f"({server_source})."
        )
        server_ok = False
    else:
        PID_FILE.unlink(missing_ok=True)
        print("mac-mcp is not running.")
        server_ok = True

    ngrok_ok = _stop_pid(
        NGROK_PID_FILE,
        "ngrok",
        args.timeout,
        args.force,
    )
    cloudflare_ok = _stop_cloudflare(args.timeout, args.force)
    if server_ok and STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve() and _server_job_plist_path().exists():
        # The server ran under Mac MCP Server: its launchd job ends with it; unload it too.
        _unload_server_job()
    return 0 if server_ok and ngrok_ok and cloudflare_ok else 1



def stop(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if style is None or getattr(args, "keep_intent", False):
        # keep_intent: the supervisor's and restart's own stop, never a person at a terminal.
        return _stop_plain(args)
    _load_env()
    style.header("stop")
    from .cli_steps import Steps
    steps = Steps(style)
    _record_intent("stopped", None)
    port = _default_port()
    unverifiable = False
    was_running = True
    with steps.step("Stopped the server", failed_label="Stop the server") as state:
        server_pid, server_source = _resolve_server_identity(port)
        if server_pid:
            state.ok = _stop_pid(PID_FILE, "mac-mcp", args.timeout, args.force)
        elif server_source in {"pid_unverifiable", "foreign_listener", "ambiguous_listener", "adoption_failed"}:
            print(f"Refusing to stop mac-mcp: server ownership is not verifiable ({server_source}).")
            state.ok = False
            unverifiable = True
        else:
            PID_FILE.unlink(missing_ok=True)
            state.label = "The server was not running"
            was_running = False
    server_ok = state.ok
    with steps.step("Stopped the public tunnel", failed_label="Stop the public tunnel") as state:
        # Both, as the plain stop does: a failed ngrok stop must not leave cloudflared running.
        ngrok_ok = _stop_pid(NGROK_PID_FILE, "ngrok", args.timeout, args.force)
        cloudflare_ok = _stop_cloudflare(args.timeout, args.force)
        state.ok = ngrok_ok and cloudflare_ok
    tunnel_ok = state.ok
    if server_ok and STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve() and _server_job_plist_path().exists():
        _unload_server_job()
    if server_ok and tunnel_ok:
        style.summary("ok", "Stopped — AI apps can no longer reach this Mac." if was_running else "Mac MCP was not running.")
        return 0
    if unverifiable:
        style.summary("error", "Mac MCP cannot tell that the program on its port is its own", next_command="mac-mcp status")
        return 1
    style.summary("error", "Something did not stop", next_command="mac-mcp stop --force")
    return 1

def _connect_config_endpoint(client: str, selection: str) -> tuple[str, str]:
    local_endpoint = f"http://127.0.0.1:{_default_port()}/mcp"
    if selection == "local":
        return local_endpoint, "local"

    should_use_public = selection == "public" or (selection == "auto" and client == "chatgpt")
    if not should_use_public:
        return local_endpoint, "local"

    public = resolve_public_endpoint()
    if not public.endpoint_url:
        if client == "chatgpt":
            raise ConnectionConfigError(_CHATGPT_NEEDS_PUBLIC)
        raise ConnectionConfigError(
            "no public MCP endpoint is configured; choose --endpoint local or configure "
            "Cloudflare, ngrok, or a custom HTTPS endpoint"
        )
    return public.endpoint_url, "public"


# Local only is the safe default, so the first ChatGPT attempt usually lands here.
_CHATGPT_NEEDS_PUBLIC = (
    "ChatGPT connects to Mac MCP from OpenAI's servers, so it needs a public HTTPS address, "
    "and this Mac is set to Local only.\n"
    "Next steps:\n"
    "  1. Open Mac MCP Settings > Connections > Public Endpoint and choose Cloudflare, ngrok or Custom HTTPS.\n"
    "  2. Run: mac-mcp status   (the public endpoint should be reported healthy)\n"
    "  3. Run this command again: mac-mcp connect-config --client chatgpt\n"
    "Local only already works for AI apps on this Mac: "
    "mac-mcp connect-config --client claude-code (or codex, opencode)."
)


def _public_endpoint_check(endpoint_url: str) -> str:
    """'ok' or 'unreachable' for the public route the snippet points at (same probe as status)."""
    route = public_health_url(SimpleNamespace(endpoint_url=endpoint_url))
    return _probe_public_route(route) if route else "unreachable"


def connect_config(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if args.client in UNGUIDED_CLIENTS:
        if style is not None:
            from .cli_views import render_error
            render_error(style, "connect-config", UNGUIDED_CLIENTS[args.client])
            return 1
        print(f"mac-mcp connect-config: {UNGUIDED_CLIENTS[args.client]}", file=sys.stderr)
        return 1
    try:
        endpoint_url, endpoint_kind = _connect_config_endpoint(args.client, args.endpoint)
        settings = load_settings()
        auth_required = not settings.allow_no_auth
        if auth_required and not settings.api_key:
            raise ConnectionConfigError(
                "authentication is required but MCP_API_KEY is not configured"
            )
        rendered = render_connection_config(
            client=args.client,
            endpoint_url=endpoint_url,
            auth_required=auth_required,
            server_name=args.name,
            auth_env=args.auth_env,
        )
    except (ConnectionConfigError, PublicEndpointError) as exc:
        if style is not None:
            from .cli_views import render_error
            render_error(style, "connect-config", str(exc))
            return 1
        print(f"mac-mcp connect-config: {exc}", file=sys.stderr)
        return 1
    if style is not None:
        from .cli_views import render_connect_config
        public_ok = (_public_endpoint_check(endpoint_url) == "ok") if endpoint_kind == "public" else None
        render_connect_config(style, rendered, endpoint_kind, endpoint_url, public_ok, auth_required)
        return 0

    print(f"Client: {rendered.client}")
    print(f"Target: {rendered.target}")
    print(f"Endpoint: {endpoint_kind} ({endpoint_url})")
    if endpoint_kind == "public":
        if _public_endpoint_check(endpoint_url) == "ok":
            print("Public endpoint check: reachable")
        else:
            print("Public endpoint check: NOT reachable right now. The client will not connect until "
                  "`mac-mcp status` reports the public endpoint healthy.")
    print(
        "Authentication: "
        + ("Bearer/API-key required; secret value not printed" if auth_required else "disabled")
    )
    print()
    print(rendered.snippet)
    if rendered.secret_instruction:
        print()
        print("Secret setup:")
        print(rendered.secret_instruction)
    if rendered.verify_instruction:
        print()
        print("Verify:")
        print(rendered.verify_instruction)
    if rendered.client == "chatgpt" and auth_required:
        print(
            "Use the query-key URL only because this ChatGPT connection path is "
            "header-limited; upstream proxies/tunnels may observe query strings."
        )
    return 0


def _server_health(port: int) -> str:
    """Probe the local /health endpoint; a live process that never answers is not healthy."""
    for attempt in range(2):
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:  # noqa: S310 - fixed loopback URL
                if response.status == 200:
                    return "ok"
        except (URLError, OSError, ValueError):
            pass
        if attempt == 0:
            time.sleep(0.5)
    return "unreachable"


def status(args: argparse.Namespace) -> int:
    """Report server and public endpoint state.

    Exit codes (stable): 0 healthy; 1 server not running, not verified, its port is
    taken by another process, or it does not answer /health; 2 server running but the
    selected public endpoint is unavailable or misconfigured (0 with --local-only).
    """
    _load_env()
    port = _default_port()
    lines: list[str] = []
    say = lines.append  # human output is collected so --json can replace it

    server_pid, server_source = _resolve_server_identity(port)
    server_running = server_pid is not None

    ngrok_validation = _validate_managed_pid(
        NGROK_PID_FILE,
        "ngrok",
        port=port,
    )
    ngrok_pid = ngrok_validation.pid if ngrok_validation.valid else None
    ngrok_running = ngrok_pid is not None
    if ngrok_validation.safe_to_remove_record:
        NGROK_PID_FILE.unlink(missing_ok=True)

    cloudflare_pid: int | None = None
    cloudflare_source = "not_running"
    launchd_raw_pid = _launchctl_pid()
    if launchd_raw_pid:
        verified_pid, identity = _cloudflare_launchd_identity(port=port)
        if verified_pid:
            cloudflare_pid = verified_pid
            cloudflare_source = "launchd_verified"
            snapshot = process_snapshot(verified_pid)
            if snapshot is not None:
                try:
                    write_process_record(
                        CLOUDFLARE_PID_FILE,
                        "cloudflared",
                        verified_pid,
                        metadata={
                            "port": port,
                            "launchd_label": CLOUDFLARE_LAUNCHD_LABEL,
                            "ownership_source": "launchd",
                        },
                        snapshot=snapshot,
                    )
                except (OSError, RuntimeError):
                    cloudflare_pid = None
                    cloudflare_source = "record_failed"
        else:
            cloudflare_source = f"launchd_{identity}"
    elif CLOUDFLARE_PID_FILE.exists():
        cf_validation = _validate_managed_pid(
            CLOUDFLARE_PID_FILE,
            "cloudflared",
            port=port,
        )
        if cf_validation.valid:
            cloudflare_pid = cf_validation.pid
            cloudflare_source = "pid_record"
        elif cf_validation.safe_to_remove_record:
            CLOUDFLARE_PID_FILE.unlink(missing_ok=True)
            cloudflare_source = cf_validation.status
        else:
            cloudflare_source = cf_validation.status
    cloudflare_running = cloudflare_pid is not None

    port_owners: list[dict] = []
    if server_running:
        say(
            f"mac-mcp is running (pid {server_pid}; identity verified via {server_source})."
        )
        say(f"mac-mcp log: {LOG_FILE}")
    else:
        if server_source == "foreign_listener":
            conflicts = _server_listener_conflicts(port)
            port_owners = [listener_owner(pid) for pid in conflicts if pid > 0]
            rendered = ", ".join(
                f"{owner['name']} (pid {owner['pid']})" if owner["name"] else f"pid {owner['pid']}"
                for owner in port_owners
            ) or "an unknown program"
            say(f"mac-mcp is not running; configured port {port} is used by {rendered}.")
        elif server_source not in {"not_running"}:
            say(f"mac-mcp ownership is not verified ({server_source}).")
        else:
            say("mac-mcp is not running.")

    public_error: str | None = None
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError as exc:
        public = None
        public_error = str(exc)
        say(f"public endpoint configuration error: {exc}")

    if public is not None:
        if public.mode == "custom":
            say("public endpoint mode: custom")
            say(f"MCP URL: {public.endpoint_url}")
        elif public.mode == "cloudflare":
            say("public endpoint mode: cloudflare")
            if cloudflare_running:
                say(
                    f"cloudflared is running (pid {cloudflare_pid}; identity verified via {cloudflare_source})."
                )
                say(f"cloudflared log: {CLOUDFLARE_LOG_FILE}")
                say(f"MCP URL: {public.endpoint_url}")
            else:
                say(
                    "Cloudflare Tunnel is configured but no verified mac-mcp cloudflared process is running "
                    f"({cloudflare_source})."
                )
        elif public.mode == "ngrok":
            say("public endpoint mode: ngrok")
            if ngrok_running:
                say(f"ngrok is running (pid {ngrok_pid}; identity verified).")
                say(f"ngrok log: {NGROK_LOG_FILE}")
                say(f"MCP URL: {public.endpoint_url}")
            else:
                say(
                    "ngrok is configured but no verified mac-mcp ngrok process is running "
                    f"({ngrok_validation.status})."
                )
        else:
            say("public endpoint mode: local only")

    if ngrok_running and (public is None or public.mode != "ngrok"):
        say(
            f"ngrok managed process is still running (pid {ngrok_pid}) but is not "
            "the selected public endpoint mode."
        )

    if cloudflare_running and (public is None or public.mode != "cloudflare"):
        say(
            f"cloudflared managed process is still running (pid {cloudflare_pid}) but is not "
            "the selected public endpoint mode."
        )

    health = _server_health(port) if server_running else None
    if health == "unreachable":
        say(f"mac-mcp process is running but http://127.0.0.1:{port}/health does not answer.")
    tunnel_missing = public is not None and (
        (public.mode == "cloudflare" and not cloudflare_running)
        or (public.mode == "ngrok" and not ngrok_running)
    )
    # A running tunnel process is not proof of a working route: probe it.
    public_route: str | None = None
    if server_running and health != "unreachable" and public is not None and not tunnel_missing \
            and public.mode in {"cloudflare", "ngrok", "custom"}:
        route_url = public_health_url(public)
        public_route = _probe_public_route(route_url) if route_url else None
        if public_route == "unreachable":
            say(f"public endpoint {route_url} does not answer (the tunnel process is running).")
    remediation: list[str] = []
    if not server_running:
        state = {"not_running": "stopped", "foreign_listener": "port_conflict"}.get(server_source, "ownership_unverified")
        code = 1
        remediation.append({
            "stopped": "Start it with: mac-mcp start",
            "port_conflict": port_conflict_advice(port, port_owners),
            "ownership_unverified": "Run mac-mcp restart to start a verified server.",
        }[state])
    elif health == "unreachable":
        state, code = "unresponsive", 1
        remediation.append("Run mac-mcp restart; if it keeps failing, check mac-mcp logs server.")
    elif public is None or tunnel_missing or public_route == "unreachable":
        state = "config_error" if public is None else "degraded"
        say("overall: degraded (local server running; selected public endpoint unavailable)")
        # Exit 2 keeps scripts from reading a missing tunnel as healthy;
        # --local-only restores the local-server-only verdict.
        code = 0 if getattr(args, "local_only", False) else 2
        if public is None:
            remediation.append("Fix the public endpoint settings; mac-mcp doctor shows the details.")
        elif tunnel_missing:
            remediation.append(f"Run mac-mcp restart to start the {public.mode} tunnel, or use --local-only to ignore it.")
        else:
            remediation.append(
                f"The {public.mode} endpoint is configured but its public /health does not answer; "
                "check the network and the tunnel log (mac-mcp logs), or use --local-only to ignore it."
            )
    else:
        state, code = "healthy", 0

    supervision = supervisor_report()
    say(_format_supervision(supervision))
    from . import privacy_policy

    rules = privacy_policy.policy(fresh=True)
    privacy = {"paused": rules["paused"], "private_mode": rules["private_mode"],
               "excluded_apps": len(rules["excluded_apps"]), "excluded_sites": len(rules["excluded_sites"])}
    if rules["paused"]:
        say("privacy: observation is PAUSED (screen, pages, clipboard and voice are not read; Settings > Privacy).")
    if rules["private_mode"]:
        say("privacy: private mode is on (nothing about this work is kept).")

    tunnel_pid = cloudflare_pid if public is not None and public.mode == "cloudflare" else (
        ngrok_pid if public is not None and public.mode == "ngrok" else None)
    stray = [
        {"name": name, "pid": pid}
        for name, pid, mode in (("ngrok", ngrok_pid, "ngrok"), ("cloudflared", cloudflare_pid, "cloudflare"))
        if pid is not None and (public is None or public.mode != mode)
    ]
    report = {
        "ok": code == 0,
        "state": state,
        "exit_code": code,
        "server": {
            "running": server_running,
            "pid": server_pid,
            "identity": server_source,
            "port": port,
            "health": health,
            "log": str(LOG_FILE),
            "port_owners": port_owners,
        },
        "public_endpoint": {
            "mode": public.mode if public is not None else None,
            "url": public.endpoint_url if public is not None else None,
            "tunnel_running": (
                (cloudflare_running if public.mode == "cloudflare" else ngrok_running)
                if public is not None and public.mode in {"cloudflare", "ngrok"} else None
            ),
            "tunnel_pid": tunnel_pid,
            "route": public_route,
            "error": public_error,
        },
        "stray_processes": stray,
        "privacy": privacy,
        "supervisor": supervision,
        "remediation": remediation,
    }
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return code
    style = Style.for_stream()
    if style is not None:
        from .cli_views import render_status
        render_status(style, report)
        return code
    for line in lines:
        sys.stdout.write(line + "\n")
    return code


def _restart_status_path() -> Path:
    return STATE_DIR / "restart-status.json"


def _write_restart_status(state: str, **details: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": str(state),
        "updated_at": time.time(),
        **{str(key): value for key, value in details.items()},
    }
    path = _restart_status_path()
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def _lifecycle_flags(args: argparse.Namespace) -> list[str]:
    """Start flags that reproduce this lifecycle request (no secrets: paths and modes only)."""
    command = ["--host", str(args.host), "--port", str(int(args.port))]
    if getattr(args, "reload", False):
        command.append("--reload")
    for flag, attr in (
        ("--public-mode", "public_mode"),
        ("--public-url", "public_url"),
        ("--cloudflare-tunnel", "cloudflare_tunnel"),
        ("--cloudflare-token-file", "cloudflare_token_file"),
        ("--cloudflared-bin", "cloudflared_bin"),
        ("--ngrok-domain", "ngrok_domain"),
        ("--ngrok-bin", "ngrok_bin"),
    ):
        value = getattr(args, attr, None)
        if value:
            command.extend([flag, str(value)])
    if getattr(args, "ngrok", False):
        command.append("--ngrok")
    return command


def _restart_handoff_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable, "-m", "mcp_server.cli", "restart",
        "--timeout", str(float(args.timeout)),
        *_lifecycle_flags(args),
    ]


def _restart_handoff_job() -> tuple[bool, int | None]:
    target = _launchctl_target(RESTART_HANDOFF_LABEL)
    try:
        proc = _launchctl_run("print", target, capture=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if proc.returncode != 0:
        return False, None
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", proc.stdout or "")
    if not match:
        return True, None
    try:
        pid = int(match.group(1))
    except ValueError:
        return True, None
    return True, pid if pid > 0 else None


def _restart_handoff_plist_path() -> Path:
    return STATE_DIR / "restart-handoff.plist"


def _remove_stale_restart_handoff() -> bool:
    loaded, pid = _restart_handoff_job()
    if not loaded:
        _restart_handoff_plist_path().unlink(missing_ok=True)
        return True
    if pid and _pid_alive(pid):
        return False
    try:
        proc = _launchctl_run(
            "bootout",
            _launchctl_target(RESTART_HANDOFF_LABEL),
            capture=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    _restart_handoff_plist_path().unlink(missing_ok=True)
    return True


def _write_restart_handoff_plist(args: argparse.Namespace) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.touch(mode=0o600, exist_ok=True)
    os.chmod(LOG_FILE, 0o600)
    path = _restart_handoff_plist_path()
    env_command = [
        "/usr/bin/env",
        f"{RESTART_HANDOFF_ENV}=1",
        f"MAC_MCP_STATE_DIR={STATE_DIR}",
        f"PYTHONPATH={PROJECT_ROOT}",
        # A waiting requester (restart --wait, or the themed restart watching its stages)
        # stays alive until the outcome, so the worker must not wait for it to exit first.
        f"{RESTART_REQUESTER_ENV}={0 if getattr(args, 'wait', False) or getattr(args, 'requester_waits', False) else os.getpid()}",
        *_restart_handoff_command(args),
    ]
    payload = {
        "Label": RESTART_HANDOFF_LABEL,
        "ProgramArguments": env_command,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "Umask": 0o077,
    }
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with tmp.open("wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _spawn_detached_restart(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    requested_at = time.time()
    loaded, pid = _restart_handoff_job()
    if loaded and pid and _pid_alive(pid):
        print(f"mac-mcp restart is already in progress under launchd (helper pid {pid}).")
        return _wait_for_restart_outcome(args, since=0.0) if getattr(args, "wait", False) else 0
    if loaded and not _remove_stale_restart_handoff():
        print("Could not clear the previous mac-mcp restart handoff job.")
        return 1

    _write_restart_status(
        "requested",
        requested_by_pid=os.getpid(),
        server_pid=_resolve_server_identity(_default_port(), adopt_listener=False)[0],
    )
    plist_path = _write_restart_handoff_plist(args)
    try:
        proc = _launchctl_run(
            "bootstrap",
            f"gui/{os.getuid()}",
            str(plist_path),
            capture=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _write_restart_status("failed", stage="bootstrap", error=str(exc))
        print(f"Could not hand off mac-mcp restart to launchd: {exc}")
        return 1
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "launchctl bootstrap failed").strip()
        _write_restart_status("failed", stage="bootstrap", error=detail)
        print(f"Could not hand off mac-mcp restart to launchd: {detail}")
        return 1

    time.sleep(0.1)
    loaded, helper_pid = _restart_handoff_job()
    if not loaded:
        _write_restart_status("failed", stage="bootstrap", error="launchd handoff job disappeared before execution")
        print(f"mac-mcp restart handoff did not remain registered. See log: {LOG_FILE}")
        return 1
    print(
        "mac-mcp restart handed off to one-shot launchd worker"
        + (f" (helper pid {helper_pid})." if helper_pid else ".")
    )
    if getattr(args, "wait", False):
        return _wait_for_restart_outcome(args, since=requested_at)
    return 0


def _read_restart_status() -> dict:
    try:
        payload = json.loads(_restart_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _wait_for_restart_outcome(args: argparse.Namespace, *, since: float) -> int:
    """Block until the handed-off restart reports a final state; 0 only for success."""
    deadline = time.time() + 2 * _startup_health_timeout_s() + RESTART_REQUESTER_WAIT_S + 60
    payload: dict = {}
    while time.time() < deadline:
        payload = _read_restart_status()
        state = str(payload.get("state") or "")
        if state in RESTART_FINAL_STATES and float(payload.get("updated_at") or 0) >= since:
            break
        time.sleep(0.25)
    else:
        print(f"mac-mcp restart did not report an outcome in time (last stage: {payload.get('stage') or 'unknown'}).")
        return 3
    state = str(payload.get("state"))
    if state == "succeeded":
        retried = " after one recovery attempt" if payload.get("recovered_after_retry") else ""
        print(f"mac-mcp restarted (pid {payload.get('server_pid')}; health verified{retried}).")
        return 0
    if state == "degraded":
        print("mac-mcp restarted locally, but the public endpoint did not start. Run `mac-mcp doctor`.")
        return 1
    stage = payload.get("stage") or "unknown"
    if payload.get("server_left_running"):
        print(f"mac-mcp restart was not attempted ({stage}): {payload.get('error')}. The server kept running.")
    else:
        print(f"mac-mcp restart failed at stage '{stage}'. Repair with: {payload.get('repair_command') or 'mac-mcp doctor'}")
    return 1


def _restart_health_ok(
    args: argparse.Namespace,
    timeout_s: float = 10.0,
    *,
    expected_pid: Optional[int] = None,
) -> bool:
    host = str(getattr(args, "host", "") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    port = int(getattr(args, "port", _default_port()))
    url = f"http://{host}:{port}/health?probe=basic"
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if expected_pid is not None and not _pid_alive(int(expected_pid)):
            return False
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                    if isinstance(payload, dict) and payload.get("ok") is True:
                        return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _wait_for_restart_requester_exit() -> bool:
    raw = os.getenv(RESTART_REQUESTER_ENV, "").strip()
    try:
        requester_pid = int(raw)
    except ValueError:
        requester_pid = 0
    if requester_pid <= 0:
        time.sleep(RESTART_RESPONSE_GRACE_S)
        return True

    deadline = time.monotonic() + RESTART_REQUESTER_WAIT_S
    while _pid_alive(requester_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(requester_pid):
        _write_restart_status(
            "failed",
            stage="requester_wait",
            requester_pid=requester_pid,
            helper_pid=os.getpid(),
        )
        return False

    time.sleep(RESTART_RESPONSE_GRACE_S)
    return True


def _install_restart_signal_receipts() -> None:
    def handler(signum, _frame) -> None:
        try:
            _write_restart_status(
                "failed",
                stage="signal",
                signal=int(signum),
                helper_pid=os.getpid(),
            )
        finally:
            os._exit(128 + int(signum))

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def _restart_preflight(args: argparse.Namespace) -> str | None:
    """Checks that must pass before a restart stops a working server."""
    try:
        public = _public_endpoint_config(args)
    except PublicEndpointError as exc:
        return f"public endpoint configuration error: {exc}"
    try:
        validate_bootstrap_security(load_settings(), host=args.host, public_endpoint_mode=public.mode)
    except RuntimeError as exc:
        return f"security bootstrap error: {exc}"
    if public.mode == "cloudflare" and not _resolve_cloudflared_binary(getattr(args, "cloudflared_bin", None)):
        return "cloudflared is not installed"
    if public.mode == "ngrok" and not _resolve_ngrok_binary(getattr(args, "ngrok_bin", None)):
        return "ngrok is not installed"
    if Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve():
        venv = inspect_venv(PROJECT_ROOT)
        if venv["status"] != "ok":
            return (
                f"runtime Python environment is {venv['status']}; repair it with: "
                f"python3 {PROJECT_ROOT / 'mcp_server' / 'venv_repair.py'} repair"
            )
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import uvicorn, fastapi, mcp, httpx"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, cwd=str(PROJECT_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"runtime Python could not run ({type(exc).__name__})"
    if probe.returncode != 0:
        return "runtime Python cannot import the server dependencies"
    return None


def _recover_failed_restart(args: argparse.Namespace, exit_code: int, *, stage: str = "start") -> int:
    """After a failed start: one bounded retry, then a final state that says whether the server is up."""
    if _restart_health_ok(args, timeout_s=3):
        server_pid, source = _resolve_server_identity(int(args.port), adopt_listener=False)
        _write_restart_status(
            "degraded", stage="public_endpoint", exit_code=exit_code, server_pid=server_pid,
            ownership_source=source, health=True, repair_command="mac-mcp doctor",
        )
        print("mac-mcp restarted locally, but the public endpoint did not start. Run `mac-mcp doctor`.")
        return exit_code or 1
    _write_restart_status("running", stage="recovering", first_stage=stage, first_exit_code=exit_code,
                          helper_pid=os.getpid())
    print(f"mac-mcp restart failed at '{stage}' (exit {exit_code}); retrying the start once.")
    time.sleep(RESTART_RECOVERY_DELAY_S)
    retry_code = start(args)
    server_up = _restart_health_ok(args, timeout_s=_startup_health_timeout_s() if retry_code == 0 else 3)
    server_pid, source = _resolve_server_identity(int(args.port), adopt_listener=False)
    if retry_code == 0 and server_up:
        _write_restart_status(
            "succeeded", server_pid=server_pid, ownership_source=source, health=True,
            recovered_after_retry=True, first_stage=stage, first_exit_code=exit_code,
        )
        print("mac-mcp restart recovered on the second start attempt.")
        return 0
    if server_up:
        _write_restart_status(
            "degraded", stage="public_endpoint", exit_code=retry_code, server_pid=server_pid,
            ownership_source=source, health=True, first_stage=stage, repair_command="mac-mcp doctor",
        )
        print("mac-mcp restarted locally, but the public endpoint did not start. Run `mac-mcp doctor`.")
        return retry_code or 1
    _write_restart_status(
        "failed", stage=stage, exit_code=retry_code or exit_code, server_down=True, helper_pid=os.getpid(),
        repair_command="mac-mcp doctor && mac-mcp start",
    )
    print(f"mac-mcp is down after a failed restart. See {LOG_FILE}; repair with: mac-mcp doctor && mac-mcp start")
    return retry_code or exit_code or 1


def restart(args: argparse.Namespace) -> int:
    _load_env()
    handoff_child = os.getenv(RESTART_HANDOFF_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if not handoff_child:
        # Always delegate restart to launchd. A restart tears down the server that
        # may currently be carrying this command; launchd ownership prevents MCP
        # cancellation cleanup or shell process-group cleanup from killing the worker.
        style = Style.for_stream()
        if style is None:
            return _spawn_detached_restart(args)
        return _restart_themed(args, style)

    # This marker identifies only the one-shot launchd worker. Clear it before
    # any descendants can inherit it and mistake themselves for the worker.
    os.environ.pop(RESTART_HANDOFF_ENV, None)
    _install_restart_signal_receipts()
    _write_restart_status(
        "running",
        stage="waiting_for_requester",
        helper_pid=os.getpid(),
        requester_pid=int(os.getenv(RESTART_REQUESTER_ENV, "0") or 0),
    )
    if not _wait_for_restart_requester_exit():
        return 1
    # The requester PID is also one-shot coordination state. Once the requester
    # has exited, descendants must not inherit stale restart coordination.
    os.environ.pop(RESTART_REQUESTER_ENV, None)

    preflight_error = _restart_preflight(args)
    if preflight_error:
        _write_restart_status(
            "failed", stage="preflight", error=preflight_error, helper_pid=os.getpid(), server_left_running=True,
        )
        print(f"Restart aborted before stopping the server: {preflight_error}")
        return 1

    target_port = int(getattr(args, "port", _default_port()))
    target_server_pid, target_source = _resolve_server_identity(target_port, adopt_listener=False)
    _write_restart_status(
        "running",
        stage="stopping",
        helper_pid=os.getpid(),
        helper_ppid=os.getppid(),
        helper_pgid=os.getpgid(0),
        helper_sid=os.getsid(0),
        target_server_pid=target_server_pid,
        target_source=target_source,
    )
    stop_args = argparse.Namespace(timeout=args.timeout, force=True, keep_intent=True)
    stop_code = stop(stop_args)
    if stop_code != 0 and not _server_gone(target_port, timeout_s=float(args.timeout)):
        _write_restart_status("failed", stage="stop", exit_code=stop_code, helper_pid=os.getpid())
        print("Restart aborted because managed process shutdown was not verified.")
        return stop_code
    if stop_code != 0:
        # The old server stopped listening first and exited after the wait: it is gone now,
        # so bring the new one up instead of leaving Mac MCP down.
        print("The old server exited after the stop wait; starting the new one.")

    _write_restart_status("running", stage="starting", helper_pid=os.getpid())
    start_code = start(args)
    if start_code != 0:
        return _recover_failed_restart(args, start_code)

    _write_restart_status("running", stage="verifying", helper_pid=os.getpid())
    if not _restart_health_ok(args):
        return _recover_failed_restart(args, 1, stage="health")

    server_pid, source = _resolve_server_identity(int(args.port), adopt_listener=False)
    _write_restart_status(
        "succeeded",
        server_pid=server_pid,
        ownership_source=source,
        health=True,
    )
    return 0


_RESTART_STAGES = {
    "stopping": ("Stopped the server", "Stop the server"),
    "starting": ("Started the server", "Start the server"),
    "verifying": ("Checked that it answers", "Check that it answers"),
    "recovering": ("Recovered after a failed start", "Recover after a failed start"),
    "public_endpoint": ("Connected the public tunnel", "Connect the public tunnel"),
}


def _restart_themed(args: argparse.Namespace, style) -> int:
    """In a terminal, restart waits for the launchd worker and shows its stages as steps."""
    style.header("restart")
    from .cli_steps import Steps
    steps = Steps(style)
    since = time.time()
    with steps.step("Handed the restart to launchd", failed_label="Hand the restart to launchd") as state:
        # This process watches the stages itself (wait=False) and stays alive meanwhile.
        code = _spawn_detached_restart(argparse.Namespace(**{**vars(args), "wait": False, "requester_waits": True}))
        state.ok = code == 0
    if code != 0:
        style.summary("error", "The restart could not start", next_command="mac-mcp doctor")
        return code
    return _watch_restart(style, steps, args, since)


def _watch_restart(style, steps, args: argparse.Namespace, since: float) -> int:
    try:
        return _watch_restart_stages(style, steps, since)
    except BaseException:
        # Ctrl+C or a broken status file: fix the step's line and bring the cursor back.
        if steps.active:
            steps.end(False, failed_label=steps.running_label)
        raise


def _restart_updated_at(payload: dict) -> float:
    try:
        return float(payload.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _watch_restart_stages(style, steps, since: float) -> int:
    deadline = time.time() + 2 * _startup_health_timeout_s() + RESTART_REQUESTER_WAIT_S + 60
    current: str | None = None
    payload: dict = {}
    while time.time() < deadline:
        payload = _read_restart_status()
        state = str(payload.get("state") or "")
        stage = str(payload.get("stage") or "")
        if state in RESTART_FINAL_STATES and _restart_updated_at(payload) >= since:
            break
        if stage in _RESTART_STAGES and stage != current:
            if current is not None:
                # Recovering means the step before it failed.
                if stage == "recovering":
                    steps.end(False, failed_label=_RESTART_STAGES[current][1])
                else:
                    steps.end(True)
            done, doing = _RESTART_STAGES[stage]
            steps.begin(done, running=doing)
            current = stage
        time.sleep(0.25)
    else:
        if current is not None:
            steps.end(False, failed_label=_RESTART_STAGES[current][1])
        style.summary("error", "The restart did not report back in time", next_command="mac-mcp status")
        return 3
    state = str(payload.get("state"))
    if current is not None:
        # Degraded: the server is up; only a tunnel step on screen failed.
        ok = state == "succeeded" or (state == "degraded" and current != "public_endpoint")
        steps.end(ok, str(payload.get("error") or ""), _RESTART_STAGES[current][1])
    if state == "succeeded":
        retried = " · after one recovery attempt" if payload.get("recovered_after_retry") else ""
        style.summary("ok", f"Restarted — pid {payload.get('server_pid')}{retried}")
        return 0
    if state == "degraded":
        style.summary("warn", "Restarted locally, but the public address did not start", next_command="mac-mcp doctor")
        return 1
    if payload.get("server_left_running"):
        style.summary("warn", f"Restart not attempted ({payload.get('stage')}): {payload.get('error')}. The server kept running.")
        return 1
    # Without a stage step on screen, the summary says where it stopped.
    where = "" if current is not None else f" ({str(payload.get('stage') or 'unknown').replace('_', ' ')})"
    style.summary("error", f"Restart failed{where}", next_command=str(payload.get("repair_command") or "mac-mcp doctor"))
    return 1


def _server_gone(port: int, *, timeout_s: float) -> bool:
    """True once no Mac MCP server is recorded or listening on the port (polled, bounded)."""
    deadline = time.time() + max(0.0, timeout_s)
    while True:
        record = read_process_record(PID_FILE)
        recorded_alive = record is not None and _pid_alive(int(record.pid))
        if not recorded_alive and not listener_pids(int(port)):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def credential(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if style is not None and args.provider == "cloudflare":
        return _credential_themed(args, style)
    if args.provider != "cloudflare":
        print("Unsupported credential provider.")
        return 2
    if args.action == "save":
        try:
            write_cloudflare_token(sys.stdin.read(64 * 1024))
        except (OSError, PublicEndpointError) as exc:
            print(f"Could not save Cloudflare Tunnel credential: {exc}")
            return 2
        print("Cloudflare Tunnel credential saved securely (owner-only 0600).")
        return 0
    if args.action == "remove":
        removed = remove_cloudflare_token()
        print("Cloudflare Tunnel credential removed." if removed else "Cloudflare Tunnel credential was not configured.")
        return 0
    state = inspect_cloudflare_credential()
    if state.configured and state.secure:
        print("Cloudflare Tunnel credential: configured (secure 0600 owner-only file).")
        return 0
    if not state.configured:
        print("Cloudflare Tunnel credential: not configured.")
        return 1
    print(f"Cloudflare Tunnel credential: configured but unsafe ({state.reason}).")
    return 2


def _credential_themed(args: argparse.Namespace, style) -> int:
    """credential in a terminal. A token typed here is read hidden; a piped one as before."""
    from .cli_views import render_credential_status

    if args.action == "save":
        style.header("credential")
        if sys.stdin.isatty():
            import getpass
            secret = getpass.getpass(f"{INDENT}Cloudflare Tunnel token: ")
            style.write()
        else:
            secret = sys.stdin.read(64 * 1024)
        try:
            write_cloudflare_token(secret)
        except (OSError, PublicEndpointError) as exc:
            style.summary("error", f"Could not save the credential: {exc}", gap=False)
            return 2
        finally:
            secret = ""
        style.summary("ok", "Saved the Cloudflare Tunnel credential", note="owner-only file", gap=False)
        return 0
    if args.action == "remove":
        removed = remove_cloudflare_token()
        style.header("credential")
        if removed:
            style.summary("ok", "Removed the Cloudflare Tunnel credential", gap=False)
        else:
            style.summary("info", "No Cloudflare Tunnel credential was saved", gap=False)
        return 0
    state = inspect_cloudflare_credential()
    render_credential_status(style, state)
    if state.configured and state.secure:
        return 0
    return 1 if not state.configured else 2


def connect_app(args: argparse.Namespace) -> int:
    """One-click setup: add Mac MCP to an AI app (ChatGPT: copy its connector address)."""
    from . import client_setup

    style = Style.for_stream(json_output=bool(args.json))
    if style is not None:
        return _connect_app_themed(args, style)
    try:
        if args.client == "chatgpt":
            result = _copy_chatgpt_address()
        else:
            result = client_setup.connect(args.client)
    except (client_setup.SetupError, ConnectionConfigError, PublicEndpointError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "client": args.client, "error": str(exc)}))
        else:
            print(f"mac-mcp connect: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Mac MCP is added to {result['name']}. {result['next']}")
    return 0


def _connect_app_themed(args: argparse.Namespace, style) -> int:
    from . import client_setup
    from .cli_steps import Steps

    if args.client == "chatgpt":
        done, doing = "Copied ChatGPT's address", "Copy ChatGPT's address"
    else:
        name = next((app.name for app in client_setup.APPS if app.id == args.client), args.client)
        done, doing = f"Added Mac MCP to {name}", f"Add Mac MCP to {name}"
    style.header("connect")
    result: dict = {}
    with Steps(style).step(done, failed_label=doing) as state:
        try:
            result = _copy_chatgpt_address() if args.client == "chatgpt" else client_setup.connect(args.client)
        except (client_setup.SetupError, ConnectionConfigError, PublicEndpointError) as exc:
            print(str(exc))
            state.ok = False
    if not state.ok:
        style.summary("error", "Mac MCP was not added", next_command="mac-mcp clients")
        return 1
    style.summary("ok", str(result.get("next") or f"Mac MCP is added to {result.get('name')}."))
    return 0


def _copy_chatgpt_address() -> dict[str, object]:
    """ChatGPT is set up in its own window: put the address, key included, on the clipboard."""
    _load_env()
    endpoint_url, _ = _connect_config_endpoint("chatgpt", "auto")
    settings = load_settings()
    address = endpoint_url
    if not settings.allow_no_auth:
        if not settings.api_key:
            raise ConnectionConfigError("authentication is required but MCP_API_KEY is not configured")
        address = f"{endpoint_url}?{urlencode({'ApiKey': settings.api_key})}"
    # Straight to the clipboard: the key is never printed.
    subprocess.run(["/usr/bin/pbcopy"], input=address, text=True, check=True, timeout=5)
    return {
        "ok": True,
        "client": "chatgpt",
        "name": "ChatGPT",
        "copied": True,
        "next": "The address is copied. In ChatGPT, open Settings › Apps & Connectors, create a connector "
                "named Mac MCP and paste the address as its URL.",
    }


def clients(args: argparse.Namespace) -> int:
    """Which AI apps are on this Mac and which already have Mac MCP."""
    from . import client_setup

    apps = client_setup.apps_status()
    style = Style.for_stream(json_output=bool(args.json))
    if style is not None:
        from .cli_views import render_clients
        render_clients(style, apps)
        return 0
    if args.json:
        print(json.dumps({"ok": True, "apps": apps}))
    else:
        for app in apps:
            state = ("added (old setup with the key in its settings; run mac-mcp connect to update)"
                     if app["added"] and app.get("keyless") is False
                     else "added" if app["added"] else ("installed" if app["installed"] else "not installed"))
            print(f"{app['name']}: {state}")
    return 0


def stdio(args: argparse.Namespace) -> int:
    """Serve MCP over stdin/stdout for AI apps that start Mac MCP as a command."""
    from .stdio_bridge import run

    _load_env()
    return run(_default_port(), os.getenv("MCP_API_KEY", "").strip() or None)


def dashboard(args: argparse.Namespace) -> int:
    _load_env()
    style = Style.for_stream()
    port = _default_port()
    server_pid, server_source = _resolve_server_identity(port)
    if not server_pid:
        if style is not None:
            from .cli_views import render_error
            render_error(style, "dashboard", "Mac MCP is not running", next_command="mac-mcp start")
            return 1
        print(
            f"mac-mcp is not running with verified ownership ({server_source}). "
            "Start it first with: mac-mcp start"
        )
        return 1
    host = os.getenv("MAC_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    url = f"{_local_url(host, port)}/dashboard"
    token_file = dashboard_token_path()
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        if style is not None:
            from .cli_views import render_error
            render_error(style, "dashboard", "The dashboard key is missing", next_command="mac-mcp restart")
            return 1
        print("Dashboard credential is unavailable. Restart Mac MCP once, then run: mac-mcp dashboard")
        return 1
    authenticated_url = f"{url}#token={quote(token, safe='')}"
    opened = webbrowser.open(authenticated_url)
    if style is not None:
        # The key stays in the address handed to the browser; the screen shows it without.
        style.header("dashboard")
        style.rows([(None, "Address", url, "")])
        if opened:
            style.summary("ok", "Opened the dashboard in your browser")
        else:
            style.summary("warn", "The browser did not open", next_command="mac-mcp dashboard")
        return 0
    print(f"dashboard: {url} (authenticated local launch)")
    if not opened:
        print("The browser did not open automatically; run 'mac-mcp dashboard' again from this Mac.")
    return 0


def doctor(args: argparse.Namespace) -> int:
    from .diagnostics import format_report, run_doctor, write_support_bundle

    report = run_doctor()
    support_path = None
    if args.support_bundle is not None:
        support_path = write_support_bundle(report, args.support_bundle or None)
        report = dict(report)
        report["support_bundle"] = str(support_path)
    if getattr(args, "local_only", False):
        code = 0 if report.get("local_ok", report.get("ok")) else 1
    else:
        code = 0 if report.get("ok") else 1
    if args.json:
        print(json.dumps({**report, "exit_code": code}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        style = Style.for_stream()
        if style is not None:
            from .cli_views import render_doctor
            render_doctor(style, report, bool(getattr(args, "verbose", False)), str(support_path) if support_path else None)
            return code
        print(format_report(report))
        if support_path is not None:
            print(f"Support bundle: {support_path}")
    return code


def conformance(args: argparse.Namespace) -> int:
    from .conformance import run_conformance
    from .diagnostics import format_report

    style = Style.for_stream(json_output=bool(args.json))
    if style is not None:
        from .cli_steps import Steps
        from .cli_views import render_conformance
        style.header("conformance")
        with Steps(style).step("Ran the Computer Use checks", failed_label="Run the Computer Use checks"):
            report = run_conformance(live=bool(args.live))
        render_conformance(style, report)
        return 0 if report.get("ok") else 1
    report = run_conformance(live=bool(args.live))
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_report(report, title="Mac MCP Computer Use Conformance"))
        metrics = report.get("metrics", {})
        print(f"Deterministic pass rate: {metrics.get('deterministic_pass_rate', 0)}% | focus regressions={metrics.get('focus_safety_regressions', 0)} | duration={report.get('duration_ms', 0)}ms")
    return 0 if report.get("ok") else 1


def update(args: argparse.Namespace) -> int:
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if style is not None:
        return _update_themed(args, style)
    try:
        if install_kind() == "pkg":
            payload = package_update_payload(check_only=bool(args.check))
            if args.check and getattr(args, "json", False):
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(f"[mac-mcp update] {payload['message']} {payload['download_url']}")
            return 0 if args.check else 3
        validate_update_state()
        if args.check:
            info = check_update(args.repo, args.runtime, branch=args.branch, remote=args.remote, fetch=True)
            print(format_check_json(info) if getattr(args, "json", False) else format_check(info))
            return 2 if info.dirty else 0
        if getattr(args, "json", False):
            print("mac-mcp update: --json requires --check", file=sys.stderr)
            return 2
        repo, runtime = resolve_update_paths(args.repo, args.runtime)
        info = check_update(repo, runtime, branch=args.branch, remote=args.remote, fetch=True)
        if info.dirty:
            raise UpdateError("Repository has local changes. Commit or stash them before updating.")
        if not info.update_available:
            print("[mac-mcp update] Mac MCP is already up to date.")
            return 0
        blocker = secure_bootstrap_update_blocker(runtime)
        if blocker is not None:
            raise UpdateError(
                str(blocker.get("summary") or "Secure bootstrap migration is required before updating.")
            )
        payload, proc = launch_detached_update(
            info,
            repo,
            runtime,
            branch=args.branch,
            remote=args.remote,
            launchd_label=os.getenv("MAC_MCP_LAUNCHD_LABEL", "mac-mcp-uvicorn"),
            skip_restart=getattr(args, "skip_restart", False),
            skip_deps=getattr(args, "skip_deps", False),
        )
        print(
            f"[mac-mcp update] Detached updater started (pid {payload['updater_pid']}).",
            flush=True,
        )
        return_code = proc.wait()
        log_path = Path(str(payload["log_path"]))
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
        if log_text:
            print(log_text, end="" if log_text.endswith("\n") else "\n")
        return 0 if return_code == 0 else 1
    except UpdateInProgress as exc:
        print(f"mac-mcp update: {exc}", file=sys.stderr)
        if exc.state.get("log_path"):
            print(f"mac-mcp update: follow it in {exc.state['log_path']}", file=sys.stderr)
        return 1
    except UpdateError as exc:
        print(f"mac-mcp update failed: {exc}", file=sys.stderr)
        return 1


def _update_themed(args: argparse.Namespace, style) -> int:
    """update in a terminal: the check as a screen, the update as steps with its log's end on failure."""
    from .cli_steps import Steps
    from .cli_views import render_error, render_package_update, render_update_check

    headed = False  # whether a screen's header is drawn, so an error does not draw a second
    try:
        if install_kind() == "pkg":
            render_package_update(style, package_update_payload(check_only=bool(args.check)))
            return 0 if args.check else 3
        validate_update_state()
        if args.check:
            info = check_update(args.repo, args.runtime, branch=args.branch, remote=args.remote, fetch=True)
            render_update_check(style, info)
            return 2 if info.dirty else 0
        style.header("update")
        headed = True
        steps = Steps(style)
        with steps.step("Checked for a verified release", failed_label="Check for a verified release") as state:
            repo, runtime = resolve_update_paths(args.repo, args.runtime)
            info = check_update(repo, runtime, branch=args.branch, remote=args.remote, fetch=True)
            if info.dirty:
                raise UpdateError("Repository has local changes. Commit or stash them before updating.")
            blocker = secure_bootstrap_update_blocker(runtime) if info.update_available else None
            if blocker is not None:
                raise UpdateError(str(blocker.get("summary") or "Secure bootstrap migration is required before updating."))
        if not info.update_available:
            style.summary("ok", "Mac MCP is already up to date.")
            return 0
        log_path: Optional[Path] = None
        with steps.step("Installed the update", failed_label="Install the update") as state:
            payload, proc = launch_detached_update(
                info, repo, runtime, branch=args.branch, remote=args.remote,
                launchd_label=os.getenv("MAC_MCP_LAUNCHD_LABEL", "mac-mcp-uvicorn"),
                skip_restart=getattr(args, "skip_restart", False), skip_deps=getattr(args, "skip_deps", False),
            )
            log_path = Path(str(payload["log_path"]))
            try:
                return_code = proc.wait()
            except KeyboardInterrupt:
                # The updater has its own session: it goes on, and restarts Mac MCP when done.
                state.label = f"The update keeps running in the background (pid {payload.get('updater_pid')})"
                return_code = None
            if return_code not in (0, None):
                state.ok = False
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines = []
                # The end of the update log says what went wrong.
                print("\n".join(lines[-15:]))
        if not state.ok:
            style.summary("error", "The update did not finish", next_command="mac-mcp logs update")
            return 1
        if return_code is None:
            style.summary("warn", "Stopped watching; Mac MCP finishes the update on its own", next_command="mac-mcp logs update")
            return 130
        style.summary("ok", f"Updated to {info.release_id or info.target_commit[:8]}", note=f"log {home_path(log_path)}")
        return 0
    except UpdateInProgress as exc:
        where = f" Follow it in {exc.state['log_path']}." if exc.state.get("log_path") else ""
        if headed:
            style.summary("error", f"{exc}{where}")
        else:
            render_error(style, "update", f"{exc}{where}")
        return 1
    except UpdateError as exc:
        if headed:
            style.summary("error", f"Update failed: {exc}")
        else:
            render_error(style, "update", f"Update failed: {exc}")
        return 1


def _recipe_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Call the local recipe launcher API with the dashboard token."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as UrlRequest

    port = _default_port()
    try:
        token = dashboard_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return 3, {"error": "dashboard_token_missing", "message": "Mac MCP has not been started on this Mac yet."}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = UrlRequest(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=90) as response:  # noqa: S310 - fixed loopback URL
            return 0, json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            body = {}
        return 1, {"error": body.get("error") or f"http_{exc.code}", **body}
    except (URLError, OSError) as exc:
        return 3, {"error": "server_unreachable", "message": f"Mac MCP is not running ({exc}). Start it with: mac-mcp start"}


def logins(args: argparse.Namespace) -> int:
    """Manage saved logins for browser_sign_in. Passwords are read from stdin and never printed."""
    from . import login_vault

    command = args.logins_command
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if style is not None:
        return _logins_themed(args, style)
    try:
        if command == "list":
            rows = login_vault.list_logins()
            if args.json:
                print(json.dumps({"ok": True, "logins": rows}, ensure_ascii=False))
            else:
                for row in rows:
                    scope = "  (and subdomains)" if row.get("subdomains") else ""
                    print(f"{row['id']}  {row['site']}  {row['username']}{scope}")
                if not rows:
                    print("No saved logins.")
            return 0
        if command == "add":
            password = sys.stdin.readline()
            password = password[:-1] if password.endswith("\n") else password
            row = login_vault.add_login(args.site, args.username, password, label=args.label,
                                        subdomains=True if args.subdomains else None)
            password = ""
            print(json.dumps({"ok": True, "login": row}, ensure_ascii=False) if args.json else f"Saved login for {row['site']}.")
            return 0
        if command == "subdomains":
            row = login_vault.set_subdomains(args.login_id, args.state == "on")
            print(json.dumps({"ok": True, "login": row}, ensure_ascii=False) if args.json
                  else f"{row['site']}: {'also on its subdomains' if row['subdomains'] else 'this site only'}.")
            return 0
        if command == "remove":
            removed = login_vault.remove_login(args.login_id)
            print(json.dumps({"ok": removed}) if args.json else ("Removed." if removed else "No such saved login."))
            return 0 if removed else 1
        if command == "import-preview":
            items = login_vault.preview_import(args.file)
            print(json.dumps({"ok": True, "items": items}, ensure_ascii=False) if args.json
                  else "\n".join(f"{item['index']}  {item['site']}  {item['username']}" for item in items) or "No usable logins.")
            return 0
        if command == "import":
            indexes = None if args.select == "all" else [int(part) for part in args.select.split(",") if part.strip()]
            result = login_vault.import_logins(args.file, indexes, delete_file=args.delete_file)
            print(json.dumps({"ok": True, **result}) if args.json
                  else f"Imported {result['imported']} login(s), skipped {result['skipped']}"
                       + ("; export file deleted." if result["file_deleted"] else "."))
            return 0
    except (login_vault.VaultError, ValueError) as exc:
        code = getattr(exc, "code", "invalid_arguments")
        print(json.dumps({"ok": False, "error": code, "message": str(exc)}) if getattr(args, "json", False)
              else f"mac-mcp logins: {exc}", file=sys.stderr if not getattr(args, "json", False) else sys.stdout)
        return 1
    return 2


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _logins_themed(args: argparse.Namespace, style) -> int:
    """logins in a terminal: sites and usernames only; a password typed here is read hidden."""
    import shlex

    from . import login_vault

    command = args.logins_command
    style.header("logins")
    try:
        if command == "list":
            rows = login_vault.list_logins()
            if not rows:
                style.summary("info", "No saved logins", note="add one in Settings › Saved Logins", gap=False)
                return 0
            style.rows([(None, row["site"], row["username"],
                         row["id"] + (" · also subdomains" if row.get("subdomains") else "")) for row in rows])
            style.summary("ok", _plural(len(rows), "saved login"))
            return 0
        if command == "add":
            if sys.stdin.isatty():
                import getpass
                password = getpass.getpass(f"{INDENT}Password for {args.username} at {args.site}: ")
                style.write()
            else:
                password = sys.stdin.readline()
                password = password[:-1] if password.endswith("\n") else password
            try:
                row = login_vault.add_login(args.site, args.username, password, label=args.label,
                                            subdomains=True if args.subdomains else None)
            finally:
                password = ""
            style.summary("ok", f"Saved the login for {row['site']}", gap=False)
            return 0
        if command == "subdomains":
            row = login_vault.set_subdomains(args.login_id, args.state == "on")
            scope = "also on its subdomains" if row["subdomains"] else "this site only"
            style.summary("ok", f"{row['site']}: {scope}", gap=False)
            return 0
        if command == "remove":
            if login_vault.remove_login(args.login_id):
                style.summary("ok", "Removed the saved login", gap=False)
                return 0
            style.summary("error", "No such saved login", next_command="mac-mcp logins list", gap=False)
            return 1
        if command == "import-preview":
            items = login_vault.preview_import(args.file)
            if not items:
                style.summary("info", "No usable logins", gap=False)
                return 0
            style.rows([(None, str(item["index"]), item["site"], item["username"]) for item in items])
            style.summary("info", f"{_plural(len(items), 'login')} can be imported",
                          next_command=f"mac-mcp logins import {shlex.quote(str(args.file))}")
            return 0
        if command == "import":
            indexes = None if args.select == "all" else [int(part) for part in args.select.split(",") if part.strip()]
            result = login_vault.import_logins(args.file, indexes, delete_file=args.delete_file)
            style.summary("ok", f"Imported {_plural(result['imported'], 'login')} · skipped {result['skipped']}",
                          note="export file deleted" if result["file_deleted"] else "", gap=False)
            return 0
    except (login_vault.VaultError, ValueError) as exc:
        style.summary("error", str(exc), gap=False)
        return 1
    return 2


def _print_catalog_review(report: dict) -> None:
    pack = report["pack"]
    print(f"{pack['name']} ({pack['id']} {pack['version']}, {pack['kind']}) by {pack['publisher']}")
    print(f"  {pack['description']}")
    print(f"  signed by {report.get('signer')}; catalog {str(report.get('catalog_digest'))[:16]}")
    installed = report.get("installed_version")
    print(f"  action: {report['action']}" + (f" (installed {installed})" if installed else ""))
    print(f"  declared capabilities: {', '.join(report['capabilities']) or 'none'}")
    effective = report.get("effective") or {}
    if effective.get("tools"):
        print(f"  recipe tools: {', '.join(effective['tools'])}")
    changes = report.get("permission_changes")
    if changes and (changes["added"] or changes["removed"]):
        print(f"  permission changes: +{', '.join(changes['added']) or '-'}  -{', '.join(changes['removed']) or '-'}")
    for item in report.get("files") or []:
        print(f"  file {item['path']}  {item['size']} B  sha256 {item['sha256'][:16]}")
    if report.get("scripts_held"):
        print(f"  scripts held back: {', '.join(report['scripts_held'])} ({report.get('script_note')})")
    if report.get("note"):
        print(f"  {report['note']}")
    if report.get("blocked"):
        print(f"  BLOCKED: {report['blocked']}")


def catalog_command(args: argparse.Namespace) -> int:
    """Review and install signed skill and recipe packs (#146). Installing needs --yes after review."""
    from . import catalog

    command = args.catalog_command
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if style is not None:
        return _catalog_themed(args, style)
    try:
        if command == "list":
            listed = catalog.list_packs()
            if args.json:
                print(json.dumps(listed, ensure_ascii=False))
            else:
                print(f"Catalog signed by {listed['signer']}")
                for pack in listed["packs"]:
                    state = (f"installed {pack['installed_version']}" + (", update available" if pack["update_available"] else "")
                             if pack["installed_version"] else "not installed")
                    print(f"{pack['id']}  {pack['version']}  {pack['kind']}  {state}  - {pack['name']}")
            return 0
        if command == "review":
            report = catalog.review(args.pack_id)
            print(json.dumps(report, ensure_ascii=False)) if args.json else _print_catalog_review(report)
            return 0
        if command == "install":
            if not args.yes:
                report = catalog.review(args.pack_id)
                if args.json:
                    print(json.dumps({"ok": False, "error": "CONFIRM_REQUIRED", "review": report}, ensure_ascii=False))
                else:
                    _print_catalog_review(report)
                    print("Nothing installed. Run again with --yes to install this exact version.")
                return 1
            done = catalog.install(args.pack_id, confirm=True)
            print(json.dumps(done, ensure_ascii=False) if args.json
                  else f"Installed {done['pack_id']} {done['version']}." + (" Recipe is a draft: activate it after review."
                                                                           if done.get("recipe_id") else ""))
            return 0
        if command == "enable-script":
            if not args.yes:
                try:
                    catalog.enable_script(args.pack_id, args.path)
                except catalog.CatalogError as exc:
                    if exc.code != "CONFIRM_REQUIRED":
                        raise
                    script = exc.extra["script"]
                    print(json.dumps({"ok": False, "error": exc.code, "script": script}, ensure_ascii=False) if args.json
                          else f"{script['path']}  sha256 {script['sha256']}\n---\n{script['content']}\n---\n"
                               "Nothing enabled. Run again with --yes to install this script; it can run anything.")
                    return 1
            done = catalog.enable_script(args.pack_id, args.path, confirm=True)
            print(json.dumps(done) if args.json else f"Enabled {done['path']} for {done['pack_id']}.")
            return 0
    except catalog.CatalogError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, default=str) if getattr(args, "json", False)
              else f"mac-mcp catalog: {exc}", file=sys.stdout if getattr(args, "json", False) else sys.stderr)
        return 1
    return 2


def _catalog_themed(args: argparse.Namespace, style) -> int:
    from . import catalog
    from .cli_steps import Steps
    from .cli_views import dim_lines, render_catalog_list, render_catalog_review, render_error, render_script_review

    command = args.catalog_command
    drawn = False
    try:
        if command == "list":
            render_catalog_list(style, catalog.list_packs())
            return 0
        if command == "review" or (command == "install" and not args.yes):
            report = catalog.review(args.pack_id)
            render_catalog_review(style, report)
            if report.get("blocked"):
                style.summary("error", f"Blocked: {report['blocked']}")
                return 0 if command == "review" else 1
            if command == "review":
                style.summary("ok", f"Reviewed {report['pack']['id']} {report['pack']['version']}")
                return 0
            style.summary("warn", "Nothing installed", next_command=f"mac-mcp catalog install {args.pack_id} --yes")
            return 1
        if command == "install":
            style.header("catalog")
            drawn = True
            with Steps(style).step(f"Installed {args.pack_id}", failed_label=f"Install {args.pack_id}") as state:
                done = catalog.install(args.pack_id, confirm=True)
                state.label = f"Installed {done['pack_id']} {done['version']}"
            style.summary("ok", f"{done['pack_id']} {done['version']} is installed")
            if done.get("recipe_id"):
                dim_lines(style, "The recipe is a draft: activate it after review.", indent="    ")
            return 0
        if command == "enable-script":
            if not args.yes:
                try:
                    catalog.enable_script(args.pack_id, args.path)
                except catalog.CatalogError as exc:
                    if exc.code != "CONFIRM_REQUIRED":
                        raise
                    render_script_review(style, exc.extra["script"],
                                         f"mac-mcp catalog enable-script {args.pack_id} {args.path} --yes")
                    return 1
            done = catalog.enable_script(args.pack_id, args.path, confirm=True)
            style.header("catalog")
            style.summary("ok", f"Enabled {done['path']} for {done['pack_id']}", gap=False)
            return 0
    except catalog.CatalogError as exc:
        if drawn:
            style.summary("error", str(exc))
        else:
            render_error(style, "catalog", str(exc))
        return 1
    return 2


def recipe(args: argparse.Namespace) -> int:
    """List or run saved recipes for Shortcuts, Raycast and scripts."""
    _load_env()
    style = Style.for_stream(json_output=bool(getattr(args, "json", False)))
    if style is not None:
        return _recipe_themed(args, style)
    if args.recipe_command == "list":
        code, body = _recipe_request("GET", "/dashboard/api/recipes")
        if args.json:
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return code
        if code:
            print(body.get("message") or body.get("error"), file=sys.stderr)
            return code
        items = body.get("recipes") or []
        if not items:
            print("No active recipes. Save one with computer_plan(save_as_recipe=...) and activate it.")
        for item in items:
            params = ", ".join(
                f"{name}:{spec.get('type')}{'' if spec.get('required') and 'default' not in spec else '?'}"
                for name, spec in (item.get("parameters") or {}).items()
            )
            print(f"{item['recipe_id']}  [{item.get('status')}]  {item.get('name')}" + (f"  ({params})" if params else ""))
        return 0
    values: dict = {}
    for pair in args.param or []:
        if "=" not in pair:
            print(f"--param must be name=value: {pair}", file=sys.stderr)
            return 1
        key, value = pair.split("=", 1)
        values[key.strip()] = value
    code, body = _recipe_request("POST", "/dashboard/api/recipes/run", {"recipe_id": args.recipe_id, "values": values})
    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
    elif code == 0 and body.get("ok"):
        print(f"Recipe {body.get('name') or args.recipe_id} completed ({body.get('steps_executed')} steps).")
    else:
        print(f"Recipe did not complete: {body.get('status') or body.get('error')}: {body.get('message') or ''}".rstrip(": "),
              file=sys.stderr)
    if code:
        return code
    if body.get("ok"):
        return 0
    return 2 if body.get("status") == "approval_required" else 1


def _recipe_params(item: dict) -> str:
    return ", ".join(
        f"{name}:{spec.get('type')}{'' if spec.get('required') and 'default' not in spec else '?'}"
        for name, spec in (item.get("parameters") or {}).items()
    )


def _recipe_unreachable(style, body: dict, *, drawn: bool) -> None:
    """The server is down or was never started: say so and name the command that starts it."""
    text = ("Mac MCP has not been started on this Mac yet" if body.get("error") == "dashboard_token_missing"
            else "Mac MCP is not running")
    if drawn:
        style.summary("error", text, next_command="mac-mcp start")
    else:
        from .cli_views import render_error
        render_error(style, "recipe", text, next_command="mac-mcp start")


def _recipe_themed(args: argparse.Namespace, style) -> int:
    from .cli_steps import Steps
    from .cli_views import dim_lines, render_error

    if args.recipe_command == "list":
        code, body = _recipe_request("GET", "/dashboard/api/recipes")
        if code == 3:
            _recipe_unreachable(style, body, drawn=False)
            return code
        if code:
            render_error(style, "recipe", str(body.get("message") or body.get("error")))
            return code
        items = body.get("recipes") or []
        style.header("recipe")
        if not items:
            style.summary("info", "No active recipes", gap=False)
            dim_lines(style, "Save one with computer_plan(save_as_recipe=…), then activate it.", indent="    ")
            return 0
        style.rows([("ok" if item.get("status") == "active" else "warn", str(item.get("name") or item["recipe_id"]),
                     item["recipe_id"], _recipe_params(item)) for item in items])
        style.summary("ok", _plural(len(items), "recipe"))
        return 0
    values: dict = {}
    for pair in args.param or []:
        if "=" not in pair:
            render_error(style, "recipe", f"--param must be name=value: {pair}")
            return 1
        key, value = pair.split("=", 1)
        values[key.strip()] = value
    style.header("recipe")
    steps = Steps(style)
    code, body = 0, {}
    with steps.step(f"Ran {args.recipe_id}", failed_label=f"Run {args.recipe_id}") as state:
        code, body = _recipe_request("POST", "/dashboard/api/recipes/run", {"recipe_id": args.recipe_id, "values": values})
        if code == 0 and body.get("ok"):
            pass
        elif body.get("status") == "approval_required":
            state.label = f"Ran {args.recipe_id} up to a step that needs approval"
        else:
            state.ok = False
            if code != 3:
                print(f"{body.get('status') or body.get('error')}: {body.get('message') or ''}".rstrip(": "))
    name = body.get("name") or args.recipe_id
    if code == 3:
        _recipe_unreachable(style, body, drawn=True)
        return code
    if code == 0 and body.get("ok"):
        style.summary("ok", f"{name} completed", note=_plural(int(body.get("steps_executed") or 0), "step"))
        return 0
    if body.get("status") == "approval_required":
        style.summary("warn", f"{name} needs approval first")
        if body.get("message"):
            dim_lines(style, str(body["message"]), indent="    ")
        return code or 2
    style.summary("error", "The recipe did not complete")
    return code or 1


def logs(args: argparse.Namespace) -> int:
    """Print recent, redacted lines from one Mac MCP log."""
    from .security import BASE_DIR

    paths = managed_logs(BASE_DIR)
    if args.component == "update":
        update_dir = update_logs_dir()
        candidates = sorted(update_dir.glob("upd_*.log"), key=lambda item: item.stat().st_mtime) if update_dir.is_dir() else []
        path = candidates[-1] if candidates else update_dir / "upd_none.log"
    else:
        path = paths[args.component]
    limits = log_limits()
    style = Style.for_stream()
    if style is not None:
        return _logs_themed(args, style, paths, path, limits)
    if args.list:
        for name, item in {**paths, "update": update_logs_dir()}.items():
            try:
                size = item.stat().st_size if item.is_file() else sum(f.stat().st_size for f in item.glob("*.log"))
            except OSError:
                size = 0
            print(f"{name:12} {size / 1024 / 1024:8.1f} MB  {item}")
        print(f"bounds: {limits['max_bytes'] // (1024 * 1024)} MB x {limits['backups'] + 1} files per log, "
              f"audit 5 MB x 4, newest {limits['update_logs_kept']} update logs")
        return 0
    if not path.exists():
        print(f"No {args.component} log yet ({path}).", file=sys.stderr)
        return 1
    print(f"== {path} (last {args.lines} lines, secrets redacted)")
    print(tail_log(path, args.lines))
    return 0


def _logs_themed(args: argparse.Namespace, style, paths: dict, path: Path, limits: dict) -> int:
    """logs in a terminal: a header and one line about the log, then its lines raw (copyable)."""
    if args.list:
        rows, total = [], 0
        for name, item in {**paths, "update": update_logs_dir()}.items():
            try:
                size = item.stat().st_size if item.is_file() else sum(f.stat().st_size for f in item.glob("*.log"))
            except OSError:
                size = 0
            total += size
            rows.append((None, name, f"{size / 1024 / 1024:.1f} MB", home_path(item)))
        style.header("logs")
        style.rows(rows)
        style.write()
        kept = (f"Kept: {limits['max_bytes'] // (1024 * 1024)} MB × {limits['backups'] + 1} files per log, "
                f"audit 5 MB × 4, newest {limits['update_logs_kept']} update logs")
        for line in textwrap.wrap(kept, style.width - len(INDENT)):
            style.write(INDENT + style.paint(line, dim=True))
        style.summary("ok", f"{len(rows)} logs · {total / 1024 / 1024:.1f} MB in all")
        return 0
    if not path.exists():
        from .cli_views import render_error
        render_error(style, "logs", f"No {args.component} log yet ({home_path(path)})")
        return 1
    text = tail_log(path, args.lines)
    style.header("logs")
    about = f"{args.component} · {home_path(path)} · last {args.lines} lines · secrets redacted"
    for line in textwrap.wrap(about, style.width - len(INDENT), break_long_words=False):
        style.write(INDENT + style.paint(line, dim=True))
    if not text.strip():
        style.summary("ok", f"The {args.component} log is empty.")
        return 0
    style.write()
    # Raw: no colour, indent or wrapping, so lines can be copied or searched as they are.
    style.stream.write(text.rstrip("\n") + "\n")
    count = len(text.splitlines())
    style.summary("ok", f"{count} line{'s' if count != 1 else ''}")
    return 0


def _themed_command_help(style: Style, parser: argparse.ArgumentParser) -> None:
    from .cli_views import render_command_help
    render_command_help(style, parser)


ThemedArgumentParser.themed_help = staticmethod(_themed_command_help)


def build_parser() -> argparse.ArgumentParser:
    # prog is fixed so the help reads the same whatever argv[0] is (the installed CLI's is mac-mcp).
    parser = ThemedArgumentParser(prog="mac-mcp", description="Manage the Mac MCP local server.")
    parser.add_argument("--version", action="version", version=f"mac-mcp {__version__}")
    # Subcommands (and theirs) are themed parsers too: argparse builds them with parser_class.
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ThemedArgumentParser)

    def add_start_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default=os.getenv("MAC_MCP_HOST", DEFAULT_HOST))
        p.add_argument("--port", default=_default_port(), type=int)
        p.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode for development.")
        p.add_argument("--public-mode", choices=("none", "ngrok", "cloudflare", "custom"), default=None, help="Override the configured public endpoint mode for this run.")
        p.add_argument("--public-url", default=None, help="Public HTTPS MCP endpoint. Supplying it without --public-mode implies custom mode.")
        p.add_argument("--cloudflare-tunnel", default=None, help="Cloudflare Tunnel name/UUID for cloudflare mode.")
        p.add_argument("--cloudflare-token-file", default=None, help="Owner-controlled Cloudflare Tunnel token file; avoids exposing the token in process arguments.")
        p.add_argument("--cloudflared-bin", default=None, help="Path or command name for cloudflared. Defaults to cloudflared.")
        p.add_argument("--ngrok", action="store_true", help="Legacy alias for --public-mode ngrok.")
        p.add_argument("--ngrok-domain", default=None, help="Override NGROK_DOMAIN for this run.")
        p.add_argument("--ngrok-bin", default=None, help="Path or command name for the ngrok binary. Defaults to ngrok.")

    p_start = sub.add_parser("start", help="Start the local server and its configured public endpoint mode.")
    add_start_flags(p_start)
    p_start.set_defaults(func=start)

    p_stop = sub.add_parser("stop", help="Stop the local server and any managed public tunnel.")
    p_stop.add_argument("--timeout", type=float, default=STOP_TIMEOUT_S)
    p_stop.add_argument("--force", action="store_true")
    # Internal: the supervisor's own stop before a restart keeps the "running" intent.
    p_stop.add_argument("--keep-intent", action="store_true", help=argparse.SUPPRESS)
    p_stop.set_defaults(func=stop)

    p_restart = sub.add_parser("restart", help="Restart the local server and apply the configured public endpoint mode.")
    add_start_flags(p_restart)
    p_restart.add_argument("--timeout", type=float, default=STOP_TIMEOUT_S)
    p_restart.add_argument(
        "--wait", action="store_true",
        help="Wait until the restart finishes; exit 0 only when the server restarted healthy.",
    )
    p_restart.set_defaults(func=restart)

    p_status = sub.add_parser("status", help="Show server and public endpoint status. Exits 0 healthy, 1 server down or not answering, 2 selected public endpoint unavailable or misconfigured.")
    p_status.add_argument("--local-only", action="store_true", help="Exit 0 whenever the local server is running, ignoring the public endpoint.")
    p_status.add_argument("--json", action="store_true", help="Print state, health and remediation as JSON (same exit codes).")
    p_status.set_defaults(func=status)

    p_connect = sub.add_parser(
        "connect-config",
        help="Generate a secret-safe MCP connection snippet for a supported client.",
    )
    p_connect.add_argument("--client", required=True, choices=SUPPORTED_CLIENTS + tuple(UNGUIDED_CLIENTS))
    p_connect.add_argument(
        "--endpoint",
        choices=SUPPORTED_ENDPOINTS,
        default="auto",
        help="Endpoint choice. auto uses public for ChatGPT and local for Codex, OpenCode and Claude Code.",
    )
    p_connect.add_argument(
        "--name",
        default=DEFAULT_SERVER_NAME,
        help="Client-side MCP server name (letters, numbers, underscores, hyphens).",
    )
    p_connect.add_argument(
        "--auth-env",
        default=DEFAULT_AUTH_ENV,
        help="Client environment variable that will hold the API key; its value is never printed.",
    )
    p_connect.set_defaults(func=connect_config)

    p_connect_app = sub.add_parser(
        "connect",
        help="Add Mac MCP to an AI app on this Mac in one step (ChatGPT: copy its connector address).",
    )
    p_connect_app.add_argument("--client", required=True, choices=("claude-code", "codex", "claude-desktop",
                                                                   "opencode", "kimi", "grok", "antigravity", "pi",
                                                                   "cline", "kilo", "goose", "chatgpt"))
    p_connect_app.add_argument("--json", action="store_true", help="Print the outcome as JSON.")
    p_connect_app.set_defaults(func=connect_app)

    p_clients = sub.add_parser("clients", help="List the AI apps on this Mac and whether Mac MCP is added.")
    p_clients.add_argument("--json", action="store_true", help="Print as JSON.")
    p_clients.set_defaults(func=clients)

    p_privacy = sub.add_parser("privacy", help="Mac MCP's stored data (Settings > Privacy).")
    privacy_sub = p_privacy.add_subparsers(dest="privacy_command", required=True)
    p_privacy_delete = privacy_sub.add_parser("delete", help="Preview, or move stores to the Trash with --yes.")
    p_privacy_delete.add_argument("--store", action="append", help="A store key; repeat for more.")
    p_privacy_delete.add_argument("--all", action="store_true", help="Every store that can be deleted.")
    p_privacy_delete.add_argument("--yes", action="store_true", help="Move them to the Trash.")
    p_privacy_delete.add_argument("--json", action="store_true", help="Print the outcome as JSON.")
    p_privacy_delete.set_defaults(func=privacy_delete)

    p_stdio = sub.add_parser(
        "stdio",
        help="Speak MCP over stdin/stdout, forwarding to the running server (what one-click app setup runs).",
    )
    p_stdio.set_defaults(func=stdio)

    p_dashboard = sub.add_parser("dashboard", help="Open the local Mac MCP operations dashboard.")
    p_dashboard.set_defaults(func=dashboard)

    p_credential = sub.add_parser("credential", help="Manage local provider credentials without exposing secret values in process arguments.")
    p_credential.add_argument("provider", choices=("cloudflare",))
    p_credential.add_argument("action", choices=("status", "save", "remove"))
    p_credential.set_defaults(func=credential)

    p_doctor = sub.add_parser("doctor", help="Diagnose local Mac MCP runtime, permissions, dependencies, and companions.")
    p_doctor.add_argument("--json", action="store_true", help="Print the diagnostic report as JSON.")
    p_doctor.add_argument("--support-bundle", nargs="?", const="", default=None, metavar="PATH", help="Write an owner-only redacted support bundle. Optional PATH overrides the default location.")
    p_doctor.add_argument("--verbose", action="store_true", help="In a terminal, list every check.")
    p_doctor.add_argument("--local-only", action="store_true", help="Exit 0 when the local runtime is healthy even if the selected public endpoint is unreachable.")
    p_doctor.set_defaults(func=doctor)

    p_conformance = sub.add_parser("conformance", help="Run deterministic Computer Use contract/regression checks.")
    p_conformance.add_argument("--json", action="store_true", help="Print the conformance report as JSON.")
    p_conformance.add_argument("--live", action="store_true", help="Also include read-only live Mac/companion health checks.")
    p_conformance.set_defaults(func=conformance)

    p_recipe = sub.add_parser("recipe", help="List or run saved, activated recipes (for Shortcuts, Raycast and scripts).")
    recipe_sub = p_recipe.add_subparsers(dest="recipe_command", required=True)
    p_recipe_list = recipe_sub.add_parser("list", help="List active and paused recipes with their parameters.")
    p_recipe_list.add_argument("--json", action="store_true")
    p_recipe_run = recipe_sub.add_parser("run", help="Run a recipe. Exit 0 done, 1 failed, 2 needs approval, 3 server not running.")
    p_recipe_run.add_argument("recipe_id")
    p_recipe_run.add_argument("--param", action="append", metavar="NAME=VALUE", help="Recipe parameter; repeat for each.")
    p_recipe_run.add_argument("--json", action="store_true")
    p_recipe.set_defaults(func=recipe)

    p_logins = sub.add_parser("logins", help="Saved logins that agents can use with browser_sign_in without seeing them.")
    logins_sub = p_logins.add_subparsers(dest="logins_command", required=True)
    p_logins_list = logins_sub.add_parser("list", help="List saved logins (sites and usernames only).")
    p_logins_list.add_argument("--json", action="store_true")
    p_logins_add = logins_sub.add_parser("add", help="Save a login; the password is read from stdin.")
    p_logins_add.add_argument("--site", required=True, help="Site URL or host name, e.g. https://github.com")
    p_logins_add.add_argument("--username", required=True)
    p_logins_add.add_argument("--label")
    p_logins_add.add_argument("--subdomains", action="store_true",
                              help="Also use it on the site's subdomains (never on shared hosting such as github.io).")
    p_logins_add.add_argument("--json", action="store_true")
    p_logins_scope = logins_sub.add_parser("subdomains", help="Allow or stop a saved login on its site's subdomains.")
    p_logins_scope.add_argument("login_id")
    p_logins_scope.add_argument("state", choices=["on", "off"])
    p_logins_scope.add_argument("--json", action="store_true")
    p_logins_remove = logins_sub.add_parser("remove", help="Delete a saved login and its Keychain item.")
    p_logins_remove.add_argument("login_id")
    p_logins_remove.add_argument("--json", action="store_true")
    p_logins_preview = logins_sub.add_parser("import-preview", help="Show the logins in a Passwords app export (no passwords).")
    p_logins_preview.add_argument("file")
    p_logins_preview.add_argument("--json", action="store_true")
    p_logins_import = logins_sub.add_parser("import", help="Save chosen logins from a Passwords app export.")
    p_logins_import.add_argument("file")
    p_logins_import.add_argument("--select", default="all", help="Comma-separated row indexes from import-preview, or all.")
    p_logins_import.add_argument("--delete-file", action="store_true", help="Delete the export file after importing.")
    p_logins_import.add_argument("--json", action="store_true")
    p_logins.set_defaults(func=logins)

    p_catalog = sub.add_parser("catalog", help="Review and install signed skill and recipe packs.")
    catalog_sub = p_catalog.add_subparsers(dest="catalog_command", required=True)
    p_catalog_list = catalog_sub.add_parser("list", help="List catalog packs and what is installed.")
    p_catalog_list.add_argument("--json", action="store_true")
    p_catalog_review = catalog_sub.add_parser("review", help="Show a pack: provenance, capabilities, files and changes.")
    p_catalog_review.add_argument("pack_id")
    p_catalog_review.add_argument("--json", action="store_true")
    p_catalog_install = catalog_sub.add_parser("install", help="Install or update a pack after review (needs --yes).")
    p_catalog_install.add_argument("pack_id")
    p_catalog_install.add_argument("--yes", action="store_true", help="Install the reviewed version.")
    p_catalog_install.add_argument("--json", action="store_true")
    p_catalog_script = catalog_sub.add_parser("enable-script", help="Install one held script of a skill pack (needs --yes).")
    p_catalog_script.add_argument("pack_id")
    p_catalog_script.add_argument("path", help="Script path in the pack, e.g. scripts/check.sh")
    p_catalog_script.add_argument("--yes", action="store_true", help="Enable this exact script.")
    p_catalog_script.add_argument("--json", action="store_true")
    p_catalog.set_defaults(func=catalog_command)

    p_logs = sub.add_parser("logs", help="Show recent, redacted lines from a Mac MCP log, or --list sizes and bounds.")
    p_logs.add_argument("component", nargs="?", default="server", choices=("server", "cloudflared", "ngrok", "audit", "update"))
    p_logs.add_argument("-n", "--lines", type=int, default=80, help="Number of lines (max 2000).")
    p_logs.add_argument("--list", action="store_true", help="List every log with its size and the retention bounds.")
    p_logs.set_defaults(func=logs)

    p_uninstall = sub.add_parser("uninstall", help="Preview or remove Mac MCP; removed parts go to the Trash.")
    p_uninstall.add_argument("--yes", action="store_true", help="Do it (without this flag nothing changes).")
    p_uninstall.add_argument("--include-state", action="store_true",
                             help="Also move ~/.mac-mcp: your settings, keys, tokens, memory and logs.")
    p_uninstall.add_argument("--include-source", action="store_true", help="Also move the source checkout.")
    p_uninstall.set_defaults(func=uninstall)

    p_repair = sub.add_parser("repair", help="Check and fix the parts Mac MCP manages (preview unless --yes).")
    p_repair.add_argument("--yes", action="store_true", help="Apply the fixes.")
    p_repair.set_defaults(func=repair)

    p_autostart = sub.add_parser("autostart", help="Start Mac MCP when you log in: on, off, or show the setting.")
    p_autostart.add_argument("state", nargs="?", choices=("on", "off"), help="Turn start at login on or off.")
    p_autostart.add_argument("--json", action="store_true", help="Print the setting as JSON.")
    p_autostart.set_defaults(func=autostart)

    p_update = sub.add_parser("update", help="Update Mac MCP to the latest verified stable release checkpoint.")
    p_update.add_argument("--check", action="store_true", help="Check for updates without changing files.")
    p_update.add_argument("--json", action="store_true", help="Print the update check as JSON (requires --check).")
    p_update.add_argument("--repo", default=None, help="Git repository path. Defaults to ~/Projects/mac-mcp.")
    p_update.add_argument("--runtime", default=None, help="Runtime path. Defaults to ~/mac-mcp.")
    p_update.add_argument("--branch", default="main", help="Git branch to follow. Defaults to main.")
    p_update.add_argument("--remote", default="origin", help="Git remote to follow. Defaults to origin.")
    p_update.add_argument("--skip-restart", action="store_true", help=argparse.SUPPRESS)
    p_update.add_argument("--skip-deps", action="store_true", help=argparse.SUPPRESS)
    p_update.set_defaults(func=update)

    _remember_summaries(parser)
    return parser


def _remember_summaries(parser: argparse.ArgumentParser) -> None:
    """Give every subcommand parser its one-line help, for the themed --help to show."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                child.summary = helps.get(name) or ""
                _remember_summaries(child)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = build_parser()
    words = list(sys.argv[1:] if argv is None else argv)
    # Only the home screen is drawn here; each command decides on its own theme.
    style = Style.for_stream() if not words or words in (["-h"], ["--help"]) else None
    if style is not None:
        from .cli_views import render_home
        render_home(style, __version__, None if words else _home_running())
        return 0
    args = parser.parse_args(argv)
    return int(args.func(args))


def _home_running() -> bool:
    """For the home screen's one status line: a verified server owns the port."""
    return _resolve_server_identity(_default_port())[0] is not None


def command_names() -> list[str]:
    """Every top-level command, for the grouped help's test."""
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []


if __name__ == "__main__":
    raise SystemExit(main())
