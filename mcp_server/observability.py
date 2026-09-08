from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP


DEFAULT_TELEMETRY_DIR = Path.home() / ".mac-mcp" / "dashboard"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_EVENTS = 20_000
DEFAULT_PREVIEW_CHARS = 4_000
RECENT_MEMORY_EVENTS = 250

_SECRET_KEYS = {
    "authorization", "proxy_authorization", "api_key", "apikey", "access_key",
    "client_secret", "secret", "password", "passwd", "passphrase", "token",
    "access_token", "refresh_token", "id_token", "session_token", "session_id",
    "cookie", "set_cookie", "private_key", "credentials", "credential",
}
_SECRET_KEY_SUFFIXES = ("_password", "_secret", "_token", "_api_key", "_apikey")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_QUERY_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)\s*[=:]\s*)([^\s&;,]+)"
)
_ENV_SECRET_RE = re.compile(
    r"(?im)^(\s*(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*=\s*)(.+)$"
)
_SK_RE = re.compile(r"\b(?:sk|pk|rk|ghp|github_pat)-?[A-Za-z0-9_-]{16,}\b")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return min(max(int(raw), minimum), maximum)
    except ValueError:
        return default


def _is_secret_key(key: Optional[str]) -> bool:
    if not key:
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _redact_text(text: str) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _ENV_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _SK_RE.sub("[REDACTED]", text)
    return text


def _looks_encoded_blob(text: str) -> bool:
    if text.startswith("data:image/") or text.startswith("data:application/octet-stream"):
        return True
    compact = text.replace("\n", "").replace("\r", "")
    return len(compact) >= 768 and bool(_BASE64_RE.fullmatch(compact))


