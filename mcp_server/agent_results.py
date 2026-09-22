from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RESULT_ENVELOPE_VERSION = 1
RESULT_ENVELOPE_MARKER = "TASK_RESULT_ENVELOPE_V1"
_CONTRACT_RE = re.compile(
    r"(?ms)^\s*" + re.escape(RESULT_ENVELOPE_MARKER) + r"\s*\n(?:```json\s*)?(\{.*?\})(?:\s*```)?\s*$"
)
_ALLOWED_OUTCOMES = {"success", "partial_failure", "failure"}
_ALLOWED_GATE_DECISIONS = {"pass", "fail"}
_MAX_ITEMS = 256
_MAX_TEXT = 12000


class ResultContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def result_contract_instruction(*, detailed: bool = False) -> str:
    detail = (
        "Use claims/evidence/artifacts generously when they materially support the handoff."
        if detailed
        else "Keep arrays compact and include only material claims/evidence/artifacts."
    )
    return (
        "Return a provider-independent typed handoff. Your final response MUST end with the marker "
        f"{RESULT_ENVELOPE_MARKER} followed by one JSON object (optionally in a json code fence). "
        "The JSON schema is: "
        '{"schema_version":1,"outcome":"success|partial_failure|failure","summary":"...",'
        '"claims":[{"id":"c1","statement":"...","key":"optional-stable-key","value":"optional-value"}],'
        '"evidence":[{"id":"e1","ref":"stable-reference","summary":"...","claim_ids":["c1"]}],'
        '"artifacts":[{"id":"a1","ref":"path-or-uri","description":"...","sha256":"optional"}],'
        '"warnings":["..."],"confidence":0.0,'
        '"errors":[{"subtask_id":"optional","code":"...","message":"..."}],'
        '"provenance":{},'
        '"quality_gate":{"decision":"pass|fail","feedback":"optional"}'
        "}. Omit quality_gate unless you are reviewing another task. "
        "Do not put raw logs, chain-of-thought, secrets, tokens, cookies, or full tool transcripts in the envelope. "
        + detail
    )


def _clean_text(value: Any, *, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _clean_id(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip()).strip("-")
    return (text or fallback)[:160]


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True), limit=2000)


def _normalize_claim(item: Any, index: int) -> Dict[str, Any]:
    if isinstance(item, str):
        item = {"statement": item}
    if not isinstance(item, Mapping):
        raise ResultContractError("invalid_claim", f"claims[{index}] must be an object or string")
    statement = _clean_text(item.get("statement") or item.get("text"))
    if not statement:
        raise ResultContractError("invalid_claim", f"claims[{index}] requires statement")
    row: Dict[str, Any] = {
        "id": _clean_id(item.get("id"), f"c{index + 1}"),
        "statement": statement,
    }
    key = _clean_text(item.get("key"), limit=300)
    if key:
        row["key"] = key
    if "value" in item:
        row["value"] = _json_scalar(item.get("value"))
    status = _clean_text(item.get("status"), limit=80)
    if status:
        row["status"] = status
    return row


def _normalize_evidence(item: Any, index: int) -> Dict[str, Any]:
    if isinstance(item, str):
        item = {"ref": item}
    if not isinstance(item, Mapping):
        raise ResultContractError("invalid_evidence", f"evidence[{index}] must be an object or string")
    ref = _clean_text(item.get("ref") or item.get("uri") or item.get("path"), limit=2000)
    summary = _clean_text(item.get("summary") or item.get("description"), limit=4000)
    if not ref and not summary:
        raise ResultContractError("invalid_evidence", f"evidence[{index}] requires ref or summary")
    row: Dict[str, Any] = {
        "id": _clean_id(item.get("id"), f"e{index + 1}"),
        "ref": ref,
        "summary": summary,
        "claim_ids": [
            _clean_id(value, f"claim-{pos + 1}")
            for pos, value in enumerate(list(item.get("claim_ids") or [])[:64])
            if str(value or "").strip()
        ],
    }
    kind = _clean_text(item.get("kind"), limit=80)
    if kind:
        row["kind"] = kind
    return row


