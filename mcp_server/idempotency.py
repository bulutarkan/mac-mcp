"""Caller-supplied idempotency keys for state-changing tool calls.

A client that loses the response to a write cannot tell whether it ran. If it
sends idempotency_key, Mac MCP records the call under (caller, tool, key)
together with a hash of its arguments:

  - a repeat of a completed call returns the first result (idempotent_replay)
    instead of running the tool again;
  - a repeat while the first call is still running, or after it ended with an
    unknown outcome, is refused with that status and is never re-executed;
  - the same key with different arguments is a conflict;
  - a call refused before it dispatched anything frees the key for a retry.

This is deduplication, not exactly-once execution: work that a crashed call
did before it recorded completion stays unknown. The caller is the agent id
or the authenticated actor, not the MCP session, so a reconnected client
still finds its earlier call. Entries are kept for RETENTION_S in an
owner-only database; results that look like secrets or exceed
MAX_RESULT_BYTES are not stored, and a replay then reports completion
without the original result.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

RETENTION_S = 24 * 3600
MAX_ENTRIES = 2_000
MAX_RESULT_BYTES = 256 * 1024
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
TRANSPORT_FIELDS = ("description", "idempotency_key")

KEY_SCHEMA = {
    "type": "string", "minLength": 8, "maxLength": 128, "pattern": KEY_PATTERN.pattern,
    "description": "Optional. Repeat with the same key and arguments to get the first result instead of running twice.",
}

_LOCK = threading.Lock()


class IdempotencyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def journal_path() -> Path:
    base = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return base / "idempotency.sqlite3"


def _connect() -> sqlite3.Connection:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(path, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calls (scope TEXT PRIMARY KEY, tool TEXT NOT NULL, args_hash TEXT NOT NULL, "
        "state TEXT NOT NULL, result TEXT, retained INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
        "updated_at REAL NOT NULL)"
    )
    return conn


def validate_key(key: Any) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise IdempotencyError(
            "invalid_idempotency_key", "idempotency_key must be 8-128 characters of letters, digits, '.', '_', ':' or '-'",
        )
    return key


def scope_for(caller: str, tool: str, key: str) -> str:
    return hashlib.sha256(f"{caller}\0{tool}\0{key}".encode("utf-8")).hexdigest()


def arguments_hash(arguments: Dict[str, Any]) -> str:
    material = {k: v for k, v in (arguments or {}).items() if k not in TRANSPORT_FIELDS}
    canonical = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM calls WHERE updated_at < ?", (now - RETENTION_S,))
    conn.execute(
        "DELETE FROM calls WHERE scope IN (SELECT scope FROM calls ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
        (MAX_ENTRIES,),
    )


def claim(scope: str, tool: str, args_hash: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ("new", None) after recording the call as started, or the earlier call's state."""
    now = time.time()
    with _LOCK, closing(_connect()) as conn, conn:
        _prune(conn, now)
        row = conn.execute("SELECT args_hash, state, result, retained FROM calls WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO calls (scope, tool, args_hash, state, created_at, updated_at) VALUES (?, ?, ?, 'started', ?, ?)",
                (scope, tool, args_hash, now, now),
            )
            return "new", None
        if row[0] != args_hash:
            return "conflict", None
        if row[1] == "completed":
            return "completed", {"retained": bool(row[3]), "result": json.loads(row[2]) if row[3] and row[2] else None}
        return row[1], None


def complete(scope: str, stored: Any) -> bool:
    """Record completion; stores the result unless it is too large or looks sensitive."""
    from .data_guard import contains_direct_secret

    text = json.dumps(stored, ensure_ascii=False, default=str)
    retained = len(text.encode("utf-8")) <= MAX_RESULT_BYTES and not contains_direct_secret(text)
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE calls SET state = 'completed', result = ?, retained = ?, updated_at = ? WHERE scope = ?",
            (text if retained else None, int(retained), time.time(), scope),
        )
    return retained


def mark_unknown(scope: str) -> None:
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute("UPDATE calls SET state = 'unknown', updated_at = ? WHERE scope = ?", (time.time(), scope))


def release(scope: str) -> None:
    """Forget a call that was refused before it did anything, so the key can be retried."""
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM calls WHERE scope = ? AND state = 'started'", (scope,))


def dump_result(result: Any) -> Dict[str, Any]:
    """Serialize a FastMCP call_tool result (content blocks, optionally with structured content)."""
    content, structured, shape = result, None, "content"
    if isinstance(result, tuple) and len(result) == 2:
        content, structured, shape = result[0], result[1], "tuple"
    blocks = [
        block.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(block, "model_dump") else block
        for block in (content or [])
    ]
    return {"shape": shape, "content": blocks, "structured": structured}


def _mark_replay(value: Any) -> Any:
    if isinstance(value, dict):
        return {**value, "idempotent_replay": True}
    return value


def replay(earlier: Optional[Dict[str, Any]]) -> Any:
    """Rebuild the stored result, flagged idempotent_replay; without a stored result say so."""
    from mcp.types import ContentBlock, TextContent
    from pydantic import TypeAdapter

    if not earlier or not earlier.get("retained") or not earlier.get("result"):
        payload = {"ok": True, "status": "completed", "idempotent_replay": True, "result_retained": False,
                   "note": "This call already completed; its result was not kept (too large or sensitive)."}
        text = json.dumps(payload, ensure_ascii=False)
        return [TextContent(type="text", text=text)], {"result": payload}
    stored = earlier["result"]
    blocks = []
    for block in stored.get("content") or []:
        if block.get("type") == "text":
            try:
                block = {**block, "text": json.dumps(_mark_replay(json.loads(block["text"])), ensure_ascii=False)}
            except (ValueError, TypeError):
                pass
        blocks.append(block)
    content = TypeAdapter(list[ContentBlock]).validate_python(blocks)
    if stored.get("shape") != "tuple":
        return content
    structured = stored.get("structured")
    if isinstance(structured, dict) and isinstance(structured.get("result"), dict):
        structured = {**structured, "result": _mark_replay(structured["result"])}
    else:
        structured = _mark_replay(structured)
    return content, structured
