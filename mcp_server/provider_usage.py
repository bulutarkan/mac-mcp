from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

SCHEMA_VERSION = 1
SOURCE_REPORT = "report"
SOURCE_NATIVE_TOKENIZER = "native_tokenizer"
SOURCE_ESTIMATE = "estimate"
RETENTION_DAYS = 400
COMPONENTS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")


def db_path() -> Path:
    state = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return state / "state" / "provider_usage.sqlite3"


def _local_bucket(timestamp: float) -> tuple[str, str]:
    dt = datetime.fromtimestamp(float(timestamp)).astimezone()
    return dt.date().isoformat(), f"{dt.tzname() or 'local'}{dt.strftime('%z')}"


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


@dataclass(frozen=True)
class NormalizedUsage:
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    reasoning_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    total_tokens: Optional[int]
    recognized: bool
    total_only: bool


@dataclass(frozen=True)
class UsageRecord:
    event_key: str
    provider: str
    session_id: str
    event_id: str
    timestamp: float
    agent_id: Optional[str]
    model: Optional[str]
    model_verified: bool
    source: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    reasoning_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    total_tokens: Optional[int]
    provider_version: Optional[str] = None
    requested_model: Optional[str] = None
    effective_model: Optional[str] = None


def normalize_codex_usage(usage: Any) -> NormalizedUsage:
    if not isinstance(usage, Mapping):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    known = {
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "cache_write_input_tokens",
    }
    if not (known & set(usage.keys())):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    reasoning = _nonnegative_int(usage.get("reasoning_output_tokens"))
    cache_read = _nonnegative_int(usage.get("cached_input_tokens"))
    cache_write = _nonnegative_int(usage.get("cache_write_input_tokens"))
    # Codex cached_input is a subset of input; reasoning_output is a breakdown
    # of output. Total must not add either breakdown twice.
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return NormalizedUsage(
        input_tokens, output_tokens, reasoning, cache_read, cache_write,
        total, True, False,
    )


def normalize_opencode_usage(tokens: Any) -> NormalizedUsage:
    if not isinstance(tokens, Mapping):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    known = {"input", "output", "reasoning", "cache", "total"}
    if not (known & set(tokens.keys())):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
    input_tokens = _nonnegative_int(tokens.get("input"))
    output_tokens = _nonnegative_int(tokens.get("output"))
    reasoning = _nonnegative_int(tokens.get("reasoning"))
    cache_read = _nonnegative_int(cache.get("read"))
    cache_write = _nonnegative_int(cache.get("write"))
    total = _nonnegative_int(tokens.get("total"))
    components = (input_tokens, output_tokens, reasoning, cache_read, cache_write)
    total_only = total is not None and all(value is None for value in components)
    # OpenCode 1.18.x total equals its five reported components. Only derive it
    # when the complete native breakdown is present; never ratio-fill gaps.
    if total is None and all(value is not None for value in components):
        total = sum(int(value or 0) for value in components)
    recognized = total is not None or any(value is not None for value in components)
    return NormalizedUsage(
        input_tokens, output_tokens, reasoning, cache_read, cache_write,
        total, recognized, total_only,
    )


class ProviderUsageStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or db_path()).expanduser()
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS provider_usage_events (
                event_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                local_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                agent_id TEXT,
                requested_model TEXT,
                effective_model TEXT,
                model TEXT,
                model_verified INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                provider_version TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                total_tokens INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_provider_usage_events_date
                ON provider_usage_events(local_date DESC);
            CREATE INDEX IF NOT EXISTS idx_provider_usage_events_provider
                ON provider_usage_events(provider, local_date DESC);

            CREATE TABLE IF NOT EXISTS provider_usage_daily (
                local_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                agents INTEGER NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                input_known INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                output_known INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_known INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_known INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_known INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                total_known INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (
                    local_date, timezone, provider, model, source, schema_version
                )
            );
            CREATE INDEX IF NOT EXISTS idx_provider_usage_daily_date
                ON provider_usage_daily(local_date DESC);

            CREATE TABLE IF NOT EXISTS provider_usage_daily_agents (
                local_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                agent_id TEXT NOT NULL,
                PRIMARY KEY (local_date, timezone, provider, model, agent_id)
            );

            CREATE TABLE IF NOT EXISTS codex_usage_sessions (
                session_id TEXT PRIMARY KEY,
                last_ordinal INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_usage_diagnostics (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            """
        )
        event_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(provider_usage_events)"
            ).fetchall()
        }
        if "requested_model" not in event_columns:
            conn.execute(
                "ALTER TABLE provider_usage_events ADD COLUMN requested_model TEXT"
            )
        if "effective_model" not in event_columns:
            conn.execute(
                "ALTER TABLE provider_usage_events ADD COLUMN effective_model TEXT"
            )

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            self._schema(conn)

    def diagnostic_increment(self, name: str, amount: int = 1) -> None:
        clean = str(name or "unknown").strip()[:80] or "unknown"
        try:
            with self._lock, closing(self._connect()) as conn, conn:
                self._schema(conn)
                conn.execute(
                    """
                    INSERT INTO provider_usage_diagnostics(name, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        value=value+excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (clean, max(0, int(amount)), time.time()),
                )
        except Exception:
            pass

    def begin_codex_turn(self, session_id: str) -> tuple[int, str]:
        session = str(session_id or "").strip()
        if not session:
            raise ValueError("session_id is required")
        with self._lock, closing(self._connect()) as conn, conn:
            self._schema(conn)
            row = conn.execute(
                "SELECT last_ordinal FROM codex_usage_sessions WHERE session_id=?",
                (session,),
            ).fetchone()
            ordinal = int(row["last_ordinal"] or 0) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO codex_usage_sessions(session_id, last_ordinal, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_ordinal=excluded.last_ordinal,
                    updated_at=excluded.updated_at
                """,
                (session, ordinal, time.time()),
            )
        return ordinal, f"codex:{session}:turn:{ordinal}"

    def ingest(self, record: UsageRecord) -> bool:
        provider = str(record.provider or "").strip().lower()
        if provider not in {"codex", "opencode"}:
            return False
        event_key = str(record.event_key or "").strip()
        session_id = str(record.session_id or "").strip()
        event_id = str(record.event_id or "").strip()
        if not event_key or not session_id or not event_id:
            self.diagnostic_increment("invalid_identity")
            return False
        if record.source not in {SOURCE_REPORT, SOURCE_NATIVE_TOKENIZER, SOURCE_ESTIMATE}:
            self.diagnostic_increment("invalid_source")
            return False

        local_date, timezone_name = _local_bucket(record.timestamp)
        requested_model = str(record.requested_model or "").strip()
        effective_model = str(record.effective_model or "").strip()
        if not record.model_verified:
            requested_model = ""
            effective_model = ""
        model = (
            effective_model
            or requested_model
            or (str(record.model or "").strip() if record.model_verified else "")
        )
        raw_values = {
            "input": record.input_tokens,
            "output": record.output_tokens,
            "reasoning": record.reasoning_tokens,
            "cache_read": record.cache_read_tokens,
            "cache_write": record.cache_write_tokens,
            "total": record.total_tokens,
        }
        values = {key: _nonnegative_int(value) for key, value in raw_values.items()}

        try:
            with self._lock, closing(self._connect()) as conn, conn:
                self._schema(conn)
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO provider_usage_events (
                        event_key, provider, session_id, event_id, timestamp,
                        local_date, timezone, agent_id,
                        requested_model, effective_model, model, model_verified,
                        source, schema_version, provider_version,
                        input_tokens, output_tokens, reasoning_tokens,
                        cache_read_tokens, cache_write_tokens, total_tokens
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_key, provider, session_id, event_id, float(record.timestamp),
                        local_date, timezone_name,
                        str(record.agent_id or "").strip() or None,
                        requested_model or None,
                        effective_model or None,
                        model or None,
                        1 if record.model_verified and model else 0,
                        record.source, SCHEMA_VERSION,
                        str(record.provider_version or "").strip() or None,
                        values["input"], values["output"], values["reasoning"],
                        values["cache_read"], values["cache_write"], values["total"],
                    ),
                )
                if inserted.rowcount != 1:
                    conn.execute(
                        """
                        INSERT INTO provider_usage_diagnostics(name, value, updated_at)
                        VALUES ('duplicate_events', 1, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            value=value+1, updated_at=excluded.updated_at
                        """,
                        (time.time(),),
                    )
                    return False

                new_agent = 0
                agent_id = str(record.agent_id or "").strip()
                if agent_id:
                    marker = conn.execute(
                        """
                        INSERT OR IGNORE INTO provider_usage_daily_agents(
                            local_date, timezone, provider, model, agent_id
                        ) VALUES (?,?,?,?,?)
                        """,
                        (local_date, timezone_name, provider, model, agent_id),
                    )
                    new_agent = 1 if marker.rowcount == 1 else 0

                fields: list[Any] = [
                    local_date, timezone_name, provider, model, record.source,
                    SCHEMA_VERSION, new_agent, 1,
                ]
                for component in COMPONENTS:
                    value = values[component]
                    fields.extend([int(value or 0), 1 if value is not None else 0])

                conn.execute(
                    """
                    INSERT INTO provider_usage_daily (
                        local_date, timezone, provider, model, source, schema_version,
                        agents, turns,
                        input_tokens, input_known,
                        output_tokens, output_known,
                        reasoning_tokens, reasoning_known,
                        cache_read_tokens, cache_read_known,
                        cache_write_tokens, cache_write_known,
                        total_tokens, total_known
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(
                        local_date, timezone, provider, model, source, schema_version
                    ) DO UPDATE SET
                        agents=agents+excluded.agents,
                        turns=turns+excluded.turns,
                        input_tokens=input_tokens+excluded.input_tokens,
                        input_known=input_known+excluded.input_known,
                        output_tokens=output_tokens+excluded.output_tokens,
                        output_known=output_known+excluded.output_known,
                        reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens,
                        reasoning_known=reasoning_known+excluded.reasoning_known,
                        cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,
                        cache_read_known=cache_read_known+excluded.cache_read_known,
                        cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens,
                        cache_write_known=cache_write_known+excluded.cache_write_known,
                        total_tokens=total_tokens+excluded.total_tokens,
                        total_known=total_known+excluded.total_known
                    """,
                    fields,
                )
                conn.execute(
                    """
                    INSERT INTO provider_usage_diagnostics(name, value, updated_at)
                    VALUES ('ingested_events', 1, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        value=value+1, updated_at=excluded.updated_at
                    """,
                    (time.time(),),
                )
                cutoff = (
                    datetime.now().astimezone().date() - timedelta(days=RETENTION_DAYS)
                ).isoformat()
                conn.execute("DELETE FROM provider_usage_events WHERE local_date < ?", (cutoff,))
                conn.execute("DELETE FROM provider_usage_daily WHERE local_date < ?", (cutoff,))
                conn.execute("DELETE FROM provider_usage_daily_agents WHERE local_date < ?", (cutoff,))
            return True
        except Exception:
            self.diagnostic_increment("store_errors")
            return False

    def summary(self, *, days: int = 365) -> Dict[str, Any]:
        bounded = max(1, min(int(days or 365), RETENTION_DAYS))
        start = (
            datetime.now().astimezone().date() - timedelta(days=bounded - 1)
        ).isoformat()

        with self._lock, closing(self._connect()) as conn:
            self._schema(conn)
            rows = conn.execute(
                """
                SELECT provider,
                    SUM(turns) turns,
                    SUM(input_tokens) input_tokens, SUM(input_known) input_known,
                    SUM(output_tokens) output_tokens, SUM(output_known) output_known,
                    SUM(reasoning_tokens) reasoning_tokens, SUM(reasoning_known) reasoning_known,
                    SUM(cache_read_tokens) cache_read_tokens, SUM(cache_read_known) cache_read_known,
                    SUM(cache_write_tokens) cache_write_tokens, SUM(cache_write_known) cache_write_known,
                    SUM(total_tokens) total_tokens, SUM(total_known) total_known
                FROM provider_usage_daily
                WHERE local_date >= ?
                GROUP BY provider
                ORDER BY provider
                """,
                (start,),
            ).fetchall()
            source_rows = conn.execute(
                """
                SELECT provider, GROUP_CONCAT(DISTINCT source) sources
                FROM provider_usage_daily
                WHERE local_date >= ?
                GROUP BY provider
                """,
                (start,),
            ).fetchall()
            agent_rows = conn.execute(
                """
                SELECT provider, COUNT(DISTINCT agent_id) agents
                FROM provider_usage_events
                WHERE local_date >= ? AND agent_id IS NOT NULL
                GROUP BY provider
                """,
                (start,),
            ).fetchall()
            model_rows = conn.execute(
                """
                SELECT provider, model, SUM(turns) turns,
                    SUM(total_tokens) total_tokens, SUM(total_known) total_known
                FROM provider_usage_daily
                WHERE local_date >= ? AND model <> ''
                GROUP BY provider, model
                ORDER BY provider, SUM(total_tokens) DESC
                """,
                (start,),
            ).fetchall()
            available_since = conn.execute(
                "SELECT MIN(local_date) FROM provider_usage_daily"
            ).fetchone()[0]
            diagnostics = {
                str(row["name"]): int(row["value"] or 0)
                for row in conn.execute(
                    "SELECT name, value FROM provider_usage_diagnostics"
                ).fetchall()
            }

        agent_counts = {str(row["provider"]): int(row["agents"] or 0) for row in agent_rows}
        sources: Dict[str, list[str]] = {}
        for row in source_rows:
            values = [part for part in str(row["sources"] or "").split(",") if part]
            sources[str(row["provider"])] = sorted(values)
        models: Dict[str, list[Dict[str, Any]]] = {}
        for row in model_rows:
            provider = str(row["provider"])
            turns = int(row["turns"] or 0)
            known = int(row["total_known"] or 0)
            models.setdefault(provider, []).append({
                "model": str(row["model"]),
                "turns": turns,
                "total_tokens": int(row["total_tokens"] or 0) if turns and known == turns else None,
            })

        providers: Dict[str, Any] = {}
        for row in rows:
            provider = str(row["provider"])
            turns = int(row["turns"] or 0)
            provider_sources = sources.get(provider, [])
            payload: Dict[str, Any] = {
                "provider": provider,
                "available": True,
                "turns": turns,
                "agents": agent_counts.get(provider, 0),
                "source": provider_sources[0] if len(provider_sources) == 1 else "mixed",
                "sources": provider_sources,
                "schema_version": SCHEMA_VERSION,
                "models": models.get(provider, []),
            }
            for component in COMPONENTS:
                known = int(row[f"{component}_known"] or 0)
                payload[f"{component}_tokens"] = (
                    int(row[f"{component}_tokens"] or 0)
                    if turns > 0 and known == turns else None
                )
                payload[f"{component}_known_turns"] = known
            providers[provider] = payload

        providers.setdefault("chatgpt", {
            "provider": "chatgpt",
            "available": False,
            "reason": "provider_usage_unavailable",
            "turns": 0,
            "agents": 0,
            "source": None,
            "sources": [],
            "schema_version": SCHEMA_VERSION,
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "total_tokens": None,
            "models": [],
        })
        return {
            "ok": True,
            "days": bounded,
            "available_since": available_since,
            "schema_version": SCHEMA_VERSION,
            "metric_name": "Provider Tokens",
            "metric_scope": (
                "Provider-native delegated-agent usage only. Exact provider reports "
                "remain separate from MCP Payload Tokens; provider-defined breakdowns can overlap."
            ),
            "fallback_policy": (
                "No reliable provider-native tokenizer fallback is configured for "
                "missing usage events; missing data remains unavailable."
            ),
            "providers": providers,
            "diagnostics": diagnostics,
        }


_STORE_LOCK = threading.Lock()
_STORE: Optional[ProviderUsageStore] = None
_STORE_PATH: Optional[Path] = None


def get_store() -> ProviderUsageStore:
    global _STORE, _STORE_PATH
    path = db_path()
    with _STORE_LOCK:
        if _STORE is None or _STORE_PATH != path:
            _STORE = ProviderUsageStore(path)
            _STORE_PATH = path
        return _STORE


def summary(*, days: int = 365) -> Dict[str, Any]:
    return get_store().summary(days=days)
