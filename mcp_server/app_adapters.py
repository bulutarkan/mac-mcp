from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from .security import Settings
from .tools_ui import (
    _apple_string,
    _capture_focus_context,
    _post_action_focus_decision,
    _restore_focus_context,
    _run_osascript,
)

_RS = chr(30)
_US = chr(31)
_ADAPTER_VERSION = 1
_MAX_LIMIT = 25
_DEFAULT_LIMIT = 10

_APP_ALIASES = {
    "finder": "Finder",
    "notes": "Notes",
    "mail": "Mail",
    "calendar": "Calendar",
    "preview": "Preview",
    "settings": "System Settings",
    "system settings": "System Settings",
    "system preferences": "System Settings",
}

_ACTIONS = {
    "Finder": ("selection", "select_file"),
    "Notes": ("find_notes", "open_note"),
    "Mail": ("find_messages", "open_message"),
    "Calendar": ("find_events", "open_event"),
    "Preview": ("list_documents", "open_document"),
    "System Settings": ("list_panes", "open_pane"),
}

_READ_ACTIONS = {
    "capabilities",
    "selection",
    "find_notes",
    "find_messages",
    "find_events",
    "list_documents",
    "list_panes",
}


class AppAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def normalize_app_name(app: str) -> Optional[str]:
    return _APP_ALIASES.get(str(app or "").strip().lower())


def supported_apps() -> tuple[str, ...]:
    return tuple(_ACTIONS)


def supported_actions(app: str) -> tuple[str, ...]:
    canonical = normalize_app_name(app)
    return _ACTIONS.get(canonical or "", ())


def is_read_action(action: str) -> bool:
    return str(action or "").strip().lower().replace("-", "_") in _READ_ACTIONS


def _normalized_action(action: str) -> str:
    return str(action or "capabilities").strip().lower().replace("-", "_")


def _bounded_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "limit must be an integer.") from exc
    if value < 1 or value > _MAX_LIMIT:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            f"limit must be between 1 and {_MAX_LIMIT}.",
        )
    return value


def _base(app: str, action: str) -> Dict[str, Any]:
    return {
        "adapter": "first_party",
        "adapter_version": _ADAPTER_VERSION,
        "app": app,
        "action": action,
        "supported": True,
        "semantic_tool_calls": 1,
    }


def _fallback(app: str, action: str, reason: str) -> Dict[str, Any]:
    label = app or "frontmost"
    return {
        "ok": False,
        "supported": False,
        "adapter": "generic_ax_fallback",
        "adapter_version": _ADAPTER_VERSION,
        "app": label,
        "action": action,
        "reason_code": "APP_ADAPTER_UNSUPPORTED",
        "error": reason,
        "fallback": {
            "observe": {
                "tool": "mac_observe",
                "arguments": {"app": label, "include_screenshot": False},
            },
            "act_tool": "mac_act",
            "automatic": False,
        },
    }


def _run(script: str, *, timeout_s: float = 10.0, code: str = "APP_ADAPTER_SCRIPT_FAILED") -> str:
    ok, stdout, stderr = _run_osascript(script, timeout_s=max(0.5, min(float(timeout_s), 30.0)))
    if not ok:
        detail = (stderr or stdout or "AppleScript failed").strip()
        raise AppAdapterError(code, detail[:800])
    return (stdout or "").rstrip("\r\n")


def _parse_records(raw: str, fields: tuple[str, ...]) -> list[Dict[str, str]]:
    rows: list[Dict[str, str]] = []
    if not raw:
        return rows
    for record in raw.split(_RS):
        if not record:
            continue
        values = record.split(_US)
        if len(values) != len(fields):
            raise AppAdapterError(
                "APP_ADAPTER_RESPONSE_INVALID",
                f"Expected {len(fields)} fields from semantic adapter, got {len(values)}.",
            )
        rows.append(dict(zip(fields, values)))
    return rows


def _focus_begin(preserve_focus: bool, deadline: float) -> Optional[Dict[str, Any]]:
    if not preserve_focus:
        return None
    context, error = _capture_focus_context(deadline)
    if context is None:
        raise AppAdapterError(
            "FOCUS_SNAPSHOT_FAILED",
            error or "Could not capture current focus before semantic app action.",
        )
    return context


