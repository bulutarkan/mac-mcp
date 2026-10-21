"""An undo trail for what mac_app changes in Notes, Reminders, Calendar, Mail and Contacts (#259, #43).

Before a supported change, the item's whole state is read through the app's scripting model and kept
in an owner-only record; right after it, the new state is read and fingerprinted. Undo puts the old
state back only while the item still looks exactly as Mac MCP left it, so an edit the owner (or another
agent) made since is never overwritten; force=true overrides that check. An item Mac MCP created is
removed again; a deleted reminder is made again, with a new id. Sends and shortcut runs cannot be
undone and say so. Records expire like file transactions (7 days, 200 records).

Nothing here asks the person anything: recording never blocks a change, and a change whose old
state could not be read still runs, marked as not undoable with the reason.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import paths
from .app_adapter_core import _US, AppAdapterError, _date_setup, _mailbox_expr, _run
from .private_storage import PRIVATE_FILE_MODE, ensure_private_dir
from .tools_ui import _apple_string

RETENTION_S = 7 * 24 * 3600
MAX_RECORDS = 200
MAX_STATE_CHARS = 400_000  # a note bigger than this is changed but not recorded
_LOCK = threading.Lock()

# What each action leaves behind and how it is put back.
#   restore: the item still exists; its old fields are set again.
#   delete_created: the action made the item; undo removes it.
#   recreate: the action deleted the item; undo makes it again (new id).
_PLANS = {
    ("Notes", "update_note"): "restore", ("Notes", "append_to_note"): "restore",
    ("Notes", "create_note"): "delete_created",
    ("Reminders", "update_reminder"): "restore", ("Reminders", "complete_reminder"): "restore",
    ("Reminders", "create_reminder"): "delete_created", ("Reminders", "delete_reminder"): "recreate",
    ("Calendar", "update_event"): "restore", ("Calendar", "create_event"): "delete_created",
    ("Mail", "update_message"): "restore", ("Mail", "create_draft"): "delete_created",
    ("Contacts", "update_contact"): "restore", ("Contacts", "create_contact"): "delete_created",
}
_IRREVERSIBLE = {
    "send_mail": "A sent email cannot be called back.",
    "send_message": "A sent message cannot be called back.",
    "run_shortcut": "A shortcut does whatever its own actions do; Mac MCP cannot know how to reverse them.",
}
UNDO_HINT = "mac_app action=undo item_id=<undo_id> (same app) puts it back while the item is unchanged since."


class UndoError(AppAdapterError):
    pass


def enabled() -> bool:
    """MAC_MCP_APP_UNDO=0 turns recording off (the tests' fake apps do; so can the owner)."""
    return os.getenv("MAC_MCP_APP_UNDO", "1").strip().lower() not in {"0", "false", "off", "no"}


def undoable_actions() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for app, action in _PLANS:
        out.setdefault(app, []).append(action)
    return out


# ── Records ──────────────────────────────────────────────────────────────────

def _root() -> Path:
    return paths.state_dir() / "app-undo"


def _record_path(undo_id: str) -> Path:
    if not undo_id.startswith("appundo_") or not undo_id[8:].isalnum() or len(undo_id) > 64:
        raise UndoError("UNDO_NOT_FOUND", "That is not an undo id from a mac_app result.")
    return _root() / f"{undo_id}.json"


def _write(record: Dict[str, Any]) -> None:
    root = ensure_private_dir(_root())
    path = root / f"{record['undo_id']}.json"
    tmp = root / f".{record['undo_id']}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _read(undo_id: str) -> Dict[str, Any]:
    try:
        return json.loads(_record_path(undo_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UndoError("UNDO_NOT_FOUND", "No undo record has that id; it may have expired (records last 7 days).")


def _prune(now: float) -> None:
    """Before a new record: drop expired ones and keep room for it under MAX_RECORDS."""
    root = _root()
    if not root.is_dir():
        return
    entries = []
    for path in root.glob("appundo_*.json"):
        try:
            created = float(json.loads(path.read_text(encoding="utf-8")).get("created_at") or 0)
        except (OSError, ValueError):
            created = 0.0
        entries.append((created, path))
    entries.sort(reverse=True)
    for index, (created, path) in enumerate(entries):
        if index >= MAX_RECORDS - 1 or created + RETENTION_S <= now:
            path.unlink(missing_ok=True)


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Newest records first, without any app content: for the dashboard's change history."""
    root = _root()
    rows: List[Dict[str, Any]] = []
    if root.is_dir():
        for path in root.glob("appundo_*.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return [public(row) for row in rows[:max(1, min(int(limit), 100))]]


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    return {
        "undo_id": record["undo_id"], "app": record["app"], "action": record["action"],
        "item": dict(record.get("item") or {}), "state": record.get("state"),
        "created_at": record.get("created_at"), "expires_at": record.get("expires_at"),
        "undoable": record.get("state") == "recorded" and float(record.get("expires_at") or 0) > now,
        "undone_at": record.get("undone_at"), "new_item_id": record.get("new_item_id"),
    }


_NOUNS = {"Notes": "note", "Reminders": "reminder", "Calendar": "event", "Mail": "email", "Contacts": "contact"}


def history_item(record: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """A record in the dashboard's change history, shaped like a file transaction and without content."""
    stamp = time.time() if now is None else now
    row = public(record)
    expired = float(record.get("expires_at") or 0) <= stamp
    blocked = "already_undone" if record.get("state") == "undone" else "expired" if expired else None
    return {
        "transaction_id": row["undo_id"], "created_at": row["created_at"], "committed_at": row["created_at"],
        "undone_at": row["undone_at"], "operation": row["action"], "operation_class": "app_change",
        "state": "undone" if record.get("state") == "undone" else "committed", "reversibility": "full",
        "undoable": True, "undo_expires_at": row["expires_at"], "path_count": 0,
        "target_summary": f"{row['app']} · {_NOUNS.get(row['app'], 'item')}", "unsupported_count": 0,
        "child_count": 0, "can_undo": blocked is None, "undo_blocked_reason": blocked,
    }


def history(*, now: Optional[float] = None) -> List[Dict[str, Any]]:
    root = _root()
    if not root.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in root.glob("appundo_*.json"):
        try:
            rows.append(history_item(json.loads(path.read_text(encoding="utf-8")), now=now))
        except (OSError, ValueError, KeyError):
            continue
    return rows


def app_of(undo_id: str) -> str:
    return str(_read(undo_id).get("app") or "")


def _fingerprint(state: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── Reading an item's whole state ────────────────────────────────────────────

_ISO_HANDLER = r'''
on isoOf(d)
    if d is missing value then return ""
    set y to year of d as integer
    set mo to month of d as integer
    return (y as text) & "-" & my pad(mo) & "-" & my pad(day of d) & "T" & my pad(hours of d) & ":" & my pad(minutes of d) & ":" & my pad(seconds of d)
end isoOf
on pad(n)
    return text -2 thru -1 of ("0" & (n as integer as text))
end pad
'''


def _script(*lines: str) -> str:
    return _ISO_HANDLER + "\n".join(["set us to ASCII character 31", *lines])


def _fields(raw: str, names: tuple) -> Optional[Dict[str, str]]:
    if raw == "GONE":
        return None
    # Every reply ends with "#": the separators count as whitespace, so an empty last field would be trimmed.
    parts = raw.split(_US)
    if parts[-1] == "#":
        parts = parts[:-1]
    if parts[0] != "OK" or len(parts) != len(names) + 1:
        raise UndoError("UNDO_READ_FAILED", "The app did not return the item's state.")
    return dict(zip(names, parts[1:]))


def _bool(value: str) -> bool:
    return value.strip().lower() == "true"


_NOTE = ("id", "name", "folder", "folder_id", "attachments", "body")


def read_note(note_id: str, timeout_s: float = 10.0) -> Optional[Dict[str, Any]]:
    raw = _run(_script(
        'tell application "Notes"',
        f"    set matches to every note whose id is {_apple_string(note_id)}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set n to item 1 of matches",
        # A deleted note stays findable in Recently Deleted (a folder of its own) for 30 days.
        "    set f to container of n",
        '    return "OK" & us & (id of n as text) & us & (name of n as text) & us & (name of f as text) '
        "& us & (id of f as text) & us & ((count of attachments of n) as text) & us & (body of n as text) & us & \"#\"",
        "end tell",
    ), timeout_s=timeout_s, code="UNDO_READ_FAILED")
    row = _fields(raw, _NOTE)
    if row is None:
        return None
    return {**row, "attachments": int(row["attachments"] or 0)}


_REMINDER = ("id", "name", "list", "due", "allday", "completed", "flagged", "priority", "body")


def read_reminder(rid: str, timeout_s: float = 10.0) -> Optional[Dict[str, Any]]:
    raw = _run(_script(
        'tell application "Reminders"',
        f"    set matches to every reminder whose id is {_apple_string(rid)}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set r to item 1 of matches",
        '    set dueText to ""',
        '    set alldayText to "false"',
        "    if allday due date of r is not missing value then",
        "        set dueText to my isoOf(allday due date of r)",
        '        set alldayText to "true"',
        "    else if due date of r is not missing value then",
        "        set dueText to my isoOf(due date of r)",
        "    end if",
        '    set bodyText to ""',
        "    if body of r is not missing value then set bodyText to body of r as text",
        '    return "OK" & us & (id of r as text) & us & (name of r as text) & us & (name of container of r as text) '
        "& us & dueText & us & alldayText & us & (completed of r as text) & us & (flagged of r as text) "
        "& us & (priority of r as text) & us & bodyText & us & \"#\"",
        "end tell",
    ), timeout_s=timeout_s, code="UNDO_READ_FAILED")
    row = _fields(raw, _REMINDER)
    if row is None:
        return None
    return {**row, "allday": _bool(row["allday"]), "completed": _bool(row["completed"]),
            "flagged": _bool(row["flagged"]), "priority": int(row["priority"] or 0)}


_EVENT = ("uid", "calendar", "summary", "start", "end", "allday", "location", "description")


def _event_lookup(uid: str) -> List[str]:
    return [
        "    set foundEvents to {}",
        "    set foundCal to missing value",
        "    repeat with c in calendars",
        f"        set matches to every event of c whose uid is {_apple_string(uid)}",
        "        repeat with m in matches",
        "            set end of foundEvents to m",
        "            set foundCal to c",
        "        end repeat",
        "    end repeat",
    ]


def read_event(uid: str, timeout_s: float = 20.0) -> Optional[Dict[str, Any]]:
    raw = _run(_script(
        'tell application "Calendar"',
        *_event_lookup(uid),
        '    if (count of foundEvents) is not 1 then return "GONE"',
        "    set e to item 1 of foundEvents",
        '    set loc to ""',
        "    if location of e is not missing value then set loc to location of e as text",
        '    set desc to ""',
        "    if description of e is not missing value then set desc to description of e as text",
        '    return "OK" & us & (uid of e as text) & us & (name of foundCal as text) & us & (summary of e as text) '
        "& us & my isoOf(start date of e) & us & my isoOf(end date of e) & us & (allday event of e as text) "
        "& us & loc & us & desc & us & \"#\"",
        "end tell",
    ), timeout_s=timeout_s, code="UNDO_READ_FAILED")
    row = _fields(raw, _EVENT)
    if row is None:
        return None
    return {**row, "allday": _bool(row["allday"])}


_MESSAGE = ("message_id", "account", "mailbox", "read", "flagged")


def _mailbox_by_name(variable: str, account_var: str, name: str) -> List[str]:
    """Set `variable` to the mailbox of that name in the account, looking into nested folders too."""
    return [
        f"    set {variable} to missing value",
        "    try",
        f"        set {variable} to mailbox {_apple_string(name)} of {account_var}",
        "    end try",
        f"    if {variable} is missing value then",
        f"        repeat with b in mailboxes of {account_var}",
        f"            if (name of b as text) is {_apple_string(name)} then set {variable} to contents of b",
        "        end repeat",
        "    end if",
    ]


def read_message(message_id: str, *, mailbox_expr: Optional[str] = None, account: Optional[str] = None,
                 mailbox_name: Optional[str] = None, timeout_s: float = 15.0) -> Optional[Dict[str, Any]]:
    """A message by its Message-ID, in a standard mailbox (inbox, drafts...) or a named mailbox of an account."""
    if mailbox_expr:
        locate = [f"    set box to {mailbox_expr}"]
    else:
        locate = [f"    set acct to account {_apple_string(account or '')}",
                  *_mailbox_by_name("box", "acct", mailbox_name or "")]
    raw = _run(_script(
        'tell application "Mail"',
        *locate,
        '    if box is missing value then return "GONE"',
        f"    set matches to (every message of box whose message id is {_apple_string(message_id)})",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set m to item 1 of matches",
        '    return "OK" & us & (message id of m as text) & us & (name of account of mailbox of m as text) '
        "& us & (name of mailbox of m as text) & us & (read status of m as text) & us & (flagged status of m as text) "
        '& us & "#"',
        "end tell",
    ), timeout_s=timeout_s, code="UNDO_READ_FAILED")
    row = _fields(raw, _MESSAGE)
    if row is None:
        return None
    return {**row, "read": _bool(row["read"]), "flagged": _bool(row["flagged"])}


_PERSON = ("id", "name", "first_name", "last_name", "organization", "note", "emails", "phones")
_FS, _GS = chr(28), chr(29)

# A person's fields, each label and address kept with its value; "" where Contacts says missing value.
_PERSON_READ = [
    "    set fs to ASCII character 28",
    "    set gs to ASCII character 29",
    '    set fn to ""',
    "    if first name of p is not missing value then set fn to first name of p",
    '    set ln to ""',
    "    if last name of p is not missing value then set ln to last name of p",
    '    set org to ""',
    "    if organization of p is not missing value then set org to organization of p",
    '    set noteText to ""',
    "    if note of p is not missing value then set noteText to note of p",
    '    set mailText to ""',
    "    repeat with e in emails of p",
    '        set lab to ""',
    "        if label of e is not missing value then set lab to label of e",
    "        set mailText to mailText & lab & fs & (value of e as text) & gs",
    "    end repeat",
    '    set phoneText to ""',
    "    repeat with e in phones of p",
    '        set lab to ""',
    "        if label of e is not missing value then set lab to label of e",
    "        set phoneText to phoneText & lab & fs & (value of e as text) & gs",
    "    end repeat",
    '    return "OK" & us & (id of p as text) & us & (name of p as text) & us & fn & us & ln & us & org & us & '
    'noteText & us & mailText & us & phoneText & us & "#"',
]


def _labelled(text: str) -> List[List[str]]:
    return [item.split(_FS, 1) for item in text.split(_GS) if _FS in item]


def read_person(person_id: str, timeout_s: float = 10.0) -> Optional[Dict[str, Any]]:
    raw = _run(_script(
        'tell application "Contacts"',
        f"    set matches to every person whose id is {_apple_string(person_id)}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set p to item 1 of matches",
        *_PERSON_READ,
        "end tell",
    ), timeout_s=timeout_s, code="UNDO_READ_FAILED")
    row = _fields(raw, _PERSON)
    if row is None:
        return None
    return {**row, "emails": _labelled(row["emails"]), "phones": _labelled(row["phones"])}


# ── Before and after a change ────────────────────────────────────────────────

@dataclass
class Plan:
    """What mac_app knows about a change before it runs."""
    app: str
    action: str
    kind: str = ""
    before: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None  # why it cannot be undone, when that is known up front
    context: Dict[str, Any] = field(default_factory=dict)


def _note_by_name(query: str, exact: bool, timeout_s: float) -> str:
    """The id of the one note append_to_note will match by name, or "" (it refuses none or several)."""
    test = f"name is {_apple_string(query)}" if exact else f"name contains {_apple_string(query)}"
    raw = _run("\n".join(['tell application "Notes"', f"    set matches to every note whose {test}",
                           '    if (count of matches) is not 1 then return ""',
                           "    return id of item 1 of matches as text", "end tell"]),
               timeout_s=timeout_s, code="UNDO_READ_FAILED")
    return raw.strip()


def prepare(app: str, action: str, *, item_id: Optional[str] = None, query: Optional[str] = None,
            exact: bool = False, mailbox: Optional[str] = None, timeout_s: float = 10.0) -> Optional[Plan]:
    """Read the item's old state before mac_app changes it. Never raises: a failure is the plan's reason."""
    if not enabled():
        return None
    if action in _IRREVERSIBLE:
        return Plan(app, action, reason=_IRREVERSIBLE[action])
    kind = _PLANS.get((app, action))
    if kind is None:
        return None
    plan = Plan(app, action, kind=kind)
    if kind == "delete_created":
        return plan
    try:
        if app == "Notes":
            note_id = str(item_id or "").strip()
            if not note_id and query:
                note_id = _note_by_name(str(query).strip(), exact, timeout_s)
            plan.before = read_note(note_id, timeout_s) if note_id else None
            if plan.before and plan.before["attachments"]:
                plan.reason = ("The note has attachments; putting its old text back could lose them, "
                               "so this change is not recorded for undo.")
            elif plan.before and len(plan.before["body"]) > MAX_STATE_CHARS:
                plan.reason = "The note is too large to keep a copy of; this change is not recorded for undo."
        elif app == "Reminders":
            plan.before = read_reminder(str(item_id or ""), timeout_s)
        elif app == "Calendar":
            plan.before = read_event(str(item_id or ""), max(timeout_s, 20.0))
        elif app == "Contacts":
            plan.before = read_person(str(item_id or ""), timeout_s)
        elif app == "Mail":
            plan.context["mailbox_expr"] = _mailbox_expr(mailbox)[1]
            plan.before = read_message(str(item_id or ""), mailbox_expr=plan.context["mailbox_expr"], timeout_s=timeout_s)
    except AppAdapterError as exc:
        plan.reason = f"Its old state could not be read, so this change is not recorded for undo ({exc.code})."
        return plan
    if plan.before is None and plan.reason is None:
        plan.reason = "The item was not found before the change, so there is nothing to put back."
    return plan


def _created_id(app: str, payload: Dict[str, Any]) -> str:
    item = payload.get({"Notes": "note", "Reminders": "reminder", "Calendar": "event", "Mail": "draft",
                        "Contacts": "contact"}[app]) or {}
    return str(item.get("uid") or item.get("message_id") or item.get("id") or "")


def _read_after(plan: Plan, payload: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    app = plan.app
    if plan.kind == "recreate":
        return None
    item_id = _created_id(app, payload) if plan.kind == "delete_created" else str(
        (plan.before or {}).get("id") or (plan.before or {}).get("uid") or (plan.before or {}).get("message_id") or "")
    if app == "Notes":
        return read_note(item_id, timeout_s)
    if app == "Reminders":
        return read_reminder(item_id, timeout_s)
    if app == "Calendar":
        return read_event(item_id, max(timeout_s, 20.0))
    if app == "Contacts":
        return read_person(item_id, timeout_s)
    if plan.kind == "delete_created":
        return read_message(item_id, mailbox_expr="drafts mailbox", timeout_s=timeout_s)
    moved_to = str(payload.get("moved_to") or "")
    if moved_to:
        return read_message(item_id, account=(plan.before or {}).get("account"), mailbox_name=moved_to,
                            timeout_s=timeout_s)
    return read_message(item_id, mailbox_expr=plan.context.get("mailbox_expr"), timeout_s=timeout_s)


def _label(app: str, state: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> Dict[str, Any]:
    state = state or {}
    title = state.get("name") or state.get("summary") or ""
    if not title:
        item = payload.get({"Notes": "note", "Reminders": "reminder", "Calendar": "event", "Mail": "draft",
                            "Contacts": "contact"}.get(app, ""), {}) or {}
        title = item.get("title") or item.get("name") or item.get("summary") or item.get("subject") or ""
    item_id = state.get("id") or state.get("uid") or state.get("message_id") or _created_id(app, payload) \
        if app in {"Notes", "Reminders", "Calendar", "Mail", "Contacts"} else ""
    return {"id": str(item_id or "")[:300], "title": str(title)[:120]}


def record(plan: Optional[Plan], payload: Dict[str, Any], *, timeout_s: float = 10.0,
           now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """After a successful change: keep the record and say how to undo it (the result's `undo` field)."""
    if plan is None:
        return None
    if plan.action in _IRREVERSIBLE:
        if payload.get("sent") is False or payload.get("ran") is False:
            return None
        return {"undoable": False, "reason": plan.reason}
    if payload.get("changed") is False or payload.get("duplicate") or payload.get("deleted") is False:
        return {"undoable": False, "reason": "Nothing was changed, so there is nothing to undo."}
    if plan.reason:
        return {"undoable": False, "reason": plan.reason}
    try:
        after = _read_after(plan, payload, timeout_s)
    except AppAdapterError as exc:
        return {"undoable": False, "reason": f"The new state could not be read back, so this change is not "
                                             f"recorded for undo ({exc.code})."}
    if after is None and plan.kind != "recreate":
        return {"undoable": False, "reason": "The changed item could not be found afterwards."}
    stamp = time.time() if now is None else now
    undo_id = "appundo_" + secrets.token_hex(8)
    entry = {
        "undo_id": undo_id, "app": plan.app, "action": plan.action, "kind": plan.kind,
        "item": _label(plan.app, after or plan.before, payload),
        "before": plan.before, "after": after, "after_fingerprint": _fingerprint(after),
        "context": plan.context, "created_at": stamp, "expires_at": stamp + RETENTION_S, "state": "recorded",
    }
    with _LOCK:
        _prune(stamp)
        _write(entry)
    return {"undoable": True, "undo_id": undo_id, "expires_at": entry["expires_at"], "how": UNDO_HINT}


# ── Putting things back ──────────────────────────────────────────────────────

def _restore_note(before: Dict[str, Any], timeout_s: float) -> None:
    raw = _run("\n".join([
        "set us to ASCII character 31",
        'tell application "Notes"',
        f"    set matches to every note whose id is {_apple_string(before['id'])}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set n to item 1 of matches",
        f"    set body of n to {_apple_string(before['body'])}",
        f"    if (name of n as text) is not {_apple_string(before['name'])} then set name of n to {_apple_string(before['name'])}",
        '    return "OK"',
        "end tell",
    ]), timeout_s=timeout_s, code="UNDO_FAILED")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The note no longer exists; nothing was put back.")


def _due_lines(var: str, due: str, allday: bool) -> List[str]:
    if not due:
        return [f"    set due date of {var} to missing value", f"    set allday due date of {var} to missing value"]
    when = datetime.fromisoformat(due)
    setup = [f"    {line}" for line in _date_setup("dueDate", when).split("\n")]
    return setup + [f"    set {'allday due date' if allday else 'due date'} of {var} to dueDate"]


def _reminder_fields(var: str, before: Dict[str, Any]) -> List[str]:
    return [
        f"    set name of {var} to {_apple_string(before['name'])}",
        f"    set body of {var} to {_apple_string(before['body'])}",
        f"    set priority of {var} to {int(before['priority'])}",
        f"    set flagged of {var} to {'true' if before['flagged'] else 'false'}",
        *_due_lines(var, before["due"], before["allday"]),
        f"    set completed of {var} to {'true' if before['completed'] else 'false'}",
    ]


def _restore_reminder(before: Dict[str, Any], timeout_s: float) -> None:
    raw = _run("\n".join([
        'tell application "Reminders"',
        f"    set matches to every reminder whose id is {_apple_string(before['id'])}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set r to item 1 of matches",
        *_reminder_fields("r", before),
        '    return "OK"',
        "end tell",
    ]), timeout_s=timeout_s, code="UNDO_FAILED")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The reminder no longer exists; nothing was put back.")


def _recreate_reminder(before: Dict[str, Any], timeout_s: float) -> str:
    raw = _run("\n".join([
        'tell application "Reminders"',
        f"    set chosen to lists whose name is {_apple_string(before['list'])}",
        "    if (count of chosen) is 0 then set chosen to {default list}",
        "    set l to item 1 of chosen",
        f"    set r to make new reminder at end of l with properties {{name:{_apple_string(before['name'])}}}",
        *_reminder_fields("r", before),
        "    return id of r as text",
        "end tell",
    ]), timeout_s=timeout_s, code="UNDO_FAILED")
    return raw.strip()


def _restore_event(before: Dict[str, Any], timeout_s: float) -> None:
    start = datetime.fromisoformat(before["start"])
    end = datetime.fromisoformat(before["end"])
    raw = _run(_script(
        _date_setup("startDate", start),
        _date_setup("endDate", end),
        'tell application "Calendar"',
        *_event_lookup(before["uid"]),
        '    if (count of foundEvents) is not 1 then return "GONE"',
        "    set e to item 1 of foundEvents",
        f"    set summary of e to {_apple_string(before['summary'])}",
        f"    set allday event of e to {'true' if before['allday'] else 'false'}",
        # Moving the start first could put it after the old end, which Calendar refuses.
        "    if startDate > (end date of e) then",
        "        set end date of e to endDate",
        "        set start date of e to startDate",
        "    else",
        "        set start date of e to startDate",
        "        set end date of e to endDate",
        "    end if",
        f"    set location of e to {_apple_string(before['location'])}",
        f"    set description of e to {_apple_string(before['description'])}",
        '    return "OK"',
        "end tell",
    ), timeout_s=max(timeout_s, 20.0), code="UNDO_FAILED")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The event no longer exists; nothing was put back.")


def _restore_message(entry: Dict[str, Any], timeout_s: float) -> None:
    before, after = entry["before"], entry["after"]
    lines = ['tell application "Mail"', f"    set acct to account {_apple_string(after['account'])}",
             *_mailbox_by_name("box", "acct", after["mailbox"]),
             '    if box is missing value then return "GONE"',
             f"    set matches to (every message of box whose message id is {_apple_string(after['message_id'])})",
             '    if (count of matches) is not 1 then return "GONE"',
             "    set m to item 1 of matches",
             f"    set read status of m to {'true' if before['read'] else 'false'}",
             f"    set flagged status of m to {'true' if before['flagged'] else 'false'}"]
    if after["mailbox"] != before["mailbox"]:
        lines += [f"    set backAcct to account {_apple_string(before['account'])}",
                  *_mailbox_by_name("home", "backAcct", before["mailbox"]),
                  '    if home is missing value then return "NO_MAILBOX"',
                  "    move m to home"]
    lines += ['    return "OK"', "end tell"]
    raw = _run("\n".join(lines), timeout_s=max(timeout_s, 15.0), code="UNDO_FAILED")
    if raw == "NO_MAILBOX":
        raise UndoError("UNDO_FAILED", f"The mailbox {before['mailbox']!r} is gone; the message was not moved back.")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The message is no longer where Mac MCP left it; nothing was put back.")


def _person_lines(kind: str, items: List[List[str]]) -> List[str]:
    """Contacts' own lists (emails, phones) set again in their old order with their old labels."""
    lines = [f"    delete every {kind} of p"]
    for label, value in items:
        lines.append(f"    make new {kind} at end of {kind}s of p with properties "
                     f"{{label:{_apple_string(label or 'other')}, value:{_apple_string(value)}}}")
    return lines


def _restore_person(before: Dict[str, Any], timeout_s: float) -> None:
    raw = _run("\n".join([
        'tell application "Contacts"',
        f"    set matches to every person whose id is {_apple_string(before['id'])}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set p to item 1 of matches",
        f"    set first name of p to {_apple_string(before['first_name'])}",
        f"    set last name of p to {_apple_string(before['last_name'])}",
        f"    set organization of p to {_apple_string(before['organization'])}",
        f"    set note of p to {_apple_string(before['note'])}",
        *_person_lines("email", before.get("emails") or []),
        *_person_lines("phone", before.get("phones") or []),
        "    save",
        '    return "OK"',
        "end tell",
    ]), timeout_s=timeout_s, code="UNDO_FAILED")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The contact is no longer there; nothing was put back.")


def _delete_created(entry: Dict[str, Any], timeout_s: float) -> None:
    app, after = entry["app"], entry["after"]
    if app == "Notes":
        script = ['tell application "Notes"', f"    set matches to every note whose id is {_apple_string(after['id'])}",
                  '    if (count of matches) is not 1 then return "GONE"', "    delete item 1 of matches"]
    elif app == "Reminders":
        script = ['tell application "Reminders"',
                  f"    set matches to every reminder whose id is {_apple_string(after['id'])}",
                  '    if (count of matches) is not 1 then return "GONE"', "    delete item 1 of matches"]
    elif app == "Calendar":
        script = ['tell application "Calendar"', *_event_lookup(after["uid"]),
                  '    if (count of foundEvents) is not 1 then return "GONE"', "    delete item 1 of foundEvents"]
    elif app == "Contacts":
        script = ['tell application "Contacts"', f"    set matches to every person whose id is {_apple_string(after['id'])}",
                  '    if (count of matches) is not 1 then return "GONE"', "    delete item 1 of matches", "    save"]
    else:
        script = ['tell application "Mail"',
                  f"    set matches to (every message of drafts mailbox whose message id is {_apple_string(after['message_id'])})",
                  '    if (count of matches) is not 1 then return "GONE"', "    delete item 1 of matches"]
    raw = _run("\n".join([*script, '    return "OK"', "end tell"]), timeout_s=max(timeout_s, 20.0), code="UNDO_FAILED")
    if raw != "OK":
        raise UndoError("UNDO_ITEM_GONE", "The item Mac MCP made is already gone; nothing was removed.")


def _current(entry: Dict[str, Any], timeout_s: float) -> Optional[Dict[str, Any]]:
    after = entry.get("after") or {}
    app = entry["app"]
    if app == "Notes":
        return read_note(after["id"], timeout_s)
    if app == "Reminders":
        return read_reminder(after["id"], timeout_s)
    if app == "Calendar":
        return read_event(after["uid"], max(timeout_s, 20.0))
    if app == "Contacts":
        return read_person(after["id"], timeout_s)
    return read_message(after["message_id"], account=after.get("account"), mailbox_name=after.get("mailbox"),
                        timeout_s=timeout_s)


def _matches_before(entry: Dict[str, Any], current: Optional[Dict[str, Any]]) -> bool:
    """Whether the item reads back as it was (notes by title and visible text: Notes rewrites HTML)."""
    before = entry.get("before") or {}
    if current is None:
        return False
    if entry["app"] == "Notes":
        return current.get("name") == before.get("name") and _visible(current.get("body")) == _visible(before.get("body"))
    keys = [key for key in before if key not in {"id", "uid", "list", "calendar", "account"}]
    return all(current.get(key) == before.get(key) for key in keys)


def _visible(html_text: Optional[str]) -> str:
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", str(html_text or ""))
    return " ".join(html.unescape(text).split())


def undo(undo_id: str, *, app: str, force: bool = False, timeout_s: float = 15.0,
         now: Optional[float] = None) -> Dict[str, Any]:
    with _LOCK:
        entry = _read(str(undo_id or "").strip())
    stamp = time.time() if now is None else now
    if entry["app"] != app:
        raise UndoError("UNDO_WRONG_APP", f"That undo id belongs to {entry['app']}; call mac_app with app={entry['app']}.")
    if entry.get("state") == "undone":
        raise UndoError("UNDO_ALREADY_DONE", "This change was already undone.", undone_at=entry.get("undone_at"))
    if float(entry.get("expires_at") or 0) <= stamp:
        raise UndoError("UNDO_EXPIRED", "This change is older than 7 days; its undo record has expired.")
    kind = entry["kind"]
    if kind == "recreate":
        if not force and (entry.get("before") or {}).get("id") and read_reminder(entry["before"]["id"], timeout_s):
            raise UndoError("UNDO_CONFLICT", "The reminder exists again; nothing was made twice.")
    else:
        current = _current(entry, timeout_s)
        if current is None:
            raise UndoError("UNDO_ITEM_GONE", "The item is no longer there (deleted or moved since); nothing was changed.")
        if _fingerprint(current) != entry.get("after_fingerprint") and not force:
            raise UndoError(
                "UNDO_CONFLICT",
                "The item was changed after Mac MCP changed it; undo would overwrite that, so nothing was changed. "
                "Pass force=true to put the old state back anyway.", conflict=True)
    out: Dict[str, Any] = {"ok": True, "undone": True, "undo_id": entry["undo_id"], "undid": entry["action"],
                           "item": entry.get("item"), "verified": False}
    if kind == "delete_created":
        _delete_created(entry, timeout_s)
        gone = _current(entry, timeout_s)
        # A deleted note moves to Recently Deleted: still there, but in another folder.
        out.update(removed=True, verification="read_back",
                   verified=gone is None or (entry["app"] == "Notes" and gone.get("folder_id") != entry["after"].get("folder_id")))
        if entry["app"] == "Notes":
            out["note"] = "The note is in Notes' Recently Deleted folder for 30 days."
    elif kind == "recreate":
        new_id = _recreate_reminder(entry["before"], timeout_s)
        entry["new_item_id"] = new_id
        again = read_reminder(new_id, timeout_s) if new_id else None
        out.update(recreated=True, new_item_id=new_id, verification="read_back",
                   verified=bool(again) and _matches_before({**entry, "before": entry["before"]}, again),
                   note="The reminder was made again with a new id; its old id no longer exists.")
    else:
        restore: Callable[..., None] = {"Notes": _restore_note, "Reminders": _restore_reminder,
                                        "Calendar": _restore_event,
                                        "Contacts": _restore_person}.get(entry["app"]) or (lambda *_: None)
        if entry["app"] == "Mail":
            _restore_message(entry, timeout_s)
            back = read_message(entry["before"]["message_id"], account=entry["before"]["account"],
                                mailbox_name=entry["before"]["mailbox"], timeout_s=timeout_s)
        else:
            restore(entry["before"], timeout_s)
            back = _current({**entry, "after": entry["before"]}, timeout_s)
        out.update(restored=True, verification="read_back", verified=_matches_before(entry, back))
    entry.update(state="undone", undone_at=stamp, before=None, after=None, forced=bool(force))
    with _LOCK:
        _write(entry)  # what was kept to undo it is no longer needed
    return out
