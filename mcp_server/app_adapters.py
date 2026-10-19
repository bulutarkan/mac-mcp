from __future__ import annotations

import html
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .security import Settings
from .foreground_guard import current_foreground_authorization
from .send_approval import request_send_approval
from .workspace_arbitration import (
    claim_delegated_resource, delegated_agent_identity, native_app_human_takeover, native_app_resource_id,
)
from .tools_ui import (
    _apple_string,
    _capture_focus_context,
    _post_action_focus_decision,
    _restore_focus_context,
    _run_osascript,
)

from .app_adapter_core import (  # noqa: F401  (re-exported: tests and app_adapters_more use these names)
    _MAILBOX_EXPRESSIONS, _RS, _US, AppAdapterError, _clean_field, _date_setup, _event_datetime, _mailbox_expr,
    _parse_date_bound, _parse_records, _plain_text, _run, _single_line,
)
from . import app_adapters_more as more
from . import app_undo
from . import desktops
_ADAPTER_VERSION = 1
_MAX_LIMIT = 25
_DEFAULT_LIMIT = 10

_APP_ALIASES = {
    "finder": "Finder",
    "notes": "Notes",
    "mail": "Mail",
    "calendar": "Calendar",
    "preview": "Preview",
    "reminders": "Reminders",
    "settings": "System Settings",
    "system settings": "System Settings",
    "system preferences": "System Settings",
    "messages": "Messages",
    "imessage": "Messages",
    "imessages": "Messages",
    "xcode": "Xcode",
    "contacts": "Contacts",
    "address book": "Contacts",
    "shortcuts": "Shortcuts",
    "mission control": "Mission Control",
    "desktops": "Mission Control",
    "desktop": "Mission Control",
    "spaces": "Mission Control",
}

_ACTIONS = {
    "Finder": ("selection", "select_file"),
    "Notes": ("find_notes", "open_note", "create_note", "append_to_note", "update_note", "undo"),
    "Mail": ("find_messages", "open_message", "create_draft", "send_mail", "update_message", "undo"),
    "Calendar": ("find_events", "find_free_time", "plan_day", "open_event", "create_event", "update_event", "undo"),
    "Reminders": ("list_reminders", "create_reminder", "update_reminder", "complete_reminder", "delete_reminder",
                  "undo"),
    "Preview": ("list_documents", "open_document"),
    "System Settings": ("list_panes", "open_pane"),
    "Messages": ("find_chats", "read_messages", "search_messages", "send_message"),
    "Xcode": ("list_workspaces",),
    "Contacts": ("find_contacts",),
    "Shortcuts": ("list_shortcuts", "run_shortcut"),
    "Mission Control": ("list_desktops", "create_desktop", "remove_desktop", "open_on_desktop", "move_to_desktop"),
}

_READ_ACTIONS = {
    "capabilities",
    "selection",
    "find_notes",
    "find_messages",
    "find_events",
    "list_documents",
    "list_panes",
    "list_reminders",
    "find_chats",
    "read_messages",
    "search_messages",
    "list_workspaces",
    "find_free_time",
    "plan_day",
    "find_contacts",
    "list_shortcuts",
    "list_desktops",
}

# Change app data through the app's own scripting model; they never drive the UI,
# so a person using the app's window does not block them.
_DATA_WRITE_ACTIONS = {"create_event", "update_event", "complete_reminder", "create_note", "create_draft",
                       "create_reminder", "append_to_note", "update_reminder", "delete_reminder", "update_note",
                       "update_message", "undo"}
# A shortcut can do anything its actions do, so running one is gated like run_command: the shell
# switch and the permission profile decide, as for any other command (no new approval prompt).
_RUN_ACTIONS = {"run_shortcut"}
_RUN_TIMEOUT_S = 120.0
# Reach other people through the app's scripting model; the person confirms each
# one in a native panel unless they turned that off (send_approval).
_SEND_ACTIONS = {"send_mail", "send_message"}
# Work on desktops the person is not looking at (or Mission Control for a moment) and never
# take focus, so a person using some app's window does not block them.
_DESKTOP_ACTIONS = {"create_desktop", "remove_desktop", "open_on_desktop", "move_to_desktop"}


# Apps people ask for that have no first-party adapter yet (#129). Each entry
# says why and which path is reliable today, so capability discovery never
# implies support that does not exist. Generic AX means mac_observe + mac_act.
_SUPPORT_MATRIX: Dict[str, Dict[str, Any]] = {
    "Messages": {
        "aliases": ("messages", "imessage", "imessages"),
        "bundle_paths": ("/System/Applications/Messages.app",),
        "native_api": "AppleScript dictionary (chats, participants, send); message text is read from the Messages "
                      "database by Mac MCP Messages Reader, which needs Full Disk Access",
        "tasks": {
            "find_conversation": "first_party: find_chats (query matches chat name, participant name or handle)",
            "read_conversation": "first_party: read_messages and search_messages (read-only)",
            "send_message": "first_party: send_message to an existing chat; the person confirms it first by default",
        },
    },
    "Xcode": {
        "aliases": ("xcode",),
        "bundle_paths": ("/Applications/Xcode.app",),
        "native_api": "AppleScript dictionary (workspace documents, schemes, targets); xcodebuild is the stable "
                      "command-line path for builds",
        "tasks": {
            "inspect_project": "first_party: list_workspaces (open projects, schemes, active scheme, targets)",
            "build_or_test": "run_command or start_background_job: xcodebuild build/test",
            "inspect_window": "generic_ax: mac_observe the project window and navigator",
        },
    },
    "Slack": {
        "aliases": ("slack",),
        "bundle_paths": ("/Applications/Slack.app",),
        "native_api": "none (Electron app without a scripting dictionary); slack:// links need team and channel ids",
        "tasks": {
            "find_conversation": "generic_ax: mac_observe the sidebar, then mac_act on the channel or DM",
            "read_conversation": "generic_ax: messages are exposed as AX text in the open channel",
        },
    },
    "Visual Studio Code": {
        "aliases": ("visual studio code", "vscode", "vs code", "code"),
        "bundle_paths": ("/Applications/Visual Studio Code.app",),
        "native_api": "none (Electron app without a scripting dictionary); the `code` CLI opens files and folders "
                      "when installed",
        "tasks": {
            "inspect_editor": "generic_ax: mac_observe the window; editor tabs and panels are AX groups",
            "open_file": "run_command: code -g <path>:<line> when the CLI is installed, else generic_ax",
        },
    },
}


def _matrix_entry(app: str) -> Optional[tuple[str, Dict[str, Any]]]:
    key = str(app or "").strip().lower()
    for name, entry in _SUPPORT_MATRIX.items():
        if key == name.lower() or key in entry["aliases"]:
            return name, entry
    return None


def support_matrix() -> Dict[str, Any]:
    """Which apps have first-party adapters and which rely on generic AX, with the reason."""
    rows: Dict[str, Any] = {name: {"support": "first_party", "actions": list(actions)} for name, actions in _ACTIONS.items()}
    for name, entry in _SUPPORT_MATRIX.items():
        extra = {"native_api": entry["native_api"], "tasks": dict(entry["tasks"])}
        if name in rows:
            # Some tasks are first-party, the rest keep their generic path; when none is left, all are built in.
            partial = any(not how.startswith("first_party") for how in entry["tasks"].values())
            rows[name].update(support="partial_first_party" if partial else "first_party", **extra)
        else:
            rows[name] = {"support": "generic_ax", **extra}
    return rows


_ACTION_WORDS = {"selection": "See what is selected", "list_workspaces": "List open projects",
                 "plan_day": "Plan a day", "find_free_time": "Find free time"}
_HOW_WORDS = {"first_party": "Built in", "generic_ax": "By looking at the window and clicking",
              "run_command": "With a command", "run_command or start_background_job": "With a command"}
_SUPPORT_WORDS = {"first_party": "Built in", "partial_first_party": "Partly built in",
                  "generic_ax": "By looking at the window"}


def _action_words(action: str) -> str:
    return _ACTION_WORDS.get(action) or action.replace("_", " ").capitalize()


