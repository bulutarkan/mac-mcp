from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

SCHEMA_VERSION = 1
MAX_RECEIPTS = 512
MAX_UNCERTAIN_EVENTS = 32
MAX_PENDING_EFFECTS = 32
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")

_SIDE_EFFECT_CAPABILITIES = {
    "local_write",
    "process_control",
    "ui_action",
    "external_side_effect",
    "raw_execution",
    "update_control",
    "agent_delegation",
    "human_interaction",
}

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class WorkflowCheckpointError(RuntimeError):
    code = "workflow_checkpoint_error"


class CheckpointIntegrityError(WorkflowCheckpointError):
    code = "checkpoint_integrity_failed"


class CheckpointUnknownError(WorkflowCheckpointError):
    code = "resume_outcome_unknown"


class CheckpointConflictError(WorkflowCheckpointError):
    code = "resume_conflict"


def _now() -> float:
    return time.time()


def _state_dir() -> Path:
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()


def workflow_root() -> Path:
    return _state_dir() / "workflows"


def _ensure_root() -> Path:
    root = workflow_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(agents, 0o700)
    except OSError:
        pass
    return root


def _validate_id(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not _ID_RE.fullmatch(clean):
        raise WorkflowCheckpointError(f"invalid {field}")
    return clean


def _workflow_path(workflow_id: str) -> Path:
    return workflow_root() / f"{_validate_id(workflow_id, 'workflow_id')}.json"


def _agent_map_path(agent_id: str) -> Path:
    return workflow_root() / "agents" / f"{_validate_id(agent_id, 'agent_id')}.json"


def _lock_key(workflow_id: str) -> str:
    return f"workflow:{_validate_id(workflow_id, 'workflow_id')}"


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _workflow_lock(workflow_id: str) -> Iterator[None]:
    root = _ensure_root()
    key = _lock_key(workflow_id)
    with _thread_lock(key):
        lock_path = root / f".{workflow_id}.lock"
        with lock_path.open("a", encoding="utf-8") as handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def workflow_input_hash(
    *, prompt: str, provider: str, cwd: str, access_mode: str,
    scope: Mapping[str, Any] | None, role: str | None,
) -> str:
    return _sha256({
        "prompt": str(prompt or ""),
        "provider": str(provider or "").lower(),
        "cwd": str(cwd or ""),
        "access_mode": str(access_mode or ""),
        "scope": dict(scope or {}),
        "role": str(role or "").lower() or None,
    })


def _envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "payload": payload,
        "integrity_sha256": _sha256(payload),
    }


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # File fsync + atomic replace is the primary durability boundary;
            # some filesystems do not support fsync on directories.
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _read_envelope(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointIntegrityError(f"checkpoint unreadable: {path.name}") from exc
    if not isinstance(raw, dict) or int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise CheckpointIntegrityError("checkpoint schema mismatch")
    payload = raw.get("payload")
    expected = str(raw.get("integrity_sha256") or "")
    if not isinstance(payload, dict) or not expected or expected != _sha256(payload):
        raise CheckpointIntegrityError("checkpoint integrity mismatch")
    return payload


def _write_workflow(payload: Dict[str, Any]) -> None:
    _atomic_write(_workflow_path(str(payload["workflow_id"])), _envelope(payload))


def _write_agent_map(agent_id: str, *, workflow_id: str, input_hash: str, generation: int) -> None:
    payload = {
        "agent_id": _validate_id(agent_id, "agent_id"),
        "workflow_id": _validate_id(workflow_id, "workflow_id"),
        "input_hash": str(input_hash),
        "resume_generation": int(generation),
        "updated_at": _now(),
    }
    _atomic_write(_agent_map_path(agent_id), _envelope(payload))


def workflow_id_for_agent(agent_id: str) -> Optional[str]:
    path = _agent_map_path(agent_id)
    if not path.exists():
        return None
    payload = _read_envelope(path)
    workflow_id = str(payload.get("workflow_id") or "")
    return _validate_id(workflow_id, "workflow_id")


def read_workflow(workflow_id: str) -> Dict[str, Any]:
    path = _workflow_path(workflow_id)
    if not path.exists():
        raise CheckpointUnknownError("workflow checkpoint is missing")
    return _read_envelope(path)


def workflow_for_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    path = _agent_map_path(agent_id)
    if not path.exists():
        return None
    mapping = _read_envelope(path)
    workflow = read_workflow(str(mapping.get("workflow_id") or ""))
    if str(mapping.get("input_hash") or "") != str(workflow.get("input_hash") or ""):
        raise CheckpointIntegrityError("agent/workflow input hash mismatch")
    if agent_id not in list(workflow.get("agent_lineage") or []):
        raise CheckpointIntegrityError("agent is not in workflow lineage")
    return workflow


def create_workflow(
    *, agent_id: str, input_hash: str, provider: str, workflow_id: Optional[str] = None,
) -> Dict[str, Any]:
    workflow_id = workflow_id or ("wf_" + uuid.uuid4().hex[:16])
    _validate_id(workflow_id, "workflow_id")
    _validate_id(agent_id, "agent_id")
    now = _now()
    payload: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "input_hash": str(input_hash),
        "provider": str(provider or "").lower(),
        "resume_generation": 0,
        "state": "running",
        "safety": "verified",
        "unknown_reason": None,
        "current_agent_id": agent_id,
        "agent_lineage": [agent_id],
        "session_id": None,
        "provider_job_id": None,
        "receipt_count": 0,
        "receipts": [],
        "receipt_chain_sha256": hashlib.sha256(b"").hexdigest(),
        "pending_effects": [],
        "provider_event_count": 0,
        "checkpoint_cursor": None,
        "uncertain_events": [],
        "resume_token": None,
        "resume_parent_agent_id": None,
        "created_at": now,
        "updated_at": now,
        "last_checkpoint_at": now,
    }
    with _workflow_lock(workflow_id):
        if _workflow_path(workflow_id).exists():
            raise CheckpointConflictError("workflow already exists")
        _write_workflow(payload)
        _write_agent_map(agent_id, workflow_id=workflow_id, input_hash=input_hash, generation=0)
    return dict(payload)


