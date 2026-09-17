from __future__ import annotations

import argparse
import os
import plistlib
import signal
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

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
from .update_helper import UpdateError, apply_update, check_update, format_check

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


def _load_env() -> None:
    load_dotenv(ENV_FILE)


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
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def _remove_stale_pid(path: Path) -> None:
    pid = _read_pid(path)
    if pid is None or not _pid_alive(pid):
        path.unlink(missing_ok=True)


def _stop_pid(path: Path, name: str, timeout: float, force: bool) -> bool:
    pid = _read_pid(path)
    if not pid or not _pid_alive(pid):
        path.unlink(missing_ok=True)
        print(f"{name} is not running.")
        return True

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            path.unlink(missing_ok=True)
            print(f"{name} stopped.")
            return True
        time.sleep(0.2)

    if force:
        os.kill(pid, signal.SIGKILL)
        path.unlink(missing_ok=True)
        print(f"{name} force-stopped.")
        return True

    print(f"{name} did not stop within {timeout}s. Run: mac-mcp stop --force")
    return False


def _local_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _server_listener_pid(port: int) -> int | None:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(lsof).exists():
        return None
    try:
        probe = subprocess.run(
            [lsof, f"-tiTCP:{int(port)}", "-sTCP:LISTEN"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if probe.returncode not in {0, 1}:
        return None
    for raw in (probe.stdout or "").splitlines():
        try:
            pid = int(raw.strip())
        except ValueError:
            continue
        if pid <= 0 or not _pid_alive(pid):
            continue
        try:
            proc = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        command = (proc.stdout or "").strip()
        if proc.returncode == 0 and APP_MODULE in command and "uvicorn" in command and f"--port {int(port)}" in command:
            return pid
    return None


def _adopt_server_listener(port: int) -> int | None:
    pid = _server_listener_pid(port)
    if pid:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid), encoding="utf-8")
    return pid


def _start_server(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _remove_stale_pid(PID_FILE)
    pid = _read_pid(PID_FILE)
    if pid and _pid_alive(pid):
        print(f"mac-mcp is already running (pid {pid}).")
        _launch_menu_app()
        return 0
    pid = _adopt_server_listener(int(args.port))
    if pid:
        print(f"mac-mcp is already running (pid {pid}; adopted existing listener).")
        _launch_menu_app()
        return 0

    env = os.environ.copy()
    env.setdefault("MAC_MCP_HOST", args.host)
    env.setdefault("MAC_MCP_PORT", str(args.port))
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_MODULE,
        "--host",
        args.host,
        "--port",
        str(args.port),
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
    PID_FILE.write_text(str(proc.pid))
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"mac-mcp failed to start. See log: {LOG_FILE}")
        PID_FILE.unlink(missing_ok=True)
        return proc.returncode or 1

    print(f"mac-mcp started on {_local_url(args.host, args.port)} (pid {proc.pid}).")
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
    candidates = [
        configured,
        os.getenv("NGROK_BIN"),
        shutil.which("ngrok"),
        "/opt/homebrew/bin/ngrok",
        "/usr/local/bin/ngrok",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate))
    return None


def _resolve_cloudflared_binary(configured: str | None = None) -> str | None:
    candidates = [
        configured,
        os.getenv("CLOUDFLARED_BIN"),
        shutil.which("cloudflared"),
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate))
    return None


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