def owner_overview() -> List[Dict[str, Any]]:
    """support_matrix() in plain words for Settings › Apps (#262): what agents can do in each app.

    Actions are grouped by what they do, so the limits show: reads change nothing, sends ask the
    person first (unless turned off in Settings › Apps), shortcuts run like commands.
    """
    rows = []
    for name, entry in support_matrix().items():
        actions = entry.get("actions") or []
        groups = {"reads": [a for a in actions if a in _READ_ACTIONS],
                  "changes": [a for a in actions if a in _DATA_WRITE_ACTIONS],
                  "sends": [a for a in actions if a in _SEND_ACTIONS],
                  "runs": [a for a in actions if a in _RUN_ACTIONS]}
        grouped = {a for items in groups.values() for a in items}
        groups["opens"] = [a for a in actions if a not in grouped]
        other_ways = []
        for task, text in (entry.get("tasks") or {}).items():
            how, _, detail = str(text).partition(":")
            if how.strip() == "first_party":
                continue  # already in the action lists above
            other_ways.append({"task": _action_words(task), "how": _HOW_WORDS.get(how.strip(), how.strip()),
                               "detail": detail.strip()})
        rows.append({"name": name, "support": entry["support"], "support_words": _SUPPORT_WORDS[entry["support"]],
                     "actions": list(actions),
                     **{key: [_action_words(a) for a in items] for key, items in groups.items()},
                     "other_ways": other_ways, "native_api": entry.get("native_api")})
    order = {"first_party": 0, "partial_first_party": 1, "generic_ax": 2}
    return sorted(rows, key=lambda row: (order[row["support"]], row["name"]))


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


def _mail_update(
    item_id: Optional[str], *, mailbox: Optional[str], read: Optional[bool], flagged: Optional[bool],
    move_to: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    """Mark a message read or unread, flag or unflag it, or move it to another mailbox of its account (#196)."""
    message_id = str(item_id or "").strip()
    if not message_id:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail update_message requires item_id from find_messages.")
    target = _single_line(move_to, "move_to", 200)
    if read is None and flagged is None and not target:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Pass read, flagged and/or move_to to change.")
    mailbox_key, mailbox_expr = _mailbox_expr(mailbox)
    sel = f"(every message of sourceBox whose message id is {_apple_string(message_id)})"
    lines = [
        "set us to ASCII character 31",
        'tell application "Mail"',
        f"    set sourceBox to {mailbox_expr}",
        f"    set matches to {sel}",
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set m to item 1 of matches",
    ]
    if read is not None:
        lines.append(f"    set read status of m to {'true' if read else 'false'}")
    if flagged is not None:
        lines.append(f"    set flagged status of m to {'true' if flagged else 'false'}")
    lines += ["    set readState to read status of m as text", "    set flagState to flagged status of m as text"]
    if target:
        names = ("Archive", "Arşiv", "All Mail", "Tüm Postalar") if target.casefold() in {"archive", "arşiv"} else (target,)
        lines += [
            "    set acct to account of mailbox of m",
            "    set destination to missing value",
            *[line for name in names for line in (
                "    if destination is missing value then", "        try",
                f"            set destination to mailbox {_apple_string(name)} of acct",
                "        end try", "    end if")],
            # Nested folders ("[Gmail]/All Mail") are not always reachable by name; look through them.
            "    if destination is missing value then",
            "        repeat with b in mailboxes of acct",
            f"            if {{{', '.join(_apple_string(name) for name in names)}}} contains (name of b as text) then set destination to contents of b",
            "        end repeat",
            "    end if",
            '    if destination is missing value then',
            '        set boxNames to ""',
            "        repeat with b in mailboxes of acct",
            '            if boxNames is not "" then set boxNames to boxNames & us',
            "            set boxNames to boxNames & (name of b as text)",
            "        end repeat",
            '        return "NO_MAILBOX" & us & boxNames',
            "    end if",
            "    move m to destination",
            "    set moved to 0",
            "    repeat 20 times",
            f"        set moved to count of (every message of destination whose message id is {_apple_string(message_id)})",
            "        if moved > 0 then exit repeat",
            "        delay 0.25",
            "    end repeat",
            '    if moved is 0 then return "NOT_VERIFIED"',
            '    return "OK" & us & readState & us & flagState & us & (name of destination as text)',
        ]
    else:
        lines.append('    return "OK" & us & readState & us & flagState & us & ""')
    lines.append("end tell")
    try:
        raw = _run("\n".join(lines), timeout_s=max(timeout_s, 10.0), code="MAIL_UPDATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} Check with find_messages before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    if parts[0] == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("MAIL_MESSAGE_NOT_UNIQUE" if count else "MAIL_MESSAGE_NOT_FOUND",
                              f"The message was not found once in {mailbox_key}; nothing was changed.", match_count=count)
    if parts[0] == "NO_MAILBOX":
        raise AppAdapterError("MAIL_MAILBOX_NOT_FOUND", "That account has no such mailbox; nothing was moved.",
                              mailboxes=parts[1:][:50])
    if parts[0] != "OK" or (read is not None and parts[1] != str(read).lower()) \
            or (flagged is not None and parts[2] != str(flagged).lower()):
        raise AppAdapterError("MAIL_UPDATE_NOT_VERIFIED", "Mail did not confirm the change.",
                              outcome_unknown=True, automatic_retry=False)
    out: Dict[str, Any] = {"ok": True, "updated": True, "verified": True, "verification": "read_back",
                           "message_id": message_id, "read": parts[1] == "true", "flagged": parts[2] == "true"}
    if target:
        out["moved_to"] = parts[3]
    return out


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


def _event_readback(event: str, calendar: str) -> str:
    return (
        f"(uid of {event} as text) & us & (summary of {event} as text) & us & (name of {calendar} as text)"
        f" & us & (start date of {event} as text) & us & (end date of {event} as text)"
    )


def _event_row(parts: list[str]) -> Dict[str, str]:
    keys = ("uid", "summary", "calendar", "start", "end")
    return dict(zip(keys, parts[: len(keys)]))


def _calendar_lookup(title: str, start: datetime, calendar: str, timeout_s: float) -> Optional[Dict[str, str]]:
    """Find an event by exact title and start, to settle an uncertain create."""
    calendars = f"calendars whose name is {_apple_string(calendar)}" if calendar else "calendars"
    script = "\n".join([
        "set us to ASCII character 31",
        _date_setup("startDate", start),
        'tell application "Calendar"',
        f"    repeat with c in ({calendars})",
        f"        set matches to every event of c whose summary is {_apple_string(title)} and start date is startDate",
        "        if (count of matches) > 0 then",
        "            set e to item 1 of matches",
        f"            return {_event_readback('e', 'c')}",
        "        end if",
        "    end repeat",
        "end tell",
        'return ""',
    ])
    raw = _run(script, timeout_s=timeout_s, code="CALENDAR_LOOKUP_FAILED")
    return _event_row(raw.split(_US)) if raw else None


_CALENDAR_ERRORS = {
    "NO_CALENDAR": ("CALENDAR_NOT_FOUND", "No calendar has that name."),
    "CALENDAR_NOT_UNIQUE": ("CALENDAR_NOT_UNIQUE", "More than one calendar has that name."),
    "READ_ONLY": ("CALENDAR_READ_ONLY", "That calendar does not accept changes; nothing was changed."),
    "NO_WRITABLE_CALENDAR": ("CALENDAR_NOT_FOUND", "No writable calendar is available."),
}


def _calendar_create(
    title: Optional[str], start: Optional[str], end: Optional[str], *, calendar: Optional[str],
    location: Optional[str], notes: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    summary = _clean_field(title, "title", 300)
    if not summary:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Calendar create_event requires title.")
    start_dt = _event_datetime(start, "start")
    all_day = len(str(start or "").strip()) == 10
    if end:
        end_dt = _event_datetime(end, "end")
    else:
        end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end_dt <= start_dt:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "end must be after start.")
    cal_name = _clean_field(calendar, "calendar", 200)
    place = _clean_field(location, "location", 500)
    body = _clean_field(notes, "notes", 4000)
    if cal_name:
        choose = [
            f"    set calendarMatches to calendars whose name is {_apple_string(cal_name)}",
            '    if (count of calendarMatches) is 0 then return "NO_CALENDAR"',
            '    if (count of calendarMatches) > 1 then return "CALENDAR_NOT_UNIQUE"',
            "    set targetCal to item 1 of calendarMatches",
            '    if not (writable of targetCal) then return "READ_ONLY"',
        ]
    else:
        choose = [
            "    set targetCal to missing value",
            "    repeat with c in calendars",
            "        if writable of c then",
            "            set targetCal to c",
            "            exit repeat",
            "        end if",
            "    end repeat",
            '    if targetCal is missing value then return "NO_WRITABLE_CALENDAR"',
        ]
    props = (
        f"{{summary:{_apple_string(summary)}, start date:startDate, end date:endDate, "
        f"allday event:{'true' if all_day else 'false'}}}"
    )
    lines = [
        "set us to ASCII character 31",
        _date_setup("startDate", start_dt),
        _date_setup("endDate", end_dt),
        'tell application "Calendar"',
        *choose,
        # A replayed request finds the event it already made instead of adding a twin.
        f"    set existing to every event of targetCal whose summary is {_apple_string(summary)} and start date is startDate",
        "    if (count of existing) > 0 then",
        "        set e to item 1 of existing",
        f"        return \"DUPLICATE\" & us & {_event_readback('e', 'targetCal')}",
        "    end if",
        f"    set e to make new event at end of events of targetCal with properties {props}",
    ]
    if place:
        lines.append(f"    set location of e to {_apple_string(place)}")
    if body:
        lines.append(f"    set description of e to {_apple_string(body)}")
    lines += [
        "    set newUid to uid of e",
        "    set readBack to every event of targetCal whose uid is newUid",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED" & us & newUid',
        "    set e to item 1 of readBack",
        f"    return \"OK\" & us & {_event_readback('e', 'targetCal')}",
        "end tell",
    ]
    try:
        raw = _run("\n".join(lines), timeout_s=timeout_s, code="CALENDAR_CREATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        # The event may exist even though the command failed or timed out; look
        # once and report what is known instead of creating it again.
        try:
            found = _calendar_lookup(summary, start_dt, cal_name, timeout_s=min(timeout_s, 8.0))
        except AppAdapterError:
            found = None
        if found:
            return {"ok": True, "created": True, "verified": True, "verification": "lookup_after_error",
                    "event": found, "all_day": all_day}
        raise AppAdapterError(
            exc.code,
            f"{exc} The event may or may not exist; check with find_events before trying again.",
            outcome_unknown=True, automatic_retry=False,
        ) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status in _CALENDAR_ERRORS:
        code, message = _CALENDAR_ERRORS[status]
        raise AppAdapterError(code, message)
    if status == "DUPLICATE":
        return {
            "ok": True, "created": False, "duplicate": True, "verified": True,
            "verification": "existing_event_matched", "event": _event_row(parts[1:]), "all_day": all_day,
            "message": "An event with this title and start already exists; nothing was created.",
        }
    if status != "OK":
        raise AppAdapterError("CALENDAR_CREATE_NOT_VERIFIED", "Calendar did not confirm the new event.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "created": True, "verified": True, "verification": "read_back",
            "event": _event_row(parts[1:]), "all_day": all_day}


def _calendar_update(
    item_id: Optional[str], *, title: Optional[str], start: Optional[str], end: Optional[str],
    location: Optional[str], notes: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    uid = str(item_id or "").strip()
    if not uid:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Calendar update_event requires item_id (the event uid).")
    setup: list[str] = []
    changes: list[str] = []
    changed: Dict[str, Any] = {}
    if title is not None:
        summary = _clean_field(title, "title", 300)
        if not summary:
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "title must not be empty.")
        changes.append(f"    set summary of e to {_apple_string(summary)}")
        changed["title"] = summary
    start_dt = _event_datetime(start, "start") if start is not None else None
    end_dt = _event_datetime(end, "end") if end is not None else None
    if start_dt is not None and end_dt is not None and end_dt <= start_dt:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "end must be after start.")
    if start_dt is not None:
        setup.append(_date_setup("newStart", start_dt))
        changed["start"] = start_dt.isoformat(timespec="minutes")
    if end_dt is not None:
        setup.append(_date_setup("newEnd", end_dt))
        changed["end"] = end_dt.isoformat(timespec="minutes")
    if start_dt is not None and end_dt is not None:
        # Calendar rejects a start after the current end, so widen first.
        changes += ["    set end date of e to newEnd", "    set start date of e to newStart", "    set end date of e to newEnd"]
    elif start_dt is not None:
        changes += [
            "    set eventLength to (end date of e) - (start date of e)",
            "    set end date of e to newStart + eventLength",
            "    set start date of e to newStart",
            "    set end date of e to newStart + eventLength",
        ]
    elif end_dt is not None:
        changes += ['    if newEnd is less than or equal to (start date of e) then return "BAD_RANGE"',
                    "    set end date of e to newEnd"]
    if location is not None:
        changes.append(f"    set location of e to {_apple_string(_clean_field(location, 'location', 500))}")
        changed["location"] = str(location).strip()
    if notes is not None:
        changes.append(f"    set description of e to {_apple_string(_clean_field(notes, 'notes', 4000))}")
        changed["notes"] = True
    if not changes:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                              "update_event needs at least one of title, start, end, location or notes.")
    lines = [
        "set us to ASCII character 31",
        *setup,
        'tell application "Calendar"',
        "    set foundEvents to {}",
        "    set foundCal to missing value",
        "    repeat with c in calendars",
        f"        set matches to every event of c whose uid is {_apple_string(uid)}",
        "        repeat with m in matches",
        "            set end of foundEvents to m",
        "            set foundCal to c",
        "        end repeat",
        "    end repeat",
        '    if (count of foundEvents) is not 1 then return "COUNT" & us & ((count of foundEvents) as text)',
        '    if not (writable of foundCal) then return "READ_ONLY"',
        "    set e to item 1 of foundEvents",
        *changes,
        f"    return \"OK\" & us & {_event_readback('e', 'foundCal')}",
        "end tell",
    ]
    try:
        raw = _run("\n".join(lines), timeout_s=timeout_s, code="CALENDAR_UPDATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(
            exc.code, f"{exc} The event may be partly updated; read it with find_events before trying again.",
            outcome_unknown=True, automatic_retry=False,
        ) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("CALENDAR_EVENT_NOT_UNIQUE" if count else "CALENDAR_EVENT_NOT_FOUND",
                              "Calendar event was not uniquely identified; nothing was changed.", match_count=count)
    if status in _CALENDAR_ERRORS:
        code, message = _CALENDAR_ERRORS[status]
        raise AppAdapterError(code, message)
    if status == "BAD_RANGE":
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "end must be after the event's start; nothing was changed.")
    if status != "OK":
        raise AppAdapterError("CALENDAR_UPDATE_NOT_VERIFIED", "Calendar did not confirm the update.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "updated": True, "verified": True, "verification": "read_back",
            "event": _event_row(parts[1:]), "changed": changed}


