from __future__ import annotations

import base64
import json
import math
import os
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .runtime_settings import usage_privacy

TOKENIZER_ID = "mac_mcp_payload_v1"
MEASUREMENT_CLASS = "canonical_json_text_v1"
DEFAULT_USAGE_RETENTION_DAYS = 400
DEFAULT_USAGE_QUEUE_SIZE = 2048
# Raw payloads waiting for the quiet window; past this the batch is measured and written anyway.
DEFAULT_USAGE_BATCH_BYTES = 16 * 1024 * 1024
_USAGE_IDLE_EXIT_S = 2.0
_USAGE_QUIET_WINDOW_S = 0.5

_ENCODED_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_WORD_OR_PUNCT_RE = re.compile(r"(\w+)|[^\w\s]", re.UNICODE)
_IMAGE_TYPES = {"image", "image_url", "input_image"}
_AUDIO_TYPES = {"audio", "input_audio"}
_BINARY_KEYS = {
    "data", "bytes", "blob", "binary", "image_data", "audio_data", "base64",
    "image", "screenshot", "thumbnail",
}
_LATENCY_BUCKETS = (
    ("lt_10", 10),
    ("lt_50", 50),
    ("lt_100", 100),
    ("lt_250", 250),
    ("lt_500", 500),
    ("lt_1000", 1000),
    ("lt_5000", 5000),
    ("gte_5000", None),
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return min(max(int(raw), minimum), maximum)
    except ValueError:
        return default


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (dict, list, tuple, set, str, bytes, bytearray, memoryview, bool, int, float)) or value is None:
        return value
    try:
        return vars(value)
    except Exception:
        return str(value)