def sanitize_value(value: Any, *, key: Optional[str] = None, preview_chars: int = DEFAULT_PREVIEW_CHARS,
                   depth: int = 0) -> Any:
    """Return a JSON-safe, bounded, secret-aware representation for dashboard telemetry."""
    if _is_secret_key(key):
        return "[REDACTED]"
    if depth > 12:
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[binary · {len(value):,} bytes]"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        safe = _redact_text(value)
        if _looks_encoded_blob(safe):
            label = "image data" if safe.startswith("data:image/") else "encoded/binary value"
            return f"[{label} · {len(value):,} chars]"
        if len(safe) > preview_chars:
            omitted = len(safe) - preview_chars
            return safe[:preview_chars] + f"\n… [{omitted:,} chars omitted]"
        return safe
    if hasattr(value, "model_dump"):
        try:
            return sanitize_value(value.model_dump(), key=key, preview_chars=preview_chars, depth=depth + 1)
        except Exception:
            pass
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for child_key, child_value in value.items():
            text_key = str(child_key)
            out[text_key] = sanitize_value(
                child_value, key=text_key, preview_chars=preview_chars, depth=depth + 1
            )
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        cap = 100
        result = [
            sanitize_value(item, preview_chars=preview_chars, depth=depth + 1)
            for item in items[:cap]
        ]
        if len(items) > cap:
            result.append(f"[{len(items) - cap:,} more items omitted]")
        return result
    try:
        return sanitize_value(vars(value), key=key, preview_chars=preview_chars, depth=depth + 1)
    except Exception:
        return _redact_text(str(value))[:preview_chars]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _parse_json(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class TelemetryManager:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        retention_days: Optional[int] = None,
        max_events: Optional[int] = None,
        preview_chars: Optional[int] = None,
    ) -> None:
        self.started_at = time.time()
        telemetry_dir = Path(os.getenv("MAC_MCP_TELEMETRY_DIR", str(DEFAULT_TELEMETRY_DIR))).expanduser()
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or (telemetry_dir / "telemetry.sqlite3")).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days or _env_int(
            "MAC_MCP_TELEMETRY_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 1, 365
        )
        self.max_events = max_events or _env_int(
            "MAC_MCP_TELEMETRY_MAX_EVENTS", DEFAULT_MAX_EVENTS, 100, 500_000
        )
        self.preview_chars = preview_chars or _env_int(
            "MAC_MCP_TELEMETRY_PREVIEW_CHARS", DEFAULT_PREVIEW_CHARS, 256, 40_000
        )
        self._active: Dict[str, Dict[str, Any]] = {}
        self._recent: deque[Dict[str, Any]] = deque(maxlen=RECENT_MEMORY_EVENTS)
        self._subscribers: List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.RLock()
        self._writes = 0
        self._init_db()
        self._load_recent()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        needs_schema = not self.db_path.exists()
        conn = sqlite3.connect(self.db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        if needs_schema:
            self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_events (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL,
                tool TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                arguments_json TEXT,
                result_json TEXT,
                result_size INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_events_time ON tool_events(timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_events_tool_time ON tool_events(tool, timestamp DESC)"
        )

    def _init_db(self) -> None:
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _load_recent(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_events ORDER BY timestamp DESC LIMIT ?", (RECENT_MEMORY_EVENTS,)
            ).fetchall()
        for row in reversed(rows):
            self._recent.append(self._row_to_event(row))

    def _row_to_event(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "kind": "call_finished",
            "event_id": row["event_id"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "tool": row["tool"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_ms": row["duration_ms"],
            "arguments": _parse_json(row["arguments_json"]),
            "result": _parse_json(row["result_json"]),
            "result_size": row["result_size"],
            "error": row["error"],
        }

    def _publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            def deliver(q: asyncio.Queue = queue, payload: Dict[str, Any] = dict(event)) -> None:
                try:
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                self.unsubscribe(queue)

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    def start_call(self, source: str, tool: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        now = time.time()
        event_id = "evt_" + uuid.uuid4().hex[:14]
        event = {
            "kind": "call_started",
            "event_id": event_id,
            "timestamp": now,
            "source": str(source or "mcp"),
            "tool": str(tool or "unknown"),
            "status": "running",
            "started_at": now,
            "arguments": sanitize_value(arguments or {}, preview_chars=self.preview_chars),
        }
        with self._lock:
            self._active[event_id] = event
        self._publish(event)
        return event_id

    def update_arguments(self, event_id: str, arguments: Dict[str, Any]) -> None:
        with self._lock:
            event = self._active.get(event_id)
            if event is not None:
                event["arguments"] = sanitize_value(arguments, preview_chars=self.preview_chars)

    def finish_call(self, event_id: str, *, result: Any = None, error: Optional[BaseException | str] = None) -> Dict[str, Any]:
        ended = time.time()
        with self._lock:
            started_event = self._active.pop(event_id, None)
        if started_event is None:
            return {"event_id": event_id, "status": "unknown"}
        safe_result = None if error is not None else sanitize_value(result, preview_chars=self.preview_chars)
        safe_error = None
        if error is not None:
            if isinstance(error, BaseException):
                safe_error = sanitize_value(
                    f"{error.__class__.__name__}: {error}", preview_chars=self.preview_chars
                )
            else:
                safe_error = sanitize_value(str(error), preview_chars=self.preview_chars)
        result_text = _json_text(safe_result) if safe_result is not None else ""
        event = {
            "kind": "call_finished",
            "event_id": event_id,
            "timestamp": ended,
            "source": started_event["source"],
            "tool": started_event["tool"],
            "status": "error" if error is not None else "success",
            "started_at": started_event["started_at"],
            "ended_at": ended,
            "duration_ms": max(0, int((ended - float(started_event["started_at"])) * 1000)),
            "arguments": started_event.get("arguments") or {},
            "result": safe_result,
            "result_size": len(result_text.encode("utf-8")),
            "error": safe_error,
        }
        self._insert_event(event)
        with self._lock:
            self._recent.append(event)
        self._publish(event)
        return event

    def _insert_event(self, event: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_events (
                    event_id, timestamp, source, tool, status, started_at, ended_at,
                    duration_ms, arguments_json, result_json, result_size, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"], event["timestamp"], event["source"], event["tool"],
                    event["status"], event["started_at"], event["ended_at"], event["duration_ms"],
                    _json_text(event.get("arguments")), _json_text(event.get("result")),
                    int(event.get("result_size") or 0), event.get("error"),
                ),
            )
        self._writes += 1
        if self._writes % 100 == 1:
            self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - (self.retention_days * 86400)
        with self._connect() as conn:
            conn.execute("DELETE FROM tool_events WHERE timestamp < ?", (cutoff,))
            conn.execute(
                """
                DELETE FROM tool_events
                WHERE event_id NOT IN (
                    SELECT event_id FROM tool_events ORDER BY timestamp DESC LIMIT ?
                )
                """,
                (self.max_events,),
            )

    def active_calls(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            events = [dict(item) for item in self._active.values()]
        for event in events:
            event["duration_ms"] = max(0, int((now - float(event["started_at"])) * 1000))
        return sorted(events, key=lambda item: item["started_at"], reverse=True)

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._lock:
            return [dict(event) for event in list(self._recent)[-bounded:]][::-1]

    def query_events(
        self,
        *,
        hours: float = 24,
        limit: int = 100,
        source: Optional[str] = None,
        status: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        bounded_hours = max(0.05, min(float(hours), 24 * 365))
        bounded_limit = max(1, min(int(limit), 500))
        clauses = ["timestamp >= ?"]
        params: List[Any] = [time.time() - bounded_hours * 3600]
        if source and source != "all":
            clauses.append("source = ?")
            params.append(source)
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if tool:
            clauses.append("tool LIKE ?")
            params.append(f"%{tool}%")
        params.append(bounded_limit)
        sql = "SELECT * FROM tool_events WHERE " + " AND ".join(clauses) + " ORDER BY timestamp DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def summary(self, hours: float = 24) -> Dict[str, Any]:
        bounded_hours = max(0.05, min(float(hours), 24 * 365))
        cutoff = time.time() - bounded_hours * 3600
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                       AVG(duration_ms) AS avg_duration
                FROM tool_events WHERE timestamp >= ?
                """,
                (cutoff,),
            ).fetchone()
            durations = [
                int(row[0]) for row in conn.execute(
                    "SELECT duration_ms FROM tool_events WHERE timestamp >= ? ORDER BY duration_ms", (cutoff,)
                ).fetchall()
            ]
            top_tools = [
                dict(row) for row in conn.execute(
                    """
                    SELECT tool, COUNT(*) AS calls,
                           ROUND(AVG(duration_ms)) AS avg_duration_ms,
                           SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors
                    FROM tool_events WHERE timestamp >= ?
                    GROUP BY tool ORDER BY calls DESC, tool ASC LIMIT 8
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            source_rows = [
                dict(row) for row in conn.execute(
                    "SELECT source, COUNT(*) AS calls FROM tool_events WHERE timestamp >= ? GROUP BY source",
                    (cutoff,),
                ).fetchall()
            ]
        total = int(totals["total"] or 0)
        success = int(totals["success"] or 0)
        errors = int(totals["errors"] or 0)
        p95 = 0
        if durations:
            # Nearest-rank percentile: ceil(0.95 * N) - 1. This keeps small
            # samples honest instead of rounding the slowest call away.
            import math
            p95 = durations[min(len(durations) - 1, max(0, math.ceil(len(durations) * 0.95) - 1))]
        return {
            "window_hours": bounded_hours,
            "total_calls": total,
            "success_calls": success,
            "error_calls": errors,
            "success_rate": round((success / total * 100), 1) if total else 100.0,
            "avg_duration_ms": int(round(float(totals["avg_duration"] or 0))),
            "p95_duration_ms": int(p95),
            "active_calls": len(self.active_calls()),
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "top_tools": top_tools,
            "sources": source_rows,
            "retention_days": self.retention_days,
            "max_events": self.max_events,
        }


class ObservedFastMCP(FastMCP):
    """FastMCP with one central telemetry hook for every protocol tool call."""

    def __init__(self, *args: Any, telemetry: TelemetryManager, **kwargs: Any) -> None:
        self.telemetry = telemetry
        super().__init__(*args, **kwargs)

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        event_id = self.telemetry.start_call("mcp", name, arguments)
        try:
            result = await super().call_tool(name, arguments)
        except BaseException as exc:
            self.telemetry.finish_call(event_id, error=exc)
            raise
        self.telemetry.finish_call(event_id, result=result)
        return result