_REMINDER_FIELDS = ("id", "name", "list", "due", "completed")


def _reminders_list(
    query: Optional[str], *, list_name: Optional[str], include_completed: bool, limit: int, timeout_s: float,
) -> Dict[str, Any]:
    name_filter = _clean_field(query, "query", 300)
    chosen_list = _clean_field(list_name, "list_name", 200)
    conditions = [] if include_completed else ["completed is false"]
    if name_filter:
        conditions.append(f"name contains {_apple_string(name_filter)}")
    where = (" whose " + " and ".join(conditions)) if conditions else ""
    lists = f"lists whose name is {_apple_string(chosen_list)}" if chosen_list else "lists"
    script = "\n".join([
        "set rs to ASCII character 30",
        "set us to ASCII character 31",
        f"set maxRows to {limit}",
        'set outText to ""',
        "set rowCount to 0",
        'tell application "Reminders"',
        f"    set chosen to {lists}",
        '    if (count of chosen) is 0 then return "NO_LIST"',
        "    repeat with l in chosen",
        "        if rowCount >= maxRows then exit repeat",
        f"        set matches to (every reminder of l{where})",
        "        repeat with r in matches",
        "            if rowCount >= maxRows then exit repeat",
        '            set dueText to ""',
        "            try",
        "                set dueValue to due date of r",
        "                if dueValue is not missing value then set dueText to dueValue as text",
        "            end try",
        "            set rowText to (id of r as text) & us & (name of r as text) & us & (name of l as text) & us & dueText & us & (completed of r as text)",
        '            if outText is not "" then set outText to outText & rs',
        "            set outText to outText & rowText",
        "            set rowCount to rowCount + 1",
        "        end repeat",
        "    end repeat",
        "end tell",
        "return outText",
    ])
    raw = _run(script, timeout_s=timeout_s, code="REMINDERS_LIST_FAILED")
    if raw == "NO_LIST":
        raise AppAdapterError("REMINDERS_LIST_NOT_FOUND", "No Reminders list has that name.")
    rows: list[Dict[str, Any]] = list(_parse_records(raw, _REMINDER_FIELDS))
    for row in rows:
        row["completed"] = row["completed"] == "true"
    return {"ok": True, "count": len(rows), "reminders": rows, "truncated": len(rows) >= limit}


def _reminders_complete(item_id: Optional[str], *, timeout_s: float) -> Dict[str, Any]:
    rid = str(item_id or "").strip()
    if not rid:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                              "Reminders complete_reminder requires item_id from list_reminders.")
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Reminders"',
        f"    set matches to every reminder whose id is {_apple_string(rid)}",
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set r to item 1 of matches",
        '    if completed of r then return "ALREADY" & us & (id of r as text) & us & (name of r as text)',
        "    set completed of r to true",
        f"    set readBack to every reminder whose id is {_apple_string(rid)}",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        '    return "OK" & us & (id of r as text) & us & (name of r as text) & us & ((completed of (item 1 of readBack)) as text)',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="REMINDERS_COMPLETE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} Check with list_reminders before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("REMINDER_NOT_UNIQUE" if count else "REMINDER_NOT_FOUND",
                              "Reminder was not uniquely identified; nothing was changed.", match_count=count)
    if status == "ALREADY":
        return {"ok": True, "completed": True, "changed": False, "verified": True,
                "verification": "already_completed", "reminder": {"id": parts[1], "name": parts[2]}}
    if status != "OK" or parts[-1] != "true":
        raise AppAdapterError("REMINDER_COMPLETE_NOT_VERIFIED", "Reminders did not confirm completion.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "completed": True, "changed": True, "verified": True, "verification": "read_back",
            "reminder": {"id": parts[1], "name": parts[2]}}


