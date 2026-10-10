from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HELPER_BUILD_LOCK = threading.Lock()
# binary path -> event set when its single in-flight background build finishes.
_HELPER_BUILDS: Dict[str, threading.Event] = {}
_HELPER_BUILD_ERRORS: Dict[str, str] = {}
_HELPER_BUILD_TIMEOUT_S = 60
_FRAME_TOLERANCE = 4.0


def _cache_dir() -> Path:
    override = os.getenv("MAC_MCP_WINDOW_CAPTURE_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".mac-mcp" / "cache" / "native-window-capture").resolve()


def _helper_source() -> Path:
    return Path(__file__).with_name("window_list.swift")


def _build_helper(swiftc: str, target: str, source: Path, binary: Path, cache: Path) -> Optional[str]:
    try:
        cache.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                swiftc,
                "-O",
                "-target",
                target,
                "-framework",
                "CoreGraphics",
                str(source),
                "-o",
                str(binary),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_HELPER_BUILD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "WINDOW_CAPTURE_HELPER_BUILD_FAILED"
    if proc.returncode != 0:
        return "WINDOW_CAPTURE_HELPER_BUILD_FAILED"
    try:
        binary.chmod(0o700)
    except OSError:
        return "WINDOW_CAPTURE_HELPER_PERMISSION_FAILED"
    return None


def _helper_path(timeout_s: Optional[float] = None) -> Tuple[Optional[Path], Optional[str]]:
    """Return the compiled helper, waiting at most timeout_s for a cold build.

    A cold build runs once in the background and keeps going after the caller
    gives up, so an observation never outlasts its own deadline and the next
    request finds the helper ready.
    """
    source = _helper_source()
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not source.exists():
        return None, "WINDOW_CAPTURE_HELPER_SOURCE_MISSING"
    if not Path(swiftc).exists():
        return None, "WINDOW_CAPTURE_SWIFTC_UNAVAILABLE"

    arch = platform.machine().strip() or "arm64"
    target = f"{arch}-apple-macos13.0"
    fingerprint = source.read_bytes() + b"\0" + target.encode("utf-8")
    source_hash = hashlib.sha256(fingerprint).hexdigest()[:16]
    cache = _cache_dir()
    binary = cache / f"window-list-{source_hash}"
    if binary.exists() and os.access(binary, os.X_OK):
        return binary, None

    key = str(binary)
    with _HELPER_BUILD_LOCK:
        if binary.exists() and os.access(binary, os.X_OK):
            return binary, None
        done = _HELPER_BUILDS.get(key)
        if done is None or (done.is_set() and key in _HELPER_BUILD_ERRORS):
            # Start (or retry after a failed attempt) a single background build.
            _HELPER_BUILD_ERRORS.pop(key, None)
            done = threading.Event()
            _HELPER_BUILDS[key] = done

            def build() -> None:
                error = _build_helper(swiftc, target, source, binary, cache)
                with _HELPER_BUILD_LOCK:
                    if error:
                        _HELPER_BUILD_ERRORS[key] = error
                    else:
                        _HELPER_BUILDS.pop(key, None)
                done.set()

            threading.Thread(target=build, name="window-capture-helper-build", daemon=True).start()

    wait_s = _HELPER_BUILD_TIMEOUT_S if timeout_s is None else max(0.0, float(timeout_s))
    if not done.wait(wait_s):
        return None, "WINDOW_CAPTURE_HELPER_WARMING"
    with _HELPER_BUILD_LOCK:
        error = _HELPER_BUILD_ERRORS.get(key)
    if error:
        return None, error
    return binary, None


def _window_rows(timeout_s: float = 5.0) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    rows, _displays, error = _window_list(timeout_s)
    return rows, error


def displays(timeout_s: float = 5.0) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Active displays in global desktop points (main display top-left is 0,0; y grows down)."""
    _rows, found, error = _window_list(timeout_s)
    return found, error


def display_for_frame(found: Optional[List[Dict[str, Any]]], frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The display holding the largest part of a frame, or None when it is on no display."""
    best, best_area = None, 0.0
    fx, fy = _number(frame.get("x")), _number(frame.get("y"))
    fw, fh = _number(frame.get("width")), _number(frame.get("height"))
    if None in (fx, fy, fw, fh):
        return None
    for display in found or []:
        dx, dy = _number(display.get("x")) or 0.0, _number(display.get("y")) or 0.0
        dw, dh = _number(display.get("width")) or 0.0, _number(display.get("height")) or 0.0
        area = max(0.0, min(fx + fw, dx + dw) - max(fx, dx)) * max(0.0, min(fy + fh, dy + dh) - max(fy, dy))
        if area > best_area:
            best, best_area = display, area
    return best


def _window_list(
    timeout_s: float = 5.0,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]], Optional[str]]:
    budget = max(0.1, min(float(timeout_s), 15.0))
    started = time.monotonic()
    helper, error = _helper_path(budget)
    if helper is None:
        return None, None, error or "WINDOW_CAPTURE_HELPER_UNAVAILABLE"
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_TIMEOUT"
    try:
        proc = subprocess.run(
            [str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.1, remaining),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_TIMEOUT"
    except (OSError, subprocess.SubprocessError):
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_FAILED"
    if proc.returncode != 0:
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_FAILED"
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_INVALID"
    found_displays: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        found_displays = [row for row in payload.get("displays") or [] if isinstance(row, dict)]
        payload = payload.get("windows")
    if not isinstance(payload, list):
        return None, None, "WINDOW_CAPTURE_WINDOW_LIST_INVALID"
    return [row for row in payload if isinstance(row, dict)], found_displays, None


def _number(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_matches(row: Dict[str, Any], position: Dict[str, Any]) -> bool:
    for key in ("x", "y", "width", "height"):
        left = _number(row.get(key))
        right = _number(position.get(key))
        if left is None or right is None or abs(left - right) > _FRAME_TOLERANCE:
            return False
    return True


def resolve_window_id(
    pid: int,
    window: Dict[str, Any],
    *,
    timeout_s: float = 5.0,
    rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[int], Optional[str], Dict[str, Any]]:
    """Resolve one AX window to one CGWindowID without choosing an ambiguous sibling."""
    try:
        target_pid = int(pid)
    except (TypeError, ValueError):
        return None, "WINDOW_CAPTURE_PID_INVALID", {}
    if target_pid <= 0:
        return None, "WINDOW_CAPTURE_PID_INVALID", {}

    position = window.get("position") if isinstance(window.get("position"), dict) else {}
    if not all(_number(position.get(key)) is not None for key in ("x", "y", "width", "height")):
        return None, "WINDOW_CAPTURE_FRAME_UNAVAILABLE", {}

    source_rows = rows
    found_displays: Optional[List[Dict[str, Any]]] = None
    if source_rows is None:
        source_rows, found_displays, error = _window_list(timeout_s)
        if source_rows is None:
            return None, error or "WINDOW_CAPTURE_WINDOW_LIST_FAILED", {}

    candidates = []
    for row in source_rows:
        try:
            row_pid = int(row.get("pid") or 0)
            layer = int(row.get("layer") or 0)
        except (TypeError, ValueError):
            continue
        if row_pid != target_pid or layer != 0:
            continue
        if _frame_matches(row, position):
            candidates.append(row)

    title = str(window.get("title") or "").strip()
    if len(candidates) > 1 and title:
        titled = [row for row in candidates if str(row.get("title") or "").strip() == title]
        if titled:
            candidates = titled

    if not candidates:
        return None, "WINDOW_CAPTURE_TARGET_NOT_FOUND", {
            "candidate_count": 0,
            "match_basis": "pid+frame",
        }
    if len(candidates) != 1:
        return None, "WINDOW_CAPTURE_TARGET_AMBIGUOUS", {
            "candidate_count": len(candidates),
            "match_basis": "pid+frame+title" if title else "pid+frame",
        }

    match = candidates[0]
    try:
        window_id = int(match.get("id"))
    except (TypeError, ValueError):
        return None, "WINDOW_CAPTURE_ID_INVALID", {}
    if window_id <= 0:
        return None, "WINDOW_CAPTURE_ID_INVALID", {}

    frame = {key: _number(match.get(key)) for key in ("x", "y", "width", "height")}
    details: Dict[str, Any] = {
        "candidate_count": 1,
        "match_basis": "pid+frame+title" if title else "pid+frame",
        "on_screen": bool(match.get("onScreen", False)),
        "frame": frame,
    }
    if found_displays is not None:
        display = display_for_frame(found_displays, frame)
        details["display"] = display
        if display is None:
            details["on_screen"] = False
    return window_id, None, details