def _app_pid(app: str, deadline: float) -> int:
    script = f'''tell application "System Events"
    if not (exists application process {_apple_string(app)}) then return "0"
    return unix id of application process {_apple_string(app)} as text
end tell'''
    ok, stdout, _ = _run_osascript(script, timeout_s=max(0.5, min(4.0, deadline - time.monotonic())))
    if not ok:
        return 0
    try:
        return int((stdout or "0").strip())
    except ValueError:
        return 0


def _focus_finish(context: Optional[Dict[str, Any]], app: str, deadline: float) -> Dict[str, Any]:
    if context is None:
        return {"requested": False, "status": "not_requested"}
    target_pid = _app_pid(app, deadline)
    if target_pid <= 0:
        return {"requested": True, "status": "target_pid_unavailable", "restored": False}
    decision, error = _post_action_focus_decision(
        context,
        {"pid": target_pid, "window_index": 0},
        deadline,
    )
    if decision == "restore":
        restored, message, exact = _restore_focus_context(context, deadline)
        return {
            "requested": True,
            "status": "restored" if restored else "restore_failed",
            "restored": bool(restored),
            "exact_window": bool(exact),
            "detail": message,
        }
    return {
        "requested": True,
        "status": decision,
        "restored": False,
        **({"detail": error} if error else {}),
    }


