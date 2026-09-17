from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp.utilities.types import Image

from .native_targets import (
    decorate_metadata as _decorate_native_metadata,
    lookup_app as _lookup_app_handle,
    lookup_window as _lookup_window_handle,
    public_window_rows as _public_window_rows,
    rebase_element_id as _rebase_element_id,
    window_by_handle as _window_by_handle,
    window_handle_map as _window_handle_map,
)
from .security import Settings, truncate


_FIELD_SEPARATOR = chr(31)
_RECORD_SEPARATOR = chr(30)
_ELEMENT_ID_RE = re.compile(r"^w[1-9][0-9]*(?:/[1-9][0-9]*)*$")
_OBSERVATION_TTL_S = 300
_MAX_OBSERVATIONS = 64
_MAX_ACTIONS = 20
_MAX_TEXT_CHARS = 100_000
_OBSERVE_BUDGET_S = 30
_ACTION_BUDGET_S = 60
_SCREENSHOT_FORMAT = "jpeg"
_SCREENSHOT_MAX_DIMENSION = 1600
_SCREENSHOT_MAX_BYTES = 600_000

_KEY_CODES: Dict[str, int] = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "pageup": 116,
    "page_up": 116,
    "pagedown": 121,
    "page_down": 121,
    "home": 115,
    "end": 119,
    "forwarddelete": 117,
    "forward_delete": 117,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}

_MODIFIER_MAP: Dict[str, str] = {
    "cmd": "command down",
    "command": "command down",
    "meta": "command down",
    "opt": "option down",
    "option": "option down",
    "alt": "option down",
    "ctrl": "control down",
    "control": "control down",
    "shift": "shift down",
    "fn": "function down",
}

_RISKY_WORDS = {
    "delete", "remove", "trash", "discard", "send", "submit", "publish",
    "purchase", "buy", "pay", "payment", "confirm", "approve", "logout",
    "sign out", "sil", "silme", "gönder", "gonder", "yayınla", "yayinla",
    "satın al", "satin al", "öde", "ode", "onayla", "çıkış", "cikis",
}

_OBSERVATIONS: Dict[str, Dict[str, Any]] = {}
_OBSERVATIONS_LOCK = threading.Lock()


def _operation_timeout(deadline: Optional[float], fallback_s: float) -> float:
    if deadline is None:
        return max(0.1, min(float(fallback_s), 120.0))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("macOS UI action time budget exceeded")
    return max(0.1, min(float(fallback_s), remaining))


def _terminate_process_group(proc: subprocess.Popen[str], grace_s: float = 0.5) -> None:
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


