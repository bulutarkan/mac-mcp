from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, require_shell_enabled, truncate
from .workspace_sandbox import shell_execution_plan
from .shell_transactions import (
    ShellCaptureError, begin_shell_capture, finalize_shell_capture, shell_capture_http_error,
)
from .tool_cancellation import (
    ToolCancelledError, cancellation_checkpoint, register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)


def _timeout(settings: Settings, timeout_s: Optional[int]) -> int:
    if timeout_s is None:
        return settings.default_command_timeout_s
    return min(max(1, int(timeout_s)), settings.max_command_timeout_s)


def _terminate_process_group(proc: subprocess.Popen[str], grace_s: float = 0.5) -> None:
    """Stop the shell and any descendants after a command timeout."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_command(
    settings: Settings,
    command: str,
    timeout_s: Optional[int] = None,
    *,
    reversible: bool = False,
    reversible_root: Optional[str] = None,
    join_transaction_ids: Optional[List[str]] = None,
    require_full_reversibility: bool = False,
) -> Dict[str, Any]:
    """Run any shell command in zsh login mode, optionally with bounded filesystem capture."""
    require_shell_enabled(settings)
    if join_transaction_ids and not reversible:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "join_transaction_ids requires reversible=true.",
        )
    timeout = _timeout(settings, timeout_s)
    cancellation_checkpoint()
    plan = shell_execution_plan(settings.workdir)
    env = plan.env
    argv = plan.argv(command)
    capture: Optional[Dict[str, Any]] = None
    capture_txid: Optional[str] = None
    if reversible:
        root = Path(reversible_root).expanduser() if reversible_root else Path(plan.cwd)
        try:
            capture = begin_shell_capture(root, require_full=bool(require_full_reversibility))
            capture_txid = str(capture["transaction_id"])
        except BaseException as exc:
            if isinstance(exc, HTTPException):
                raise
            raise shell_capture_http_error(exc) from exc

    start = time.perf_counter()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(plan.cwd),
            start_new_session=True,
        )
    except OSError as exc:
        if capture_txid:
            try:
                from .file_transactions import abort_capture_transaction
                abort_capture_transaction(capture_txid, reason="process_start_failed", outcome_unknown=False)
            except Exception:
                pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"error": "command_start_failed", "message": str(exc), "action_executed": False},
        ) from exc
    cleanup_token = register_cancellation_cleanup(lambda: _terminate_process_group(proc))
    timed_out = False
    try:
        cancellation_checkpoint()
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        cancellation_checkpoint()
        duration_ms = int((time.perf_counter() - start) * 1000)
    except ToolCancelledError:
        _terminate_process_group(proc)
        proc.wait()
        if capture_txid:
            try:
                finalize_shell_capture(capture_txid, join_transaction_ids=join_transaction_ids)
            except Exception:
                pass
        raise
    except subprocess.TimeoutExpired as e:
        timed_out = True
        _terminate_process_group(proc)
        proc.wait()
        duration_ms = int((time.perf_counter() - start) * 1000)
        transaction: Optional[Dict[str, Any]] = None
        if capture_txid:
            try:
                transaction = finalize_shell_capture(capture_txid, join_transaction_ids=join_transaction_ids)
            except BaseException as capture_exc:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    {
                        "error": "shell_capture_finalize_failed",
                        "message": str(capture_exc),
                        "action_executed": True,
                        "outcome": "unknown",
                        "transaction_id": capture_txid,
                    },
                ) from capture_exc
        detail: Dict[str, Any] = {
            "error": "command_timeout",
            "message": f"Command timed out after {timeout}s",
            "action_executed": True,
            "outcome": "terminated",
        }
        if transaction is not None:
            detail["transaction"] = transaction
            detail["transaction_id"] = transaction.get("transaction_id")
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, detail) from e
    finally:
        unregister_cancellation_cleanup(cleanup_token)

    stdout, _ = truncate(stdout_raw or "", settings.max_output_chars)
    stderr, _ = truncate(stderr_raw or "", settings.max_output_chars)
    result: Dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "command": command,
        "sandboxed": plan.sandboxed,
        "cwd": str(plan.cwd),
    }
    if capture_txid:
        try:
            transaction = finalize_shell_capture(capture_txid, join_transaction_ids=join_transaction_ids)
        except BaseException as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {
                    "error": "shell_capture_finalize_failed",
                    "message": str(exc),
                    "action_executed": True,
                    "outcome": "unknown",
                    "transaction_id": capture_txid,
                    "exit_code": proc.returncode,
                },
            ) from exc
        result.update(transaction)
        result["reversible_capture"] = True
    return result


def process_list(settings: Settings, filter: Optional[str] = None) -> Dict[str, Any]:
    """List running processes, optionally filtered by name."""
    cmd = "ps aux"
    result = run_command(settings, cmd)
    if filter and result["ok"]:
        lines = result["stdout"].splitlines()
        header = lines[0] if lines else ""
        matched = [l for l in lines[1:] if filter.lower() in l.lower()]
        result["stdout"] = "\n".join([header] + matched)
        result["matched_count"] = len(matched)
    return result


def kill_process(settings: Settings, pid: int, signal: str = "TERM") -> Dict[str, Any]:
    """Kill a process by PID. Signal: TERM (graceful) or KILL (force)."""
    allowed_signals = {"TERM", "KILL", "HUP", "INT", "QUIT"}
    sig = signal.upper()
    if sig not in allowed_signals:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Signal must be one of: {', '.join(allowed_signals)}")
    return run_command(settings, f"kill -{sig} {pid}")


def get_system_info(settings: Settings) -> Dict[str, Any]:
    """Get Mac system info: CPU, memory, disk, uptime, hostname."""
    script = r"""
echo "=== HOSTNAME ==="
hostname
echo "=== UPTIME ==="
uptime
echo "=== CPU ==="
sysctl -n machdep.cpu.brand_string 2>/dev/null || echo "unknown"
echo "=== MEMORY ==="
vm_stat | perl -ne '/page size of (\d+)/ and $size=$1; /Pages free:\s+(\d+)/ and printf("Free: %.2f GB\n", $1*$size/1073741824); /Pages active:\s+(\d+)/ and printf("Active: %.2f GB\n", $1*$size/1073741824)'
echo "=== DISK ==="
df -h / | tail -1
echo "=== BATTERY ==="
pmset -g batt 2>/dev/null | head -2 || echo "no battery info"
echo "=== NETWORK ==="
ifconfig | grep "inet " | grep -v 127.0.0.1
"""
    return run_command(settings, script)
