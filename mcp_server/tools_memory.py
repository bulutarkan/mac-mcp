from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import unicodedata
import uuid
import warnings
from contextlib import closing
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException, status

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_IMPORTANCE = "normal"
VALID_IMPORTANCE = {"low", "normal", "high", "critical"}
FEATURE_VECTOR_DIMS = 256
FEATURE_VECTOR_BACKEND = "feature_hash_v1"
APPLE_VECTOR_BACKEND = "apple_nl_en_v1"
MULTILINGUAL_VECTOR_BACKEND = "fastembed_multilingual_minilm_v1"
MULTILINGUAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MULTILINGUAL_DIMS = 384
_FASTEMBED_MODELS: Dict[str, Any] = {}
_FASTEMBED_LOCK = threading.Lock()
INDEX_NAME = "memory-index.sqlite3"
ENTRY_END = "<!-- /memory -->"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_RE = re.compile(
    r"^## (?P<time>\d{2}:\d{2}:\d{2})\n"
    r"(?P<meta>(?:<!-- [^\n]+ -->\n)+)"
    r"\n?(?P<content>.*?)\n"
    r"<!-- /memory -->\s*",
    re.MULTILINE | re.DOTALL,
)
META_RE = re.compile(r"<!--\s*([a-z_]+):\s*(.*?)\s*-->")


@dataclass
class MemoryEntry:
    memory_id: str
    date: str
    created_at: str
    updated_at: Optional[str]
    content: str
    tags: List[str]
    importance: str
    source: Optional[str]
    file_path: str

    @property
    def time(self) -> str:
        try:
            return datetime.fromisoformat(self.created_at).strftime("%H:%M:%S")
        except Exception:
            return self.created_at[11:19] if len(self.created_at) >= 19 else "00:00:00"

    def public(self, *, include_content: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "memory_id": self.memory_id,
            "date": self.date,
            "time": self.time,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "importance": self.importance,
            "source": self.source,
            "file_path": self.file_path,
        }
        if include_content:
            out["content"] = self.content
        return out


def _tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Europe/Istanbul")
        except Exception:
            pass
    return timezone(timedelta(hours=3))


def _now() -> datetime:
    return datetime.now(_tz())


def _memory_root() -> Path:
    raw = os.getenv("MAC_MCP_MEMORY_DIR", "").strip()
    root = Path(raw).expanduser() if raw else (Path.home() / ".mac-mcp" / "memory")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path(root: Optional[Path] = None) -> Path:
    return (root or _memory_root()) / INDEX_NAME


def _parse_date(value: str, field: str) -> date_cls:
    if not DATE_RE.match(str(value or "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} must use YYYY-MM-DD format.")
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} is not a valid calendar date.") from exc


def _date_range(
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if date and (date_from or date_to):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date cannot be combined with date_from/date_to.")
    if date:
        parsed = _parse_date(date, "date").isoformat()
        return parsed, parsed
    start = _parse_date(date_from, "date_from").isoformat() if date_from else None
    end = _parse_date(date_to, "date_to").isoformat() if date_to else None
    if start and end and start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date_from cannot be after date_to.")
    return start, end


def _clean_tags(tags: Optional[Sequence[str]]) -> List[str]:
    if not tags:
        return []
    out: List[str] = []
    seen = set()
    for tag in tags:
        value = re.sub(r"\s+", " ", str(tag or "")).strip()
        if not value:
            continue
        if len(value) > 80:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each memory tag must be at most 80 characters.")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out[:32]


def _clean_importance(value: Optional[str]) -> str:
    result = str(value or DEFAULT_IMPORTANCE).strip().lower()
    if result not in VALID_IMPORTANCE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"importance must be one of: {', '.join(sorted(VALID_IMPORTANCE))}.",
        )
    return result


def _day_path(root: Path, day: str) -> Path:
    parsed = _parse_date(day, "date")
    path = root / f"{parsed.year:04d}" / f"{parsed.month:02d}" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.name


def _new_memory_id() -> str:
    return "mem_" + uuid.uuid4().hex[:20]


