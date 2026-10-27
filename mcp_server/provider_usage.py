from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .runtime_settings import usage_privacy
from . import privacy_policy
from . import paths

SCHEMA_VERSION = 1
SOURCE_REPORT = "report"
SOURCE_NATIVE_TOKENIZER = "native_tokenizer"
SOURCE_ESTIMATE = "estimate"
RETENTION_DAYS = 400
COMPONENTS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")
REPORTING_PROVIDERS = frozenset({"codex", "opencode", "claude", "kimi", "grok", "antigravity", "pi", "kilo", "cline", "goose",
                                 "copilot"})


def db_path() -> Path:
    return paths.provider_usage_db()


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


def normalize_claude_usage(usage: Any) -> NormalizedUsage:
    if not isinstance(usage, Mapping):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    known = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
    if not (known & set(usage.keys())):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), Mapping) else {}
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    reasoning = _nonnegative_int(details.get("thinking_tokens"))
    cache_read = _nonnegative_int(usage.get("cache_read_input_tokens"))
    cache_write = _nonnegative_int(usage.get("cache_creation_input_tokens"))
    # Claude input_tokens exclude cached and cache-creation tokens, which are
    # billed separately; thinking tokens are a breakdown of output.
    parts = (input_tokens, output_tokens, cache_read, cache_write)
    total = sum(int(value or 0) for value in parts) if input_tokens is not None and output_tokens is not None else None
    return NormalizedUsage(
        input_tokens, output_tokens, reasoning, cache_read, cache_write,
        total, True, False,
    )


