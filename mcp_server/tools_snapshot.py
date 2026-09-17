from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .security import Settings
from .tools_browser import browser_list_tabs
from .tools_macos import get_running_apps
from .tools_terminal import get_system_info
from .tools_ui import _scan_native_windows


DEFAULT_SECTIONS = ("apps", "windows", "selected_context", "browser_tabs", "clipboard", "system")
_MAX_SECTIONS = len(DEFAULT_SECTIONS)
_DEFAULT_OUTPUT_BUDGET = 16_384
_MAX_OUTPUT_BUDGET = 64_000


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _run_osascript(script: str, timeout_s: float = 5.0) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        timeout=max(0.2, min(float(timeout_s), 15.0)),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "osascript failed").strip())
    return (proc.stdout or "").strip()


def _read_apps(settings: Settings, *, app_limit: int, **_: Any) -> Dict[str, Any]:
    result = get_running_apps(settings)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or result.get("stderr") or "could not list running apps"))
    raw = str(result.get("stdout") or "")
    apps = [part.strip() for part in raw.split(",") if part.strip()]
    return {"count": len(apps), "apps": apps[:app_limit], "truncated": len(apps) > app_limit}


def _read_windows(settings: Settings, *, window_limit: int, **_: Any) -> Dict[str, Any]:
    metadata, error = _scan_native_windows(None, None)
    if metadata is None:
        raise RuntimeError(error or "could not read native windows")
    rows = []
    for row in list(metadata.get("windows") or [])[:window_limit]:
        if not isinstance(row, dict):
            continue
        rows.append({
            key: row.get(key)
            for key in (
                "index", "title", "focused", "main", "minimized", "position",
                "window_handle", "identity_kind", "identity_status",
            )
            if row.get(key) is not None
        })
    total = int(metadata.get("window_count") or len(metadata.get("windows") or []))
    return {
        "active_app": metadata.get("active_app"),
        "pid": metadata.get("pid"),
        "app_handle": metadata.get("app_handle"),
        "window_count": total,
        "windows": rows,
        "truncated": total > len(rows),
    }


def _read_selected_context(settings: Settings, *, selected_file_limit: int, **_: Any) -> Dict[str, Any]:
    # ASCII record separator keeps paths independent of commas/spaces and never reads file contents.
    script = r'''
set rs to ASCII character 30
set folderPath to ""
set selectedPaths to {}
tell application "System Events"
    set finderRunning to exists application process "Finder"
end tell
if finderRunning then
    tell application "Finder"
        try
            set folderPath to POSIX path of (insertion location as alias)
        end try
        try
            repeat with anItem in selection
                try
                    set end of selectedPaths to POSIX path of (anItem as alias)
                end try
            end repeat
        end try
    end tell
end if
set AppleScript's text item delimiters to rs
set selectedText to selectedPaths as text
set AppleScript's text item delimiters to ""
return (finderRunning as text) & rs & folderPath & rs & selectedText
'''
    raw = _run_osascript(script)
    parts = raw.split(chr(30)) if raw else []
    finder_running = bool(parts and parts[0].strip().lower() == "true")
    folder = parts[1].strip() if len(parts) > 1 else ""
    selected = [part.strip() for part in parts[2:] if part.strip()]
    return {
        "finder_running": finder_running,
        "folder": folder or None,
        "selected_count": len(selected),
        "selected_paths": selected[:selected_file_limit],
        "truncated": len(selected) > selected_file_limit,
    }


def _read_browser_tabs(settings: Settings, *, browser_tab_limit: int, **_: Any) -> Dict[str, Any]:
    browsers: Dict[str, Any] = {}
    combined: List[Dict[str, Any]] = []
    for browser in ("Safari", "Chrome"):
        try:
            result = browser_list_tabs(settings, browser)
            tabs = list(result.get("tabs") or [])
            browsers[browser] = {"ok": True, "count": len(tabs)}
            for row in tabs:
                if not isinstance(row, dict):
                    continue
                combined.append({
                    key: row.get(key)
                    for key in ("browser", "window_index", "tab_index", "active", "title", "url", "tab_handle")
                    if row.get(key) is not None
                })
        except Exception as exc:
            browsers[browser] = {"ok": False, "error": str(exc)[:240]}
    successful_browsers = [name for name, row in browsers.items() if row.get("ok")]
    failed_browsers = [name for name, row in browsers.items() if not row.get("ok")]
    if not successful_browsers:
        detail = "; ".join(f"{name}: {browsers[name].get('error')}" for name in failed_browsers)
        raise RuntimeError(f"could not enumerate browser tabs ({detail})")
    # Active tabs first, then stable browser/window/tab ordering.
    combined.sort(key=lambda row: (not bool(row.get("active")), str(row.get("browser")), int(row.get("window_index") or 0), int(row.get("tab_index") or 0)))
    return {
        "count": len(combined),
        "tabs": combined[:browser_tab_limit],
        "browsers": browsers,
        "partial": bool(failed_browsers),
        "failed_browsers": failed_browsers,
        "truncated": len(combined) > browser_tab_limit,
    }


def _read_clipboard(settings: Settings, **_: Any) -> Dict[str, Any]:
    # `clipboard info` returns only declared pasteboard types and byte sizes, never contents.
    raw = _run_osascript("clipboard info")
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    types: List[Dict[str, Any]] = []
    for i in range(0, len(parts), 2):
        kind = parts[i]
        size: Optional[int] = None
        if i + 1 < len(parts):
            try:
                size = int(parts[i + 1])
            except ValueError:
                pass
        types.append({"type": kind, "bytes": size})
    return {
        "type_count": len(types),
        "types": types[:12],
        "has_text": any(token in str(item.get("type") or "").lower() for item in types for token in ("text", "string", "utf8", "ut16")),
        "content_included": False,
        "truncated": len(types) > 12,
    }


