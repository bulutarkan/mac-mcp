from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import signal
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from urllib.request import urlopen

from dotenv import load_dotenv

from .security import dashboard_token_path, load_settings, validate_bootstrap_security
from .public_endpoint import (
    PublicEndpointError,
    inspect_cloudflare_credential,
    remove_cloudflare_token,
    resolve_public_endpoint,
    write_cloudflare_token,
)
from .runtime_settings import server_setting
from .managed_process import (
    listener_pids,
    matches_role,
    migrate_legacy_record,
    pid_alive,
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
from .tools_update import launch_detached_update
from .version import __version__
from .connection_config import (
    ConnectionConfigError,
    DEFAULT_AUTH_ENV,
    DEFAULT_SERVER_NAME,
    SUPPORTED_CLIENTS,
    SUPPORTED_ENDPOINTS,
    render_connection_config,
)

APP_MODULE = "mcp_server.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "mcp_server" / ".env"
STATE_DIR = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
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
            print(f"Refusing further signals to {name}: process identity became unverifiable.")
            return False
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
        rendered = ", ".join(str(pid) for pid in foreign if pid > 0) or "unknown"
        print(
            f"Port {port} is already listening under unmanaged process pid(s) {rendered}; "
            "refusing to adopt or start mac-mcp."
        )
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
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_MODULE,
        "--host",
        args.host,
        "--port",
        str(port),
    ]
    if args.reload:
        cmd.append("--reload")

    log = LOG_FILE.open("a", encoding="utf-8")
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
    log = NGROK_LOG_FILE.open("a", encoding="utf-8")
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
    return 0


def start(args: argparse.Namespace) -> int:
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


def stop(args: argparse.Namespace) -> int:
    _load_env()
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
    return 0 if server_ok and ngrok_ok and cloudflare_ok else 1


def _connect_config_endpoint(client: str, selection: str) -> tuple[str, str]:
    local_endpoint = f"http://127.0.0.1:{_default_port()}/mcp"
    if selection == "local":
        return local_endpoint, "local"

    should_use_public = selection == "public" or (selection == "auto" and client == "chatgpt")
    if not should_use_public:
        return local_endpoint, "local"

    public = resolve_public_endpoint()
    if not public.endpoint_url:
        raise ConnectionConfigError(
            "no public MCP endpoint is configured; choose --endpoint local or configure "
            "Cloudflare, ngrok, or a custom HTTPS endpoint"
        )
    return public.endpoint_url, "public"


def connect_config(args: argparse.Namespace) -> int:
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
        print(f"mac-mcp connect-config: {exc}", file=sys.stderr)
        return 1

    print(f"Client: {rendered.client}")
    print(f"Target: {rendered.target}")
    print(f"Endpoint: {endpoint_kind} ({endpoint_url})")
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
    if rendered.client == "chatgpt" and auth_required:
        print(
            "Use the query-key URL only because this ChatGPT connection path is "
            "header-limited; upstream proxies/tunnels may observe query strings."
        )
    return 0


def status(args: argparse.Namespace) -> int:
    _load_env()
    port = _default_port()

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

    if server_running:
        print(
            f"mac-mcp is running (pid {server_pid}; identity verified via {server_source})."
        )
        print(f"mac-mcp log: {LOG_FILE}")
    else:
        if server_source == "foreign_listener":
            conflicts = _server_listener_conflicts(port)
            rendered = ", ".join(str(pid) for pid in conflicts if pid > 0) or "unknown"
            print(
                f"mac-mcp is not running; configured port {port} is occupied by "
                f"unmanaged listener pid(s) {rendered}."
            )
        elif server_source not in {"not_running"}:
            print(f"mac-mcp ownership is not verified ({server_source}).")
        else:
            print("mac-mcp is not running.")

    try:
        public = resolve_public_endpoint()
    except PublicEndpointError as exc:
        public = None
        print(f"public endpoint configuration error: {exc}")

    if public is not None:
        if public.mode == "custom":
            print("public endpoint mode: custom")
            print(f"MCP URL: {public.endpoint_url}")
        elif public.mode == "cloudflare":
            print("public endpoint mode: cloudflare")
            if cloudflare_running:
                print(
                    f"cloudflared is running (pid {cloudflare_pid}; identity verified via {cloudflare_source})."
                )
                print(f"cloudflared log: {CLOUDFLARE_LOG_FILE}")
                print(f"MCP URL: {public.endpoint_url}")
            else:
                print(
                    "Cloudflare Tunnel is configured but no verified mac-mcp cloudflared process is running "
                    f"({cloudflare_source})."
                )
        elif public.mode == "ngrok":
            print("public endpoint mode: ngrok")
            if ngrok_running:
                print(f"ngrok is running (pid {ngrok_pid}; identity verified).")
                print(f"ngrok log: {NGROK_LOG_FILE}")
                print(f"MCP URL: {public.endpoint_url}")
            else:
                print(
                    "ngrok is configured but no verified mac-mcp ngrok process is running "
                    f"({ngrok_validation.status})."
                )
        else:
            print("public endpoint mode: local only")

    if ngrok_running and (public is None or public.mode != "ngrok"):
        print(
            f"ngrok managed process is still running (pid {ngrok_pid}) but is not "
            "the selected public endpoint mode."
        )

    if cloudflare_running and (public is None or public.mode != "cloudflare"):
        print(
            f"cloudflared managed process is still running (pid {cloudflare_pid}) but is not "
            "the selected public endpoint mode."
        )

    return 0 if server_running else 1


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


