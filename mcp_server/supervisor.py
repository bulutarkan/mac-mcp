"""One supervision pass for the CLI-managed server, run by launchd every 30 s.

`mac-mcp start` records the intent "running" and loads this job; `mac-mcp
stop` records "stopped" before it stops anything, so an intentional stop is
never undone. A pass does nothing unless the intent is "running", and also
steps aside while an update holds the update lock or a restart is in
progress. Otherwise it checks local health and, when the server process is
gone or has failed several checks in a row, starts it again through the
normal `mac-mcp start` path (which takes the start lock, so it never races a
user's start). Recoveries are bounded: at most RECOVERY_LIMIT attempts per
RECOVERY_WINDOW_S, then it waits for the window to pass.

Every pass writes supervisor-state.json, which `mac-mcp status --json` and
`mac-mcp doctor` report.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.request import urlopen

RECOVERY_LIMIT = 3
RECOVERY_WINDOW_S = 900.0
UNHEALTHY_PASSES_BEFORE_RESTART = 4
RESTART_IN_PROGRESS_S = 600.0
INTENT_FILE = "server-intent.json"
STATE_FILE = "supervisor-state.json"


def state_dir() -> Path:
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_intent(root: Optional[Path] = None) -> dict[str, Any]:
    return _read_json((root or state_dir()) / INTENT_FILE)


def write_intent(desired: str, *, start_args: Optional[list[str]] = None, root: Optional[Path] = None) -> None:
    """Record whether the user wants the server running ("running"/"stopped")."""
    path = (root or state_dir()) / INTENT_FILE
    previous = _read_json(path)
    payload = {"desired": desired, "updated_at": time.time()}
    payload["start_args"] = start_args if start_args is not None else previous.get("start_args") or []
    _write_json(path, payload)


def read_state(root: Optional[Path] = None) -> dict[str, Any]:
    return _read_json((root or state_dir()) / STATE_FILE)


def _update_lock_held() -> bool:
    configured = os.getenv("MAC_MCP_UPDATE_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".mac-mcp" / "update"
    path = root / "update.lock"
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def _restart_in_progress(root: Path, now: float) -> bool:
    status = _read_json(root / "restart-status.json")
    if str(status.get("state") or "") not in {"requested", "running"}:
        return False
    return now - float(status.get("updated_at") or 0) < RESTART_IN_PROGRESS_S


def _healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health?probe=basic", timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            return response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
    except Exception:
        return False


def _server_process_present(root: Path, port: int) -> bool:
    from .managed_process import listener_pids, pid_alive, read_process_record

    record = read_process_record(root / "mac-mcp.pid")
    if record is not None and record.pid > 0 and pid_alive(record.pid):
        return True
    return bool(listener_pids(port))


def ngrok_selected(start_args: list[str]) -> bool:
    """Whether the recorded start uses an ngrok tunnel (Cloudflare has its own launchd KeepAlive)."""
    if "--ngrok" in start_args:
        return True
    if "--public-mode" in start_args:
        try:
            return start_args[start_args.index("--public-mode") + 1] == "ngrok"
        except IndexError:
            return False
    try:
        from .public_endpoint import resolve_public_endpoint

        return resolve_public_endpoint().mode == "ngrok"
    except Exception:
        return False


def _ngrok_alive(root: Path, port: int) -> bool:
    from .managed_process import validate_process_record

    return bool(validate_process_record(root / "ngrok.pid", "ngrok", port=port).valid)


def _log_tail(root: Path) -> str:
    from .log_retention import tail_log

    return tail_log(root / "mac-mcp.log", lines=8)[-1200:]


def _cli(args: list[str], timeout_s: float = 180.0) -> int:
    env = os.environ.copy()
    env.pop("MAC_MCP_MANAGED_SERVER", None)
    # A recovery brings the server back; it must not reopen a menu app the user quit.
    env["MAC_MCP_SKIP_MENU_APP"] = "1"
    try:
        return subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_s, env=env,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def run_once(
    *,
    root: Optional[Path] = None,
    now: Optional[float] = None,
    healthy: Callable[[int], bool] = _healthy,
    process_present: Optional[Callable[[Path, int], bool]] = None,
    cli: Callable[[list[str]], int] = _cli,
    update_lock_held: Callable[[], bool] = _update_lock_held,
    tunnel_selected: Callable[[list[str]], bool] = ngrok_selected,
    tunnel_alive: Callable[[Path, int], bool] = _ngrok_alive,
) -> dict[str, Any]:
    root = root or state_dir()
    now = time.time() if now is None else now
    process_present = process_present or _server_process_present
    state = read_state(root)
    state["last_check_at"] = now
    intent = read_intent(root)
    start_args = [str(item) for item in intent.get("start_args") or []]
    port = _port_from_args(start_args)

    def finish(result: str, **extra: Any) -> dict[str, Any]:
        state.update({"last_result": result, **extra})
        _write_json(root / STATE_FILE, state)
        return state

    if os.getenv("MAC_MCP_SUPERVISOR", "1").strip().lower() in {"0", "false", "no", "off"}:
        return finish("disabled")
    if intent.get("desired") != "running":
        state["unhealthy_passes"] = 0
        return finish("idle_stopped")
    if update_lock_held():
        return finish("skipped_update")
    if _restart_in_progress(root, now):
        return finish("skipped_restart")
    if healthy(port):
        state["unhealthy_passes"] = 0
        if tunnel_selected(start_args) and not tunnel_alive(root, port):
            return _recover_tunnel(state, start_args, now, cli, finish)
        return finish("healthy")

    present = process_present(root, port)
    if present:
        state["unhealthy_passes"] = int(state.get("unhealthy_passes") or 0) + 1
        if state["unhealthy_passes"] < UNHEALTHY_PASSES_BEFORE_RESTART:
            return finish("unhealthy")
        reason = "unresponsive"
    else:
        reason = "process_exited"

    attempts = [float(at) for at in state.get("recovery_attempts") or [] if now - float(at) < RECOVERY_WINDOW_S]
    if len(attempts) >= RECOVERY_LIMIT:
        state["recovery_attempts"] = attempts
        return finish("backoff", backoff_until=min(attempts) + RECOVERY_WINDOW_S, down_reason=reason)

    evidence = _log_tail(root)
    if reason == "unresponsive":
        cli(["stop", "--force", "--keep-intent"])
    code = cli(["start", *start_args])
    recovered = code == 0 and healthy(port)
    attempts.append(now)
    state.update({
        "recovery_attempts": attempts,
        "unhealthy_passes": 0,
        "last_recovery": {
            "at": now,
            "reason": reason,
            "result": "recovered" if recovered else "failed",
            "exit_code": code,
            "log_tail": evidence,
        },
    })
    return finish("recovered" if recovered else "recovery_failed")


def _recover_tunnel(state, start_args, now, cli, finish) -> dict[str, Any]:
    """The server is fine but its ngrok tunnel exited: start it again (the server is only adopted)."""
    attempts = [float(at) for at in state.get("tunnel_recovery_attempts") or [] if now - float(at) < RECOVERY_WINDOW_S]
    if len(attempts) >= RECOVERY_LIMIT:
        state["tunnel_recovery_attempts"] = attempts
        return finish("tunnel_backoff", backoff_until=min(attempts) + RECOVERY_WINDOW_S)
    code = cli(["start", *start_args])
    attempts.append(now)
    # Exit 3 from start: ngrok runs but its public route does not answer yet.
    result = "recovered" if code == 0 else ("degraded" if code == 3 else "failed")
    state.update({
        "tunnel_recovery_attempts": attempts,
        "last_tunnel_recovery": {"at": now, "tunnel": "ngrok", "result": result, "exit_code": code},
    })
    return finish("tunnel_recovered" if code == 0 else "tunnel_recovery_failed")


def _port_from_args(args: list[str]) -> int:
    if "--port" in args:
        try:
            return int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    try:
        return int(os.getenv("MAC_MCP_PORT", "8765"))
    except ValueError:
        return 8765


def main() -> int:
    run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