def _entry_markdown(entry: MemoryEntry) -> str:
    tags_json = json.dumps(entry.tags, ensure_ascii=False, separators=(",", ":"))
    source = entry.source or ""
    lines = [
        f"## {entry.time}",
        f"<!-- memory_id: {entry.memory_id} -->",
        f"<!-- created_at: {entry.created_at} -->",
    ]
    if entry.updated_at:
        lines.append(f"<!-- updated_at: {entry.updated_at} -->")
    lines.extend([
        f"<!-- tags: {tags_json} -->",
        f"<!-- importance: {entry.importance} -->",
        f"<!-- source: {source} -->",
        "",
        entry.content.rstrip(),
        ENTRY_END,
    ])
    return "\n".join(lines).rstrip() + "\n"


def _render_day(day: str, entries: Sequence[MemoryEntry]) -> str:
    body = f"# Memory — {day}\n\n"
    if entries:
        body += "\n---\n\n".join(_entry_markdown(entry).rstrip() for entry in entries) + "\n"
    return body


def _parse_tags(value: str) -> List[str]:
    try:
        raw = json.loads(value)
        if isinstance(raw, list):
            return [str(v) for v in raw if str(v).strip()]
    except Exception:
        pass
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_day_file(root: Path, path: Path) -> List[MemoryEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    day = path.stem
    _parse_date(day, "memory filename")
    entries: List[MemoryEntry] = []
    for match in ENTRY_RE.finditer(text):
        meta = {key: value for key, value in META_RE.findall(match.group("meta"))}
        memory_id = meta.get("memory_id", "").strip()
        created_at = meta.get("created_at", "").strip()
        if not memory_id or not created_at:
            continue
        entries.append(MemoryEntry(
            memory_id=memory_id,
            date=day,
            created_at=created_at,
            updated_at=meta.get("updated_at", "").strip() or None,
            content=match.group("content").rstrip(),
            tags=_parse_tags(meta.get("tags", "[]")),
            importance=meta.get("importance", DEFAULT_IMPORTANCE).strip() or DEFAULT_IMPORTANCE,
            source=meta.get("source", "").strip() or None,
            file_path=_relative_path(root, path),
        ))
    return entries


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _normalize(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "")).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("ı", "i")
    return re.sub(r"[^a-z0-9çğıöşü]+", " ", value).strip()


def _tokens(text: str) -> List[str]:
    return [token for token in _normalize(text).split() if len(token) >= 2]


def _feature_vector(text: str) -> List[float]:
    tokens = _tokens(text)
    features: List[Tuple[str, float]] = [(f"w:{token}", 1.0) for token in tokens]
    features += [(f"b:{tokens[i]}_{tokens[i+1]}", 0.75) for i in range(len(tokens) - 1)]
    norm_text = " ".join(tokens)
    compact = norm_text.replace(" ", "_")
    if len(compact) >= 3:
        features += [(f"c:{compact[i:i+3]}", 0.18) for i in range(len(compact) - 2)]
    vec = [0.0] * FEATURE_VECTOR_DIMS
    for feature, weight in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % FEATURE_VECTOR_DIMS
        sign = -1.0 if ((raw >> 8) & 1) else 1.0
        vec[index] += sign * weight
    length = math.sqrt(sum(v * v for v in vec))
    if length:
        vec = [v / length for v in vec]
    return vec


def _normalize_vector(vector: Sequence[float]) -> List[float]:
    values = [float(v) for v in vector]
    length = math.sqrt(sum(v * v for v in values))
    return [v / length for v in values] if length else values