def _restart_handoff_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable, "-m", "mcp_server.cli", "restart",
        "--host", str(args.host),
        "--port", str(int(args.port)),
        "--timeout", str(float(args.timeout)),
    ]
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
        f"{RESTART_REQUESTER_ENV}={os.getpid()}",
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
    loaded, pid = _restart_handoff_job()
    if loaded and pid and _pid_alive(pid):
        print(f"mac-mcp restart is already in progress under launchd (helper pid {pid}).")
        return 0
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
    return 0


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


def restart(args: argparse.Namespace) -> int:
    _load_env()
    handoff_child = os.getenv(RESTART_HANDOFF_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if not handoff_child:
        # Always delegate restart to launchd. A restart tears down the server that
        # may currently be carrying this command; launchd ownership prevents MCP
        # cancellation cleanup or shell process-group cleanup from killing the worker.
        return _spawn_detached_restart(args)

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
    stop_args = argparse.Namespace(timeout=args.timeout, force=True)
    stop_code = stop(stop_args)
    if stop_code != 0:
        _write_restart_status("failed", stage="stop", exit_code=stop_code, helper_pid=os.getpid())
        print("Restart aborted because managed process shutdown was not verified.")
        return stop_code

    _write_restart_status("running", stage="starting", helper_pid=os.getpid())
    start_code = start(args)
    if start_code != 0:
        _write_restart_status("failed", stage="start", exit_code=start_code, helper_pid=os.getpid())
        return start_code

    _write_restart_status("running", stage="verifying", helper_pid=os.getpid())
    if not _restart_health_ok(args):
        _write_restart_status("failed", stage="health", exit_code=1, helper_pid=os.getpid())
        print("mac-mcp restart health verification failed.")
        return 1

    server_pid, source = _resolve_server_identity(int(args.port), adopt_listener=False)
    _write_restart_status(
        "succeeded",
        server_pid=server_pid,
        ownership_source=source,
        health=True,
    )
    return 0


def credential(args: argparse.Namespace) -> int:
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


def dashboard(args: argparse.Namespace) -> int:
    _load_env()
    port = _default_port()
    server_pid, server_source = _resolve_server_identity(port)
    if not server_pid:
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
        print("Dashboard credential is unavailable. Restart Mac MCP once, then run: mac-mcp dashboard")
        return 1
    authenticated_url = f"{url}#token={quote(token, safe='')}"
    opened = webbrowser.open(authenticated_url)
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
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_report(report))
        if support_path is not None:
            print(f"Support bundle: {support_path}")
    return 0 if report.get("ok") else 1


def conformance(args: argparse.Namespace) -> int:
    from .conformance import run_conformance
    from .diagnostics import format_report

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
    try:
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
    except UpdateError as exc:
        print(f"mac-mcp update failed: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Manage the Mac MCP local server.")
    parser.add_argument("--version", action="version", version=f"mac-mcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

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
    p_stop.add_argument("--timeout", type=float, default=5)
    p_stop.add_argument("--force", action="store_true")
    p_stop.set_defaults(func=stop)

    p_restart = sub.add_parser("restart", help="Restart the local server and apply the configured public endpoint mode.")
    add_start_flags(p_restart)
    p_restart.add_argument("--timeout", type=float, default=5)
    p_restart.set_defaults(func=restart)

    p_status = sub.add_parser("status", help="Show server and public endpoint status.")
    p_status.set_defaults(func=status)

    p_connect = sub.add_parser(
        "connect-config",
        help="Generate a secret-safe MCP connection snippet for a supported client.",
    )
    p_connect.add_argument("--client", required=True, choices=SUPPORTED_CLIENTS)
    p_connect.add_argument(
        "--endpoint",
        choices=SUPPORTED_ENDPOINTS,
        default="auto",
        help="Endpoint choice. auto uses public for ChatGPT and local for Codex/OpenCode.",
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

    p_dashboard = sub.add_parser("dashboard", help="Open the local Mac MCP operations dashboard.")
    p_dashboard.set_defaults(func=dashboard)

    p_credential = sub.add_parser("credential", help="Manage local provider credentials without exposing secret values in process arguments.")
    p_credential.add_argument("provider", choices=("cloudflare",))
    p_credential.add_argument("action", choices=("status", "save", "remove"))
    p_credential.set_defaults(func=credential)

    p_doctor = sub.add_parser("doctor", help="Diagnose local Mac MCP runtime, permissions, dependencies, and companions.")
    p_doctor.add_argument("--json", action="store_true", help="Print the diagnostic report as JSON.")
    p_doctor.add_argument("--support-bundle", nargs="?", const="", default=None, metavar="PATH", help="Write an owner-only redacted support bundle. Optional PATH overrides the default location.")
    p_doctor.set_defaults(func=doctor)

    p_conformance = sub.add_parser("conformance", help="Run deterministic Computer Use contract/regression checks.")
    p_conformance.add_argument("--json", action="store_true", help="Print the conformance report as JSON.")
    p_conformance.add_argument("--live", action="store_true", help="Also include read-only live Mac/companion health checks.")
    p_conformance.set_defaults(func=conformance)

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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
