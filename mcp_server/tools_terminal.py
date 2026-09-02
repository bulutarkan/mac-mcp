from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from .security import Settings, truncate


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


def run_command(settings: Settings, command: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """Run any shell command in zsh login mode."""
    timeout = _timeout(settings, timeout_s)
    env = os.environ.copy()
    env.update({
        "HOME": str(Path.home()),
        "USER": os.getenv("USER", Path.home().name),
        "LOGNAME": os.getenv("LOGNAME", os.getenv("USER", Path.home().name)),
        "PATH": f"{os.environ.get('PATH', '')}:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    })

    argv = ["/bin/zsh", "-lc", command] if settings.allow_shell else command.split()
    start = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(settings.workdir),
        start_new_session=True,
    )
    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        duration_ms = int((time.perf_counter() - start) * 1000)
    except subprocess.TimeoutExpired as e:
        _terminate_process_group(proc)
        proc.wait()
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT,
                            f"Command timed out after {timeout}s") from e

    stdout, _ = truncate(stdout_raw or "", settings.max_output_chars)
    stderr, _ = truncate(stderr_raw or "", settings.max_output_chars)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "command": command,
    }


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
