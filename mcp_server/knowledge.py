"""Knowledge folders (#197): local search, with sources, over folders the owner picks.

- Only the folders in settings.json ``knowledge.folders`` (Settings › Privacy) are read. The home
  folder itself, the disk root, system folders, ~/Library and Mac MCP's own state are refused.
- Text is extracted on this Mac: plain text and code as is, PDFs page by page with PDFKit (through
  osascript, no extra dependency), Word/RTF/OpenDocument/HTML with textutil. Nothing leaves the Mac.
  Scanned PDFs without a text layer are listed as having no text (no OCR yet).
- The index is ~/.mac-mcp/knowledge/index.sqlite3 (owner-only, secure_delete). A scan compares each
  file's size and modification time, and its SHA-256 when those changed: new and changed files are
  indexed, deleted files and files of removed folders are dropped. A background thread scans every
  SCAN_EVERY_S, and a search first refreshes when the last scan is older than FRESH_S, within a
  short time budget (what is left is reported as pending).
- Search is hybrid, like memory_search: SQLite FTS5 plus local vectors from the shared embedding
  worker. Every result names its file, page (PDFs), a snippet, the file's SHA-256, when it changed
  and when it was indexed. Scoped agents only get results inside their own path roots.
- Private mode pauses indexing (search still answers from what is already indexed).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from fastapi import HTTPException, status

from . import embedding_manager as embeddings
from . import paths
from .policy_scope import path_is_within
from .runtime_settings import load_runtime_settings, update_runtime_setting

log = logging.getLogger("mac_mcp.knowledge")

INDEX_NAME = "index.sqlite3"
SCAN_EVERY_S = 300.0
FRESH_S = 60.0
SEARCH_REFRESH_BUDGET_S = 3.0
BACKGROUND_BUDGET_S = 120.0
MAX_FOLDERS = 20
MAX_FILES_PER_FOLDER = 20_000
MAX_CHUNKS = 100_000
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_CHARS_PER_FILE = 2_000_000
MAX_PDF_PAGES = 2_000
CHUNK_CHARS = 1_200
CHUNK_OVERLAP = 150
EMBED_BATCH = 32
VECTOR_BLOCK = 4_096
MAX_RESULTS = 25
MAX_PER_FILE = 3
SNIPPET_CHARS = 420
MIN_SEMANTIC_ALONE = 0.35

TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".xml", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".sh", ".sql",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".css", ".scss",
})
TEXTUTIL_SUFFIXES = frozenset({".docx", ".doc", ".rtf", ".odt", ".html", ".htm", ".webarchive"})
PDF_SUFFIXES = frozenset({".pdf"})
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | TEXTUTIL_SUFFIXES | PDF_SUFFIXES
SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", "DerivedData", "build", "dist", "Library", "Pods",
})

_PDF_SCRIPT = """
ObjC.import('PDFKit');
function run(argv) {
  var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) return JSON.stringify({error: 'unreadable'});
  if (doc.isLocked) return JSON.stringify({error: 'locked'});
  var pages = [], n = Math.min(doc.pageCount, Number(argv[1]));
  for (var i = 0; i < n; i++) {
    var text = doc.pageAtIndex(i).string;
    pages.push(text && !text.isNil() ? text.js : '');
  }
  return JSON.stringify({pages: pages, page_count: Number(doc.pageCount)});
}
"""

_INDEX_LOCK = threading.Lock()
_WAKE = threading.Event()
_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None


# Folders --------------------------------------------------------------------------------------

def _home() -> Path:
    return Path.home().resolve()


def _refused_roots() -> Tuple[Path, ...]:
    home = _home()
    return (Path("/"), home, Path("/System"), Path("/Library"), Path("/Applications"), Path("/private"),
            Path("/usr"), Path("/bin"), Path("/sbin"), Path("/etc"), Path("/var"), Path("/opt"),
            Path("/Volumes"), Path("/Users"))


def folder_problem(raw: str) -> Optional[str]:
    """Why a folder cannot be a knowledge folder, or None when it can."""
    text = str(raw or "").strip()
    if not text:
        return "Choose a folder."
    path = Path(text).expanduser()
    if not path.is_absolute():
        return "Use the folder's full path."
    resolved = path.resolve(strict=False)
    if not resolved.is_dir():
        return "That folder doesn't exist."
    if resolved in _refused_roots():
        return "Choose a specific folder, not your whole home folder, the disk or a system folder."
    home = _home()
    for inside in (home / "Library", paths.state_dir().resolve(strict=False), Path("/System"), Path("/Library"),
                   Path("/usr"), Path("/bin"), Path("/sbin"), Path("/private/etc"), Path("/private/var/db")):
        if path_is_within(resolved, inside):
            return "Mac MCP doesn't search inside system folders, ~/Library or its own data."
    return None


def configured_folders() -> List[str]:
    section = load_runtime_settings().get("knowledge")
    raw = section.get("folders") if isinstance(section, dict) else None
    folders: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and folder_problem(item) is None:
            resolved = str(Path(item).expanduser().resolve(strict=False))
            if resolved not in folders:
                folders.append(resolved)
    return folders[:MAX_FOLDERS]


def _save_folders(folders: Sequence[str]) -> None:
    update_runtime_setting("knowledge", "folders", list(folders))


def add_folder(raw: str) -> Dict[str, Any]:
    problem = folder_problem(raw)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    resolved = str(Path(raw).expanduser().resolve(strict=False))
    folders = configured_folders()
    if resolved not in folders:
        if len(folders) >= MAX_FOLDERS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"At most {MAX_FOLDERS} folders.")
        if any(path_is_within(resolved, folder) for folder in folders):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That folder is already inside one you chose.")
        # A new parent replaces the folders it contains.
        folders = [folder for folder in folders if not path_is_within(folder, resolved)] + [resolved]
        _save_folders(folders)
    wake()
    return overview()


def remove_folder(raw: str) -> Dict[str, Any]:
    target = str(Path(str(raw or "")).expanduser().resolve(strict=False))
    folders = [folder for folder in configured_folders() if folder != target]
    _save_folders(folders)
    # Drop its files now when the indexer is idle; otherwise its next scan does.
    if _INDEX_LOCK.acquire(timeout=2):
        try:
            with closing(_connect()) as conn:
                _drop_outside(conn, folders)
        finally:
            _INDEX_LOCK.release()
    else:
        wake()
    return overview()


# Index ----------------------------------------------------------------------------------------

def index_dir() -> Path:
    return paths.knowledge_dir()


def _connect() -> sqlite3.Connection:
    root = index_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    index = root / INDEX_NAME
    conn = sqlite3.connect(str(index), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA secure_delete=ON;
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            kind TEXT NOT NULL,
            pages INTEGER,
            chunks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT,
            indexed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            page INTEGER,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            vector_backend TEXT NOT NULL,
            vector BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    try:
        conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('secure-delete', 1)")
        conn.commit()
    except sqlite3.DatabaseError:
        pass
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(index) + suffix)
        if candidate.exists():
            os.chmod(candidate, 0o600)
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _delete_file_rows(conn: sqlite3.Connection, path: str) -> None:
    ids = [row["chunk_id"] for row in conn.execute("SELECT chunk_id FROM chunks WHERE path = ?", (path,))]
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        marks = ",".join("?" * len(part))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", part)
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
    conn.execute("DELETE FROM files WHERE path = ?", (path,))


def _drop_outside(conn: sqlite3.Connection, folders: Sequence[str]) -> int:
    dropped = 0
    for row in conn.execute("SELECT path, folder FROM files").fetchall():
        if row["folder"] not in folders:
            _delete_file_rows(conn, row["path"])
            dropped += 1
    conn.commit()
    return dropped


# Extraction -----------------------------------------------------------------------------------

def _walk(folder: str) -> Iterator[Path]:
    count = 0
    for current, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(names):
            if name.startswith(".") or Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            path = Path(current) / name
            if path.is_symlink():
                continue
            yield path
            count += 1
            if count >= MAX_FILES_PER_FOLDER:
                return


def _kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in TEXTUTIL_SUFFIXES:
        return "document"
    return "text"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(path: Path) -> Tuple[List[Tuple[Optional[int], str]], Optional[str]]:
    """[(page or None, text)], error. Pages are 1-based for PDFs."""
    kind = _kind(path)
    size = path.stat().st_size
    if size > (MAX_TEXT_FILE_BYTES if kind == "text" else MAX_DOCUMENT_BYTES):
        return [], "too large"
    if kind == "text":
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            return [], "binary"
        return [(None, data.decode("utf-8", errors="replace")[:MAX_CHARS_PER_FILE])], None
    if kind == "document":
        try:
            out = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", "--", str(path)],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return [], "unreadable"
        if out.returncode != 0:
            return [], "unreadable"
        return [(None, out.stdout[:MAX_CHARS_PER_FILE])], None
    try:
        out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _PDF_SCRIPT, str(path), str(MAX_PDF_PAGES)],
                             capture_output=True, text=True, timeout=120)
        payload = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], "unreadable"
    if payload.get("error"):
        return [], str(payload["error"])
    pages: List[Tuple[Optional[int], str]] = []
    total = 0
    for number, text in enumerate(payload.get("pages") or [], start=1):
        text = str(text or "")
        total += len(text)
        if total > MAX_CHARS_PER_FILE:
            break
        pages.append((number, text))
    return pages, None


def chunk(text: str) -> List[str]:
    clean = re.sub(r"[ \t\r\f\v]+", " ", text)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    if not clean:
        return []
    out: List[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + CHUNK_CHARS)
        if end < len(clean):
            # End at a paragraph or sentence break when one is near.
            window = clean[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > CHUNK_CHARS // 2:
                end = start + cut + 1
        piece = clean[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(clean):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def _vectors(texts: Sequence[str]) -> Tuple[str, List[List[float]]]:
    backend, _dims, vectors = embeddings.semantic_vectors(texts, allow_start=True)
    return backend, vectors


def _pack(vector: Sequence[float]) -> bytes:
    import numpy as np
    return np.asarray(vector, dtype=np.float32).tobytes()


# Scanning -------------------------------------------------------------------------------------

def _private_mode() -> bool:
    from . import privacy_policy
    return bool(privacy_policy.policy().get("private_mode"))


def _index_one(conn: sqlite3.Connection, folder: str, path: Path, stat: os.stat_result, digest: str) -> int:
    pages, error = extract(path)
    pieces: List[Tuple[Optional[int], int, str]] = []
    for page, text in pages:
        for ordinal, piece in enumerate(chunk(text)):
            pieces.append((page, ordinal, piece))
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if total + len(pieces) > MAX_CHUNKS:
        error, pieces = "index is full", []
    rows: List[Tuple[Optional[int], int, str, str, bytes]] = []
    for start in range(0, len(pieces), EMBED_BATCH):
        batch = pieces[start:start + EMBED_BATCH]
        backend, vectors = _vectors([piece for _page, _ordinal, piece in batch])
        rows.extend((page, ordinal, piece, backend, _pack(vector))
                    for (page, ordinal, piece), vector in zip(batch, vectors))
    _delete_file_rows(conn, str(path))
    for page, ordinal, piece, backend, blob in rows:
        cursor = conn.execute(
            "INSERT INTO chunks(path, page, ordinal, text, vector_backend, vector) VALUES(?, ?, ?, ?, ?, ?)",
            (str(path), page, ordinal, piece, backend, blob))
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)", (cursor.lastrowid, piece))
    state = "error" if error else ("indexed" if rows else "no_text")
    conn.execute(
        "INSERT INTO files(path, folder, size, mtime_ns, sha256, kind, pages, chunks, status, error, indexed_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(path), folder, stat.st_size, stat.st_mtime_ns, digest, _kind(path),
         len(pages) if _kind(path) == "pdf" else None, len(rows), state, error, _now_iso()))
    conn.commit()
    return len(rows)


def scan(*, budget_s: float = BACKGROUND_BUDGET_S, folders: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Bring the index up to date with the folders, within budget_s. Safe to call from any thread."""
    folders = list(configured_folders() if folders is None else folders)
    summary: Dict[str, Any] = {"indexed": 0, "removed": 0, "unchanged": 0, "pending": 0, "paused": None}
    if _private_mode():
        summary["paused"] = "private_mode"
        return summary
    if not _INDEX_LOCK.acquire(timeout=0.5):
        summary["busy"] = True
        return summary
    deadline = time.monotonic() + max(0.1, budget_s)
    try:
        with closing(_connect()) as conn:
            summary["removed"] += _drop_outside(conn, folders)
            known = {row["path"]: row for row in conn.execute(
                "SELECT path, folder, size, mtime_ns, sha256 FROM files")}
            seen: set[str] = set()
            for folder in folders:
                for path in _walk(folder):
                    key = str(path)
                    seen.add(key)
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    row = known.get(key)
                    if row and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                        summary["unchanged"] += 1
                        continue
                    if time.monotonic() > deadline:
                        summary["pending"] += 1
                        continue
                    try:
                        digest = _sha256(path)
                    except OSError:
                        continue
                    if row and row["sha256"] == digest:
                        conn.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE path = ?",
                                     (stat.st_size, stat.st_mtime_ns, key))
                        conn.commit()
                        summary["unchanged"] += 1
                        continue
                    try:
                        _index_one(conn, folder, path, stat, digest)
                        summary["indexed"] += 1
                    except Exception as exc:  # one bad file never stops the scan
                        log.warning("knowledge: could not index a file: %s", type(exc).__name__)
            # Deleted, renamed, now hidden or past the per-folder limit: no longer searchable.
            for key in set(known) - seen:
                _delete_file_rows(conn, key)
                summary["removed"] += 1
            conn.commit()
            if summary["pending"] == 0:
                _set_meta(conn, "last_scan_at", _now_iso())
                _set_meta(conn, "last_scan_monotonic", str(time.monotonic()))
            _set_meta(conn, "pending", str(summary["pending"]))
            conn.commit()
    finally:
        _INDEX_LOCK.release()
    return summary