def update_provider_state(
    agent_id: str, *, session_id: Optional[str] = None, provider_job_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return None
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        changed = False
        if session_id and str(payload.get("session_id") or "") != str(session_id):
            payload["session_id"] = str(session_id)
            changed = True
        if provider_job_id and str(payload.get("provider_job_id") or "") != str(provider_job_id):
            payload["provider_job_id"] = str(provider_job_id)
            changed = True
        if changed:
            now = _now()
            payload["updated_at"] = now
            payload["last_checkpoint_at"] = now
            _write_workflow(payload)
        return dict(payload)


def _append_uncertain_event(payload: Dict[str, Any], *, reason: str, tool: Optional[str], event_type: Optional[str]) -> None:
    events = list(payload.get("uncertain_events") or [])
    events.append({
        "reason": str(reason)[:120],
        "tool": str(tool or "")[:120] or None,
        "event_type": str(event_type or "")[:80] or None,
        "at": _now(),
        "generation": int(payload.get("resume_generation") or 0),
    })
    payload["uncertain_events"] = events[-MAX_UNCERTAIN_EVENTS:]
    payload["safety"] = "unknown"
    payload["unknown_reason"] = str(reason)[:200]


def mark_checkpoint_unknown(
    agent_id: str, reason: str, *, tool: Optional[str] = None, event_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return None
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        _append_uncertain_event(payload, reason=reason, tool=tool, event_type=event_type)
        now = _now()
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)
        return dict(payload)


def _provider_cursor_kind(provider: str, event: Mapping[str, Any]) -> Optional[str]:
    event_type = str(event.get("type") or "")
    if provider == "codex":
        if event_type == "thread.started":
            return "session_started"
        if event_type in {"turn.started", "turn.completed"}:
            return event_type.replace(".", "_")
        if event_type in {"item.started", "item.completed"}:
            item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
            item_type = str(item.get("type") or "")
            suffix = "started" if event_type.endswith("started") else "completed"
            if item_type == "mcp_tool_call":
                return f"mcp_tool_{suffix}"
            if item_type == "command_execution":
                return f"native_command_{suffix}"
            if item_type == "file_change":
                return f"native_file_change_{suffix}"
            if item_type == "agent_message":
                return "agent_message"
    elif provider == "opencode":
        if event_type in {"step_start", "step_finish"}:
            return "step_started" if event_type == "step_start" else "step_finished"
        if event_type == "tool_use":
            part = event.get("part") if isinstance(event.get("part"), Mapping) else {}
            tool = str(part.get("tool") or "")
            return "mcp_tool_event" if tool.startswith("mac-mcp_") else "native_tool_event"
    elif provider == "chatgpt":
        mapping = {
            "job_started": "session_started",
            "turn_started": "turn_started",
            "turn_completed": "turn_completed",
            "tool_start": "provider_tool_started",
            "tool_end": "provider_tool_completed",
            "interrupted": "interrupted",
            "final": "final",
        }
        return mapping.get(event_type)
    return None


def _record_provider_cursor(agent_id: str, provider: str, event: Mapping[str, Any]) -> None:
    kind = _provider_cursor_kind(provider, event)
    if not kind:
        return
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        if str(payload.get("current_agent_id") or "") != str(agent_id):
            return
        seq = int(payload.get("provider_event_count") or 0) + 1
        now = _now()
        payload["provider_event_count"] = seq
        payload["checkpoint_cursor"] = {
            "seq": seq,
            "kind": kind,
            "resume_generation": int(payload.get("resume_generation") or 0),
            "at": now,
        }
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)


