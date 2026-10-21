"""Read and search Messages without opening a conversation (#261). Read-only by construction.

Messages keeps its history in ~/Library/Messages/chat.db, which macOS lets only apps with Full
Disk Access open. Mac MCP does not ask for that itself: a small helper, "Mac MCP Messages
Reader", is the one app the owner adds there. It opens that one file read-only and runs one
statement SQLite reports as read-only (menu_app/MessagesReader). This module builds the queries,
turns rows into messages and never sends, marks or deletes anything.

The helper ships inside Mac MCP.app but runs from a copy in ~/.mac-mcp/helpers: macOS judges code
nested in an app bundle as that app, so inside the bundle the owner's grant to the helper would
not count (and granting Mac MCP.app would cover far more than Messages). The copy keeps the
helper's signature, so the grant follows it across updates.

Each message carries its conversation (chat_id, the same id find_chats returns), who sent it
and when. Newer macOS versions keep the text only in attributedBody (an NSArchiver stream); the
helper's mm_text() SQL function returns the plain string either way, and mm_fold() folds case,
accents and the Turkish dotless i so a search runs as one query. Secrets in message text (keys,
tokens, passwords in key=value form) are redacted like everywhere else in Mac MCP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import paths
from .data_guard import redact_sensitive_text
from .private_storage import ensure_private_dir

READER_NAME = "Mac MCP Messages Reader.app"
TEXT_LIMIT = 2000
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Ordinary messages only: no tapbacks (associated_message_type), no "named the group" style events.
_PLAIN = "m.associated_message_type = 0 AND m.item_type = 0"


class MessagesError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# The helper

def bundled_reader() -> Optional[Path]:
    """The helper as Mac MCP.app ships it."""
    for app in (Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")):
        path = app / "Contents" / "Helpers" / READER_NAME
        if path.exists():
            return path
    return None


def installed_reader() -> Path:
    return paths.state_dir() / "helpers" / READER_NAME


def _fingerprint(app: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        for part in ("Contents/Info.plist", "Contents/MacOS/MacMCPMessagesReader", "Contents/_CodeSignature/CodeResources"):
            digest.update((app / part).read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


def _install(source: Path, target: Path) -> None:
    ensure_private_dir(target.parent)
    staging = target.with_name(f".{target.name}.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    # ditto keeps the code signature and extended attributes that make the copy the same app.
    subprocess.run(["/usr/bin/ditto", str(source), str(staging)], capture_output=True, timeout=60, check=True)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)


def reader_app() -> Optional[Path]:
    """The helper to run: the copy in ~/.mac-mcp/helpers, refreshed when Mac MCP.app ships a new one."""
    configured = os.getenv("MAC_MCP_MESSAGES_READER", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    target = installed_reader()
    source = bundled_reader()
    if source is not None and _fingerprint(source) != _fingerprint(target):
        try:
            _install(source, target)
        except (OSError, subprocess.SubprocessError):
            pass  # a copy that is already there keeps working
    return target if target.exists() else None


def _run_helper(sql: str, params: Sequence[Any], max_rows: int) -> Dict[str, Any]:
    app = reader_app()
    if app is None:
        raise MessagesError("MESSAGES_READER_MISSING",
                            "Mac MCP Messages Reader is not installed; reinstall the Mac MCP app (mac-mcp repair).")
    folder = ensure_private_dir(paths.state_dir() / "tmp")
    with tempfile.TemporaryDirectory(dir=folder) as work:
        request, result = Path(work) / "request.json", Path(work) / "result.json"
        request.write_text(json.dumps({"sql": sql, "params": list(params), "max_rows": max_rows}), encoding="utf-8")
        # Started through Launch Services so the helper answers for itself to Full Disk Access.
        subprocess.run(["/usr/bin/open", "-W", "-n", "-g", str(app), "--args", "--request", str(request),
                        "--result", str(result)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        try:
            return json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "error": "no_result", "message": "Mac MCP Messages Reader did not answer."}


# Tests swap this for a runner over a temporary database.
_RUNNER: Callable[[str, Sequence[Any], int], Dict[str, Any]] = _run_helper


def _query(sql: str, params: Sequence[Any] = (), max_rows: int = 500) -> List[Dict[str, Any]]:
    answer = _RUNNER(sql, params, max_rows)
    if not answer.get("ok"):
        if answer.get("error") == "full_disk_access":
            raise MessagesError(
                "MESSAGES_FULL_DISK_ACCESS_REQUIRED",
                "Reading Messages needs Full Disk Access for Mac MCP Messages Reader. The owner turns it on in "
                "Mac MCP Settings › Apps › Reading Messages (System Settings › Privacy & Security › Full Disk "
                "Access). Do not work around it.", retryable=False,
            )
        raise MessagesError("MESSAGES_READ_FAILED", str(answer.get("message") or answer.get("error") or "read failed"))
    columns = answer.get("columns") or []
    return [dict(zip(columns, row)) for row in answer.get("rows") or []]


def access() -> Dict[str, Any]:
    """Whether reading works now: granted, needs_full_disk_access or reader_missing."""
    if reader_app() is None:
        return {"state": "reader_missing", "reader": None}
    try:
        _query("SELECT count(*) AS n FROM chat", max_rows=1)
    except MessagesError as exc:
        state = "needs_full_disk_access" if exc.code == "MESSAGES_FULL_DISK_ACCESS_REQUIRED" else "error"
        return {"state": state, "reader": str(reader_app()), "message": str(exc)}
    return {"state": "granted", "reader": str(reader_app())}


# ---------------------------------------------------------------------------
# Rows

def _when(value: Any) -> Optional[str]:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    seconds = raw / 1e9 if raw > 10**12 else float(raw)  # nanoseconds since 2001 on current macOS
    return (_APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def _apple_time(value: str, *, end: bool) -> int:
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            moment = datetime.fromisoformat(text).astimezone()
            if end:
                moment += timedelta(days=1)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.astimezone()
    except ValueError as exc:
        raise MessagesError("APP_ADAPTER_ARGUMENT_INVALID", f"{value!r} is not a date (YYYY-MM-DD or ISO time).") from exc
    return int((moment - _APPLE_EPOCH).total_seconds() * 1e9)


def _clean(text: Optional[str]) -> Tuple[str, bool]:
    value = (text or "").replace("￼", "").strip()  # U+FFFC stands where an attachment sits
    value = redact_sensitive_text(value)
    return (value[:TEXT_LIMIT], len(value) > TEXT_LIMIT)


def _message(row: Dict[str, Any], chats: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    body, clipped = _clean(row.get("body"))
    chat = chats.get(int(row.get("chat_rowid") or 0)) or {}
    from_me = bool(row.get("is_from_me"))
    out = {
        "message_id": row.get("guid"), "chat_id": chat.get("chat_id"), "chat": chat.get("name"),
        "date": _when(row.get("date")), "from_me": from_me,
        "sender": "me" if from_me else (row.get("sender") or None),
        "text": body, "service": row.get("service"),
    }
    if clipped:
        out["text_truncated"] = True
    if row.get("cache_has_attachments"):
        out["has_attachments"] = True
    return out


# ---------------------------------------------------------------------------
# Conversations

_CHATS_SQL = """
SELECT c.ROWID AS rowid, c.guid AS guid, c.chat_identifier AS identifier, c.display_name AS display_name,
       c.service_name AS service,
       (SELECT group_concat(h.id, char(31)) FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id
         WHERE chj.chat_id = c.ROWID) AS handles,
       (SELECT max(cmj.message_date) FROM chat_message_join cmj WHERE cmj.chat_id = c.ROWID) AS last_date