def _last_scan_age(conn: sqlite3.Connection) -> float:
    try:
        return time.monotonic() - float(_meta(conn, "last_scan_monotonic") or "-1e18")
    except ValueError:
        return float("inf")


# Search ---------------------------------------------------------------------------------------

def _fts_scores(conn: sqlite3.Connection, query: str) -> Dict[int, float]:
    terms = [term for term in re.findall(r"\w+", query.lower()) if len(term) > 1][:12]
    if not terms:
        return {}
    # Long words also match by their first letters, so suffixes (Turkish "sözleşmesi" / "sözleşme",
    # English "invoices" / "invoice") still meet.
    expression = " OR ".join(f'"{term[:5]}"*' if len(term) >= 7 else f'"{term}"' for term in terms)
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(chunks_fts) AS rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 400",
            (expression,)).fetchall()
    except sqlite3.DatabaseError:
        return {}
    if not rows:
        return {}
    best = min(float(row["rank"]) for row in rows)
    worst = max(float(row["rank"]) for row in rows)
    spread = (worst - best) or 1.0
    # bm25 is lower-is-better; map the best match to 1.0 and the weakest kept one to 0.2.
    return {int(row["rowid"]): 1.0 - 0.8 * (float(row["rank"]) - best) / spread for row in rows}