def _finder_selection(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to 500
set maxSelected to {limit}
tell application "Finder"
    if (count of windows) is 0 then return "NO_WINDOW"
    set expectedWindowID to id of front window
    set currentFolder to POSIX path of (target of front window as alias)
    set currentViewName to current view of front window as text
end tell
if currentViewName is not "list view" then return "UNSUPPORTED_VIEW" & us & currentViewName & us & currentFolder
set selectedNames to {{}}
tell application "System Events"
    tell process "Finder"
        if (count of windows) is 0 then return "WINDOW_GONE"
        set allItems to entire contents of window 1
        repeat with e in allItems
            set elementRole to ""
            set elementDescription to ""
            try
                set elementRole to role of e as text
                set elementDescription to description of e as text
            end try
            if elementRole is "AXOutline" and elementDescription is "list view" then
                set rowCount to 0
                repeat with rw in rows of e
                    set rowCount to rowCount + 1
                    if rowCount > maxRows then exit repeat
                    set isSelected to false
                    try
                        set isSelected to value of attribute "AXSelected" of rw
                    end try
                    if isSelected then
                        set descendants to {{}}
                        try
                            set descendants to entire contents of rw
                        end try
                        repeat with ch in descendants
                            try
                                if (role of ch as text) is "AXTextField" then
                                    set end of selectedNames to value of ch as text
                                    exit repeat
                                end if
                            end try
                        end repeat
                        if (count of selectedNames) >= maxSelected then exit repeat
                    end if
                end repeat
                exit repeat
            end if
        end repeat
    end tell
end tell
tell application "Finder"
    if (count of windows) is 0 then return "WINDOW_GONE"
    if (id of front window) is not expectedWindowID then return "WINDOW_CHANGED"
end tell
set AppleScript's text item delimiters to rs
set selectedText to selectedNames as text
set AppleScript's text item delimiters to ""
return "OK" & us & currentFolder & us & selectedText'''
    raw = _run(script, timeout_s=timeout_s, code="FINDER_SELECTION_FAILED")
    parts = raw.split(_US, 2)
    status = parts[0] if parts else ""
    if status == "NO_WINDOW":
        return {"ok": True, "folder": None, "selected_count": 0, "selected_paths": [], "truncated": False}
    if status == "UNSUPPORTED_VIEW":
        view = parts[1] if len(parts) > 1 else "unknown"
        raise AppAdapterError(
            "FINDER_STATE_UNSUPPORTED",
            f"Finder semantic selection currently requires list view; current view is {view}.",
            current_view=view,
        )
    if status in {"WINDOW_GONE", "WINDOW_CHANGED"}:
        raise AppAdapterError(
            "FINDER_WINDOW_CHANGED",
            "Finder window changed while semantic selection was being read.",
        )
    if status != "OK" or len(parts) < 2:
        raise AppAdapterError("FINDER_SELECTION_NOT_VERIFIED", "Finder selection response was invalid.")
    folder = parts[1]
    names = [name for name in (parts[2].split(_RS) if len(parts) > 2 and parts[2] else []) if name]
    selected_paths = [str((Path(folder) / name).resolve(strict=False)) for name in names]
    return {
        "ok": True,
        "folder": folder or None,
        "selected_count": len(selected_paths),
        "selected_paths": selected_paths,
        "truncated": len(selected_paths) >= limit,
    }


def _selection(settings: Settings, *, limit: int, timeout_s: float = 8.0) -> Dict[str, Any]:
    del settings
    return _finder_selection(limit=limit, timeout_s=timeout_s)


def _finder_select_file(path: str, *, preserve_focus: bool, timeout_s: float) -> Dict[str, Any]:
    target = Path(str(path or "")).expanduser().resolve(strict=True)
    if not target.is_file():
        raise AppAdapterError("FINDER_PATH_INVALID", "Finder select_file requires an existing regular file.")

    deadline = time.monotonic() + max(3.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    prepare = f'''set us to ASCII character 31
tell application "Finder"
    set parentAlias to (POSIX file {_apple_string(str(target.parent))}) as alias
    set targetWindow to make new Finder window to parentAlias
    set current view of targetWindow to list view
    set index of targetWindow to 1
    delay 0.1
    return (id of targetWindow as text) & us & (POSIX path of (target of targetWindow as alias))
end tell'''
    prepared = _run(
        prepare,
        timeout_s=min(5.0, max(1.0, deadline - time.monotonic())),
        code="FINDER_WINDOW_PREPARE_FAILED",
    ).split(_US)
    if len(prepared) != 2 or not prepared[0].isdigit():
        raise AppAdapterError("FINDER_WINDOW_PREPARE_FAILED", "Finder did not return a stable target window identity.")
    window_id = int(prepared[0])
    folder = str(Path(prepared[1]).resolve(strict=False))
    if Path(folder) != target.parent:
        raise AppAdapterError(
            "FINDER_WINDOW_TARGET_MISMATCH",
            "Finder opened a different folder than the requested file parent.",
            expected_folder=str(target.parent),
            actual_folder=folder,
        )

    select_script = f'''set targetName to {_apple_string(target.name)}
set expectedWindowID to {window_id}
set us to ASCII character 31
set maxRows to 500
tell application "Finder"
    if (count of windows) is 0 then return "WINDOW_GONE"
    if (id of front window) is not expectedWindowID then return "WINDOW_CHANGED"
    if (current view of front window as text) is not "list view" then return "VIEW_CHANGED"
    set currentFolder to POSIX path of (target of front window as alias)
end tell
tell application "System Events"
    tell process "Finder"
        if (count of windows) is 0 then return "WINDOW_GONE"
        set allItems to entire contents of window 1
        repeat with e in allItems
            set elementRole to ""
            set elementDescription to ""
            try
                set elementRole to role of e as text
                set elementDescription to description of e as text
            end try
            if elementRole is "AXOutline" and elementDescription is "list view" then
                set rowCount to 0
                repeat with rw in rows of e
                    set rowCount to rowCount + 1
                    if rowCount > maxRows then return "ROW_BUDGET"
                    set descendants to {{}}
                    try
                        set descendants to entire contents of rw
                    end try
                    repeat with ch in descendants
                        try
                            if (role of ch as text) is "AXTextField" and (value of ch as text) is targetName then
                                set value of attribute "AXSelected" of rw to true
                                delay 0.08
                                set selectedNow to value of attribute "AXSelected" of rw
                                if not selectedNow then return "VERIFY_FAILED"
                                tell application "Finder"
                                    if (count of windows) is 0 then return "WINDOW_GONE"
                                    if (id of front window) is not expectedWindowID then return "WINDOW_CHANGED"
                                    set finalFolder to POSIX path of (target of front window as alias)
                                end tell
                                return "OK" & us & finalFolder
                            end if
                        end try
                    end repeat
                end repeat
                return "NOT_FOUND"
            end if
        end repeat
    end tell
end tell
return "NO_LIST"'''
    raw = _run(
        select_script,
        timeout_s=min(10.0, max(1.0, deadline - time.monotonic())),
        code="FINDER_SELECT_FAILED",
    )
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status in {"WINDOW_GONE", "WINDOW_CHANGED", "VIEW_CHANGED"}:
        raise AppAdapterError(
            "FINDER_WINDOW_CHANGED",
            "Finder target window changed while selecting the requested file.",
            window_id=window_id,
        )
    if status == "ROW_BUDGET":
        raise AppAdapterError(
            "FINDER_ROW_BUDGET_EXCEEDED",
            "Finder list view exceeded the bounded semantic row scan before the requested file was found.",
            max_rows=500,
        )
    if status == "NOT_FOUND":
        raise AppAdapterError(
            "FINDER_ITEM_NOT_FOUND",
            "Finder did not expose the requested file in the dedicated list-view window.",
            path=str(target),
        )
    if status in {"VERIFY_FAILED", "NO_LIST"} or status != "OK":
        raise AppAdapterError(
            "FINDER_SELECTION_NOT_VERIFIED",
            "Finder did not verify the requested row as AXSelected.",
            path=str(target),
        )
    final_folder = str(Path(parts[1]).resolve(strict=False)) if len(parts) > 1 else folder
    if Path(final_folder) != target.parent:
        raise AppAdapterError(
            "FINDER_WINDOW_TARGET_MISMATCH",
            "Finder window navigated while selecting the requested file.",
            expected_folder=str(target.parent),
            actual_folder=final_folder,
        )
    result = {
        "ok": True,
        "verified": True,
        "verification": "AXSelected",
        "path": str(target),
        "folder": str(target.parent),
        "finder_window_id": window_id,
        "selected_count": 1,
    }
    result["focus"] = _focus_finish(focus, "Finder", deadline)
    return result

def _notes_find(query: str, *, exact: bool, limit: int, timeout_s: float) -> Dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes find_notes requires query.")
    predicate = f'name is {_apple_string(q)}' if exact else f'name contains {_apple_string(q)}'
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
set outText to ""
tell application "Notes"
    set matches to every note whose {predicate}
    set rowCount to 0
    repeat with n in matches
        if rowCount >= maxRows then exit repeat
        set folderName to "__MAC_MCP_NONE__"
        try
            set folderName to name of container of n as text
        end try
        set rowText to (id of n as text) & us & (name of n as text) & us & folderName
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="NOTES_FIND_FAILED")
    rows = _parse_records(raw, ("id", "title", "folder"))
    for row in rows:
        if row.get("folder") == "__MAC_MCP_NONE__":
            row["folder"] = None
    return {"ok": True, "count": len(rows), "notes": rows, "truncated": len(rows) >= limit}


def _notes_open(
    item_id: Optional[str], query: Optional[str], *, exact: bool,
    preserve_focus: bool, timeout_s: float,
) -> Dict[str, Any]:
    note_id = str(item_id or "").strip()
    q = str(query or "").strip()
    if not note_id and not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes open_note requires item_id or query.")
    if note_id:
        selector = f'id is {_apple_string(note_id)}'
    else:
        selector = f'name is {_apple_string(q)}' if exact else f'name contains {_apple_string(q)}'
    deadline = time.monotonic() + max(2.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    script = f'''set us to ASCII character 31
tell application "Notes"
    set matches to every note whose {selector}
    set matchCount to count of matches
    if matchCount is not 1 then return "COUNT" & us & (matchCount as text)
    set targetNote to item 1 of matches
    set targetID to id of targetNote as text
    set targetName to name of targetNote as text
    show targetNote
    delay 0.15
    set verified to false
    try
        set verified to (selection is {{targetNote}})
    end try
    if not verified then return "VERIFY_FAILED" & us & targetID & us & targetName
    return "OK" & us & targetID & us & targetName
end tell'''
    raw = _run(script, timeout_s=timeout_s, code="NOTES_OPEN_FAILED")
    parts = raw.split(_US)
    if parts and parts[0] == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError(
            "NOTES_NOTE_NOT_UNIQUE",
            "Notes target was not uniquely identified.",
            match_count=count,
        )
    if not parts or parts[0] != "OK":
        raise AppAdapterError("NOTES_OPEN_NOT_VERIFIED", "Notes did not verify the requested note as selected.")
    result = {"ok": True, "verified": True, "id": parts[1], "title": parts[2]}
    result["focus"] = _focus_finish(focus, "Notes", deadline)
    return result


_MAILBOX_EXPRESSIONS = {
    "inbox": "inbox",
    "sent": "sent mailbox",
    "drafts": "drafts mailbox",
    "junk": "junk mailbox",
    "trash": "trash mailbox",
}


def _mailbox_expr(mailbox: Optional[str]) -> tuple[str, str]:
    key = str(mailbox or "inbox").strip().lower()
    expr = _MAILBOX_EXPRESSIONS.get(key)
    if not expr:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            f"mailbox must be one of: {', '.join(_MAILBOX_EXPRESSIONS)}.",
        )
    return key, expr


def _mail_find(
    query: Optional[str], sender: Optional[str], *, mailbox: Optional[str],
    exact: bool, limit: int, timeout_s: float,
) -> Dict[str, Any]:
    q = str(query or "").strip()
    s = str(sender or "").strip()
    if not q and not s:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail find_messages requires query and/or sender.")
    mailbox_key, mailbox_expr = _mailbox_expr(mailbox)
    predicates: list[str] = []
    if q:
        predicates.append(f'subject is {_apple_string(q)}' if exact else f'subject contains {_apple_string(q)}')
    if s:
        predicates.append(f'sender contains {_apple_string(s)}')
    where = " and ".join(predicates)
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
set outText to ""
tell application "Mail"
    set targetMailbox to {mailbox_expr}
    set matches to every message of targetMailbox whose {where}
    set rowCount to 0
    repeat with m in matches
        if rowCount >= maxRows then exit repeat
        set receivedText to "__MAC_MCP_NONE__"
        try
            set receivedText to date received of m as text
            if receivedText is "" then set receivedText to "__MAC_MCP_NONE__"
        end try
        set rowText to (message id of m as text) & us & (subject of m as text) & us & (sender of m as text) & us & receivedText
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="MAIL_FIND_FAILED")
    rows = _parse_records(raw, ("message_id", "subject", "sender", "date_received"))
    for row in rows:
        if row.get("date_received") == "__MAC_MCP_NONE__":
            row["date_received"] = None
    return {
        "ok": True,
        "mailbox": mailbox_key,
        "count": len(rows),
        "messages": rows,
        "truncated": len(rows) >= limit,
    }


def _mail_open(
    item_id: str, *, mailbox: Optional[str], preserve_focus: bool, timeout_s: float,
) -> Dict[str, Any]:
    message_id = str(item_id or "").strip()
    if not message_id:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail open_message requires item_id from find_messages.")
    mailbox_key, mailbox_expr = _mailbox_expr(mailbox)
    deadline = time.monotonic() + max(2.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    script = f'''set us to ASCII character 31
tell application "Mail"
    set targetMailbox to {mailbox_expr}
    set matches to every message of targetMailbox whose message id is {_apple_string(message_id)}
    set matchCount to count of matches
    if matchCount is not 1 then return "COUNT" & us & (matchCount as text)
    set targetMessage to item 1 of matches
    open targetMessage
    delay 0.1
    return "OK" & us & (message id of targetMessage as text) & us & (subject of targetMessage as text)
end tell'''
    raw = _run(script, timeout_s=timeout_s, code="MAIL_OPEN_FAILED")
    parts = raw.split(_US)
    if parts and parts[0] == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("MAIL_MESSAGE_NOT_UNIQUE", "Mail target was not uniquely identified.", match_count=count)
    if not parts or parts[0] != "OK":
        raise AppAdapterError("MAIL_OPEN_NOT_VERIFIED", "Mail did not accept the semantic open command.")
    result = {
        "ok": True,
        "verified": True,
        "verification": "application_command_accepted",
        "mailbox": mailbox_key,
        "message_id": parts[1],
        "subject": parts[2],
    }
    result["focus"] = _focus_finish(focus, "Mail", deadline)
    return result


def _parse_date_bound(value: Optional[str], *, end: bool) -> datetime:
    if not value:
        now = datetime.now()
        return now + timedelta(days=365 if end else -30)
    text = str(value).strip()
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d")
            if end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            "Calendar date bounds must use YYYY-MM-DD or ISO-8601 local datetime.",
        ) from exc


def _date_setup(name: str, dt: datetime) -> str:
    return f'''set {name} to current date
set year of {name} to {dt.year}
set month of {name} to {dt.month}
set day of {name} to {dt.day}
set hours of {name} to {dt.hour}
set minutes of {name} to {dt.minute}
set seconds of {name} to {dt.second}'''


def _calendar_find(
    query: str, *, date_from: Optional[str], date_to: Optional[str],
    exact: bool, limit: int, timeout_s: float,
) -> Dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Calendar find_events requires query.")
    start = _parse_date_bound(date_from, end=False)
    end = _parse_date_bound(date_to, end=True)
    if end < start:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "date_to must not be before date_from.")
    predicate = f'summary is {_apple_string(q)}' if exact else f'summary contains {_apple_string(q)}'
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
{_date_setup("fromDate", start)}
{_date_setup("toDate", end)}
set outText to ""
set rowCount to 0
tell application "Calendar"
    repeat with c in calendars
        if rowCount >= maxRows then exit repeat
        set matches to every event of c whose start date >= fromDate and start date <= toDate and {predicate}
        repeat with e in matches
            if rowCount >= maxRows then exit repeat
            set startText to start date of e as text
            set endText to end date of e as text
            set rowText to (uid of e as text) & us & (summary of e as text) & us & (name of c as text) & us & startText & us & endText
            if outText is not "" then set outText to outText & rs
            set outText to outText & rowText
            set rowCount to rowCount + 1
        end repeat
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="CALENDAR_FIND_FAILED")
    rows = _parse_records(raw, ("uid", "summary", "calendar", "start", "end"))
    return {
        "ok": True,
        "count": len(rows),
        "events": rows,
        "truncated": len(rows) >= limit,
        "date_from": start.isoformat(timespec="seconds"),
        "date_to": end.isoformat(timespec="seconds"),
    }


def _calendar_open(item_id: str, *, preserve_focus: bool, timeout_s: float) -> Dict[str, Any]:
    uid = str(item_id or "").strip()
    if not uid:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Calendar open_event requires item_id/uid from find_events.")
    deadline = time.monotonic() + max(2.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    script = f'''set us to ASCII character 31
tell application "Calendar"
    set foundEvents to {{}}
    repeat with c in calendars
        set matches to every event of c whose uid is {_apple_string(uid)}
        repeat with e in matches
            set end of foundEvents to e
        end repeat
    end repeat
    set matchCount to count of foundEvents
    if matchCount is not 1 then return "COUNT" & us & (matchCount as text)
    set targetEvent to item 1 of foundEvents
    show targetEvent
    delay 0.1
    return "OK" & us & (uid of targetEvent as text) & us & (summary of targetEvent as text)
end tell'''
    raw = _run(script, timeout_s=timeout_s, code="CALENDAR_OPEN_FAILED")
    parts = raw.split(_US)
    if parts and parts[0] == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("CALENDAR_EVENT_NOT_UNIQUE", "Calendar event was not uniquely identified.", match_count=count)
    if not parts or parts[0] != "OK":
        raise AppAdapterError("CALENDAR_OPEN_NOT_VERIFIED", "Calendar did not accept the semantic show command.")
    result = {
        "ok": True,
        "verified": True,
        "verification": "application_command_accepted",
        "uid": parts[1],
        "summary": parts[2],
    }
    result["focus"] = _focus_finish(focus, "Calendar", deadline)
    return result


def _preview_list(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
set outText to ""
tell application "Preview"
    set rowCount to 0
    repeat with d in documents
        if rowCount >= maxRows then exit repeat
        set docPath to "__MAC_MCP_NONE__"
        try
            set docPath to path of d as text
            if docPath is "" then set docPath to "__MAC_MCP_NONE__"
        end try
        set rowText to (name of d as text) & us & docPath
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="PREVIEW_LIST_FAILED")
    rows = _parse_records(raw, ("name", "path"))
    for row in rows:
        if row.get("path") == "__MAC_MCP_NONE__":
            row["path"] = None
    return {"ok": True, "count": len(rows), "documents": rows, "truncated": len(rows) >= limit}


def _preview_paths(timeout_s: float = 3.0) -> list[str]:
    script = '''set rs to ASCII character 30
set outText to ""
tell application "Preview"
    repeat with d in documents
        set p to ""
        try
            set p to path of d as text
        end try
        if p is not "" then
            if outText is not "" then set outText to outText & rs
            set outText to outText & p
        end if
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="PREVIEW_LIST_FAILED")
    return [part for part in raw.split(_RS) if part]