def _note_html(title: str, body: str) -> str:
    # Notes names a note after its first line, so the title leads the body.
    lines = [f"<div><h1>{html.escape(title)}</h1></div>"]
    lines += [f"<div>{html.escape(line) if line.strip() else '<br>'}</div>" for line in body.split("\n")] if body else []
    return "".join(lines)


_NOTE_FIELDS = ("id", "title", "folder")
_NOTES_ERRORS = {
    "NO_ACCOUNT": ("NOTES_ACCOUNT_NOT_FOUND", "No Notes account has that name; nothing was created."),
    "NO_FOLDER": ("NOTES_FOLDER_NOT_FOUND", "No Notes folder has that name; nothing was created."),
    "FOLDER_NOT_UNIQUE": ("NOTES_FOLDER_NOT_UNIQUE",
                          "More than one Notes folder has that name; pass account to choose one. Nothing was created."),
}


def _notes_target_folder(folder: str, account: str) -> list[str]:
    lines: list[str] = []
    if account:
        lines += [
            f"    set accountMatches to accounts whose name is {_apple_string(account)}",
            '    if (count of accountMatches) is 0 then return "NO_ACCOUNT"',
            "    set targetAccount to item 1 of accountMatches",
        ]
    else:
        lines.append("    set targetAccount to default account")
    if folder:
        scope = "folders of targetAccount" if account else "folders"
        lines += [
            f"    set folderMatches to {scope} whose name is {_apple_string(folder)}",
            '    if (count of folderMatches) is 0 then return "NO_FOLDER"',
            '    if (count of folderMatches) > 1 then return "FOLDER_NOT_UNIQUE"',
            "    set targetFolder to item 1 of folderMatches",
        ]
    else:
        lines.append("    set targetFolder to default folder of targetAccount")
    return lines


def _note_readback(note: str) -> str:
    return f"(id of {note} as text) & us & (name of {note} as text) & us & (name of targetFolder as text)"


def _notes_create(
    title: Optional[str], body: Optional[str], *, folder: Optional[str], account: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    name = _single_line(title, "title", 300)
    if not name:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes create_note requires title.")
    text = _plain_text(body, "body", 20000)
    folder_name = _single_line(folder, "folder", 200)
    account_name = _single_line(account, "account", 200)
    target = _notes_target_folder(folder_name, account_name)
    lines = [
        "set us to ASCII character 31",
        'tell application "Notes"',
        *target,
        # A replayed request finds the note it already made instead of adding a twin.
        f"    set existing to notes of targetFolder whose name is {_apple_string(name)}",
        "    if (count of existing) > 0 then",
        "        set n to item 1 of existing",
        f"        return \"DUPLICATE\" & us & {_note_readback('n')}",
        "    end if",
        f"    set n to make new note at targetFolder with properties {{body:{_apple_string(_note_html(name, text))}}}",
        "    set newId to id of n",
        "    set readBack to notes of targetFolder whose id is newId",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED" & us & newId',
        "    set n to item 1 of readBack",
        f"    return \"OK\" & us & {_note_readback('n')}",
        "end tell",
    ]
    try:
        raw = _run("\n".join(lines), timeout_s=timeout_s, code="NOTES_CREATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        # The note may exist even though the command failed or timed out; look
        # once and report what is known instead of creating it again.
        lookup = [
            "set us to ASCII character 31",
            'tell application "Notes"',
            *target,
            f"    set existing to notes of targetFolder whose name is {_apple_string(name)}",
            '    if (count of existing) is 0 then return ""',
            "    set n to item 1 of existing",
            f"    return {_note_readback('n')}",
            "end tell",
        ]
        try:
            found = _run("\n".join(lookup), timeout_s=min(timeout_s, 8.0), code="NOTES_LOOKUP_FAILED")
        except AppAdapterError:
            found = ""
        if found and found.split(_US)[0] not in _NOTES_ERRORS:
            return {"ok": True, "created": True, "verified": True, "verification": "lookup_after_error",
                    "note": dict(zip(_NOTE_FIELDS, found.split(_US)))}
        raise AppAdapterError(
            exc.code, f"{exc} The note may or may not exist; check with find_notes before trying again.",
            outcome_unknown=True, automatic_retry=False,
        ) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status in _NOTES_ERRORS:
        code, message = _NOTES_ERRORS[status]
        raise AppAdapterError(code, message)
    if status == "DUPLICATE":
        return {
            "ok": True, "created": False, "duplicate": True, "verified": True,
            "verification": "existing_note_matched", "note": dict(zip(_NOTE_FIELDS, parts[1:])),
            "message": "A note with this title already exists in that folder; nothing was created.",
        }
    if status != "OK":
        raise AppAdapterError("NOTES_CREATE_NOT_VERIFIED", "Notes did not confirm the new note.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "created": True, "verified": True, "verification": "read_back",
            "note": dict(zip(_NOTE_FIELDS, parts[1:]))}


_EMAIL_RE = re.compile(r"^[^@\s<>\",;:()\[\]]+@[^@\s<>\",;:()\[\]]+\.[^@\s<>\",;:()\[\]]+$")
_MAX_RECIPIENTS = 20


def _addresses(value: Optional[str], field: str) -> list[str]:
    items = [item.strip() for item in re.split(r"[,;\n]", str(value or "")) if item.strip()]
    if len(items) > _MAX_RECIPIENTS:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{field} accepts at most {_MAX_RECIPIENTS} addresses.")
    for item in items:
        if len(item) > 254 or not _EMAIL_RE.match(item):
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                                  f"{field} must be plain email addresses separated by commas.")
    return items


# A person's name as written in Contacts: letters first, then letters, spaces, dots, apostrophes, hyphens.
_CONTACT_NAME_RE = re.compile(r"^[^\W\d_][^\W_]*(?:[ .'’\-]+[^\W_]+)*\.?$")


def _recipients(value: Optional[str], field: str, timeout_s: float) -> tuple[list[str], Dict[str, str]]:
    """Addresses, with names looked up in Contacts (#196): a name must lead to exactly one address."""
    items = [item.strip() for item in re.split(r"[,;\n]", str(value or "")) if item.strip()]
    if len(items) > _MAX_RECIPIENTS:  # before any Contacts lookup
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{field} accepts at most {_MAX_RECIPIENTS} addresses.")
    resolved: Dict[str, str] = {}
    out: list[str] = []
    for item in items:
        if "@" in item:
            out.append(item)
            continue
        if not _CONTACT_NAME_RE.match(item):
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                                  f"{field} must be email addresses or contact names separated by commas.")
        found = more.contacts_find(item, limit=10, timeout_s=timeout_s)["contacts"]
        exact = [person for person in found if person["name"].casefold() == item.casefold()]
        people = exact or found
        addresses = sorted({address for person in people for address in person["emails"]})
        if len(people) == 1 and len(addresses) == 1:
            resolved[item] = addresses[0]
            out.append(addresses[0])
            continue
        if not people:
            raise AppAdapterError("MAIL_RECIPIENT_NOT_FOUND", f"No contact matches {item!r}; pass an email address.")
        if not addresses:
            raise AppAdapterError("MAIL_RECIPIENT_NO_ADDRESS", f"{people[0]['name']} has no email address in Contacts.")
        raise AppAdapterError("MAIL_RECIPIENT_AMBIGUOUS",
                              f"{item!r} matches more than one address; pass the one you mean. Nothing was done.",
                              candidates=[{"name": person["name"], "emails": person["emails"]} for person in people][:10])
    return _addresses(", ".join(out), field), resolved


_MAX_ATTACHMENTS = 10
_ATTACHMENT_LIMIT = 25 * 1024 * 1024


def _attachment_paths(values: Optional[List[str]]) -> list[str]:
    paths = [str(Path(str(value)).expanduser()) for value in (values or []) if str(value or "").strip()]
    if len(paths) > _MAX_ATTACHMENTS:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"Attach at most {_MAX_ATTACHMENTS} files.")
    total = 0
    for path in paths:
        target = Path(path)
        if not target.is_file():
            raise AppAdapterError("MAIL_ATTACHMENT_NOT_FOUND", f"{target.name} is not a file that exists; nothing was created.")
        total += target.stat().st_size
    if total > _ATTACHMENT_LIMIT:
        raise AppAdapterError("MAIL_ATTACHMENT_TOO_LARGE", "Attachments must add up to at most 25 MB; nothing was created.")
    return paths