def _parse_system_stdout(stdout: str) -> Dict[str, Any]:
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in str(stdout or "").splitlines():
        match = re.fullmatch(r"=== ([A-Z]+) ===", line.strip())
        if match:
            current = match.group(1).lower()
            sections[current] = []
        elif current and line.strip():
            sections[current].append(line.strip())
    uptime = (sections.get("uptime") or [None])[0]
    load = None
    if uptime:
        match = re.search(r"load averages?:\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)", uptime)
        if match:
            load = [float(match.group(i)) for i in (1, 2, 3)]
    return {
        "hostname": (sections.get("hostname") or [None])[0],
        "uptime": uptime,
        "load_average": load,
        "cpu": (sections.get("cpu") or [None])[0],
        "memory": sections.get("memory") or [],
        "disk": (sections.get("disk") or [None])[0],
        "battery": sections.get("battery") or [],
    }


def _read_system(settings: Settings, **_: Any) -> Dict[str, Any]:
    result = get_system_info(settings)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or result.get("stderr") or "could not read system info"))
    return _parse_system_stdout(str(result.get("stdout") or ""))


_SECTION_READERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "apps": _read_apps,
    "windows": _read_windows,
    "selected_context": _read_selected_context,
    "browser_tabs": _read_browser_tabs,
    "clipboard": _read_clipboard,
    "system": _read_system,
}


def _clip(value: Any, *, max_items: int, max_string: int, depth: int = 0) -> Any:
    if depth >= 5:
        return str(value)[:max_string]
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max(0, max_string - 1)] + "…"
    if isinstance(value, list):
        return [_clip(item, max_items=max_items, max_string=max_string, depth=depth + 1) for item in value[:max_items]]
    if isinstance(value, dict):
        return {str(k): _clip(v, max_items=max_items, max_string=max_string, depth=depth + 1) for k, v in value.items()}
    return value



def _with_output_bytes(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Stabilize the self-reported JSON size after adding the field itself.
    payload["output_bytes"] = 0
    for _ in range(3):
        measured = _json_bytes(payload)
        if payload.get("output_bytes") == measured:
            break
        payload["output_bytes"] = measured
    return payload

def _fit_snapshot(payload: Dict[str, Any], budget_bytes: int) -> Dict[str, Any]:
    budget = max(4_096, min(int(budget_bytes), _MAX_OUTPUT_BUDGET))
    candidate = _with_output_bytes(dict(payload))
    if _json_bytes(candidate) <= budget:
        return candidate
    out = dict(payload)
    out["output_truncated"] = True
    for max_items, max_string in ((8, 320), (4, 220), (2, 160), (1, 100)):
        out["sections"] = _clip(payload.get("sections", {}), max_items=max_items, max_string=max_string)
        candidate = _with_output_bytes(dict(out))
        if _json_bytes(candidate) <= budget:
            return candidate
    compact_sections = {}
    for name, section in (payload.get("sections") or {}).items():
        if isinstance(section, dict):
            compact_sections[name] = {
                key: section.get(key)
                for key in ("ok", "duration_ms", "error")
                if section.get(key) is not None
            }
            compact_sections[name]["data_omitted"] = True
    out["sections"] = compact_sections
    return _with_output_bytes(out)


def unified_read_snapshot(
    settings: Settings,
    sections: Optional[List[str]] = None,
    app_limit: int = 20,
    window_limit: int = 12,
    browser_tab_limit: int = 12,
    selected_file_limit: int = 10,
    max_output_bytes: int = _DEFAULT_OUTPUT_BUDGET,
) -> Dict[str, Any]:
    requested = list(DEFAULT_SECTIONS if sections is None else sections)
    requested = list(dict.fromkeys(str(item).strip().lower() for item in requested if str(item).strip()))
    if not requested:
        raise ValueError("sections must contain at least one snapshot section")
    unknown = [item for item in requested if item not in _SECTION_READERS]
    if unknown:
        raise ValueError(f"Unknown snapshot sections: {', '.join(unknown)}")
    if len(requested) > _MAX_SECTIONS:
        raise ValueError(f"sections may contain at most {_MAX_SECTIONS} items")

    limits = {
        "app_limit": max(1, min(int(app_limit), 50)),
        "window_limit": max(1, min(int(window_limit), 30)),
        "browser_tab_limit": max(1, min(int(browser_tab_limit), 30)),
        "selected_file_limit": max(1, min(int(selected_file_limit), 30)),
    }
    started = time.perf_counter()
    results: Dict[str, Dict[str, Any]] = {}
    worker_count = min(len(requested), _MAX_SECTIONS)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mac-snapshot") as executor:
        futures = {}
        for name in requested:
            section_start = time.perf_counter()
            future = executor.submit(_SECTION_READERS[name], settings, **limits)
            futures[future] = (name, section_start)
        for future in as_completed(futures):
            name, section_start = futures[future]
            duration_ms = int((time.perf_counter() - section_start) * 1000)
            try:
                data = future.result()
                results[name] = {"ok": True, "duration_ms": duration_ms, "data": data}
            except Exception as exc:
                results[name] = {"ok": False, "duration_ms": duration_ms, "error": str(exc)[:500]}

    ordered = {name: results[name] for name in requested}
    failed = [name for name, row in ordered.items() if not row.get("ok")]
    payload: Dict[str, Any] = {
        "ok": len(failed) < len(requested),
        "partial": bool(failed),
        "requested_sections": requested,
        "completed_sections": [name for name in requested if name not in failed],
        "failed_sections": failed,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "parallelism": worker_count,
        "sections": ordered,
        "limits": limits,
    }
    return _fit_snapshot(payload, max_output_bytes)