FROM chat c
"""


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in text if not unicodedata.combining(char)).replace("ı", "i")


def chats() -> Dict[int, Dict[str, Any]]:
    rows = _query(_CHATS_SQL, max_rows=5000)
    out: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        handles = [item for item in str(row.get("handles") or "").split("\x1f") if item]
        name = row.get("display_name") or ", ".join(handles) or row.get("identifier") or ""
        out[int(row["rowid"])] = {
            "rowid": int(row["rowid"]), "chat_id": row.get("guid"), "identifier": row.get("identifier") or "",
            "name": name, "handles": handles, "service": row.get("service"), "last_date": row.get("last_date") or 0,
        }
    return out


def list_chats(query: Optional[str], *, limit: int) -> Dict[str, Any]:
    """Conversations, most recent first, for find_chats while Messages is closed. Handles, not contact names."""
    known = sorted(chats().values(), key=lambda chat: -int(chat["last_date"] or 0))
    needle = " ".join(str(query or "").split())
    folded, digits = _fold(needle), _digits(needle)
    rows = [chat for chat in known if not needle or folded in _fold(chat["name"])
            or any(folded in _fold(handle) for handle in chat["handles"])
            or (len(digits) >= 6 and any(_digits(handle).endswith(digits[-9:]) for handle in chat["handles"]))]
    return {
        "ok": True, "count": len(rows[:limit]), "matched": len(rows), "truncated": len(rows) > limit,
        "chats": [{"chat_id": chat["chat_id"], "name": chat["name"], "participants": [], "handles": chat["handles"],
                   "last_message": _when(chat["last_date"])} for chat in rows[:limit]],
        "source": "messages_database",
        "note": "Messages is closed, so chats are listed from its database with numbers and addresses, not contact "
                "names. Read one with read_messages (item_id = chat_id) or search with search_messages.",
    }


def _same_chat(chat: Dict[str, Any], wanted: str) -> bool:
    """find_chats ids (from Messages scripting) and chat.db guids name a chat by its identifier."""
    wanted = wanted.strip()
    if not wanted:
        return False
    if wanted in (chat["chat_id"], chat["identifier"]):
        return True
    return wanted.rsplit(";", 1)[-1] == chat["identifier"]


def _contact_handles(query: str) -> List[str]:
    try:
        from . import app_adapters_more as more

        found = more.contacts_find(query, limit=10, timeout_s=8.0)
    except Exception:  # Contacts is a convenience here; a chat name or number still works without it
        return []
    handles: List[str] = []
    for person in found.get("contacts") or []:
        handles.extend(str(item) for item in (person.get("emails") or []) + (person.get("phones") or []))
    return handles


def resolve_chat(item_id: Optional[str], query: Optional[str], known: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    if item_id:
        for chat in known.values():
            if _same_chat(chat, str(item_id)):
                return chat
        raise MessagesError("MESSAGES_CHAT_NOT_FOUND", "No conversation has that chat_id. Use find_chats.")
    needle = str(query or "").strip()
    if not needle:
        raise MessagesError("APP_ADAPTER_ARGUMENT_INVALID", "Give item_id (a chat_id from find_chats) or query.")
    folded, digits = _fold(needle), _digits(needle)
    matches = [chat for chat in known.values()
               if folded in _fold(chat["name"]) or any(folded in _fold(handle) for handle in chat["handles"])
               or (len(digits) >= 6 and any(_digits(handle).endswith(digits[-9:]) for handle in chat["handles"]))]
    if not matches:
        # A person's name: their phone numbers and addresses from Contacts.
        wanted = {_digits(item)[-9:] if _digits(item) else _fold(item) for item in _contact_handles(needle)}
        wanted.discard("")
        matches = [chat for chat in known.values() if any(
            (_digits(handle)[-9:] if _digits(handle) else _fold(handle)) in wanted for handle in chat["handles"])]
    if not matches:
        raise MessagesError("MESSAGES_CHAT_NOT_FOUND", "No conversation matches; use find_chats to see them.")
    if len(matches) > 1:
        # Prefer the one-to-one chat over groups that also include the person.
        single = [chat for chat in matches if len(chat["handles"]) <= 1]
        if len(single) == 1:
            return single[0]
        raise MessagesError(
            "MESSAGES_CHAT_AMBIGUOUS", "Several conversations match; pass item_id.",
            chats=[{"chat_id": chat["chat_id"], "name": chat["name"]}
                   for chat in sorted(matches, key=lambda item: -int(item["last_date"]))[:10]],
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Reading and searching

_MESSAGES_SQL = """
SELECT m.ROWID AS rowid, m.guid AS guid, mm_text(m.text, m.attributedBody) AS body, m.date AS date,
       m.is_from_me AS is_from_me, m.service AS service, m.cache_has_attachments AS cache_has_attachments,
       h.id AS sender, cmj.chat_id AS chat_rowid
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE {where}
ORDER BY m.date DESC, m.ROWID DESC
LIMIT ?
"""


def _cursor_where(cursor: Optional[str]) -> Tuple[str, List[Any]]:
    if not cursor:
        return "", []
    match = re.fullmatch(r"(\d+):(\d+)", str(cursor))
    if not match:
        raise MessagesError("APP_ADAPTER_ARGUMENT_INVALID", "cursor is the next_cursor of an earlier result.")
    date, rowid = int(match.group(1)), int(match.group(2))
    return " AND (m.date < ? OR (m.date = ? AND m.ROWID < ?))", [date, date, rowid]


def _range_where(date_from: Optional[str], date_to: Optional[str]) -> Tuple[str, List[Any]]:
    where, params = "", []
    if date_from:
        where += " AND m.date >= ?"
        params.append(_apple_time(date_from, end=False))
    if date_to:
        where += " AND m.date < ?"
        params.append(_apple_time(date_to, end=True))
    return where, params


def read_messages(item_id: Optional[str], query: Optional[str], *, limit: int, cursor: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """The latest messages of one conversation, newest first; next_cursor reads older ones."""
    known = chats()
    chat = resolve_chat(item_id, query, known)
    where, params = f"cmj.chat_id = ? AND {_PLAIN}", [chat["rowid"]]
    extra, extra_params = _range_where(date_from, date_to)
    after, after_params = _cursor_where(cursor)
    rows = _query(_MESSAGES_SQL.format(where=where + extra + after), [*params, *extra_params, *after_params, limit + 1],
                  max_rows=limit + 1)
    more_left = len(rows) > limit
    rows = rows[:limit]
    return {
        "ok": True, "chat_id": chat["chat_id"], "chat": chat["name"], "participants": chat["handles"],
        "count": len(rows), "messages": [_message(row, known) for row in rows],
        "next_cursor": f"{rows[-1]['date']}:{rows[-1]['rowid']}" if more_left and rows else None,
        "order": "newest_first", "read_only": True,
    }


def _same_text(value: Optional[str]) -> str:
    return " ".join(unicodedata.normalize("NFC", (value or "").replace("\ufffc", "")).split())


def find_sent(chat_id: str, text: str, *, since_s: float) -> Optional[Dict[str, Any]]:
    """The message this Mac sent to that conversation with that text at or after since_s, if Messages has
    written it to its history (#42). Only that chat, only from_me, only new rows: an older message with the
    same words or the same words in another chat never count. The text itself is not returned."""
    known = chats()
    chat = resolve_chat(chat_id, None, known)
    # A couple of seconds' slack: Messages stamps the row a moment after Mac MCP noted the time.
    since = int((datetime.fromtimestamp(since_s - 2.0, timezone.utc) - _APPLE_EPOCH).total_seconds() * 1e9)
    rows = _query(_MESSAGES_SQL.format(where=f"cmj.chat_id = ? AND {_PLAIN} AND m.is_from_me = 1 AND m.date >= ?"),
                  [chat["rowid"], since, 20], max_rows=20)
    wanted = _same_text(text)
    for row in rows:
        if _same_text(row.get("body")) == wanted:
            return {"message_id": row.get("guid"), "chat_id": chat["chat_id"], "date": _when(row.get("date"))}
    return None


def search_messages(query: str, *, item_id: Optional[str] = None, limit: int, cursor: Optional[str] = None,
                    date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """Messages whose text contains query (any case or accents), newest first, across chats or in one."""
    needle = " ".join(str(query or "").split())
    if len(needle) < 2:
        raise MessagesError("APP_ADAPTER_ARGUMENT_INVALID", "search_messages needs query (at least 2 characters).")
    known = chats()
    where, params = f"{_PLAIN} AND instr(mm_fold(mm_text(m.text, m.attributedBody)), mm_fold(?)) > 0", [needle]
    chat = None
    if item_id:
        chat = resolve_chat(item_id, None, known)
        where += " AND cmj.chat_id = ?"
        params.append(chat["rowid"])
    extra, extra_params = _range_where(date_from, date_to)
    after, after_params = _cursor_where(cursor)
    rows = _query(_MESSAGES_SQL.format(where=where + extra + after), [*params, *extra_params, *after_params, limit + 1],
                  max_rows=limit + 1)
    more_left = len(rows) > limit
    rows = rows[:limit]
    return {
        "ok": True, "query": needle, "chat_id": chat["chat_id"] if chat else None, "count": len(rows),
        "messages": [_message(row, known) for row in rows],
        "next_cursor": f"{rows[-1]['date']}:{rows[-1]['rowid']}" if more_left and rows else None,
        "order": "newest_first", "read_only": True,
    }