def _preview_open(path: str, *, preserve_focus: bool, timeout_s: float) -> Dict[str, Any]:
    target = Path(str(path or "")).expanduser().resolve(strict=True)
    if not target.is_file():
        raise AppAdapterError("PREVIEW_PATH_INVALID", "Preview open_document requires an existing regular file.")
    before = target.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    deadline = time.monotonic() + max(2.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    args = ["open"]
    if preserve_focus:
        args.append("-g")
    args.extend(["-a", "Preview", str(target)])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=max(1.0, min(timeout_s, 10.0)))
    except subprocess.TimeoutExpired as exc:
        raise AppAdapterError("PREVIEW_OPEN_TIMEOUT", "Preview open command timed out.") from exc
    if proc.returncode != 0:
        raise AppAdapterError("PREVIEW_OPEN_FAILED", (proc.stderr or "Preview open failed").strip()[:800])
    verified = False
    while time.monotonic() < deadline:
        for candidate in _preview_paths(timeout_s=min(2.0, max(0.5, deadline - time.monotonic()))):
            try:
                if Path(candidate).expanduser().resolve(strict=False) == target:
                    verified = True
                    break
            except Exception:
                continue
        if verified:
            break
        time.sleep(0.1)
    if not verified:
        raise AppAdapterError("PREVIEW_DOCUMENT_NOT_VERIFIED", "Preview did not expose the requested document.")
    after = target.stat()
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if after_identity != before_identity:
        raise AppAdapterError("PREVIEW_FILE_IDENTITY_CHANGED", "The file changed while Preview was opening it.")
    result = {"ok": True, "verified": True, "path": str(target), "name": target.name}
    result["focus"] = _focus_finish(focus, "Preview", deadline)
    return result


