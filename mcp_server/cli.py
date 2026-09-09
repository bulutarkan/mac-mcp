from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from .update_helper import UpdateError, apply_update, check_update, format_check

APP_MODULE = "mcp_server.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "mcp_server" / ".env"
STATE_DIR = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
PID_FILE = STATE_DIR / "mac-mcp.pid"
NGROK_PID_FILE = STATE_DIR / "ngrok.pid"
LOG_FILE = STATE_DIR / "mac-mcp.log"
NGROK_LOG_FILE = STATE_DIR / "ngrok.log"


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
        for name in ("MAC_MCP_SETTINGS_PATH", "MAC_MCP_STATE_DIR", "MAC_MCP_CLI_PATH", "MAC_MCP_PORT", "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE", "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT"):
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


def _start_server(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _remove_stale_pid(PID_FILE)
    pid = _read_pid(PID_FILE)
    if pid and _pid_alive(pid):
        print(f"mac-mcp is already running (pid {pid}).")
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
    dashboard_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"dashboard: {_local_url(dashboard_host, args.port)}/dashboard")
    print(f"mac-mcp log: {LOG_FILE}")
    _launch_menu_app()
    return 0


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
    server_code = _start_server(args)
    if server_code != 0:
        return server_code
    if args.ngrok:
        return _start_ngrok(args)
    print("ngrok was not started. Use: mac-mcp start --ngrok")
    return 0


def stop(args: argparse.Namespace) -> int:
    _load_env()
    server_ok = _stop_pid(PID_FILE, "mac-mcp", args.timeout, args.force)
    ngrok_ok = _stop_pid(NGROK_PID_FILE, "ngrok", args.timeout, args.force)
    return 0 if server_ok and ngrok_ok else 1


def status(args: argparse.Namespace) -> int:
    _load_env()
    server_pid = _read_pid(PID_FILE)
    ngrok_pid = _read_pid(NGROK_PID_FILE)
    server_running = bool(server_pid and _pid_alive(server_pid))
    ngrok_running = bool(ngrok_pid and _pid_alive(ngrok_pid))

    if server_running:
        print(f"mac-mcp is running (pid {server_pid}).")
        print(f"mac-mcp log: {LOG_FILE}")
    else:
        PID_FILE.unlink(missing_ok=True)
        print("mac-mcp is not running.")

    if ngrok_running:
        domain = os.getenv("NGROK_DOMAIN", "").strip()
        suffix = f" at https://{domain}" if domain else ""
        print(f"ngrok is running (pid {ngrok_pid}){suffix}.")
        print(f"ngrok log: {NGROK_LOG_FILE}")
    else:
        NGROK_PID_FILE.unlink(missing_ok=True)
        print("ngrok is not running for mac-mcp.")

    return 0 if server_running else 1


def restart(args: argparse.Namespace) -> int:
    _load_env()
    stop_args = argparse.Namespace(timeout=args.timeout, force=True)
    stop(stop_args)
    return start(args)


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
    opened = webbrowser.open(url)
    print(f"dashboard: {url}")
    if not opened:
        print("The browser did not open automatically; paste the dashboard URL into a local browser.")
    return 0


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
        p.add_argument("--port", default=int(os.getenv("MAC_MCP_PORT", DEFAULT_PORT)), type=int)
        p.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode for development.")
        p.add_argument("--ngrok", action="store_true", help="Also start an ngrok tunnel using NGROK_DOMAIN from mcp_server/.env.")
        p.add_argument("--ngrok-domain", default=None, help="Override NGROK_DOMAIN for this run.")
        p.add_argument("--ngrok-bin", default=None, help="Path or command name for the ngrok binary. Defaults to ngrok.")

    p_start = sub.add_parser("start", help="Start the local server, optionally with ngrok.")
    add_start_flags(p_start)
    p_start.set_defaults(func=start)

    p_stop = sub.add_parser("stop", help="Stop the local server and the managed ngrok tunnel.")
    p_stop.add_argument("--timeout", type=float, default=5)
    p_stop.add_argument("--force", action="store_true")
    p_stop.set_defaults(func=stop)

    p_restart = sub.add_parser("restart", help="Restart the local server, optionally with ngrok.")
    add_start_flags(p_restart)
    p_restart.add_argument("--timeout", type=float, default=5)
    p_restart.set_defaults(func=restart)

    p_status = sub.add_parser("status", help="Show server and ngrok status.")
    p_status.set_defaults(func=status)

    p_dashboard = sub.add_parser("dashboard", help="Open the local Mac MCP operations dashboard.")
    p_dashboard.set_defaults(func=dashboard)

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
