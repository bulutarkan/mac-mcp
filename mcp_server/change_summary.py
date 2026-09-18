from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

from .observability import sanitize_value

_SIDE_EFFECT_CAPS = {
    "local_write", "ui_action", "external_side_effect", "update_control", "agent_delegation",
}

_FILE_TOOLS = {
    "write_file", "write_files_batch", "edit_file", "move_file", "copy_file",
    "delete_path", "file_transaction_batch", "file_transaction_undo", "create_directory",
    "screenshot", "browser_screenshot", "browser_wait_for_download", "browser_upload_artifact",
}
_BROWSER_TOOLS = {
    "browser_open_url", "browser_activate_tab", "browser_close_tab", "browser_act", "browser_do",
    "browser_execute_js", "browser_click_selector", "browser_type_selector", "browser_scroll",
    "browser_press_key", "browser_coordinate_click",
}
_COMMAND_TOOLS = {
    "run_command", "run_commands_parallel", "start_background_job", "stop_job", "kill_process",
    "run_applescript", "computer_plan", "tool_invoke",
}
_APP_TOOLS = {"open_app", "mac_act", "mac_app"}
_EXTERNAL_TOOLS = {"http_request", "open_url", "send_notification", "set_reminder"}
_SYSTEM_TOOLS = {"clipboard_set", "set_volume", "set_brightness", "mac_mcp_update"}
_AGENT_TOOLS = {"spawn_agent", "spawn_agents", "agent_action"}


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_text(value: Any, limit: int = 180) -> Optional[str]:
    text = str(sanitize_value(value, preview_chars=limit) or "").strip()
    if not text or text.startswith("[") and text.endswith("]"):
        return text or None
    return " ".join(text.split())[:limit]


def _safe_path(value: Any) -> Optional[str]:
    text = _safe_text(value, 500)
    if not text:
        return None
    try:
        return str(Path(text).expanduser())[:500]
    except (TypeError, ValueError):
        return text[:500]


def _safe_origin(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    default = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port
    except ValueError:
        return None
    suffix = f":{port}" if port and port != default else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _capabilities(event: Mapping[str, Any]) -> set[str]:
    risk = _mapping(event.get("effective_risk"))
    raw = risk.get("capabilities") or []
    return {str(item) for item in raw if item}


def _is_change(event: Mapping[str, Any]) -> bool:
    if str(event.get("status") or "").lower() != "success":
        return False
    tool = str(event.get("tool") or "")
    if tool in _FILE_TOOLS | _BROWSER_TOOLS | _COMMAND_TOOLS | _APP_TOOLS | _EXTERNAL_TOOLS | _SYSTEM_TOOLS | _AGENT_TOOLS:
        return True
    return bool(_capabilities(event).intersection(_SIDE_EFFECT_CAPS))


def _file_items(event: Mapping[str, Any]) -> List[Dict[str, Any]]:
    tool = str(event.get("tool") or "")
    args = _mapping(event.get("arguments")); result = _mapping(event.get("result"))
    items: List[Dict[str, Any]] = []
    if tool == "write_files_batch":
        paths = result.get("written") if isinstance(result.get("written"), list) else []
        if not paths:
            paths = [item.get("path") for item in (args.get("files") or []) if isinstance(item, Mapping)]
        for path in paths:
            if target := _safe_path(path):
                items.append({"action": "Wrote file", "target": target})
    elif tool == "file_transaction_batch":
        for action in args.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            kind = str(action.get("type") or "").lower()
            if kind == "move":
                source = _safe_path(action.get("source")); dest = _safe_path(action.get("destination"))
                items.append({"action": "Moved file", "target": dest or source or "File", "detail": f"{source} → {dest}" if source and dest else None})
            else:
                target = _safe_path(action.get("path") or action.get("destination") or action.get("source"))
                if target:
                    labels = {"write": "Wrote file", "delete": "Deleted path", "copy": "Copied file", "mkdir": "Created directory"}
                    items.append({"action": labels.get(kind, "Changed file"), "target": target})
    elif tool == "move_file":
        source = _safe_path(args.get("source")); dest = _safe_path(args.get("destination") or result.get("destination"))
        items.append({"action": "Moved file", "target": dest or source or "File", "detail": f"{source} → {dest}" if source and dest else None})
    elif tool == "copy_file":
        source = _safe_path(args.get("source")); dest = _safe_path(args.get("destination") or result.get("destination"))
        items.append({"action": "Copied file", "target": dest or source or "File", "detail": f"{source} → {dest}" if source and dest else None})
    else:
        target = _safe_path(result.get("path") or args.get("path") or result.get("file") or result.get("destination"))
        labels = {
            "write_file": "Wrote file", "edit_file": "Edited file", "delete_path": "Deleted path",
            "create_directory": "Created directory", "file_transaction_undo": "Undid file change",
            "screenshot": "Saved screenshot", "browser_screenshot": "Saved browser screenshot",
            "browser_wait_for_download": "Downloaded file", "browser_upload_artifact": "Uploaded file",
        }
        items.append({"action": labels.get(tool, "Changed file"), "target": target or "Filesystem"})
    return items or [{"action": "Changed filesystem", "target": "Filesystem"}]


def _browser_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or "")
    args = _mapping(event.get("arguments")); result = _mapping(event.get("result"))
    browser = _safe_text(args.get("browser") or result.get("browser"), 80) or "Browser"
    origin = _safe_origin(result.get("url") or args.get("url"))
    handle = _safe_text(result.get("tab_handle") or args.get("tab_handle"), 80)
    labels = {
        "browser_open_url": "Opened or navigated tab", "browser_activate_tab": "Selected tab",
        "browser_close_tab": "Closed tab", "browser_act": "Interacted with page", "browser_do": "Ran browser task",
        "browser_execute_js": "Ran page script", "browser_click_selector": "Clicked page element",
        "browser_type_selector": "Typed into page", "browser_scroll": "Scrolled page",
        "browser_press_key": "Pressed browser key", "browser_coordinate_click": "Clicked page",
    }
    detail_parts = [part for part in (origin, f"tab {handle}" if handle else None) if part]
    return {"action": labels.get(tool, "Changed browser state"), "target": browser, "detail": " · ".join(detail_parts) or None}


