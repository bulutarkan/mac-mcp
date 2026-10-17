"""Hash-linked, tamper-evident security events.

Every security event gets a chain sequence number and a SHA-256 over its
fields plus the previous event's hash, so editing, deleting or reordering a
retained event breaks the chain. Retention removes only a prefix of the chain
and first stores where it ends (the anchor), so pruning stays verifiable.
Every CHECKPOINT_EVERY events the latest (seq, hash) is also appended to a
file outside the database; a checkpoint the database no longer matches shows
that recent events were cut off or rewritten.

Limit: a process running as the same user can also rewrite the checkpoint file
and recompute the chain. This detects tampering by anything that edits the
database alone (other tools, partial edits, a careless script); it is not a
defence against an attacker with full control of this account.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

GENESIS_HASH = "0" * 64
CHECKPOINT_EVERY = 50
HASHED_FIELDS = (
    "event_id", "timestamp", "session_id", "event_type", "tool", "tool_class", "origin",
    "decision", "reason_code", "profile", "actor", "agent_id", "target_summary",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(security_events)").fetchall()}
    for column, kind in (("chain_seq", "INTEGER"), ("prev_hash", "TEXT"), ("record_hash", "TEXT")):
        if column not in columns:
            conn.execute(f"ALTER TABLE security_events ADD COLUMN {column} {kind}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_security_events_chain ON security_events(chain_seq)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS security_chain_anchor ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL, hash TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    # Mac MCP only appends events and prunes a prefix behind the anchor. Every other change to a
    # retained event (an edit, a swap, a delete) bumps this counter, so a check can skip re-walking
    # the already verified part only while it is unchanged (#254).
    conn.execute("CREATE TABLE IF NOT EXISTS security_chain_changes (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO security_chain_changes (id, n) VALUES (1, 0)")
    conn.executescript(_CHANGE_TRIGGERS)


_CHANGE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS security_events_edited AFTER UPDATE ON security_events
BEGIN UPDATE security_chain_changes SET n = n + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS security_events_removed AFTER DELETE ON security_events
WHEN OLD.chain_seq IS NOT NULL AND OLD.chain_seq > COALESCE((SELECT seq FROM security_chain_anchor WHERE id = 1), 0)
BEGIN UPDATE security_chain_changes SET n = n + 1 WHERE id = 1; END;
"""
_TRIGGER_NAMES = ("security_events_edited", "security_events_removed")


def change_mark(conn: sqlite3.Connection) -> Optional[int]:
    """How often a retained event was edited or deleted; None when that can no longer be told."""
    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'security_events'")}
    if not set(_TRIGGER_NAMES) <= present:
        return None
    row = conn.execute("SELECT n FROM security_chain_changes WHERE id = 1").fetchone()
    return int(row[0]) if row else None


def record_hash(event: Mapping[str, Any], seq: int, prev_hash: str) -> str:
    payload = {field: event.get(field) for field in HASHED_FIELDS}
    payload["timestamp"] = repr(float(event.get("timestamp") or 0.0))
    payload.update(chain_seq=int(seq), prev_hash=prev_hash)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _anchor(conn: sqlite3.Connection) -> tuple[int, str]:
    row = conn.execute("SELECT seq, hash FROM security_chain_anchor WHERE id = 1").fetchone()
    return (int(row[0]), str(row[1])) if row else (0, GENESIS_HASH)


def append(conn: sqlite3.Connection, event: Mapping[str, Any]) -> tuple[int, str]:
    """Insert one security event as the next chain link. Call inside a write transaction."""
    last = conn.execute(
        "SELECT chain_seq, record_hash FROM security_events WHERE chain_seq IS NOT NULL "
        "ORDER BY chain_seq DESC LIMIT 1"
    ).fetchone()
    prev_seq, prev_hash = (int(last[0]), str(last[1])) if last else _anchor(conn)
    seq = prev_seq + 1
    digest = record_hash(event, seq, prev_hash)
    fields = list(HASHED_FIELDS) + ["chain_seq", "prev_hash", "record_hash"]
    values = [event.get(field) for field in HASHED_FIELDS] + [seq, prev_hash, digest]
    conn.execute(
        f"INSERT INTO security_events ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values,
    )
    return seq, digest


def prune(conn: sqlite3.Connection, *, cutoff: float, max_events: int) -> int:
    """Drop old events as a prefix of the chain, remembering where the kept part starts."""
    # Unchained rows from before the chain existed follow the old rules.
    conn.execute("DELETE FROM security_events WHERE chain_seq IS NULL AND timestamp < ?", (cutoff,))
    newest = conn.execute("SELECT MAX(chain_seq) FROM security_events").fetchone()[0]
    if newest is None:
        return 0
    by_count = int(newest) - int(max_events)
    by_age = conn.execute(
        "SELECT MAX(chain_seq) FROM security_events WHERE chain_seq IS NOT NULL AND timestamp < ?", (cutoff,),
    ).fetchone()[0]
    upto = max(by_count, int(by_age or 0))
    if upto <= 0:
        return 0
    row = conn.execute(
        "SELECT chain_seq, record_hash FROM security_events WHERE chain_seq <= ? ORDER BY chain_seq DESC LIMIT 1",
        (upto,),
    ).fetchone()
    if row is None:
        return 0
    conn.execute(
        "INSERT INTO security_chain_anchor (id, seq, hash, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, hash = excluded.hash, updated_at = excluded.updated_at",
        (int(row[0]), str(row[1]), time.time()),
    )
    return conn.execute("DELETE FROM security_events WHERE chain_seq <= ?", (int(row[0]),)).rowcount


