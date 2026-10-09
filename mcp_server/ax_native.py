"""Read the Accessibility tree with the native Swift observer (ax_observe.swift).

The AppleScript observer sends one System Events Apple Event per attribute per
node; a 130-node Finder window took ~33 s. The native observer batches each
node's attributes into one Accessibility call and returns the same record
format in tens to hundreds of milliseconds.

It is compiled on first use in the background and enabled only in the
CLI-managed server. Until it is ready, or whenever it cannot answer (not built,
app not found by name, Accessibility refused), callers fall back to the
AppleScript observer, so observation never depends on it. MAC_MCP_AX_NATIVE=0
turns it off.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

from . import swift_build
from .tool_cancellation import (
    ToolCancelledError,
    cancellation_checkpoint,
    register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)

_SOURCE = Path(__file__).resolve().with_name("ax_observe.swift")
_lock = threading.Lock()
_state = {"allowed": False, "executable": None, "building": False, "disabled": False}

# Exit codes from ax_observe.swift that mean "use the AppleScript observer".
_NOT_TRUSTED = 3
_APP_NOT_FOUND = 4


def enable() -> None:
    with _lock:
        _state["allowed"] = True


def _build() -> None:
    try:
        directory = swift_build.cache_dir("MAC_MCP_AX_NATIVE_CACHE", "~/.mac-mcp/cache/ax-observe")
        executable = swift_build.compile_cached(_SOURCE, directory, "ax-observe")
        with _lock:
            _state["executable"] = str(executable)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        with _lock:
            _state["disabled"] = True
    finally:
        with _lock:
            _state["building"] = False


def _executable() -> Optional[str]:
    if os.getenv("MAC_MCP_AX_NATIVE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _lock:
        if not _state["allowed"] or _state["disabled"]:
            return None
        if _state["executable"]:
            return str(_state["executable"])
        if not _state["building"]:
            _state["building"] = True
            threading.Thread(target=_build, name="ax-observe-build", daemon=True).start()
    return None


def observe(
    *,
    app: Optional[str],
    app_pid: Optional[int],
    window_index: int,
    max_depth: int,
    max_children: int,
    max_nodes: int,
    timeout_s: float,
) -> Optional[Tuple[bool, str, str]]:
    """Return (ok, raw_records, error), or None when the AppleScript observer should run."""
    executable = _executable()
    if executable is None:
        return None
    if app_pid is not None:
        target = ["--pid", str(int(app_pid))]
    elif app:
        target = ["--name", str(app)]
    else:
        target = ["--frontmost"]
    command = [
        executable, *target, "--window", str(int(window_index)), "--max-depth", str(int(max_depth)),
        "--max-children", str(int(max_children)), "--max-nodes", str(int(max_nodes)),
    ]
    cancellation_checkpoint()
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return None
    cleanup = register_cancellation_cleanup(proc.kill)
    try:
        stdout, stderr = proc.communicate(timeout=max(0.5, float(timeout_s)))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, "", f"Accessibility observation timed out after {timeout_s}s"
    except ToolCancelledError:
        proc.kill()
        proc.wait()
        raise
    finally:
        unregister_cancellation_cleanup(cleanup)
    if proc.returncode in {_NOT_TRUSTED, _APP_NOT_FOUND} or proc.returncode != 0:
        return None
    return True, stdout.decode("utf-8", "replace"), ""
