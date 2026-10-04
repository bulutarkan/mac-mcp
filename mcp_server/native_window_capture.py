from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HELPER_BUILD_LOCK = threading.Lock()
_FRAME_TOLERANCE = 4.0


def _cache_dir() -> Path:
    override = os.getenv("MAC_MCP_WINDOW_CAPTURE_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".mac-mcp" / "cache" / "native-window-capture").resolve()


def _helper_source() -> Path:
    return Path(__file__).with_name("window_list.swift")


def _helper_path() -> Tuple[Optional[Path], Optional[str]]:
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

    with _HELPER_BUILD_LOCK:
        if binary.exists() and os.access(binary, os.X_OK):
            return binary, None
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
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None, "WINDOW_CAPTURE_HELPER_BUILD_FAILED"
        if proc.returncode != 0:
            return None, "WINDOW_CAPTURE_HELPER_BUILD_FAILED"
        try:
            binary.chmod(0o700)
        except OSError:
            return None, "WINDOW_CAPTURE_HELPER_PERMISSION_FAILED"
        return binary, None


def _window_rows(timeout_s: float = 5.0) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    helper, error = _helper_path()
    if helper is None:
        return None, error or "WINDOW_CAPTURE_HELPER_UNAVAILABLE"
    try:
        proc = subprocess.run(
            [str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.1, min(float(timeout_s), 15.0)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "WINDOW_CAPTURE_WINDOW_LIST_TIMEOUT"
    except (OSError, subprocess.SubprocessError):
        return None, "WINDOW_CAPTURE_WINDOW_LIST_FAILED"
    if proc.returncode != 0:
        return None, "WINDOW_CAPTURE_WINDOW_LIST_FAILED"
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None, "WINDOW_CAPTURE_WINDOW_LIST_INVALID"
    if not isinstance(payload, list):
        return None, "WINDOW_CAPTURE_WINDOW_LIST_INVALID"
    return [row for row in payload if isinstance(row, dict)], None


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
    if source_rows is None:
        source_rows, error = _window_rows(timeout_s)
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

    return window_id, None, {
        "candidate_count": 1,
        "match_basis": "pid+frame+title" if title else "pid+frame",
        "on_screen": bool(match.get("onScreen", False)),
    }