def _command_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or ""); args = _mapping(event.get("arguments")); result = _mapping(event.get("result"))
    cwd = _safe_path(args.get("cwd"))
    labels = {
        "run_command": "Ran command", "run_commands_parallel": "Ran commands", "start_background_job": "Started background job",
        "stop_job": "Stopped background job", "kill_process": "Stopped process", "run_applescript": "Ran AppleScript",
        "computer_plan": "Ran computer plan", "tool_invoke": "Invoked tool",
    }
    detail = cwd
    if tool == "run_commands_parallel" and isinstance(args.get("commands"), list):
        detail = f"{len(args['commands'])} commands" + (f" · {cwd}" if cwd else "")
    elif tool == "start_background_job":
        job = _safe_text(result.get("job_id"), 80)
        detail = " · ".join(part for part in (job, cwd) if part) or None
    return {"action": labels.get(tool, "Ran action"), "target": "Terminal", "detail": detail}


def _app_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or ""); args = _mapping(event.get("arguments")); result = _mapping(event.get("result"))
    app = _safe_text(args.get("app_name") or args.get("app") or result.get("app") or result.get("application"), 100) or "macOS app"
    if tool == "open_app":
        return {"action": "Opened app", "target": app}
    if tool == "mac_app":
        action = _safe_text(args.get("action"), 80) or "app action"
        return {"action": "Used app adapter", "target": app, "detail": action}
    action = _safe_text(args.get("action") or args.get("action_type"), 80) or "UI action"
    return {"action": "Interacted with app", "target": app, "detail": action}


def _external_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or ""); args = _mapping(event.get("arguments")); result = _mapping(event.get("result"))
    if tool == "http_request":
        method = str(args.get("method") or "GET").upper()[:12]
        origin = _safe_origin(args.get("url") or result.get("url")) or "Remote endpoint"
        return {"action": f"Sent HTTP {method}", "target": origin}
    if tool == "open_url":
        return {"action": "Opened URL", "target": _safe_origin(args.get("url")) or "External URL"}
    if tool == "send_notification":
        return {"action": "Sent local notification", "target": "Notification Center"}
    if tool == "set_reminder":
        return {"action": "Created reminder", "target": "Reminders"}
    return {"action": "External side effect", "target": str(event.get("tool") or "External target")}