def _settings_list(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
set outText to ""
tell application "System Settings"
    set rowCount to 0
    repeat with p in panes
        if rowCount >= maxRows then exit repeat
        set paneName to "__MAC_MCP_NONE__"
        try
            set paneName to name of p as text
            if paneName is "" then set paneName to "__MAC_MCP_NONE__"
        end try
        set rowText to (id of p as text) & us & paneName
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="SETTINGS_LIST_FAILED")
    rows = _parse_records(raw, ("id", "name"))
    for row in rows:
        if row.get("name") == "__MAC_MCP_NONE__":
            row["name"] = ""
    return {"ok": True, "count": len(rows), "panes": rows, "truncated": len(rows) >= limit}


def _settings_open(
    item_id: Optional[str], query: Optional[str], *, exact: bool,
    preserve_focus: bool, timeout_s: float,
) -> Dict[str, Any]:
    pane_id = str(item_id or "").strip()
    q = str(query or "").strip()
    if not pane_id and not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "System Settings open_pane requires item_id or query.")
    selector = (
        f'id is {_apple_string(pane_id)}'
        if pane_id
        else (f'name is {_apple_string(q)}' if exact else f'name contains {_apple_string(q)}')
    )
    deadline = time.monotonic() + max(2.0, min(float(timeout_s), 20.0))
    focus = _focus_begin(preserve_focus, deadline)
    script = f'''set us to ASCII character 31
tell application "System Settings"
    set matches to every pane whose {selector}
    set matchCount to count of matches
    if matchCount is not 1 then return "COUNT" & us & (matchCount as text)
    set targetPane to item 1 of matches
    reveal targetPane
    delay 0.15
    set currentID to ""
    try
        set currentID to id of current pane as text
    end try
    set targetID to id of targetPane as text
    if currentID is not targetID then return "VERIFY_FAILED" & us & targetID & us & currentID
    set targetName to "__MAC_MCP_NONE__"
    try
        set targetName to name of targetPane as text
        if targetName is "" then set targetName to "__MAC_MCP_NONE__"
    end try
    return "OK" & us & targetID & us & targetName
end tell'''
    raw = _run(script, timeout_s=timeout_s, code="SETTINGS_OPEN_FAILED")
    parts = raw.split(_US)
    if parts and parts[0] == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("SETTINGS_PANE_NOT_UNIQUE", "Settings pane was not uniquely identified.", match_count=count)
    if not parts or parts[0] != "OK":
        raise AppAdapterError("SETTINGS_OPEN_NOT_VERIFIED", "System Settings did not verify the requested pane.")
    result = {
        "ok": True,
        "verified": True,
        "id": parts[1],
        "name": "" if parts[2] == "__MAC_MCP_NONE__" else parts[2],
    }
    result["focus"] = _focus_finish(focus, "System Settings", deadline)
    return result


