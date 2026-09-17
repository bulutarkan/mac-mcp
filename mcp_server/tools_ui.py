from __future__ import annotations

import copy
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

from .native_action_verification import (
    ACTION_VERIFY_POLL_S as _NATIVE_ACTION_VERIFY_POLL_S,
    ACTION_VERIFY_TIMEOUT_S as _NATIVE_ACTION_VERIFY_TIMEOUT_S,
    READINESS_POLL_S as _NATIVE_READINESS_POLL_S,
    READINESS_STABLE_MS as _NATIVE_READINESS_STABLE_MS,
    READINESS_TIMEOUT_S as _NATIVE_READINESS_TIMEOUT_S,
    effect_changed as _native_effect_changed,
    geometry_signature as _native_geometry_signature,
    observed_geometry_matches as _native_observed_geometry_matches,
    readiness_reason as _native_readiness_reason,
    verification_required as _native_verification_required,
)
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



def _normalize_action_state_mode(
    state_mode: Optional[str], return_state: Optional[bool],
) -> Tuple[str, bool]:
    """Resolve the new state_mode contract while keeping the legacy boolean alias."""
    if state_mode is None:
        if return_state is None:
            return "delta", False
        return ("full" if bool(return_state) else "none"), bool(return_state)
    mode = str(state_mode).strip().lower()
    if mode not in {"none", "delta", "full"}:
        raise ValueError("state_mode must be one of: none, delta, full")
    if return_state is not None:
        legacy_mode = "full" if bool(return_state) else "none"
        if legacy_mode != mode:
            raise ValueError("state_mode conflicts with legacy return_state; use state_mode only")
    return mode, False