def checkpoint_path(db_path: Path) -> Path:
    return Path(db_path).with_name("security-chain-checkpoints.log")


def write_checkpoint(db_path: Path, seq: int, digest: str) -> None:
    path = checkpoint_path(db_path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{int(seq)} {digest} {time.time():.3f}\n".encode())
    finally:
        os.close(fd)


def _latest_checkpoint(db_path: Path) -> Optional[tuple[int, str]]:
    try:
        lines = checkpoint_path(db_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            return int(parts[0]), parts[1]
    return None


def verify(conn: sqlite3.Connection, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Walk the retained chain; report the first broken link and checkpoint agreement."""
    anchor_seq, expected_prev = _anchor(conn)
    return _walk(conn, db_path, anchor_seq, anchor_seq, expected_prev)


def verify_since(conn: sqlite3.Connection, db_path: Optional[Path], *, anchor_seq: int, seq: int,
                 digest: str, mark: Optional[int]) -> Optional[Dict[str, Any]]:
    """Check only the events after an earlier full verify that ended at (seq, digest) with change mark.

    The verified part must be untouched since (no edit or delete counted, none of them
    missing, its last link unchanged) and every newer event must follow it. None when
    that cannot be shown this way (the chain was pruned, something was changed, the
    change triggers are gone): the caller then walks the whole chain, which also names
    the problem exactly.
    """
    if mark is None or change_mark(conn) != mark or _anchor(conn)[0] != anchor_seq:
        return None
    kept = conn.execute("SELECT COUNT(*) FROM security_events WHERE chain_seq > ? AND chain_seq <= ?",
                        (anchor_seq, seq)).fetchone()[0]
    if int(kept) != seq - anchor_seq:
        return None
    if seq > anchor_seq:
        row = conn.execute("SELECT record_hash FROM security_events WHERE chain_seq = ?", (seq,)).fetchone()
        if row is None or str(row[0] or "") != digest:
            return None
    result = _walk(conn, db_path, anchor_seq, seq, digest)
    return result if result["ok"] else None


def _walk(conn: sqlite3.Connection, db_path: Optional[Path], anchor_seq: int, after_seq: int,
          expected_prev: str) -> Dict[str, Any]:
    cursor = conn.execute(
        f"SELECT {','.join(HASHED_FIELDS)}, chain_seq, prev_hash, record_hash FROM security_events "
        "WHERE chain_seq > ? ORDER BY chain_seq", (after_seq,)
    )
    unchained = int(conn.execute("SELECT COUNT(*) FROM security_events WHERE chain_seq IS NULL").fetchone()[0])
    checkpoint = _latest_checkpoint(db_path) if db_path is not None else None
    expected_seq = after_seq + 1
    checked = 0
    checkpoint_hash: Optional[str] = None
    while True:
        rows = cursor.fetchmany(5_000)  # streamed: a long chain is never held in memory at once
        if not rows:
            break
        for row in rows:
            event = dict(zip(HASHED_FIELDS, row[: len(HASHED_FIELDS)]))
            seq, prev_hash, stored = int(row[-3]), str(row[-2] or ""), str(row[-1] or "")
            if seq != expected_seq:
                return _broken("missing_events", expected_seq, checked, unchained)
            if prev_hash != expected_prev:
                return _broken("broken_link", seq, checked, unchained)
            if record_hash(event, seq, prev_hash) != stored:
                return _broken("modified_event", seq, checked, unchained)
            if checkpoint is not None and seq == checkpoint[0]:
                checkpoint_hash = stored
            checked += 1
            expected_prev, expected_seq = stored, seq + 1
    result: Dict[str, Any] = {
        "ok": True, "checked": checked, "latest_seq": expected_seq - 1, "latest_hash": expected_prev,
        "anchor_seq": anchor_seq, "unchained_legacy": unchained, "checkpoint": None,
    }
    if checkpoint is not None:
        seq, digest = checkpoint
        result["checkpoint"] = {"seq": seq}
        if seq > anchor_seq:
            if seq > result["latest_seq"]:
                result.update(ok=False, problem="truncated_after_checkpoint", seq=seq)
            else:
                if seq <= after_seq:  # verified by an earlier walk; its stored hash still has to match
                    row = conn.execute("SELECT record_hash FROM security_events WHERE chain_seq = ?",
                                       (seq,)).fetchone()
                    checkpoint_hash = str(row[0] or "") if row else None
                if checkpoint_hash != digest:
                    result.update(ok=False, problem="checkpoint_mismatch", seq=seq)
    return result


def _broken(problem: str, seq: int, checked: int, unchained: int) -> Dict[str, Any]:
    return {"ok": False, "problem": problem, "seq": seq, "checked": checked, "unchained_legacy": unchained}