def _encoded_blob_bytes(text: str) -> Optional[int]:
    stripped = text.strip()
    if stripped.startswith("data:") and "," in stripped:
        header, encoded = stripped.split(",", 1)
        if ";base64" in header.lower():
            try:
                return len(base64.b64decode(encoded, validate=False))
            except Exception:
                return max(0, (len(encoded) * 3) // 4)
    compact = stripped.replace("\n", "").replace("\r", "")
    if len(compact) >= 768 and _ENCODED_RE.fullmatch(compact):
        return max(0, (len(compact.rstrip("=")) * 3) // 4)
    return None


@dataclass(frozen=True)
class PayloadMetrics:
    canonical_bytes: int
    tokens: int
    image_count: int
    binary_bytes: int


def canonical_payload(value: Any) -> tuple[Any, int, int]:
    """Return canonical JSON-safe payload plus image/binary counters.

    Binary/image bodies are replaced with small sentinels before tokenization so
    base64 blobs cannot masquerade as text tokens. No canonical text is persisted.
    """
    image_count = 0
    binary_bytes = 0

    def walk(item: Any, *, key: Optional[str] = None, depth: int = 0) -> Any:
        nonlocal image_count, binary_bytes
        if depth > 32:
            return "[depth-limit]"
        item = _jsonable(item)
        if isinstance(item, float) and not math.isfinite(item):
            # JSON has no NaN or Infinity: measured as the text a JSON writer shows for them (#93).
            return "NaN" if item != item else ("Infinity" if item > 0 else "-Infinity")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, (bytes, bytearray, memoryview)):
            size = len(item)
            binary_bytes += size
            return {"$binary_bytes": size}
        if isinstance(item, str):
            if item.startswith("data:image/"):
                encoded_size = _encoded_blob_bytes(item) or 0
                image_count += 1
                binary_bytes += encoded_size
                return {"$image": True, "bytes": encoded_size}
            encoded_size = _encoded_blob_bytes(item)
            if encoded_size is not None and (key or "").lower() in _BINARY_KEYS:
                binary_bytes += encoded_size
                return {"$binary_bytes": encoded_size}
            return item
        if isinstance(item, Mapping):
            type_name = str(item.get("type") or "").strip().lower()
            mime = str(item.get("mimeType") or item.get("mime_type") or "").strip().lower()
            looks_image = type_name in _IMAGE_TYPES or mime.startswith("image/")
            looks_audio = type_name in _AUDIO_TYPES or mime.startswith("audio/")
            if looks_image or looks_audio:
                count_as_image = looks_image
                raw_size = 0
                for candidate in ("data", "bytes", "base64", "image_data", "audio_data"):
                    if candidate not in item:
                        continue
                    value = item.get(candidate)
                    if isinstance(value, (bytes, bytearray, memoryview)):
                        raw_size = len(value)
                    elif isinstance(value, str):
                        raw_size = _encoded_blob_bytes(value) or len(value.encode("utf-8"))
                    break
                if count_as_image:
                    image_count += 1
                binary_bytes += raw_size
                return {
                    "$image" if looks_image else "$audio": True,
                    "bytes": raw_size,
                    "mime": mime or None,
                }
            out: Dict[str, Any] = {}
            for original_key, original_value in sorted(
                item.items(), key=lambda pair: str(pair[0])
            ):
                child_key = str(original_key)
                # Preserve keys because field names are part of the MCP text payload.
                out[child_key] = walk(original_value, key=child_key, depth=depth + 1)
            return out
        if isinstance(item, (list, tuple, set)):
            return [walk(child, depth=depth + 1) for child in list(item)]
        return str(item)

    return walk(value), image_count, binary_bytes


def canonical_json_text(value: Any) -> tuple[str, int, int]:
    payload, image_count, binary_bytes = canonical_payload(value)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return text, image_count, binary_bytes


def count_payload_tokens(text: str) -> int:
    """Versioned token-like metric, intentionally not provider billing tokens.

    Runs of Unicode word characters are chunked by UTF-8 width (4 bytes/token);
    punctuation is one token and whitespace carries no token. The algorithm is
    deterministic, dependency-free, and pinned by TOKENIZER_ID.
    """
    total = 0
    for match in _WORD_OR_PUNCT_RE.finditer(text):
        word = match.group(1)
        if word is None:
            total += 1
        else:
            width = len(word) if word.isascii() else len(word.encode("utf-8", "surrogatepass"))
            total += (width + 3) // 4
    return total


def measure_payload(value: Any) -> PayloadMetrics:
    text, image_count, binary_bytes = canonical_json_text(value)
    return PayloadMetrics(
        canonical_bytes=len(text.encode("utf-8")),
        tokens=count_payload_tokens(text),
        image_count=image_count,
        binary_bytes=binary_bytes,
    )


def ensure_usage_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_daily (
            local_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            tool TEXT NOT NULL,
            actor_class TEXT NOT NULL,
            tokenizer_id TEXT NOT NULL,
            measurement_class TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            input_bytes INTEGER NOT NULL DEFAULT 0,
            output_bytes INTEGER NOT NULL DEFAULT 0,
            image_count INTEGER NOT NULL DEFAULT 0,
            binary_bytes INTEGER NOT NULL DEFAULT 0,
            duration_total_ms INTEGER NOT NULL DEFAULT 0,
            latency_lt_10 INTEGER NOT NULL DEFAULT 0,
            latency_lt_50 INTEGER NOT NULL DEFAULT 0,
            latency_lt_100 INTEGER NOT NULL DEFAULT 0,
            latency_lt_250 INTEGER NOT NULL DEFAULT 0,
            latency_lt_500 INTEGER NOT NULL DEFAULT 0,
            latency_lt_1000 INTEGER NOT NULL DEFAULT 0,
            latency_lt_5000 INTEGER NOT NULL DEFAULT 0,
            latency_gte_5000 INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (
                local_date, timezone, tool, actor_class,
                tokenizer_id, measurement_class
            )
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_daily_date ON usage_daily(local_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_daily_tool_date ON usage_daily(tool, local_date DESC)"
    )
    # What memory and skill calls came to (#110): counts per outcome, a skill's name, nothing else.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_skill_daily (
            local_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            actor_class TEXT NOT NULL,
            tool TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (local_date, timezone, actor_class, tool, outcome, detail)
        )
        """
    )


MEMORY_SKILL_TOOLS = frozenset({"memory_search", "memory_add", "memory_update", "skill_search", "skill_get"})


def _payload_dict(value: Any) -> Dict[str, Any]:
    """The tool's own result dict from what MCP returns: a dict, a {"result": {...}} wrapper, a
    (content, structured) pair, or text content holding JSON."""
    if isinstance(value, dict):
        inner = value.get("result")
        if isinstance(inner, dict) and "ok" not in value:
            return inner
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return _payload_dict(_json_or_none(value["text"]))
        return value
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[1], dict):
            return _payload_dict(value[1])
        for item in value:
            found = _payload_dict(item)
            if found:
                return found
        return {}
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return _payload_dict(_json_or_none(text))
    return {}


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def memory_skill_outcome(tool: str, status: str, arguments: Any, result: Any,
                         error: Optional[str] = None) -> Optional[tuple[str, str]]:
    """(outcome, detail) for a memory or skill call; None for any other tool.

    Outcomes: matched / no_match for searches, saved / already_saved / updated / unchanged for
    writes, rejected_secret when a secret was refused, loaded / not_found for skill_get, error
    otherwise. detail is a skill's name for skill_get and empty for everything else."""
    # A call through tool_invoke is recorded twice, as tool_invoke and as the tool that ran; only the
    # second counts here, so tool_invoke itself is never a memory or skill call.
    if tool not in MEMORY_SKILL_TOOLS:
        return None
    if status != "success":
        text = str(error or "")
        if "MEMORY_CONTAINS_SECRET" in text:
            return "rejected_secret", ""
        if tool == "skill_get" and ("not found" in text.lower() or "404" in text):
            return "not_found", ""
        return "error", ""
    data = _payload_dict(result)
    if tool in {"memory_search", "skill_search"}:
        return ("matched" if int(data.get("count") or 0) > 0 else "no_match"), ""
    if tool == "memory_add":
        return ("already_saved" if data.get("action") == "already_saved" else "saved"), ""
    if tool == "memory_update":
        return ("unchanged" if data.get("action") == "unchanged" else "updated"), ""
    args = arguments if isinstance(arguments, dict) else {}
    return "loaded", str(data.get("name") or args.get("name") or "").strip()[:80]


@dataclass
class UsageSample:
    timestamp: float
    tool: str
    actor_class: str
    status: str
    duration_ms: int
    arguments: Any
    result: Any
    # Approximate payload bytes, when the producer knows them; bounds what the batch holds.
    size_hint: int = 0
    # The error text of a failed call, read only to tell outcomes apart (#110); never stored.
    error: Optional[str] = None


class UsageCollector:
    def __init__(
        self,
        db_path: Path,
        *,
        queue_size: Optional[int] = None,
        batch_bytes: Optional[int] = None,
        enabled: Optional[bool] = None,
        local_bucket: Optional[Callable[[float], tuple[str, str]]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.enabled = _env_bool("MAC_MCP_USAGE_ENABLED", True) if enabled is None else bool(enabled)
        self.queue_size = queue_size or _env_int(
            "MAC_MCP_USAGE_QUEUE_SIZE", DEFAULT_USAGE_QUEUE_SIZE, 16, 100_000
        )
        self.batch_bytes = batch_bytes or _env_int(
            "MAC_MCP_USAGE_BATCH_BYTES", DEFAULT_USAGE_BATCH_BYTES, 1_000, 1 << 30
        )
        self._queue: queue.Queue[UsageSample] = queue.Queue(maxsize=self.queue_size)
        self._local_bucket = local_bucket or self._default_local_bucket
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._dropped = 0
        self._worker_errors = 0
        self._samples_skipped = 0
        self._processed = 0

    @staticmethod
    def _default_local_bucket(timestamp: float) -> tuple[str, str]:
        dt = datetime.fromtimestamp(float(timestamp)).astimezone()
        offset = dt.strftime("%z")
        label = dt.tzname() or "local"
        return dt.date().isoformat(), f"{label}{offset}"

    def diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            worker_alive = bool(self._worker and self._worker.is_alive())
            return {
                "enabled": self.enabled,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self.queue_size,
                "queue_dropped": self._dropped,
                "worker_errors": self._worker_errors,
                "samples_skipped": self._samples_skipped,
                "processed": self._processed,
                "worker_alive": worker_alive,
                "tokenizer_id": TOKENIZER_ID,
                "measurement_class": MEASUREMENT_CLASS,
            }

    def submit(self, sample: UsageSample) -> bool:
        if not self.enabled or not usage_privacy()["enabled"]:
            return False
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="mac-mcp-usage",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=_USAGE_IDLE_EXIT_S)
            except queue.Empty:
                return

            # Usage is lower priority than returning tool results. Keep draining
            # while the producer is active; tokenize/write only after a bounded
            # quiet window. The queue itself is bounded, so overload drops usage
            # samples (with diagnostics) instead of slowing the user's tool call. A producer
            # that never pauses would keep every raw result alive, so past batch_bytes the
            # batch is written without waiting for the quiet window.
            batch = [first]
            held = max(0, int(first.size_hint or 0))
            while len(batch) < self.queue_size and held < self.batch_bytes:
                try:
                    sample = self._queue.get(timeout=_USAGE_QUIET_WINDOW_S)
                except queue.Empty:
                    break
                batch.append(sample)
                held += max(0, int(sample.size_hint or 0))

            while len(batch) < self.queue_size and held < self.batch_bytes:
                try:
                    sample = self._queue.get_nowait()
                except queue.Empty:
                    break
                batch.append(sample)
                held += max(0, int(sample.size_hint or 0))

            try:
                self._write_batch(batch)
                with self._lock:
                    self._processed += len(batch)
            except Exception:
                with self._lock:
                    self._worker_errors += len(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def wait_idle(self, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.01, float(timeout_s))
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def _write_batch(self, samples: Iterable[UsageSample]) -> None:
        rows = []
        outcomes = []
        for sample in samples:
            try:
                outcome = memory_skill_outcome(str(sample.tool or ""), str(sample.status or ""), sample.arguments,
                                               sample.result, sample.error)
            except Exception:
                outcome = None
            if outcome is not None:
                local_date, timezone = self._local_bucket(sample.timestamp)
                outcomes.append((local_date, timezone, str(sample.actor_class or "primary"), str(sample.tool),
                                 outcome[0], outcome[1]))
            try:
                input_metrics = measure_payload(sample.arguments)
                output_metrics = measure_payload(sample.result)
            except Exception:
                # One sample that cannot be measured is left out, not the batch around it (#93).
                with self._lock:
                    self._worker_errors += 1
                    self._samples_skipped += 1
                continue
            local_date, timezone = self._local_bucket(sample.timestamp)
            latency = max(0, int(sample.duration_ms or 0))
            buckets = {name: 0 for name, _ in _LATENCY_BUCKETS}
            for name, ceiling in _LATENCY_BUCKETS:
                if ceiling is None or latency < ceiling:
                    buckets[name] = 1
                    break
            rows.append((
                local_date,
                timezone,
                str(sample.tool or "unknown"),
                str(sample.actor_class or "primary"),
                TOKENIZER_ID,
                MEASUREMENT_CLASS,
                1,
                1 if sample.status == "success" else 0,
                0 if sample.status == "success" else 1,
                input_metrics.tokens,
                output_metrics.tokens,
                input_metrics.canonical_bytes,
                output_metrics.canonical_bytes,
                input_metrics.image_count + output_metrics.image_count,
                input_metrics.binary_bytes + output_metrics.binary_bytes,
                latency,
                buckets["lt_10"],
                buckets["lt_50"],
                buckets["lt_100"],
                buckets["lt_250"],
                buckets["lt_500"],
                buckets["lt_1000"],
                buckets["lt_5000"],
                buckets["gte_5000"],
            ))

        conn = sqlite3.connect(self.db_path, timeout=2.0)
        try:
            with conn:
                ensure_usage_schema(conn)
                conn.executemany(
                    """
                    INSERT INTO usage_daily (
                        local_date, timezone, tool, actor_class,
                        tokenizer_id, measurement_class,
                        calls, success_count, error_count,
                        input_tokens, output_tokens, input_bytes, output_bytes,
                        image_count, binary_bytes, duration_total_ms,
                        latency_lt_10, latency_lt_50, latency_lt_100, latency_lt_250,
                        latency_lt_500, latency_lt_1000, latency_lt_5000, latency_gte_5000
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(
                        local_date, timezone, tool, actor_class,
                        tokenizer_id, measurement_class
                    ) DO UPDATE SET
                        calls=calls+excluded.calls,
                        success_count=success_count+excluded.success_count,
                        error_count=error_count+excluded.error_count,
                        input_tokens=input_tokens+excluded.input_tokens,
                        output_tokens=output_tokens+excluded.output_tokens,
                        input_bytes=input_bytes+excluded.input_bytes,
                        output_bytes=output_bytes+excluded.output_bytes,
                        image_count=image_count+excluded.image_count,
                        binary_bytes=binary_bytes+excluded.binary_bytes,
                        duration_total_ms=duration_total_ms+excluded.duration_total_ms,
                        latency_lt_10=latency_lt_10+excluded.latency_lt_10,
                        latency_lt_50=latency_lt_50+excluded.latency_lt_50,
                        latency_lt_100=latency_lt_100+excluded.latency_lt_100,
                        latency_lt_250=latency_lt_250+excluded.latency_lt_250,
                        latency_lt_500=latency_lt_500+excluded.latency_lt_500,
                        latency_lt_1000=latency_lt_1000+excluded.latency_lt_1000,
                        latency_lt_5000=latency_lt_5000+excluded.latency_lt_5000,
                        latency_gte_5000=latency_gte_5000+excluded.latency_gte_5000
                    """,
                    rows,
                )
                conn.executemany(
                    """
                    INSERT INTO memory_skill_daily (local_date, timezone, actor_class, tool, outcome, detail, count)
                    VALUES (?,?,?,?,?,?,1)
                    ON CONFLICT(local_date, timezone, actor_class, tool, outcome, detail)
                    DO UPDATE SET count=count+1
                    """,
                    outcomes,
                )
                _prune_usage(conn)
        finally:
            conn.close()


def _usage_cutoff(retention_days: int) -> str:
    return (datetime.now().astimezone().date() - timedelta(days=retention_days - 1)).isoformat()


def _prune_usage(conn: sqlite3.Connection) -> None:
    cutoff = _usage_cutoff(usage_privacy()["retention_days"])
    conn.execute("DELETE FROM usage_daily WHERE local_date < ?", (cutoff,))
    conn.execute("DELETE FROM memory_skill_daily WHERE local_date < ?", (cutoff,))


def clear_usage(db_path: Path) -> int:
    """Delete every stored tool-usage aggregate; returns the number of rows removed."""
    conn = sqlite3.connect(Path(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA secure_delete=ON")
        with conn:
            ensure_usage_schema(conn)
            removed = conn.execute("DELETE FROM usage_daily").rowcount
            removed += conn.execute("DELETE FROM memory_skill_daily").rowcount
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return int(removed or 0)
    finally:
        conn.close()


def _latency_percentile(
    bucket_counts: Mapping[str, int], percentile: float
) -> tuple[Optional[int], Optional[str]]:
    total = sum(max(0, int(bucket_counts.get(name, 0))) for name, _ in _LATENCY_BUCKETS)
    if total <= 0:
        return None, None
    threshold = max(1, int(math.ceil(total * percentile)))
    seen = 0
    for name, ceiling in _LATENCY_BUCKETS:
        seen += max(0, int(bucket_counts.get(name, 0)))
        if seen >= threshold:
            if ceiling is None:
                return 5000, "gte"
            return int(ceiling), "lt"
    return 5000, "gte"


def query_usage_summary(
    db_path: Path,
    *,
    days: int = 365,
    actor_class: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    privacy = usage_privacy()
    bounded_days = max(1, min(int(days or 365), privacy["retention_days"]))
    today = datetime.now().astimezone().date()
    start_date = (today - timedelta(days=bounded_days - 1)).isoformat()

    conn = sqlite3.connect(Path(db_path), timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            ensure_usage_schema(conn)
            # A shorter retention takes effect at once, not only on the next write.
            _prune_usage(conn)
        where = ["local_date >= ?"]
        params: list[Any] = [start_date]
        if actor_class in {"primary", "scoped_subagent"}:
            where.append("actor_class = ?")
            params.append(actor_class)
        where_sql = " AND ".join(where)
        sums = """
            SUM(calls) AS calls,
            SUM(success_count) AS success_count,
            SUM(error_count) AS error_count,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(input_bytes) AS input_bytes,
            SUM(output_bytes) AS output_bytes,
            SUM(image_count) AS image_count,
            SUM(binary_bytes) AS binary_bytes,
            SUM(duration_total_ms) AS duration_total_ms,
            SUM(latency_lt_10) AS latency_lt_10,
            SUM(latency_lt_50) AS latency_lt_50,
            SUM(latency_lt_100) AS latency_lt_100,
            SUM(latency_lt_250) AS latency_lt_250,
            SUM(latency_lt_500) AS latency_lt_500,
            SUM(latency_lt_1000) AS latency_lt_1000,
            SUM(latency_lt_5000) AS latency_lt_5000,
            SUM(latency_gte_5000) AS latency_gte_5000
        """
        daily_rows = conn.execute(
            f"SELECT local_date, {sums} FROM usage_daily WHERE {where_sql} GROUP BY local_date ORDER BY local_date",
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT {sums} FROM usage_daily WHERE {where_sql}",
            params,
        ).fetchone()
        top_tools = conn.execute(
            f"""
            SELECT tool, {sums}
            FROM usage_daily
            WHERE {where_sql}
            GROUP BY tool
            ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC, SUM(calls) DESC, tool ASC
            LIMIT 8
            """,
            params,
        ).fetchall()
        available_since = conn.execute("SELECT MIN(local_date) FROM usage_daily").fetchone()[0]
        memory_rows = conn.execute(
            f"SELECT tool, outcome, detail, SUM(count) AS count FROM memory_skill_daily WHERE {where_sql} "
            "GROUP BY tool, outcome, detail ORDER BY tool, outcome, detail",
            params,
        ).fetchall()
    finally:
        conn.close()

    def row_payload(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
        if row is None:
            return {
                "calls": 0, "success_count": 0, "error_count": 0,
                "input_tokens": 0, "output_tokens": 0,
                "input_bytes": 0, "output_bytes": 0,
                "image_count": 0, "binary_bytes": 0,
                "duration_total_ms": 0,
                "p50_latency_ms": None, "p95_latency_ms": None,
                "p50_latency_relation": None, "p95_latency_relation": None,
            }
        data = {
            key: int(row[key] or 0)
            for key in (
                "calls", "success_count", "error_count",
                "input_tokens", "output_tokens", "input_bytes", "output_bytes",
                "image_count", "binary_bytes", "duration_total_ms",
            )
        }
        buckets = {name: int(row[f"latency_{name}"] or 0) for name, _ in _LATENCY_BUCKETS}
        p50_ms, p50_relation = _latency_percentile(buckets, 0.50)
        p95_ms, p95_relation = _latency_percentile(buckets, 0.95)
        data["p50_latency_ms"] = p50_ms
        data["p95_latency_ms"] = p95_ms
        data["p50_latency_relation"] = p50_relation
        data["p95_latency_relation"] = p95_relation
        return data

    daily = []
    for row in daily_rows:
        item = {"date": row["local_date"], **row_payload(row)}
        daily.append(item)

    tools = []
    for row in top_tools:
        tools.append({"tool": row["tool"], **row_payload(row)})

    return {
        "ok": True,
        "days": bounded_days,
        "actor_class": actor_class or "all",
        "metering_enabled": privacy["enabled"],
        "retention_days": privacy["retention_days"],
        "stored_data": "Daily per-tool aggregates only (counts, sizes, latency buckets); no prompts, arguments or results.",
        "available_since": available_since,
        "history_complete_since": available_since,
        "tokenizer_id": TOKENIZER_ID,
        "measurement_class": MEASUREMENT_CLASS,
        "metric_name": "MCP Payload Tokens",
        "metric_scope": "Mac-MCP-attributable tool arguments/results only; not provider billing, model context, cache, or reasoning tokens.",
        "input_definition": "Canonical normalized MCP tool arguments.",
        "output_definition": "Canonical MCP tool result returned to the client.",
        "daily": daily,
        "totals": row_payload(total),
        "top_tools": tools,
        "memory_skills": _memory_skill_payload(memory_rows),
        "diagnostics": diagnostics or {},
    }


def _memory_skill_payload(rows: Iterable[sqlite3.Row]) -> Dict[str, Any]:
    """Memory and skill outcomes per tool, and which skills were loaded how often (#110)."""
    tools: Dict[str, Dict[str, int]] = {}
    skills: Dict[str, int] = {}
    for row in rows:
        count = int(row["count"] or 0)
        outcomes = tools.setdefault(str(row["tool"]), {})
        outcomes[str(row["outcome"])] = outcomes.get(str(row["outcome"]), 0) + count
        if row["tool"] == "skill_get" and row["outcome"] == "loaded" and row["detail"]:
            skills[str(row["detail"])] = skills.get(str(row["detail"]), 0) + count
    return {
        "tools": {tool: dict(sorted(outcomes.items())) for tool, outcomes in sorted(tools.items())},
        "skills_loaded": [{"name": name, "count": count}
                          for name, count in sorted(skills.items(), key=lambda item: (-item[1], item[0]))[:10]],
    }