def _normalize_artifact(item: Any, index: int) -> Dict[str, Any]:
    if isinstance(item, str):
        item = {"ref": item}
    if not isinstance(item, Mapping):
        raise ResultContractError("invalid_artifact", f"artifacts[{index}] must be an object or string")
    ref = _clean_text(item.get("ref") or item.get("uri") or item.get("path"), limit=3000)
    if not ref:
        raise ResultContractError("invalid_artifact", f"artifacts[{index}] requires ref/path/uri")
    row: Dict[str, Any] = {
        "id": _clean_id(item.get("id"), f"a{index + 1}"),
        "ref": ref,
        "description": _clean_text(item.get("description") or item.get("summary"), limit=3000),
    }
    sha = _clean_text(item.get("sha256"), limit=128).lower()
    if sha:
        row["sha256"] = sha
    return row


def _normalize_error(item: Any, index: int) -> Dict[str, Any]:
    if isinstance(item, str):
        item = {"message": item}
    if not isinstance(item, Mapping):
        raise ResultContractError("invalid_error", f"errors[{index}] must be an object or string")
    message = _clean_text(item.get("message") or item.get("error"), limit=4000)
    if not message:
        raise ResultContractError("invalid_error", f"errors[{index}] requires message")
    row: Dict[str, Any] = {
        "code": _clean_id(item.get("code"), f"error-{index + 1}"),
        "message": message,
    }
    subtask = _clean_text(item.get("subtask_id"), limit=160)
    if subtask:
        row["subtask_id"] = subtask
    return row


def normalize_result_envelope(
    raw: Mapping[str, Any],
    *,
    provenance: Optional[Mapping[str, Any]] = None,
    contract_status: str = "valid",
) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ResultContractError("invalid_root", "result envelope must be an object")
    try:
        version = int(raw.get("schema_version"))
    except Exception as exc:
        raise ResultContractError("invalid_schema_version", "schema_version must be 1") from exc
    if version != RESULT_ENVELOPE_VERSION:
        raise ResultContractError("unsupported_schema_version", f"unsupported schema_version: {version}")
    outcome = str(raw.get("outcome") or "").strip().lower()
    if outcome not in _ALLOWED_OUTCOMES:
        raise ResultContractError("invalid_outcome", "outcome must be success, partial_failure, or failure")
    summary = _clean_text(raw.get("summary"), limit=_MAX_TEXT)
    if not summary:
        raise ResultContractError("missing_summary", "summary is required")
    claims_raw = list(raw.get("claims") or [])
    evidence_raw = list(raw.get("evidence") or [])
    artifacts_raw = list(raw.get("artifacts") or [])
    warnings_raw = list(raw.get("warnings") or [])
    errors_raw = list(raw.get("errors") or raw.get("failed_subtasks") or [])
    for name, values in (
        ("claims", claims_raw), ("evidence", evidence_raw), ("artifacts", artifacts_raw),
        ("warnings", warnings_raw), ("errors", errors_raw),
    ):
        if len(values) > _MAX_ITEMS:
            raise ResultContractError("too_many_items", f"{name} exceeds {_MAX_ITEMS} items")

    confidence_raw = raw.get("confidence", 0.5)
    try:
        confidence = float(confidence_raw)
    except Exception as exc:
        raise ResultContractError("invalid_confidence", "confidence must be numeric") from exc
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise ResultContractError("invalid_confidence", "confidence must be between 0 and 1")

    merged_provenance: Dict[str, Any] = {}
    if isinstance(raw.get("provenance"), Mapping):
        for key, value in raw["provenance"].items():
            if value is not None:
                merged_provenance[str(key)[:80]] = _json_scalar(value)
    for key, value in dict(provenance or {}).items():
        if value is not None:
            merged_provenance[str(key)[:80]] = _json_scalar(value)

    envelope: Dict[str, Any] = {
        "schema_version": RESULT_ENVELOPE_VERSION,
        "contract_status": contract_status,
        "outcome": outcome,
        "summary": summary,
        "claims": [_normalize_claim(item, i) for i, item in enumerate(claims_raw)],
        "evidence": [_normalize_evidence(item, i) for i, item in enumerate(evidence_raw)],
        "artifacts": [_normalize_artifact(item, i) for i, item in enumerate(artifacts_raw)],
        "warnings": [_clean_text(value, limit=3000) for value in warnings_raw if str(value or "").strip()],
        "confidence": round(confidence, 4),
        "errors": [_normalize_error(item, i) for i, item in enumerate(errors_raw)],
        "provenance": merged_provenance,
        "quality_gate": None,
        "truncation": {"truncated": False, "omitted": {}},
    }
    gate = raw.get("quality_gate")
    if gate is not None:
        if not isinstance(gate, Mapping):
            raise ResultContractError("invalid_quality_gate", "quality_gate must be an object")
        decision = str(gate.get("decision") or "").strip().lower()
        if decision not in _ALLOWED_GATE_DECISIONS:
            raise ResultContractError("invalid_quality_gate", "quality_gate.decision must be pass or fail")
        envelope["quality_gate"] = {
            "decision": decision,
            "feedback": _clean_text(gate.get("feedback"), limit=6000),
        }
    return envelope