def note_provider_event(agent_id: str, provider: str, event: Mapping[str, Any]) -> None:
    """Persist a sanitized provider milestone and fail closed on native/opaque mutations."""
    provider = str(provider or "").lower()
    event_type = str(event.get("type") or "")
    _record_provider_cursor(agent_id, provider, event)
    if provider == "codex" and event_type == "item.started":
        item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
        item_type = str(item.get("type") or "")
        if item_type in {"command_execution", "file_change"}:
            mark_checkpoint_unknown(agent_id, "unverified_provider_native_activity", tool=item_type, event_type=event_type)
    elif provider == "opencode" and event_type == "tool_use":
        part = event.get("part") if isinstance(event.get("part"), Mapping) else {}
        tool = str(part.get("tool") or "")
        if tool and not tool.startswith("mac-mcp_"):
            mark_checkpoint_unknown(agent_id, "unverified_provider_native_activity", tool=tool, event_type=event_type)
    elif provider == "chatgpt" and event_type == "tool_start":
        mark_checkpoint_unknown(agent_id, "opaque_provider_tool_activity", tool="provider_tool", event_type=event_type)


def risk_has_side_effect(capabilities: Sequence[str], destructive: bool = False) -> bool:
    caps = {str(item) for item in capabilities}
    return bool(destructive or caps.intersection(_SIDE_EFFECT_CAPABILITIES))


def _explicit_ok(value: Any) -> Optional[bool]:
    if isinstance(value, dict):
        if isinstance(value.get("ok"), bool):
            return bool(value["ok"])
        if "result" in value:
            nested = _explicit_ok(value.get("result"))
            if nested is not None:
                return nested
    if isinstance(value, tuple) and len(value) == 2:
        nested = _explicit_ok(value[1])
        if nested is not None:
            return nested
    if isinstance(value, (list, tuple)) and len(value) == 1:
        item = value[0]
        if isinstance(item, dict):
            nested = _explicit_ok(item)
            if nested is not None:
                return nested
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return _explicit_ok(parsed)
    return None


def begin_side_effect(
    agent_id: str, *, tool: str, family: str, capabilities: Sequence[str], destructive: bool,
    arguments: Mapping[str, Any], event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not risk_has_side_effect(capabilities, destructive) or tool == "tool_invoke":
        return None
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        raise CheckpointUnknownError("durable workflow checkpoint is missing for a side-effecting delegated agent")
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        if str(payload.get("current_agent_id") or "") != str(agent_id):
            raise CheckpointConflictError("agent is not the current workflow owner")
        if str(payload.get("state") or "") != "running":
            raise CheckpointConflictError("workflow is not running")
        if str(payload.get("safety") or "verified") == "unknown":
            raise CheckpointUnknownError(str(payload.get("unknown_reason") or "workflow outcome is already unknown"))
        now = _now()
        intent = {
            "intent_id": "intent_" + uuid.uuid4().hex[:16],
            "tool": str(tool)[:120],
            "family": str(family)[:80],
            "destructive": bool(destructive),
            "argument_sha256": _sha256(dict(arguments)),
            "event_id": str(event_id)[:120] if event_id else None,
            "resume_generation": int(payload.get("resume_generation") or 0),
            "started_at": now,
        }
        pending = list(payload.get("pending_effects") or [])
        if len(pending) >= MAX_PENDING_EFFECTS:
            _append_uncertain_event(
                payload, reason="side_effect_intent_overflow", tool=tool, event_type="tool_start",
            )
            payload["updated_at"] = now
            payload["last_checkpoint_at"] = now
            _write_workflow(payload)
            raise CheckpointUnknownError("too many unresolved side-effect intents")
        pending.append(intent)
        payload["pending_effects"] = pending
        if str(payload.get("safety") or "verified") == "verified":
            payload["safety"] = "pending"
            payload["unknown_reason"] = "side_effect_in_flight"
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)
        return {"workflow_id": workflow_id, **intent}


