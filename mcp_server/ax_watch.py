"""Accessibility change tokens from the native watcher (ax_watch.swift).

The watcher keeps an AXObserver per application and counts its notifications.
``change_token(pid)`` returns "session:generation"; when it still matches the
token taken before a full observation, the app has reported no UI change
since then, so a conditional observe can skip another full tree walk.

Any doubt returns None and callers walk the tree: the helper is not built yet
or not enabled (only the CLI-managed server enables it), Accessibility is
refused, the app refused the core notifications, the app exited, the helper
crashed or was slow, or MAC_MCP_AX_WATCH=0.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import threading
from pathlib import Path
from typing import Optional

from . import swift_build

_SOURCE = Path(__file__).resolve().with_name("ax_watch.swift")
_REPLY_TIMEOUT_S = 0.5
_FAILURE_LIMIT = 3

_lock = threading.Lock()
_state = {"allowed": False, "executable": None, "building": False, "disabled": False, "failures": 0}
_proc: Optional[subprocess.Popen] = None


def enable() -> None:
    with _lock:
        _state["allowed"] = True


def _build() -> None:
    try:
        directory = swift_build.cache_dir("MAC_MCP_AX_WATCH_CACHE", "~/.mac-mcp/cache/ax-watch")
        executable = swift_build.compile_cached(_SOURCE, directory, "ax-watch")
        with _lock:
            _state["executable"] = str(executable)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        with _lock:
            _state["disabled"] = True
    finally:
        with _lock:
            _state["building"] = False


def _ready_executable() -> Optional[str]:
    """Return the helper path once built; start one background build otherwise. Caller holds _lock."""
    if os.getenv("MAC_MCP_AX_WATCH", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not _state["allowed"] or _state["disabled"]:
        return None
    if _state["executable"]:
        return str(_state["executable"])
    if not _state["building"]:
        _state["building"] = True
        threading.Thread(target=_build, name="ax-watch-build", daemon=True).start()
    return None


def _stop_locked() -> None:
    global _proc
    proc, _proc = _proc, None
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdin, proc.stdout):
        try:
            stream.close()
        except (OSError, AttributeError):
            pass


def _fail_locked() -> None:
    _stop_locked()
    _state["failures"] += 1
    if _state["failures"] >= _FAILURE_LIMIT:
        _state["disabled"] = True


def _request_locked(executable: str, pid: int) -> Optional[dict]:
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = subprocess.Popen(
            [executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    proc = _proc
    try:
        proc.stdin.write((json.dumps({"op": "watch", "pid": int(pid)}) + "\n").encode())
        proc.stdin.flush()
        readable, _, _ = select.select([proc.stdout], [], [], _REPLY_TIMEOUT_S)
        if not readable:
            raise TimeoutError("ax_watch did not reply")
        line = proc.stdout.readline()
        if not line:
            raise EOFError("ax_watch exited")
        return json.loads(line)
    except (OSError, ValueError, TimeoutError, EOFError):
        _fail_locked()
        return None


def change_token(pid: Optional[int]) -> Optional[str]:
    """Return the app's current change token, registering the watch on first use."""
    if not pid or int(pid) <= 0:
        return None
    with _lock:
        executable = _ready_executable()
        if executable is None:
            return None
        reply = _request_locked(executable, int(pid))
        if not reply or not reply.get("ok") or not reply.get("watching"):
            return None
        _state["failures"] = 0
        return f"{reply.get('session')}:{int(reply.get('gen') or 0)}"


def shutdown() -> None:
    with _lock:
        _stop_locked()