def _wait_for_cloudflare_launchd(timeout: float = 8.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _launchctl_pid()
        if pid:
            CLOUDFLARE_PID_FILE.write_text(str(pid), encoding="utf-8")
            return pid
        time.sleep(0.2)
    return None


def _stop_cloudflare(timeout: float, force: bool) -> bool:
    recorded_pid = _read_pid(CLOUDFLARE_PID_FILE)
    launchd_pid = _launchctl_pid()
    launchd_ok = _bootout_cloudflare_launchd()
    disabled_ok = _set_cloudflare_launchd_enabled(False)
    if launchd_pid and recorded_pid == launchd_pid:
        recorded_pid = None
    manual_ok = True
    if recorded_pid and _pid_alive(recorded_pid):
        manual_ok = _stop_pid(CLOUDFLARE_PID_FILE, "cloudflared", timeout, force)
    else:
        CLOUDFLARE_PID_FILE.unlink(missing_ok=True)
    if launchd_ok:
        deadline = time.time() + timeout
        while launchd_pid and _pid_alive(launchd_pid) and time.time() < deadline:
            time.sleep(0.2)
        if launchd_pid and _pid_alive(launchd_pid):
            launchd_ok = False
    if launchd_ok and disabled_ok and manual_ok:
        CLOUDFLARE_PID_FILE.unlink(missing_ok=True)
        print("cloudflared stopped.")
        return True
    print("cloudflared did not stop cleanly.")
    return False


def _start_cloudflare(args: argparse.Namespace, public) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    launchd_pid = _launchctl_pid()
    if launchd_pid:
        CLOUDFLARE_PID_FILE.write_text(str(launchd_pid), encoding="utf-8")
        print(f"cloudflared is already running under launchd for mac-mcp (pid {launchd_pid}).")
        return 0

    cloudflared = _resolve_cloudflared_binary(getattr(args, "cloudflared_bin", None))
    if cloudflared is None:
        print("cloudflared was not found. Install it with Homebrew or set CLOUDFLARED_BIN.")
        return 2

    target = f"http://127.0.0.1:{int(args.port)}"
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

    previous_pid = _read_pid(CLOUDFLARE_PID_FILE)
    previous_pid = previous_pid if previous_pid and _pid_alive(previous_pid) else None
    plist_path = _write_cloudflare_launchd_plist(cmd)
    if _cloudflare_launchd_loaded() and not _bootout_cloudflare_launchd():
        print("Could not replace the existing cloudflared launchd job.")
        return 1
    if not _set_cloudflare_launchd_enabled(True):
        print("Could not enable the cloudflared launchd job.")
        return 1
    ok, detail = _bootstrap_cloudflare_launchd(plist_path)
    if not ok:
        print(f"cloudflared failed to register with launchd{': ' + detail if detail else '.'}")
        return 1
    pid = _wait_for_cloudflare_launchd()
    if not pid:
        _bootout_cloudflare_launchd()
        print(f"cloudflared failed to become healthy under launchd. See log: {CLOUDFLARE_LOG_FILE}")
        return 1

    if previous_pid and previous_pid != pid and _pid_alive(previous_pid):
        try:
            os.kill(previous_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    CLOUDFLARE_PID_FILE.write_text(str(pid), encoding="utf-8")
    print(f"Cloudflare Tunnel started under launchd: {public.endpoint_url} -> {target} (pid {pid}).")
    print(f"cloudflared log: {CLOUDFLARE_LOG_FILE}")
    return 0


def _stop_unselected_public_processes(selected_mode: str) -> None:
    for mode, path, label in (
        ("ngrok", NGROK_PID_FILE, "ngrok"),
        ("cloudflare", CLOUDFLARE_PID_FILE, "cloudflared"),
    ):
        _remove_stale_pid(path)
        pid = _read_pid(path)
        if mode != selected_mode:
            if mode == "cloudflare" and (_cloudflare_launchd_loaded() or (pid and _pid_alive(pid))):
                _stop_cloudflare(3.0, True)
            elif pid and _pid_alive(pid):
                _stop_pid(path, label, 3.0, True)


def _start_ngrok(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _remove_stale_pid(NGROK_PID_FILE)
    pid = _read_pid(NGROK_PID_FILE)
    if pid and _pid_alive(pid):
        print(f"ngrok is already running for mac-mcp (pid {pid}).")
        return 0

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

    public_url = f"https://{domain}"
    target = str(args.port)
    cmd = [ngrok_path, "http", f"--domain={domain}", target]
    log = NGROK_LOG_FILE.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    NGROK_PID_FILE.write_text(str(proc.pid))
    time.sleep(0.8)
    if proc.poll() is not None:
        print(f"ngrok failed to start. See log: {NGROK_LOG_FILE}")
        NGROK_PID_FILE.unlink(missing_ok=True)
        return proc.returncode or 1

    print(f"ngrok tunnel started: {public_url} -> {_local_url(args.host, args.port)} (pid {proc.pid}).")
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
    if not (_read_pid(PID_FILE) and _pid_alive(_read_pid(PID_FILE) or 0)):
        _adopt_server_listener(_default_port())
    server_ok = _stop_pid(PID_FILE, "mac-mcp", args.timeout, args.force)
    ngrok_ok = _stop_pid(NGROK_PID_FILE, "ngrok", args.timeout, args.force)
    cloudflare_ok = _stop_cloudflare(args.timeout, args.force)
    return 0 if server_ok and ngrok_ok and cloudflare_ok else 1


def status(args: argparse.Namespace) -> int:
    _load_env()
    server_pid = _read_pid(PID_FILE)
    if not (server_pid and _pid_alive(server_pid)):
        server_pid = _adopt_server_listener(_default_port())
    ngrok_pid = _read_pid(NGROK_PID_FILE)
    cloudflare_pid = _launchctl_pid() or _read_pid(CLOUDFLARE_PID_FILE)
    server_running = bool(server_pid and _pid_alive(server_pid))
    ngrok_running = bool(ngrok_pid and _pid_alive(ngrok_pid))
    cloudflare_running = bool(cloudflare_pid and _pid_alive(cloudflare_pid))
    if cloudflare_running and cloudflare_pid:
        CLOUDFLARE_PID_FILE.write_text(str(cloudflare_pid), encoding="utf-8")

    if server_running:
        print(f"mac-mcp is running (pid {server_pid}).")
        print(f"mac-mcp log: {LOG_FILE}")
    else:
        PID_FILE.unlink(missing_ok=True)
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
                print(f"cloudflared is running (pid {cloudflare_pid}).")
                print(f"cloudflared log: {CLOUDFLARE_LOG_FILE}")
                print(f"MCP URL: {public.endpoint_url}")
            else:
                CLOUDFLARE_PID_FILE.unlink(missing_ok=True)
                print("Cloudflare Tunnel is configured but not running for mac-mcp.")
        elif public.mode == "ngrok":
            print("public endpoint mode: ngrok")
            if ngrok_running:
                print(f"ngrok is running (pid {ngrok_pid}).")
                print(f"ngrok log: {NGROK_LOG_FILE}")
                print(f"MCP URL: {public.endpoint_url}")
            else:
                NGROK_PID_FILE.unlink(missing_ok=True)
                print("ngrok is configured but not running for mac-mcp.")
        else:
            print("public endpoint mode: local only")

    if ngrok_running and (public is None or public.mode != "ngrok"):
        print(f"ngrok managed process is still running (pid {ngrok_pid}) but is not the selected public endpoint mode.")
    elif not ngrok_running:
        NGROK_PID_FILE.unlink(missing_ok=True)

    if cloudflare_running and (public is None or public.mode != "cloudflare"):
        print(f"cloudflared managed process is still running (pid {cloudflare_pid}) but is not the selected public endpoint mode.")
    elif not cloudflare_running:
        CLOUDFLARE_PID_FILE.unlink(missing_ok=True)

    return 0 if server_running else 1


def restart(args: argparse.Namespace) -> int:
    _load_env()
    stop_args = argparse.Namespace(timeout=args.timeout, force=True)
    stop(stop_args)
    return start(args)


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
    server_pid = _read_pid(PID_FILE)
    if not server_pid or not _pid_alive(server_pid):
        PID_FILE.unlink(missing_ok=True)
        print("mac-mcp is not running. Start it first with: mac-mcp start")
        return 1
    host = os.getenv("MAC_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    try:
        port = int(os.getenv("MAC_MCP_PORT", DEFAULT_PORT))
    except ValueError:
        port = int(DEFAULT_PORT)
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
        if args.check:
            info = check_update(args.repo, args.runtime, branch=args.branch, remote=args.remote, fetch=True)
            print(format_check(info))
            return 2 if info.dirty else 0
        apply_update(
            repo=args.repo, runtime=args.runtime, branch=args.branch, remote=args.remote,
            launchd_label=os.getenv("MAC_MCP_LAUNCHD_LABEL", "mac-mcp-uvicorn"),
            skip_restart=getattr(args, "skip_restart", False),
            skip_deps=getattr(args, "skip_deps", False),
        )
        return 0
    except UpdateError as exc:
        print(f"mac-mcp update failed: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Manage the Mac MCP local server.")
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

    p_update = sub.add_parser("update", help="Update Mac MCP from the latest commit on a Git branch.")
    p_update.add_argument("--check", action="store_true", help="Check for updates without changing files.")
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