_DRAFT_FIELDS = ("message_id", "subject", "account", "sender")
_MAIL_ERRORS = {
    "NO_ACCOUNT": ("MAIL_ACCOUNT_NOT_FOUND", "No enabled Mail account matches; nothing was created."),
    "ACCOUNT_NOT_UNIQUE": ("MAIL_ACCOUNT_NOT_UNIQUE", "More than one Mail account matches; nothing was created."),
    "NO_ADDRESS": ("MAIL_ACCOUNT_NO_ADDRESS", "That Mail account has no sending address; nothing was created."),
}


def _mail_account_pick(account: Optional[str]) -> tuple[list[str], str]:
    """AppleScript lines that set targetAccount, and the sender address expression."""
    chosen = _single_line(account, "account", 254)
    if chosen:
        pick = [
            "    set accountMatches to {}",
            "    repeat with a in (accounts whose enabled is true)",
            # Mail returns an account's addresses only as a whole list, never item by item.
            "        set accountAddresses to email addresses of a",
            f"        if (name of a is {_apple_string(chosen)}) or (accountAddresses contains {_apple_string(chosen)}) then set end of accountMatches to (contents of a)",
            "    end repeat",
            '    if (count of accountMatches) is 0 then return "NO_ACCOUNT"',
            '    if (count of accountMatches) > 1 then return "ACCOUNT_NOT_UNIQUE"',
            "    set targetAccount to item 1 of accountMatches",
        ]
        return pick, (_apple_string(chosen) if "@" in chosen else "(item 1 of senderAddresses)")
    # With several accounts the sender is never guessed.
    pick = [
        "    set enabledAccounts to accounts whose enabled is true",
        '    if (count of enabledAccounts) is 0 then return "NO_ACCOUNT"',
        "    if (count of enabledAccounts) > 1 then",
        '        set accountNames to ""',
        "        repeat with a in enabledAccounts",
        '            if accountNames is not "" then set accountNames to accountNames & us',
        "            set accountNames to accountNames & (name of a as text)",
        "        end repeat",
        '        return "ACCOUNT_REQUIRED" & us & accountNames',
        "    end if",
        "    set targetAccount to item 1 of enabledAccounts",
    ]
    return pick, "(item 1 of senderAddresses)"