def _fastembed_cache(root: Path) -> Path:
    raw = os.getenv("MAC_MCP_MEMORY_MODEL_CACHE", "").strip()
    cache = Path(raw).expanduser() if raw else (Path.home() / ".mac-mcp" / "cache" / "fastembed")
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _fastembed_model(root: Path, *, allow_download: bool) -> Optional[Any]:
    mode = os.getenv("MAC_MCP_MEMORY_EMBEDDING", "auto").strip().lower()
    if mode in {"apple", "feature", "feature_hash", "off", "disabled"}:
        return None
    cache = _fastembed_cache(root)
    if not allow_download and next(cache.rglob("*.onnx"), None) is None:
        return None
    try:
        from fastembed import TextEmbedding
    except Exception:
        return None

    key = str(cache)
    cached = _FASTEMBED_MODELS.get(key)
    if cached is not None:
        return cached

    with _FASTEMBED_LOCK:
        cached = _FASTEMBED_MODELS.get(key)
        if cached is not None:
            return cached
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"The model .* now uses mean pooling instead of CLS embedding.*",
                )
                model = TextEmbedding(
                    model_name=MULTILINGUAL_MODEL,
                    cache_dir=str(cache),
                    threads=max(1, min(4, os.cpu_count() or 1)),
                    local_files_only=not allow_download,
                )
        except Exception:
            return None
        _FASTEMBED_MODELS[key] = model
        return model


def _multilingual_vectors(
    root: Path,
    texts: Sequence[str],
    *,
    allow_download: bool,
) -> Optional[List[List[float]]]:
    if not texts:
        return []
    model = _fastembed_model(root, allow_download=allow_download)
    if model is None:
        return None
    try:
        vectors = list(model.embed(list(texts)))
        if len(vectors) != len(texts):
            return None
        normalized = [_normalize_vector(vector.tolist() if hasattr(vector, "tolist") else vector) for vector in vectors]
        if not normalized or any(len(vector) != MULTILINGUAL_DIMS for vector in normalized):
            return None
        return normalized
    except Exception:
        return None


