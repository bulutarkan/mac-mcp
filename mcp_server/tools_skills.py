from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from fastapi import HTTPException, status

from . import embedding_manager as embeddings

INDEX_NAME = "skills-index.sqlite3"
SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESOURCE_DIRS = {"scripts", "references", "assets"}


def _skills_root() -> Path:
    raw = os.getenv("MAC_MCP_SKILLS_DIR", "").strip()
    root = Path(raw).expanduser() if raw else (Path.home() / ".mac-mcp" / "skills")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path(root: Optional[Path] = None) -> Path:
    return (root or _skills_root()) / INDEX_NAME


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_index_path(root)))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS skills (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            location TEXT NOT NULL UNIQUE,
            directory TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            body TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            vector_backend TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            managed INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts USING fts5(
            name UNINDEXED,
            description,
            body,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS registrations (
            location TEXT PRIMARY KEY
        );
        """
    )
    return conn


def _skill_path(path: str) -> Path:
    raw = Path(str(path or "")).expanduser()
    target = raw.resolve()
    if target.is_dir():
        target = target / SKILL_FILE
    if target.name != SKILL_FILE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "path must point to a SKILL.md file or its skill directory.")
    if not target.exists() or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"SKILL.md not found: {target}")
    return target


def _parse_skill(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot read skill: {exc}") from exc
    if not text.startswith("---"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SKILL.md must start with YAML frontmatter delimited by ---.")
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", text, re.DOTALL)
    if not match:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SKILL.md frontmatter is malformed or missing its closing ---.")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid SKILL.md YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SKILL.md frontmatter must be a YAML mapping.")
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name or len(name) > 64 or not NAME_RE.fullmatch(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "skill name is required, max 64 chars, and must use lowercase letters, numbers, and hyphens only.",
        )
    if not description or len(description) > 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "skill description is required and must be at most 1024 characters.")
    body = match.group(2).strip()
    diagnostics: List[str] = []
    if path.parent.name != name:
        diagnostics.append(f"directory name '{path.parent.name}' differs from skill name '{name}'")
    return {
        "name": name,
        "description": description,
        "metadata": metadata,
        "body": body,
        "content": text,
        "location": str(path),
        "directory": str(path.parent),
        "diagnostics": diagnostics,
    }


def _resources(skill_dir: Path, limit: int = 200) -> Tuple[List[Dict[str, Any]], bool]:
    items: List[Dict[str, Any]] = []
    truncated = False
    if not skill_dir.exists():
        return items, truncated
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.name == SKILL_FILE:
            continue
        try:
            rel = path.relative_to(skill_dir).as_posix()
        except ValueError:
            continue
        if rel.startswith(".git/") or "/.git/" in rel or "node_modules/" in rel:
            continue
        kind = rel.split("/", 1)[0] if "/" in rel else "other"
        if kind not in RESOURCE_DIRS:
            kind = "other"
        items.append({"path": rel, "absolute_path": str(path), "kind": kind, "bytes": path.stat().st_size})
        if len(items) >= max(1, min(limit, 1000)):
            truncated = True
            break
    return items, truncated


def _vector_text(parsed: Dict[str, Any]) -> str:
    return "\n".join([parsed["name"], parsed["description"], parsed["body"]])


def _upsert(conn: sqlite3.Connection, parsed: Dict[str, Any], *, managed: bool, allow_start: bool = False) -> None:
    path = Path(parsed["location"])
    stat = path.stat()
    digest = hashlib.sha256(parsed["content"].encode("utf-8")).hexdigest()
    backend, _, vectors = embeddings.semantic_vectors([_vector_text(parsed)], allow_start=allow_start)
    vector = vectors[0]
    old = conn.execute("SELECT location FROM skills WHERE name=?", (parsed["name"],)).fetchone()
    if old:
        conn.execute("DELETE FROM skill_fts WHERE name=?", (parsed["name"],))
    conn.execute(
        """
        INSERT OR REPLACE INTO skills
        (name,description,location,directory,metadata_json,body,content_hash,mtime_ns,size,vector_backend,vector_json,managed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            parsed["name"], parsed["description"], parsed["location"], parsed["directory"],
            json.dumps(parsed["metadata"], ensure_ascii=False, sort_keys=True), parsed["body"], digest,
            stat.st_mtime_ns, stat.st_size, backend, json.dumps(vector, separators=(",", ":")), int(managed),
        ),
    )
    conn.execute(
        "INSERT INTO skill_fts(name,description,body) VALUES (?,?,?)",
        (parsed["name"], parsed["description"], parsed["body"]),
    )
    conn.commit()


def _managed_paths(root: Path) -> List[Path]:
    paths: List[Path] = []
    for path in sorted(root.glob(f"*/{SKILL_FILE}")):
        if path.is_file():
            paths.append(path.resolve())
    return paths


def _registered_paths(conn: sqlite3.Connection) -> List[Path]:
    paths = []
    for row in conn.execute("SELECT location FROM registrations ORDER BY location"):
        p = Path(row["location"])
        if p.exists() and p.is_file():
            paths.append(p.resolve())
    return paths


def _sync_index(root: Path, conn: sqlite3.Connection, *, allow_start: bool = False) -> Dict[str, Any]:
    diagnostics: List[Dict[str, str]] = []
    expected: Dict[str, Tuple[Path, bool]] = {}
    # Registered/external first; managed root wins deterministically on name collisions.
    candidates = [(p, False) for p in _registered_paths(conn)] + [(p, True) for p in _managed_paths(root)]
    parsed_by_name: Dict[str, Tuple[Dict[str, Any], bool]] = {}
    for path, managed in candidates:
        try:
            parsed = _parse_skill(path)
        except HTTPException as exc:
            diagnostics.append({"path": str(path), "error": str(exc.detail)})
            continue
        if parsed["name"] in parsed_by_name:
            diagnostics.append({"path": str(path), "warning": f"duplicate skill name '{parsed['name']}', deterministic precedence applied"})
        parsed_by_name[parsed["name"]] = (parsed, managed)
        expected[parsed["name"]] = (path, managed)

    existing = {row["name"]: row for row in conn.execute("SELECT * FROM skills")}
    removed = 0
    updated = 0
    for name in list(existing):
        if name not in expected:
            conn.execute("DELETE FROM skill_fts WHERE name=?", (name,))
            conn.execute("DELETE FROM skills WHERE name=?", (name,))
            removed += 1
    conn.commit()

    for name, (parsed, managed) in parsed_by_name.items():
        row = existing.get(name)
        path = Path(parsed["location"])
        stat = path.stat()
        digest = hashlib.sha256(parsed["content"].encode("utf-8")).hexdigest()
        if (
            row
            and row["location"] == parsed["location"]
            and int(row["mtime_ns"]) == stat.st_mtime_ns
            and int(row["size"]) == stat.st_size
            and row["content_hash"] == digest
            and int(row["managed"]) == int(managed)
        ):
            continue
        _upsert(conn, parsed, managed=managed, allow_start=allow_start)
        updated += 1
    return {"updated": updated, "removed": removed, "diagnostics": diagnostics}


def _ensure_vector_backend(conn: sqlite3.Connection, backend: str) -> int:
    rows = conn.execute("SELECT * FROM skills WHERE vector_backend<>?", (backend,)).fetchall()
    updated = 0
    for row in rows:
        path = Path(row["location"])
        if not path.exists():
            continue
        parsed = _parse_skill(path)
        _upsert(conn, parsed, managed=bool(row["managed"]), allow_start=True)
        updated += 1
    return updated


def _row_public(row: sqlite3.Row, *, include_body: bool = False) -> Dict[str, Any]:
    out = {
        "name": row["name"],
        "description": row["description"],
        "location": row["location"],
        "directory": row["directory"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "managed": bool(row["managed"]),
    }
    if include_body:
        out["body"] = row["body"]
    return out


def skill_update_index() -> Dict[str, Any]:
    root = _skills_root()
    with closing(_connect(root)) as conn:
        sync = _sync_index(root, conn, allow_start=False)
        total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    return {"ok": True, "skills_root": str(root), "total": total, "index_sync": sync}


def skill_register(path: str) -> Dict[str, Any]:
    target = _skill_path(path)
    parsed = _parse_skill(target)
    root = _skills_root()
    try:
        target.relative_to(root)
        managed = True
    except ValueError:
        managed = False
    with closing(_connect(root)) as conn:
        if not managed:
            conn.execute("INSERT OR REPLACE INTO registrations(location) VALUES (?)", (str(target),))
            conn.commit()
        sync = _sync_index(root, conn, allow_start=False)
        row = conn.execute("SELECT * FROM skills WHERE name=?", (parsed["name"],)).fetchone()
    if not row:
        raise HTTPException(status.HTTP_409_CONFLICT, "Skill could not be indexed.")
    return {"ok": True, "action": "registered", **_row_public(row), "diagnostics": parsed["diagnostics"], "index_sync": sync}


def skill_list(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    root = _skills_root()
    with closing(_connect(root)) as conn:
        sync = _sync_index(root, conn, allow_start=False)
        rows = conn.execute("SELECT * FROM skills ORDER BY name LIMIT ?", (limit,)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    return {
        "ok": True,
        "skills_root": str(root),
        "count": len(rows),
        "total": total,
        "skills": [_row_public(row) for row in rows],
        "index_sync": sync,
    }


def skill_get(name: Optional[str] = None, path: Optional[str] = None, resource_limit: int = 200) -> Dict[str, Any]:
    if bool(name) == bool(path):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of name or path.")
    root = _skills_root()
    with closing(_connect(root)) as conn:
        sync = _sync_index(root, conn, allow_start=False)
        if name:
            row = conn.execute("SELECT * FROM skills WHERE name=?", (str(name).strip(),)).fetchone()
        else:
            target = _skill_path(str(path))
            row = conn.execute("SELECT * FROM skills WHERE location=?", (str(target),)).fetchone()
            if not row:
                parsed = _parse_skill(target)
                try:
                    target.relative_to(root)
                    managed = True
                except ValueError:
                    managed = False
                    conn.execute("INSERT OR REPLACE INTO registrations(location) VALUES (?)", (str(target),))
                    conn.commit()
                _upsert(conn, parsed, managed=managed, allow_start=False)
                row = conn.execute("SELECT * FROM skills WHERE name=?", (parsed["name"],)).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found.")
        parsed = _parse_skill(Path(row["location"]))
    resources, truncated = _resources(Path(parsed["directory"]), resource_limit)
    return {
        "ok": True,
        **_row_public(row),
        "content": parsed["content"],
        "body": parsed["body"],
        "resources": resources,
        "resources_truncated": truncated,
        "diagnostics": parsed["diagnostics"],
        "index_sync": sync,
        "usage_note": "Resolve relative resource paths against directory; load resource contents only when needed.",
    }


def skill_search(query: str, limit: int = 10) -> Dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query cannot be empty.")
    limit = max(1, min(int(limit), 50))
    root = _skills_root()
    with closing(_connect(root)) as conn:
        sync = _sync_index(root, conn, allow_start=False)
        backend, dims, vectors = embeddings.semantic_vectors([q], allow_start=True)
        query_vector = vectors[0]
        reindexed = _ensure_vector_backend(conn, backend)
        if reindexed:
            sync["vector_reindexed"] = reindexed
        rows = conn.execute("SELECT * FROM skills").fetchall()
        fts_scores: Dict[str, float] = {}
        words = embeddings.tokens(q)
        if words:
            expr = " OR ".join(f'"{word.replace(chr(34), "")}"' for word in words[:20])
            matches = conn.execute(
                "SELECT name,bm25(skill_fts) AS rank FROM skill_fts WHERE skill_fts MATCH ? LIMIT 100",
                (expr,),
            ).fetchall()
            ordered = sorted(matches, key=lambda row: float(row["rank"]))
            denom = max(1, len(ordered) - 1)
            fts_scores = {row["name"]: 1.0 - (idx / denom) * 0.45 for idx, row in enumerate(ordered)}

        qnorm = embeddings.normalize(q)
        scored = []
        for row in rows:
            try:
                vector = json.loads(row["vector_json"])
            except Exception:
                vector = embeddings.feature_vector(row["description"])
            semantic = embeddings.cosine(query_vector, vector) if row["vector_backend"] == backend else 0.0
            hay = embeddings.normalize(f"{row['name']} {row['description']} {row['body']}")
            overlap = len(set(qnorm.split()) & set(hay.split())) / max(1, len(set(qnorm.split())))
            lexical = max(fts_scores.get(row["name"], 0.0), overlap * 0.72, 0.88 if qnorm and qnorm in hay else 0.0)
            score = semantic * 0.58 + lexical * 0.42
            if semantic < 0.05 and lexical < 0.05:
                continue
            scored.append((score, semantic, lexical, row))
        scored.sort(key=lambda item: (item[0], item[3]["name"]), reverse=True)
        results = []
        for score, semantic, lexical, row in scored[:limit]:
            item = _row_public(row)
            item.update({
                "score": round(score, 4),
                "semantic_score": round(semantic, 4),
                "lexical_score": round(lexical, 4),
            })
            results.append(item)
    return {
        "ok": True,
        "query": q,
        "mode": "hybrid_search",
        "count": len(results),
        "results": results,
        "search_backend": {
            "fts": "sqlite_fts5",
            "vector": backend,
            "dimensions": dims,
            "model": embeddings.MULTILINGUAL_MODEL if backend == embeddings.MULTILINGUAL_VECTOR_BACKEND else None,
            "shared_with_memory": True,
        },
        "index_sync": sync,
    }