def _semantic_scores(conn: sqlite3.Connection, query: str, allowed: Optional[set[str]]) -> Dict[int, float]:
    import numpy as np
    backend, vectors = _vectors([query])
    if not vectors:
        return {}
    q = np.asarray(vectors[0], dtype=np.float32)
    norm = float(np.linalg.norm(q)) or 1.0
    q = q / norm
    scores: Dict[int, float] = {}
    cursor = conn.execute("SELECT chunk_id, path, vector FROM chunks WHERE vector_backend = ?", (backend,))
    while True:
        block = cursor.fetchmany(VECTOR_BLOCK)
        if not block:
            break
        rows = [row for row in block if allowed is None or row["path"] in allowed]
        if not rows:
            continue
        matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype=np.float32).reshape(len(rows), -1)
        if matrix.shape[1] != q.shape[0]:
            continue
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        sims = (matrix @ q) / norms
        for row, sim in zip(rows, sims.tolist()):
            if sim > 0.2:
                scores[int(row["chunk_id"])] = float(sim)
    return scores


def _snippet(text: str, query: str) -> str:
    if len(text) <= SNIPPET_CHARS:
        return text
    lowered = text.lower()
    hits = [lowered.find(term) for term in re.findall(r"\w+", query.lower()) if len(term) > 2]
    hits = [hit for hit in hits if hit >= 0]
    start = max(0, min(hits) - SNIPPET_CHARS // 3) if hits else 0
    piece = text[start:start + SNIPPET_CHARS].strip()
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(text) else "")


