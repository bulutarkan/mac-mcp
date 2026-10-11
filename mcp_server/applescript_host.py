"""Run browser-bridge AppleScript in a reusable helper process instead of osascript.

Launching osascript costs roughly 150-200 ms per call before Safari is even asked
anything; the browser bridge makes one or two such calls per action. A small Swift
helper (applescript_host.swift) runs scripts with NSAppleScript and stays alive, so
only the Apple Event itself is paid per call.

The helper is optional: it is compiled on first use in the background, and until
it is ready, or if it cannot be built or keeps failing, callers fall back to
osascript. A call that times out or is cancelled kills its helper process, which
is exactly what happens to an osascript process today. Set
MAC_MCP_APPLESCRIPT_HOST=0 to always use osascript. Only the CLI-managed server
enables it, so tests and imports never build or start the helper.

An idle helper holds about 40 MB and costs about 20 ms to start again, so a helper
unused for MAC_MCP_APPLESCRIPT_HOST_IDLE_S (default 120 s) is stopped. One timer,
armed only while helpers sit idle, does this; an empty pool never wakes up.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import swift_build
from .tool_cancellation import (
    ToolCancelledError,
    cancellation_checkpoint,
    register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)

_SOURCE = Path(__file__).resolve().with_name("applescript_host.swift")
_POOL_LIMIT = 3
_FAILURE_LIMIT = 3
_DEFAULT_IDLE_RETIRE_S = 120.0

_lock = threading.Lock()
_idle: List["_Host"] = []
_live = 0
_reaper: Optional[threading.Timer] = None
_state = {"executable": None, "building": False, "disabled": False, "failures": 0, "allowed": False}


class HostUnavailable(RuntimeError):
    """The helper cannot run this call; use osascript instead (nothing was executed)."""


class HostTimeout(RuntimeError):
    """The script did not finish in time; its helper process was killed."""


def enable() -> None:
    """Allow the helper in this process; only the CLI-managed server calls this."""
    with _lock:
        _state["allowed"] = True


def _enabled() -> bool:
    if not _state["allowed"]:
        return False
    return os.getenv("MAC_MCP_APPLESCRIPT_HOST", "1").strip().lower() not in {"0", "false", "no", "off"}


def _build() -> None:
    try:
        directory = swift_build.cache_dir("MAC_MCP_APPLESCRIPT_HOST_CACHE", "~/.mac-mcp/cache/applescript-host")
        executable = swift_build.compile_cached(_SOURCE, directory, "applescript-host")
        with _lock:
            _state["executable"] = str(executable)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        with _lock:
            _state["disabled"] = True
    finally:
        with _lock:
            _state["building"] = False


def _executable() -> Optional[str]:
    """Return the helper binary, starting a background build the first time."""
    if not _enabled():
        return None
    with _lock:
        if _state["disabled"]:
            return None
        if _state["executable"]:
            return str(_state["executable"])
        if not _state["building"]:
            _state["building"] = True
            threading.Thread(target=_build, name="applescript-host-build", daemon=True).start()
    return None


class _Host:
    def __init__(self, executable: str) -> None:
        self.proc = subprocess.Popen(
            [executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._buffer = b""
        self.idle_since = time.monotonic()

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def request(self, script: str, timeout_s: float) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps({"script": script}).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout_s
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostTimeout(f"AppleScript timed out after {timeout_s}s")
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
            cancellation_checkpoint()
            if ready:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    raise EOFError("AppleScript host exited")
                self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def _checkout(executable: str) -> _Host:
    global _live
    with _lock:
        while _idle:
            host = _idle.pop()
            if host.proc.poll() is None:
                return host
            _live -= 1
        if _live >= _POOL_LIMIT:
            raise HostUnavailable("all AppleScript hosts are busy")
        _live += 1
    try:
        return _Host(executable)
    except OSError as exc:
        with _lock:
            _live -= 1
        raise HostUnavailable(str(exc)) from exc


def _retire(host: _Host) -> None:
    global _live
    host.kill()
    with _lock:
        _live -= 1


def _idle_retire_s() -> float:
    try:
        value = float(os.getenv("MAC_MCP_APPLESCRIPT_HOST_IDLE_S", _DEFAULT_IDLE_RETIRE_S))
    except ValueError:
        value = _DEFAULT_IDLE_RETIRE_S
    return max(5.0, min(value, 3600.0))


def _arm_reaper_locked() -> None:
    """Arm the single retirement timer for the oldest idle helper (caller holds _lock)."""
    global _reaper
    if _reaper is not None or not _idle:
        return
    oldest = min(host.idle_since for host in _idle)
    delay = max(0.05, oldest + _idle_retire_s() - time.monotonic())
    _reaper = threading.Timer(delay, _reap_idle)
    _reaper.daemon = True
    _reaper.start()


def _reap_idle() -> None:
    global _reaper, _live
    limit = _idle_retire_s()
    now = time.monotonic()
    with _lock:
        _reaper = None
        expired = [host for host in _idle if now - host.idle_since >= limit]
        _idle[:] = [host for host in _idle if host not in expired]
        _live -= len(expired)
        _arm_reaper_locked()
    for host in expired:
        host.kill()


def _note_failure() -> None:
    with _lock:
        _state["failures"] += 1
        if _state["failures"] >= _FAILURE_LIMIT:
            _state["disabled"] = True


def run(script: str, timeout_s: float) -> Tuple[bool, str, str]:
    """Run one script; returns (ok, stdout, stderr) shaped like an osascript call.

    Raises HostUnavailable (nothing ran: use osascript), HostTimeout, or
    ToolCancelledError (the helper was killed mid-call).
    """
    executable = _executable()
    if executable is None:
        raise HostUnavailable("AppleScript host is not ready")
    host = _checkout(executable)
    cleanup = register_cancellation_cleanup(host.kill)
    try:
        try:
            reply = host.request(script, max(0.1, float(timeout_s)))
        except (HostTimeout, ToolCancelledError):
            _retire(host)
            raise
        except (OSError, EOFError, ValueError) as exc:
            # The helper died or answered garbage; whether the script ran is unknown,
            # so this is reported as a failure, never retried through osascript.
            _retire(host)
            _note_failure()
            return False, "", f"AppleScript host failed: {exc}"
    finally:
        unregister_cancellation_cleanup(cleanup)
    with _lock:
        host.idle_since = time.monotonic()
        _idle.append(host)
        _state["failures"] = 0
        _arm_reaper_locked()
    if reply.get("ok"):
        return True, str(reply.get("result") or ""), ""
    number = int(reply.get("number") or 0)
    # osascript labels compile failures (OSA -2760..-2740) as syntax errors.
    kind = "syntax error" if -2760 <= number <= -2740 else "execution error"
    return False, "", f"{kind}: {reply.get('error') or 'AppleScript error'} ({number})"


def shutdown() -> None:
    global _live, _reaper
    with _lock:
        hosts, _idle[:] = list(_idle), []
        _live -= len(hosts)
        if _reaper is not None:
            _reaper.cancel()
            _reaper = None
    for host in hosts:
        host.kill()
