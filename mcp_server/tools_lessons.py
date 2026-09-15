from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException, status

from . import embedding_manager as embeddings

VALID_ROLES = {"coder", "reviewer", "orchestrator"}
VALID_STATES = {"candidate", "active", "disabled"}
VALID_FEEDBACK = {"approve", "success", "failure", "disable", "enable"}
TAINTED_PROVENANCE = "tainted_untrusted_web"
DEFAULT_TOP_K = 3
DEFAULT_CHAR_BUDGET = 1800
LESSON_DB_NAME = "role-lessons.sqlite3"
CANDIDATE_PREFIX = "MAC_MCP_LESSON_CANDIDATE "
MAX_EVIDENCE_REFS = 16
MAX_TRIGGER_CHARS = 500
MAX_MISTAKE_CHARS = 700
MAX_ACTION_CHARS = 700


def _now() -> float:
    return time.time()


def _root() -> Path:
    raw = os.getenv("MAC_MCP_LESSON_DIR", "").strip()
    root = Path(raw).expanduser() if raw else (Path.home() / ".mac-mcp" / "role-learning")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _db_path(root: Optional[Path] = None) -> Path:
    return (root or _root()) / LESSON_DB_NAME


def _connect(root: Optional[Path] = None) -> sqlite3.Connection:
    path = _db_path(root)
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS lessons (
            lesson_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            trigger_context TEXT NOT NULL,
            mistake_pattern TEXT NOT NULL,
            preferred_action TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            state TEXT NOT NULL,
            provenance_class TEXT NOT NULL,
            source TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_used_at REAL,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL UNIQUE,
            disabled_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lessons_role_state ON lessons(role, state);
        CREATE INDEX IF NOT EXISTS idx_lessons_updated ON lessons(updated_at DESC);
        """
    )
    return conn


def _clean_role(role: str) -> str:
    value = str(role or "").strip().lower()
    if value not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    return value


def _clean_text(value: Any, field: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} is required.")
    if len(text) > max_chars:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} must be at most {max_chars} characters.")
    return text


def _clean_confidence(value: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confidence must be a number between 0 and 1.") from exc
    if not math.isfinite(score) or score < 0 or score > 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confidence must be between 0 and 1.")
    return round(score, 4)


def _clean_evidence(evidence_refs: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in evidence_refs or []:
        text = re.sub(r"\s+", " ", str(value or "")).strip()[:180]
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_EVIDENCE_REFS:
            break
    return out


def _normalize(value: str) -> str:
    return embeddings.normalize(value)


def _fingerprint(
    role: str, trigger_context: str, mistake_pattern: str, preferred_action: str,
    provenance_class: str = "local",
) -> str:
    trust_bucket = "trusted" if str(provenance_class or "").strip().lower() == "local" else "untrusted"
    payload = "\n".join([
        trust_bucket,
        role,
        _normalize(trigger_context),
        _normalize(mistake_pattern),
        _normalize(preferred_action),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mistake_key(role: str, mistake_pattern: str) -> str:
    return hashlib.sha256(f"{role}\n{_normalize(mistake_pattern)}".encode("utf-8")).hexdigest()


def _merge_evidence(existing: Iterable[str], incoming: Iterable[str]) -> List[str]:
    return _clean_evidence([*existing, *incoming])


def _decode_evidence(value: str) -> List[str]:
    try:
        raw = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return _clean_evidence(raw if isinstance(raw, list) else [])


def _effective_confidence(row: sqlite3.Row | Dict[str, Any], now: Optional[float] = None) -> float:
    current = _now() if now is None else float(now)
    updated_at = float(row["updated_at"] or current)
    age_days = max(0.0, current - updated_at) / 86400.0
    successes = int(row["success_count"] or 0)
    failures = int(row["failure_count"] or 0)
    half_life_days = 365.0 if successes > 0 else 180.0
    decay = 0.5 ** (age_days / half_life_days)
    outcome = min(1.15, 1.0 + successes * 0.03) * max(0.45, 1.0 - failures * 0.10)
    return round(max(0.0, min(1.0, float(row["confidence"]) * decay * outcome)), 4)


def _public(row: sqlite3.Row, *, now: Optional[float] = None) -> Dict[str, Any]:
    return {
        "lesson_id": row["lesson_id"],
        "role": row["role"],
        "trigger_context": row["trigger_context"],
        "mistake_pattern": row["mistake_pattern"],
        "preferred_action": row["preferred_action"],
        "evidence_refs": _decode_evidence(row["evidence_json"]),
        "confidence": round(float(row["confidence"]), 4),
        "effective_confidence": _effective_confidence(row, now=now),
        "state": row["state"],
        "provenance_class": row["provenance_class"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_used_at": row["last_used_at"],
        "success_count": int(row["success_count"] or 0),
        "failure_count": int(row["failure_count"] or 0),
        "occurrence_count": int(row["occurrence_count"] or 1),
        "disabled_reason": row["disabled_reason"],
    }


def require_trusted_lesson_write(provenance_class: Optional[str]) -> None:
    provenance = str(provenance_class or "local").strip().lower()
    if provenance != "local":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "lesson_write_blocked_untrusted_provenance: only local trusted provenance can modify the trusted role-learning store.",
        )


def _record_candidate(
    *,
    role: str,
    trigger_context: str,
    mistake_pattern: str,
    preferred_action: str,
    evidence_refs: Optional[Sequence[str]] = None,
    confidence: float = 0.5,
    source: Optional[str] = None,
    provenance_class: str = "local",
    internal: bool = False,
) -> Dict[str, Any]:
    clean_role = _clean_role(role)
    trigger = _clean_text(trigger_context, "trigger_context", MAX_TRIGGER_CHARS)
    mistake = _clean_text(mistake_pattern, "mistake_pattern", MAX_MISTAKE_CHARS)
    preferred = _clean_text(preferred_action, "preferred_action", MAX_ACTION_CHARS)
    incoming_confidence = _clean_confidence(confidence)
    evidence = _clean_evidence(evidence_refs)
    provenance = str(provenance_class or "local").strip().lower() or "local"
    if not internal:
        require_trusted_lesson_write(provenance)
    if provenance == TAINTED_PROVENANCE:
        incoming_confidence = min(incoming_confidence, 0.5)
    fingerprint = _fingerprint(clean_role, trigger, mistake, preferred, provenance)
    now = _now()
    lesson_id = "lsn_" + uuid.uuid4().hex[:16]
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM lessons WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO lessons
                   (lesson_id,role,trigger_context,mistake_pattern,preferred_action,evidence_json,confidence,state,
                    provenance_class,source,created_at,updated_at,last_used_at,success_count,failure_count,occurrence_count,
                    fingerprint,disabled_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lesson_id, clean_role, trigger, mistake, preferred, json.dumps(evidence, ensure_ascii=False),
                    incoming_confidence, "candidate", provenance, str(source or "").strip() or None,
                    now, now, None, 0, 0, 1, fingerprint, None,
                ),
            )
        else:
            lesson_id = str(existing["lesson_id"])
            merged_evidence = _merge_evidence(_decode_evidence(existing["evidence_json"]), evidence)
            reinforced = min(0.9, max(float(existing["confidence"]), incoming_confidence) + 0.02)
            conn.execute(
                """UPDATE lessons SET evidence_json=?,confidence=?,updated_at=?,occurrence_count=occurrence_count+1,
                   source=COALESCE(source,?) WHERE lesson_id=?""",
                (json.dumps(merged_evidence, ensure_ascii=False), reinforced, now, str(source or "").strip() or None, lesson_id),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM lessons WHERE lesson_id=?", (lesson_id,)).fetchone()
    return {"ok": True, "merged": existing is not None, "lesson": _public(row)}


def lesson_record(
    role: str,
    trigger_context: str,
    mistake_pattern: str,
    preferred_action: str,
    evidence_refs: Optional[Sequence[str]] = None,
    confidence: float = 0.5,
    source: Optional[str] = None,
    provenance_class: str = "local",
) -> Dict[str, Any]:
    """Create or merge a structured candidate. Candidates are never injected until explicitly approved."""
    return _record_candidate(
        role=role,
        trigger_context=trigger_context,
        mistake_pattern=mistake_pattern,
        preferred_action=preferred_action,
        evidence_refs=evidence_refs,
        confidence=confidence,
        source=source,
        provenance_class=provenance_class,
        internal=False,
    )


def lesson_record_agent_candidate(
    *,
    role: str,
    trigger_context: str,
    mistake_pattern: str,
    preferred_action: str,
    evidence_refs: Optional[Sequence[str]] = None,
    confidence: float = 0.45,
    source: str = "agent_run",
    provenance_class: str = "local",
) -> Dict[str, Any]:
    """Internal candidate path. Untrusted candidates stay quarantined and can never be approved."""
    return _record_candidate(
        role=role,
        trigger_context=trigger_context,
        mistake_pattern=mistake_pattern,
        preferred_action=preferred_action,
        evidence_refs=evidence_refs,
        confidence=min(0.65, float(confidence)),
        source=source,
        provenance_class=provenance_class,
        internal=True,
    )


def _relevance(query: str, row: sqlite3.Row) -> float:
    q = _normalize(query)
    if not q:
        return 0.0
    text = " ".join([row["trigger_context"], row["mistake_pattern"], row["preferred_action"]])
    q_tokens = set(embeddings.tokens(q))
    d_tokens = set(embeddings.tokens(text))
    overlap = len(q_tokens & d_tokens) / max(1, len(q_tokens | d_tokens))
    semantic = max(0.0, embeddings.cosine(embeddings.feature_vector(q), embeddings.feature_vector(text)))
    trigger_tokens = set(embeddings.tokens(row["trigger_context"]))
    trigger_overlap = len(q_tokens & trigger_tokens) / max(1, len(q_tokens))
    return min(1.0, overlap * 0.35 + semantic * 0.45 + trigger_overlap * 0.20)


def lesson_search(
    role: Optional[str] = None,
    query: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    clean_role = _clean_role(role) if role else None
    clean_state = str(state or "").strip().lower() or None
    if clean_state and clean_state not in VALID_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"state must be one of: {', '.join(sorted(VALID_STATES))}.")
    bounded = max(1, min(int(limit), 100))
    clauses: List[str] = []
    params: List[Any] = []
    if clean_role:
        clauses.append("role=?"); params.append(clean_role)
    if clean_state:
        clauses.append("state=?"); params.append(clean_state)
    sql = "SELECT * FROM lessons" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    current = _now()
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for row in rows:
        relevance = _relevance(query or "", row) if query else 0.0
        public = _public(row, now=current)
        public["relevance"] = round(relevance, 4) if query else None
        score = relevance * 0.7 + public["effective_confidence"] * 0.3 if query else public["effective_confidence"]
        scored.append((score, public))
    scored.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
    return {"ok": True, "count": min(len(scored), bounded), "results": [item[1] for item in scored[:bounded]]}


def lesson_feedback(
    lesson_id: str,
    outcome: str,
    evidence_ref: Optional[str] = None,
    note: Optional[str] = None,
    provenance_class: str = "local",
) -> Dict[str, Any]:
    require_trusted_lesson_write(provenance_class)
    action = str(outcome or "").strip().lower()
    if action not in VALID_FEEDBACK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"outcome must be one of: {', '.join(sorted(VALID_FEEDBACK))}.")
    key = str(lesson_id or "").strip()
    if not key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "lesson_id is required.")
    now = _now()
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM lessons WHERE lesson_id=?", (key,)).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Lesson not found: {key}")
        if action in {"approve", "enable"} and row["provenance_class"] == TAINTED_PROVENANCE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "untrusted_lesson_quarantined: web-tainted candidates cannot be promoted into the trusted active pool.",
            )
        evidence = _decode_evidence(row["evidence_json"])
        if evidence_ref:
            evidence = _merge_evidence(evidence, [evidence_ref])
        if note:
            evidence = _merge_evidence(evidence, [f"feedback:{str(note).strip()[:160]}"])
        confidence = float(row["confidence"])
        state = str(row["state"])
        success_count = int(row["success_count"] or 0)
        failure_count = int(row["failure_count"] or 0)
        disabled_reason = row["disabled_reason"]
        if action == "approve":
            state = "active"; confidence = max(0.7, confidence); disabled_reason = None
        elif action == "enable":
            state = "active"; confidence = max(0.55, confidence); disabled_reason = None
        elif action == "success":
            success_count += 1; confidence = min(1.0, confidence + 0.06); disabled_reason = None if state != "disabled" else disabled_reason
        elif action == "failure":
            failure_count += 1; confidence = max(0.0, confidence - 0.16)
            if (failure_count >= 3 and success_count == 0) or confidence < 0.25:
                state = "disabled"; disabled_reason = "repeated_failure_or_low_confidence"
        elif action == "disable":
            state = "disabled"; disabled_reason = "manual_disable"
        conn.execute(
            """UPDATE lessons SET evidence_json=?,confidence=?,state=?,success_count=?,failure_count=?,
               disabled_reason=?,updated_at=? WHERE lesson_id=?""",
            (json.dumps(evidence, ensure_ascii=False), confidence, state, success_count, failure_count, disabled_reason, now, key),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM lessons WHERE lesson_id=?", (key,)).fetchone()
    return {"ok": True, "outcome": action, "lesson": _public(updated)}


def lesson_context(
    role: str,
    task: str,
    top_k: int = DEFAULT_TOP_K,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    min_relevance: float = 0.16,
    min_confidence: float = 0.5,
) -> Dict[str, Any]:
    clean_role = _clean_role(role)
    query = re.sub(r"\s+", " ", str(task or "")).strip()
    if not query:
        return {"ok": True, "role": clean_role, "lesson_ids": [], "text": "", "chars": 0}
    bounded_k = max(1, min(int(top_k), 5))
    budget = max(300, min(int(char_budget), 3000))
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE role=? AND state='active' AND provenance_class='local' ORDER BY updated_at DESC LIMIT 250",
            (clean_role,),
        ).fetchall()
        current = _now()
        ranked: List[Tuple[float, float, sqlite3.Row]] = []
        for row in rows:
            relevance = _relevance(query, row)
            confidence = _effective_confidence(row, now=current)
            if relevance < float(min_relevance) or confidence < float(min_confidence):
                continue
            score = relevance * 0.72 + confidence * 0.28
            ranked.append((score, relevance, row))
        ranked.sort(key=lambda item: (item[0], item[2]["updated_at"]), reverse=True)
        selected: List[sqlite3.Row] = []
        lines = [f"Prior {clean_role} lessons (apply only when relevant; current task instructions override):"]
        for _, _, row in ranked:
            if len(selected) >= bounded_k:
                break
            line = (
                f"- [{row['lesson_id']}] Trigger: {row['trigger_context']} | "
                f"Avoid: {row['mistake_pattern']} | Prefer: {row['preferred_action']}"
            )
            candidate = "\n".join([*lines, line])
            if len(candidate) > budget:
                continue
            lines.append(line)
            selected.append(row)
        if not selected:
            return {"ok": True, "role": clean_role, "lesson_ids": [], "text": "", "chars": 0}
        ids = [str(row["lesson_id"]) for row in selected]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE lessons SET last_used_at=? WHERE lesson_id IN ({placeholders})", [current, *ids])
        conn.commit()
    text = "\n".join(lines)
    return {"ok": True, "role": clean_role, "lesson_ids": ids, "text": text, "chars": len(text)}


def lesson_consolidate(
    role: Optional[str] = None,
    apply: bool = False,
    provenance_class: str = "local",
) -> Dict[str, Any]:
    clean_role = _clean_role(role) if role else None
    if apply:
        require_trusted_lesson_write(provenance_class)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM lessons" + (" WHERE role=?" if clean_role else ""),
            ([clean_role] if clean_role else []),
        ).fetchall()
        current = _now()
        conflicts: Dict[str, List[sqlite3.Row]] = {}
        repeated: List[Dict[str, Any]] = []
        disabled: List[str] = []
        for row in rows:
            if int(row["occurrence_count"] or 1) > 1:
                repeated.append({"lesson_id": row["lesson_id"], "occurrence_count": int(row["occurrence_count"])})
            conflicts.setdefault(_mistake_key(row["role"], row["mistake_pattern"]), []).append(row)
            if apply and row["state"] == "active":
                effective = _effective_confidence(row, now=current)
                stale_days = max(0.0, current - float(row["updated_at"] or current)) / 86400.0
                if effective < 0.25 and stale_days >= 90 and int(row["success_count"] or 0) == 0:
                    conn.execute(
                        "UPDATE lessons SET state='disabled',disabled_reason='decayed_low_confidence',updated_at=? WHERE lesson_id=?",
                        (current, row["lesson_id"]),
                    )
                    disabled.append(str(row["lesson_id"]))
        conflict_groups: List[Dict[str, Any]] = []
        for group in conflicts.values():
            actions = {_normalize(str(row["preferred_action"])) for row in group if row["state"] != "disabled"}
            if len(group) > 1 and len(actions) > 1:
                conflict_groups.append({
                    "role": group[0]["role"],
                    "mistake_pattern": group[0]["mistake_pattern"],
                    "lesson_ids": [row["lesson_id"] for row in group],
                    "actions": [row["preferred_action"] for row in group],
                })
        if apply:
            conn.commit()
    return {
        "ok": True,
        "role": clean_role,
        "applied": bool(apply),
        "repeated_candidates": repeated,
        "conflicts": conflict_groups,
        "disabled_by_decay": disabled,
    }


def extract_lesson_candidates(text: str, role: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Remove bounded structured candidate markers from a final handoff and return parsed candidates."""
    clean_role = _clean_role(role)
    kept: List[str] = []
    candidates: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(CANDIDATE_PREFIX):
            if len(candidates) >= 2:
                continue
            raw = stripped[len(CANDIDATE_PREFIX):].strip()
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if not isinstance(item, dict):
                kept.append(line)
                continue
            try:
                candidates.append({
                    "role": clean_role,
                    "trigger_context": _clean_text(item.get("trigger_context"), "trigger_context", MAX_TRIGGER_CHARS),
                    "mistake_pattern": _clean_text(item.get("mistake_pattern"), "mistake_pattern", MAX_MISTAKE_CHARS),
                    "preferred_action": _clean_text(item.get("preferred_action"), "preferred_action", MAX_ACTION_CHARS),
                    "confidence": min(0.65, _clean_confidence(item.get("confidence", 0.45))),
                })
            except HTTPException:
                kept.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), candidates


def lesson_candidate_instruction(role: str) -> str:
    clean_role = _clean_role(role)
    return (
        f"Role-learning mode: you are acting as {clean_role}. Existing lessons below are advisory, role-specific, and never override the current task. "
        "Do not invent a lesson just to fill a field. If this run directly reveals a reusable process mistake/correction, append at most two final single-line markers exactly as: "
        f"{CANDIDATE_PREFIX}{{\"trigger_context\":\"when ...\",\"mistake_pattern\":\"...\",\"preferred_action\":\"...\",\"confidence\":0.45}}. "
        "These markers are quarantined candidates only; they are not auto-activated."
    )