def _scope_roots() -> Optional[Tuple[str, ...]]:
    from .policy import current_policy_context
    scope = current_policy_context().scope
    return None if scope is None else scope.path_roots


def knowledge_search(query: str, path: Optional[str] = None, limit: int = 8) -> Dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query is required.")
    if len(q) > 1000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query must be at most 1000 characters.")
    limit = max(1, min(int(limit), MAX_RESULTS))
    folders = configured_folders()
    if not folders:
        return {"ok": True, "query": q, "count": 0, "results": [], "folders": [],
                "message": "No knowledge folders yet. The owner can add folders in Mac MCP Settings › Privacy › "
                           "Folders Mac MCP can search."}
    within: Optional[str] = None
    if path:
        within = str(Path(path).expanduser().resolve(strict=False))
        if not any(path_is_within(within, folder) for folder in folders):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "path must be one of the knowledge folders or inside one: " + ", ".join(folders))
    roots = _scope_roots()
    refresh: Dict[str, Any] = {}
    with closing(_connect()) as conn:
        stale = _last_scan_age(conn) > FRESH_S
    if stale:
        refresh = scan(budget_s=SEARCH_REFRESH_BUDGET_S, folders=folders)
    with closing(_connect()) as conn:
        files = {row["path"]: row for row in conn.execute("SELECT * FROM files WHERE status = 'indexed'")}

        def visible(file_path: str) -> bool:
            if within and not path_is_within(file_path, within):
                return False
            return roots is None or any(path_is_within(file_path, root) for root in roots)

        allowed = {file_path for file_path in files if visible(file_path)}
        lexical = _fts_scores(conn, q)
        semantic = _semantic_scores(conn, q, allowed)
        ids = set(lexical) | set(semantic)
        rows = {}
        for start in range(0, len(ids), 500):
            part = list(ids)[start:start + 500]
            marks = ",".join("?" * len(part))
            for row in conn.execute(f"SELECT chunk_id, path, page, text FROM chunks WHERE chunk_id IN ({marks})", part):
                if row["path"] in allowed:
                    rows[int(row["chunk_id"])] = row
        scored = sorted(((0.6 * semantic.get(cid, 0.0) + 0.4 * lexical.get(cid, 0.0), cid) for cid in rows),
                        reverse=True)
        results: List[Dict[str, Any]] = []
        per_file: Dict[str, int] = {}
        for score, cid in scored:
            row = rows[cid]
            if not lexical.get(cid) and semantic.get(cid, 0.0) < MIN_SEMANTIC_ALONE:
                continue  # only loosely related and no shared word: not worth citing
            if per_file.get(row["path"], 0) >= MAX_PER_FILE:
                continue
            per_file[row["path"]] = per_file.get(row["path"], 0) + 1
            info = files[row["path"]]
            results.append({
                "path": row["path"],
                "name": Path(row["path"]).name,
                "page": row["page"],
                "snippet": _snippet(row["text"], q),
                "score": round(score, 4),
                "semantic_score": round(semantic.get(cid, 0.0), 4),
                "lexical_score": round(lexical.get(cid, 0.0), 4),
                "sha256": info["sha256"],
                "modified_at": datetime.fromtimestamp(info["mtime_ns"] / 1e9, timezone.utc).replace(microsecond=0).isoformat(),
                "indexed_at": info["indexed_at"],
                "folder": info["folder"],
            })
            if len(results) >= limit:
                break
        pending = int(_meta(conn, "pending") or 0)
        last = _meta(conn, "last_scan_at")
    answer: Dict[str, Any] = {
        "ok": True, "query": q, "count": len(results), "results": results,
        "folders": folders if roots is None else [f for f in folders if any(path_is_within(f, r) or path_is_within(r, f) for r in roots)],
        "index": {"last_scan_at": last, "pending_files": pending, "searched_files": len(allowed)},
        "how_to_cite": "Answer from these snippets and name each source as its file name with the page when there is one.",
    }
    if refresh.get("paused"):
        answer["index"]["paused"] = refresh["paused"]
    if pending:
        answer["index"]["note"] = f"{pending} new or changed file(s) are still being indexed; results may miss them."
    return answer