def _mail_create_draft(
    subject: Optional[str], body: Optional[str], *, to: Optional[str], cc: Optional[str],
    account: Optional[str], timeout_s: float, attachments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    title = _single_line(subject, "title", 300)
    if not title:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail create_draft requires title (the subject).")
    text = _plain_text(body, "body", 20000)
    files = _attachment_paths(attachments)
    to_list, resolved = _recipients(to, "to", timeout_s)
    cc_list, resolved_cc = _recipients(cc, "cc", timeout_s)
    resolved.update(resolved_cc)
    pick, address = _mail_account_pick(account)
    readback = "draftId & us & (subject of d as text) & us & (name of targetAccount as text) & us & senderText"
    # Mail gives a just-saved draft its message id a moment later; until then reading it fails. Closing the
    # compose object saves the draft again under a new message id, so the id is read after that (#259 E2E).
    wait_for_id = [
        "    set draftId to missing value",
        "    repeat 40 times",
        "        try",
        "            set draftId to message id of d as text",
        "            exit repeat",
        "        end try",
        "        delay 0.25",
        f"        set again to messages of drafts mailbox whose subject is {_apple_string(title)}",
        "        if (count of again) > 0 then set d to item 1 of again",
        "    end repeat",
        '    if draftId is missing value then return "NOT_VERIFIED"',
    ]
    lines = [
        "set us to ASCII character 31",
        'tell application "Mail"',
        *pick,
        "    set senderAddresses to email addresses of targetAccount",
        '    if (count of senderAddresses) is 0 then return "NO_ADDRESS"',
        f"    set senderText to (full name of targetAccount) & \" <\" & {address} & \">\"",
        # A replayed request finds the draft it already saved instead of adding a twin.
        f"    set existing to messages of drafts mailbox whose subject is {_apple_string(title)}",
        "    if (count of existing) > 0 then",
        "        set d to item 1 of existing",
        *[f"    {line}" for line in wait_for_id],
        f"        return \"DUPLICATE\" & us & {readback}",
        "    end if",
        "    set m to make new outgoing message with properties "
        f"{{subject:{_apple_string(title)}, content:{_apple_string(text)}, sender:senderText, visible:false}}",
        "    tell m",
        *[f"        make new to recipient at end of to recipients with properties {{address:{_apple_string(item)}}}"
          for item in to_list],
        *[f"        make new cc recipient at end of cc recipients with properties {{address:{_apple_string(item)}}}"
          for item in cc_list],
        *[f"        make new attachment with properties {{file name:(POSIX file {_apple_string(path)})}} "
          "at after the last paragraph of content" for path in files],
        "    end tell",
        # Mail adds attachments in the background; give it a moment before saving.
        *(["    delay 1"] if files else []),
        # Saved to Drafts only; this adapter never sends.
        "    save m",
        "    set found to {}",
        "    repeat 40 times",
        f"        set found to messages of drafts mailbox whose subject is {_apple_string(title)}",
        "        if (count of found) > 0 then exit repeat",
        "        delay 0.25",
        "    end repeat",
        '    if (count of found) is 0 then return "NOT_VERIFIED"',
        "    set d to item 1 of found",
        # Mail keeps the hidden compose object until it quits whatever we do; closing
        # with saving yes can never discard the draft that was just saved.
        "    close m saving yes",
        # For a moment the old and the new copy can both be there; wait for the one that stays.
        "    repeat 20 times",
        "        delay 0.25",
        f"        set found to messages of drafts mailbox whose subject is {_apple_string(title)}",
        "        if (count of found) is 1 then exit repeat",
        "    end repeat",
        "    if (count of found) > 0 then set d to item 1 of found",
        *wait_for_id,
        '    set attached to 0',
        "    try",
        "        set attached to count of mail attachments of d",
        "    end try",
        f"    return \"OK\" & us & {readback} & us & (attached as text)",
        "end tell",
    ]
    try:
        raw = _run("\n".join(lines), timeout_s=max(timeout_s, 15.0), code="MAIL_DRAFT_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(
            exc.code, f"{exc} A draft may or may not have been saved (Mail can also save it when it quits); "
            "check with find_messages (mailbox=drafts) before trying again.", outcome_unknown=True, automatic_retry=False,
        ) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status in _MAIL_ERRORS:
        code, message = _MAIL_ERRORS[status]
        raise AppAdapterError(code, message)
    if status == "ACCOUNT_REQUIRED":
        raise AppAdapterError("MAIL_ACCOUNT_REQUIRED",
                              "Several Mail accounts are enabled; pass account (its name or address). Nothing was created.",
                              accounts=parts[1:])
    if status == "DUPLICATE":
        return {
            "ok": True, "created": False, "duplicate": True, "sent": False, "verified": True,
            "verification": "existing_draft_matched", "draft": dict(zip(_DRAFT_FIELDS, parts[1:])),
            "message": "A draft with this subject already exists; nothing was created.",
        }
    if status != "OK":
        raise AppAdapterError("MAIL_DRAFT_NOT_VERIFIED",
                              "Mail did not show the draft in Drafts yet; it may still appear there.",
                              outcome_unknown=True, automatic_retry=False)
    out = {"ok": True, "created": True, "sent": False, "verified": True, "verification": "drafts_mailbox",
           "draft": dict(zip(_DRAFT_FIELDS, parts[1:5])), "to": to_list, "cc": cc_list}
    if resolved:
        out["resolved_from_contacts"] = resolved
    if files:
        attached = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        out["attachments"] = [Path(path).name for path in files]
        out["attachments_verified"] = attached >= len(files)
        if attached < len(files):
            out["message"] = (f"The draft was saved, but Mail shows {attached} of {len(files)} attachments so far; "
                              "open the draft to check before sending.")
    return out


def _send_dry_run() -> bool:
    # Test servers only: everything up to and including the confirmation runs, the send itself does not.
    return os.getenv("MAC_MCP_SEND_DRY_RUN", "").strip() == "1"


def _requester() -> str:
    identity = delegated_agent_identity()
    actor = str((identity or {}).get("actor") or "").strip()
    return f"{actor} through Mac MCP" if actor else "an AI agent through Mac MCP"


def _not_sent(approval: Dict[str, Any]) -> Dict[str, Any]:
    decision = approval.get("decision")
    code = {"cancel": "SEND_CANCELLED", "timeout": "SEND_CONFIRMATION_TIMED_OUT"}.get(
        str(decision), "SEND_CONFIRMATION_UNAVAILABLE")
    return {
        "ok": False, "sent": False, "reason_code": code, "confirmation": approval, "automatic_retry": False,
        "message": {"SEND_CANCELLED": "The person chose not to send it; do not try again unless they ask.",
                    "SEND_CONFIRMATION_TIMED_OUT": "Nobody confirmed in time; nothing was sent."}.get(
            code, "No confirmation could be shown, so nothing was sent."),
    }


def _mail_send(
    settings: Settings, subject: Optional[str], body: Optional[str], *, to: Optional[str], cc: Optional[str],
    account: Optional[str], timeout_s: float,
) -> Dict[str, Any]:
    """Send an email after the person confirms it, then look for it in Sent."""
    title = _single_line(subject, "title", 300)
    if not title:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail send_mail requires title (the subject).")
    text = _plain_text(body, "body", 20000)
    # Names are looked up in Contacts; the confirmation shows the addresses they became.
    to_list, _ = _recipients(to, "to", timeout_s)
    cc_list, _ = _recipients(cc, "cc", timeout_s)
    if not to_list:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Mail send_mail requires at least one to address.")
    pick, address = _mail_account_pick(account)
    head = [
        "set us to ASCII character 31",
        'tell application "Mail"',
        *pick,
        "    set senderAddresses to email addresses of targetAccount",
        '    if (count of senderAddresses) is 0 then return "NO_ADDRESS"',
        f"    set senderText to (full name of targetAccount) & \" <\" & {address} & \">\"",
    ]
    # Read-only: who it would come from, for the person to see before anything exists.
    raw = _run("\n".join(head + ['    return "OK" & us & (name of targetAccount as text) & us & senderText', "end tell"]),
               timeout_s=max(timeout_s, 15.0), code="MAIL_SEND_FAILED")
    parts = raw.split(_US)
    if parts[0] in _MAIL_ERRORS:
        code, message = _MAIL_ERRORS[parts[0]]
        raise AppAdapterError(code, message.replace("created", "sent"))
    if parts[0] == "ACCOUNT_REQUIRED":
        raise AppAdapterError("MAIL_ACCOUNT_REQUIRED",
                              "Several Mail accounts are enabled; pass account (its name or address). Nothing was sent.",
                              accounts=parts[1:])
    account_name, sender = (parts + ["", ""])[1:3]
    approval = request_send_approval(
        settings, app="mail", app_bundle_id="com.apple.mail", title="Send this email?",
        fields=[{"label": "From", "value": sender}, {"label": "To", "value": ", ".join(to_list)},
                {"label": "Cc", "value": ", ".join(cc_list)}, {"label": "Subject", "value": title}],
        body=text, requester=_requester(),
    )
    if not approval["approved"]:
        return {**_not_sent(approval), "to": to_list, "cc": cc_list, "subject": title}
    if _send_dry_run():
        return {"ok": True, "sent": False, "dry_run": True, "confirmation": approval, "to": to_list, "subject": title}
    send = head + [
        "    set m to make new outgoing message with properties "
        f"{{subject:{_apple_string(title)}, content:{_apple_string(text)}, sender:senderText, visible:false}}",
        "    tell m",
        *[f"        make new to recipient at end of to recipients with properties {{address:{_apple_string(item)}}}"
          for item in to_list],
        *[f"        make new cc recipient at end of cc recipients with properties {{address:{_apple_string(item)}}}"
          for item in cc_list],
        "    end tell",
        "    if (send m) then return \"SENT\"",
        '    return "NOT_SENT"',
        "end tell",
    ]
    try:
        status = _run("\n".join(send), timeout_s=max(timeout_s, 20.0), code="MAIL_SEND_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The email may or may not have gone out; check Sent before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    if status != "SENT":
        raise AppAdapterError("MAIL_SEND_REFUSED", "Mail did not accept the message for sending; nothing was sent.")
    # Mail sends in the background; look for it in Sent for a short while.
    check = "\n".join([
        'tell application "Mail"',
        "    set cutoff to (current date) - 300",
        f"    return count of (messages of sent mailbox whose subject is {_apple_string(title)} and date sent > cutoff)",
        "end tell",
    ])
    in_sent = False
    for _ in range(8):
        try:
            in_sent = int(_run(check, timeout_s=10, code="MAIL_SENT_CHECK_FAILED") or 0) > 0
        except (AppAdapterError, ValueError):
            in_sent = False
        if in_sent:
            break
        time.sleep(1.5)
    return {
        "ok": True, "sent": True, "verified": in_sent, "verification": "sent_mailbox" if in_sent else "accepted_by_mail",
        "confirmation": approval, "account": account_name, "from": sender, "to": to_list, "cc": cc_list, "subject": title,
        "automatic_retry": False,
    }


def _messages_send(
    settings: Settings, chat_id: Optional[str], query: Optional[str], body: Optional[str], *, timeout_s: float,
) -> Dict[str, Any]:
    """Send a message to one existing conversation after the person confirms it."""
    text = _plain_text(body, "body", 5000).strip()
    if not text:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Messages send_message requires body (the message text).")
    wanted = str(chat_id or "").strip()
    if not wanted and not str(query or "").strip():
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                              "Pass item_id (a chat_id from find_chats) or a query that matches exactly one chat.")
    found = _messages_find_chats(None if wanted else query, limit=_MAX_LIMIT, timeout_s=timeout_s)
    chats = [chat for chat in found["chats"] if chat["chat_id"] == wanted] if wanted else found["chats"]
    if not chats:
        raise AppAdapterError("MESSAGES_CHAT_NOT_FOUND", "No conversation matches; nothing was sent. Use find_chats.")
    if len(chats) > 1 or (not wanted and found["truncated"]):
        raise AppAdapterError("MESSAGES_CHAT_AMBIGUOUS",
                              "More than one conversation matches; pass item_id from find_chats. Nothing was sent.",
                              candidates=chats[:10])
    chat = chats[0]
    approval = request_send_approval(
        settings, app="messages", app_bundle_id="com.apple.MobileSMS", title="Send this message?",
        fields=[{"label": "To", "value": chat["name"]},
                {"label": "Address", "value": ", ".join(chat["handles"])}],
        body=text, requester=_requester(),
    )
    if not approval["approved"]:
        return {**_not_sent(approval), "chat": chat}
    if _send_dry_run():
        return {"ok": True, "sent": False, "dry_run": True, "confirmation": approval, "chat": chat}
    script = "\n".join([
        f'if application "Messages" is not running then return "{_NOT_RUNNING}"',
        'tell application "Messages"',
        f"    send {_apple_string(text)} to chat id {_apple_string(chat['chat_id'])}",
        "end tell",
        'return "SENT"',
    ])
    try:
        status = _run(script, timeout_s=max(timeout_s, 15.0), code="MESSAGES_SEND_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The message may or may not have gone out; check the conversation "
                              "with mac_observe before trying again.", outcome_unknown=True, automatic_retry=False) from exc
    if status == _NOT_RUNNING:
        raise _not_running("Messages", "use mac_observe once it is open")
    return {
        "ok": True, "sent": True, "verified": False, "verification": "accepted_by_messages",
        "confirmation": approval, "chat": chat, "automatic_retry": False,
        "note": "Messages scripting cannot read the conversation back; look at it with mac_observe to confirm delivery.",
    }


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


_NOT_RUNNING = "__MAC_MCP_NOT_RUNNING__"


def _not_running(app: str, alternative: str) -> AppAdapterError:
    # Reading never launches the app: opening it is a visible side effect the caller should choose.
    return AppAdapterError(
        "APP_NOT_RUNNING", f"{app} is not running; open it first (open_app) or {alternative}.", retryable=False,
    )


def _messages_find_chats(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    """Locate conversations by chat name, participant name or handle. Message text is not exposed."""
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
if application "Messages" is not running then return "{_NOT_RUNNING}"
tell application "Messages"
    set chatIds to id of every chat
    set chatNames to name of every chat
    set peopleNames to name of participants of every chat
    set peopleHandles to handle of participants of every chat
end tell
set AppleScript's text item delimiters to "; "
set outText to ""
repeat with i from 1 to count of chatIds
    set rowText to (item i of chatIds as text) & us & (item i of chatNames as text) & us & ¬
        ((item i of peopleNames) as text) & us & ((item i of peopleHandles) as text)
    if outText is not "" then set outText to outText & rs
    set outText to outText & rowText
end repeat
set AppleScript's text item delimiters to ""
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="MESSAGES_LIST_FAILED")
    if raw == _NOT_RUNNING:
        # Closed Messages is not opened: its database lists the chats when Messages Reader may read it.
        from . import messages_reader, privacy_policy

        try:
            privacy_policy.check_app("Messages", "com.apple.MobileSMS")
            return messages_reader.list_chats(query, limit=limit)
        except (messages_reader.MessagesError, privacy_policy.PrivacyBlocked):
            raise _not_running("Messages", "use mac_observe on its window once it is open") from None
    needle = " ".join(str(query or "").split()).lower()
    chats = []
    for row in _parse_records(raw, ("chat_id", "name", "participants", "handles")):
        name = "" if row["name"] == "missing value" else row["name"]
        people = [item for item in row["participants"].split("; ") if item and item != "missing value"]
        handles = [item for item in row["handles"].split("; ") if item and item != "missing value"]
        haystack = " ".join([name, *people, *handles]).lower()
        if needle and needle not in haystack:
            continue
        chats.append({"chat_id": row["chat_id"], "name": name or ", ".join(people or handles),
                      "participants": people, "handles": handles})
    return {
        "ok": True, "count": len(chats[:limit]), "matched": len(chats), "chats": chats[:limit],
        "truncated": len(chats) > limit,
        "note": "Read a chat with read_messages (item_id = chat_id) or search with search_messages.",
    }


def _messages_read(action: str, *, item_id: Optional[str], query: Optional[str], limit: int,
                   cursor: Optional[str], date_from: Optional[str], date_to: Optional[str]) -> Dict[str, Any]:
    """Message text from the Messages database, read-only (#261); see messages_reader."""
    from . import messages_reader, privacy_policy

    # The owner's exclusions hold here as for looking at the Messages window (Settings › Privacy).
    try:
        privacy_policy.check_app("Messages", "com.apple.MobileSMS")
    except privacy_policy.PrivacyBlocked as exc:
        raise AppAdapterError(exc.code, str(exc.detail), retryable=False) from exc
    try:
        if action == "read_messages":
            return messages_reader.read_messages(item_id, query, limit=limit, cursor=cursor,
                                                 date_from=date_from, date_to=date_to)
        return messages_reader.search_messages(str(query or ""), item_id=item_id, limit=limit, cursor=cursor,
                                               date_from=date_from, date_to=date_to)
    except messages_reader.MessagesError as exc:
        raise AppAdapterError(exc.code, str(exc), **exc.extra) from exc


def _xcode_list_workspaces(*, limit: int, timeout_s: float) -> Dict[str, Any]:
    """Open Xcode projects and workspaces with their schemes and targets."""
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
if application "Xcode" is not running then return "{_NOT_RUNNING}"
set AppleScript's text item delimiters to "; "
set outText to ""
tell application "Xcode"
    set activeName to ""
    try
        set activeName to name of active workspace document
    end try
    set rowCount to 0
    repeat with d in workspace documents
        if rowCount ≥ {limit} then exit repeat
        set docPath to ""
        set schemeNames to ""
        set activeScheme to ""
        set targetNames to ""
        set isLoaded to "false"
        set isActive to "false"
        if (name of d as text) is activeName then set isActive to "true"
        try
            set docPath to path of d as text
        end try
        try
            set isLoaded to loaded of d as text
        end try
        try
            set schemeNames to (name of every scheme of d) as text
        end try
        try
            set activeScheme to name of active scheme of d
        end try
        try
            repeat with proj in projects of d
                if targetNames is not "" then set targetNames to targetNames & "; "
                set targetNames to targetNames & ((name of every target of proj) as text)
            end repeat
        end try
        set rowText to (name of d as text) & us & docPath & us & isLoaded & us & ¬
            isActive & us & schemeNames & us & activeScheme & us & targetNames
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
set AppleScript's text item delimiters to ""
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="XCODE_LIST_FAILED")
    if raw == _NOT_RUNNING:
        raise _not_running("Xcode", "inspect the project with run_command: xcodebuild -list")
    split = lambda text: [item for item in text.split("; ") if item]  # noqa: E731
    workspaces = [
        {"name": row["name"], "path": row["path"] or None, "loaded": row["loaded"] == "true",
         "active": row["active"] == "true", "schemes": split(row["schemes"]),
         "active_scheme": row["active_scheme"] or None, "targets": split(row["targets"])}
        for row in _parse_records(raw, ("name", "path", "loaded", "active", "schemes", "active_scheme", "targets"))
    ]
    return {"ok": True, "count": len(workspaces), "workspaces": workspaces, "truncated": len(workspaces) >= limit}


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