def normalize_grok_usage(usage: Any) -> NormalizedUsage:
    """Grok Build's `end` event: the whole run's usage. Like Claude, input excludes cached
    tokens and reasoning is part of output; total_tokens is reported."""
    if not isinstance(usage, Mapping):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    known = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "total_tokens"}
    if not (known & set(usage.keys())):
        return NormalizedUsage(None, None, None, None, None, None, False, False)
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    reasoning = _nonnegative_int(usage.get("reasoning_tokens"))
    cache_read = _nonnegative_int(usage.get("cache_read_input_tokens"))
    cache_write = _nonnegative_int(usage.get("cache_creation_input_tokens"))
    total = _nonnegative_int(usage.get("total_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = sum(int(value or 0) for value in (input_tokens, output_tokens, cache_read, cache_write))
    components = (input_tokens, output_tokens, reasoning, cache_read, cache_write)
    total_only = total is not None and all(value is None for value in components)
    return NormalizedUsage(
        input_tokens, output_tokens, reasoning, cache_read, cache_write,
        total, total is not None or any(value is not None for value in components), total_only,
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

            -- Which team, saved team (preset) and member each team agent ran as, kept as long as its
            -- tokens. Labels only: never a prompt. Tokens stay in provider_usage_events (joined by agent_id).
            CREATE TABLE IF NOT EXISTS provider_usage_team_agents (
                agent_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                team_title TEXT,
                preset_id TEXT,
                preset_name TEXT,
                member_id TEXT,
                member_name TEXT,
                provider TEXT,
                model TEXT,
                reasoning TEXT,
                role TEXT,
                task_id TEXT,
                task_title TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT,
                gate TEXT,
                local_date TEXT NOT NULL,
                timezone TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_usage_team_agents_preset
                ON provider_usage_team_agents(preset_id, local_date DESC);
            CREATE INDEX IF NOT EXISTS idx_provider_usage_team_agents_team
                ON provider_usage_team_agents(team_id);
            CREATE INDEX IF NOT EXISTS idx_provider_usage_events_agent
                ON provider_usage_events(agent_id);
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

    def _prune(self, conn: sqlite3.Connection) -> None:
        cutoff = (
            datetime.now().astimezone().date() - timedelta(days=usage_privacy()["retention_days"] - 1)
        ).isoformat()
        conn.execute("DELETE FROM provider_usage_events WHERE local_date < ?", (cutoff,))
        conn.execute("DELETE FROM provider_usage_daily WHERE local_date < ?", (cutoff,))
        conn.execute("DELETE FROM provider_usage_daily_agents WHERE local_date < ?", (cutoff,))
        conn.execute("DELETE FROM provider_usage_team_agents WHERE local_date < ?", (cutoff,))

    def clear(self) -> int:
        """Delete every stored provider usage row; returns the number removed."""
        with self._lock, closing(self._connect()) as conn:
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                removed = 0
                for table in ("provider_usage_events", "provider_usage_daily", "provider_usage_daily_agents",
                              "provider_usage_team_agents"):
                    removed += int(conn.execute(f"DELETE FROM {table}").rowcount or 0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    def ingest(self, record: UsageRecord) -> bool:
        if not usage_privacy()["enabled"] or not privacy_policy.keeps_records():
            return False
        provider = str(record.provider or "").strip().lower()
        if provider not in REPORTING_PROVIDERS:
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
                self._prune(conn)
            return True
        except Exception:
            self.diagnostic_increment("store_errors")
            return False

    def summary(self, *, days: int = 365) -> Dict[str, Any]:
        privacy = usage_privacy()
        bounded = max(1, min(int(days or 365), privacy["retention_days"]))
        start = (
            datetime.now().astimezone().date() - timedelta(days=bounded - 1)
        ).isoformat()

        with self._lock, closing(self._connect()) as conn:
            self._schema(conn)
            with conn:
                self._prune(conn)
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
            team_rows = conn.execute(
                """
                SELECT e.provider, SUM(e.total_tokens) tokens
                FROM provider_usage_events e
                JOIN provider_usage_team_agents t ON t.agent_id = e.agent_id
                WHERE e.local_date >= ? AND t.preset_id IS NOT NULL
                GROUP BY e.provider
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
        team_tokens = {str(row["provider"]): int(row["tokens"] or 0) for row in team_rows}
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
                # How many of these tokens saved teams (Settings › Agents › Teams) used; Usage › Teams has them.
                "team_tokens": team_tokens.get(provider, 0),
            }
            for component in COMPONENTS:
                known = int(row[f"{component}_known"] or 0)
                payload[f"{component}_tokens"] = (
                    int(row[f"{component}_tokens"] or 0)
                    if turns > 0 and known == turns else None
                )
                payload[f"{component}_known_turns"] = known
            providers[provider] = payload

        return {
            "ok": True,
            "days": bounded,
            "metering_enabled": privacy["enabled"],
            "retention_days": privacy["retention_days"],
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

    # Teams: which team, saved team (Settings › Agents › Teams) and member each team agent ran as.

    def record_team_agent(self, attribution: Mapping[str, Any]) -> bool:
        """Remember who a team agent ran as, once per agent; a second call for the same agent changes nothing."""
        if not usage_privacy()["enabled"] or not privacy_policy.keeps_records():
            return False
        agent_id = _label(attribution.get("agent_id"), 64)
        team_id = _label(attribution.get("team_id"), 64)
        started = attribution.get("started_at")
        if not agent_id or not team_id or isinstance(started, bool) or not isinstance(started, (int, float)):
            return False
        local_date, timezone_name = _local_bucket(float(started))
        values = [agent_id, team_id] + [
            _label(attribution.get(key), limit) for key, limit in _TEAM_AGENT_LABELS
        ] + [float(started), local_date, timezone_name]
        try:
            with self._lock, closing(self._connect()) as conn, conn:
                self._schema(conn)
                inserted = conn.execute(
                    f"""
                    INSERT INTO provider_usage_team_agents (
                        agent_id, team_id, {", ".join(key for key, _ in _TEAM_AGENT_LABELS)},
                        started_at, local_date, timezone
                    ) VALUES ({", ".join("?" for _ in values)})
                    ON CONFLICT(agent_id) DO NOTHING
                    """,
                    values,
                )
                return inserted.rowcount == 1
        except Exception:
            self.diagnostic_increment("store_errors")
            return False

    def finish_team_agent(self, agent_id: str, *, started_at: Optional[float] = None, ended_at: Optional[float] = None,
                          status: Optional[str] = None, gate: Optional[str] = None) -> bool:
        """How a recorded team agent ended: when it ran, its status and, for a reviewer, its gate (pass/fail)."""
        clean = _label(agent_id, 64)
        if not clean:
            return False

        def number(value: Any) -> Optional[float]:
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        try:
            with self._lock, closing(self._connect()) as conn, conn:
                self._schema(conn)
                updated = conn.execute(
                    """
                    UPDATE provider_usage_team_agents SET
                        started_at=COALESCE(?, started_at), ended_at=COALESCE(?, ended_at),
                        status=COALESCE(?, status), gate=COALESCE(?, gate)
                    WHERE agent_id=?
                    """,
                    (number(started_at), number(ended_at), _label(status, 32), _label(gate, 16), clean),
                )
                return updated.rowcount == 1
        except Exception:
            self.diagnostic_increment("store_errors")
            return False

    def team_agent_states(self) -> Dict[str, bool]:
        """Every recorded team agent and whether its end is recorded."""
        with self._lock, closing(self._connect()) as conn:
            self._schema(conn)
            rows = conn.execute("SELECT agent_id, ended_at FROM provider_usage_team_agents").fetchall()
        return {str(row["agent_id"]): row["ended_at"] is not None for row in rows}

    def team_summary(self, *, days: int = 365,
                     presets: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
        """Tokens by saved team, member, day and run, read from the provider events of each team agent.

        `presets` is the saved teams now in Settings (id -> {"name", "member_ids"}); one that is gone keeps
        its history with exists=False. None means the saved teams could not be read, so none is shown as deleted. Teams started
        without a saved team are counted together in other_teams.
        """
        privacy = usage_privacy()
        bounded = max(1, min(int(days or 365), privacy["retention_days"]))
        start = (datetime.now().astimezone().date() - timedelta(days=bounded - 1)).isoformat()
        with self._lock, closing(self._connect()) as conn:
            self._schema(conn)
            with conn:
                self._prune(conn)
            agent_rows = conn.execute(
                "SELECT * FROM provider_usage_team_agents ORDER BY started_at, agent_id"
            ).fetchall()
            token_rows = conn.execute(
                """
                SELECT e.agent_id, e.local_date, SUM(e.total_tokens) tokens,
                    COUNT(*) turns, COUNT(e.total_tokens) known
                FROM provider_usage_events e
                JOIN provider_usage_team_agents t ON t.agent_id = e.agent_id
                WHERE e.local_date >= ?
                GROUP BY e.agent_id, e.local_date
                """,
                (start,),
            ).fetchall()
            provider_rows = conn.execute(
                """
                SELECT e.provider,
                    SUM(e.total_tokens) tokens,
                    SUM(CASE WHEN t.preset_id IS NOT NULL THEN e.total_tokens END) team_tokens
                FROM provider_usage_events e
                LEFT JOIN provider_usage_team_agents t ON t.agent_id = e.agent_id
                WHERE e.local_date >= ?
                GROUP BY e.provider
                """,
                (start,),
            ).fetchall()
            available_since = conn.execute(
                "SELECT MIN(local_date) FROM provider_usage_team_agents"
            ).fetchone()[0]

        tokens_by_agent: Dict[str, int] = {}
        daily_by_agent: Dict[str, Dict[str, int]] = {}
        unreported_turns = 0
        for row in token_rows:
            agent_id = str(row["agent_id"])
            amount = int(row["tokens"] or 0)
            tokens_by_agent[agent_id] = tokens_by_agent.get(agent_id, 0) + amount
            daily_by_agent.setdefault(agent_id, {})[str(row["local_date"])] = amount
            unreported_turns += int(row["turns"] or 0) - int(row["known"] or 0)

        runs: Dict[str, List[Dict[str, Any]]] = {}
        for row in agent_rows:
            runs.setdefault(str(row["team_id"]), []).append(dict(row))
        groups: Dict[str, List[Dict[str, Any]]] = {}
        other = {"tokens": 0, "runs": 0}
        for team_id, team_agents in runs.items():
            if not any(agent["local_date"] >= start or agent["agent_id"] in tokens_by_agent for agent in team_agents):
                continue
            run = _team_run(team_id, team_agents, tokens_by_agent)
            preset_id = next((agent["preset_id"] for agent in team_agents if agent["preset_id"]), None)
            if preset_id:
                run["_agents"] = team_agents
                groups.setdefault(str(preset_id), []).append(run)
            else:
                other["tokens"] += run["tokens"]
                other["runs"] += 1

        teams = [
            _team_group(preset_id, group, daily_by_agent, presets)
            for preset_id, group in groups.items()
        ]
        teams.sort(key=lambda team: (not team["exists"], -team["tokens"], -(team["last_run_at"] or 0)))
        total = sum(team["tokens"] for team in teams)
        for team in teams:
            team["share"] = team["tokens"] / total if total else 0.0
        return {
            "ok": True,
            "days": bounded,
            "metering_enabled": privacy["enabled"],
            "retention_days": privacy["retention_days"],
            "available_since": available_since,
            "total_tokens": total,
            "runs": sum(team["runs"] for team in teams),
            "teams": teams,
            "other_teams": other,
            "unreported_turns": unreported_turns,
            # Each provider's delegated tokens in the period and how many of them saved teams used.
            "providers": {
                str(row["provider"]): {"tokens": int(row["tokens"] or 0), "team_tokens": int(row["team_tokens"] or 0)}
                for row in provider_rows
            },
        }


# The labels a team agent is remembered by, and how long each may be.
_TEAM_AGENT_LABELS = (
    ("team_title", 120), ("preset_id", 64), ("preset_name", 60), ("member_id", 64), ("member_name", 60),
    ("provider", 32), ("model", 120), ("reasoning", 32), ("role", 32), ("task_id", 64), ("task_title", 120),
)


def _label(value: Any, limit: int) -> Optional[str]:
    text = " ".join(str(value).split()) if value is not None else ""
    return text[:limit] or None


def _member_key(agent: Mapping[str, Any]) -> str:
    """A member is its saved-team member id; without one (an older or ad hoc row), its name or task."""
    if agent.get("member_id"):
        return str(agent["member_id"])
    return "name:" + str(agent.get("member_name") or agent.get("task_title") or agent.get("task_id") or "?")


def _team_run(team_id: str, agents: List[Dict[str, Any]], tokens: Mapping[str, int]) -> Dict[str, Any]:
    """One team run: its members (each with every agent it took, revisions included) and how it ended."""
    members: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        key = _member_key(agent)
        member = members.setdefault(key, {"key": key, "tokens": 0, "duration_s": None, "agents": 0})
        member.update({
            "name": agent["member_name"] or agent["task_title"] or agent["task_id"],
            "provider": agent["provider"], "model": agent["model"], "reasoning": agent["reasoning"],
            "role": agent["role"], "status": agent["status"] if agent["ended_at"] is not None else "running",
        })
        if agent["gate"]:
            member["gate"] = agent["gate"]
        member["tokens"] += tokens.get(agent["agent_id"], 0)
        member["agents"] += 1
        if agent["ended_at"] is not None:
            member["duration_s"] = (member["duration_s"] or 0) + max(0.0, agent["ended_at"] - agent["started_at"])
    rows = list(members.values())
    started = min(agent["started_at"] for agent in agents)
    finished = all(agent["ended_at"] is not None for agent in agents)
    ended = max(agent["ended_at"] for agent in agents) if finished else None
    gates = [row for row in rows if row.get("gate")]
    return {
        "team_id": team_id,
        "title": agents[0]["team_title"],
        "started_at": started,
        "ended_at": ended,
        "duration_s": max(0.0, ended - started) if ended is not None else None,
        "tokens": sum(row["tokens"] for row in rows),
        "outcome": {
            "members": len(rows),
            "completed": sum(1 for row in rows if row["status"] == "completed"),
            "running": sum(1 for row in rows if row["status"] == "running"),
            "failed": sum(1 for row in rows if row["status"] not in ("completed", "running")),
            "gates": len(gates),
            "gates_passed": sum(1 for row in gates if row["gate"] == "pass"),
        },
        "members": rows,
    }


_TEAM_RUN_LIMIT = 50


def _team_group(preset_id: str, runs: List[Dict[str, Any]], daily_by_agent: Mapping[str, Mapping[str, int]],
                presets: Optional[Mapping[str, Mapping[str, Any]]]) -> Dict[str, Any]:
    """A saved team over the period: its tokens, members, days and runs, most recent run first."""
    runs.sort(key=lambda run: run["started_at"], reverse=True)
    latest = runs[0]["_agents"]
    exists = presets is None or preset_id in presets
    saved = (presets or {}).get(preset_id) or {}
    name = saved.get("name") or next(
        (agent["preset_name"] for agent in reversed(latest) if agent["preset_name"]), None) or preset_id
    # Members in the saved team's order, then the latest run's order for members it no longer has.
    order = {key: index for index, key in enumerate(saved.get("member_ids") or [])}
    for row in runs[0]["members"]:
        order.setdefault(row["key"], len(order))
    members: Dict[str, Dict[str, Any]] = {}
    daily: Dict[str, Dict[str, int]] = {}
    # Oldest run first, so each member keeps the provider, model and name it ran with last.
    for run in reversed(runs):
        for row in run["members"]:
            member = members.setdefault(row["key"], {"key": row["key"], "tokens": 0, "runs": 0})
            member.update({key: row[key] for key in ("name", "provider", "model", "reasoning", "role")})
            member["tokens"] += row["tokens"]
            member["runs"] += 1
        for agent in run.pop("_agents"):
            for day, amount in daily_by_agent.get(agent["agent_id"], {}).items():
                parts = daily.setdefault(day, {})
                key = _member_key(agent)
                parts[key] = parts.get(key, 0) + amount
    tokens = sum(run["tokens"] for run in runs)
    ordered = sorted(members.values(), key=lambda member: (order.get(member["key"], len(order)), -member["tokens"]))
    for run in runs:
        run["members"].sort(key=lambda row: order.get(row["key"], len(order)))
    for member in ordered:
        member["share"] = member["tokens"] / tokens if tokens else 0.0
    durations = [run["duration_s"] for run in runs if run["duration_s"] is not None]
    return {
        "id": preset_id,
        "name": name,
        "exists": exists,
        "tokens": tokens,
        "runs": len(runs),
        "last_run_at": runs[0]["started_at"],
        "average_duration_s": sum(durations) / len(durations) if durations else None,
        "gates": sum(run["outcome"]["gates"] for run in runs),
        "gates_passed": sum(run["outcome"]["gates_passed"] for run in runs),
        "members": ordered,
        "daily": [{"date": day, "members": daily[day]} for day in sorted(daily)],
        "run_list": runs[:_TEAM_RUN_LIMIT],
    }


_AGENT_TOKEN_KINDS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")


def _agent_totals_query(count: int) -> str:
    sums = ", ".join(
        f"SUM({kind}_tokens) {kind}, COUNT({kind}_tokens) {kind}_known" for kind in _AGENT_TOKEN_KINDS
    )
    return (f"SELECT agent_id, {sums} FROM provider_usage_events "
            f"WHERE agent_id IN ({', '.join('?' for _ in range(count))}) GROUP BY agent_id")


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


def agent_totals(agent_ids: List[str]) -> Dict[str, Dict[str, Optional[int]]]:
    """Token totals by kind for each agent that has usage rows; a kind no row reported is None."""
    ids = [str(agent_id) for agent_id in dict.fromkeys(agent_ids) if agent_id][:500]
    if not ids:
        return {}
    store = get_store()
    with store._lock, closing(store._connect()) as conn:
        store._schema(conn)
        rows = conn.execute(_agent_totals_query(len(ids)), ids).fetchall()
    return {
        str(row["agent_id"]): {
            kind: (int(row[kind] or 0) if row[f"{kind}_known"] else None) for kind in _AGENT_TOKEN_KINDS
        }
        for row in rows
    }


def clear() -> int:
    return get_store().clear()


def record_team_agent(attribution: Mapping[str, Any]) -> bool:
    return get_store().record_team_agent(attribution)


def finish_team_agent(agent_id: str, **ending: Any) -> bool:
    return get_store().finish_team_agent(agent_id, **ending)


def team_summary(*, days: int = 365, presets: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    return get_store().team_summary(days=days, presets=presets)