def mac_app(
    settings: Settings,
    *,
    app: str,
    action: str = "capabilities",
    query: Optional[str] = None,
    item_id: Optional[str] = None,
    path: Optional[str] = None,
    mailbox: Optional[str] = None,
    sender: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    exact: bool = False,
    preserve_focus: bool = True,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    canonical = normalize_app_name(app)
    normalized_action = _normalized_action(action)
    if canonical is None:
        return _fallback(str(app or "").strip(), normalized_action, "No first-party adapter is registered for this app.")

    if normalized_action == "capabilities":
        return {
            **_base(canonical, normalized_action),
            "ok": True,
            "actions": list(_ACTIONS[canonical]),
            "generic_fallback": {
                "observe": "mac_observe",
                "act": "mac_act",
                "automatic": False,
            },
        }
    if normalized_action not in _ACTIONS[canonical]:
        return _fallback(
            canonical,
            normalized_action,
            f"{canonical} adapter does not support action '{normalized_action}'.",
        )

    try:
        bounded = _bounded_limit(limit)
        timeout = max(1.0, min(float(timeout_s), 30.0))
        if canonical == "Finder":
            payload = (
                _selection(settings, limit=bounded, timeout_s=timeout)
                if normalized_action == "selection"
                else _finder_select_file(str(path or ""), preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Notes":
            payload = (
                _notes_find(str(query or ""), exact=exact, limit=bounded, timeout_s=timeout)
                if normalized_action == "find_notes"
                else _notes_open(item_id, query, exact=exact, preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Mail":
            payload = (
                _mail_find(query, sender, mailbox=mailbox, exact=exact, limit=bounded, timeout_s=timeout)
                if normalized_action == "find_messages"
                else _mail_open(str(item_id or ""), mailbox=mailbox, preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Calendar":
            payload = (
                _calendar_find(
                    str(query or ""), date_from=date_from, date_to=date_to,
                    exact=exact, limit=bounded, timeout_s=timeout,
                )
                if normalized_action == "find_events"
                else _calendar_open(str(item_id or ""), preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Preview":
            payload = (
                _preview_list(limit=bounded, timeout_s=timeout)
                if normalized_action == "list_documents"
                else _preview_open(str(path or ""), preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "System Settings":
            payload = (
                _settings_list(limit=bounded, timeout_s=timeout)
                if normalized_action == "list_panes"
                else _settings_open(item_id, query, exact=exact, preserve_focus=preserve_focus, timeout_s=timeout)
            )
        else:  # pragma: no cover - canonical registry is closed above.
            return _fallback(canonical, normalized_action, "Adapter implementation is unavailable.")
    except AppAdapterError as exc:
        return {
            **_base(canonical, normalized_action),
            "ok": False,
            "reason_code": exc.code,
            "error": str(exc),
            **exc.extra,
            "fallback": {
                "observe": {
                    "tool": "mac_observe",
                    "arguments": {"app": canonical, "include_screenshot": False},
                },
                "act_tool": "mac_act",
                "automatic": False,
            },
        }
    except (FileNotFoundError, ValueError) as exc:
        return {
            **_base(canonical, normalized_action),
            "ok": False,
            "reason_code": "APP_ADAPTER_ARGUMENT_INVALID",
            "error": str(exc),
        }
    except Exception as exc:
        return {
            **_base(canonical, normalized_action),
            "ok": False,
            "reason_code": "APP_ADAPTER_FAILED",
            "error": str(exc)[:800],
        }

    return {**_base(canonical, normalized_action), **payload}