def _mission_control(
    action: str, *, item_id: Optional[str], target_app: Optional[str], path: Optional[str],
    url: Optional[str], window_ids: Optional[List[int]],
) -> Dict[str, Any]:
    """Desktops for an agent the owner asked to work apart; desktops.py does the work."""
    def space_id() -> int:
        try:
            return int(str(item_id or "").strip())
        except ValueError:
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                                  f"{action} requires item_id = a desktop id from list_desktops.") from None

    if action == "list_desktops":
        result = desktops.list_desktops()
    elif action == "create_desktop":
        display = str(item_id or "").strip()
        if not display:
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                                  "create_desktop requires item_id = a display id from list_desktops.")
        result = desktops.create_desktop(display)
    elif action == "remove_desktop":
        result = desktops.remove_desktop(space_id())
    elif action == "open_on_desktop":
        if not str(target_app or "").strip():
            raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                                  "open_on_desktop requires target_app (an app name or bundle id).")
        targets = [str(Path(path).expanduser()) if path else "", str(url or "")]
        result = desktops.open_on_desktop(space_id(), str(target_app), [item for item in targets if item])
    else:
        result = desktops.move_windows(space_id(), window_ids or [])
    if not result.get("ok"):
        extra = {key: value for key, value in result.items() if key not in {"ok", "error", "reason_code", "message"}}
        # Chrome's companion explains a failure in "message"; keep it as the error text.
        error = result.get("error") or result.get("message") or "The desktop change did not happen."
        if result.get("message") and result.get("error") and result["message"] != result["error"]:
            extra["detail"] = result["message"]
        raise AppAdapterError(str(result.get("reason_code") or "DESKTOPS_FAILED"), str(error), **extra)
    # Every change is read back by the helper before it answers ok.
    return {**result, "ok": True, **({} if action == "list_desktops" else {"verified": True})}


def _undo_prepare(app: str, action: str, **kwargs: Any) -> Optional[app_undo.Plan]:
    """The old state of what this action changes (#259). Recording never stops a change."""
    try:
        return app_undo.prepare(app, action, **kwargs)
    except Exception as exc:  # an unexpected failure only costs the undo record
        return app_undo.Plan(app, action, reason=f"Its old state could not be read ({type(exc).__name__}).")