def abandon_side_effect(
    agent_id: str, intent_id: Optional[str], reason: str, *, tool: Optional[str] = None,
    event_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return None
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        pending = list(payload.get("pending_effects") or [])
        matched_tool = tool
        if intent_id:
            kept = []
            for item in pending:
                if str(item.get("intent_id") or "") == str(intent_id):
                    matched_tool = matched_tool or str(item.get("tool") or "") or None
                else:
                    kept.append(item)
            payload["pending_effects"] = kept
        _append_uncertain_event(
            payload, reason=reason, tool=matched_tool, event_type=event_type or "tool_result",
        )
        now = _now()
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)
        return dict(payload)


def record_side_effect_outcome(
    agent_id: str, *, tool: str, family: str, capabilities: Sequence[str], destructive: bool,
    arguments: Mapping[str, Any], result: Any, event_id: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not risk_has_side_effect(capabilities, destructive) or tool == "tool_invoke":
        return None
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return None
    explicit_ok = _explicit_ok(result)
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        now = _now()
        pending = list(payload.get("pending_effects") or [])
        matched_intent: Optional[Dict[str, Any]] = None
        kept: list[Dict[str, Any]] = []
        for item in pending:
            if matched_intent is None and intent_id and str(item.get("intent_id") or "") == str(intent_id):
                matched_intent = dict(item)
            else:
                kept.append(item)
        if not intent_id or matched_intent is None:
            _append_uncertain_event(
                payload, reason="side_effect_intent_missing", tool=tool, event_type="tool_result",
            )
            payload["updated_at"] = now
            payload["last_checkpoint_at"] = now
            _write_workflow(payload)
            return {"verified": False, "workflow_id": workflow_id, "reason": "side_effect_intent_missing"}
        payload["pending_effects"] = kept

        if explicit_ok is False:
            _append_uncertain_event(
                payload, reason="side_effect_result_not_verified", tool=tool, event_type="tool_result",
            )
            payload["updated_at"] = now
            payload["last_checkpoint_at"] = now
            _write_workflow(payload)
            return {"verified": False, "workflow_id": workflow_id, "reason": payload.get("unknown_reason")}

        receipt = {
            "receipt_id": "rcpt_" + uuid.uuid4().hex[:16],
            "intent_id": str((matched_intent or {}).get("intent_id") or intent_id or "") or None,
            "tool": str(tool)[:120],
            "family": str(family)[:80],
            "destructive": bool(destructive),
            "argument_sha256": str((matched_intent or {}).get("argument_sha256") or _sha256(dict(arguments))),
            "result_sha256": _sha256(result),
            "event_id": str(event_id or (matched_intent or {}).get("event_id") or "")[:120] or None,
            "resume_generation": int(payload.get("resume_generation") or 0),
            "verified_at": now,
        }
        previous_chain = str(payload.get("receipt_chain_sha256") or hashlib.sha256(b"").hexdigest())
        payload["receipt_chain_sha256"] = hashlib.sha256((previous_chain + _sha256(receipt)).encode("ascii")).hexdigest()
        receipts = list(payload.get("receipts") or [])
        receipts.append(receipt)
        payload["receipts"] = receipts[-MAX_RECEIPTS:]
        payload["receipt_count"] = int(payload.get("receipt_count") or 0) + 1
        if not payload.get("pending_effects") and str(payload.get("safety") or "") == "pending":
            payload["safety"] = "verified"
            payload["unknown_reason"] = None
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)
        return {"verified": True, **receipt, "workflow_id": workflow_id}


def mark_terminal(agent_id: str, status: str) -> Optional[Dict[str, Any]]:
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        return None
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        if str(payload.get("current_agent_id") or "") != agent_id:
            return dict(payload)
        normalized = str(status or "failed").lower()
        payload["state"] = "completed" if normalized == "completed" else "interrupted"
        now = _now()
        payload["updated_at"] = now
        payload["last_checkpoint_at"] = now
        _write_workflow(payload)
        return dict(payload)


