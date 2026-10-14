"""Ask macOS for a permission and read it fresh (the onboarding's "Allow" buttons).

Kept apart from permission_probe, which only reads states and must never prompt.

With Mac MCP Server installed (the app the permissions belong to, see server_helper), it
asks: opened as an app with `--request`, macOS shows its prompt naming Mac MCP. Asked from
inside the background server instead, macOS stays silent for Screen Recording. A process
also keeps the answer it saw when it started, so states are read from a fresh helper
process (`--check`) and the server is restarted once a new grant appears.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .permission_probe import DENIED, GRANTED, UNKNOWN, PERMISSIONS, _library, accessibility_state, screen_recording_state

REQUESTABLE = ("accessibility", "screen_recording")


def _prompt_accessibility() -> None:
    """AXIsProcessTrustedWithOptions with the prompt option: macOS asks once, naming the holder."""
    services = _library("ApplicationServices")
    foundation = _library("CoreFoundation")
    if services is None or foundation is None:
        return
    try:
        key = ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt")
        true = ctypes.c_void_p.in_dll(foundation, "kCFBooleanTrue")
        create = foundation.CFDictionaryCreate
        release = foundation.CFRelease
        trusted = services.AXIsProcessTrustedWithOptions
    except (AttributeError, ValueError):
        return
    create.restype = ctypes.c_void_p
    create.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                       ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    trusted.restype = ctypes.c_bool
    trusted.argtypes = [ctypes.c_void_p]
    release.argtypes = [ctypes.c_void_p]
    keys = (ctypes.c_void_p * 1)(key.value)
    values = (ctypes.c_void_p * 1)(true.value)
    options = create(None, keys, values, 1, None, None)
    try:
        trusted(options)
    finally:
        if options:
            release(options)


def _request_screen_capture() -> bool:
    graphics = _library("CoreGraphics")
    if graphics is None:
        return False
    try:
        request = graphics.CGRequestScreenCaptureAccess
    except AttributeError:
        return False
    request.restype = ctypes.c_bool
    request.argtypes = []
    return bool(request())


_RUNTIME = Path(__file__).resolve().parent.parent
_HELPER_NAMES = {"accessibility": "accessibility", "screen_recording": "screen-recording"}


def _helper(runtime: Optional[Path]) -> Optional[Path]:
    exe = (runtime or _RUNTIME) / ".helper" / "Mac MCP Server.app" / "Contents" / "MacOS" / "mac-mcp-server"
    return exe if exe.is_file() and os.access(exe, os.X_OK) else None


def _in_process(permission: str) -> str:
    return accessibility_state() if permission == "accessibility" else screen_recording_state()


def fresh_state(permission: str, *, runtime: Optional[Path] = None, run: Callable[..., Any] = subprocess.run) -> str:
    """The permission as a newly started Mac MCP Server sees it (this process may not, yet)."""
    exe = _helper(runtime)
    if exe is None:
        return _in_process(permission)
    try:
        out = run([str(exe), "--check", _HELPER_NAMES[permission]], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return _in_process(permission)
    # The helper prints granted or denied; anything else means it could not tell, not a refusal.
    answer = (out.stdout or "").strip()
    return answer if answer in (GRANTED, DENIED) else UNKNOWN


def permission_status(*, runtime: Optional[Path] = None, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Fresh states, and whether this server must restart to use a grant it cannot see yet."""
    status: Dict[str, Any] = {}
    restart = False
    for permission in REQUESTABLE:
        state = fresh_state(permission, runtime=runtime, run=run)
        status[permission] = state
        restart = restart or (state == GRANTED and _in_process(permission) != GRANTED)
    status["restart_needed"] = restart
    return status


def request_permission(
    permission: str, *, runtime: Optional[Path] = None, run: Callable[..., Any] = subprocess.run,
) -> Dict[str, Any]:
    if permission not in REQUESTABLE:
        raise ValueError(f"unknown permission: {permission}")
    exe = _helper(runtime)
    if exe is not None:
        bundle = exe.parents[2]
        problem = None
        try:
            # Opened as an app, Mac MCP Server asks and macOS shows its prompt; -W waits for it.
            opened = run(["/usr/bin/open", "-n", "-W", "-a", str(bundle), "--args", "--request", _HELPER_NAMES[permission]],
                         capture_output=True, text=True, timeout=60)
            if getattr(opened, "returncode", 0) not in (0, 1):  # 1 is the helper's own "not granted"
                problem = (getattr(opened, "stderr", "") or "").strip().splitlines()[-1:] or [f"exit {opened.returncode}"]
                problem = problem[0]
        except subprocess.TimeoutExpired:
            problem = "the prompt did not answer within a minute"
        except (OSError, subprocess.SubprocessError) as exc:
            problem = str(exc) or exc.__class__.__name__
        state = fresh_state(permission, runtime=runtime, run=run)
        result = {"permission": permission, "state": state,
                  "restart_needed": state == GRANTED and _in_process(permission) != GRANTED}
        if problem and state != GRANTED:
            result["prompt_error"] = _prompt_error(permission, problem)
        return result
    return _request_in_process(permission)


def _prompt_error(permission: str, problem: str) -> str:
    """What to do when macOS could not show its prompt (not the same as the owner saying no)."""
    where = str(PERMISSIONS.get(permission, {}).get("settings_path") or "System Settings > Privacy & Security")
    return (f"macOS could not show the permission prompt ({problem[:120]}). "
            f"Open {where} and turn on Mac MCP there.")


def _request_in_process(permission: str) -> Dict[str, Any]:
    """Ask macOS for a permission from the server process, so the prompt names its holder."""
    if permission == "accessibility":
        _prompt_accessibility()
        state = accessibility_state()
        return {"permission": permission, "state": state, "restart_needed": False}
    if permission == "screen_recording":
        _request_screen_capture()
        state = screen_recording_state()
        # The grant takes effect for this process only after a restart.
        return {"permission": permission, "state": state, "restart_needed": state != GRANTED}
    raise ValueError(f"unknown permission: {permission}")