def _apple_helper(root: Path) -> Optional[Path]:
    mode = os.getenv("MAC_MCP_MEMORY_EMBEDDING", "auto").strip().lower()
    if mode in {"feature", "feature_hash", "off", "disabled"}:
        return None
    swiftc = Path("/usr/bin/swiftc")
    source = Path(__file__).with_name("memory_embedding.swift")
    if not swiftc.exists() or not source.exists():
        return None
    cache = root / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary = cache / f"memory-embed-{source_hash}"
    if binary.exists() and os.access(binary, os.X_OK):
        return binary
    try:
        proc = subprocess.run(
            [str(swiftc), "-O", str(source), "-o", str(binary)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return None
        binary.chmod(0o700)
        return binary
    except Exception:
        return None


def _apple_vectors(root: Path, texts: Sequence[str]) -> Optional[List[List[float]]]:
    if not texts:
        return []
    helper = _apple_helper(root)
    if helper is None:
        return None
    try:
        proc = subprocess.run(
            [str(helper)], input=json.dumps(list(texts), ensure_ascii=False),
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        payload = json.loads(proc.stdout)
        vectors = payload.get("vectors") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            return None
        normalized: List[List[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                return None
            normalized.append(_normalize_vector(vector))
        return normalized
    except Exception:
        return None


def _semantic_vectors(
    root: Path,
    texts: Sequence[str],
    *,
    allow_download: bool = False,
) -> Tuple[str, int, List[List[float]]]:
    mode = os.getenv("MAC_MCP_MEMORY_EMBEDDING", "auto").strip().lower()
    if mode in {"auto", "multilingual", "fastembed"}:
        multilingual = _multilingual_vectors(root, texts, allow_download=allow_download)
        if multilingual:
            return MULTILINGUAL_VECTOR_BACKEND, len(multilingual[0]), multilingual
    if mode in {"auto", "apple", "multilingual", "fastembed"}:
        apple = _apple_vectors(root, texts)
        if apple:
            return APPLE_VECTOR_BACKEND, len(apple[0]), apple
    vectors = [_feature_vector(text) for text in texts]
    return FEATURE_VECTOR_BACKEND, FEATURE_VECTOR_DIMS, vectors

def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _content_hash(entry: MemoryEntry) -> str:
    payload = json.dumps({
        "content": entry.content,
        "tags": entry.tags,
        "importance": entry.importance,
        "source": entry.source,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_index_path(root)))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            importance TEXT NOT NULL,
            source TEXT,
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector_backend TEXT NOT NULL,
            vector_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_date ON memories(date);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
        CREATE TABLE IF NOT EXISTS indexed_files (
            file_path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            memory_id UNINDEXED,
            content,
            tags,
            source,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    return conn


def _file_signature(path: Path) -> Tuple[int, int, str]:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return stat.st_mtime_ns, stat.st_size, digest


def _index_file(conn: sqlite3.Connection, root: Path, path: Path) -> None:
    rel = _relative_path(root, path)
    entries = _parse_day_file(root, path)
    old_ids = [row["memory_id"] for row in conn.execute("SELECT memory_id FROM memories WHERE file_path=?", (rel,))]
    for memory_id in old_ids:
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE file_path=?", (rel,))
    vector_texts = [" ".join([entry.content, " ".join(entry.tags), entry.source or ""]) for entry in entries]
    vector_backend, _, vectors = _semantic_vectors(root, vector_texts, allow_download=False) if entries else (FEATURE_VECTOR_BACKEND, FEATURE_VECTOR_DIMS, [])
    for entry, vector in zip(entries, vectors):
        conn.execute(
            """INSERT OR REPLACE INTO memories
               (memory_id,date,created_at,updated_at,content,tags_json,importance,source,file_path,content_hash,vector_backend,vector_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry.memory_id, entry.date, entry.created_at, entry.updated_at, entry.content,
                json.dumps(entry.tags, ensure_ascii=False), entry.importance, entry.source, rel,
                _content_hash(entry), vector_backend, json.dumps(vector, separators=(",", ":")),
            ),
        )
        conn.execute(
            "INSERT INTO memory_fts(memory_id,content,tags,source) VALUES (?,?,?,?)",
            (entry.memory_id, entry.content, " ".join(entry.tags), entry.source or ""),
        )
    mtime_ns, size, digest = _file_signature(path)
    conn.execute(
        "INSERT OR REPLACE INTO indexed_files(file_path,mtime_ns,size,sha256) VALUES (?,?,?,?)",
        (rel, mtime_ns, size, digest),
    )
    conn.commit()


def _sync_index(root: Path, conn: sqlite3.Connection) -> Dict[str, int]:
    files = sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    current = {_relative_path(root, path): path for path in files}
    indexed = {row["file_path"]: row for row in conn.execute("SELECT * FROM indexed_files")}
    reindexed = 0
    removed = 0
    for rel, row in indexed.items():
        if rel not in current:
            ids = [r["memory_id"] for r in conn.execute("SELECT memory_id FROM memories WHERE file_path=?", (rel,))]
            for memory_id in ids:
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            conn.execute("DELETE FROM memories WHERE file_path=?", (rel,))
            conn.execute("DELETE FROM indexed_files WHERE file_path=?", (rel,))
            removed += 1
    conn.commit()
    for rel, path in current.items():
        stat = path.stat()
        row = indexed.get(rel)
        if row and int(row["mtime_ns"]) == stat.st_mtime_ns and int(row["size"]) == stat.st_size:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if row and row["sha256"] == digest:
            conn.execute(
                "UPDATE indexed_files SET mtime_ns=?,size=? WHERE file_path=?",
                (stat.st_mtime_ns, stat.st_size, rel),
            )
            conn.commit()
            continue
        _index_file(conn, root, path)
        reindexed += 1
    return {"reindexed_files": reindexed, "removed_files": removed}


def _ensure_vector_backend(root: Path, conn: sqlite3.Connection, backend: str) -> int:
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM memories WHERE vector_backend<>? ORDER BY file_path",
        (backend,),
    ).fetchall()
    reindexed = 0
    for row in rows:
        path = root / str(row["file_path"])
        if not path.exists():
            continue
        _index_file(conn, root, path)
        reindexed += 1
    return reindexed


def _row_entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        memory_id=row["memory_id"], date=row["date"], created_at=row["created_at"],
        updated_at=row["updated_at"], content=row["content"], tags=json.loads(row["tags_json"] or "[]"),
        importance=row["importance"], source=row["source"], file_path=row["file_path"],
    )


def _fts_scores(conn: sqlite3.Connection, query: str, allowed_ids: set[str]) -> Dict[str, float]:
    tokens = _tokens(query)
    if not tokens:
        return {}
    expr = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:20])
    rows = conn.execute(
        "SELECT memory_id,bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? LIMIT 200",
        (expr,),
    ).fetchall()
    selected = [(row["memory_id"], float(row["rank"])) for row in rows if row["memory_id"] in allowed_ids]
    if not selected:
        return {}
    # FTS5 BM25 is lower/better and often negative. Convert to 0..1 while preserving order.
    ordered = sorted(selected, key=lambda item: item[1])
    total = max(1, len(ordered) - 1)
    return {memory_id: 1.0 - (idx / total) * 0.45 for idx, (memory_id, _) in enumerate(ordered)}


def _lexical_similarity(query: str, entry: MemoryEntry) -> float:
    q = _normalize(query)
    if not q:
        return 0.0
    hay = _normalize(" ".join([entry.content, " ".join(entry.tags), entry.source or ""]))
    q_tokens = set(q.split())
    h_tokens = set(hay.split())
    overlap = len(q_tokens & h_tokens) / max(1, len(q_tokens))
    score = overlap * 0.72
    if q == hay:
        score = max(score, 1.0)
    elif q in hay:
        score = max(score, 0.88)
    elif hay.startswith(q):
        score = max(score, 0.82)
    return min(1.0, score)


def _recency_score(day: str) -> float:
    try:
        days = max(0, (_now().date() - date_cls.fromisoformat(day)).days)
    except Exception:
        return 0.0
    return math.exp(-days / 180.0)


def _select_rows(
    conn: sqlite3.Connection,
    start: Optional[str],
    end: Optional[str],
) -> List[sqlite3.Row]:
    clauses = []
    args: List[Any] = []
    if start:
        clauses.append("date>=?")
        args.append(start)
    if end:
        clauses.append("date<=?")
        args.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(f"SELECT * FROM memories{where}", args).fetchall()


def _tag_match(entry: MemoryEntry, tags: Sequence[str]) -> bool:
    if not tags:
        return True
    present = {tag.casefold() for tag in entry.tags}
    return all(tag.casefold() in present for tag in tags)


def memory_add(
    content: str,
    tags: Optional[List[str]] = None,
    importance: str = DEFAULT_IMPORTANCE,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    value = str(content or "").strip()
    if not value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content cannot be empty.")
    if ENTRY_END in value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content contains a reserved memory marker.")
    if len(value) > 100_000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content is too large (max 100,000 characters).")
    clean_tags = _clean_tags(tags)
    clean_importance = _clean_importance(importance)
    clean_source = re.sub(r"\s+", " ", str(source or "")).strip() or None
    if clean_source and len(clean_source) > 120:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source must be at most 120 characters.")

    root = _memory_root()
    now = _now()
    day = now.date().isoformat()
    path = _day_path(root, day)
    entries = _parse_day_file(root, path) if path.exists() else []
    entry = MemoryEntry(
        memory_id=_new_memory_id(), date=day, created_at=now.isoformat(timespec="seconds"),
        updated_at=None, content=value, tags=clean_tags, importance=clean_importance,
        source=clean_source, file_path=_relative_path(root, path),
    )
    entries.append(entry)
    _atomic_write(path, _render_day(day, entries))
    with closing(_connect(root)) as conn:
        _index_file(conn, root, path)
    return {"ok": True, "action": "added", **entry.public(), "timezone": "Europe/Istanbul"}


def memory_search(
    query: Optional[str] = None,
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tags: Optional[List[str]] = None,
    importance: Optional[str] = None,
    sort: str = "relevance",
    limit: int = 20,
) -> Dict[str, Any]:
    start, end = _date_range(date, date_from, date_to)
    clean_tags = _clean_tags(tags)
    clean_importance = _clean_importance(importance) if importance else None
    sort = str(sort or "relevance").strip().lower()
    if sort not in {"relevance", "newest", "oldest"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sort must be relevance, newest, or oldest.")
    limit = max(1, min(int(limit), 100))
    q = str(query or "").strip()
    root = _memory_root()
    with closing(_connect(root)) as conn:
        sync = _sync_index(root, conn)

        query_backend: Optional[str] = None
        query_dims = 0
        query_vector: Optional[List[float]] = None
        if q:
            query_backend, query_dims, query_vectors = _semantic_vectors(root, [q], allow_download=True)
            query_vector = query_vectors[0] if query_vectors else None
            if query_backend and query_vector:
                vector_reindexed = _ensure_vector_backend(root, conn, query_backend)
                if vector_reindexed:
                    sync["vector_reindexed_files"] = vector_reindexed

        rows = _select_rows(conn, start, end)
        entries = [_row_entry(row) for row in rows]
        entries = [entry for entry in entries if _tag_match(entry, clean_tags)]
        if clean_importance:
            entries = [entry for entry in entries if entry.importance == clean_importance]
        if not q:
            reverse = sort != "oldest"
            entries.sort(key=lambda entry: entry.created_at, reverse=reverse)
            return {
                "ok": True, "query": None, "mode": "list", "sort": sort,
                "date_from": start, "date_to": end, "count": min(limit, len(entries)),
                "total_matches": len(entries), "results": [entry.public() for entry in entries[:limit]],
                "index_sync": sync,
            }

        allowed = {entry.memory_id for entry in entries}
        fts = _fts_scores(conn, q, allowed)
        q_feature = _feature_vector(q)
        q_apple: Optional[List[float]] = None
        scored: List[Tuple[float, float, float, float, MemoryEntry]] = []
        row_map = {row["memory_id"]: row for row in rows}
        for entry in entries:
            row = row_map[entry.memory_id]
            try:
                vector = json.loads(row["vector_json"])
            except Exception:
                vector = _feature_vector(entry.content)
            backend = str(row["vector_backend"] or FEATURE_VECTOR_BACKEND)
            if query_vector is not None and backend == query_backend:
                semantic = _cosine(query_vector, vector)
            elif backend == APPLE_VECTOR_BACKEND:
                if q_apple is None:
                    apple_query = _apple_vectors(root, [q])
                    q_apple = apple_query[0] if apple_query else []
                semantic = _cosine(q_apple, vector)
            elif backend == FEATURE_VECTOR_BACKEND:
                semantic = _cosine(q_feature, vector)
            else:
                semantic = 0.0
            lexical = max(_lexical_similarity(q, entry), fts.get(entry.memory_id, 0.0))
            recency = _recency_score(entry.date)
            score = (semantic * 0.50) + (lexical * 0.45) + (recency * 0.05)
            if semantic < 0.05 and lexical < 0.05:
                continue
            scored.append((score, semantic, lexical, recency, entry))
        if sort == "newest":
            scored.sort(key=lambda item: item[4].created_at, reverse=True)
        elif sort == "oldest":
            scored.sort(key=lambda item: item[4].created_at)
        else:
            scored.sort(key=lambda item: (item[0], item[4].created_at), reverse=True)
        results = []
        for score, semantic, lexical, recency, entry in scored[:limit]:
            item = entry.public()
            item.update({
                "score": round(score, 4), "semantic_score": round(semantic, 4),
                "lexical_score": round(lexical, 4), "recency_score": round(recency, 4),
            })
            results.append(item)
        backend_info: Dict[str, Any] = {
            "fts": "sqlite_fts5",
            "vector": query_backend or FEATURE_VECTOR_BACKEND,
            "dimensions": query_dims or FEATURE_VECTOR_DIMS,
            "fallback": [APPLE_VECTOR_BACKEND, FEATURE_VECTOR_BACKEND],
        }
        if query_backend == MULTILINGUAL_VECTOR_BACKEND:
            backend_info["model"] = MULTILINGUAL_MODEL
            backend_info["multilingual"] = True
        return {
            "ok": True, "query": q, "mode": "hybrid_search", "sort": sort,
            "date_from": start, "date_to": end, "count": len(results),
            "total_matches": len(scored), "results": results,
            "search_backend": backend_info,
            "index_sync": sync,
        }


def memory_get(memory_id: str) -> Dict[str, Any]:
    key = str(memory_id or "").strip()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_id is required.")
    root = _memory_root()
    with closing(_connect(root)) as conn:
        _sync_index(root, conn)
        row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (key,)).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Memory not found: {key}")
    return {"ok": True, **_row_entry(row).public()}


def _selection(
    *,
    date: Optional[str], date_from: Optional[str], date_to: Optional[str], limit: int,
    action: str,
) -> Dict[str, Any]:
    listing = memory_search(date=date, date_from=date_from, date_to=date_to, sort="newest", limit=limit)
    listing["action"] = action
    listing["selection_required"] = True
    listing["message"] = f"Choose a memory_id from these timestamped memories, then call memory_{action} again with that memory_id."
    return listing


def _locate_entry(root: Path, memory_id: str) -> Tuple[Path, List[MemoryEntry], int]:
    with closing(_connect(root)) as conn:
        _sync_index(root, conn)
        row = conn.execute("SELECT file_path FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Memory not found: {memory_id}")
    path = root / row["file_path"]
    entries = _parse_day_file(root, path)
    for idx, entry in enumerate(entries):
        if entry.memory_id == memory_id:
            return path, entries, idx
    raise HTTPException(status.HTTP_409_CONFLICT, "Memory index is out of sync with Markdown source. Retry the operation.")


def memory_update(
    memory_id: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    importance: Optional[str] = None,
    source: Optional[str] = None,
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    if not memory_id:
        return _selection(date=date, date_from=date_from, date_to=date_to, limit=limit, action="update")
    # Date filters are selection helpers only; exact IDs are globally unique.
    if date or date_from or date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Do not combine memory_id with date/date_from/date_to.")
    root = _memory_root()
    path, entries, idx = _locate_entry(root, str(memory_id).strip())
    entry = entries[idx]
    if content is not None:
        value = str(content).strip()
        if not value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "content cannot be empty.")
        if ENTRY_END in value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "content contains a reserved memory marker.")
        entry.content = value
    if tags is not None:
        entry.tags = _clean_tags(tags)
    if importance is not None:
        entry.importance = _clean_importance(importance)
    if source is not None:
        clean_source = re.sub(r"\s+", " ", str(source)).strip()
        entry.source = clean_source or None
    if content is None and tags is None and importance is None and source is None:
        return {"ok": True, "action": "unchanged", **entry.public()}
    entry.updated_at = _now().isoformat(timespec="seconds")
    entries[idx] = entry
    _atomic_write(path, _render_day(entry.date, entries))
    with closing(_connect(root)) as conn:
        _index_file(conn, root, path)
    return {"ok": True, "action": "updated", **entry.public()}


def memory_delete(
    memory_id: Optional[str] = None,
    confirm: bool = False,
    date: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    if not memory_id:
        return _selection(date=date, date_from=date_from, date_to=date_to, limit=limit, action="delete")
    if date or date_from or date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Do not combine memory_id with date/date_from/date_to.")
    root = _memory_root()
    path, entries, idx = _locate_entry(root, str(memory_id).strip())
    entry = entries[idx]
    if not confirm:
        return {
            "ok": False, "action": "delete", "confirmation_required": True,
            "message": "Call memory_delete again with confirm=true to permanently delete this memory.",
            "memory": entry.public(),
        }
    del entries[idx]
    _atomic_write(path, _render_day(entry.date, entries))
    with closing(_connect(root)) as conn:
        _index_file(conn, root, path)
    return {"ok": True, "action": "deleted", "memory_id": entry.memory_id, "date": entry.date, "time": entry.time, "file_path": entry.file_path}
