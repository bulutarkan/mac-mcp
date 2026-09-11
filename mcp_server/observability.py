from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .steering import SteeringManager, attach_steering, preemption_error

from .policy import (
    PolicyContext,
    annotations_for_tool,
    current_policy_context,
    evaluate_profile,
    evaluate_tool_scope,
    filter_scoped_result,
    policy_metadata,
    profile_denied_result,
    resolve_risk,
    scope_denied_result,
)


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


_TELEMETRY_METADATA_COLUMNS = {
    "declared_risk": "declared_risk_json",
    "effective_risk": "effective_risk_json",
    "profile": "profile",
    "policy_decision": "policy_decision",
    "actor": "actor",
    "agent_id": "agent_id",
    "team_id": "team_id",
    "resource": "resource_json",
    "scope": "scope_json",
    "lock": "lock_json",
}
_JSON_METADATA_FIELDS = {"declared_risk", "effective_risk", "resource", "scope", "lock"}


def _mapping_failure_status(value: Any) -> Optional[str]:
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump()
        except Exception:
            return None
    if not isinstance(value, dict):
        return None
    marker = str(value.get("error") or value.get("code") or "").strip().lower()
    state = str(value.get("status") or value.get("state") or "").strip().lower()
    if value.get("denied") is True or marker in {"denied", "profile_denied", "scope_denied"} or state == "denied":
        return "denied"
    if value.get("blocked") is True or marker == "blocked" or state == "blocked":
        return "blocked"
    if value.get("ok") is False or state in {"error", "failed", "failure", "cancelled"}:
        return "error"
    return None