def _system_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or ""); args = _mapping(event.get("arguments"))
    labels = {
        "clipboard_set": ("Updated clipboard", "Clipboard"),
        "set_volume": ("Changed volume", "System audio"),
        "set_brightness": ("Changed brightness", "Display"),
        "mac_mcp_update": ("Updated Mac MCP", "Mac MCP"),
    }
    action, target = labels.get(tool, ("Changed system state", "macOS"))
    detail = None
    if tool in {"set_volume", "set_brightness"}:
        level = args.get("level")
        if isinstance(level, (int, float)):
            detail = f"Set to {int(level)}%"
    return {"action": action, "target": target, "detail": detail}


def _agent_item(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool = str(event.get("tool") or ""); args = _mapping(event.get("arguments"))
    labels = {"spawn_agent": "Started delegated agent", "spawn_agents": "Started agent team", "agent_action": "Changed agent state"}
    target = _safe_text(args.get("title") or args.get("team_id") or args.get("agent_id"), 120) or "Delegated agents"
    return {"action": labels.get(tool, "Changed agent state"), "target": target}


def change_items_for_event(event: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if not _is_change(event):
        return []
    tool = str(event.get("tool") or "")
    if tool in _FILE_TOOLS:
        category, raw_items = "files", _file_items(event)
    elif tool in _BROWSER_TOOLS:
        category, raw_items = "tabs", [_browser_item(event)]
    elif tool in _COMMAND_TOOLS:
        category, raw_items = "commands", [_command_item(event)]
    elif tool in _APP_TOOLS:
        category, raw_items = "apps", [_app_item(event)]
    elif tool in _EXTERNAL_TOOLS:
        category, raw_items = "external", [_external_item(event)]
    elif tool in _SYSTEM_TOOLS:
        category, raw_items = "system", [_system_item(event)]
    elif tool in _AGENT_TOOLS:
        category, raw_items = "agents", [_agent_item(event)]
    else:
        family = _safe_text(_mapping(event.get("effective_risk")).get("family"), 80) or "other"
        category, raw_items = "other", [{"action": "Changed Mac state", "target": family}]

    items: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        detail = item.get("detail")
        if detail is None:
            detail = None
        receipt = {
            "event_id": event.get("event_id"),
            "tool": tool,
            "timestamp": event.get("timestamp"),
            "category": category,
            "action": _safe_text(item.get("action"), 120) or "Changed Mac state",
            "target": _safe_text(item.get("target"), 500) or "macOS",
            "detail": _safe_text(detail, 500) if detail else None,
            "session_id": event.get("session_id"),
            "agent_id": event.get("agent_id"),
            "team_id": event.get("team_id"),
        }
        if index:
            receipt["event_item"] = index + 1
        items.append(receipt)
    return items


def build_change_summary(events: Iterable[Mapping[str, Any]], *, identity: Optional[Mapping[str, Any]] = None,
                         max_items: int = 50) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for event in sorted(list(events), key=lambda row: float(row.get("timestamp") or 0.0)):
        items.extend(change_items_for_event(event))
    items = items[-max(1, min(int(max_items), 200)):]
    counts = Counter(str(item.get("category") or "other") for item in items)
    last_at = max((float(item.get("timestamp") or 0.0) for item in items), default=None)
    first_at = min((float(item.get("timestamp") or 0.0) for item in items), default=None)
    labels = [
        ("files", "file", "files"), ("apps", "app", "apps"),
        ("tabs", "browser action", "browser actions"), ("commands", "command", "commands"),
        ("external", "external send", "external sends"), ("system", "system change", "system changes"),
        ("agents", "agent action", "agent actions"),
    ]
    parts = [f"{counts[key]} {singular if counts[key] == 1 else plural}" for key, singular, plural in labels if counts.get(key)]
    headline = " · ".join(parts[:4]) if parts else "No Mac changes recorded"
    return {
        "ok": True,
        "identity": dict(identity or {}),
        "change_count": len(items),
        "counts": dict(sorted(counts.items())),
        "headline": headline,
        "first_changed_at": first_at,
        "last_changed_at": last_at,
        "items": list(reversed(items)),
    }