def _resize_screenshot(path: str, timeout_s: float) -> Optional[str]:
    """Resize screenshots before returning them to MCP clients."""
    executable = shutil.which("sips") or "/usr/bin/sips"
    if not Path(executable).exists():
        return "sips is not available to resize the screenshot"
    proc = subprocess.Popen(
        [executable, "-Z", str(_SCREENSHOT_MAX_DIMENSION), path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return f"screenshot resize timed out after {timeout_s}s"
    except Exception as exc:
        return f"Could not resize screenshot: {exc}"
    if proc.returncode != 0:
        return (stderr or "sips failed").strip()
    return None


def _run_osascript(script: str, timeout_s: float = 30) -> Tuple[bool, str, str]:
    timeout_s = max(0.1, min(float(timeout_s), 120.0))
    proc = subprocess.Popen(
        ["osascript", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=script, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return False, "", f"AppleScript timed out after {timeout_s}s"
    except Exception as exc:
        return False, "", f"Could not run osascript: {exc}"

    return proc.returncode == 0, (stdout or "").strip(), (stderr or "").strip()


def _apple_string(value: str) -> str:
    """Return a safe AppleScript string literal for short control values."""
    value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\r", "\\r").replace("\n", "\\r")
    return f'"{value}"'


def _normalize_app(app: Optional[str]) -> Optional[str]:
    if app is None:
        return None
    value = str(app).strip()
    if not value or value.lower() in {"frontmost", "active", "current"}:
        return None
    if len(value) > 200:
        raise ValueError("app must be at most 200 characters")
    return value


def _validate_element_id(element_id: Any) -> str:
    if not isinstance(element_id, str) or not _ELEMENT_ID_RE.fullmatch(element_id):
        raise ValueError("element_id must look like 'w1/2/1' from mac_observe")
    return element_id


def _element_expression(element_id: str) -> str:
    """Translate a bounded observation path into an AppleScript object specifier."""
    parts = element_id.split("/")
    expression = f"window {int(parts[0][1:])}"
    for part in parts[1:]:
        expression = f"UI element {int(part)} of {expression}"
    return expression


def _parse_number(value: str) -> Optional[int]:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def _parse_observation(raw: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    records = [record for record in raw.split(_RECORD_SEPARATOR) if record]
    metadata: Dict[str, Any] = {"windows": []}
    nodes: List[Dict[str, Any]] = []

    for record in records:
        fields = record.split(_FIELD_SEPARATOR)
        if not fields:
            continue
        if fields[0] == "__META__":
            if len(fields) < 5:
                continue
            metadata.update({
                "active_app": fields[1],
                "frontmost": _parse_bool(fields[2]),
                "window_count": _parse_number(fields[3]) or 0,
                "window_names": [name for name in fields[4].split(" || ") if name],
                "pid": _parse_number(fields[5]) if len(fields) > 5 else None,
                "bundle_id": fields[6] if len(fields) > 6 else "",
            })
            continue
        if fields[0] == "__WINDOW__":
            if len(fields) < 12:
                continue
            metadata.setdefault("windows", []).append({
                "index": _parse_number(fields[1]) or 0,
                "title": fields[2],
                "document": fields[3],
                "identifier": fields[4],
                "position": {
                    "x": _parse_number(fields[5]),
                    "y": _parse_number(fields[6]),
                    "width": _parse_number(fields[7]),
                    "height": _parse_number(fields[8]),
                },
                "subrole": fields[9],
                "focused": _parse_bool(fields[10]),
                "main": _parse_bool(fields[11]),
            })
            continue
        if fields[0] != "__NODE__" or len(fields) < 16:
            continue

        x = _parse_number(fields[8])
        y = _parse_number(fields[9])
        width = _parse_number(fields[10])
        height = _parse_number(fields[11])
        actions = [item.strip() for item in fields[14].split(",") if item.strip()]
        nodes.append({
            "element_id": fields[1],
            "parent_id": fields[2] or None,
            "role": fields[3],
            "subrole": fields[4],
            "title": fields[5],
            "description": fields[6],
            "value": fields[7],
            "position": {"x": x, "y": y, "width": width, "height": height},
            "enabled": _parse_bool(fields[12]),
            "focused": _parse_bool(fields[13]),
            "actions": actions,
            "child_count": _parse_number(fields[15]) or 0,
        })

    return metadata, nodes


def _observation_script(
    app: Optional[str],
    window_index: int,
    max_depth: int,
    max_children: int,
    max_nodes: int = 500,
    app_pid: Optional[int] = None,
) -> str:
    if app_pid is not None:
        app_selection = f"set p to first application process whose unix id is {int(app_pid)}"
    else:
        app_selection = (
            "set p to first application process whose frontmost is true"
            if app is None
            else f"set p to first application process whose name is {_apple_string(app)}"
        )
    window_condition = (
        "if windowIndex is 0 or wi is windowIndex then"
        if window_index == 0
        else "if wi is windowIndex then"
    )

    return f'''use scripting additions

on cleanText(v, fs, rs)
    try
        set t to v as text
    on error
        set t to ""
    end try
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {{return, linefeed, tab, fs, rs}}
    set parts to every text item of t
    set AppleScript's text item delimiters to " "
    set t to parts as text
    set AppleScript's text item delimiters to oldDelims
    if (length of t) > 4000 then set t to text 1 thru 4000 of t
    return t
end cleanText

using terms from application "System Events"
on nodeRecord(nodeRef, nodeId, parentId, fs, rs)
    set roleText to ""
    set subroleText to ""
    set titleText to ""
    set descriptionText to ""
    set valueText to ""
    set xText to ""
    set yText to ""
    set widthText to ""
    set heightText to ""
    set enabledText to "false"
    set focusedText to "false"
    set actionText to ""
    set childCountText to "0"

    tell application "System Events"
        try
            set roleText to role of nodeRef as text
        end try
        try
            set subroleText to subrole of nodeRef as text
        end try
        try
            set titleText to title of nodeRef as text
        end try
        try
            set descriptionText to description of nodeRef as text
        end try
        try
            set valueText to value of nodeRef as text
        end try
        if roleText contains "SecureText" or subroleText contains "Secure" then set valueText to "[redacted]"
        try
            set p to position of nodeRef
            set xText to item 1 of p as text
            set yText to item 2 of p as text
        end try
        try
            set s to size of nodeRef
            set widthText to item 1 of s as text
            set heightText to item 2 of s as text
        end try
        try
            set enabledText to (enabled of nodeRef) as text
        end try
        try
            set focusedText to (focused of nodeRef) as text
        end try
        try
            set actionNames to name of actions of nodeRef
            set actionText to actionNames as text
        end try
        try
            set childCountText to (count of UI elements of nodeRef) as text
        end try
    end tell

    return "__NODE__" & fs & my cleanText(nodeId, fs, rs) & fs & my cleanText(parentId, fs, rs) & fs & ¬
        my cleanText(roleText, fs, rs) & fs & my cleanText(subroleText, fs, rs) & fs & ¬
        my cleanText(titleText, fs, rs) & fs & my cleanText(descriptionText, fs, rs) & fs & ¬
        my cleanText(valueText, fs, rs) & fs & my cleanText(xText, fs, rs) & fs & ¬
        my cleanText(yText, fs, rs) & fs & my cleanText(widthText, fs, rs) & fs & ¬
        my cleanText(heightText, fs, rs) & fs & my cleanText(enabledText, fs, rs) & fs & ¬
        my cleanText(focusedText, fs, rs) & fs & my cleanText(actionText, fs, rs) & fs & ¬
        my cleanText(childCountText, fs, rs)
end nodeRecord

on walkNode(nodeRef, nodeId, parentId, depth, maxDepth, maxChildren, maxNodes, recordList, counter, fs, rs)
    if (item 1 of counter) is greater than or equal to maxNodes then return
    set item 1 of counter to ((item 1 of counter) + 1)
    set end of recordList to my nodeRecord(nodeRef, nodeId, parentId, fs, rs)
    if depth is greater than or equal to maxDepth then return

    tell application "System Events"
        try
            set children to UI elements of nodeRef
            set childIndex to 1
            repeat with childItem in children
                if childIndex is greater than maxChildren then exit repeat
                if (item 1 of counter) is greater than or equal to maxNodes then exit repeat
                set childRef to contents of childItem
                my walkNode(childRef, nodeId & "/" & childIndex, nodeId, depth + 1, maxDepth, maxChildren, maxNodes, recordList, counter, fs, rs)
                set childIndex to childIndex + 1
            end repeat
        end try
    end tell
end walkNode
end using terms from

set fs to character id 31
set rs to character id 30
set windowIndex to {window_index}
set maxDepth to {max_depth}
set maxChildren to {max_children}
set maxNodes to {max_nodes}
set recordList to {{}}
set counter to {{0}}

tell application "System Events"
    {app_selection}
    set processName to name of p as text
    set processPid to ""
    try
        set processPid to unix id of p as text
    end try
    set bundleId to ""
    try
        set bundleId to bundle identifier of p as text
    end try
    set isFrontmost to false
    try
        set isFrontmost to frontmost of p
    end try
    set windowCount to count of windows of p
    set windowNames to ""
    repeat with wi from 1 to windowCount
        try
            set windowName to name of window wi of p as text
        on error
            set windowName to ""
        end try
        if windowName is not "" then
            if windowNames is not "" then set windowNames to windowNames & " || "
            set windowNames to windowNames & windowName
        end if
    end repeat
    set meta to "__META__" & fs & my cleanText(processName, fs, rs) & fs & (isFrontmost as text) & fs & ¬
        (windowCount as text) & fs & my cleanText(windowNames, fs, rs) & fs & ¬
        my cleanText(processPid, fs, rs) & fs & my cleanText(bundleId, fs, rs)
    set end of recordList to meta

    repeat with wi from 1 to windowCount
        try
            set w to window wi of p
            set windowTitle to ""
            set windowDocument to ""
            set windowIdentifier to ""
            set windowX to ""
            set windowY to ""
            set windowWidth to ""
            set windowHeight to ""
            set windowSubrole to ""
            set windowFocused to "false"
            set windowMain to "false"
            try
                set windowTitle to title of w as text
            end try
            try
                set windowDocument to value of attribute "AXDocument" of w as text
            end try
            try
                set windowIdentifier to value of attribute "AXIdentifier" of w as text
            end try
            try
                set wp to position of w
                set windowX to item 1 of wp as text
                set windowY to item 2 of wp as text
            end try
            try
                set ws to size of w
                set windowWidth to item 1 of ws as text
                set windowHeight to item 2 of ws as text
            end try
            try
                set windowSubrole to subrole of w as text
            end try
            try
                set windowFocused to value of attribute "AXFocused" of w as text
            end try
            try
                set windowMain to value of attribute "AXMain" of w as text
            end try
            set windowRecord to "__WINDOW__" & fs & (wi as text) & fs & ¬
                my cleanText(windowTitle, fs, rs) & fs & my cleanText(windowDocument, fs, rs) & fs & ¬
                my cleanText(windowIdentifier, fs, rs) & fs & my cleanText(windowX, fs, rs) & fs & ¬
                my cleanText(windowY, fs, rs) & fs & my cleanText(windowWidth, fs, rs) & fs & ¬
                my cleanText(windowHeight, fs, rs) & fs & my cleanText(windowSubrole, fs, rs) & fs & ¬
                my cleanText(windowFocused, fs, rs) & fs & my cleanText(windowMain, fs, rs)
            set end of recordList to windowRecord
            {window_condition}
                my walkNode(w, "w" & wi, "", 0, maxDepth, maxChildren, maxNodes, recordList, counter, fs, rs)
            end if
        end try
    end repeat
end tell

set AppleScript's text item delimiters to rs
set outputText to recordList as text
set AppleScript's text item delimiters to ""
return outputText
'''


def _capture_screen(timeout_s: float = 15) -> Tuple[Optional[bytes], Optional[str]]:
    fd, path = tempfile.mkstemp(prefix="mac-mcp-screen-", suffix=".jpg")
    os.close(fd)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["/usr/sbin/screencapture", "-x", "-t", _SCREENSHOT_FORMAT, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _, stderr = proc.communicate(timeout=_operation_timeout(None, timeout_s))
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            proc.wait()
            return None, f"screencapture timed out after {timeout_s}s"
        if proc.returncode != 0:
            message = (stderr or "").strip() or "screencapture failed"
            return None, message

        remaining = timeout_s - (time.monotonic() - started)
        if remaining > 0.1:
            _resize_screenshot(path, min(5.0, remaining))
        data = Path(path).read_bytes()
        if not data:
            return None, "screencapture returned an empty image"
        if len(data) > _SCREENSHOT_MAX_BYTES:
            return None, (
                f"screenshot omitted because its encoded size ({len(data)} bytes) "
                f"exceeds the {_SCREENSHOT_MAX_BYTES}-byte connector safety limit"
            )
        return data, None
    except Exception as exc:
        return None, f"Could not capture screen: {exc}"
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _ocr_image(image_data: bytes, timeout_s: float = 20) -> Tuple[Optional[str], Optional[str]]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return None, "OCR unavailable: tesseract is not installed"

    fd, path = tempfile.mkstemp(prefix="mac-mcp-ocr-", suffix=".jpg")
    os.close(fd)
    try:
        Path(path).write_bytes(image_data)
        language = "eng"
        try:
            langs = subprocess.run(
                [tesseract, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=min(10.0, _operation_timeout(None, timeout_s)),
            ).stdout
            available = {
                line.strip()
                for line in langs.splitlines()
                if line.strip() and not line.startswith("List")
            }
            if {"eng", "tur"}.issubset(available):
                language = "tur+eng"
            elif "tur" in available:
                language = "tur"
        except Exception:
            pass

        proc = subprocess.run(
            [tesseract, path, "stdout", "-l", language, "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=_operation_timeout(None, timeout_s),
        )
        if proc.returncode != 0:
            return None, proc.stderr.strip() or "tesseract failed"
        text, _ = truncate(proc.stdout.strip(), 20_000)
        return text, None
    except subprocess.TimeoutExpired:
        return None, f"OCR timed out after {timeout_s}s"
    except Exception as exc:
        return None, f"Could not run OCR: {exc}"
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _save_observation(
    active_app: str,
    window_index: int,
    nodes: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> str:
    observation_id = f"obs_{uuid.uuid4().hex}"
    now = time.time()
    window_handles = _window_handle_map(metadata)
    with _OBSERVATIONS_LOCK:
        _OBSERVATIONS[observation_id] = {
            "active_app": active_app,
            "app_handle": metadata.get("app_handle"),
            "app_pid": metadata.get("pid"),
            "bundle_id": metadata.get("bundle_id") or "",
            "window_index": window_index,
            "window_handles": window_handles,
            "selected_window_handle": window_handles.get(window_index) if window_index > 0 else None,
            "created_at": now,
            "nodes": {node["element_id"]: node for node in nodes},
        }
        expired = [
            key for key, value in _OBSERVATIONS.items()
            if now - float(value.get("created_at", now)) > _OBSERVATION_TTL_S
        ]
        for key in expired:
            _OBSERVATIONS.pop(key, None)
        while len(_OBSERVATIONS) > _MAX_OBSERVATIONS:
            oldest = min(_OBSERVATIONS, key=lambda key: _OBSERVATIONS[key].get("created_at", now))
            _OBSERVATIONS.pop(oldest, None)
    return observation_id


def _get_observation(observation_id: str) -> Optional[Dict[str, Any]]:
    with _OBSERVATIONS_LOCK:
        observation = _OBSERVATIONS.get(observation_id)
        if observation is None:
            return None
        if time.time() - float(observation.get("created_at", 0)) > _OBSERVATION_TTL_S:
            _OBSERVATIONS.pop(observation_id, None)
            return None
        return observation


def _native_target_error(reason_code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": False,
        "error": message,
        "reason_code": reason_code,
        "retryable": reason_code in {
            "WINDOW_HANDLE_UNKNOWN", "APP_HANDLE_UNKNOWN", "STALE_WINDOW_HANDLE",
            "STALE_APP_HANDLE", "WINDOW_IDENTITY_UNAVAILABLE",
        },
    }
    payload.update(extra)
    return payload


def _scan_native_windows(
    app: Optional[str], app_pid: Optional[int], deadline: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ok, raw, error = _run_osascript(
        _observation_script(app, 0, 0, 1, max_nodes=100, app_pid=app_pid),
        timeout_s=_operation_timeout(deadline, 15),
    )
    if not ok:
        return None, error or "Could not resolve the native application/window target."
    metadata, _ = _parse_observation(raw)
    return _decorate_native_metadata(metadata), None


def _resolve_registered_native_target(
    app: Optional[str],
    app_handle: Optional[str],
    window_handle: Optional[str],
    deadline: Optional[float] = None,
) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    normalized_app = _normalize_app(app)
    if window_handle:
        record = _lookup_window_handle(str(window_handle))
        if record is None:
            return None, None, None, None, _native_target_error(
                "WINDOW_HANDLE_UNKNOWN",
                "window_handle is unknown or expired; call mac_observe again using app/window_index.",
                window_handle=window_handle,
            )
        if app_handle and record.get("app_handle") != app_handle:
            return None, None, None, None, _native_target_error(
                "TARGET_HANDLE_MISMATCH", "app_handle and window_handle refer to different targets."
            )
        if normalized_app and normalized_app.lower() != str(record.get("app_name") or "").lower():
            return None, None, None, None, _native_target_error(
                "TARGET_HANDLE_MISMATCH", "app does not match the application bound to window_handle."
            )
        metadata, error = _scan_native_windows(
            str(record.get("app_name") or ""), int(record.get("pid") or 0), deadline
        )
        if metadata is None or metadata.get("app_handle") != record.get("app_handle"):
            return None, None, None, None, _native_target_error(
                "STALE_APP_HANDLE", "The application process bound to window_handle is no longer available.",
                app_handle=record.get("app_handle"), window_handle=window_handle,
            )
        window = _window_by_handle(metadata, str(window_handle))
        if window is None:
            return None, None, None, None, _native_target_error(
                "STALE_WINDOW_HANDLE", "The window bound to window_handle no longer exists or changed identity.",
                app_handle=record.get("app_handle"), window_handle=window_handle,
            )
        return (
            str(metadata.get("active_app") or record.get("app_name") or ""),
            int(metadata.get("pid") or record.get("pid") or 0),
            int(window.get("index") or 0),
            metadata,
            None,
        )

    if app_handle:
        record = _lookup_app_handle(str(app_handle))
        if record is None:
            return None, None, None, None, _native_target_error(
                "APP_HANDLE_UNKNOWN",
                "app_handle is unknown or expired; call mac_observe again using the application name.",
                app_handle=app_handle,
            )
        if normalized_app and normalized_app.lower() != str(record.get("app_name") or "").lower():
            return None, None, None, None, _native_target_error(
                "TARGET_HANDLE_MISMATCH", "app does not match the application bound to app_handle."
            )
        metadata, error = _scan_native_windows(
            str(record.get("app_name") or ""), int(record.get("pid") or 0), deadline
        )
        if metadata is None or metadata.get("app_handle") != app_handle:
            return None, None, None, None, _native_target_error(
                "STALE_APP_HANDLE", "The application process bound to app_handle is no longer available.",
                app_handle=app_handle,
            )
        return (
            str(metadata.get("active_app") or record.get("app_name") or ""),
            int(metadata.get("pid") or record.get("pid") or 0),
            None,
            metadata,
            None,
        )

    return normalized_app, None, None, None, None


def _format_result(payload: Dict[str, Any], image_data: Optional[bytes] = None) -> Any:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if image_data:
        return [text, Image(data=image_data, format=_SCREENSHOT_FORMAT)]
    return text


def _collect_observation(
    settings: Settings,
    app: Optional[str],
    window_index: int,
    max_depth: int,
    max_children: int,
    include_screenshot: bool,
    ocr: bool,
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[Dict[str, Any], Optional[bytes]]:
    local_deadline = time.monotonic() + _OBSERVE_BUDGET_S
    if deadline is not None:
        local_deadline = min(local_deadline, deadline)
    ok, raw, error = _run_osascript(
        _observation_script(app, window_index, max_depth, max_children, app_pid=app_pid),
        timeout_s=_operation_timeout(local_deadline, 20),
    )
    if not ok:
        return {
            "ok": False,
            "error": error or "Could not read macOS Accessibility state.",
            "hint": "Grant Accessibility permission to the process running mac-mcp in System Settings > Privacy & Security > Accessibility.",
        }, None

    metadata, nodes = _parse_observation(raw)
    metadata = _decorate_native_metadata(metadata)
    active_app = str(metadata.get("active_app") or app or "")
    windows = list(metadata.get("windows") or [])
    selected_window: Optional[Dict[str, Any]] = None
    if window_index > 0:
        selected_window = next(
            (row for row in windows if int(row.get("index") or 0) == int(window_index)), None
        )
        if selected_window is None:
            return _native_target_error(
                "WINDOW_NOT_FOUND",
                f"window_index {window_index} does not exist for {active_app or 'the target application'}.",
                app_handle=metadata.get("app_handle"),
            ), None

    observation_id = _save_observation(active_app, window_index, nodes, metadata)

    image_data: Optional[bytes] = None
    screenshot_error: Optional[str] = None
    if include_screenshot or ocr:
        try:
            image_data, screenshot_error = _capture_screen(
                _operation_timeout(local_deadline, 10)
            )
        except TimeoutError as exc:
            image_data, screenshot_error = None, str(exc)

    selected_handle = selected_window.get("window_handle") if selected_window else None
    payload: Dict[str, Any] = {
        "ok": True,
        "observation_id": observation_id,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "active_app": active_app,
        "app_handle": metadata.get("app_handle"),
        "app_pid": metadata.get("pid"),
        "bundle_id": metadata.get("bundle_id") or "",
        "window_handle": selected_handle,
        "window_index": window_index,
        "targeting_status": (
            selected_window.get("identity_status") if selected_window else "multi_window"
        ),
        "frontmost": bool(metadata.get("frontmost", False)),
        "window_count": int(metadata.get("window_count") or 0),
        "window_names": metadata.get("window_names", []),
        "windows": _public_window_rows(metadata),
        "node_count": len(nodes),
        "nodes": nodes,
        "screenshot": {
            "requested": include_screenshot,
            "included_as_image_content": bool(image_data and include_screenshot),
            "mime_type": f"image/{_SCREENSHOT_FORMAT}" if image_data and include_screenshot else None,
        },
    }
    if selected_window is not None and not selected_handle:
        payload["targeting_reason"] = "WINDOW_IDENTITY_AMBIGUOUS_OR_UNAVAILABLE"
    if screenshot_error:
        payload["screenshot"]["error"] = screenshot_error

    if ocr:
        if image_data:
            try:
                ocr_text, ocr_error = _ocr_image(
                    image_data, _operation_timeout(local_deadline, 15)
                )
            except TimeoutError as exc:
                ocr_text, ocr_error = None, str(exc)
            payload["ocr"] = {
                "requested": True,
                "ok": ocr_error is None,
                "text": ocr_text or "",
            }
            if ocr_error:
                payload["ocr"]["error"] = ocr_error
        else:
            payload["ocr"] = {
                "requested": True,
                "ok": False,
                "text": "",
                "error": screenshot_error or "OCR could not capture the screen",
            }

    return payload, image_data if include_screenshot else None


def observe_ui(
    settings: Settings,
    app: Optional[str] = None,
    window_index: int = 1,
    max_depth: int = 5,
    max_children: int = 30,
    include_screenshot: bool = True,
    ocr: bool = False,
    app_handle: Optional[str] = None,
    window_handle: Optional[str] = None,
) -> Any:
    """Read a macOS app/window Accessibility tree with stable native target handles."""
    try:
        normalized_app = _normalize_app(app)
        if window_index < 0:
            return {"ok": False, "error": "window_index must be 0 (all) or a positive window number."}
        max_depth = max(0, min(int(max_depth), 8))
        max_children = max(1, min(int(max_children), 100))
        deadline = time.monotonic() + _OBSERVE_BUDGET_S
        resolved_app, resolved_pid, resolved_window_index, _, target_error = _resolve_registered_native_target(
            normalized_app, app_handle, window_handle, deadline
        )
        if target_error is not None:
            return target_error
        if window_handle:
            if not resolved_window_index:
                return _native_target_error(
                    "STALE_WINDOW_HANDLE", "Could not resolve window_handle to a current window."
                )
            window_index = int(resolved_window_index)
        payload, image_data = _collect_observation(
            settings,
            resolved_app,
            int(window_index),
            max_depth,
            max_children,
            bool(include_screenshot),
            bool(ocr),
            deadline=deadline,
            app_pid=resolved_pid,
        )
        return _format_result(payload, image_data)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Could not observe macOS UI: {exc}"}


def _resolve_app(app: Optional[str], deadline: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    normalized = _normalize_app(app)
    selection = (
        "set p to first application process whose frontmost is true"
        if normalized is None
        else f"set p to first application process whose name is {_apple_string(normalized)}"
    )
    ok, stdout, stderr = _run_osascript(
        f'''tell application "System Events"
    {selection}
    return name of p as text
end tell''',
        timeout_s=_operation_timeout(deadline, 15),
    )
    if not ok:
        return None, stderr or "Could not resolve the target application."
    return stdout.strip(), None


def _target_script(
    app: str, element_id: str, body: str, activate: bool = True, app_pid: Optional[int] = None,
) -> str:
    expression = _element_expression(element_id)
    activation = "set frontmost to true" if activate else ""
    selection = (
        f"set p to first application process whose unix id is {int(app_pid)}"
        if app_pid is not None
        else f"set p to first application process whose name is {_apple_string(app)}"
    )
    return f'''tell application "System Events"
    {selection}
    tell p
        {activation}
        set targetElement to {expression}
        {body}
    end tell
end tell'''


def _process_script(
    app: str, body: str, activate: bool = True, app_pid: Optional[int] = None,
) -> str:
    activation = "set frontmost to true" if activate else ""
    selection = (
        f"set p to first application process whose unix id is {int(app_pid)}"
        if app_pid is not None
        else f"set p to first application process whose name is {_apple_string(app)}"
    )
    return f'''tell application "System Events"
    {selection}
    tell p
        {activation}
        {body}
    end tell
end tell'''


def _run_cliclick(arguments: List[str], timeout_s: float = 30) -> Tuple[bool, str]:
    executable = shutil.which("cliclick") or "/opt/homebrew/bin/cliclick"
    if not Path(executable).exists():
        return False, "cliclick is not installed; coordinate mouse/typing actions are unavailable."
    timeout_s = max(0.1, min(float(timeout_s), 120.0))
    proc = subprocess.Popen(
        [executable, "-w", "20", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return False, f"cliclick timed out after {timeout_s}s"
    except Exception as exc:
        return False, f"Could not run cliclick: {exc}"
    if proc.returncode != 0:
        return False, (stderr or stdout or "").strip() or "cliclick failed"
    return True, ""


def _coordinate(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer coordinate")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer coordinate") from exc
    if number < -100_000 or number > 100_000:
        raise ValueError(f"{name} is outside the supported screen coordinate range")
    return number


def _node_coordinates(node: Optional[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    if not node:
        return None
    position = node.get("position") or {}
    x = position.get("x")
    y = position.get("y")
    width = position.get("width") or 0
    height = position.get("height") or 0
    if x is None or y is None:
        return None
    try:
        return int(x + max(1, width / 2)), int(y + max(1, height / 2))
    except (TypeError, ValueError):
        return None


def _click(
    app: str,
    action: Dict[str, Any],
    node: Optional[Dict[str, Any]],
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    click_count = int(action.get("click_count", 2 if action.get("type") == "double_click" else 1))
    if click_count not in {1, 2}:
        return False, "click_count must be 1 or 2"
    button = str(action.get("button", "left")).lower()
    if button not in {"left", "right"}:
        return False, "button must be 'left' or 'right'"

    element_id = action.get("element_id")
    if element_id is not None:
        try:
            element_id = _validate_element_id(element_id)
        except ValueError as exc:
            return False, str(exc)
        click_body = (
            'perform action "AXPress" of targetElement'
            if click_count == 1 and button == "left"
            else (
                "click targetElement\n        delay 0.1\n        click targetElement"
                if click_count == 2 and button == "left"
                else "click targetElement"
            )
        )
        ok, _, error = _run_osascript(
            _target_script(app, element_id, click_body, app_pid=app_pid),
            timeout_s=_operation_timeout(deadline, 30),
        )
        if ok:
            return True, "semantic click completed"
        if app_pid is not None:
            return False, error or "semantic click failed for stable native target; coordinate fallback disabled"
        fallback = _node_coordinates(node)
        if fallback is None:
            return False, error or "semantic click failed"
        x, y = fallback
    else:
        try:
            x = _coordinate(action.get("x"), "x")
            y = _coordinate(action.get("y"), "y")
        except ValueError as exc:
            return False, str(exc)

    prefix = "rc" if button == "right" else ("dc" if click_count == 2 else "c")
    ok, error = _run_cliclick(
        [f"{prefix}:{x},{y}"], timeout_s=_operation_timeout(deadline, 30)
    )
    return ok, error or "coordinate click completed"


def _focus_element(
    app: str, element_id: str, deadline: Optional[float] = None, app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    ok, _, error = _run_osascript(
        _target_script(app, element_id, "click targetElement", app_pid=app_pid),
        timeout_s=_operation_timeout(deadline, 30),
    )
    return ok, error


def _set_clipboard(text: str, deadline: Optional[float] = None) -> Tuple[bool, str]:
    timeout_s = _operation_timeout(deadline, 10)
    proc = subprocess.Popen(
        ["pbcopy"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(input=text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return False, f"pbcopy timed out after {timeout_s}s"
    except Exception as exc:
        return False, f"Could not access clipboard: {exc}"
    if proc.returncode != 0:
        return False, (stderr or "").strip() or "pbcopy failed"
    return True, ""


def _get_clipboard(deadline: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    timeout_s = _operation_timeout(deadline, 10)
    proc = subprocess.Popen(
        ["pbpaste"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return None, f"pbpaste timed out after {timeout_s}s"
    except Exception as exc:
        return None, f"Could not access clipboard: {exc}"
    if proc.returncode != 0:
        return None, (stderr or "").strip() or "pbpaste failed"
    return stdout or "", None


def _paste_text(
    app: str,
    element_id: str,
    text: str,
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    focused, focus_error = _focus_element(app, element_id, deadline, app_pid)
    if not focused:
        return False, focus_error or "Could not focus target element"
    previous, previous_error = _get_clipboard(deadline)
    if previous is None:
        return False, previous_error or "Could not save the current clipboard"
    copied, copy_error = _set_clipboard(text, deadline)
    if not copied:
        return False, copy_error
    try:
        ok, _, error = _run_osascript(
            _process_script(app, 'keystroke "v" using {command down}', app_pid=app_pid),
            timeout_s=_operation_timeout(deadline, 30),
        )
        return ok, error or "paste completed"
    finally:
        # Restoring the user's clipboard is cleanup and must not be blocked by the
        # action budget that was consumed by the paste itself.
        _set_clipboard(previous)


def _type_text(
    app: str,
    element_id: str,
    text: str,
    clear: bool,
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    focused, focus_error = _focus_element(app, element_id, deadline, app_pid)
    if not focused:
        return False, focus_error or "Could not focus target element"
    if clear:
        ok, _, error = _run_osascript(
            _process_script(
                app,
                'keystroke "a" using {command down}\n        key code 51',
                app_pid=app_pid,
            ),
            timeout_s=_operation_timeout(deadline, 30),
        )
        if not ok:
            return False, error or "Could not clear the target text field"
    ok, error = _run_cliclick(
        [f"t:{text}"], timeout_s=_operation_timeout(deadline, 45)
    )
    if ok:
        return True, "text typed"
    pasted, paste_error = _paste_text(app, element_id, text, deadline, app_pid)
    return pasted, paste_error if not pasted else "text pasted as typing fallback"


def _key(
    app: str,
    key: Any,
    modifiers: Any,
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    if not isinstance(key, str) or not key.strip():
        return False, "key is required"
    key_value = key.strip()
    key_lower = key_value.lower()
    modifier_values = modifiers if isinstance(modifiers, list) else []
    modifier_parts = []
    for modifier in modifier_values:
        mapped = _MODIFIER_MAP.get(str(modifier).lower())
        if not mapped:
            return False, f"Unsupported modifier: {modifier}"
        modifier_parts.append(mapped)
    using_clause = f" using {{{', '.join(modifier_parts)}}}" if modifier_parts else ""
    if key_lower in _KEY_CODES:
        command = f"key code {_KEY_CODES[key_lower]}{using_clause}"
    elif len(key_value) == 1:
        command = f"keystroke {_apple_string(key_value)}{using_clause}"
    else:
        return False, "Unknown key names must be a single character or a supported key such as return, tab, escape, or page_down"
    ok, _, error = _run_osascript(
        _process_script(app, command, app_pid=app_pid), timeout_s=_operation_timeout(deadline, 30)
    )
    return ok, error or "key sent"


def _scroll(
    app: str,
    action: Dict[str, Any],
    node: Optional[Dict[str, Any]],
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    direction = str(action.get("direction", "down")).lower().replace("-", "_")
    action_name = {
        "up": "AXScrollUp",
        "down": "AXScrollDown",
        "left": "AXScrollLeft",
        "right": "AXScrollRight",
    }.get(direction)
    if not action_name:
        return False, "direction must be up, down, left, or right"
    try:
        pages = max(1, min(int(action.get("pages", 1)), 10))
    except (TypeError, ValueError):
        return False, "pages must be an integer between 1 and 10"

    element_id = action.get("element_id")
    if element_id is not None:
        try:
            element_id = _validate_element_id(element_id)
        except ValueError as exc:
            return False, str(exc)
        body = (
            f'repeat {pages} times\n'
            f'            try\n'
            f'                perform action "{action_name}" of targetElement\n'
            f'            on error\n'
            f'                key code {121 if direction == "down" else 116 if direction == "up" else 124 if direction == "right" else 123}\n'
            f'            end try\n'
            f'            delay 0.1\n'
            f'        end repeat'
        )
        ok, _, error = _run_osascript(
            _target_script(app, element_id, body, app_pid=app_pid),
            timeout_s=_operation_timeout(deadline, 30),
        )
        if ok:
            return True, "semantic scroll completed"
        if app_pid is not None:
            return False, error or "semantic scroll failed for stable native target; key fallback disabled"
        # If an app does not expose AXScroll actions, fall back to page keys.

    key_name = {
        "down": "pagedown",
        "up": "pageup",
        "left": "left",
        "right": "right",
    }[direction]
    for _ in range(pages):
        ok, message = _key(app, key_name, [], deadline, app_pid)
        if not ok:
            return False, message
        if deadline is not None and time.monotonic() >= deadline:
            return False, "macOS UI action time budget exceeded"
    return True, "key-based scroll completed"


def _accessibility_action(
    app: str,
    action: Dict[str, Any],
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    try:
        element_id = _validate_element_id(action.get("element_id"))
    except ValueError as exc:
        return False, str(exc)
    action_name = str(action.get("name", "")).strip()
    if not re.fullmatch(r"AX[A-Za-z0-9]+", action_name):
        return False, "name must be an Accessibility action such as AXPress or AXShowMenu"
    ok, _, error = _run_osascript(
        _target_script(app, element_id, f'perform action "{action_name}" of targetElement', app_pid=app_pid),
        timeout_s=_operation_timeout(deadline, 30),
    )
    return ok, error or f"{action_name} completed"


def _drag(action: Dict[str, Any], deadline: Optional[float] = None) -> Tuple[bool, str]:
    source = action.get("from")
    target = action.get("to")
    try:
        if isinstance(source, dict):
            from_x = _coordinate(source.get("x"), "from.x")
            from_y = _coordinate(source.get("y"), "from.y")
        else:
            from_x = _coordinate(action.get("from_x"), "from_x")
            from_y = _coordinate(action.get("from_y"), "from_y")
        if isinstance(target, dict):
            to_x = _coordinate(target.get("x"), "to.x")
            to_y = _coordinate(target.get("y"), "to.y")
        else:
            to_x = _coordinate(action.get("to_x"), "to_x")
            to_y = _coordinate(action.get("to_y"), "to_y")
        duration_ms = max(0, min(int(action.get("duration_ms", 150)), 5_000))
    except (TypeError, ValueError) as exc:
        return False, str(exc)

    commands = [f"dd:{from_x},{from_y}"]
    if duration_ms:
        commands.append(f"w:{duration_ms}")
    commands.extend([f"dm:{to_x},{to_y}", f"du:{to_x},{to_y}"])
    ok, error = _run_cliclick(
        commands, timeout_s=_operation_timeout(deadline, 45)
    )
    return ok, error or "drag completed"


def _is_risky_click(node: Optional[Dict[str, Any]]) -> bool:
    if not node:
        return False
    searchable = " ".join(
        str(node.get(field) or "")
        for field in ("title", "description", "value", "role", "subrole")
    ).lower()
    return any(word in searchable for word in _RISKY_WORDS)


def _perform_action(
    app: str,
    action: Dict[str, Any],
    node: Optional[Dict[str, Any]],
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
) -> Tuple[bool, str]:
    action_type = str(action.get("type", "")).strip().lower().replace("-", "_")
    if action_type in {"click", "double_click"}:
        return _click(app, action, node, deadline, app_pid)
    if action_type == "scroll":
        return _scroll(app, action, node, deadline, app_pid)
    if action_type in {"type", "type_text"}:
        element_id = _validate_element_id(action.get("element_id"))
        text = action.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text) > _MAX_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TEXT_CHARS} characters")
        return _type_text(app, element_id, text, bool(action.get("clear", True)), deadline, app_pid)
    if action_type == "paste":
        element_id = _validate_element_id(action.get("element_id"))
        text = action.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text) > _MAX_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TEXT_CHARS} characters")
        return _paste_text(app, element_id, text, deadline, app_pid)
    if action_type in {"key", "keyboard", "shortcut"}:
        return _key(app, action.get("key"), action.get("modifiers", []), deadline, app_pid)
    if action_type in {"action", "accessibility_action", "menu"}:
        return _accessibility_action(app, action, deadline, app_pid)
    if action_type == "drag":
        return _drag(action, deadline)
    raise ValueError(
        "Unsupported action type. Use click, double_click, scroll, type, paste, key, drag, or accessibility_action."
    )


def _element_window_index(element_id: Optional[str]) -> Optional[int]:
    if not element_id:
        return None
    match = re.match(r"^w([1-9][0-9]*)", str(element_id))
    return int(match.group(1)) if match else None


def _resolve_action_native_target(
    *,
    stored: Optional[Dict[str, Any]],
    requested_app: Optional[str],
    app_handle: Optional[str],
    window_handle: Optional[str],
    element_id: Optional[str],
    deadline: Optional[float],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    stored_app = str((stored or {}).get("active_app") or "") or None
    stored_app_handle = (stored or {}).get("app_handle")
    if app_handle and stored_app_handle and app_handle != stored_app_handle:
        return None, _native_target_error(
            "TARGET_HANDLE_MISMATCH", "app_handle does not match the application in observation_id."
        )

    original_window_index = _element_window_index(element_id)
    stored_window_index = int((stored or {}).get("window_index") or 0)
    stored_window_handles = (stored or {}).get("window_handles") or {}
    inferred_window_handle: Optional[str] = None
    if stored is not None:
        if stored_window_index > 0:
            inferred_window_handle = (stored or {}).get("selected_window_handle")
        elif original_window_index is not None:
            inferred_window_handle = stored_window_handles.get(original_window_index)

    if window_handle and inferred_window_handle and window_handle != inferred_window_handle:
        return None, _native_target_error(
            "TARGET_HANDLE_MISMATCH",
            "window_handle does not match the window that produced the requested element_id.",
            window_handle=window_handle,
            observed_window_handle=inferred_window_handle,
        )

    effective_window_handle = window_handle or inferred_window_handle
    effective_app_handle = app_handle or stored_app_handle

    if stored is not None and effective_window_handle is None:
        if stored_window_index > 0 or original_window_index is not None:
            return None, _native_target_error(
                "WINDOW_IDENTITY_UNAVAILABLE",
                "The observed window did not have a unique stable identity; observe again after making the target window distinguishable.",
            )
        return None, _native_target_error(
            "TARGET_WINDOW_REQUIRED",
            "This observation contains multiple windows; provide window_handle or use an element_id from a uniquely identified window.",
        )

    if effective_window_handle:
        target_app, target_pid, target_window_index, metadata, error = _resolve_registered_native_target(
            requested_app or stored_app,
            str(effective_app_handle) if effective_app_handle else None,
            str(effective_window_handle),
            deadline,
        )
        if error is not None:
            return None, error
        return {
            "app": target_app,
            "pid": target_pid,
            "window_index": target_window_index,
            "app_handle": (metadata or {}).get("app_handle") or effective_app_handle,
            "window_handle": effective_window_handle,
        }, None

    if effective_app_handle:
        target_app, target_pid, _, metadata, error = _resolve_registered_native_target(
            requested_app or stored_app, str(effective_app_handle), None, deadline
        )
        if error is not None:
            return None, error
        return {
            "app": target_app,
            "pid": target_pid,
            "window_index": original_window_index,
            "app_handle": (metadata or {}).get("app_handle") or effective_app_handle,
            "window_handle": None,
        }, None

    target_app, resolve_error = _resolve_app(requested_app or stored_app, deadline)
    if not target_app:
        return None, {"ok": False, "error": resolve_error or "Could not resolve target application"}
    return {
        "app": target_app,
        "pid": None,
        "window_index": original_window_index,
        "app_handle": None,
        "window_handle": None,
    }, None


def act_ui(
    settings: Settings,
    actions: List[Dict[str, Any]],
    observation_id: Optional[str] = None,
    app: Optional[str] = None,
    return_state: bool = True,
    allow_risky: bool = False,
    app_handle: Optional[str] = None,
    window_handle: Optional[str] = None,
) -> Any:
    """Perform bounded macOS UI actions against re-resolved native app/window handles."""
    deadline = time.monotonic() + _ACTION_BUDGET_S
    try:
        if not isinstance(actions, list) or not actions:
            return {"ok": False, "error": "actions must be a non-empty list"}
        if len(actions) > _MAX_ACTIONS:
            return {"ok": False, "error": f"actions may contain at most {_MAX_ACTIONS} items"}

        stored = _get_observation(observation_id) if observation_id else None
        if observation_id and stored is None:
            return {
                "ok": False,
                "error": "observation_id is missing or expired; call mac_observe again before acting.",
            }

        requested_app = _normalize_app(app)
        stored_app = str(stored.get("active_app")) if stored else None
        if requested_app and stored_app and requested_app.lower() != stored_app.lower():
            return {"ok": False, "error": "app does not match the application in observation_id"}

        stored_nodes = (stored or {}).get("nodes", {})
        results: List[Dict[str, Any]] = []
        last_target: Optional[Dict[str, Any]] = None

        for index, action in enumerate(actions):
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "timed_out": True,
                    "active_app": (last_target or {}).get("app") or stored_app or requested_app,
                    "actions": results,
                    "error": f"mac_act exceeded its {_ACTION_BUDGET_S}s total time budget",
                }
            if not isinstance(action, dict):
                return {"ok": False, "error": f"actions[{index}] must be an object"}

            action_type = str(action.get("type", "")).strip().lower().replace("-", "_")
            original_element_id = action.get("element_id")
            node = None
            if original_element_id is not None:
                original_element_id = _validate_element_id(original_element_id)
                node = stored_nodes.get(original_element_id)
                if observation_id and node is None and action_type not in {"drag"}:
                    return {
                        "ok": False,
                        "error": f"actions[{index}].element_id was not present in observation_id; observe again.",
                    }
            if action_type in {"click", "double_click"} and not allow_risky and _is_risky_click(node):
                return {
                    "ok": False,
                    "blocked": True,
                    "error": "This element looks like a potentially consequential control. Set allow_risky=true only when the action is intentional.",
                    "element_id": original_element_id,
                }

            target, target_error = _resolve_action_native_target(
                stored=stored,
                requested_app=requested_app,
                app_handle=app_handle,
                window_handle=window_handle,
                element_id=original_element_id,
                deadline=deadline,
            )
            if target_error is not None:
                target_error["actions"] = results
                target_error["failed_action_index"] = index
                return target_error
            assert target is not None
            last_target = target

            if target.get("window_handle") and (
                action_type in {"key", "keyboard", "shortcut", "drag"}
                or (action_type in {"click", "double_click", "scroll"} and original_element_id is None)
            ):
                return _native_target_error(
                    "WINDOW_BOUND_ACTION_REQUIRES_ELEMENT",
                    "This action cannot yet be bound safely to a specific native window without an element_id. "
                    "Use an element-targeted action; focus-safe window-level control is handled separately.",
                    actions=results,
                    failed_action_index=index,
                    window_handle=target.get("window_handle"),
                )

            resolved_action = dict(action)
            resolved_element_id = original_element_id
            if original_element_id and target.get("window_index"):
                resolved_element_id = _rebase_element_id(
                    original_element_id, int(target["window_index"])
                )
                resolved_action["element_id"] = resolved_element_id

            started = time.perf_counter()
            timed_out = False
            try:
                ok, message = _perform_action(
                    str(target.get("app") or ""),
                    resolved_action,
                    node,
                    deadline,
                    int(target["pid"]) if target.get("pid") else None,
                )
            except TimeoutError as exc:
                ok, message = False, str(exc)
                timed_out = True
            except ValueError as exc:
                ok, message = False, str(exc)

            result: Dict[str, Any] = {
                "index": index,
                "type": action_type,
                "element_id": original_element_id,
                "ok": ok,
                "message": message,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "app_handle": target.get("app_handle"),
                "window_handle": target.get("window_handle"),
                "resolved_window_index": target.get("window_index"),
            }
            if resolved_element_id != original_element_id:
                result["resolved_element_id"] = resolved_element_id
            if timed_out:
                result["timed_out"] = True
            results.append(result)
            if not ok:
                response = {
                    "ok": False,
                    "active_app": target.get("app"),
                    "app_handle": target.get("app_handle"),
                    "window_handle": target.get("window_handle"),
                    "actions": results,
                }
                if timed_out:
                    response["timed_out"] = True
                return response
            if deadline - time.monotonic() <= 0:
                return {
                    "ok": False,
                    "timed_out": True,
                    "active_app": target.get("app"),
                    "actions": results,
                    "error": f"mac_act exceeded its {_ACTION_BUDGET_S}s total time budget",
                }
            time.sleep(min(0.08, max(0.0, deadline - time.monotonic())))

        if not return_state:
            return {
                "ok": True,
                "active_app": (last_target or {}).get("app"),
                "app_handle": (last_target or {}).get("app_handle"),
                "window_handle": (last_target or {}).get("window_handle"),
                "actions": results,
            }

        stored_window_index = int((stored or {}).get("window_index") or 1)
        post_window_handle = window_handle
        if post_window_handle is None and stored is not None and stored_window_index > 0:
            post_window_handle = stored.get("selected_window_handle")
        post_app_handle = app_handle or (stored or {}).get("app_handle")

        post_app = (last_target or {}).get("app") or requested_app or stored_app
        post_pid = (last_target or {}).get("pid")
        post_window_index = stored_window_index
        if post_window_handle:
            resolved_app, resolved_pid, resolved_index, _, target_error = _resolve_registered_native_target(
                post_app,
                str(post_app_handle) if post_app_handle else None,
                str(post_window_handle),
                deadline,
            )
            if target_error is not None:
                return {
                    "ok": True,
                    "active_app": post_app,
                    "actions": results,
                    "post_state_ok": False,
                    "post_state_error": target_error,
                    "previous_observation_id": observation_id,
                }
            post_app, post_pid, post_window_index = resolved_app, resolved_pid, int(resolved_index or 1)
        elif post_app_handle:
            resolved_app, resolved_pid, _, _, target_error = _resolve_registered_native_target(
                post_app, str(post_app_handle), None, deadline
            )
            if target_error is None:
                post_app, post_pid = resolved_app, resolved_pid

        try:
            post_payload, image_data = _collect_observation(
                settings,
                post_app,
                post_window_index,
                max_depth=5,
                max_children=30,
                include_screenshot=True,
                ocr=False,
                deadline=deadline,
                app_pid=int(post_pid) if post_pid else None,
            )
        except TimeoutError as exc:
            return {
                "ok": True,
                "active_app": post_app,
                "actions": results,
                "post_state_ok": False,
                "post_state_error": str(exc),
                "previous_observation_id": observation_id,
            }
        post_payload["actions"] = results
        post_payload["previous_observation_id"] = observation_id
        return _format_result(post_payload, image_data)
    except TimeoutError as exc:
        return {"ok": False, "timed_out": True, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Could not act on macOS UI: {exc}"}