def _extract_marked_json(text: str) -> Optional[str]:
    marker_index = text.rfind(RESULT_ENVELOPE_MARKER)
    if marker_index < 0:
        return None
    tail = text[marker_index + len(RESULT_ENVELOPE_MARKER):].strip()
    if tail.startswith("```"):
        lines = tail.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        tail = "\n".join(lines).strip()
    return tail


def legacy_result_envelope(
    text: str,
    *,
    provenance: Optional[Mapping[str, Any]] = None,
    quality_gate: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    summary = _clean_text(text, limit=_MAX_TEXT) or "Agent finished without a structured handoff."
    raw: Dict[str, Any] = {
        "schema_version": RESULT_ENVELOPE_VERSION,
        "outcome": "success",
        "summary": summary,
        "claims": [],
        "evidence": [],
        "artifacts": [],
        "warnings": [
            "Legacy provider fallback: no TASK_RESULT_ENVELOPE_V1 marker was emitted; structured claims/evidence may be unavailable."
        ],
        "confidence": 0.5,
        "errors": [],
        "provenance": {},
    }
    if quality_gate:
        raw["quality_gate"] = dict(quality_gate)
    return normalize_result_envelope(raw, provenance=provenance, contract_status="legacy_fallback")


def parse_provider_result(
    text: str,
    *,
    provenance: Optional[Mapping[str, Any]] = None,
    legacy_quality_gate: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    raw_text = str(text or "").strip()
    marked = _extract_marked_json(raw_text)
    if marked is None:
        envelope = legacy_result_envelope(
            raw_text, provenance=provenance, quality_gate=legacy_quality_gate,
        )
        return envelope, {
            "valid": True,
            "contract_status": "legacy_fallback",
            "marker_present": False,
            "error": None,
        }
    try:
        decoded = json.loads(marked)
    except json.JSONDecodeError as exc:
        raise ResultContractError("invalid_json", f"invalid marked result envelope JSON: {exc.msg}") from exc
    envelope = normalize_result_envelope(decoded, provenance=provenance, contract_status="valid")
    return envelope, {
        "valid": True,
        "contract_status": "valid",
        "marker_present": True,
        "error": None,
    }


def _canonical_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dedupe_key(row: Mapping[str, Any], *, fallback_fields: Sequence[str]) -> str:
    for key in ("ref", "sha256"):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    material = "|".join(str(row.get(key) or "").strip() for key in fallback_fields)
    return "hash:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def reduce_task_results(
    task_results: Sequence[Tuple[str, Mapping[str, Any]]],
    *,
    team_id: Optional[str] = None,
) -> Dict[str, Any]:
    ordered = [(str(task_id), copy.deepcopy(dict(envelope))) for task_id, envelope in task_results]
    claims: List[Dict[str, Any]] = []
    evidence_by_key: Dict[str, Dict[str, Any]] = {}
    artifacts_by_key: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    errors: List[Dict[str, Any]] = []
    confidences: List[float] = []
    summaries: List[str] = []
    outcome_rows: List[str] = []
    provenance_children: List[Dict[str, Any]] = []
    keyed_claims: Dict[str, List[Dict[str, Any]]] = {}

    for task_id, envelope in ordered:
        summaries.append(f"{task_id}: {_clean_text(envelope.get('summary'), limit=3000)}")
        outcome_rows.append(str(envelope.get("outcome") or "failure"))
        try:
            confidences.append(float(envelope.get("confidence", 0.0)))
        except Exception:
            pass
        provenance_children.append({
            "task_id": task_id,
            "agent_id": (envelope.get("provenance") or {}).get("agent_id"),
            "contract_status": envelope.get("contract_status"),
        })
        for index, claim in enumerate(list(envelope.get("claims") or [])):
            row = copy.deepcopy(dict(claim))
            original_id = str(row.get("id") or f"c{index + 1}")
            row["source_task_id"] = task_id
            row["source_claim_id"] = original_id
            row["id"] = f"{task_id}:{original_id}"
            claims.append(row)
            key = str(row.get("key") or "").strip()
            if key and "value" in row:
                keyed_claims.setdefault(key, []).append(row)
        for evidence in list(envelope.get("evidence") or []):
            row = copy.deepcopy(dict(evidence))
            key = _dedupe_key(row, fallback_fields=("kind", "summary"))
            existing = evidence_by_key.get(key)
            mapped_claims = [
                f"{task_id}:{cid}" for cid in list(row.get("claim_ids") or [])
            ]
            if existing is None:
                row["source_task_ids"] = [task_id]
                row["source_evidence_ids"] = [str(row.get("id") or "")]
                row["claim_ids"] = mapped_claims
                row["id"] = f"fan-e{len(evidence_by_key) + 1}"
                evidence_by_key[key] = row
            else:
                if task_id not in existing["source_task_ids"]:
                    existing["source_task_ids"].append(task_id)
                source_id = str(row.get("id") or "")
                if source_id and source_id not in existing["source_evidence_ids"]:
                    existing["source_evidence_ids"].append(source_id)
                for claim_id in mapped_claims:
                    if claim_id not in existing["claim_ids"]:
                        existing["claim_ids"].append(claim_id)
        for artifact in list(envelope.get("artifacts") or []):
            row = copy.deepcopy(dict(artifact))
            key = _dedupe_key(row, fallback_fields=("ref", "description"))
            existing = artifacts_by_key.get(key)
            if existing is None:
                row["source_task_ids"] = [task_id]
                row["source_artifact_ids"] = [str(row.get("id") or "")]
                row["id"] = f"fan-a{len(artifacts_by_key) + 1}"
                artifacts_by_key[key] = row
            else:
                if task_id not in existing["source_task_ids"]:
                    existing["source_task_ids"].append(task_id)
                source_id = str(row.get("id") or "")
                if source_id and source_id not in existing["source_artifact_ids"]:
                    existing["source_artifact_ids"].append(source_id)
        for warning in list(envelope.get("warnings") or []):
            message = f"{task_id}: {_clean_text(warning, limit=3000)}"
            if message not in warnings:
                warnings.append(message)
        for error in list(envelope.get("errors") or []):
            row = copy.deepcopy(dict(error))
            row.setdefault("subtask_id", task_id)
            row["source_task_id"] = task_id
            errors.append(row)

    contradictions: List[Dict[str, Any]] = []
    for key in sorted(keyed_claims):
        rows = keyed_claims[key]
        values: Dict[str, List[str]] = {}
        for row in rows:
            values.setdefault(_canonical_value(row.get("value")), []).append(str(row.get("source_task_id")))
        if len(values) > 1:
            contradictions.append({
                "key": key,
                "values": [
                    {"value": json.loads(encoded), "source_task_ids": sorted(set(task_ids))}
                    for encoded, task_ids in sorted(values.items())
                ],
            })
    if contradictions:
        warnings.append(f"Detected {len(contradictions)} conflicting claim key(s) during deterministic fan-in.")

    successes = sum(1 for outcome in outcome_rows if outcome == "success")
    failures = sum(1 for outcome in outcome_rows if outcome == "failure")
    partials = sum(1 for outcome in outcome_rows if outcome == "partial_failure")
    if not ordered or (failures and not successes and not partials):
        outcome = "failure"
    elif failures or partials or errors:
        outcome = "partial_failure"
    else:
        outcome = "success"

    envelope: Dict[str, Any] = {
        "schema_version": RESULT_ENVELOPE_VERSION,
        "contract_status": "reduced",
        "outcome": outcome,
        "summary": "\n".join(summaries) or "No child results.",
        "claims": claims,
        "evidence": list(evidence_by_key.values()),
        "artifacts": list(artifacts_by_key.values()),
        "warnings": warnings,
        "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "errors": errors,
        "provenance": {
            "team_id": team_id,
            "children": provenance_children,
            "reducer": "deterministic-v1",
        },
        "quality_gate": None,
        "contradictions": contradictions,
        "truncation": {"truncated": False, "omitted": {}},
    }
    return envelope


def bound_result_envelope(envelope: Mapping[str, Any], char_limit: int) -> Dict[str, Any]:
    limit = max(500, int(char_limit))
    out = copy.deepcopy(dict(envelope))
    out.setdefault("truncation", {"truncated": False, "omitted": {}})
    original_chars = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    omitted: Dict[str, int] = {}

    def size() -> int:
        return len(json.dumps(out, ensure_ascii=False, separators=(",", ":")))

    for key in ("evidence", "claims", "warnings", "errors", "artifacts", "contradictions"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        while rows and size() > limit:
            rows.pop()
            omitted[key] = omitted.get(key, 0) + 1
    gate = out.get("quality_gate")
    if size() > limit and isinstance(gate, dict):
        feedback = str(gate.get("feedback") or "")
        if feedback:
            keep = max(80, min(len(feedback), limit // 6))
            if len(feedback) > keep:
                gate["feedback"] = feedback[: keep - 1] + "…"
                omitted["quality_gate_feedback_chars"] = len(feedback) - keep
    if size() > limit:
        summary = str(out.get("summary") or "")
        keep = max(80, min(len(summary), limit // 4))
        if len(summary) > keep:
            out["summary"] = summary[: keep - 1] + "…"
            omitted["summary_chars"] = len(summary) - keep
    provenance = out.get("provenance")
    if size() > limit and isinstance(provenance, dict) and isinstance(provenance.get("children"), list):
        children = provenance["children"]
        while children and size() > limit:
            children.pop()
            omitted["provenance_children"] = omitted.get("provenance_children", 0) + 1
    out["truncation"] = {
        "truncated": bool(omitted),
        "omitted": omitted,
        "original_chars": original_chars,
        "returned_chars": 0,
    }
    # The metadata itself consumes space. If still over budget, progressively
    # shorten summary while retaining schema/provenance/truncation fields.
    while size() > limit and len(str(out.get("summary") or "")) > 40:
        summary = str(out.get("summary") or "")
        cut = max(40, len(summary) - max(20, size() - limit))
        removed = len(summary) - cut
        out["summary"] = summary[: max(1, cut - 1)] + "…"
        omitted["summary_chars"] = omitted.get("summary_chars", 0) + removed
    out["truncation"]["truncated"] = bool(omitted)
    out["truncation"]["omitted"] = omitted
    out["truncation"]["returned_chars"] = len(
        json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    )
    return out