def _undo_record(plan: Optional[app_undo.Plan], payload: Dict[str, Any], *, timeout_s: float) -> Optional[Dict[str, Any]]:
    try:
        return app_undo.record(plan, payload, timeout_s=timeout_s)
    except Exception as exc:
        return {"undoable": False, "reason": f"The undo record could not be kept ({type(exc).__name__})."}


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
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    calendar: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    list_name: Optional[str] = None,
    include_completed: bool = False,
    body: Optional[str] = None,
    folder: Optional[str] = None,
    account: Optional[str] = None,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    duration_min: Optional[int] = None,
    hours: Optional[str] = None,
    include_weekends: bool = False,
    buffer_min: Optional[int] = None,
    read: Optional[bool] = None,
    flagged: Optional[bool] = None,
    move_to: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    target_app: Optional[str] = None,
    url: Optional[str] = None,
    window_ids: Optional[List[int]] = None,
    cursor: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    canonical = normalize_app_name(app)
    normalized_action = _normalized_action(action)
    if canonical is None and str(app or "").strip().lower() in {"all", "*"} and normalized_action == "capabilities":
        return {"ok": True, "adapter_version": _ADAPTER_VERSION, "action": normalized_action, "apps": support_matrix()}
    if canonical is None:
        listed = _matrix_entry(app)
        if listed is not None:
            name, entry = listed
            if normalized_action == "capabilities":
                return {
                    "ok": True, "supported": False, "adapter": "generic_ax_fallback",
                    "adapter_version": _ADAPTER_VERSION, "app": name, "action": normalized_action,
                    "installed": any(Path(path).exists() for path in entry["bundle_paths"]),
                    "actions": [], "native_api": entry["native_api"], "tasks": dict(entry["tasks"]),
                    "generic_fallback": {"observe": "mac_observe", "act": "mac_act", "automatic": False},
                }
            unsupported = _fallback(name, normalized_action, f"{name} has no first-party adapter; see tasks for the reliable path.")
            unsupported["tasks"] = dict(entry["tasks"])
            return unsupported
        return _fallback(str(app or "").strip(), normalized_action, "No first-party adapter is registered for this app.")

    if normalized_action == "capabilities":
        listed = _SUPPORT_MATRIX.get(canonical)
        return {
            **_base(canonical, normalized_action),
            "ok": True,
            "actions": list(_ACTIONS[canonical]),
            **({"native_api": listed["native_api"], "tasks": dict(listed["tasks"])} if listed else {}),
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

    # With running commands off, only shortcuts the owner listed in Settings › Apps can run (#195).
    if normalized_action in _RUN_ACTIONS and not settings.allow_shell and not more.owner_allowed_shortcuts():
        return {
            **_base(canonical, normalized_action), "ok": False, "reason_code": "SHELL_DISABLED",
            "error": "Running shortcuts is off because running commands is off (MCP_ALLOW_SHELL=false) and no "
                     "shortcut is allowed in Settings › Apps.",
        }

    if normalized_action not in _READ_ACTIONS | _DATA_WRITE_ACTIONS | _SEND_ACTIONS | _RUN_ACTIONS | _DESKTOP_ACTIONS:
        human_guard = native_app_human_takeover(canonical)
        if human_guard is not None:
            reason_code = str(
                human_guard.get("reason_code") or "HUMAN_ACTIVE_RESOURCE"
            )
            return {
                **_base(canonical, normalized_action),
                "ok": False,
                "reason_code": reason_code,
                "error": reason_code.lower(),
                "retryable": True,
                "retry_after_ms": 750,
                "human_priority": True,
                "yielded": True,
                "resource_kind": "native_app",
                "message": (
                    "The user currently owns this visible native app. "
                    "The delegated agent yielded instead of changing its UI."
                    if reason_code == "HUMAN_ACTIVE_RESOURCE"
                    else
                    "Mac MCP could not prove this native app is free of human ownership; "
                    "the delegated agent yielded fail-closed."
                ),
            }

    foreground_grant = current_foreground_authorization()
    if (
        normalized_action not in _READ_ACTIONS
        and preserve_focus is False
        and foreground_grant is None
    ):
        return {
            **_base(canonical, normalized_action),
            "ok": False,
            "reason_code": "FOREGROUND_REQUIRED",
            "error": "foreground_required",
            "foreground_required": True,
            "retryable": False,
            "message": (
                "Leaving a native app adapter action in the foreground requires an explicit "
                "trusted local-user foreground capability. A model-visible preserve_focus=false "
                "parameter cannot authorize focus changes."
            ),
        }

    if normalized_action not in _READ_ACTIONS:
        arbitration = claim_delegated_resource(
            "native_app",
            native_app_resource_id(app=canonical),
            mode="write",
        )
        if arbitration is not None and not arbitration.get("ok"):
            reason_code = str(
                arbitration.get("reason_code") or "RESOURCE_BUSY"
            )
            return {
                **_base(canonical, normalized_action),
                "ok": False,
                "reason_code": reason_code,
                "error": reason_code.lower(),
                "retryable": bool(arbitration.get("retryable", True)),
                "retry_after_ms": 750,
                "yielded": True,
                "resource_kind": "native_app",
                "message": (
                    "Another agent owns this native app resource. "
                    "The delegated adapter action yielded before mutation."
                ),
            }

    try:
        bounded = _bounded_limit(limit)
        timeout = max(1.0, min(float(timeout_s), _RUN_TIMEOUT_S if normalized_action in _RUN_ACTIONS else 30.0))
        if normalized_action in {"find_free_time", "plan_day"}:
            # Calendar can take 20+ seconds to list a week across many calendars; these only read, so they wait.
            timeout = 30.0
        undo_plan: Optional[app_undo.Plan] = None
        if normalized_action != "undo":
            undo_plan = _undo_prepare(canonical, normalized_action, item_id=item_id, query=query, exact=exact,
                                      mailbox=mailbox, timeout_s=timeout)
        if normalized_action == "undo":
            payload = app_undo.undo(str(item_id or ""), app=canonical, force=bool(force), timeout_s=timeout)
        elif canonical == "Finder":
            payload = (
                _selection(settings, limit=bounded, timeout_s=timeout)
                if normalized_action == "selection"
                else _finder_select_file(str(path or ""), preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Notes":
            if normalized_action == "find_notes":
                payload = _notes_find(str(query or ""), exact=exact, limit=bounded, timeout_s=timeout)
            elif normalized_action == "create_note":
                payload = _notes_create(title, body, folder=folder, account=account, timeout_s=timeout)
            elif normalized_action == "append_to_note":
                payload = more.notes_append(item_id, query, body, exact=exact, timeout_s=timeout)
            elif normalized_action == "update_note":
                payload = more.notes_update(item_id, title=title, body=body, timeout_s=timeout)
            else:
                payload = _notes_open(item_id, query, exact=exact, preserve_focus=preserve_focus, timeout_s=timeout)
        elif canonical == "Mail":
            if normalized_action == "find_messages":
                payload = _mail_find(query, sender, mailbox=mailbox, exact=exact, limit=bounded, timeout_s=timeout)
            elif normalized_action == "create_draft":
                payload = _mail_create_draft(title, body, to=to, cc=cc, account=account, timeout_s=timeout,
                                             attachments=attachments)
            elif normalized_action == "update_message":
                payload = _mail_update(item_id, mailbox=mailbox, read=read, flagged=flagged, move_to=move_to,
                                       timeout_s=timeout)
            elif normalized_action == "send_mail":
                payload = _mail_send(settings, title, body, to=to, cc=cc, account=account, timeout_s=timeout)
            else:
                payload = _mail_open(str(item_id or ""), mailbox=mailbox, preserve_focus=preserve_focus, timeout_s=timeout)
        elif canonical == "Calendar":
            if normalized_action == "find_events":
                payload = _calendar_find(
                    str(query or ""), date_from=date_from, date_to=date_to,
                    exact=exact, limit=bounded, timeout_s=timeout,
                )
            elif normalized_action == "find_free_time":
                payload = more.calendar_free_time(
                    date_from=date_from, date_to=date_to, calendar=calendar, duration_min=duration_min,
                    hours=hours, include_weekends=include_weekends, limit=bounded, timeout_s=timeout,
                    buffer_min=buffer_min,
                )
            elif normalized_action == "plan_day":
                payload = more.plan_day(date_from=date_from, calendar=calendar, duration_min=duration_min,
                                        hours=hours, buffer_min=buffer_min, limit=bounded, timeout_s=timeout)
            elif normalized_action == "create_event":
                payload = _calendar_create(
                    title, start, end, calendar=calendar, location=location, notes=notes, timeout_s=timeout,
                )
            elif normalized_action == "update_event":
                payload = _calendar_update(
                    item_id, title=title, start=start, end=end, location=location, notes=notes, timeout_s=timeout,
                )
            else:
                payload = _calendar_open(str(item_id or ""), preserve_focus=preserve_focus, timeout_s=timeout)
        elif canonical == "Reminders":
            if normalized_action == "list_reminders":
                payload = _reminders_list(query, list_name=list_name, include_completed=include_completed,
                                          limit=bounded, timeout_s=timeout)
            elif normalized_action == "create_reminder":
                payload = more.reminders_create(title, list_name=list_name, due=start, notes=notes or body,
                                                timeout_s=timeout)
            elif normalized_action == "update_reminder":
                payload = more.reminders_update(item_id, title=title, due=start,
                                                notes=notes if notes is not None else body, timeout_s=timeout)
            elif normalized_action == "delete_reminder":
                payload = more.reminders_delete(item_id, timeout_s=timeout)
            else:
                payload = _reminders_complete(item_id, timeout_s=timeout)
        elif canonical == "Contacts":
            payload = more.contacts_find(query, limit=bounded, timeout_s=timeout)
        elif canonical == "Shortcuts":
            payload = (
                more.shortcuts_list(query, limit=bounded, timeout_s=timeout)
                if normalized_action == "list_shortcuts"
                else more.shortcuts_run(item_id, title, body, timeout_s=timeout, allowed_only=not settings.allow_shell)
            )
        elif canonical == "Preview":
            payload = (
                _preview_list(limit=bounded, timeout_s=timeout)
                if normalized_action == "list_documents"
                else _preview_open(str(path or ""), preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Messages":
            if normalized_action == "send_message":
                payload = _messages_send(settings, item_id, query, body, timeout_s=timeout)
            elif normalized_action in {"read_messages", "search_messages"}:
                payload = _messages_read(normalized_action, item_id=item_id, query=query, limit=bounded,
                                         cursor=cursor, date_from=date_from, date_to=date_to)
            else:
                payload = _messages_find_chats(query, limit=bounded, timeout_s=timeout)
        elif canonical == "Xcode":
            payload = _xcode_list_workspaces(limit=bounded, timeout_s=timeout)
        elif canonical == "System Settings":
            payload = (
                _settings_list(limit=bounded, timeout_s=timeout)
                if normalized_action == "list_panes"
                else _settings_open(item_id, query, exact=exact, preserve_focus=preserve_focus, timeout_s=timeout)
            )
        elif canonical == "Mission Control":
            payload = _mission_control(
                normalized_action, item_id=item_id, target_app=target_app, path=path, url=url, window_ids=window_ids,
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

    result = {**_base(canonical, normalized_action), **payload}
    undo_info = _undo_record(undo_plan, payload, timeout_s=timeout)
    if undo_info is not None:
        result["undo"] = undo_info
    if foreground_grant is not None and preserve_focus is False and normalized_action not in _READ_ACTIONS:
        result["foreground_authorization_source"] = foreground_grant.source
    return result