# Owner overview -------------------------------------------------------------------------------

def overview() -> Dict[str, Any]:
    folders = configured_folders()
    rows: Dict[str, Dict[str, Any]] = {folder: {"path": folder, "files": 0, "indexed": 0, "no_text": 0, "errors": 0,
                                                 "chunks": 0} for folder in folders}
    last = None
    pending = 0
    if index_dir().joinpath(INDEX_NAME).exists():
        with closing(_connect()) as conn:
            for row in conn.execute("SELECT folder, status, COUNT(*) AS n, SUM(chunks) AS c FROM files GROUP BY folder, status"):
                entry = rows.get(row["folder"])
                if entry is None:
                    continue
                entry["files"] += int(row["n"])
                entry["chunks"] += int(row["c"] or 0)
                key = {"indexed": "indexed", "no_text": "no_text"}.get(row["status"], "errors")
                entry[key] += int(row["n"])
            last = _meta(conn, "last_scan_at")
            pending = int(_meta(conn, "pending") or 0)
    return {"ok": True, "folders": list(rows.values()), "last_scan_at": last, "pending_files": pending,
            "paused": "private_mode" if _private_mode() else None,
            "limits": {"folders": MAX_FOLDERS, "files_per_folder": MAX_FILES_PER_FOLDER, "chunks": MAX_CHUNKS}}


# Background indexer ---------------------------------------------------------------------------

def wake() -> None:
    _WAKE.set()


def _loop() -> None:
    while not _STOP.is_set():
        try:
            if configured_folders():
                result = scan()
                if result.get("pending"):
                    _WAKE.set()  # more to do: go on right away, in budget-sized steps
        except Exception as exc:  # the indexer never takes the server down
            log.warning("knowledge: scan failed: %s", type(exc).__name__)
        _WAKE.wait(SCAN_EVERY_S)
        _WAKE.clear()


def start_indexer() -> None:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="mac-mcp-knowledge", daemon=True)
    _THREAD.start()


def stop_indexer() -> None:
    _STOP.set()
    _WAKE.set()