def prepare_resume(agent_id: str, *, expected_input_hash: str, session_id: str) -> Dict[str, Any]:
    workflow_id = workflow_id_for_agent(agent_id)
    if not workflow_id:
        raise CheckpointUnknownError("durable checkpoint is unavailable for this agent")
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        if str(payload.get("input_hash") or "") != str(expected_input_hash or ""):
            raise CheckpointUnknownError("checkpoint input hash does not match the original task")
        if str(payload.get("current_agent_id") or "") != agent_id:
            raise CheckpointConflictError("agent is not the current workflow owner")
        if payload.get("state") == "resuming":
            raise CheckpointConflictError("a resume is already in progress")
        if payload.get("state") == "completed":
            raise CheckpointConflictError("completed workflows do not need crash resume")
        if list(payload.get("pending_effects") or []):
            raise CheckpointUnknownError("side-effect outcome is pending from an interrupted tool call")
        if str(payload.get("safety") or "unknown") != "verified":
            raise CheckpointUnknownError(str(payload.get("unknown_reason") or "checkpoint outcome is unknown"))
        if not session_id:
            raise CheckpointUnknownError("provider session id is unavailable")
        stored_session = str(payload.get("session_id") or "")
        if stored_session and stored_session != str(session_id):
            raise CheckpointUnknownError("provider session id does not match the checkpoint")
        generation = int(payload.get("resume_generation") or 0) + 1
        token = "rsm_" + uuid.uuid4().hex[:20]
        payload.update({
            "state": "resuming",
            "resume_generation": generation,
            "resume_token": token,
            "resume_parent_agent_id": agent_id,
            "session_id": str(session_id),
            "updated_at": _now(),
        })
        _write_workflow(payload)
        return {
            "workflow_id": workflow_id,
            "input_hash": str(payload["input_hash"]),
            "provider": str(payload.get("provider") or ""),
            "resume_generation": generation,
            "resume_token": token,
            "receipt_count": int(payload.get("receipt_count") or 0),
            "receipts": list(payload.get("receipts") or [])[-20:],
            "last_checkpoint_at": payload.get("last_checkpoint_at"),
            "checkpoint_cursor": dict(payload.get("checkpoint_cursor") or {}) or None,
            "session_id": str(session_id),
        }


def bind_resumed_agent(
    *, workflow_id: str, parent_agent_id: str, agent_id: str, input_hash: str,
    resume_generation: int, resume_token: str, session_id: str,
) -> Dict[str, Any]:
    with _workflow_lock(workflow_id):
        payload = read_workflow(workflow_id)
        if payload.get("state") != "resuming":
            raise CheckpointConflictError("workflow is not awaiting a resumed agent")
        if str(payload.get("resume_token") or "") != str(resume_token or ""):
            raise CheckpointConflictError("resume token mismatch")
        if int(payload.get("resume_generation") or 0) != int(resume_generation):
            raise CheckpointConflictError("resume generation mismatch")
        if str(payload.get("resume_parent_agent_id") or "") != str(parent_agent_id):
            raise CheckpointConflictError("resume parent mismatch")
        if str(payload.get("input_hash") or "") != str(input_hash):
            raise CheckpointUnknownError("resume input hash mismatch")
        lineage = list(payload.get("agent_lineage") or [])
        if agent_id not in lineage:
            lineage.append(agent_id)
        payload.update({
            "state": "running",
            "current_agent_id": agent_id,
            "agent_lineage": lineage,
            "session_id": str(session_id),
            "resume_token": None,
            "resume_parent_agent_id": None,
            "updated_at": _now(),
            "last_checkpoint_at": _now(),
        })
        _write_workflow(payload)
        _write_agent_map(agent_id, workflow_id=workflow_id, input_hash=input_hash, generation=resume_generation)
        return dict(payload)


def abort_resume(workflow_id: str, *, resume_token: str, reason: str) -> None:
    try:
        with _workflow_lock(workflow_id):
            payload = read_workflow(workflow_id)
            if payload.get("state") != "resuming" or str(payload.get("resume_token") or "") != str(resume_token):
                return
            payload.update({
                "state": "interrupted",
                "resume_token": None,
                "resume_parent_agent_id": None,
                "last_resume_error": str(reason)[:200],
                "updated_at": _now(),
            })
            _write_workflow(payload)
    except WorkflowCheckpointError:
        return