def normalize_result_status(result: Any, error: Optional[BaseException | str] = None) -> str:
    """Normalize tool-shaped failures even when no exception was raised."""

    if error is not None:
        return "error"
    direct = _mapping_failure_status(result)
    if direct is not None:
        return direct
    if isinstance(result, (list, tuple)):
        for item in result:
            item_status = _mapping_failure_status(item)
            if item_status is not None:
                return item_status
            if hasattr(item, "model_dump"):
                try:
                    payload = item.model_dump()
                except Exception:
                    continue
                text = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(text, str):
                    parsed = _parse_json(text)
                    text_status = _mapping_failure_status(parsed)
                    if text_status is not None:
                        return text_status
    return "success"


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
                error TEXT,
                declared_risk_json TEXT,
                effective_risk_json TEXT,
                profile TEXT,
                policy_decision TEXT,
                actor TEXT,
                agent_id TEXT,
                team_id TEXT,
                resource_json TEXT,
                scope_json TEXT,
                lock_json TEXT
            )
            """
        )
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)").fetchall()}
        for column in _TELEMETRY_METADATA_COLUMNS.values():
            if column not in existing:
                conn.execute(f"ALTER TABLE tool_events ADD COLUMN {column} TEXT")
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
        event = {
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
        columns = set(row.keys())
        for field, column in _TELEMETRY_METADATA_COLUMNS.items():
            raw = row[column] if column in columns else None
            event[field] = _parse_json(raw) if field in _JSON_METADATA_FIELDS else raw
        return event

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

    def start_call(
        self,
        source: str,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
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
        for field in _TELEMETRY_METADATA_COLUMNS:
            event[field] = sanitize_value((metadata or {}).get(field), preview_chars=self.preview_chars)
        with self._lock:
            self._active[event_id] = event
        self._publish(event)
        return event_id

    def update_arguments(self, event_id: str, arguments: Dict[str, Any]) -> None:
        with self._lock:
            event = self._active.get(event_id)
            if event is not None:
                event["arguments"] = sanitize_value(arguments, preview_chars=self.preview_chars)

    def update_context(
        self,
        event_id: str,
        *,
        tool: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            event = self._active.get(event_id)
            if event is None:
                return
            if tool:
                event["tool"] = str(tool)
            for field in _TELEMETRY_METADATA_COLUMNS:
                if field in (metadata or {}):
                    event[field] = sanitize_value(metadata[field], preview_chars=self.preview_chars)

    def finish_call(
        self,
        event_id: str,
        *,
        result: Any = None,
        error: Optional[BaseException | str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
            "status": normalize_result_status(result, error),
            "started_at": started_event["started_at"],
            "ended_at": ended,
            "duration_ms": max(0, int((ended - float(started_event["started_at"])) * 1000)),
            "arguments": started_event.get("arguments") or {},
            "result": safe_result,
            "result_size": len(result_text.encode("utf-8")),
            "error": safe_error,
        }
        for field in _TELEMETRY_METADATA_COLUMNS:
            value = (metadata or {}).get(field, started_event.get(field))
            event[field] = sanitize_value(value, preview_chars=self.preview_chars)
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
                    duration_ms, arguments_json, result_json, result_size, error,
                    declared_risk_json, effective_risk_json, profile, policy_decision,
                    actor, agent_id, team_id, resource_json, scope_json, lock_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"], event["timestamp"], event["source"], event["tool"],
                    event["status"], event["started_at"], event["ended_at"], event["duration_ms"],
                    _json_text(event.get("arguments")), _json_text(event.get("result")),
                    int(event.get("result_size") or 0), event.get("error"),
                    _json_text(event.get("declared_risk")), _json_text(event.get("effective_risk")),
                    event.get("profile"), event.get("policy_decision"), event.get("actor"),
                    event.get("agent_id"), event.get("team_id"), _json_text(event.get("resource")),
                    _json_text(event.get("scope")), _json_text(event.get("lock")),
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
                       SUM(CASE WHEN status<>'success' THEN 1 ELSE 0 END) AS errors,
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
                           SUM(CASE WHEN status<>'success' THEN 1 ELSE 0 END) AS errors
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


_STEERING_PARENT_EVENT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mac_mcp_steering_parent_event", default=None
)


_CORE_TOOL_NAMES = {
    "run_command", "run_commands_parallel",
    "read_file", "write_file", "edit_file", "search_files", "http_request",
    "mac_observe", "mac_act",
    "browser_list_tabs", "browser_close_tab", "browser_observe", "browser_do",
    "spawn_agents", "wait_agents",
    "memory_search", "ask_user",
    "tool_discover", "tool_invoke",
}


class ObservedFastMCP(FastMCP):
    """FastMCP with central registration hints, enforcement, telemetry, and optional compact discovery."""

    def __init__(
        self,
        *args: Any,
        telemetry: TelemetryManager,
        steering: Optional[SteeringManager] = None,
        policy_context_provider: Callable[[], PolicyContext] = current_policy_context,
        **kwargs: Any,
    ) -> None:
        self.telemetry = telemetry
        self.steering = steering or SteeringManager()
        self._policy_context_provider = policy_context_provider
        super().__init__(*args, **kwargs)

    async def list_tools(self):
        tools = await super().list_tools()
        if os.getenv("MAC_MCP_TOOL_PROFILE", "core").strip().lower() != "core":
            return tools
        extra = {
            item.strip() for item in os.getenv("MAC_MCP_CORE_EXTRA_TOOLS", "").split(",") if item.strip()
        }
        allowed = _CORE_TOOL_NAMES | extra
        compact = []
        for tool in tools:
            if tool.name not in allowed:
                continue
            description = tool.description or ""
            if len(description) > 220:
                description = description[:217].rsplit(" ", 1)[0] + "..."
                tool = tool.model_copy(update={"description": description})
            compact.append(tool)
        return compact

    def tool(
        self,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        annotations: Any = None,
        icons: Any = None,
        meta: Optional[dict[str, Any]] = None,
        structured_output: Optional[bool] = None,
    ):
        def register(fn: Any):
            effective_name = name or fn.__name__
            central_annotations = annotations_for_tool(effective_name)
            return super(ObservedFastMCP, self).tool(
                name=name,
                title=title,
                description=description,
                annotations=central_annotations,
                icons=icons,
                meta=meta,
                structured_output=structured_output,
            )(fn)
        return register

    async def _call_registered_tool(self, name: str, arguments: dict[str, Any]):
        """Keep synchronous tool bodies off the server event loop.

        FastMCP 1.27 executes sync functions inline. Moving only those registered
        tool calls to a worker thread keeps localhost dashboard/steering requests
        responsive while long shell, browser, file, or UI work is in progress.
        asyncio.to_thread propagates the current contextvars into the worker.
        """
        tool = self._tool_manager.get_tool(name)
        if tool is not None and not tool.is_async:
            base_call = super(ObservedFastMCP, self).call_tool
            return await asyncio.to_thread(lambda: asyncio.run(base_call(name, arguments)))
        return await super().call_tool(name, arguments)

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        declared, effective = resolve_risk(name, arguments)
        policy_context = self._policy_context_provider()
        decision = evaluate_profile(policy_context.profile, effective)
        metadata = policy_metadata(policy_context, declared, effective, decision)
        event_id = self.telemetry.start_call("mcp", name, arguments, metadata=metadata)

        parent_event = _STEERING_PARENT_EVENT.get()
        top_level = parent_event is None
        steering_token = _STEERING_PARENT_EVENT.set(event_id) if top_level else None
        session = None
        call_registered = False

        if top_level:
            try:
                session = self.get_context().session
            except (LookupError, ValueError, AttributeError):
                session = None

        try:
            # Session-bound steering is checked before policy/tool execution. If a
            # user queued steering while this agent was idle, fail this attempted
            # tool without executing it so the model sees the new direction first.
            if top_level and session is not None:
                pending = self.steering.prepare_call(session, tool=name, arguments=arguments)
                if pending:
                    self.telemetry.finish_call(
                        event_id,
                        result={"ok": False, "error": "steering_preempted", "tool": name},
                        metadata={"policy_decision": "steering_preempted"},
                    )
                    raise ToolError(preemption_error(name, pending))

            if not decision.allowed:
                result = profile_denied_result(name, decision, declared, effective)
                self.telemetry.finish_call(event_id, result=result)
                raise ToolError(
                    f"profile_denied: tool={name}; profile={decision.profile}; reason={decision.reason}"
                )

            scope_decision = evaluate_tool_scope(policy_context.scope, name, arguments, effective)
            if not scope_decision.allowed and policy_context.scope is not None:
                result = scope_denied_result(name, scope_decision, policy_context.scope)
                self.telemetry.finish_call(
                    event_id, result=result, metadata={"policy_decision": "scope_denied"}
                )
                reasons = ",".join(scope_decision.reasons) or "scope_rejected"
                raise ToolError(f"scope_denied: tool={name}; reasons={reasons}")

            if top_level and session is not None:
                self.steering.begin_call(session, event_id, tool=name, arguments=arguments)
                call_registered = True

            try:
                result = await self._call_registered_tool(name, arguments)
            except BaseException as exc:
                self.telemetry.finish_call(event_id, error=exc)
                if call_registered and session is not None:
                    # Keep steering queued when the underlying tool fails. The next
                    # tool request for this same agent session will be preempted.
                    self.steering.finish_call(session, event_id, delivered=False)
                raise

            result = filter_scoped_result(policy_context.scope, name, result)
            # Keep menu-bar steering out of persistent telemetry; it is attached
            # only to the live MCP response after normal result logging completes.
            self.telemetry.finish_call(event_id, result=result)
            if not call_registered or session is None:
                return result
            messages = self.steering.finish_call(session, event_id, delivered=True)
            return attach_steering(result, messages)
        finally:
            if top_level and steering_token is not None:
                _STEERING_PARENT_EVENT.reset(steering_token)