def _merge_effect_state_into_node(
    node: Optional[Dict[str, Any]], state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not node:
        return None
    if state.get("connected") is False:
        return None
    merged = copy.deepcopy(node)
    for key in (
        "role", "subrole", "title", "description", "value", "enabled",
        "focused", "child_count", "actions",
    ):
        if key in state and state.get(key) is not None:
            merged[key] = copy.deepcopy(state.get(key))
    position = state.get("position")
    if isinstance(position, dict) and all(position.get(k) is not None for k in ("x", "y", "width", "height")):
        merged["position"] = copy.deepcopy(position)
    return merged


def _effect_state_requires_structural_refresh(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
) -> bool:
    if not before or not after:
        return True
    if before.get("connected", True) != after.get("connected", True):
        return True
    for key in ("role", "subrole"):
        if before.get(key) and after.get(key) and before.get(key) != after.get(key):
            return True
    for key in (
        "child_count", "window_title", "window_count", "window_child_count",
        "sheet_count", "popover_count", "menu_count",
    ):
        if key in before and key in after and before.get(key) != after.get(key):
            return True
    return False


def _store_derived_observation(
    base_observation_id: str,
    updates: Dict[str, Optional[Dict[str, Any]]],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Clone a cached observation with target-only updates, without a full AX traversal."""
    now = time.time()
    with _OBSERVATIONS_LOCK:
        base = _OBSERVATIONS.get(base_observation_id)
        if base is None or now - float(base.get("created_at", 0)) > _OBSERVATION_TTL_S:
            return None, None
        derived = copy.deepcopy(base)
        nodes = dict(derived.get("nodes") or {})
        for element_id, node in updates.items():
            if node is None:
                nodes.pop(element_id, None)
            else:
                nodes[element_id] = copy.deepcopy(node)
        observation_id = f"obs_{uuid.uuid4().hex}"
        derived["created_at"] = now
        derived["nodes"] = nodes
        _OBSERVATIONS[observation_id] = derived
        expired = [
            key for key, value in _OBSERVATIONS.items()
            if now - float(value.get("created_at", now)) > _OBSERVATION_TTL_S
        ]
        for key in expired:
            _OBSERVATIONS.pop(key, None)
        while len(_OBSERVATIONS) > _MAX_OBSERVATIONS:
            oldest = min(_OBSERVATIONS, key=lambda key: _OBSERVATIONS[key].get("created_at", now))
            _OBSERVATIONS.pop(oldest, None)
        return observation_id, copy.deepcopy(derived)


def _diff_observation_nodes(
    before_nodes: Dict[str, Dict[str, Any]],
    after_nodes: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    changed = [
        copy.deepcopy(after_nodes[element_id])
        for element_id in sorted(before_nodes.keys() & after_nodes.keys())
        if before_nodes[element_id] != after_nodes[element_id]
    ]
    added = [
        copy.deepcopy(after_nodes[element_id])
        for element_id in sorted(after_nodes.keys() - before_nodes.keys())
    ]
    removed = sorted(before_nodes.keys() - after_nodes.keys())
    return {
        "changed_nodes": changed,
        "added_nodes": added,
        "removed_element_ids": removed,
        "changed_count": len(changed),
        "added_count": len(added),
        "removed_count": len(removed),
    }


def _delta_base_payload(
    *,
    observation_id: Optional[str],
    previous_observation_id: Optional[str],
    active_app: Optional[str],
    app_handle: Optional[str],
    window_handle: Optional[str],
    node_count: int,
    delta: Dict[str, Any],
    actions: List[Dict[str, Any]],
    screenshot_requested: bool = False,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "state_mode": "delta",
        "observation_id": observation_id,
        "previous_observation_id": previous_observation_id,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "active_app": active_app,
        "app_handle": app_handle,
        "window_handle": window_handle,
        "node_count": int(node_count),
        "delta": delta,
        "actions": actions,
        "screenshot": {
            "requested": bool(screenshot_requested),
            "included_as_image_content": False,
            "mime_type": None,
        },
    }


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



def _capture_focus_context(deadline: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    metadata, error = _scan_native_windows(None, None, deadline)
    if metadata is None:
        return None, error or "Could not capture the current frontmost app/window."
    windows = [row for row in (metadata.get("windows") or []) if isinstance(row, dict)]
    focused = next((row for row in windows if row.get("focused") is True), None)
    if focused is None:
        focused = next((row for row in windows if row.get("main") is True), None)
    if focused is None and windows:
        focused = windows[0]
    return {
        "app": str(metadata.get("active_app") or ""),
        "pid": int(metadata.get("pid") or 0),
        "app_handle": metadata.get("app_handle"),
        "window_count": int(metadata.get("window_count") or len(windows)),
        "window_index": int((focused or {}).get("index") or 0),
        "window_handle": (focused or {}).get("window_handle"),
        "window_identity_status": (focused or {}).get("identity_status"),
    }, None


def _current_focus_key(deadline: Optional[float] = None) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    ok, stdout, stderr = _run_osascript(
        '''tell application "System Events"
    set p to first application process whose frontmost is true
    set pidText to unix id of p as text
    set focusedIndex to 0
    set windowCount to count of windows of p
    repeat with wi from 1 to windowCount
        try
            set w to window wi of p
            set isFocused to false
            set isMain to false
            try
                set isFocused to value of attribute "AXFocused" of w
            end try
            try
                set isMain to value of attribute "AXMain" of w
            end try
            if isFocused or isMain then
                set focusedIndex to wi
                exit repeat
            end if
        end try
    end repeat
    return pidText & tab & (focusedIndex as text)
end tell''',
        timeout_s=_operation_timeout(deadline, 5),
    )
    if not ok:
        return None, stderr or "Could not read the current frontmost focus."
    parts = stdout.strip().split("\t")
    if len(parts) != 2:
        return None, "Invalid frontmost focus response."
    try:
        return (int(parts[0]), int(parts[1])), None
    except ValueError:
        return None, "Invalid frontmost focus identifiers."


def _focus_context_matches_current(
    context: Dict[str, Any], deadline: Optional[float] = None,
) -> Tuple[Optional[bool], Optional[str]]:
    current, error = _current_focus_key(deadline)
    if current is None:
        return None, error
    expected = (int(context.get("pid") or 0), int(context.get("window_index") or 0))
    return current == expected, None


def _post_action_focus_decision(
    context: Dict[str, Any], target: Dict[str, Any], deadline: Optional[float] = None,
) -> Tuple[str, Optional[str]]:
    """Decide whether to restore, preserving a user's concurrent focus change.

    Returns preserved, restore, user_changed, or unknown. The decision is read-only.
    """
    current, error = _current_focus_key(deadline)
    if current is None:
        return "unknown", error

    previous_pid = int(context.get("pid") or 0)
    previous_window = int(context.get("window_index") or 0)
    target_pid = int(target.get("pid") or 0)
    target_window = int(target.get("window_index") or 0)
    current_pid, current_window = current

    if current_pid == previous_pid and (previous_window <= 0 or current_window == previous_window):
        return "preserved", None
    if current_pid == target_pid and previous_pid != target_pid:
        if target_window <= 0 or current_window == target_window:
            return "restore", None
        return "user_changed", None
    if current_pid not in {previous_pid, target_pid}:
        return "user_changed", None

    # Same-app window transitions need stable handles because AX window indices can move.
    if previous_pid == target_pid == current_pid:
        metadata, scan_error = _scan_native_windows(None, None, deadline)
        if metadata is None:
            return "unknown", scan_error or error
        focused = next(
            (row for row in (metadata.get("windows") or [])
             if isinstance(row, dict) and (row.get("focused") is True or row.get("main") is True)),
            None,
        )
        current_handle = (focused or {}).get("window_handle")
        previous_handle = context.get("window_handle")
        target_handle = target.get("window_handle")
        if previous_handle and current_handle == previous_handle:
            return "preserved", None
        if target_handle and current_handle == target_handle:
            return "restore", None
        return "user_changed", None

    # The user returned to the previous app but selected a different window while the
    # action was running. Do not overwrite that explicit focus choice.
    if current_pid == previous_pid:
        return "user_changed", None
    return "restore", None


def _restore_focus_context(
    context: Dict[str, Any], deadline: Optional[float] = None,
) -> Tuple[bool, str, bool]:
    pid = int(context.get("pid") or 0)
    if pid <= 0:
        return False, "Previous frontmost process identity is unavailable.", False

    resolved_index = int(context.get("window_index") or 0)
    exact_window = False
    window_handle = context.get("window_handle")
    if window_handle:
        app_name, resolved_pid, window_index, _, target_error = _resolve_registered_native_target(
            str(context.get("app") or ""),
            str(context.get("app_handle")) if context.get("app_handle") else None,
            str(window_handle),
            deadline,
        )
        if target_error is not None or not resolved_pid or not window_index:
            return False, "Previous frontmost window no longer has a valid stable identity.", False
        if int(resolved_pid) != pid:
            return False, "Previous frontmost process changed before focus restoration.", False
        resolved_index = int(window_index)
        exact_window = True

    window_body = ""
    if resolved_index > 0:
        window_body = f'''\n        try\n            perform action "AXRaise" of window {resolved_index}\n        end try\n        try\n            set value of attribute "AXMain" of window {resolved_index} to true\n        end try\n        try\n            set value of attribute "AXFocused" of window {resolved_index} to true\n        end try'''
    ok, _, error = _run_osascript(
        f'''tell application "System Events"
    set p to first application process whose unix id is {pid}
    tell p
        set frontmost to true{window_body}
    end tell
end tell''',
        timeout_s=_operation_timeout(deadline, 8),
    )
    if not ok:
        return False, error or "Could not restore the previous frontmost app/window.", exact_window

    matches, verify_error = _focus_context_matches_current(context, deadline)
    if matches is True:
        return True, "previous focus restored", exact_window
    if exact_window:
        # Window indices can move after the target app changes its window ordering. Re-scan
        # the now-frontmost app and verify against the stable native window handle.
        metadata, scan_error = _scan_native_windows(None, None, deadline)
        if metadata is not None:
            current = next(
                (row for row in (metadata.get("windows") or [])
                 if isinstance(row, dict) and (row.get("focused") is True or row.get("main") is True)),
                None,
            )
            if metadata.get("app_handle") == context.get("app_handle") and current and current.get("window_handle") == window_handle:
                return True, "previous focus restored", True
        verify_error = verify_error or scan_error
    return False, verify_error or "Previous focus could not be verified after restoration.", exact_window


def _action_requires_foreground(action: Dict[str, Any]) -> bool:
    action_type = str(action.get("type") or "").strip().lower().replace("-", "_")
    element_id = action.get("element_id")
    if action_type in {"click", "double_click"}:
        try:
            click_count = int(action.get("click_count", 2 if action_type == "double_click" else 1))
        except (TypeError, ValueError):
            return True
        button = str(action.get("button", "left")).lower()
        return not (element_id is not None and action_type == "click" and click_count == 1 and button == "left")
    if action_type == "scroll":
        return element_id is None
    if action_type in {"action", "accessibility_action", "menu"}:
        return False
    if action_type in {"type", "type_text", "paste", "key", "keyboard", "shortcut"}:
        return True
    if action_type == "drag":
        return True
    return True


def _focus_transition_needed(context: Dict[str, Any], target: Dict[str, Any]) -> bool:
    if int(context.get("pid") or 0) != int(target.get("pid") or 0):
        return True
    target_window = int(target.get("window_index") or 0)
    current_window = int(context.get("window_index") or 0)
    return bool(target_window and target_window != current_window)

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



def _parse_optional_bool(value: str) -> Optional[bool]:
    raw = str(value or "").strip().lower()
    if raw in {"true", "yes", "1"}:
        return True
    if raw in {"false", "no", "0"}:
        return False
    return None


def _parse_native_action_state(raw: str) -> Dict[str, Any]:
    fields = str(raw or "").split(_FIELD_SEPARATOR)
    if not fields or fields[0] != "__STATE__" or len(fields) < 36:
        return {"connected": False, "probe_error": "invalid_native_state_payload"}
    state: Dict[str, Any] = {
        "connected": _parse_bool(fields[1]),
        "role": fields[2],
        "subrole": fields[3],
        "title": fields[4],
        "description": fields[5],
        "value": fields[6],
        "character_count": _parse_number(fields[7]),
        "selected": _parse_optional_bool(fields[8]),
        "focused": _parse_optional_bool(fields[9]),
        "enabled": _parse_optional_bool(fields[10]),
        "hidden": _parse_optional_bool(fields[11]),
        "visible": _parse_optional_bool(fields[12]),
        "offscreen": _parse_optional_bool(fields[13]),
        "busy": _parse_optional_bool(fields[14]),
        "position": {
            "x": _parse_number(fields[15]), "y": _parse_number(fields[16]),
            "width": _parse_number(fields[17]), "height": _parse_number(fields[18]),
        },
        "child_count": _parse_number(fields[19]),
        "actions": [item.strip() for item in fields[20].split(",") if item.strip()],
        "window_title": fields[21],
        "window_count": _parse_number(fields[22]),
        "window_position": {
            "x": _parse_number(fields[23]), "y": _parse_number(fields[24]),
            "width": _parse_number(fields[25]), "height": _parse_number(fields[26]),
        },
        "window_minimized": _parse_optional_bool(fields[27]),
        "window_child_count": _parse_number(fields[28]),
        "sheet_count": _parse_number(fields[29]),
        "popover_count": _parse_number(fields[30]),
        "menu_count": _parse_number(fields[31]),
        "in_sheet": _parse_optional_bool(fields[32]),
        "in_popover": _parse_optional_bool(fields[33]),
        "popover_covers_target": _parse_optional_bool(fields[34]),
        "process_visible": _parse_optional_bool(fields[35]),
    }
    state["modal_sheet_blocks_target"] = bool(
        (state.get("sheet_count") or 0) > 0 and state.get("in_sheet") is not True
    )
    return state


def _native_action_state_script(
    app: str,
    element_id: str,
    *,
    app_pid: Optional[int] = None,
) -> str:
    expression = _element_expression(_validate_element_id(element_id))
    window_index = _element_window_index(element_id) or 1
    selection = (
        f"set p to first application process whose unix id is {int(app_pid)}"
        if app_pid is not None
        else f"set p to first application process whose name is {_apple_string(app)}"
    )
    return f'''use scripting additions

on cleanStateText(v, fs)
    try
        set t to v as text
    on error
        set t to ""
    end try
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {{return, linefeed, tab, fs}}
    set parts to every text item of t
    set AppleScript's text item delimiters to " "
    set t to parts as text
    set AppleScript's text item delimiters to oldDelims
    if (length of t) > 4000 then set t to text 1 thru 4000 of t
    return t
end cleanStateText

set fs to character id 31
set connectedText to "false"
set roleText to ""
set subroleText to ""
set titleText to ""
set descriptionText to ""
set valueText to ""
set characterCountText to ""
set selectedText to ""
set focusedText to ""
set enabledText to ""
set hiddenText to ""
set visibleText to ""
set offscreenText to ""
set busyText to ""
set xText to ""
set yText to ""
set widthText to ""
set heightText to ""
set childCountText to ""
set actionText to ""
set windowTitleText to ""
set windowCountText to ""
set windowXText to ""
set windowYText to ""
set windowWidthText to ""
set windowHeightText to ""
set windowMinimizedText to ""
set windowChildCountText to ""
set sheetCountText to ""
set popoverCountText to ""
set menuCountText to ""
set inSheetText to "false"
set inPopoverText to "false"
set popoverCoversText to "false"
set processVisibleText to ""

tell application "System Events"
    {selection}
    try
        set processVisibleText to visible of p as text
    end try
    try
        set windowCountText to count of windows of p as text
    end try
    try
        set menuCountText to count of menus of p as text
    end try
    try
        set w to window {window_index} of p
        try
            set windowTitleText to title of w as text
        end try
        try
            set wp to position of w
            set windowXText to item 1 of wp as text
            set windowYText to item 2 of wp as text
        end try
        try
            set ws to size of w
            set windowWidthText to item 1 of ws as text
            set windowHeightText to item 2 of ws as text
        end try
        try
            set windowMinimizedText to value of attribute "AXMinimized" of w as text
        end try
        try
            set windowChildCountText to count of UI elements of w as text
        end try
        try
            set sheetCountText to count of sheets of w as text
        end try
        try
            set popoverCountText to count of pop overs of w as text
        end try

        try
            tell p
                set targetElement to {expression}
            end tell
            set connectedText to "true"
            try
                set roleText to role of targetElement as text
            end try
            try
                set subroleText to subrole of targetElement as text
            end try
            try
                set titleText to title of targetElement as text
            end try
            try
                set descriptionText to description of targetElement as text
            end try
            set isSecure to (roleText contains "SecureText" or subroleText contains "Secure")
            if isSecure then
                set valueText to "[redacted]"
            else
                try
                    set valueText to value of targetElement as text
                end try
            end if
            try
                set characterCountText to value of attribute "AXNumberOfCharacters" of targetElement as text
            end try
            try
                set selectedText to selected of targetElement as text
            end try
            try
                set focusedText to focused of targetElement as text
            end try
            try
                set enabledText to enabled of targetElement as text
            end try
            try
                set hiddenText to value of attribute "AXHidden" of targetElement as text
            end try
            try
                set visibleText to value of attribute "AXVisible" of targetElement as text
            end try
            try
                set offscreenText to value of attribute "AXOffScreen" of targetElement as text
            end try
            try
                set busyText to value of attribute "AXElementBusy" of targetElement as text
            end try
            try
                set tp to position of targetElement
                set xText to item 1 of tp as text
                set yText to item 2 of tp as text
            end try
            try
                set ts to size of targetElement
                set widthText to item 1 of ts as text
                set heightText to item 2 of ts as text
            end try
            try
                set childCountText to count of UI elements of targetElement as text
            end try
            try
                set actionText to name of actions of targetElement as text
            end try

            set hasOverlay to false
            try
                if (sheetCountText as integer) > 0 then set hasOverlay to true
            end try
            try
                if (popoverCountText as integer) > 0 then set hasOverlay to true
            end try
            if hasOverlay then
                set ancestorRef to targetElement
                repeat 16 times
                    try
                        set ancestorRef to value of attribute "AXParent" of ancestorRef
                        set ancestorRole to ""
                        try
                            set ancestorRole to role of ancestorRef as text
                        end try
                        if ancestorRole is "AXSheet" then set inSheetText to "true"
                        if ancestorRole is "AXPopover" then set inPopoverText to "true"
                    on error
                        exit repeat
                    end try
                end repeat
            end if

            try
                if (popoverCountText as integer) > 0 and xText is not "" and yText is not "" and widthText is not "" and heightText is not "" then
                    set targetCenterX to (xText as number) + ((widthText as number) / 2)
                    set targetCenterY to (yText as number) + ((heightText as number) / 2)
                    repeat with popItem in pop overs of w
                        try
                            set popRef to contents of popItem
                            set pp to position of popRef
                            set ps to size of popRef
                            set px to item 1 of pp as number
                            set py to item 2 of pp as number
                            set pw to item 1 of ps as number
                            set ph to item 2 of ps as number
                            if targetCenterX is greater than or equal to px and targetCenterX is less than or equal to (px + pw) and targetCenterY is greater than or equal to py and targetCenterY is less than or equal to (py + ph) then
                                if inPopoverText is not "true" then set popoverCoversText to "true"
                            end if
                        end try
                    end repeat
                end if
            end try
        end try
    end try
end tell

return "__STATE__" & fs & connectedText & fs & ¬
    my cleanStateText(roleText, fs) & fs & my cleanStateText(subroleText, fs) & fs & ¬
    my cleanStateText(titleText, fs) & fs & my cleanStateText(descriptionText, fs) & fs & ¬
    my cleanStateText(valueText, fs) & fs & my cleanStateText(characterCountText, fs) & fs & ¬
    my cleanStateText(selectedText, fs) & fs & my cleanStateText(focusedText, fs) & fs & ¬
    my cleanStateText(enabledText, fs) & fs & my cleanStateText(hiddenText, fs) & fs & ¬
    my cleanStateText(visibleText, fs) & fs & my cleanStateText(offscreenText, fs) & fs & ¬
    my cleanStateText(busyText, fs) & fs & my cleanStateText(xText, fs) & fs & ¬
    my cleanStateText(yText, fs) & fs & my cleanStateText(widthText, fs) & fs & ¬
    my cleanStateText(heightText, fs) & fs & my cleanStateText(childCountText, fs) & fs & ¬
    my cleanStateText(actionText, fs) & fs & my cleanStateText(windowTitleText, fs) & fs & ¬
    my cleanStateText(windowCountText, fs) & fs & my cleanStateText(windowXText, fs) & fs & ¬
    my cleanStateText(windowYText, fs) & fs & my cleanStateText(windowWidthText, fs) & fs & ¬
    my cleanStateText(windowHeightText, fs) & fs & my cleanStateText(windowMinimizedText, fs) & fs & ¬
    my cleanStateText(windowChildCountText, fs) & fs & my cleanStateText(sheetCountText, fs) & fs & ¬
    my cleanStateText(popoverCountText, fs) & fs & my cleanStateText(menuCountText, fs) & fs & ¬
    my cleanStateText(inSheetText, fs) & fs & my cleanStateText(inPopoverText, fs) & fs & ¬
    my cleanStateText(popoverCoversText, fs) & fs & my cleanStateText(processVisibleText, fs)
'''


def _probe_native_action_state(
    app: str,
    element_id: str,
    *,
    app_pid: Optional[int] = None,
    deadline: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ok, stdout, stderr = _run_osascript(
        _native_action_state_script(app, element_id, app_pid=app_pid),
        timeout_s=_operation_timeout(deadline, 8),
    )
    if not ok:
        return None, stderr or "native Accessibility readiness probe failed"
    state = _parse_native_action_state(stdout)
    if state.get("probe_error"):
        return None, str(state["probe_error"])
    return state, None


def _compact_readiness_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: state.get(key)
        for key in (
            "connected", "role", "subrole", "enabled", "hidden", "visible", "offscreen",
            "busy", "position", "window_position", "window_minimized", "sheet_count",
            "popover_count", "in_sheet", "in_popover", "popover_covers_target",
        )
        if state.get(key) is not None
    }


def _wait_for_native_readiness(
    app: str,
    action: Dict[str, Any],
    observed_node: Optional[Dict[str, Any]],
    *,
    app_pid: Optional[int] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    element_id = _validate_element_id(action.get("element_id"))
    timeout_s = max(0.1, min(float(action.get("readiness_timeout_s", _NATIVE_READINESS_TIMEOUT_S)), 2.5))
    stable_ms = max(0, min(int(action.get("readiness_stable_ms", _NATIVE_READINESS_STABLE_MS)), 1500))
    poll_s = max(0.03, min(float(action.get("readiness_poll_ms", _NATIVE_READINESS_POLL_S * 1000)) / 1000.0, 0.25))
    started = time.perf_counter()
    local_deadline = started + timeout_s
    if deadline is not None:
        local_deadline = min(local_deadline, deadline)
    attempts = 0
    last_state: Dict[str, Any] = {}
    last_reason: Optional[str] = None
    last_signature: Optional[tuple[Any, ...]] = None
    stable_since: Optional[float] = None

    while True:
        state, probe_error = _probe_native_action_state(
            app, element_id, app_pid=app_pid, deadline=local_deadline,
        )
        attempts += 1
        now = time.perf_counter()
        if probe_error is not None or state is None:
            return {
                "ready": False,
                "reason_code": "READINESS_PROBE_FAILED",
                "error": probe_error or "native readiness probe failed",
                "attempts": attempts,
                "duration_ms": int((now - started) * 1000),
                "retryable": False,
            }

        last_state = state
        last_reason = _native_readiness_reason(state, observed_node=observed_node)
        if last_reason in {"STALE_ELEMENT_PATH", "ELEMENT_DETACHED"}:
            return {
                "ready": False,
                "reason_code": last_reason,
                "attempts": attempts,
                "duration_ms": int((now - started) * 1000),
                "retryable": False,
                "state": _compact_readiness_state(state),
            }

        if last_reason is None:
            if _native_observed_geometry_matches(state, observed_node):
                return {
                    "ready": True,
                    "reason_code": None,
                    "attempts": attempts,
                    "duration_ms": int((now - started) * 1000),
                    "stable_for_ms": stable_ms,
                    "settled_by": "observation_geometry_match",
                    "state": state,
                    "occlusion_check": "modal_overlay_geometry",
                    "hit_test": "unavailable_side_effect_free",
                }
            signature = _native_geometry_signature(state)
            if signature != last_signature:
                last_signature = signature
                stable_since = now
            stable_for_ms = int((now - (stable_since or now)) * 1000)
            if stable_for_ms >= stable_ms:
                return {
                    "ready": True,
                    "reason_code": None,
                    "attempts": attempts,
                    "duration_ms": int((now - started) * 1000),
                    "stable_for_ms": stable_for_ms,
                    "state": state,
                    "occlusion_check": "modal_overlay_geometry",
                    "hit_test": "unavailable_side_effect_free",
                }
            last_reason = "ELEMENT_UNSTABLE"
        else:
            stable_since = None
            last_signature = None

        if now >= local_deadline:
            return {
                "ready": False,
                "timed_out": True,
                "reason_code": last_reason or "ELEMENT_NOT_READY",
                "attempts": attempts,
                "duration_ms": int((now - started) * 1000),
                "retryable": True,
                "state": _compact_readiness_state(last_state),
            }
        time.sleep(min(poll_s, max(0.0, local_deadline - now)))



def _parse_native_effect_state(raw: str) -> Dict[str, Any]:
    fields = str(raw or "").split(_FIELD_SEPARATOR)
    if not fields or fields[0] != "__EFFECT__" or len(fields) < 13:
        return {"connected": False, "probe_error": "invalid_native_effect_payload"}
    state: Dict[str, Any] = {
        "connected": _parse_bool(fields[1]),
        "value": fields[2],
        "character_count": _parse_number(fields[3]),
        "selected": _parse_optional_bool(fields[4]),
        "enabled": _parse_optional_bool(fields[5]),
        "title": fields[6],
        "child_count": _parse_number(fields[7]),
        "window_title": fields[8],
        "window_count": _parse_number(fields[9]),
        "window_child_count": _parse_number(fields[10]),
        "sheet_count": _parse_number(fields[11]),
        "popover_count": _parse_number(fields[12]),
    }
    if len(fields) >= 22:
        state.update({
            "role": fields[13],
            "subrole": fields[14],
            "description": fields[15],
            "focused": _parse_optional_bool(fields[16]),
            "position": {
                "x": _parse_number(fields[17]), "y": _parse_number(fields[18]),
                "width": _parse_number(fields[19]), "height": _parse_number(fields[20]),
            },
            "actions": [item.strip() for item in fields[21].split(",") if item.strip()],
        })
    return state


def _native_effect_state_script(
    app: str,
    element_id: str,
    *,
    app_pid: Optional[int] = None,
) -> str:
    expression = _element_expression(_validate_element_id(element_id))
    window_index = _element_window_index(element_id) or 1
    selection = (
        f"set p to first application process whose unix id is {int(app_pid)}"
        if app_pid is not None
        else f"set p to first application process whose name is {_apple_string(app)}"
    )
    return f'''use scripting additions

on cleanEffectText(v, fs)
    try
        set t to v as text
    on error
        set t to ""
    end try
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {{return, linefeed, tab, fs}}
    set parts to every text item of t
    set AppleScript's text item delimiters to " "
    set t to parts as text
    set AppleScript's text item delimiters to oldDelims
    if (length of t) > 4000 then set t to text 1 thru 4000 of t
    return t
end cleanEffectText

set fs to character id 31
set connectedText to "false"
set valueText to ""
set characterCountText to ""
set selectedText to ""
set enabledText to ""
set titleText to ""
set childCountText to ""
set windowTitleText to ""
set windowCountText to ""
set windowChildCountText to ""
set sheetCountText to ""
set popoverCountText to ""
set roleText to ""
set subroleText to ""
set descriptionText to ""
set focusedText to ""
set xText to ""
set yText to ""
set widthText to ""
set heightText to ""
set actionText to ""

tell application "System Events"
    {selection}
    try
        set windowCountText to count of windows of p as text
    end try
    try
        set w to window {window_index} of p
        try
            set windowTitleText to title of w as text
        end try
        try
            set windowChildCountText to count of UI elements of w as text
        end try
        try
            set sheetCountText to count of sheets of w as text
        end try
        try
            set popoverCountText to count of pop overs of w as text
        end try
        try
            tell p
                set targetElement to {expression}
            end tell
            set connectedText to "true"
            set roleText to ""
            set subroleText to ""
            try
                set roleText to role of targetElement as text
            end try
            try
                set subroleText to subrole of targetElement as text
            end try
            if roleText contains "SecureText" or subroleText contains "Secure" then
                set valueText to "[redacted]"
            else
                try
                    set valueText to value of targetElement as text
                end try
            end if
            try
                set characterCountText to value of attribute "AXNumberOfCharacters" of targetElement as text
            end try
            try
                set selectedText to selected of targetElement as text
            end try
            try
                set enabledText to enabled of targetElement as text
            end try
            try
                set titleText to title of targetElement as text
            end try
            try
                set descriptionText to description of targetElement as text
            end try
            try
                set focusedText to focused of targetElement as text
            end try
            try
                set tp to position of targetElement
                set xText to item 1 of tp as text
                set yText to item 2 of tp as text
            end try
            try
                set ts to size of targetElement
                set widthText to item 1 of ts as text
                set heightText to item 2 of ts as text
            end try
            try
                set actionText to name of actions of targetElement as text
            end try
            try
                set childCountText to count of UI elements of targetElement as text
            end try
        end try
    end try
end tell

return "__EFFECT__" & fs & connectedText & fs & my cleanEffectText(valueText, fs) & fs & ¬
    my cleanEffectText(characterCountText, fs) & fs & my cleanEffectText(selectedText, fs) & fs & ¬
    my cleanEffectText(enabledText, fs) & fs & my cleanEffectText(titleText, fs) & fs & ¬
    my cleanEffectText(childCountText, fs) & fs & my cleanEffectText(windowTitleText, fs) & fs & ¬
    my cleanEffectText(windowCountText, fs) & fs & my cleanEffectText(windowChildCountText, fs) & fs & ¬
    my cleanEffectText(sheetCountText, fs) & fs & my cleanEffectText(popoverCountText, fs) & fs & ¬
    my cleanEffectText(roleText, fs) & fs & my cleanEffectText(subroleText, fs) & fs & ¬
    my cleanEffectText(descriptionText, fs) & fs & my cleanEffectText(focusedText, fs) & fs & ¬
    my cleanEffectText(xText, fs) & fs & my cleanEffectText(yText, fs) & fs & ¬
    my cleanEffectText(widthText, fs) & fs & my cleanEffectText(heightText, fs) & fs & ¬
    my cleanEffectText(actionText, fs)
'''


def _probe_native_effect_state(
    app: str,
    element_id: str,
    *,
    app_pid: Optional[int] = None,
    deadline: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ok, stdout, stderr = _run_osascript(
        _native_effect_state_script(app, element_id, app_pid=app_pid),
        timeout_s=_operation_timeout(deadline, 6),
    )
    if not ok:
        return None, stderr or "native Accessibility effect probe failed"
    state = _parse_native_effect_state(stdout)
    if state.get("probe_error"):
        return None, str(state["probe_error"])
    return state, None

def _wait_for_native_effect(
    app: str,
    action: Dict[str, Any],
    before_state: Dict[str, Any],
    *,
    app_pid: Optional[int] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    element_id = _validate_element_id(action.get("element_id"))
    timeout_s = max(0.1, min(float(action.get("verify_timeout_s", _NATIVE_ACTION_VERIFY_TIMEOUT_S)), 2.0))
    poll_s = max(0.03, min(float(action.get("verify_poll_ms", _NATIVE_ACTION_VERIFY_POLL_S * 1000)) / 1000.0, 0.25))
    started = time.perf_counter()
    local_deadline = started + timeout_s
    if deadline is not None:
        local_deadline = min(local_deadline, deadline)
    attempts = 0
    last_state: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    while True:
        state, probe_error = _probe_native_effect_state(
            app, element_id, app_pid=app_pid, deadline=local_deadline,
        )
        attempts += 1
        now = time.perf_counter()
        if state is not None:
            last_state = state
            changed, verification = _native_effect_changed(before_state, state, action)
            if changed:
                return {
                    "effect_observed": True,
                    "verification": verification,
                    "attempts": attempts,
                    "duration_ms": int((now - started) * 1000),
                    "state": state,
                }
        elif probe_error:
            last_error = probe_error

        if now >= local_deadline:
            if last_state is None and last_error:
                return {
                    "effect_observed": False,
                    "verification": "verification_unavailable",
                    "reason_code": "ACTION_VERIFICATION_UNAVAILABLE",
                    "error": last_error,
                    "attempts": attempts,
                    "duration_ms": int((now - started) * 1000),
                    "automatic_retry": False,
                }
            return {
                "effect_observed": False,
                "verification": "no_effect_after_bounded_wait",
                "reason_code": "ACTION_NO_EFFECT",
                "attempts": attempts,
                "duration_ms": int((now - started) * 1000),
                "automatic_retry": False,
            }
        time.sleep(min(poll_s, max(0.0, local_deadline - now)))

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
    activate_target: bool = True,
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
            _target_script(app, element_id, click_body, activate=activate_target, app_pid=app_pid),
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
    activate_target: bool = True,
) -> Tuple[bool, str]:
    ok, _, error = _run_osascript(
        _target_script(app, element_id, "click targetElement", activate=activate_target, app_pid=app_pid),
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
    activate_target: bool = True,
) -> Tuple[bool, str]:
    focused, focus_error = _focus_element(app, element_id, deadline, app_pid, activate_target)
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
            _process_script(app, 'keystroke "v" using {command down}', activate=activate_target, app_pid=app_pid),
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
    activate_target: bool = True,
) -> Tuple[bool, str]:
    focused, focus_error = _focus_element(app, element_id, deadline, app_pid, activate_target)
    if not focused:
        return False, focus_error or "Could not focus target element"
    if clear:
        ok, _, error = _run_osascript(
            _process_script(
                app,
                'keystroke "a" using {command down}\n        key code 51',
                app_pid=app_pid,
                activate=activate_target,
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
    pasted, paste_error = _paste_text(app, element_id, text, deadline, app_pid, activate_target)
    return pasted, paste_error if not pasted else "text pasted as typing fallback"


def _key(
    app: str,
    key: Any,
    modifiers: Any,
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
    activate_target: bool = True,
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
        _process_script(app, command, activate=activate_target, app_pid=app_pid), timeout_s=_operation_timeout(deadline, 30)
    )
    return ok, error or "key sent"


def _scroll(
    app: str,
    action: Dict[str, Any],
    node: Optional[Dict[str, Any]],
    deadline: Optional[float] = None,
    app_pid: Optional[int] = None,
    activate_target: bool = True,
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
            f'            perform action "{action_name}" of targetElement\n'
            f'            delay 0.1\n'
            f'        end repeat'
        )
        ok, _, error = _run_osascript(
            _target_script(app, element_id, body, activate=activate_target, app_pid=app_pid),
            timeout_s=_operation_timeout(deadline, 30),
        )
        if ok:
            return True, "semantic scroll completed"
        if app_pid is not None or not activate_target:
            return False, error or "semantic scroll failed for stable/background native target; key fallback disabled"
        # If an app does not expose AXScroll actions, foreground mode may fall back to page keys.

    key_name = {
        "down": "pagedown",
        "up": "pageup",
        "left": "left",
        "right": "right",
    }[direction]
    for _ in range(pages):
        ok, message = _key(app, key_name, [], deadline, app_pid, activate_target)
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
    activate_target: bool = True,
) -> Tuple[bool, str]:
    try:
        element_id = _validate_element_id(action.get("element_id"))
    except ValueError as exc:
        return False, str(exc)
    action_name = str(action.get("name", "")).strip()
    if not re.fullmatch(r"AX[A-Za-z0-9]+", action_name):
        return False, "name must be an Accessibility action such as AXPress or AXShowMenu"
    ok, _, error = _run_osascript(
        _target_script(app, element_id, f'perform action "{action_name}" of targetElement', activate=activate_target, app_pid=app_pid),
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
    activate_target: bool = True,
) -> Tuple[bool, str]:
    action_type = str(action.get("type", "")).strip().lower().replace("-", "_")
    if action_type in {"click", "double_click"}:
        return _click(app, action, node, deadline, app_pid, activate_target)
    if action_type == "scroll":
        return _scroll(app, action, node, deadline, app_pid, activate_target)
    if action_type in {"type", "type_text"}:
        element_id = _validate_element_id(action.get("element_id"))
        text = action.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text) > _MAX_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TEXT_CHARS} characters")
        return _type_text(app, element_id, text, bool(action.get("clear", True)), deadline, app_pid, activate_target)
    if action_type == "paste":
        element_id = _validate_element_id(action.get("element_id"))
        text = action.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text) > _MAX_TEXT_CHARS:
            raise ValueError(f"text must be at most {_MAX_TEXT_CHARS} characters")
        return _paste_text(app, element_id, text, deadline, app_pid, activate_target)
    if action_type in {"key", "keyboard", "shortcut"}:
        return _key(app, action.get("key"), action.get("modifiers", []), deadline, app_pid, activate_target)
    if action_type in {"action", "accessibility_action", "menu"}:
        return _accessibility_action(app, action, deadline, app_pid, activate_target)
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
    return_state: Optional[bool] = None,
    allow_risky: bool = False,
    app_handle: Optional[str] = None,
    window_handle: Optional[str] = None,
    preserve_focus: bool = True,
    state_mode: Optional[str] = None,
    include_screenshot: bool = False,
) -> Any:
    """Perform bounded macOS UI actions against re-resolved native app/window handles."""
    deadline = time.monotonic() + _ACTION_BUDGET_S
    try:
        resolved_state_mode, legacy_full_screenshot = _normalize_action_state_mode(
            state_mode, return_state
        )
        effective_screenshot = bool(include_screenshot or legacy_full_screenshot)
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
        delta_updates: Dict[str, Optional[Dict[str, Any]]] = {}
        delta_structural_refresh = stored is None
        delta_refresh_reasons: List[str] = []
        if stored is None:
            delta_refresh_reasons.append("missing_base_observation")

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

            readiness: Optional[Dict[str, Any]] = None
            before_state: Optional[Dict[str, Any]] = None
            if resolved_element_id is not None:
                try:
                    readiness = _wait_for_native_readiness(
                        str(target.get("app") or ""),
                        resolved_action,
                        node,
                        app_pid=int(target["pid"]) if target.get("pid") else None,
                        deadline=deadline,
                    )
                except TimeoutError as exc:
                    readiness = {
                        "ready": False, "timed_out": True,
                        "reason_code": "READINESS_TIMEOUT", "error": str(exc),
                        "retryable": True,
                    }
                before_state = readiness.get("state") if isinstance(readiness.get("state"), dict) else None
                if not readiness.get("ready"):
                    public_readiness = {k: v for k, v in readiness.items() if k != "state"}
                    failed = {
                        "index": index,
                        "type": action_type,
                        "element_id": original_element_id,
                        "ok": False,
                        "error": "element_not_ready",
                        "reason_code": readiness.get("reason_code") or "ELEMENT_NOT_READY",
                        "readiness": public_readiness,
                        "observe_again": True,
                        "retryable": bool(readiness.get("retryable", True)),
                        "app_handle": target.get("app_handle"),
                        "window_handle": target.get("window_handle"),
                        "resolved_window_index": target.get("window_index"),
                    }
                    if resolved_element_id != original_element_id:
                        failed["resolved_element_id"] = resolved_element_id
                    results.append(failed)
                    return {
                        "ok": False,
                        "error": "element_not_ready",
                        "reason_code": failed["reason_code"],
                        "active_app": target.get("app"),
                        "app_handle": target.get("app_handle"),
                        "window_handle": target.get("window_handle"),
                        "actions": results,
                    }

            focus_context: Optional[Dict[str, Any]] = None
            focus_required = _action_requires_foreground(resolved_action)
            focus_transition = False
            activate_target = True
            focus_mode = "foreground_allowed"
            focus_restore_attempted = False
            focus_restore_ok: Optional[bool] = None
            focus_restore_exact = False
            focus_restore_message: Optional[str] = None
            focus_user_changed = False

            if preserve_focus:
                focus_context, focus_error = _capture_focus_context(deadline)
                if focus_context is None:
                    return _native_target_error(
                        "FOCUS_SNAPSHOT_FAILED",
                        focus_error or "Could not capture the user's current frontmost app/window before acting.",
                        actions=results,
                        failed_action_index=index,
                    )
                focus_transition = _focus_transition_needed(focus_context, target)
                if (
                    focus_required
                    and focus_transition
                    and int(focus_context.get("window_count") or 0) > 1
                    and not focus_context.get("window_handle")
                ):
                    return _native_target_error(
                        "FOCUS_SNAPSHOT_AMBIGUOUS",
                        "The current user window does not have a stable identity, so a temporary focus switch cannot be restored safely.",
                        actions=results,
                        failed_action_index=index,
                        retryable=False,
                    )
                activate_target = bool(focus_required)
                focus_mode = "temporary_foreground_restore" if focus_required and focus_transition else (
                    "foreground_same_target" if focus_required else "background_ax"
                )

            started = time.perf_counter()
            timed_out = False
            try:
                ok, message = _perform_action(
                    str(target.get("app") or ""),
                    resolved_action,
                    node,
                    deadline,
                    int(target["pid"]) if target.get("pid") else None,
                    activate_target,
                )
            except TimeoutError as exc:
                ok, message = False, str(exc)
                timed_out = True
            except ValueError as exc:
                ok, message = False, str(exc)

            if preserve_focus and focus_context is not None:
                focus_decision, focus_decision_error = _post_action_focus_decision(
                    focus_context, target, deadline
                )
                if focus_decision == "restore":
                    focus_restore_attempted = True
                    try:
                        focus_restore_ok, restore_message, focus_restore_exact = _restore_focus_context(
                            focus_context, deadline
                        )
                        focus_restore_message = restore_message
                    except TimeoutError as exc:
                        focus_restore_ok = False
                        focus_restore_message = str(exc)
                elif focus_decision == "preserved":
                    focus_restore_ok = True
                elif focus_decision == "user_changed":
                    focus_user_changed = True
                    focus_restore_ok = True
                    focus_restore_message = "focus restoration skipped because the user changed foreground focus during the action"
                else:
                    # Never blindly restore when current focus cannot be read: doing so
                    # could steal focus from a user who moved elsewhere during the action.
                    focus_restore_ok = False
                    focus_restore_message = focus_decision_error or "current focus could not be verified after the action"

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
                "focus_mode": focus_mode,
                "preserve_focus": bool(preserve_focus),
            }
            if preserve_focus:
                result["focus_preserved"] = focus_restore_ok is True
                result["focus_restore_attempted"] = focus_restore_attempted
                if focus_user_changed:
                    result["focus_user_changed"] = True
                    result["focus_restore_skipped_user_change"] = True
                    if focus_restore_message:
                        result["focus_restore_message"] = focus_restore_message
                if focus_restore_attempted:
                    result["focus_restored"] = focus_restore_ok is True
                    result["focus_restore_exact"] = bool(focus_restore_exact)
                    if focus_restore_message:
                        result["focus_restore_message"] = focus_restore_message
            if resolved_element_id != original_element_id:
                result["resolved_element_id"] = resolved_element_id
            if readiness is not None:
                result["readiness"] = {
                    key: value for key, value in readiness.items()
                    if key != "state"
                }
            if timed_out:
                result["timed_out"] = True

            if (
                result.get("ok")
                and before_state is not None
                and _native_verification_required(action_type, resolved_element_id)
            ):
                try:
                    verification = _wait_for_native_effect(
                        str(target.get("app") or ""),
                        resolved_action,
                        before_state,
                        app_pid=int(target["pid"]) if target.get("pid") else None,
                        deadline=deadline,
                    )
                except TimeoutError as exc:
                    verification = {
                        "effect_observed": False,
                        "verification": "verification_timeout",
                        "reason_code": "ACTION_VERIFICATION_UNAVAILABLE",
                        "error": str(exc),
                        "automatic_retry": False,
                    }
                result["effect_observed"] = bool(verification.get("effect_observed"))
                result["verification"] = verification.get("verification")
                if verification.get("attempts") is not None:
                    result["verification_attempts"] = verification.get("attempts")
                if verification.get("duration_ms") is not None:
                    result["verification_duration_ms"] = verification.get("duration_ms")
                verification_state = verification.get("state")
                if verification.get("effect_observed") and isinstance(verification_state, dict):
                    if _effect_state_requires_structural_refresh(before_state, verification_state):
                        delta_structural_refresh = True
                        delta_refresh_reasons.append("structural_effect")
                    elif original_element_id is not None:
                        base_node = delta_updates.get(original_element_id, node)
                        merged_node = _merge_effect_state_into_node(base_node, verification_state)
                        if merged_node is None:
                            delta_structural_refresh = True
                            delta_refresh_reasons.append("target_detached")
                        else:
                            delta_updates[original_element_id] = merged_node
                elif verification.get("effect_observed"):
                    delta_structural_refresh = True
                    delta_refresh_reasons.append("effect_state_unavailable")
                if not verification.get("effect_observed"):
                    result.update({
                        "ok": False,
                        "error": "action_no_effect" if verification.get("reason_code") == "ACTION_NO_EFFECT" else "action_verification_unavailable",
                        "reason_code": verification.get("reason_code") or "ACTION_NO_EFFECT",
                        "automatic_retry": False,
                        "observe_again": True,
                    })
                    if verification.get("error"):
                        result["verification_error"] = verification.get("error")
                    result["message"] = (
                        "action executed but no observable native UI effect was detected"
                        if result["reason_code"] == "ACTION_NO_EFFECT"
                        else "action executed but its effect could not be verified safely"
                    )
            elif result.get("ok") and resolved_element_id is not None:
                result["verification"] = "readiness_only"
                delta_structural_refresh = True
                delta_refresh_reasons.append("unverified_element_effect")
            elif result.get("ok"):
                delta_structural_refresh = True
                delta_refresh_reasons.append("unbound_action_effect")

            if preserve_focus and focus_restore_ok is False:
                result.update({
                    "ok": False,
                    "error": "focus_restore_failed",
                    "reason_code": "FOCUS_RESTORE_FAILED",
                    "automatic_retry": False,
                    "observe_again": True,
                    "message": (
                        "The native action may have executed, but the user's previous focus could not be restored safely."
                    ),
                })
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
            results.append(result)
            if not result.get("ok"):
                response = {
                    "ok": False,
                    "active_app": target.get("app"),
                    "app_handle": target.get("app_handle"),
                    "window_handle": target.get("window_handle"),
                    "actions": results,
                }
                if result.get("reason_code"):
                    response["reason_code"] = result.get("reason_code")
                if result.get("automatic_retry") is False:
                    response["automatic_retry"] = False
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

        if resolved_state_mode == "none":
            return {
                "ok": True,
                "state_mode": "none",
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
                    "state_mode": resolved_state_mode,
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

        if resolved_state_mode == "delta" and stored is not None and observation_id and not delta_structural_refresh:
            derived_observation_id, derived = _store_derived_observation(
                observation_id, delta_updates
            )
            if derived_observation_id and derived is not None:
                after_nodes = dict(derived.get("nodes") or {})
                delta = _diff_observation_nodes(stored_nodes, after_nodes)
                delta.update({
                    "source": "verification_probe",
                    "structural_refresh": False,
                    "refresh_reasons": [],
                })
                payload = _delta_base_payload(
                    observation_id=derived_observation_id,
                    previous_observation_id=observation_id,
                    active_app=post_app,
                    app_handle=(last_target or {}).get("app_handle") or post_app_handle,
                    window_handle=(last_target or {}).get("window_handle") or post_window_handle,
                    node_count=len(after_nodes),
                    delta=delta,
                    actions=results,
                    screenshot_requested=effective_screenshot,
                )
                image_data: Optional[bytes] = None
                if effective_screenshot:
                    try:
                        image_data, screenshot_error = _capture_screen(
                            _operation_timeout(deadline, 10)
                        )
                    except TimeoutError as exc:
                        image_data, screenshot_error = None, str(exc)
                    payload["screenshot"].update({
                        "included_as_image_content": bool(image_data),
                        "mime_type": f"image/{_SCREENSHOT_FORMAT}" if image_data else None,
                    })
                    if screenshot_error:
                        payload["screenshot"]["error"] = screenshot_error
                return _format_result(payload, image_data)
            delta_structural_refresh = True
            delta_refresh_reasons.append("base_observation_expired")

        try:
            post_payload, image_data = _collect_observation(
                settings,
                post_app,
                post_window_index,
                max_depth=5,
                max_children=30,
                include_screenshot=effective_screenshot,
                ocr=False,
                deadline=deadline,
                app_pid=int(post_pid) if post_pid else None,
            )
        except TimeoutError as exc:
            return {
                "ok": True,
                "state_mode": resolved_state_mode,
                "active_app": post_app,
                "actions": results,
                "post_state_ok": False,
                "post_state_error": str(exc),
                "previous_observation_id": observation_id,
            }

        if resolved_state_mode == "delta":
            post_nodes = {
                node["element_id"]: node
                for node in post_payload.get("nodes", [])
                if isinstance(node, dict) and node.get("element_id")
            }
            delta = _diff_observation_nodes(stored_nodes if stored is not None else {}, post_nodes)
            delta.update({
                "source": "accessibility_refresh",
                "structural_refresh": True,
                "refresh_reasons": sorted(set(delta_refresh_reasons)) or ["delta_refresh_required"],
            })
            payload = _delta_base_payload(
                observation_id=post_payload.get("observation_id"),
                previous_observation_id=observation_id,
                active_app=post_payload.get("active_app") or post_app,
                app_handle=post_payload.get("app_handle") or post_app_handle,
                window_handle=post_payload.get("window_handle") or post_window_handle,
                node_count=int(post_payload.get("node_count") or len(post_nodes)),
                delta=delta,
                actions=results,
                screenshot_requested=effective_screenshot,
            )
            payload["screenshot"] = post_payload.get("screenshot", payload["screenshot"])
            return _format_result(payload, image_data)

        post_payload["state_mode"] = "full"
        post_payload["actions"] = results
        post_payload["previous_observation_id"] = observation_id
        return _format_result(post_payload, image_data)
    except TimeoutError as exc:
        return {"ok": False, "timed_out": True, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Could not act on macOS UI: {exc}"}