def rollback_resumed_agent(workflow_id: str, *, parent_agent_id: str, agent_id: str, reason: str) -> None:
    try:
        with _workflow_lock(workflow_id):
            payload = read_workflow(workflow_id)
            if str(payload.get("current_agent_id") or "") != str(agent_id):
                return
            payload.update({
                "state": "interrupted",
                "current_agent_id": str(parent_agent_id),
                "last_resume_error": str(reason)[:200],
                "updated_at": _now(),
                "last_checkpoint_at": _now(),
            })
            payload["agent_lineage"] = [
                item for item in list(payload.get("agent_lineage") or []) if item != agent_id
            ]
            _write_workflow(payload)
            _agent_map_path(agent_id).unlink(missing_ok=True)
    except WorkflowCheckpointError:
        return


def resume_prompt(checkpoint: Mapping[str, Any]) -> str:
    receipts = list(checkpoint.get("receipts") or [])[-12:]
    lines = [
        "Resume the same delegated task from the existing provider conversation after a verified durable checkpoint boundary.",
        f"Workflow: {checkpoint.get('workflow_id')} | resume generation: {checkpoint.get('resume_generation')} | verified side-effect receipts: {checkpoint.get('receipt_count', 0)}.",
        "Do not replay completed side effects. Reconcile current state before any new mutation or external action.",
        "If current state conflicts with these receipts, or you cannot determine whether an unrecorded side effect happened, stop and report outcome unknown instead of guessing or repeating the action.",
    ]
    cursor = checkpoint.get("checkpoint_cursor") if isinstance(checkpoint.get("checkpoint_cursor"), Mapping) else None
    if cursor:
        lines.append(
            f"Last durable provider cursor: seq={int(cursor.get('seq') or 0)} kind={str(cursor.get('kind') or 'unknown')} "
            f"generation={int(cursor.get('resume_generation') or 0)}."
        )
    if receipts:
        lines.append("Recent verified receipts (identifiers and hashes only; no raw sensitive arguments/results):")
        for item in receipts:
            lines.append(
                f"- {item.get('receipt_id')} tool={item.get('tool')} family={item.get('family')} "
                f"arg={str(item.get('argument_sha256') or '')[:12]} result={str(item.get('result_sha256') or '')[:12]}"
            )
    return "\n".join(lines)


def public_state(agent_id: str) -> Dict[str, Any]:
    try:
        workflow = workflow_for_agent(agent_id)
    except WorkflowCheckpointError as exc:
        return {
            "workflow_id": None,
            "resume_generation": None,
            "checkpoint_state": "unknown",
            "checkpoint_safety": "unknown",
            "checkpoint_reason": exc.code,
            "side_effect_receipt_count": 0,
            "pending_side_effect_count": 0,
            "checkpoint_cursor": None,
            "resumable": False,
        }
    if workflow is None:
        return {
            "workflow_id": None,
            "resume_generation": None,
            "checkpoint_state": "legacy",
            "checkpoint_safety": "unknown",
            "checkpoint_reason": "no_durable_checkpoint",
            "side_effect_receipt_count": 0,
            "pending_side_effect_count": 0,
            "checkpoint_cursor": None,
            "resumable": False,
        }
    state = str(workflow.get("state") or "unknown")
    pending_effects = list(workflow.get("pending_effects") or [])
    safety = "unknown" if pending_effects else str(workflow.get("safety") or "unknown")
    reason = "side_effect_in_flight" if pending_effects else workflow.get("unknown_reason")
    return {
        "workflow_id": workflow.get("workflow_id"),
        "resume_generation": int(workflow.get("resume_generation") or 0),
        "checkpoint_state": state,
        "checkpoint_safety": safety,
        "checkpoint_reason": reason,
        "side_effect_receipt_count": int(workflow.get("receipt_count") or 0),
        "pending_side_effect_count": len(pending_effects),
        "checkpoint_cursor": dict(workflow.get("checkpoint_cursor") or {}) or None,
        "last_durable_checkpoint_at": workflow.get("last_checkpoint_at"),
        "resumable": bool(state == "interrupted" and safety == "verified" and workflow.get("session_id")),
    }
