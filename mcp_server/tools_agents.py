from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal as signal_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi import HTTPException, status

from .policy import PolicyContext, PROFILES, current_policy_context, narrow_child_profile, profile_contains
from .policy_scope import (
    ResourceScope, access_mode_allows, child_scope, normalize_access_mode, scope_contains,
)
from .scoped_auth import get_scoped_credential_store
from .security import BASE_DIR, Settings, truncate
from .runtime_settings import provider_enabled, provider_setting
from .tools_lessons import (
    VALID_ROLES, TAINTED_PROVENANCE, extract_lesson_candidates, lesson_candidate_instruction,
    lesson_context, lesson_record_agent_candidate,
)
from .workflow_checkpoints import (
    CheckpointConflictError, CheckpointUnknownError, WorkflowCheckpointError,
    abort_resume, bind_resumed_agent, create_workflow, mark_terminal as workflow_mark_terminal,
    note_provider_event, prepare_resume, public_state as workflow_public_state,
    resume_prompt as durable_resume_prompt, rollback_resumed_agent, update_provider_state,
    workflow_for_agent, workflow_input_hash,
)
from . import browser_tabs
from .agent_worktrees import (
    GIT_ISOLATION_MODES, AgentWorktreeError, apply_worktree, cleanup_worktree,
    inspect_worktree, prepare_worktree, remapped_roots, resolve_git_base, reuse_worktree, seed_worktree,
)
from .agent_admission import (
    AdmissionError, bind_agent as admission_bind_agent, cancel_queued as admission_cancel_queued,
    heartbeat as admission_heartbeat, normalize_claims as normalize_admission_claims, release as admission_release,
    request_admission, snapshot as admission_snapshot,
)

AGENTS_DIR = BASE_DIR / "agents"
TEAMS_DIR = BASE_DIR / "agent_teams"
TERMINAL_STATUSES = {"completed", "failed", "timeout", "stalled", "cancelled"}
DEFAULT_AGENT_TIMEOUT_S = 1800
MAX_AGENT_TIMEOUT_S = 7200
DEFAULT_RESULT_LIMIT = 6000
DETAILED_RESULT_LIMIT = 20000
TEAM_RESULT_LIMIT = 2000
DEFAULT_WAIT_TIMEOUT_S = 30
MAX_WAIT_TIMEOUT_S = 300
MAX_TEAM_SIZE = 10
DEFAULT_CHATGPT_TURN_BUDGET_S = 900
DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S = 1200
DEFAULT_CHATGPT_RATE_LIMIT_BACKOFF_S = 90
MAX_CHATGPT_RATE_LIMIT_BACKOFF_S = 600
DEFAULT_CHATGPT_REDUCED_CONCURRENCY_S = 900
DEFAULT_CHATGPT_START_SPACING_S = 15
DEFAULT_TEAM_TIMEOUT_FLOOR_S = 3600
MAX_TEAM_TIMEOUT_S = 86400
MAX_TEAM_RETRY_BUDGET = 30
MAX_TEAM_TOOL_BUDGET = 100000
MAX_TEAM_TOKEN_BUDGET = 100000000
TEAM_RETRY_START_SPACING_S = 0.75
MAX_GENERIC_RETRY_BACKOFF_S = 30.0

_PROVIDER_NAMES = {"opencode", "codex", "chatgpt"}
_ACCESS_MODES = {"read_only", "workspace_write", "full"}
_RESULT_STYLES = {"concise", "detailed"}
_WAIT_MODES = {"all", "any", "majority"}
_LINEAGE_VERSION = 1
_CONTROL_DENY_ERROR = "agent_control_denied"

_AGENT_CAPABILITY_PROFILES: Dict[str, Dict[str, Any]] = {
    "browser_only": {"access_mode": "read_only", "permission_profile": "browser_only", "tool_families": ("browser",)},
    "read_only": {"access_mode": "read_only", "permission_profile": "read_only", "tool_families": None},
    "developer": {
        "access_mode": "workspace_write", "permission_profile": "developer",
        "tool_families": ("terminal", "jobs", "files", "search", "http", "agents", "memory", "skills"),
    },
    "full": {"access_mode": "full", "permission_profile": "trusted", "tool_families": None},
}
_WORKERS: Dict[str, subprocess.Popen] = {}
_WORKERS_LOCK = threading.RLock()
_META_LOCKS: Dict[str, threading.RLock] = {}
_META_LOCKS_GUARD = threading.Lock()
_TEAM_LOCKS: Dict[str, threading.RLock] = {}
_TEAM_LOCKS_GUARD = threading.Lock()
_GRAPH_TASK_TERMINAL = {"completed", "failed", "quality_failed", "skipped", "cancelled"}
_QUALITY_GATE_RE = re.compile(r"(?im)^\s*QUALITY_GATE:\s*(PASS|FAIL)\s*$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _now() -> float:
    return time.time()


def _agent_dir(agent_id: str) -> Path:
    if not agent_id or "/" in agent_id or ".." in agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid agent_id.")
    return AGENTS_DIR / agent_id


def _meta_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "meta.json"


def _meta_thread_lock(agent_id: str) -> threading.RLock:
    with _META_LOCKS_GUARD:
        return _META_LOCKS.setdefault(agent_id, threading.RLock())


@contextmanager
def _locked_meta(agent_id: str, *, create_parent: bool = False) -> Iterator[None]:
    """Serialize one agent's metadata across both threads and worker processes."""
    path = _meta_path(agent_id)
    thread_lock = _meta_thread_lock(agent_id)
    with thread_lock:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.exists():
            yield
            return
        lock_path = path.parent / ".meta.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_meta_unlocked(agent_id: str) -> Dict[str, Any]:
    path = _meta_path(agent_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt agent metadata: {agent_id}") from exc


def _write_meta_unlocked(agent_id: str, meta: Dict[str, Any]) -> None:
    path = _meta_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".meta.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_meta(agent_id: str) -> Dict[str, Any]:
    with _locked_meta(agent_id):
        return _read_meta_unlocked(agent_id)


def _write_meta(agent_id: str, meta: Dict[str, Any]) -> None:
    with _locked_meta(agent_id, create_parent=True):
        _write_meta_unlocked(agent_id, meta)


def _update_meta(
    agent_id: str,
    update: Callable[[Dict[str, Any]], Optional[bool]],
) -> Dict[str, Any]:
    """Atomically update one agent; return False from update to skip the write."""
    with _locked_meta(agent_id):
        meta = _read_meta_unlocked(agent_id)
        if update(meta) is not False:
            _write_meta_unlocked(agent_id, meta)
        return meta


def _team_dir(team_id: str) -> Path:
    if not team_id or "/" in team_id or ".." in team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid team_id.")
    return TEAMS_DIR / team_id


def _team_meta_path(team_id: str) -> Path:
    return _team_dir(team_id) / "meta.json"


def _team_thread_lock(team_id: str) -> threading.RLock:
    with _TEAM_LOCKS_GUARD:
        return _TEAM_LOCKS.setdefault(team_id, threading.RLock())


@contextmanager
def _locked_team(team_id: str, *, create_parent: bool = False) -> Iterator[None]:
    """Serialize team metadata across server threads and delegated worker processes."""
    path = _team_meta_path(team_id)
    thread_lock = _team_thread_lock(team_id)
    with thread_lock:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.exists():
            yield
            return
        lock_path = path.parent / ".team.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_team_unlocked(team_id: str) -> Dict[str, Any]:
    path = _team_meta_path(team_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent team not found: {team_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt team metadata: {team_id}") from exc


def _write_team_unlocked(team_id: str, meta: Dict[str, Any]) -> None:
    path = _team_meta_path(team_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".team.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_team(team_id: str) -> Dict[str, Any]:
    with _locked_team(team_id):
        return _read_team_unlocked(team_id)


def _write_team(team_id: str, meta: Dict[str, Any]) -> None:
    with _locked_team(team_id, create_parent=True):
        _write_team_unlocked(team_id, meta)


def _update_team(team_id: str, update: Callable[[Dict[str, Any]], Optional[bool]]) -> Dict[str, Any]:
    with _locked_team(team_id):
        meta = _read_team_unlocked(team_id)
        if update(meta) is not False:
            _write_team_unlocked(team_id, meta)
        return meta


def _lineage_fields_for_agent(
    agent_id: str,
    meta: Optional[Dict[str, Any]] = None,
    *,
    seen: Optional[set[str]] = None,
) -> Dict[str, Any]:
    current = dict(meta or _read_meta(agent_id))
    stored_root = str(current.get("lineage_root_agent_id") or "").strip()
    stored_parent = str(current.get("lineage_parent_agent_id") or "").strip() or None
    stored_ancestors = current.get("lineage_ancestors")
    if (
        int(current.get("lineage_version") or 0) >= _LINEAGE_VERSION
        and stored_root
        and isinstance(stored_ancestors, list)
        and all(isinstance(item, str) and item for item in stored_ancestors)
    ):
        return {
            "lineage_version": _LINEAGE_VERSION,
            "lineage_root_agent_id": stored_root,
            "lineage_parent_agent_id": stored_parent,
            "lineage_ancestors": list(stored_ancestors),
        }

    parent_id = str(current.get("parent_agent_id") or "").strip() or None
    visited = set(seen or ())
    if agent_id in visited:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt agent lineage cycle: {agent_id}")
    visited.add(agent_id)
    if not parent_id:
        return {
            "lineage_version": _LINEAGE_VERSION,
            "lineage_root_agent_id": agent_id,
            "lineage_parent_agent_id": None,
            "lineage_ancestors": [],
        }
    if parent_id in visited:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt agent lineage cycle: {agent_id} -> {parent_id}")
    try:
        parent_meta = _read_meta(parent_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            # Legacy orphan: fail closed for delegated control by treating this record
            # as a new root. New records always persist lineage before a parent can despawn.
            return {
                "lineage_version": _LINEAGE_VERSION,
                "lineage_root_agent_id": agent_id,
                "lineage_parent_agent_id": parent_id,
                "lineage_ancestors": [],
            }
        raise
    parent_lineage = _lineage_fields_for_agent(parent_id, parent_meta, seen=visited)
    return {
        "lineage_version": _LINEAGE_VERSION,
        "lineage_root_agent_id": parent_lineage["lineage_root_agent_id"],
        "lineage_parent_agent_id": parent_id,
        "lineage_ancestors": [*parent_lineage["lineage_ancestors"], parent_id],
    }


def _persist_agent_lineage_if_missing(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if int(meta.get("lineage_version") or 0) >= _LINEAGE_VERSION and meta.get("lineage_root_agent_id"):
        return meta
    derived = _lineage_fields_for_agent(agent_id, meta)
    def update(current: Dict[str, Any]) -> None:
        if int(current.get("lineage_version") or 0) < _LINEAGE_VERSION or not current.get("lineage_root_agent_id"):
            current.update(derived)
            current["updated_at"] = current.get("updated_at") or _now()
    return _update_meta(agent_id, update)


def _team_lineage_fields(team_id: str, team: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    current = dict(team or _read_team(team_id))
    owner = str(current.get("owner_agent_id") or "").strip() or None
    root = str(current.get("lineage_root_agent_id") or "").strip() or None
    ancestors = current.get("lineage_ancestors")
    if int(current.get("lineage_version") or 0) >= _LINEAGE_VERSION and isinstance(ancestors, list):
        return {
            "lineage_version": _LINEAGE_VERSION,
            "owner_agent_id": owner,
            "lineage_root_agent_id": root,
            "lineage_ancestors": list(ancestors),
        }
    if owner:
        try:
            # Derive first without mutating the owner's metadata. Authorization
            # must not let an unrelated caller trigger a lazy migration write.
            owner_meta = _read_meta(owner)
            owner_lineage = _lineage_fields_for_agent(owner, owner_meta)
            return {
                "lineage_version": _LINEAGE_VERSION,
                "owner_agent_id": owner,
                "lineage_root_agent_id": owner_lineage["lineage_root_agent_id"],
                "lineage_ancestors": list(owner_lineage["lineage_ancestors"]),
            }
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
    # Legacy teams had no stable owner. Keep them root-admin only instead of
    # guessing ownership from arbitrary children.
    return {
        "lineage_version": _LINEAGE_VERSION,
        "owner_agent_id": None,
        "lineage_root_agent_id": None,
        "lineage_ancestors": [],
    }


def _persist_team_lineage_if_missing(team_id: str, team: Dict[str, Any]) -> Dict[str, Any]:
    if int(team.get("lineage_version") or 0) >= _LINEAGE_VERSION:
        return team
    derived = _team_lineage_fields(team_id, team)
    def update(current: Dict[str, Any]) -> None:
        if int(current.get("lineage_version") or 0) < _LINEAGE_VERSION:
            current.update(derived)
            current["updated_at"] = current.get("updated_at") or _now()
    return _update_team(team_id, update)


def _lineage_denied(
    *, target_type: str, target_id: str, operation: str, context: Optional[PolicyContext] = None,
) -> HTTPException:
    context = context or current_policy_context()
    return HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={
            "error": _CONTROL_DENY_ERROR,
            "reason": "lineage_not_authorized",
            "actor_agent_id": context.agent_id,
            "target_type": target_type,
            "target_id": target_id,
            "operation": operation,
        },
    )


def _authorize_agent_control(
    agent_id: str, operation: str, *, meta: Optional[Dict[str, Any]] = None,
    context: Optional[PolicyContext] = None,
) -> Dict[str, Any]:
    context = context or current_policy_context()
    raw_target = dict(meta or _read_meta(agent_id))
    lineage = _lineage_fields_for_agent(agent_id, raw_target)
    if context.agent_id is None:
        return _persist_agent_lineage_if_missing(agent_id, raw_target)
    actor = str(context.agent_id)
    if actor == agent_id or actor in set(lineage["lineage_ancestors"]):
        return _persist_agent_lineage_if_missing(agent_id, raw_target)
    raise _lineage_denied(
        target_type="agent", target_id=agent_id, operation=operation, context=context
    )


def _authorize_team_control(
    team_id: str, operation: str, *, team: Optional[Dict[str, Any]] = None,
    context: Optional[PolicyContext] = None,
) -> Dict[str, Any]:
    context = context or current_policy_context()
    raw_target = dict(team or _read_team(team_id))
    lineage = _team_lineage_fields(team_id, raw_target)
    if context.agent_id is None:
        return _persist_team_lineage_if_missing(team_id, raw_target)
    actor = str(context.agent_id)
    owner = lineage.get("owner_agent_id")
    if owner and (actor == owner or actor in set(lineage.get("lineage_ancestors") or [])):
        return _persist_team_lineage_if_missing(team_id, raw_target)
    raise _lineage_denied(
        target_type="team", target_id=team_id, operation=operation, context=context
    )


def authorize_agent_control_request(
    tool: str, arguments: Optional[Dict[str, Any]] = None, *, context: Optional[PolicyContext] = None,
) -> None:
    """Preflight delegated agent-control requests before side-effect intents are opened."""
    context = context or current_policy_context()
    if context.agent_id is None:
        return
    args = dict(arguments or {})
    name = str(tool or "").strip()
    if name == "get_agent":
        target = str(args.get("agent_id") or "").strip()
        if target:
            _authorize_agent_control(target, "get_agent", context=context)
        return
    if name == "list_agents":
        team_id = str(args.get("team_id") or "").strip()
        if team_id:
            _authorize_team_control(team_id, "list_agents", context=context)
        return
    if name == "wait_agents":
        team_id = str(args.get("team_id") or "").strip()
        if team_id:
            _authorize_team_control(team_id, "wait_agents", context=context)
            return
        for target in args.get("agent_ids") or []:
            _authorize_agent_control(str(target), "wait_agents", context=context)
        return
    if name == "agent_action":
        operation = f"agent_action:{str(args.get('action') or '').strip().lower()}"
        agent_id = str(args.get("agent_id") or "").strip()
        team_id = str(args.get("team_id") or "").strip()
        if agent_id:
            _authorize_agent_control(agent_id, operation, context=context)
        elif team_id:
            _authorize_team_control(team_id, operation, context=context)


def _new_agent_lineage(agent_id: str, parent_agent_id: Optional[str]) -> Dict[str, Any]:
    if not parent_agent_id:
        return {
            "lineage_version": _LINEAGE_VERSION,
            "lineage_root_agent_id": agent_id,
            "lineage_parent_agent_id": None,
            "lineage_ancestors": [],
        }
    parent_id = str(parent_agent_id)
    parent_meta = _persist_agent_lineage_if_missing(parent_id, _read_meta(parent_id))
    parent_lineage = _lineage_fields_for_agent(parent_id, parent_meta)
    return {
        "lineage_version": _LINEAGE_VERSION,
        "lineage_root_agent_id": parent_lineage["lineage_root_agent_id"],
        "lineage_parent_agent_id": parent_id,
        "lineage_ancestors": [*parent_lineage["lineage_ancestors"], parent_id],
    }


def _team_lineage_for_spawn(parent_team_id: Optional[str]) -> Dict[str, Any]:
    # Retry/replacement teams inherit the original owner's lineage even when an
    # authorized ancestor triggers the action. Ownership must not drift upward.
    if parent_team_id:
        parent = _persist_team_lineage_if_missing(str(parent_team_id), _read_team(str(parent_team_id)))
        lineage = _team_lineage_fields(str(parent_team_id), parent)
        return {
            "lineage_version": _LINEAGE_VERSION,
            "owner_agent_id": lineage.get("owner_agent_id"),
            "lineage_root_agent_id": lineage.get("lineage_root_agent_id"),
            "lineage_ancestors": list(lineage.get("lineage_ancestors") or []),
        }
    context = current_policy_context()
    if context.agent_id is not None:
        actor = str(context.agent_id)
        actor_meta = _persist_agent_lineage_if_missing(actor, _read_meta(actor))
        lineage = _lineage_fields_for_agent(actor, actor_meta)
        return {
            "lineage_version": _LINEAGE_VERSION,
            "owner_agent_id": actor,
            "lineage_root_agent_id": lineage["lineage_root_agent_id"],
            "lineage_ancestors": list(lineage["lineage_ancestors"]),
        }
    return {
        "lineage_version": _LINEAGE_VERSION,
        "owner_agent_id": None,
        "lineage_root_agent_id": None,
        "lineage_ancestors": [],
    }


def _usage_total_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("total", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return max(0, int(value))
    total = 0
    for key in ("input", "input_tokens", "output", "output_tokens", "reasoning", "reasoning_output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += max(0, int(value))
    return total


def _team_usage_snapshot(team: Dict[str, Any]) -> Dict[str, int]:
    tool_calls = 0
    total_tokens = 0
    completed_agents = 0
    for agent_id in list(team.get("agent_ids") or []):
        try:
            meta = _read_meta(str(agent_id))
        except HTTPException:
            continue
        tool_calls += max(0, int(meta.get("tool_call_count") or 0))
        total_tokens += _usage_total_tokens(meta.get("usage"))
        if str(meta.get("status") or "") in TERMINAL_STATUSES:
            completed_agents += 1
    return {
        "tool_calls": tool_calls,
        "total_tokens": total_tokens,
        "completed_agents": completed_agents,
    }


def _team_budget_snapshot(team: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    current = float(_now() if now is None else now)
    created = float(team.get("created_at") or current)
    timeout_s = int(team.get("team_timeout_s") or 0) or None
    deadline_at = float(team.get("deadline_at") or (created + timeout_s if timeout_s else 0.0)) or None
    elapsed_s = max(0.0, current - created)
    remaining_s = max(0.0, deadline_at - current) if deadline_at else None
    usage = _team_usage_snapshot(team)

    retry_limit = team.get("max_team_retries")
    retry_limit = int(retry_limit) if retry_limit is not None else None
    retries_used = max(0, int(team.get("team_retry_count") or 0))
    retries_remaining = max(0, retry_limit - retries_used) if retry_limit is not None else None

    tool_limit = team.get("max_total_tool_calls")
    tool_limit = int(tool_limit) if tool_limit is not None else None
    tools_remaining = max(0, tool_limit - usage["tool_calls"]) if tool_limit is not None else None

    token_limit = team.get("max_total_tokens")
    token_limit = int(token_limit) if token_limit is not None else None
    tokens_remaining = max(0, token_limit - usage["total_tokens"]) if token_limit is not None else None

    admission_reason: Optional[str] = None
    if deadline_at is not None and current >= deadline_at:
        admission_reason = "team_deadline"
    elif tool_limit is not None and usage["tool_calls"] >= tool_limit:
        admission_reason = "tool_call_budget"
    elif token_limit is not None and usage["total_tokens"] >= token_limit:
        admission_reason = "token_budget"

    retry_limit_reached = retry_limit is not None and retries_used >= retry_limit
    retry_blocked = retry_limit_reached and str(team.get("last_retry_block_reason") or "") == "retry_budget"
    exhausted_reason = admission_reason or ("retry_budget" if retry_blocked else None)
    active_tasks = sum(1 for task in list(team.get("tasks") or []) if task.get("state") in {"spawning", "running"})
    max_parallel = min(max(1, int(team.get("max_parallel") or 1)), MAX_TEAM_SIZE)
    return {
        "team_timeout_s": timeout_s,
        "deadline_at": deadline_at,
        "elapsed_s": round(elapsed_s, 3),
        "remaining_s": round(remaining_s, 3) if remaining_s is not None else None,
        "max_parallel": max_parallel,
        "active": active_tasks,
        "concurrency_remaining": max(0, max_parallel - active_tasks),
        "max_team_retries": retry_limit,
        "retries_used": retries_used,
        "retries_remaining": retries_remaining,
        "max_total_tool_calls": tool_limit,
        "tool_calls_used": usage["tool_calls"],
        "tool_calls_remaining": tools_remaining,
        "max_total_tokens": token_limit,
        "total_tokens_used": usage["total_tokens"],
        "total_tokens_remaining": tokens_remaining,
        "admission_open": admission_reason is None,
        "retry_open": not retry_limit_reached and admission_reason is None,
        "exhausted": exhausted_reason is not None,
        "exhausted_reason": exhausted_reason,
        "admission_exhausted_reason": admission_reason,
    }


def _reserve_team_retry(team_id: Optional[str], agent_id: str, reason: str, requested_backoff_s: float = 0.0) -> Dict[str, Any]:
    if not team_id:
        return {"allowed": True, "delay_s": max(0.0, float(requested_backoff_s)), "remaining": None, "reason": reason}
    with _locked_team(str(team_id)):
        team = _read_team_unlocked(str(team_id))
        budget = _team_budget_snapshot(team)
        if budget.get("admission_exhausted_reason"):
            team["budget_exhausted_reason"] = budget["admission_exhausted_reason"]
            team["updated_at"] = _now()
            _write_team_unlocked(str(team_id), team)
            return {
                "allowed": False, "delay_s": 0.0, "remaining": budget.get("retries_remaining"),
                "reason": str(budget["admission_exhausted_reason"]),
            }
        limit = budget.get("max_team_retries")
        used = int(budget.get("retries_used") or 0)
        if limit is not None and used >= int(limit):
            team["retry_budget_exhausted_at"] = _now()
            team["last_retry_block_reason"] = "retry_budget"
            team["updated_at"] = _now()
            _write_team_unlocked(str(team_id), team)
            return {"allowed": False, "delay_s": 0.0, "remaining": 0, "reason": "retry_budget"}

        now = _now()
        backoff = min(MAX_GENERIC_RETRY_BACKOFF_S, max(0.0, float(requested_backoff_s)))
        earliest = max(now + backoff, float(team.get("next_retry_at") or 0.0))
        deadline = budget.get("deadline_at")
        if deadline is not None and earliest >= float(deadline):
            team["budget_exhausted_reason"] = "team_deadline"
            team["last_retry_block_reason"] = "team_deadline"
            team["updated_at"] = now
            _write_team_unlocked(str(team_id), team)
            return {
                "allowed": False, "delay_s": 0.0, "remaining": max(0, int(limit) - used) if limit is not None else None,
                "reason": "team_deadline",
            }
        team["team_retry_count"] = used + 1
        team["next_retry_at"] = earliest + TEAM_RETRY_START_SPACING_S
        team["last_retry_reason"] = str(reason or "provider_error")[:120]
        team["last_retry_agent_id"] = agent_id
        team["last_retry_reserved_at"] = now
        events = list(team.get("retry_events") or [])[-19:]
        events.append({"at": now, "agent_id": agent_id, "reason": str(reason or "provider_error")[:120], "scheduled_at": earliest})
        team["retry_events"] = events
        team["updated_at"] = now
        _write_team_unlocked(str(team_id), team)
        remaining = max(0, int(limit) - (used + 1)) if limit is not None else None
        return {"allowed": True, "delay_s": max(0.0, earliest - now), "remaining": remaining, "reason": reason}


def _validate_team_graph(tasks: List[Dict[str, Any]]) -> None:
    ids = [str(task.get("id") or "") for task in tasks]
    if len(ids) != len(set(ids)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team task ids must be unique.")
    known = set(ids)
    reviewer_targets: Dict[str, str] = {}
    for task in tasks:
        task_id = str(task["id"])
        for dep in task.get("depends_on") or []:
            if dep not in known:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Task {task_id} depends on unknown task: {dep}")
            if dep == task_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Task {task_id} cannot depend on itself.")
        review_of = str(task.get("review_of") or "").strip() or None
        if review_of:
            if review_of not in known:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Task {task_id} reviews unknown task: {review_of}")
            if review_of == task_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Task {task_id} cannot review itself.")
            if review_of in reviewer_targets:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Task {review_of} already has quality gate {reviewer_targets[review_of]}; only one reviewer gate per task is supported.",
                )
            reviewer_targets[review_of] = task_id

    indegree = {task_id: 0 for task_id in ids}
    outgoing: Dict[str, List[str]] = {task_id: [] for task_id in ids}
    for task in tasks:
        task_id = str(task["id"])
        deps = list(task.get("depends_on") or [])
        review_of = str(task.get("review_of") or "").strip() or None
        if review_of and review_of not in deps:
            deps.append(review_of)
            task["depends_on"] = deps
        for dep in deps:
            indegree[task_id] += 1
            outgoing[dep].append(task_id)
    queue = [task_id for task_id in ids if indegree[task_id] == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for child in outgoing[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team dependency graph contains a cycle.")


def _team_task_map(team: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(task.get("id")): task for task in list(team.get("tasks") or [])}


def _team_reviewer_map(team: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for task in list(team.get("tasks") or []):
        review_of = str(task.get("review_of") or "").strip()
        if review_of:
            result[review_of] = task
    return result


def _chatgpt_admission_state() -> Dict[str, Any]:
    state_path = _chatgpt_provider_state_path()
    if not state_path.exists():
        return {}
    lock_path = AGENTS_DIR / ".chatgpt-provider-state.lock"
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _chatgpt_admission_cooldown_until(state: Optional[Dict[str, Any]] = None) -> Optional[float]:
    payload = state if isinstance(state, dict) else _chatgpt_admission_state()
    until = float(payload.get("cooldown_until") or 0.0)
    return until if until > _now() else None


def _chatgpt_admission_limit_override(state: Optional[Dict[str, Any]] = None) -> Optional[int]:
    payload = state if isinstance(state, dict) else _chatgpt_admission_state()
    reduced_until = float(payload.get("reduced_until") or 0.0)
    return 1 if reduced_until > _now() else None


def _git_scope_isolated_for_admission(team: Dict[str, Any], scope: ResourceScope) -> bool:
    if str(team.get("access_mode") or "") != "workspace_write":
        return False
    if str(team.get("git_isolation") or "auto") == "off" or not team.get("git_base_commit"):
        return False
    roots = scope.path_roots
    if not roots:
        return False
    cwd = Path(str(team.get("cwd") or "")).expanduser().resolve(strict=False)
    resolved = [Path(raw).expanduser().resolve(strict=False) for raw in roots]
    # Conservative approximation of #50 applicability: all roots stay under the team
    # cwd and at least one authorized root contains cwd (normally the repo root itself).
    try:
        all_under = all(root == cwd or root.is_relative_to(cwd) for root in resolved)
    except AttributeError:  # pragma: no cover - Python <3.9 compatibility guard
        all_under = all(str(root).startswith(str(cwd) + os.sep) or root == cwd for root in resolved)
    contains_cwd = False
    for root in resolved:
        try:
            cwd.relative_to(root)
            contains_cwd = True
            break
        except ValueError:
            continue
    return all_under and contains_cwd


def _task_admission_claims(team: Dict[str, Any], task: Dict[str, Any]) -> List[Dict[str, str]]:
    explicit = list(task.get("resource_claims") or [])
    scope = ResourceScope.from_dict(task.get("scope"))
    access_mode = str(team.get("access_mode") or scope.access_mode.value)
    claims: List[Dict[str, Any]] = [dict(item) for item in explicit]
    if scope.path_roots:
        if access_mode == "read_only":
            claims.extend({"kind": "workspace", "id": root, "mode": "read"} for root in scope.path_roots)
        elif access_mode == "workspace_write" and not _git_scope_isolated_for_admission(team, scope):
            claims.extend({"kind": "workspace", "id": root, "mode": "write"} for root in scope.path_roots)
    # Existing browser logical leases are exclusive per tab; admission mirrors that
    # policy before a provider process starts but does not replace action-time leases.
    if scope.browser_tabs:
        claims.extend({"kind": "browser_tab", "id": tab, "mode": "write"} for tab in scope.browser_tabs)
    try:
        return normalize_admission_claims(claims)
    except AdmissionError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"error": exc.code, "message": str(exc), **dict(exc.details or {})},
        ) from exc


def _normalize_explicit_resource_claims(
    raw_resources: Any, *, workdir: Path, scope: ResourceScope,
) -> List[Dict[str, str]]:
    if raw_resources in (None, []):
        return []
    if not isinstance(raw_resources, list) or any(not isinstance(item, dict) for item in raw_resources):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "task.resources must be a list of resource claim objects.")
    prepared: List[Dict[str, Any]] = []
    path_kinds = {"workspace", "path", "file"}
    for raw in raw_resources:
        item = dict(raw)
        kind = str(item.get("kind") or "").strip().lower()
        identifier = str(item.get("id") or item.get("resource") or "").strip()
        if kind in path_kinds and identifier:
            candidate = Path(identifier).expanduser()
            if not candidate.is_absolute():
                candidate = workdir / candidate
            candidate = candidate.resolve(strict=False)
            roots = scope.path_roots
            if roots is not None:
                allowed = False
                for raw_root in roots:
                    root = Path(raw_root).expanduser().resolve(strict=False)
                    try:
                        candidate.relative_to(root)
                        allowed = True
                        break
                    except ValueError:
                        continue
                if not allowed:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        {"error": "resource_claim_scope_denied", "kind": kind, "id": str(candidate)},
                    )
            item["id"] = str(candidate)
        elif kind == "browser_tab" and identifier and scope.browser_tabs is not None:
            if identifier not in scope.browser_tabs:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    {"error": "resource_claim_scope_denied", "kind": kind, "id": identifier},
                )
        prepared.append(item)
    try:
        return normalize_admission_claims(prepared)
    except AdmissionError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"error": exc.code, "message": str(exc), **dict(exc.details or {})},
        ) from exc


def _task_admission_request_id(team_id: str, task: Dict[str, Any]) -> str:
    existing = str(task.get("admission_request_id") or "").strip()
    if existing:
        return existing
    generation = len(task.get("agent_ids") or [])
    return f"admit:{team_id}:{task.get('id')}:{generation}"


def _release_agent_admission(agent_id: str, meta: Optional[Dict[str, Any]] = None) -> None:
    try:
        current = dict(meta or _read_meta(agent_id))
    except HTTPException:
        current = {}
    lease_id = str(current.get("admission_lease_id") or "").strip() or None
    if not lease_id:
        return
    try:
        admission_release(AGENTS_DIR, lease_id=lease_id)
    except Exception:
        return
    try:
        def clear(current_meta: Dict[str, Any]) -> Optional[bool]:
            if str(current_meta.get("admission_lease_id") or "") != lease_id:
                return False
            current_meta["admission_lease_id"] = None
            current_meta["admission_released_at"] = _now()
            current_meta["updated_at"] = _now()
            return True
        _update_meta(agent_id, clear)
    except HTTPException:
        pass


def _release_task_admission(task: Dict[str, Any]) -> None:
    lease_id = str(task.get("admission_lease_id") or "").strip() or None
    request_id = str(task.get("admission_request_id") or "").strip() or None
    try:
        if lease_id:
            admission_release(AGENTS_DIR, lease_id=lease_id)
        elif request_id:
            admission_release(AGENTS_DIR, request_id=request_id)
    except Exception:
        pass
    task["admission_lease_id"] = None
    task["admission_request_id"] = None
    task["queued_since"] = None
    task["queued_reason"] = None
    task["queued_details"] = None
    task["queue_position"] = None


def _team_task_effectively_completed(
    task_id: str,
    task_map: Dict[str, Dict[str, Any]],
    reviewer_map: Dict[str, Dict[str, Any]],
) -> bool:
    task = task_map[task_id]
    if task.get("state") != "completed":
        return False
    reviewer = reviewer_map.get(task_id)
    if reviewer is None:
        return True
    return reviewer.get("state") == "completed" and reviewer.get("gate_result") == "pass"


def _team_task_effectively_failed(
    task_id: str,
    task_map: Dict[str, Dict[str, Any]],
    reviewer_map: Dict[str, Dict[str, Any]],
) -> bool:
    task = task_map[task_id]
    if task.get("state") in {"failed", "quality_failed", "skipped", "cancelled"}:
        return True
    reviewer = reviewer_map.get(task_id)
    return bool(reviewer and reviewer.get("state") in {"failed", "quality_failed", "skipped", "cancelled"})


def _team_dependency_satisfied(
    task: Dict[str, Any],
    dep_id: str,
    task_map: Dict[str, Dict[str, Any]],
    reviewer_map: Dict[str, Dict[str, Any]],
) -> bool:
    # A reviewer must be allowed to inspect the raw completed target; downstream tasks
    # wait for the reviewer's PASS through _team_task_effectively_completed().
    if str(task.get("review_of") or "") == dep_id:
        return task_map[dep_id].get("state") == "completed"
    return _team_task_effectively_completed(dep_id, task_map, reviewer_map)


def _team_agent_result(agent_id: Optional[str], limit: int = TEAM_RESULT_LIMIT * 2) -> str:
    if not agent_id:
        return ""
    path = _agent_dir(str(agent_id)) / "result.txt"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return truncate(text, limit)[0]


def _quality_gate_result(text: str) -> Tuple[Optional[str], str]:
    matches = list(_QUALITY_GATE_RE.finditer(text or ""))
    if len(matches) != 1:
        return None, truncate((text or "").strip(), TEAM_RESULT_LIMIT)[0]
    decision = matches[0].group(1).lower()
    feedback = ((text or "")[:matches[0].start()] + (text or "")[matches[0].end():]).strip()
    return decision, truncate(feedback, TEAM_RESULT_LIMIT)[0]


def _team_task_prompt(team: Dict[str, Any], task: Dict[str, Any], task_map: Dict[str, Dict[str, Any]]) -> str:
    pieces = [str(task.get("prompt") or "").strip()]
    dependency_sections: List[str] = []
    for dep_id in task.get("depends_on") or []:
        dep = task_map.get(str(dep_id))
        if not dep:
            continue
        result = _team_agent_result(dep.get("latest_agent_id"))
        if result:
            dependency_sections.append(f"### {dep_id}: {dep.get('title') or dep_id}\n{result}")
    if dependency_sections:
        pieces.append("Dependency results:\n" + "\n\n".join(dependency_sections))

    review_of = str(task.get("review_of") or "").strip() or None
    if review_of:
        target = task_map[review_of]
        target_result = _team_agent_result(target.get("latest_agent_id"))
        pieces.append(
            "Quality gate contract:\n"
            f"Review the latest result of task '{review_of}' below. Judge whether it satisfies the requested task. "
            "Your final response MUST contain exactly one standalone marker line: QUALITY_GATE: PASS or QUALITY_GATE: FAIL. "
            "If FAIL, give concrete revision feedback before the marker. Do not emit both markers.\n\n"
            f"Latest candidate:\n{target_result or '(no candidate result found)'}"
        )

    revision_count = int(task.get("revision_count") or 0)
    revision_feedback = str(task.get("revision_feedback") or "").strip()
    if revision_count > 0 and revision_feedback:
        previous = _team_agent_result(task.get("latest_agent_id"))
        pieces.append(
            f"Quality-gate revision {revision_count}:\n"
            f"Address this reviewer feedback before returning the revised result:\n{revision_feedback}"
            + (f"\n\nPrevious candidate:\n{previous}" if previous else "")
        )
    return "\n\n".join(piece for piece in pieces if piece)


def _team_tick(team_id: str) -> Dict[str, Any]:
    """Advance one persisted DAG team. Safe to call concurrently from worker processes."""
    with _locked_team(team_id):
        team = _read_team_unlocked(team_id)
        if int(team.get("scheduler_version") or 0) < 1:
            return team
        if team.get("cancelled"):
            return team
        tasks = list(team.get("tasks") or [])
        task_map = _team_task_map(team)
        reviewer_map = _team_reviewer_map(team)
        changed = False

        # Reconcile agent terminal states back into graph nodes.
        for task in tasks:
            if task.get("state") not in {"spawning", "running"}:
                continue
            agent_id = str(task.get("active_agent_id") or "").strip()
            if not agent_id:
                _release_task_admission(task)
                task["state"] = "failed"
                task["failure_reason"] = "missing_active_agent"
                changed = True
                continue
            try:
                agent_meta = _normalize(agent_id, _read_meta(agent_id))
            except HTTPException:
                _release_task_admission(task)
                task["state"] = "failed"
                task["failure_reason"] = "missing_agent"
                task["active_agent_id"] = None
                changed = True
                continue
            agent_status = str(agent_meta.get("status") or "")
            if agent_status not in TERMINAL_STATUSES:
                continue
            _release_agent_admission(agent_id, agent_meta)
            _release_task_admission(task)
            task["active_agent_id"] = None
            changed = True
            if agent_status != "completed":
                task["state"] = "failed"
                task["failure_reason"] = f"agent_{agent_status}"
                continue

            review_of = str(task.get("review_of") or "").strip() or None
            if not review_of:
                task["state"] = "completed"
                task["failure_reason"] = None
                continue

            decision, feedback = _quality_gate_result(_team_agent_result(agent_id))
            task["gate_attempts"] = int(task.get("gate_attempts") or 0) + 1
            task["gate_result"] = decision or "invalid"
            task["gate_feedback"] = feedback
            if decision == "pass":
                task["state"] = "completed"
                task["failure_reason"] = None
                continue
            if decision != "fail":
                task["state"] = "quality_failed"
                task["failure_reason"] = "invalid_quality_gate_contract"
                continue

            target = task_map[review_of]
            revisions = int(target.get("revision_count") or 0)
            limit = int(task["max_revisions"] if task.get("max_revisions") is not None else (team.get("max_revisions") or 0))
            if revisions >= limit:
                task["state"] = "quality_failed"
                task["failure_reason"] = "revision_limit_exhausted"
                continue
            target["revision_count"] = revisions + 1
            target["revision_feedback"] = feedback or "Reviewer returned FAIL without written feedback. Re-check the task carefully."
            target["state"] = "blocked"
            target["failure_reason"] = None
            task["state"] = "blocked"
            task["failure_reason"] = None

        task_map = _team_task_map(team)
        reviewer_map = _team_reviewer_map(team)

        # Resolve dependency readiness/failure. Repeat because skipping one task can
        # make another task impossible in the same tick.
        progressed = True
        while progressed:
            progressed = False
            for task in tasks:
                if task.get("state") not in {"blocked", "ready"}:
                    continue
                deps = [str(dep) for dep in (task.get("depends_on") or [])]
                failed_dep = next(
                    (dep for dep in deps if _team_task_effectively_failed(dep, task_map, reviewer_map)),
                    None,
                )
                if failed_dep:
                    task["state"] = "skipped"
                    task["failure_reason"] = f"dependency_failed:{failed_dep}"
                    changed = progressed = True
                    continue
                if all(_team_dependency_satisfied(task, dep, task_map, reviewer_map) for dep in deps):
                    if task.get("state") != "ready":
                        task["state"] = "ready"
                        changed = progressed = True

        budget = _team_budget_snapshot(team)
        admission_reason = str(budget.get("admission_exhausted_reason") or "").strip() or None
        if admission_reason:
            if not team.get("budget_exhausted_reason"):
                team["budget_exhausted_reason"] = admission_reason
                team["budget_exhausted_at"] = _now()
                changed = True
            for task in tasks:
                if task.get("state") not in {"blocked", "ready", "queued"}:
                    continue
                if task.get("state") == "queued":
                    _release_task_admission(task)
                task["state"] = "skipped"
                task["failure_reason"] = f"budget_exhausted:{admission_reason}"
                changed = True

        active = sum(1 for task in tasks if task.get("state") in {"spawning", "running"})
        max_parallel = min(max(1, int(team.get("max_parallel") or len(tasks) or 1)), MAX_TEAM_SIZE)
        for task in tasks:
            if active >= max_parallel:
                break
            if task.get("state") not in {"ready", "queued"}:
                continue
            request_id = _task_admission_request_id(team_id, task)
            task["admission_request_id"] = request_id
            try:
                resource_claims = _task_admission_claims(team, task)
                provider_name = str(team["provider"])
                chatgpt_state = (
                    _chatgpt_admission_state() if provider_name.lower() == "chatgpt" else {}
                )
                admission = request_admission(
                    AGENTS_DIR,
                    request_id=request_id,
                    team_id=team_id,
                    task_id=str(task["id"]),
                    provider=provider_name,
                    resources=resource_claims,
                    provider_blocked_until=(
                        _chatgpt_admission_cooldown_until(chatgpt_state) if chatgpt_state else None
                    ),
                    provider_limit_override=(
                        _chatgpt_admission_limit_override(chatgpt_state) if chatgpt_state else None
                    ),
                )
            except AdmissionError as exc:
                _release_task_admission(task)
                task["state"] = "failed"
                task["failure_reason"] = f"admission_failed:{exc.code}"
                task["queued_details"] = {"error": exc.code, "message": str(exc), **dict(exc.details or {})}
                changed = True
                continue
            if not admission.get("admitted"):
                task["state"] = "queued"
                task["queued_since"] = admission.get("queued_since") or task.get("queued_since") or _now()
                task["queued_reason"] = admission.get("reason")
                task["queued_details"] = admission.get("details") or {}
                task["queue_position"] = admission.get("queue_position")
                changed = True
                continue
            task["admission_lease_id"] = admission.get("lease_id")
            task["queued_reason"] = None
            task["queued_details"] = None
            task["queue_position"] = None
            task["state"] = "spawning"
            team["updated_at"] = _now()
            _write_team_unlocked(team_id, team)
            prompt = _team_task_prompt(team, task, task_map)
            parent_agent_id = (
                str(task.get("latest_agent_id") or "").strip()
                or str(team.get("owner_agent_id") or "").strip()
                or None
            )
            seed_agent_ids: List[str] = []
            if not str(task.get("latest_agent_id") or "").strip():
                seed_task_ids = list(task.get("depends_on") or [])
                review_of = str(task.get("review_of") or "").strip()
                if review_of and review_of not in seed_task_ids:
                    seed_task_ids.append(review_of)
                for seed_task_id in seed_task_ids:
                    seed_task = task_map.get(str(seed_task_id))
                    seed_agent = str((seed_task or {}).get("latest_agent_id") or "").strip()
                    if seed_agent and seed_agent not in seed_agent_ids:
                        seed_agent_ids.append(seed_agent)
            try:
                item = _spawn_internal(
                    settings=None,
                    provider=str(team["provider"]),
                    prompt=prompt,
                    model=team.get("model"),
                    reasoning=team.get("reasoning"),
                    cwd=team.get("cwd"),
                    timeout_s=team.get("timeout_s"),
                    title=task.get("title"),
                    result_style=str(team.get("result_style") or "concise"),
                    access_mode=str(team.get("access_mode") or "read_only"),
                    scope=ResourceScope.from_dict(task.get("scope")),
                    permission_profile=str(team.get("permission_profile") or "trusted"),
                    capability_profile=str(team.get("capability_profile") or "legacy"),
                    parent_agent_id=parent_agent_id,
                    team_id=team_id,
                    team_task_id=str(task["id"]),
                    idle_timeout_s=team.get("idle_timeout_s"),
                    retries=int(team.get("retries") or 0),
                    project=task.get("project"),
                    role=task.get("role"),
                    provenance_class=str(team.get("provenance_class") or "local"),
                    git_isolation=str(team.get("git_isolation") or "auto"),
                    git_base_commit=team.get("git_base_commit"),
                    reuse_worktree_agent_id=(
                        str(task.get("latest_agent_id") or "").strip() or None
                    ),
                    seed_worktree_agent_ids=seed_agent_ids,
                    admission_lease_id=str(task.get("admission_lease_id") or "") or None,
                    admission_resources=resource_claims,
                )
            except Exception as exc:
                _release_task_admission(task)
                task["state"] = "failed"
                task["failure_reason"] = f"spawn_failed:{str(exc)[:240]}"
                task["active_agent_id"] = None
                changed = True
                continue
            agent_id = str(item["agent_id"])
            task.setdefault("agent_ids", []).append(agent_id)
            task["active_agent_id"] = agent_id
            task["latest_agent_id"] = agent_id
            task["state"] = "running"
            if agent_id not in team.setdefault("agent_ids", []):
                team["agent_ids"].append(agent_id)
            active += 1
            changed = True

        if changed:
            team["updated_at"] = _now()
            _write_team_unlocked(team_id, team)
        return team


def _wake_global_admission_queue(*, exclude_team_id: Optional[str] = None, limit: int = 32) -> None:
    """Best-effort bounded wakeup of queued teams after global capacity is released."""
    try:
        snap = admission_snapshot(AGENTS_DIR)
    except Exception:
        return
    team_ids: List[str] = []
    for request in list(snap.get("queued") or []):
        team_id = str(request.get("team_id") or "").strip()
        if not team_id or team_id == str(exclude_team_id or "") or team_id in team_ids:
            continue
        team_ids.append(team_id)
        if len(team_ids) >= max(1, int(limit)):
            break
    for queued_team_id in team_ids:
        try:
            if not _team_meta_path(queued_team_id).exists():
                try:
                    admission_cancel_queued(AGENTS_DIR, team_id=queued_team_id)
                except Exception:
                    pass
                continue
            _team_tick(queued_team_id)
        except Exception:
            # One broken/stale team must not prevent unrelated queued teams from waking.
            continue


def _aggregate_work_outcome(
    successful_count: int,
    failure_count: int,
    pending_count: int,
    *,
    cancelled: bool = False,
    cancelled_failure_count: int = 0,
) -> Dict[str, Any]:
    successful = max(0, int(successful_count))
    failed = max(0, int(failure_count))
    pending = max(0, int(pending_count))
    mixed = successful > 0 and failed > 0
    if cancelled:
        outcome = "cancelled"
    elif pending > 0:
        outcome = "running"
    elif failed == 0 and successful > 0:
        outcome = "completed"
    elif mixed:
        outcome = "partial_failure"
    elif failed > 0 and cancelled_failure_count >= failed:
        outcome = "cancelled"
    elif failed > 0:
        outcome = "failed"
    else:
        outcome = "running"
    return {
        "success": outcome == "completed",
        "outcome": outcome,
        "partial_failure": mixed,
        "successful_count": successful,
        "failure_count": failed,
        "pending_count": pending,
        "work_count": successful + failed + pending,
    }


def _team_summary(team_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    team = dict(meta or _read_team(team_id))
    if meta is None and int(team.get("scheduler_version") or 0) >= 1:
        team = dict(_team_tick(team_id))
    agent_ids = list(team.get("agent_ids") or [])
    provider = str(team.get("provider") or "opencode").lower()
    access_mode = str(team.get("access_mode") or "workspace_write")
    access_info = _access_mode_info(provider, access_mode)
    counts: Dict[str, int] = {}
    legacy_failure_reasons: List[Dict[str, Any]] = []
    for agent_id in agent_ids:
        try:
            agent_meta = _normalize(agent_id, _read_meta(agent_id))
        except HTTPException:
            counts["missing"] = counts.get("missing", 0) + 1
            legacy_failure_reasons.append({
                "agent_id": agent_id, "status": "missing", "reason": "missing_agent",
            })
            continue
        public = _public_meta(agent_id, agent_meta)
        state = str(public.get("status") or "unknown")
        counts[state] = counts.get(state, 0) + 1
        if state in TERMINAL_STATUSES and state != "completed":
            legacy_failure_reasons.append({
                "agent_id": agent_id, "status": state, "reason": state,
            })
    terminal_count = sum(counts.get(state, 0) for state in TERMINAL_STATUSES)

    public_tasks: List[Dict[str, Any]] = []
    task_counts: Dict[str, int] = {}
    task_failure_reasons: List[Dict[str, Any]] = []
    tasks = list(team.get("tasks") or [])
    for task in tasks:
        state = str(task.get("state") or "blocked")
        task_counts[state] = task_counts.get(state, 0) + 1
        failure_reason = task.get("failure_reason")
        public_tasks.append({
            "id": task.get("id"),
            "title": task.get("title"),
            "role": task.get("role"),
            "state": state,
            "depends_on": list(task.get("depends_on") or []),
            "review_of": task.get("review_of"),
            "revision_count": int(task.get("revision_count") or 0),
            "max_revisions": int(task["max_revisions"] if task.get("max_revisions") is not None else (team.get("max_revisions") or 0)),
            "gate_attempts": int(task.get("gate_attempts") or 0),
            "gate_result": task.get("gate_result"),
            "failure_reason": failure_reason,
            "active_agent_id": task.get("active_agent_id"),
            "latest_agent_id": task.get("latest_agent_id"),
            "agent_ids": list(task.get("agent_ids") or []),
            "resource_claims": list(task.get("resource_claims") or []),
            "queued_since": task.get("queued_since"),
            "queued_reason": task.get("queued_reason"),
            "queued_details": task.get("queued_details"),
            "queue_position": task.get("queue_position"),
            "admission_lease_id": task.get("admission_lease_id"),
        })
        if state in _GRAPH_TASK_TERMINAL and state != "completed":
            task_failure_reasons.append({
                "task_id": task.get("id"),
                "state": state,
                "reason": str(failure_reason or state),
            })

    scheduler_v1 = int(team.get("scheduler_version") or 0) >= 1
    if scheduler_v1 and tasks:
        task_successful = int(task_counts.get("completed", 0))
        task_failed = sum(int(task_counts.get(state, 0)) for state in _GRAPH_TASK_TERMINAL if state != "completed")
        task_pending = max(0, len(tasks) - task_successful - task_failed)
        outcome_state = _aggregate_work_outcome(
            task_successful, task_failed, task_pending,
            cancelled=bool(team.get("cancelled")),
            cancelled_failure_count=int(task_counts.get("cancelled", 0)),
        )
        failure_reasons = task_failure_reasons
    else:
        agent_successful = int(counts.get("completed", 0))
        agent_failed = sum(int(counts.get(state, 0)) for state in TERMINAL_STATUSES if state != "completed")
        agent_failed += int(counts.get("missing", 0))
        agent_pending = max(0, len(agent_ids) - agent_successful - agent_failed)
        outcome_state = _aggregate_work_outcome(
            agent_successful, agent_failed, agent_pending,
            cancelled=bool(team.get("cancelled")),
            cancelled_failure_count=int(counts.get("cancelled", 0)),
        )
        failure_reasons = legacy_failure_reasons

    if scheduler_v1:
        if team.get("cancelled"):
            team_status = "cancelled"
        else:
            all_terminal = bool(tasks) and all(str(task.get("state") or "") in _GRAPH_TASK_TERMINAL for task in tasks)
            if all_terminal and team.get("budget_exhausted_reason"):
                team_status = "budget_exhausted"
            elif all_terminal and all(task.get("state") == "completed" for task in tasks):
                team_status = "completed"
            elif all_terminal and any(task.get("state") == "quality_failed" for task in tasks):
                team_status = "quality_failed"
            elif all_terminal:
                team_status = "completed_with_failures"
            else:
                team_status = "running"
    elif agent_ids and counts.get("completed", 0) == len(agent_ids):
        team_status = "completed"
    elif agent_ids and terminal_count >= len(agent_ids):
        team_status = "completed_with_failures"
    else:
        team_status = "running"
    try:
        global_admission = admission_snapshot(AGENTS_DIR)
        global_admission_public = {
            "global_active": global_admission.get("global_active"),
            "global_limit": global_admission.get("global_limit"),
            "provider_active": global_admission.get("provider_active"),
            "provider_limits": global_admission.get("provider_limits"),
            "queued_count": global_admission.get("queued_count"),
        }
    except Exception:
        global_admission_public = None
    return {
        "team_id": team_id,
        "status": team_status,
        "success": outcome_state["success"],
        "outcome": outcome_state["outcome"],
        "partial_failure": outcome_state["partial_failure"],
        "successful_count": outcome_state["successful_count"],
        "failure_count": outcome_state["failure_count"],
        "pending_count": outcome_state["pending_count"],
        "work_count": outcome_state["work_count"],
        "failure_reasons": failure_reasons,
        "title": team.get("title"),
        "provider": team.get("provider"),
        "model": team.get("model"),
        "reasoning": team.get("reasoning"),
        "project": team.get("project"),
        "access_mode": team.get("access_mode"),
        "permission_profile": team.get("permission_profile"),
        "scope": team.get("scope"),
        "access_mode_enforced": access_info["enforced"],
        "process_boundary": access_info.get("boundary"),
        "access_mode_note": access_info["note"],
        "created_at": team.get("created_at"),
        "updated_at": team.get("updated_at"),
        "agent_ids": agent_ids,
        "count": len(agent_ids),
        "status_counts": counts,
        "terminal_count": terminal_count,
        "parent_team_id": team.get("parent_team_id"),
        "owner_agent_id": team.get("owner_agent_id"),
        "lineage_root_agent_id": team.get("lineage_root_agent_id"),
        "scheduler_version": team.get("scheduler_version"),
        "max_parallel": team.get("max_parallel"),
        "max_revisions": team.get("max_revisions"),
        "budget": _team_budget_snapshot(team),
        "budget_exhausted_reason": team.get("budget_exhausted_reason"),
        "last_retry_reason": team.get("last_retry_reason"),
        "last_retry_block_reason": team.get("last_retry_block_reason"),
        "global_admission": global_admission_public,
        "task_count": len(tasks),
        "task_status_counts": task_counts,
        "tasks": public_tasks,
    }

def _tail_text(path: Path, max_lines: int = 40, max_chars: int = 6000) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max(1, max_lines):])[-max_chars:]


def _adaptive_retry_decision(
    meta: Dict[str, Any], exit_code: int, stop_reason: Optional[str], stdout_path: Path, stderr_path: Path,
    stdout_offset: int = 0, stderr_offset: int = 0,
) -> Dict[str, Any]:
    reason = str(stop_reason or "").strip().lower()
    if exit_code == 0 and not reason:
        return {"retryable": False, "reason": "success", "backoff_s": 0.0}
    if reason == "cancelled":
        return {"retryable": False, "reason": "cancelled", "backoff_s": 0.0}
    if reason == "timeout":
        return {"retryable": True, "reason": "timeout", "backoff_s": 1.5}
    if reason == "stalled":
        return {"retryable": True, "reason": "stalled", "backoff_s": 2.0}
    if reason == "rate_limited":
        cooldown_until = float(meta.get("cooldown_until") or 0.0)
        delay = max(1.0, cooldown_until - _now()) if cooldown_until else 5.0
        return {"retryable": True, "reason": "rate_limited", "backoff_s": min(MAX_GENERIC_RETRY_BACKOFF_S, delay)}
    if reason in {"resume_required", "outcome_unknown"}:
        return {"retryable": False, "reason": reason, "backoff_s": 0.0}

    def attempt_tail(path: Path, offset: int) -> str:
        if not path.exists():
            return ""
        try:
            raw = path.read_bytes()[max(0, int(offset)):]
            return raw[-12000:].decode("utf-8", errors="replace")[-6000:]
        except OSError:
            return ""

    tail = ("\n".join((
        attempt_tail(stdout_path, stdout_offset),
        attempt_tail(stderr_path, stderr_offset),
    ))).lower()
    non_retryable = (
        ("authentication", "auth_error"), ("not authenticated", "auth_error"),
        ("unauthorized", "auth_error"), ("invalid api key", "auth_error"),
        ("forbidden", "permission_error"), ("permission denied", "permission_error"),
        ("insufficient_quota", "quota_exhausted"), ("billing", "quota_exhausted"),
        ("usage limit", "quota_exhausted"), ("model not found", "invalid_model"),
        ("unknown model", "invalid_model"), ("invalid model", "invalid_model"),
        ("invalid argument", "invalid_request"), ("bad request", "invalid_request"),
        ("400 bad request", "invalid_request"), ("401 unauthorized", "auth_error"),
        ("403 forbidden", "permission_error"),
    )
    for marker, classification in non_retryable:
        if marker in tail:
            return {"retryable": False, "reason": classification, "backoff_s": 0.0}

    retryable = (
        ("rate limit", "rate_limited", 5.0), ("rate_limit", "rate_limited", 5.0),
        ("too many requests", "rate_limited", 5.0), (" 429", "rate_limited", 5.0),
        ("temporarily unavailable", "provider_unavailable", 3.0),
        ("service unavailable", "provider_unavailable", 3.0),
        ("overloaded", "provider_overloaded", 4.0),
        ("connection reset", "transient_transport", 2.0),
        ("connection refused", "transient_transport", 2.0),
        ("network error", "transient_transport", 2.0),
        ("econnreset", "transient_transport", 2.0),
        ("econnrefused", "transient_transport", 2.0),
        ("timed out", "transient_transport", 2.0),
        ("gateway timeout", "provider_unavailable", 3.0),
        ("bad gateway", "provider_unavailable", 3.0),
        (" 502", "provider_unavailable", 3.0),
        (" 503", "provider_unavailable", 3.0),
        (" 504", "provider_unavailable", 3.0),
    )
    for marker, classification, backoff in retryable:
        if marker in tail:
            return {"retryable": True, "reason": classification, "backoff_s": backoff}
    return {"retryable": False, "reason": "provider_error_nonretryable", "backoff_s": 0.0}


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_group(pid: Optional[int], sig: int) -> None:
    if not pid:
        return
    try:
        os.killpg(int(pid), sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _reap_worker(agent_id: str, proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    finally:
        browser_tabs.release_agent_leases(agent_id)
        with _WORKERS_LOCK:
            _WORKERS.pop(agent_id, None)


def _keychain_secret(service: str, account: str) -> Optional[str]:
    if not service or not account:
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _base_env() -> Dict[str, str]:
    env = os.environ.copy()
    home = Path.home()
    user = os.getenv("USER") or home.name
    path_parts = [
        str(home / ".opencode" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    inherited = env.get("PATH", "")
    if inherited:
        path_parts.insert(0, inherited)
    env.update({
        "HOME": str(home),
        "USER": user,
        "LOGNAME": os.getenv("LOGNAME") or user,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "PATH": ":".join(path_parts),
    })
    root = str(BASE_DIR.parent)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (":" + existing_pythonpath if existing_pythonpath else "")

    if not env.get("OPENROUTER_API_KEY"):
        service = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_SERVICE", "openrouter-api-key").strip()
        account = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_ACCOUNT", user).strip()
        key = _keychain_secret(service, account)
        if key:
            env["OPENROUTER_API_KEY"] = key
    return env


def _chatgpt_env() -> Dict[str, str]:
    env = _base_env()
    # This provider drives an authenticated interactive web browser. CI=1 changes
    # browser/automation behavior and can prevent ChatGPT UI controls from hydrating.
    env.pop("CI", None)
    return env


_PROVIDER_ENV_PASSTHROUGH: Dict[str, Tuple[str, ...]] = {
    "chatgpt": ("CHATGPT_CLI_PROFILE", "CHATGPT_CLI_CHROME", "CHATGPT_CLI_IDLE_SECONDS"),
    "codex": ("CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"),
}
_OPENCODE_MODEL_ENV: Dict[str, Tuple[str, ...]] = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"),
    "google": (
        "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_VERTEX_API_KEY",
        "GOOGLE_VERTEX_PROJECT", "GOOGLE_VERTEX_LOCATION",
    ),
    "azure": ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_RESOURCE_NAME"),
}
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_RESTRICTED_READ_DENY_ROOTS = ("/Users", "/private/tmp", "/private/var/tmp", "/private/var/folders", "/Volumes", "/Network")
_RESTRICTED_ESCAPE_EXECUTABLES = (
    "/usr/bin/security", "/usr/bin/osascript", "/usr/bin/open", "/usr/bin/shortcuts",
    "/bin/launchctl", "/usr/bin/sudo", "/usr/bin/su",
)


def _minimal_provider_env(provider: str, meta: Dict[str, Any]) -> Dict[str, str]:
    """Build an explicit provider environment instead of inheriting the server environment."""
    provider = str(provider or "").strip().lower()
    home = Path.home().resolve()
    user = os.getenv("USER") or home.name
    env: Dict[str, str] = {
        "HOME": str(home),
        "USER": user,
        "LOGNAME": os.getenv("LOGNAME") or user,
        "SHELL": "/bin/zsh",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "NO_COLOR": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "PATH": ":".join((
            str(home / ".opencode" / "bin"), str(home / ".npm-global" / "bin"),
            str(home / ".local" / "bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin",
            "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        )),
    }
    if provider != "chatgpt":
        env["CI"] = "1"
    for key in _PROVIDER_ENV_PASSTHROUGH.get(provider, ()):
        value = os.getenv(key)
        if value:
            env[key] = value
    if provider == "opencode":
        env.update({
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_SHARE": "1",
        })
        model = str(meta.get("model") or "").strip()
        model_provider = model.split("/", 1)[0].lower() if "/" in model else ""
        for key in _OPENCODE_MODEL_ENV.get(model_provider, ()):
            value = os.getenv(key)
            if value:
                env[key] = value
        if model_provider == "openrouter" and not env.get("OPENROUTER_API_KEY"):
            service = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_SERVICE", "openrouter-api-key").strip()
            account = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_ACCOUNT", user).strip()
            key = _keychain_secret(service, account)
            if key:
                env["OPENROUTER_API_KEY"] = key
    return env


def _sandbox_exec_available() -> bool:
    return sys.platform == "darwin" and _SANDBOX_EXEC.is_file() and os.access(_SANDBOX_EXEC, os.X_OK)


def _canonical_boundary_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _sbpl_quote(value: str | Path) -> str:
    return _canonical_boundary_path(value).replace("\\", "\\\\").replace('"', '\\"')


def _restricted_provider_state(agent_id: str) -> Path:
    root = _agent_dir(agent_id) / "provider_state"
    for path in (root, root / "home", root / "cache", root / "data", root / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return root


def _opencode_sandbox_profile(agent_id: str, meta: Dict[str, Any]) -> Path:
    if not _sandbox_exec_available():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "OpenCode restricted access requires macOS sandbox-exec; this host cannot enforce the requested process boundary.",
        )
    scope = ResourceScope.from_dict(meta.get("scope"))
    access_mode = str(meta.get("access_mode") or "workspace_write")
    state_root = _restricted_provider_state(agent_id).resolve()
    allowed_read = {state_root, (_agent_dir(agent_id) / "provider_config").resolve(strict=False)}
    for raw in scope.path_roots or ():
        allowed_read.add(Path(raw).expanduser().resolve(strict=False))
    binary_raw = str(meta.get("binary") or "").strip()
    if binary_raw:
        try:
            allowed_read.add(Path(binary_raw).expanduser().resolve(strict=False).parent)
        except OSError:
            pass
    deny_specs = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in _RESTRICTED_READ_DENY_ROOTS)
    read_specs = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in sorted(allowed_read, key=lambda item: str(item)))
    write_roots = {state_root, (_agent_dir(agent_id) / "provider_config").resolve(strict=False)}
    if access_mode == "workspace_write":
        write_roots.update(Path(raw).expanduser().resolve(strict=False) for raw in (scope.path_roots or ()))
    write_specs = " ".join(f'(subpath "{_sbpl_quote(root)}")' for root in sorted(write_roots, key=lambda item: str(item)))
    exec_denies = "\n".join(
        f'(deny process-exec (literal "{_sbpl_quote(path)}"))' for path in _RESTRICTED_ESCAPE_EXECUTABLES
    )
    profile = _agent_dir(agent_id) / "provider-boundary.sb"
    profile.write_text(
        "(version 1)\n"
        "(allow default)\n"
        f"(deny file-read* {deny_specs})\n"
        f"(allow file-read* {read_specs})\n"
        "(deny file-write*)\n"
        f"(allow file-write* {write_specs})\n"
        f"{exec_denies}\n",
        encoding="utf-8",
    )
    profile.chmod(0o600)
    return profile


def _provider_process_command(agent_id: str, meta: Dict[str, Any], cmd: List[str]) -> Tuple[List[str], Optional[Path]]:
    provider = str(meta.get("provider") or "").lower()
    access_mode = str(meta.get("access_mode") or "workspace_write")
    if provider == "opencode" and access_mode != "full":
        profile = _opencode_sandbox_profile(agent_id, meta)
        return [str(_SANDBOX_EXEC), "-f", str(profile), *cmd], profile
    return cmd, None


def _provider_env(agent_id: str, meta: Dict[str, Any], scoped_token: str) -> Tuple[Dict[str, str], Optional[Path]]:
    provider = str(meta.get("provider") or "").lower()
    env = _minimal_provider_env(provider, meta)
    if scoped_token:
        env["MAC_MCP_AGENT_TOKEN"] = scoped_token
    else:
        env.pop("MAC_MCP_AGENT_TOKEN", None)
    cleanup_root: Optional[Path] = None
    if provider == "opencode":
        if str(meta.get("access_mode") or "workspace_write") != "full":
            state_root = _restricted_provider_state(agent_id)
            env.update({
                "HOME": str(state_root / "home"),
                "TMPDIR": str(state_root / "tmp"),
                "XDG_CACHE_HOME": str(state_root / "cache"),
                "XDG_DATA_HOME": str(state_root / "data"),
            })
            auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
            try:
                if auth_path.is_file():
                    env["OPENCODE_AUTH_CONTENT"] = auth_path.read_text(encoding="utf-8")
            except OSError:
                pass
            # OpenCode receives its scoped MCP credential in the private generated config.
            # Do not also expose that bearer token through native child-process environment.
            env.pop("MAC_MCP_AGENT_TOKEN", None)
        cleanup_root = _agent_dir(agent_id) / "provider_config"
        config_dir = cleanup_root / "opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        try:
            cleanup_root.chmod(0o700)
            config_dir.chmod(0o700)
        except OSError:
            pass
        config_path = config_dir / "opencode.jsonc"
        payload: Dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "mac-mcp": {
                    "type": "remote",
                    "url": str(meta.get("mcp_endpoint") or "http://127.0.0.1:8765/mcp"),
                    "headers": {"Authorization": f"Bearer {scoped_token}"},
                }
            },
        }
        if str(meta.get("access_mode") or "workspace_write") != "full":
            payload["permission"] = {
                "bash": "deny",
                "task": "deny",
                "lsp": "deny",
                "skill": "deny",
                "external_directory": "deny",
                "edit": "deny" if str(meta.get("access_mode")) == "read_only" else "allow",
            }
        selected_model = str(meta.get("model") or "").strip()
        if selected_model.startswith("openrouter/"):
            model_id = selected_model.removeprefix("openrouter/")
            small_model = selected_model
            models: Dict[str, Any] = {model_id: {}}
            if model_id == "nex-agi/nex-n2.5-pro:free":
                small_model = "openrouter/nex-agi/nex-n2.5-mini:free"
                models["nex-agi/nex-n2.5-mini:free"] = {}
            payload["provider"] = {
                "openrouter": {
                    "options": {"apiKey": "{env:OPENROUTER_API_KEY}"},
                    "models": models,
                }
            }
            payload["small_model"] = small_model
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path.chmod(0o600)
        env["XDG_CONFIG_HOME"] = str(cleanup_root)
    return env, cleanup_root


def _cleanup_provider_config(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _codex_scoped_mcp_args(meta: Dict[str, Any]) -> List[str]:
    if not meta.get("scoped_mcp"):
        return []
    endpoint = str(meta.get("mcp_endpoint") or "http://127.0.0.1:8765/mcp")
    return [
        "--config", f"mcp_servers.mac-mcp.url={json.dumps(endpoint)}",
        "--config", 'mcp_servers.mac-mcp.bearer_token_env_var="MAC_MCP_AGENT_TOKEN"',
    ]


def _find_binary(provider: str) -> Optional[str]:
    provider = provider.lower()
    home = Path.home()
    if provider == "opencode":
        candidates = [
            provider_setting("opencode", "binary_path", ""),
            os.getenv("OPENCODE_BINARY"),
            shutil.which("opencode"),
            "/opt/homebrew/bin/opencode",
            str(home / ".opencode" / "bin" / "opencode"),
            "/usr/local/bin/opencode",
        ]
    elif provider == "codex":
        candidates = [
            provider_setting("codex", "binary_path", ""),
            os.getenv("CODEX_BINARY"),
            shutil.which("codex"),
            "/opt/homebrew/bin/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/usr/local/bin/codex",
            str(home / ".npm-global" / "bin" / "codex"),
        ]
    elif provider == "chatgpt":
        candidates = [
            provider_setting("chatgpt", "binary_path", ""),
            os.getenv("CHATGPT_WEB_CLI_BINARY"),
            os.getenv("CHATGPT_CLI_BINARY"),
            shutil.which("chatgpt-web"),
            shutil.which("chatgpt"),
            str(home / "Projects" / "chatgpt-web-cli" / "bin" / "chatgpt"),
        ]
    else:
        return None
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate))
    return None


def _version(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, env=_base_env())
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def _opencode_models(binary: str) -> List[str]:
    try:
        proc = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, env=_base_env())
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip() and "/" in line]
    except Exception:
        return []


def _codex_known_models() -> Tuple[List[str], Optional[str], Optional[str]]:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return [], None, None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None, None
    default_model_match = re.search(r'^model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    default_reasoning_match = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', text, re.MULTILINE)
    section_match = re.search(r'\[tui\.model_availability_nux\](.*?)(?:\n\[|\Z)', text, re.DOTALL)
    models: List[str] = []
    if section_match:
        models.extend(re.findall(r'^"([^"]+)"\s*=', section_match.group(1), re.MULTILINE))
    default_model = default_model_match.group(1) if default_model_match else None
    if default_model and default_model not in models:
        models.insert(0, default_model)
    return models, default_model, default_reasoning_match.group(1) if default_reasoning_match else None


def _chatgpt_cached_models(binary: str) -> Tuple[List[str], Optional[str]]:
    try:
        root = Path(binary).resolve().parent.parent
        payload = json.loads((root / "state.json").read_text(encoding="utf-8"))
        rows = payload.get("modelCache") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return [], None
        models: List[str] = []
        selected: Optional[str] = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            models.append(name)
            if row.get("selected"):
                selected = name
        return models, selected
    except Exception:
        return [], None


def _chatgpt_models(binary: str) -> Tuple[List[str], Optional[str]]:
    # Catalog/model discovery should not generate browser traffic on every MCP call.
    # A successful `chatgpt models` run persists this cache; use it first and only
    # touch the live web UI when no cache exists yet.
    cached_models, cached_selected = _chatgpt_cached_models(binary)
    if cached_models:
        return cached_models, cached_selected
    try:
        proc = subprocess.run([binary, "models", "--json"], capture_output=True, text=True, timeout=40, env=_chatgpt_env())
        if proc.returncode == 0:
            rows = json.loads(proc.stdout or "[]")
            if isinstance(rows, list):
                models: List[str] = []
                selected: Optional[str] = None
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if not name:
                        continue
                    models.append(name)
                    if row.get("selected"):
                        selected = name
                if models:
                    return models, selected
    except Exception:
        pass
    return [], None


def _chatgpt_effort(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not raw or raw == "none":
        return None
    aliases = {
        "low": "low", "medium": "medium", "high": "high",
        "xhigh": "extra-high", "extra-high": "extra-high", "extrahigh": "extra-high", "max": "extra-high",
    }
    return aliases.get(raw)


def _chatgpt_default_project() -> Optional[str]:
    configured = provider_setting("chatgpt", "default_project", "")
    value = str(configured or os.getenv("CHATGPT_SUBAGENT_PROJECT", "") or "").strip()
    return value or None


def _chatgpt_int_setting(name: str, env_name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(env_name)
    if raw is None or not str(raw).strip():
        raw = provider_setting("chatgpt", name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _chatgpt_budget_config() -> Tuple[int, int]:
    soft = _chatgpt_int_setting(
        "turn_budget_s", "CHATGPT_PROVIDER_TURN_BUDGET_S",
        DEFAULT_CHATGPT_TURN_BUDGET_S, 60, 3600,
    )
    hard = _chatgpt_int_setting(
        "hard_tool_budget_s", "CHATGPT_PROVIDER_HARD_TOOL_BUDGET_S",
        DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S, 120, 7200,
    )
    return soft, max(soft, hard)


def _chatgpt_rate_limit_reason_text(text: str) -> Optional[str]:
    normalized = str(text or "").lower().replace("’", "'")
    patterns = (
        ("requesting_too_fast", r"you(?:'re| are) (?:making )?requests too (?:quickly|fast)"),
        ("temporarily_limited", r"temporarily limited access to your conversations"),
        ("provider_rate_limited", r"chatgpt(?:_web)?_rate_limited|temporarily rate-limited"),
        ("too_many_requests", r"too many requests|http\s*429|\b429\b"),
        ("rate_limit", r"rate[- ]?limit(?:ed|ing)?"),
    )
    for reason, pattern in patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            return reason
    return None


def _log_text_since(path: Path, offset: int = 0, max_chars: int = 8000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, int(offset)))
            data = handle.read(max_chars * 4)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-max_chars:]


def _chatgpt_rate_limit_reason(
    stdout_path: Path,
    stderr_path: Path,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
) -> Optional[str]:
    text = _log_text_since(stdout_path, stdout_offset) + "\n" + _log_text_since(stderr_path, stderr_offset)
    return _chatgpt_rate_limit_reason_text(text)


def _chatgpt_cooldown_seconds(throttle_count: int) -> int:
    base = _chatgpt_int_setting(
        "rate_limit_backoff_s", "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_S",
        DEFAULT_CHATGPT_RATE_LIMIT_BACKOFF_S, 5, 600,
    )
    cap = _chatgpt_int_setting(
        "rate_limit_backoff_cap_s", "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_CAP_S",
        MAX_CHATGPT_RATE_LIMIT_BACKOFF_S, base, 1800,
    )
    exponent = max(0, min(6, int(throttle_count) - 1))
    return min(cap, base * (2 ** exponent))


def _chatgpt_provider_state_path() -> Path:
    return AGENTS_DIR / ".chatgpt-provider-state.json"


def _update_chatgpt_provider_state(update: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = AGENTS_DIR / ".chatgpt-provider-state.lock"
    state_path = _chatgpt_provider_state_path()
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                state = {}
            update(state)
            fd, tmp_name = tempfile.mkstemp(prefix=".chatgpt-provider-state.", suffix=".tmp", dir=AGENTS_DIR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(state, ensure_ascii=False, sort_keys=True))
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(tmp_name, state_path)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            return state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _record_chatgpt_shared_throttle(cooldown_until: float, reason: str, now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else _now())
    reduced_for = _chatgpt_int_setting(
        "reduced_concurrency_s", "CHATGPT_PROVIDER_REDUCED_CONCURRENCY_S",
        DEFAULT_CHATGPT_REDUCED_CONCURRENCY_S, 60, 3600,
    )
    def mutate(state: Dict[str, Any]) -> None:
        state["throttle_count"] = int(state.get("throttle_count") or 0) + 1
        state["last_throttled_at"] = current
        state["last_throttle_reason"] = reason
        state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), float(cooldown_until))
        state["reduced_until"] = max(float(state.get("reduced_until") or 0), current + reduced_for)
        state["next_allowed_at"] = max(float(state.get("next_allowed_at") or 0), float(cooldown_until))
    return _update_chatgpt_provider_state(mutate)


def _reserve_chatgpt_provider_start(now: Optional[float] = None) -> float:
    current = float(now if now is not None else _now())
    spacing = _chatgpt_int_setting(
        "post_throttle_start_spacing_s", "CHATGPT_PROVIDER_POST_THROTTLE_SPACING_S",
        DEFAULT_CHATGPT_START_SPACING_S, 1, 120,
    )
    delay = 0.0
    def mutate(state: Dict[str, Any]) -> None:
        nonlocal delay
        cooldown_until = float(state.get("cooldown_until") or 0)
        reduced_until = float(state.get("reduced_until") or 0)
        if current >= reduced_until and current >= cooldown_until:
            state["next_allowed_at"] = current
            delay = 0.0
            return
        reserved = max(current, cooldown_until, float(state.get("next_allowed_at") or 0))
        state["next_allowed_at"] = reserved + spacing
        delay = max(0.0, reserved - current)
    _update_chatgpt_provider_state(mutate)
    return delay


def _wait_chatgpt_provider_gate(agent_id: str) -> bool:
    delay = _reserve_chatgpt_provider_start()
    if delay <= 0:
        return True
    def mark_waiting(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current["phase"] = "throttled"
        current["note"] = f"ChatGPT provider cooldown active; next safe start in about {int(delay)}s."
        current["updated_at"] = _now()
        return True
    _update_meta(agent_id, mark_waiting)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        try:
            if _read_meta(agent_id).get("status") == "cancelled":
                return False
        except HTTPException:
            return False
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
    def mark_ready(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current["phase"] = "retrying" if int(current.get("retry_count") or 0) > 0 else "provider_starting"
        current["updated_at"] = _now()
        return True
    _update_meta(agent_id, mark_ready)
    return True


def _chatgpt_turn_budget_action(meta: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
    if str(meta.get("provider") or "").lower() != "chatgpt" or meta.get("checkpoint_pending"):
        return None
    current = float(now if now is not None else _now())
    turn_started = float(meta.get("turn_started_at") or meta.get("provider_started_at") or current)
    soft = int(meta.get("turn_budget_s") or DEFAULT_CHATGPT_TURN_BUDGET_S)
    hard = int(meta.get("hard_tool_budget_s") or DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S)
    active_tool_started = meta.get("active_tool_started_at")
    if active_tool_started:
        if current - float(active_tool_started) >= hard:
            return "hard_tool_budget"
        if current - turn_started >= soft:
            return "wait_for_tool"
        return None
    if current - turn_started >= soft:
        return "turn_budget"
    return None


def _chatgpt_checkpoint_prompt(reason: str) -> str:
    detail = "the current tool exceeded the hard safety budget" if reason == "hard_tool_budget" else "the current turn reached its budget"
    return (
        "Continue the same delegated task from the current conversation state. This is an automatic checkpoint because "
        f"{detail}. Do not repeat completed work or duplicate external side effects. Reconcile the latest visible tool/results "
        "first, then continue toward the original goal and finish with the requested handoff when complete."
    )


def _chatgpt_checkpoint_command(meta: Dict[str, Any], reason: str) -> Optional[List[str]]:
    target = str(meta.get("provider_job_id") or meta.get("session_id") or "").strip()
    binary = str(meta.get("binary") or "").strip()
    if not target or not binary:
        return None
    cmd = [binary, "interrupt", target]
    model = str(meta.get("model") or "").strip()
    if model:
        cmd += ["--model", model]
    effort = _chatgpt_effort(meta.get("reasoning"))
    if effort:
        cmd += ["--effort", effort]
    cmd.append(_chatgpt_checkpoint_prompt(reason))
    return cmd


def _request_chatgpt_checkpoint(agent_id: str, meta: Dict[str, Any], reason: str) -> bool:
    cmd = _chatgpt_checkpoint_command(meta, reason)
    if not cmd:
        return False
    requested_at = _now()
    def mark_pending(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled" or current.get("checkpoint_pending"):
            return False
        current.update({
            "checkpoint_pending": True, "phase": "checkpointing", "last_checkpoint_reason": reason,
            "last_checkpoint_requested_at": requested_at,
            "note": "ChatGPT turn budget reached; checkpointing into a fresh turn.", "updated_at": requested_at,
        })
        return True
    latest = _update_meta(agent_id, mark_pending)
    if not latest.get("checkpoint_pending"):
        return False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=_chatgpt_env())
    except (OSError, subprocess.SubprocessError) as exc:
        proc = None
        error = str(exc)
    else:
        error = (proc.stderr or proc.stdout or "").strip()
    if proc is None or proc.returncode != 0:
        def mark_failed(current: Dict[str, Any]) -> None:
            current["checkpoint_pending"] = False
            current["checkpoint_failures"] = int(current.get("checkpoint_failures") or 0) + 1
            current["phase"] = "tool" if current.get("active_tool_started_at") else "reasoning"
            current["note"] = "Automatic ChatGPT checkpoint failed; continuing current turn without duplicate retry."
            current["last_checkpoint_error"] = str(error or "checkpoint request failed")[-500:]
            current["updated_at"] = _now()
        _update_meta(agent_id, mark_failed)
        return False
    def mark_requested(current: Dict[str, Any]) -> None:
        current["checkpoint_count"] = int(current.get("checkpoint_count") or 0) + 1
        current["last_checkpoint_at"] = _now()
        current["updated_at"] = _now()
    _update_meta(agent_id, mark_requested)
    return True


def _chatgpt_session_for_job(meta: Dict[str, Any]) -> Optional[str]:
    existing = str(meta.get("session_id") or "").strip()
    if existing:
        return existing
    job_id = str(meta.get("provider_job_id") or "").strip()
    binary = str(meta.get("binary") or "").strip()
    if not job_id or not binary:
        return None
    try:
        proc = subprocess.run([binary, "jobs", "--all", "--json"], capture_output=True, text=True, timeout=10, env=_chatgpt_env())
        rows = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("id") or "") == job_id:
            session_id = str(row.get("sessionId") or row.get("session_id") or "").strip()
            return session_id or None
    return None


def _access_mode_info(provider: str, access_mode: str) -> Dict[str, Any]:
    provider = str(provider or "").lower()
    access_mode = str(access_mode or "workspace_write")
    if access_mode == "full":
        return {
            "enforced": False,
            "boundary": "explicit_full",
            "note": "Full mode is an intentional unrestricted provider process; no filesystem sandbox is claimed.",
        }
    if provider == "codex":
        return {
            "enforced": False,
            "boundary": "unsupported",
            "note": (
                "This Codex CLI build does not enforce workspace-scoped reads for legacy sandbox or permission-profile modes. "
                "Restricted modes are refused; use full only when intentionally granting broad local access."
            ),
        }
    if provider == "opencode":
        available = _sandbox_exec_available()
        return {
            "enforced": available,
            "boundary": "macos_seatbelt" if available else "unsupported",
            "note": (
                "OpenCode runs inside a Mac MCP macOS Seatbelt boundary with scoped filesystem roots and a sanitized environment."
                if available else
                "OpenCode restricted access is unavailable because macOS sandbox-exec is not available; the request is refused."
            ),
        }
    if provider == "chatgpt":
        return {
            "enforced": False,
            "boundary": "unsupported",
            "note": (
                "ChatGPT Web CLI cannot truthfully enforce read_only/workspace_write as an OS boundary because the authenticated "
                "web runtime can act outside the local subprocess filesystem. Restricted modes are refused; use full only when intended."
            ),
        }
    return {"enforced": False, "boundary": "unsupported", "note": "Provider process boundary is unsupported."}


def _validate_provider_access_mode(provider: str, access_mode: str) -> None:
    provider = str(provider or "").lower()
    access_mode = str(access_mode or "workspace_write")
    if access_mode == "full":
        return
    if provider == "opencode" and not _sandbox_exec_available():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "OpenCode restricted access is unavailable: macOS sandbox-exec is missing, so Mac MCP refuses to rely on prompt-only boundaries.",
        )
    if provider == "codex":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Codex restricted access is unavailable on this provider build: live boundary probes show that both legacy sandbox and "
            "permission-profile modes can read outside the requested workspace. The request was refused instead of claiming a false guarantee; "
            "use full explicitly when broad local access is intended.",
        )
    if provider == "chatgpt":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "ChatGPT Web CLI restricted access is unavailable: its authenticated browser/account runtime cannot be OS-confined to the "
            "requested local scope. The request was refused instead of presenting a false read-only/browser-only guarantee; use full explicitly.",
        )

def _requested_agent_scope(
    workdir: Path,
    access_mode: str,
    raw_scope: Optional[Dict[str, Any]],
    parent_scope: Optional[ResourceScope],
    parent_profile: str,
    capability_profile: Optional[str] = None,
) -> Tuple[ResourceScope, str, str]:
    requested_capability = str(capability_profile or "").strip().lower()
    preset = _AGENT_CAPABILITY_PROFILES.get(requested_capability) if requested_capability else None
    if requested_capability and preset is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"capability_profile must be one of: {', '.join(sorted(_AGENT_CAPABILITY_PROFILES))}",
        )
    effective_access_mode = str(preset["access_mode"] if preset else access_mode)
    try:
        mode = normalize_access_mode(effective_access_mode)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    profile = PROFILES.get(str(parent_profile or "trusted").strip().lower())
    if profile is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown parent permission profile.")
    if not access_mode_allows(profile.access_mode_ceiling, mode):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requested agent access_mode exceeds the parent permission profile.")

    target_profile = str(preset["permission_profile"] if preset else narrow_child_profile(profile.name, mode))
    if not profile_contains(profile.name, target_profile):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "capability_profile_denied: child profile cannot widen the parent profile.")

    data: Dict[str, Any] = dict(raw_scope or {})
    preset_families = None if preset is None else preset.get("tool_families")
    if preset_families is not None:
        requested_families = data.get("tool_families")
        if requested_families is None:
            data["tool_families"] = list(preset_families)
        elif not set(str(value) for value in requested_families).issubset(set(preset_families)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_denied: tool_families exceed capability_profile.")
    if "access_mode" in data:
        try:
            scoped_mode = normalize_access_mode(data["access_mode"])
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if scoped_mode != mode:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope.access_mode must match access_mode.")
    data["access_mode"] = mode.value
    if "path_roots" not in data and mode.value != "full":
        data["path_roots"] = [str(workdir)]
    try:
        requested = ResourceScope.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid scope: {exc}") from exc

    parent = parent_scope or ResourceScope.unrestricted()
    if not scope_contains(parent, requested):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_denied: child scope cannot widen the parent scope.")
    effective = child_scope(parent, requested)
    return effective, target_profile, (requested_capability or "legacy")


def _scope_prompt(scope: ResourceScope, profile: str, provider: str) -> str:
    scope_json = json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True)
    base = (
        "This delegated agent has a server-enforced Mac MCP scope. Do not attempt to work around it. "
        f"Permission profile: {profile}. Scope: {scope_json}. "
        "Use only the resources and tool families inside this scope."
    )
    if provider == "opencode":
        return (
            base
            + " In restricted access modes, Mac MCP also places the entire OpenCode provider process and its descendants "
              "inside a macOS Seatbelt filesystem boundary. Do not attempt to evade that boundary or launch external UI/keychain helpers."
        )
    if provider == "chatgpt":
        return (
            "This delegated agent runs through the authenticated ChatGPT web UI. Mac MCP permits this provider only with "
            f"explicit full access because restricted local OS confinement is not enforceable. Permission profile: {profile}. "
            f"Scope metadata: {scope_json}."
        )
    return base


def provider_overview() -> Dict[str, Any]:
    labels = {"opencode": "OpenCode", "codex": "Codex", "chatgpt": "ChatGPT Web CLI"}
    rows: List[Dict[str, Any]] = []
    for provider in ("opencode", "codex", "chatgpt"):
        binary = _find_binary(provider)
        rows.append({
            "id": provider,
            "name": labels[provider],
            "enabled": provider_enabled(provider),
            "detected": bool(binary),
            "binary_path": binary,
            "version": _version(binary),
        })
    return {"ok": True, "providers": rows}


def agent_catalog(
    settings: Settings,
    provider: Optional[str] = None,
    model_filter: Optional[str] = None,
    free_only: bool = False,
    limit: int = 80,
) -> Dict[str, Any]:
    requested = provider.lower().strip() if provider else None
    limit = max(1, min(int(limit), 200))
    if requested and requested not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if requested and not provider_enabled(requested):
        return {"ok": True, "providers": {}}

    providers: Dict[str, Any] = {}
    if (not requested or requested == "opencode") and provider_enabled("opencode"):
        binary = _find_binary("opencode")
        all_models = _opencode_models(binary) if binary else []
        free_models = [m for m in all_models if "free" in m.lower()]
        matched = free_models if free_only else all_models
        if model_filter:
            q = model_filter.lower().strip()
            matched = [m for m in matched if q in m.lower()]
        providers["opencode"] = {
            "available": bool(binary),
            "version": _version(binary),
            "model_count": len(all_models),
            "matched_count": len(matched),
            "models": matched[:limit],
            "models_truncated": len(matched) > limit,
            "free_models": free_models[:50],
            "reasoning": "Pass a model-supported OpenCode --variant value such as minimal/low/medium/high/max.",
            "access_modes": {
                "read_only": {"supported": _sandbox_exec_available(), **_access_mode_info("opencode", "read_only")},
                "workspace_write": {"supported": _sandbox_exec_available(), **_access_mode_info("opencode", "workspace_write")},
                "full": {"supported": True, **_access_mode_info("opencode", "full")},
            },
        }
    if (not requested or requested == "codex") and provider_enabled("codex"):
        binary = _find_binary("codex")
        models, default_model, default_reasoning = _codex_known_models()
        providers["codex"] = {
            "available": bool(binary),
            "version": _version(binary),
            "models": models,
            "default_model": default_model,
            "default_reasoning": default_reasoning,
            "reasoning_values": ["none", "low", "medium", "high", "xhigh", "max"],
            "access_modes": {
                mode: {"supported": mode == "full", **_access_mode_info("codex", mode)}
                for mode in sorted(_ACCESS_MODES)
            },
        }
    if (not requested or requested == "chatgpt") and provider_enabled("chatgpt"):
        binary = _find_binary("chatgpt")
        models, default_model = _chatgpt_models(binary) if binary else ([], None)
        matched = models
        if model_filter:
            q = model_filter.lower().strip()
            matched = [m for m in matched if q in m.lower()]
        providers["chatgpt"] = {
            "available": bool(binary),
            "version": _version(binary),
            "models": matched[:limit],
            "default_model": default_model,
            "reasoning_values": ["low", "medium", "high", "extra-high"],
            "default_reasoning": str(provider_setting("chatgpt", "default_reasoning", "high") or "high"),
            "turn_budget_s": _chatgpt_budget_config()[0],
            "hard_tool_budget_s": _chatgpt_budget_config()[1],
            "rate_limit_backoff_s": _chatgpt_cooldown_seconds(1),
            "default_project": _chatgpt_default_project(),
            "supports_project_override": True,
            "supports_resume": True,
            "scoped_mcp": False,
            "access_modes": {
                mode: {"supported": mode == "full", **_access_mode_info("chatgpt", mode)}
                for mode in sorted(_ACCESS_MODES)
            },
        }
    return {"ok": True, "providers": providers}


def _worktree_public(state: Any) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return {"enabled": False}
    public = {
        "enabled": bool(state.get("enabled")),
        "mode": state.get("mode"),
        "status": state.get("status"),
        "reason": state.get("reason"),
        "source_cwd": state.get("source_cwd"),
        "path": state.get("path"),
        "branch": state.get("branch"),
        "base_commit": state.get("base_commit"),
        "source_head_at_spawn": state.get("source_head_at_spawn"),
        "worktree_head": state.get("worktree_head"),
        "snapshot_commit": state.get("snapshot_commit"),
        "changed_files": list(state.get("changed_files") or []),
        "change_count": int(state.get("change_count") or 0),
        "has_changes": bool(state.get("has_changes")),
        "pending_changes": bool(state.get("pending_changes")),
        "diff_stat": state.get("diff_stat"),
        "apply_status": state.get("apply_status"),
        "applied_at": state.get("applied_at"),
        "applied_to_head": state.get("applied_to_head"),
        "shared_from_agent_id": state.get("shared_from_agent_id"),
    }
    return public


def _refresh_agent_worktree(agent_id: str) -> Dict[str, Any]:
    meta = _read_meta(agent_id)
    state = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else {}
    if not state or not state.get("enabled"):
        return dict(state or {})
    try:
        refreshed = inspect_worktree(state)
        refreshed.pop("inspection_error", None)
    except AgentWorktreeError as exc:
        refreshed = dict(state)
        refreshed["inspection_error"] = f"{exc.code}: {exc}"
        refreshed["inspected_at"] = _now()
    _persist_shared_worktree_state(agent_id, refreshed)
    return refreshed


def _worktree_referrers(path: str, *, exclude_agent_id: Optional[str] = None) -> List[str]:
    target = str(Path(path).expanduser().resolve(strict=False))
    refs: List[str] = []
    if not AGENTS_DIR.exists():
        return refs
    for entry in AGENTS_DIR.iterdir():
        if not entry.is_dir() or entry.name == exclude_agent_id or not (entry / "meta.json").exists():
            continue
        try:
            meta = _read_meta(entry.name)
        except HTTPException:
            continue
        state = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else {}
        raw = str(state.get("path") or "").strip()
        if raw and str(Path(raw).expanduser().resolve(strict=False)) == target:
            refs.append(entry.name)
    return sorted(refs)


def _active_worktree_referrers(path: str, *, exclude_agent_id: Optional[str] = None) -> List[str]:
    active: List[str] = []
    for ref in _worktree_referrers(path, exclude_agent_id=exclude_agent_id):
        try:
            meta = _normalize(ref, _read_meta(ref))
        except HTTPException:
            continue
        if meta.get("status") not in TERMINAL_STATUSES:
            active.append(ref)
    return active


def _persist_shared_worktree_state(agent_id: str, state: Dict[str, Any]) -> None:
    path = str(state.get("path") or "").strip()
    targets = [agent_id]
    if path:
        targets.extend(_worktree_referrers(path, exclude_agent_id=agent_id))
    for target_id in sorted(set(targets)):
        try:
            def save(current: Dict[str, Any]) -> Optional[bool]:
                existing = current.get("worktree") if isinstance(current.get("worktree"), dict) else {}
                if path and str(existing.get("path") or "") != path:
                    return False
                current["worktree"] = dict(state)
                current["updated_at"] = _now()
                return True
            _update_meta(target_id, save)
        except HTTPException:
            continue


def _worktree_despawn_blocker(agent_id: str, meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    current = dict(meta or _read_meta(agent_id))
    state = current.get("worktree") if isinstance(current.get("worktree"), dict) else {}
    if not state.get("enabled"):
        return None
    refreshed = _refresh_agent_worktree(agent_id)
    if not refreshed.get("pending_changes"):
        return None
    if str(refreshed.get("apply_status") or "") in {"applied", "nothing_to_apply", "discarded", "cleaned"}:
        return None
    return {
        "agent_id": agent_id,
        "changed_files": list(refreshed.get("changed_files") or []),
        "change_count": int(refreshed.get("change_count") or 0),
        "worktree_path": refreshed.get("path"),
        "reason": "unapplied_worktree_changes",
    }


def _worktree_error_http(exc: AgentWorktreeError, *, status_code: int = status.HTTP_409_CONFLICT) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc), **dict(exc.details or {})}
    return HTTPException(status_code, detail)


def _source_scope_from_meta(meta: Dict[str, Any]) -> ResourceScope:
    raw = meta.get("source_scope") if isinstance(meta.get("source_scope"), dict) else meta.get("scope")
    return ResourceScope.from_dict(raw)


def _source_cwd_from_meta(meta: Dict[str, Any]) -> str:
    state = meta.get("worktree") if isinstance(meta.get("worktree"), dict) else {}
    return str(state.get("source_cwd") or meta.get("source_cwd") or meta.get("cwd") or "")


def _resolve_cwd(cwd: Optional[str]) -> Path:
    workdir = Path(cwd).expanduser().resolve() if cwd else Path.home().resolve()
    if not workdir.exists() or not workdir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {workdir}")
    return workdir


def _handoff_instruction(result_style: str) -> str:
    if result_style == "detailed":
        return (
            "When the work is finished, give the parent AI a clean handoff. Do not narrate routine tool/file steps. "
            "Include verified findings/results, important evidence, blockers or caveats, and the next useful action. "
            "Keep it focused; do not dump raw logs unless they are necessary."
        )
    return (
        "When the work is finished, give the parent AI a concise handoff only. Do not narrate routine tool/file steps "
        "or your thinking process. Include only verified findings/results, material numbers or changes, important caveats, "
        "and the next useful action. Aim for roughly 250 words or less unless the task itself requires more."
    )


def _public_meta(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    current = _now()
    now = float(meta.get("ended_at") or current)
    started = float(meta.get("started_at") or now)
    last_activity = float(meta.get("last_activity_at") or started)
    first_event = meta.get("first_event_at")
    spawn_requested = float(meta.get("spawn_requested_at") or started)
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else None
    provider = str(meta.get("provider") or "opencode").lower()
    access_mode = str(meta.get("access_mode") or "workspace_write")
    access_info = _access_mode_info(provider, access_mode)
    public = {
        "agent_id": agent_id,
        "team_id": meta.get("team_id"),
        "team_task_id": meta.get("team_task_id"),
        "status": meta.get("status"),
        "phase": meta.get("phase"),
        "title": meta.get("title"),
        "role": meta.get("role"),
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "reasoning": meta.get("reasoning"),
        "project": meta.get("project"),
        "cwd": meta.get("cwd"),
        "source_cwd": meta.get("source_cwd"),
        "git_isolation": meta.get("git_isolation", "off"),
        "worktree": _worktree_public(meta.get("worktree")),
        "admission_lease_id": meta.get("admission_lease_id"),
        "admission_resources": list(meta.get("admission_resources") or []),
        "access_mode": meta.get("access_mode"),
        "permission_profile": meta.get("permission_profile"),
        "capability_profile": meta.get("capability_profile"),
        "provenance_class": meta.get("provenance_class"),
        "injected_lesson_ids": list(meta.get("injected_lesson_ids") or []),
        "lesson_context_chars": int(meta.get("lesson_context_chars") or 0),
        "lesson_candidate_ids": list(meta.get("lesson_candidate_ids") or []),
        "scope": meta.get("scope"),
        "scoped_mcp": bool(meta.get("scoped_mcp")),
        "access_mode_enforced": access_info["enforced"],
        "process_boundary": access_info.get("boundary"),
        "access_mode_note": access_info["note"],
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "duration_ms": int(max(0.0, now - started) * 1000),
        "spawn_requested_at": meta.get("spawn_requested_at"),
        "worker_started_at": meta.get("worker_started_at"),
        "provider_started_at": meta.get("provider_started_at"),
        "first_event_at": first_event,
        "first_tool_at": meta.get("first_tool_at"),
        "last_activity_at": meta.get("last_activity_at"),
        "idle_seconds": round(max(0.0, current - last_activity), 3) if meta.get("status") not in TERMINAL_STATUSES else 0.0,
        "first_event_latency_ms": int(max(0.0, float(first_event) - spawn_requested) * 1000) if first_event else None,
        "step_count": int(meta.get("step_count") or 0),
        "tool_call_count": int(meta.get("tool_call_count") or 0),
        "last_tool": meta.get("last_tool"),
        "last_tool_duration_ms": meta.get("last_tool_duration_ms"),
        "turn_count": int(meta.get("turn_count") or 0),
        "turn_elapsed_ms": int(max(0.0, current - float(meta.get("turn_started_at") or current)) * 1000) if meta.get("turn_started_at") else None,
        "turn_budget_s": meta.get("turn_budget_s"),
        "hard_tool_budget_s": meta.get("hard_tool_budget_s"),
        "checkpoint_count": int(meta.get("checkpoint_count") or 0),
        "checkpoint_pending": bool(meta.get("checkpoint_pending")),
        "last_checkpoint_at": meta.get("last_checkpoint_at"),
        "throttle_count": int(meta.get("throttle_count") or 0),
        "last_throttled_at": meta.get("last_throttled_at"),
        "last_throttle_reason": meta.get("last_throttle_reason"),
        "cooldown_until": meta.get("cooldown_until"),
        "last_event_type": meta.get("last_event_type"),
        "idle_timeout_s": meta.get("idle_timeout_s"),
        "retries": int(meta.get("retries") or 0),
        "retry_count": int(meta.get("retry_count") or 0),
        "last_retry_reason": meta.get("last_retry_reason"),
        "last_retry_classification": meta.get("last_retry_classification"),
        "last_retryable": meta.get("last_retryable"),
        "last_retry_delay_s": meta.get("last_retry_delay_s"),
        "retry_blocked_reason": meta.get("retry_blocked_reason"),
        "team_retry_remaining": meta.get("team_retry_remaining"),
        "session_id": meta.get("session_id"),
        "parent_agent_id": meta.get("parent_agent_id"),
        "lineage_root_agent_id": meta.get("lineage_root_agent_id"),
        "lineage_parent_agent_id": meta.get("lineage_parent_agent_id"),
        "attempt": meta.get("attempt", 1),
        "exit_code": meta.get("exit_code"),
        "note": meta.get("note"),
        "usage": usage,
        "output_tokens": usage.get("output") if usage else None,
    }
    public.update(workflow_public_state(agent_id))
    return public


def _normalize(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if meta.get("status") in TERMINAL_STATUSES and meta.get("admission_lease_id"):
        _release_agent_admission(agent_id, meta)
        try:
            meta = _read_meta(agent_id)
        except HTTPException:
            pass
    if meta.get("status") in {"starting", "running"}:
        worker_pid = meta.get("worker_pid")
        if worker_pid and not _is_pid_alive(worker_pid):
            provider_pid = meta.get("provider_pid")
            if provider_pid and _is_pid_alive(provider_pid):
                _kill_group(provider_pid, signal_module.SIGTERM)
            def mark_failed(current: Dict[str, Any]) -> Optional[bool]:
                current_worker_pid = current.get("worker_pid")
                if current.get("status") not in {"starting", "running"}:
                    return False
                if not current_worker_pid or _is_pid_alive(current_worker_pid):
                    return False
                current.update({
                    "status": "failed",
                    "ended_at": _now(),
                    "updated_at": _now(),
                    "note": "Agent worker exited before recording a terminal result.",
                })
                return True

            meta = _update_meta(agent_id, mark_failed)
            if meta.get("status") == "failed":
                browser_tabs.release_agent_leases(agent_id)
                _release_agent_admission(agent_id, meta)
                try:
                    workflow_mark_terminal(agent_id, "failed")
                except WorkflowCheckpointError:
                    pass
                try:
                    _refresh_agent_worktree(agent_id)
                    meta = _read_meta(agent_id)
                except Exception:
                    pass
    return meta


def _spawn_worker_process(agent_id: str, worker_log) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_server.tools_agents", "--worker", agent_id],
        cwd=str(BASE_DIR.parent),
        env=_base_env(),
        stdin=subprocess.DEVNULL,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )


def _spawn_internal(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str],
    reasoning: Optional[str],
    cwd: Optional[str],
    timeout_s: Optional[int],
    title: Optional[str],
    result_style: str,
    access_mode: str,
    scope: ResourceScope,
    permission_profile: str,
    capability_profile: str = "legacy",
    parent_agent_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    attempt: int = 1,
    team_id: Optional[str] = None,
    team_task_id: Optional[str] = None,
    idle_timeout_s: Optional[int] = None,
    retries: int = 0,
    project: Optional[str] = None,
    role: Optional[str] = None,
    provenance_class: str = "local",
    workflow_id: Optional[str] = None,
    workflow_input_hash_value: Optional[str] = None,
    resume_generation: int = 0,
    resume_token: Optional[str] = None,
    resume_parent_agent_id: Optional[str] = None,
    git_isolation: str = "auto",
    git_base_commit: Optional[str] = None,
    reuse_worktree_agent_id: Optional[str] = None,
    seed_worktree_agent_ids: Optional[List[str]] = None,
    admission_lease_id: Optional[str] = None,
    admission_resources: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    provider = provider.lower().strip()
    clean_role = str(role or "").strip().lower() or None
    if clean_role and clean_role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    provenance_class = str(provenance_class or "local").strip().lower() or "local"
    if provider not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if not provider_enabled(provider):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"provider_disabled: {provider} is disabled in Mac MCP Settings > Subagents.")
    if provider == "chatgpt":
        if reasoning is None:
            reasoning = str(provider_setting("chatgpt", "default_reasoning", "high") or "high").strip() or "high"
        if reasoning and str(reasoning).strip().lower() != "none" and not _chatgpt_effort(reasoning):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "chatgpt reasoning must be low, medium, high, or extra-high.")
        project = str(project or _chatgpt_default_project() or "").strip() or None
    elif project:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project is only supported by provider=chatgpt.")
    binary = _find_binary(provider)
    if not binary:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{provider} CLI is not installed or not executable.")
    if not prompt or not prompt.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "prompt is required.")
    if result_style not in _RESULT_STYLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"result_style must be one of: {', '.join(sorted(_RESULT_STYLES))}")
    if access_mode not in _ACCESS_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"access_mode must be one of: {', '.join(sorted(_ACCESS_MODES))}")
    _validate_provider_access_mode(provider, access_mode)
    git_isolation = str(git_isolation or "auto").strip().lower()
    if git_isolation not in GIT_ISOLATION_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"git_isolation must be one of: {', '.join(sorted(GIT_ISOLATION_MODES))}")

    source_workdir = _resolve_cwd(cwd)
    source_scope = scope
    workdir = source_workdir
    effective_timeout = min(max(10, int(timeout_s or DEFAULT_AGENT_TIMEOUT_S)), MAX_AGENT_TIMEOUT_S)
    effective_idle_timeout = None if idle_timeout_s is None else min(max(5, int(idle_timeout_s)), 3600)
    effective_retries = min(max(0, int(retries)), 3)
    agent_id = "agt_" + uuid.uuid4().hex[:10]
    agent_lineage = _new_agent_lineage(agent_id, parent_agent_id)
    worktree_created = False
    try:
        if reuse_worktree_agent_id:
            reuse_meta = _read_meta(str(reuse_worktree_agent_id))
            reuse_state = reuse_meta.get("worktree") if isinstance(reuse_meta.get("worktree"), dict) else {}
            if not reuse_state.get("enabled"):
                raise AgentWorktreeError("git_worktree_reuse_unavailable", "The parent agent has no isolated worktree to reuse.")
            worktree_state = reuse_worktree(reuse_state)
            worktree_state["shared_from_agent_id"] = str(reuse_worktree_agent_id)
            workdir = Path(str(worktree_state["cwd"])).resolve(strict=False)
            scope = ResourceScope.from_dict(reuse_meta.get("scope"))
            source_scope = _source_scope_from_meta(reuse_meta)
            source_workdir = Path(_source_cwd_from_meta(reuse_meta)).resolve(strict=False)
        else:
            worktree_state = prepare_worktree(
                agent_id=agent_id, cwd=source_workdir, path_roots=source_scope.path_roots,
                mode=git_isolation, access_mode=access_mode, base_commit=git_base_commit,
            )
            if worktree_state.get("enabled"):
                worktree_created = True
                workdir = Path(str(worktree_state["cwd"])).resolve(strict=False)
                scope_data = source_scope.to_dict()
                scope_data["path_roots"] = remapped_roots(worktree_state)
                scope = ResourceScope.from_dict(scope_data)
                seed_states: List[Dict[str, Any]] = []
                for seed_agent_id in seed_worktree_agent_ids or []:
                    seed_meta = _read_meta(str(seed_agent_id))
                    seed_state = seed_meta.get("worktree") if isinstance(seed_meta.get("worktree"), dict) else {}
                    if seed_state.get("enabled"):
                        seed_states.append(seed_state)
                if seed_states:
                    worktree_state = seed_worktree(worktree_state, seed_states)
    except AgentWorktreeError as exc:
        if worktree_created:
            try:
                cleanup_worktree(worktree_state, force=True)
            except Exception:
                pass
        raise _worktree_error_http(exc, status_code=status.HTTP_400_BAD_REQUEST if exc.code.startswith("invalid_") else status.HTTP_409_CONFLICT) from exc
    path = _agent_dir(agent_id)
    try:
        path.mkdir(parents=True, exist_ok=False)
    except Exception:
        if worktree_created:
            try:
                cleanup_worktree(worktree_state, force=True)
            except Exception:
                pass
        raise
    user_prompt = prompt.strip()
    workflow_hash = str(workflow_input_hash_value or "").strip() or workflow_input_hash(
        prompt=user_prompt, provider=provider, cwd=str(source_workdir), access_mode=access_mode,
        scope=source_scope.to_dict(), role=clean_role,
    )
    workflow_id_value = str(workflow_id or "").strip() or ("wf_" + uuid.uuid4().hex[:16])
    access_instruction = (
        "This task is read-only. Do not modify files, configuration, services, repositories, or external state. "
        "Use only inspection/read commands and tools."
        if access_mode == "read_only" else ""
    )
    scope_instruction = _scope_prompt(scope, permission_profile, provider)
    injected_lesson_ids: List[str] = []
    lesson_context_chars = 0
    role_learning_instruction = ""
    if clean_role:
        learned_context = {"lesson_ids": [], "text": "", "chars": 0}
        if provenance_class == "local":
            learned_context = lesson_context(clean_role, user_prompt)
        injected_lesson_ids = list(learned_context.get("lesson_ids") or [])
        lesson_context_chars = int(learned_context.get("chars") or 0)
        pieces = [str(learned_context.get("text") or "").strip(), lesson_candidate_instruction(clean_role)]
        role_learning_instruction = "\n\n".join(piece for piece in pieces if piece)
    effective_prompt = (
        user_prompt
        + ("\n\n" + access_instruction if access_instruction else "")
        + "\n\n" + scope_instruction
        + ("\n\n" + role_learning_instruction if role_learning_instruction else "")
        + "\n\n" + _handoff_instruction(result_style)
    )
    (path / "prompt.txt").write_text(user_prompt, encoding="utf-8")
    (path / "effective_prompt.txt").write_text(effective_prompt, encoding="utf-8")
    (path / "stdout.log").touch()
    (path / "stderr.log").touch()
    (path / "worker.log").touch()
    (path / "result.txt").touch()

    started = _now()
    chatgpt_turn_budget_s, chatgpt_hard_tool_budget_s = _chatgpt_budget_config() if provider == "chatgpt" else (None, None)
    meta: Dict[str, Any] = {
        "agent_id": agent_id,
        "team_id": team_id,
        "team_task_id": team_task_id,
        "title": (title or user_prompt.splitlines()[0][:100]).strip(),
        "role": clean_role,
        "provider": provider,
        "binary": binary,
        "model": model,
        "reasoning": reasoning,
        "project": project,
        "cwd": str(workdir),
        "source_cwd": str(source_workdir),
        "source_scope": source_scope.to_dict(),
        "git_isolation": git_isolation,
        "worktree": worktree_state,
        "admission_lease_id": str(admission_lease_id or "").strip() or None,
        "admission_resources": [dict(item) for item in (admission_resources or [])],
        "access_mode": access_mode,
        "permission_profile": permission_profile,
        "capability_profile": capability_profile,
        "provenance_class": provenance_class,
        "injected_lesson_ids": injected_lesson_ids,
        "lesson_context_chars": lesson_context_chars,
        "lesson_candidate_ids": [],
        "scope": scope.to_dict(),
        "scoped_mcp": provider in {"opencode", "codex"},
        "mcp_endpoint": os.getenv("MAC_MCP_AGENT_ENDPOINT", "http://127.0.0.1:8765/mcp"),
        "result_style": result_style,
        "timeout_s": effective_timeout,
        "idle_timeout_s": effective_idle_timeout,
        "retries": effective_retries,
        "retry_count": 0,
        "last_retry_reason": None,
        "last_retry_classification": None,
        "last_retryable": None,
        "last_retry_delay_s": None,
        "retry_blocked_reason": None,
        "team_retry_remaining": None,
        "status": "starting",
        "phase": "starting",
        "worker_pid": None,
        "provider_pid": None,
        "session_id": None,
        "resume_session_id": resume_session_id,
        "parent_agent_id": parent_agent_id,
        **agent_lineage,
        "attempt": attempt,
        "workflow_id": workflow_id_value,
        "workflow_input_hash": workflow_hash,
        "resume_generation": int(resume_generation or 0),
        "exit_code": None,
        "started_at": started,
        "spawn_requested_at": started,
        "worker_started_at": None,
        "provider_started_at": None,
        "first_event_at": None,
        "first_tool_at": None,
        "last_activity_at": started,
        "last_event_type": None,
        "step_count": 0,
        "tool_call_count": 0,
        "last_tool": None,
        "last_tool_duration_ms": None,
        "provider_job_id": None,
        "turn_count": 0,
        "turn_started_at": None,
        "turn_budget_s": chatgpt_turn_budget_s,
        "hard_tool_budget_s": chatgpt_hard_tool_budget_s,
        "active_tool_started_at": None,
        "checkpoint_count": 0,
        "checkpoint_pending": False,
        "checkpoint_waiting_for_tool": False,
        "checkpoint_failures": 0,
        "last_checkpoint_at": None,
        "last_checkpoint_reason": None,
        "throttle_count": 0,
        "last_throttled_at": None,
        "last_throttle_reason": None,
        "cooldown_until": None,
        "updated_at": started,
        "ended_at": None,
    }
    try:
        if admission_lease_id:
            admission_bind_agent(AGENTS_DIR, str(admission_lease_id), agent_id)
        _write_meta(agent_id, meta)
    except AdmissionError as exc:
        try:
            admission_release(AGENTS_DIR, lease_id=str(admission_lease_id or ""))
        except Exception:
            pass
        shutil.rmtree(path, ignore_errors=True)
        if worktree_created:
            try:
                cleanup_worktree(worktree_state, force=True)
            except Exception:
                pass
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": exc.code, "message": str(exc), **dict(exc.details or {})},
        ) from exc
    except Exception:
        if admission_lease_id:
            try:
                admission_release(AGENTS_DIR, lease_id=str(admission_lease_id))
            except Exception:
                pass
        shutil.rmtree(path, ignore_errors=True)
        if worktree_created:
            try:
                cleanup_worktree(worktree_state, force=True)
            except Exception:
                pass
        raise
    try:
        if resume_token:
            if not resume_parent_agent_id or not resume_session_id:
                raise CheckpointConflictError("resume binding requires parent agent and provider session")
            bind_resumed_agent(
                workflow_id=workflow_id_value, parent_agent_id=resume_parent_agent_id, agent_id=agent_id,
                input_hash=workflow_hash, resume_generation=int(resume_generation or 0),
                resume_token=resume_token, session_id=str(resume_session_id),
            )
        else:
            create_workflow(
                agent_id=agent_id, input_hash=workflow_hash, provider=provider, workflow_id=workflow_id_value,
            )
    except WorkflowCheckpointError as exc:
        if admission_lease_id:
            try:
                admission_release(AGENTS_DIR, lease_id=str(admission_lease_id))
            except Exception:
                pass
        shutil.rmtree(path, ignore_errors=True)
        if worktree_created:
            try:
                cleanup_worktree(worktree_state, force=True)
            except Exception:
                pass
        status_code = status.HTTP_409_CONFLICT if isinstance(exc, (CheckpointConflictError, CheckpointUnknownError)) else status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code, f"{exc.code}: {exc}") from exc

    worker_log = (path / "worker.log").open("a", encoding="utf-8")
    try:
        proc = _spawn_worker_process(agent_id, worker_log)
    except OSError as exc:
        worker_log.close()
        spawn_error = str(exc)
        def mark_spawn_failed(current: Dict[str, Any]) -> None:
            current.update({"status": "failed", "ended_at": _now(), "updated_at": _now(), "note": spawn_error})

        _update_meta(agent_id, mark_spawn_failed)
        if admission_lease_id:
            try:
                admission_release(AGENTS_DIR, lease_id=str(admission_lease_id))
            except Exception:
                pass
        if worktree_created:
            try:
                cleaned = cleanup_worktree(worktree_state, force=True)
                _update_meta(agent_id, lambda current: current.update({"worktree": cleaned, "updated_at": _now()}))
            except Exception:
                pass
        try:
            if resume_token and resume_parent_agent_id:
                rollback_resumed_agent(
                    workflow_id_value, parent_agent_id=resume_parent_agent_id, agent_id=agent_id,
                    reason="agent_worker_spawn_failed",
                )
            else:
                workflow_mark_terminal(agent_id, "failed")
        except WorkflowCheckpointError:
            pass
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start agent worker: {exc}") from exc
    worker_log.close()
    with _WORKERS_LOCK:
        _WORKERS[agent_id] = proc
    threading.Thread(target=_reap_worker, args=(agent_id, proc), daemon=True).start()
    def record_worker(current: Dict[str, Any]) -> None:
        current["worker_pid"] = proc.pid
        if current.get("status") == "starting":
            current["status"] = "running"
        current["updated_at"] = _now()

    meta = _update_meta(agent_id, record_worker)
    return {"ok": True, **_public_meta(agent_id, meta)}


def spawn_agent(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    title: Optional[str] = None,
    result_style: str = "concise",
    access_mode: str = "workspace_write",
    idle_timeout_s: Optional[int] = None,
    retries: int = 0,
    scope: Optional[Dict[str, Any]] = None,
    parent_scope: Optional[ResourceScope] = None,
    parent_profile: str = "trusted",
    capability_profile: Optional[str] = None,
    project: Optional[str] = None,
    role: Optional[str] = None,
    provenance_class: str = "local",
    git_isolation: str = "auto",
) -> Dict[str, Any]:
    workdir = _resolve_cwd(cwd)
    effective_scope, permission_profile, effective_capability_profile = _requested_agent_scope(
        workdir, access_mode, scope, parent_scope, parent_profile, capability_profile
    )
    context = current_policy_context()
    return _spawn_internal(
        settings, provider, prompt, model, reasoning, str(workdir), timeout_s, title,
        result_style, effective_scope.access_mode.value, effective_scope, permission_profile,
        capability_profile=effective_capability_profile, parent_agent_id=context.agent_id,
        idle_timeout_s=idle_timeout_s, retries=retries, project=project,
        role=role, provenance_class=provenance_class, git_isolation=git_isolation,
    )


def spawn_agents(
    settings: Settings,
    tasks: List[Dict[str, Any]],
    provider: str,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    idle_timeout_s: Optional[int] = None,
    retries: int = 1,
    result_style: str = "concise",
    access_mode: str = "read_only",
    title: Optional[str] = None,
    parent_team_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    parent_scope: Optional[ResourceScope] = None,
    parent_profile: str = "trusted",
    capability_profile: Optional[str] = None,
    project: Optional[str] = None,
    role: Optional[str] = None,
    provenance_class: str = "local",
    max_parallel: Optional[int] = None,
    max_revisions: int = 1,
    team_timeout_s: Optional[int] = None,
    max_team_retries: Optional[int] = None,
    max_total_tool_calls: Optional[int] = None,
    max_total_tokens: Optional[int] = None,
    git_isolation: str = "auto",
) -> Dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if not provider_enabled(provider):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"provider_disabled: {provider} is disabled in Mac MCP Settings > Subagents.")
    if provider != "chatgpt" and project:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project is only supported by provider=chatgpt.")
    git_isolation = str(git_isolation or "auto").strip().lower()
    if git_isolation not in GIT_ISOLATION_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"git_isolation must be one of: {', '.join(sorted(GIT_ISOLATION_MODES))}")
    team_role = str(role or "").strip().lower() or None
    if team_role and team_role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    if not tasks or not isinstance(tasks, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tasks is required.")
    if len(tasks) > MAX_TEAM_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A team can contain at most {MAX_TEAM_SIZE} tasks.")
    effective_max_parallel = len(tasks) if max_parallel is None else int(max_parallel)
    if effective_max_parallel < 1 or effective_max_parallel > MAX_TEAM_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"max_parallel must be between 1 and {MAX_TEAM_SIZE}.")
    effective_max_parallel = min(effective_max_parallel, len(tasks))
    effective_max_revisions = int(max_revisions)
    if effective_max_revisions < 0 or effective_max_revisions > 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "max_revisions must be between 0 and 3.")
    effective_child_timeout = min(max(10, int(timeout_s or DEFAULT_AGENT_TIMEOUT_S)), MAX_AGENT_TIMEOUT_S)
    waves = max(1, (len(tasks) + effective_max_parallel - 1) // effective_max_parallel)
    default_team_timeout = min(
        MAX_TEAM_TIMEOUT_S,
        max(DEFAULT_TEAM_TIMEOUT_FLOOR_S, effective_child_timeout * waves * max(1, 1 + effective_max_revisions)),
    )
    effective_team_timeout = default_team_timeout if team_timeout_s is None else int(team_timeout_s)
    if effective_team_timeout < 60 or effective_team_timeout > MAX_TEAM_TIMEOUT_S:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"team_timeout_s must be between 60 and {MAX_TEAM_TIMEOUT_S} seconds.",
        )
    child_retries = min(max(0, int(retries)), 3)
    effective_team_retries = min(MAX_TEAM_RETRY_BUDGET, child_retries * len(tasks)) if max_team_retries is None else int(max_team_retries)
    if effective_team_retries < 0 or effective_team_retries > MAX_TEAM_RETRY_BUDGET:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"max_team_retries must be between 0 and {MAX_TEAM_RETRY_BUDGET}.",
        )
    effective_tool_budget = None if max_total_tool_calls is None else int(max_total_tool_calls)
    if effective_tool_budget is not None and (effective_tool_budget < 1 or effective_tool_budget > MAX_TEAM_TOOL_BUDGET):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"max_total_tool_calls must be between 1 and {MAX_TEAM_TOOL_BUDGET} when provided.",
        )
    effective_token_budget = None if max_total_tokens is None else int(max_total_tokens)
    if effective_token_budget is not None and (effective_token_budget < 1 or effective_token_budget > MAX_TEAM_TOKEN_BUDGET):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"max_total_tokens must be between 1 and {MAX_TEAM_TOKEN_BUDGET} when provided.",
        )
    if provider == "chatgpt" and effective_token_budget is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "max_total_tokens is unavailable for provider=chatgpt because ChatGPT Web does not expose reliable token usage.",
        )

    forbidden = {"provider", "model", "reasoning", "access_mode", "result_style"}
    normalized: List[Dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}] must be an object.")
        mixed = sorted(forbidden.intersection(task.keys()))
        if mixed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Team child settings {mixed} must be supplied once at team level so every agent uses the same configuration.",
            )
        prompt = str(task.get("prompt") or task.get("task") or "").strip()
        if not prompt:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].prompt is required.")
        task_id = str(task.get("id") or task.get("task_id") or f"task_{index}").strip()
        if not _TASK_ID_RE.fullmatch(task_id):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"tasks[{index - 1}].id must match {_TASK_ID_RE.pattern!r}.",
            )
        deps_raw = task.get("depends_on") or []
        if isinstance(deps_raw, str):
            deps_raw = [deps_raw]
        if not isinstance(deps_raw, list) or any(not isinstance(dep, str) for dep in deps_raw):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].depends_on must be a list of task ids.")
        depends_on = [str(dep).strip() for dep in deps_raw if str(dep).strip()]
        if len(depends_on) != len(set(depends_on)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].depends_on contains duplicates.")
        review_of = str(task.get("review_of") or "").strip() or None
        if provider != "chatgpt" and task.get("project"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].project is only supported by provider=chatgpt.")
        child_scope_raw = task.get("scope")
        if child_scope_raw is not None and not isinstance(child_scope_raw, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].scope must be an object.")
        resources_raw = task.get("resources") or []
        if not isinstance(resources_raw, list) or any(not isinstance(item, dict) for item in resources_raw):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].resources must be a list of resource claim objects.")
        child_role = str(task.get("role") or team_role or "").strip().lower() or None
        if review_of and child_role is None:
            child_role = "reviewer"
        if review_of and child_role != "reviewer":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}] uses review_of and must have role=reviewer.")
        if child_role and child_role not in VALID_ROLES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"tasks[{index - 1}].role must be one of: {', '.join(sorted(VALID_ROLES))}.",
            )
        task_max_revisions = int(task.get("max_revisions", effective_max_revisions))
        if task_max_revisions < 0 or task_max_revisions > 3:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].max_revisions must be between 0 and 3.")
        normalized.append({
            "id": task_id,
            "prompt": prompt,
            "title": str(task.get("title") or f"Agent {index}").strip(),
            "scope": child_scope_raw,
            "project": str(task.get("project") or project or "").strip() or None,
            "role": child_role,
            "depends_on": depends_on,
            "review_of": review_of,
            "max_revisions": task_max_revisions,
            "resources": [dict(item) for item in resources_raw],
        })

    _validate_team_graph(normalized)
    # Preserve validation precedence: malformed team/task input must fail with 4xx
    # even on hosts where the selected provider binary is not installed (e.g. CI).
    if not _find_binary(provider):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{provider} CLI is not installed or not executable.")
    workdir = _resolve_cwd(cwd)
    team_scope, team_profile, effective_capability_profile = _requested_agent_scope(
        workdir, access_mode, scope, parent_scope, parent_profile, capability_profile
    )
    access_mode = team_scope.access_mode.value
    _validate_provider_access_mode(provider, access_mode)

    persisted_tasks: List[Dict[str, Any]] = []
    for task in normalized:
        if task.get("scope") is None:
            effective_scope = team_scope
        else:
            child_data = dict(task["scope"])
            if "access_mode" in child_data:
                try:
                    child_mode = normalize_access_mode(child_data["access_mode"])
                except ValueError as exc:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
                if child_mode.value != access_mode:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "task.scope.access_mode must match the team access_mode.")
            child_data["access_mode"] = access_mode
            if "path_roots" not in child_data and access_mode != "full":
                child_data["path_roots"] = [str(workdir)]
            try:
                requested_child = ResourceScope.from_dict(child_data)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid task scope: {exc}") from exc
            if not scope_contains(team_scope, requested_child):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_denied: task scope cannot widen the team scope.")
            effective_scope = child_scope(team_scope, requested_child)
        resource_claims = _normalize_explicit_resource_claims(
            task.get("resources"), workdir=workdir, scope=effective_scope,
        )
        persisted_tasks.append({
            **task,
            "resources": None,
            "resource_claims": resource_claims,
            "scope": effective_scope.to_dict(),
            "state": "blocked",
            "agent_ids": [],
            "active_agent_id": None,
            "latest_agent_id": None,
            "revision_count": 0,
            "revision_feedback": None,
            "gate_attempts": 0,
            "gate_result": None,
            "gate_feedback": None,
            "failure_reason": None,
            "admission_request_id": None,
            "admission_lease_id": None,
            "queued_since": None,
            "queued_reason": None,
            "queued_details": None,
            "queue_position": None,
        })

    try:
        team_git_base = resolve_git_base(workdir, mode=git_isolation, access_mode=access_mode)
    except AgentWorktreeError as exc:
        raise _worktree_error_http(exc, status_code=status.HTTP_409_CONFLICT) from exc
    team_id = "team_" + uuid.uuid4().hex[:10]
    created = _now()
    team_lineage = _team_lineage_for_spawn(parent_team_id)
    team_meta: Dict[str, Any] = {
        "team_id": team_id,
        "title": (title or f"{provider} team ({len(persisted_tasks)} tasks)").strip(),
        "role": team_role,
        "provenance_class": str(provenance_class or "local").strip().lower() or "local",
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "project": (str(project or _chatgpt_default_project() or "").strip() or None) if provider == "chatgpt" else None,
        "cwd": str(workdir),
        "git_isolation": git_isolation,
        "git_base_commit": team_git_base,
        "timeout_s": timeout_s,
        "idle_timeout_s": idle_timeout_s,
        "retries": child_retries,
        "result_style": result_style,
        "access_mode": access_mode,
        "permission_profile": team_profile,
        "capability_profile": effective_capability_profile,
        "scope": team_scope.to_dict(),
        "created_at": created,
        "updated_at": created,
        "parent_team_id": parent_team_id,
        **team_lineage,
        "agent_ids": [],
        "scheduler_version": 1,
        "max_parallel": effective_max_parallel,
        "max_revisions": effective_max_revisions,
        "team_timeout_s": effective_team_timeout,
        "deadline_at": created + effective_team_timeout,
        "max_team_retries": effective_team_retries,
        "team_retry_count": 0,
        "next_retry_at": None,
        "max_total_tool_calls": effective_tool_budget,
        "max_total_tokens": effective_token_budget,
        "budget_exhausted_reason": None,
        "cancelled": False,
        "tasks": persisted_tasks,
    }
    _write_team(team_id, team_meta)
    team_meta = _team_tick(team_id)
    summary = _team_summary(team_id, team_meta)
    spawned: List[Dict[str, Any]] = []
    for agent_id in list(team_meta.get("agent_ids") or []):
        try:
            child = _public_meta(agent_id, _read_meta(agent_id))
        except HTTPException:
            continue
        spawned.append({
            "agent_id": agent_id,
            "task_id": child.get("team_task_id"),
            "title": child.get("title"),
            "status": child.get("status"),
        })
    summary["spawned"] = spawned
    return {"ok": True, **summary}

def list_agents(settings: Settings, status_filter: Optional[str] = None, limit: int = 20,
                team_id: Optional[str] = None) -> Dict[str, Any]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    if team_id:
        _authorize_team_control(str(team_id), "list_agents")
    delegated = current_policy_context().agent_id is not None
    items: List[Dict[str, Any]] = []
    for path in sorted(AGENTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        raw_meta = _read_meta(path.name)
        if delegated:
            try:
                raw_meta = _authorize_agent_control(path.name, "list_agents", meta=raw_meta)
            except HTTPException as exc:
                if exc.status_code == status.HTTP_403_FORBIDDEN:
                    continue
                raise
        else:
            raw_meta = _persist_agent_lineage_if_missing(path.name, raw_meta)
        meta = _normalize(path.name, raw_meta)
        if status_filter and meta.get("status") != status_filter:
            continue
        if team_id and meta.get("team_id") != team_id:
            continue
        public = _public_meta(path.name, meta)
        result_path = path / "result.txt"
        if meta.get("status") == "completed" and result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()
            public["result_preview"] = result[:300]
        items.append(public)
        if len(items) >= max(1, min(int(limit), 200)):
            break
    result: Dict[str, Any] = {"ok": True, "count": len(items), "agents": items}
    try:
        snap = admission_snapshot(AGENTS_DIR)
        result["global_admission"] = {
            "global_active": snap.get("global_active"), "global_limit": snap.get("global_limit"),
            "provider_active": snap.get("provider_active"), "provider_limits": snap.get("provider_limits"),
            "queued_count": snap.get("queued_count"),
        }
    except Exception:
        result["global_admission"] = None
    if team_id:
        result["team"] = _team_summary(team_id)
    return result


def _wait_success_threshold(total: int, mode: str) -> int:
    if total <= 0:
        return 0
    if mode == "any":
        return 1
    if mode == "majority":
        return total // 2 + 1
    return total


def _wait_condition(successful_count: int, terminal_count: int, total: int, mode: str) -> bool:
    if total <= 0:
        return False
    if mode == "all":
        # Keep the historical completion meaning for all: the wait is satisfied
        # once every unit is terminal, while success/outcome report whether the
        # completed work actually succeeded.
        return terminal_count >= total
    return successful_count >= _wait_success_threshold(total, mode)


def _wait_quorum_possible(successful_count: int, pending_count: int, total: int, mode: str) -> bool:
    if total <= 0:
        return False
    if mode == "all":
        return True
    return successful_count + pending_count >= _wait_success_threshold(total, mode)


def _agent_wait_snapshot(states: List[Dict[str, Any]]) -> Dict[str, Any]:
    successful = sum(1 for item in states if item.get("status") == "completed")
    failures = [item for item in states if item.get("status") in TERMINAL_STATUSES and item.get("status") != "completed"]
    failed = len(failures)
    pending = max(0, len(states) - successful - failed)
    cancelled_failures = sum(1 for item in failures if item.get("status") == "cancelled")
    outcome = _aggregate_work_outcome(
        successful, failed, pending, cancelled_failure_count=cancelled_failures,
    )
    outcome["terminal_count"] = successful + failed
    outcome["failure_reasons"] = [
        {
            "agent_id": item.get("agent_id"),
            "status": item.get("status"),
            "reason": str(item.get("status") or "failed"),
        }
        for item in failures
    ]
    return outcome


def _wait_snapshot(states: List[Dict[str, Any]], team_summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if team_summary and int(team_summary.get("scheduler_version") or 0) >= 1 and int(team_summary.get("task_count") or 0) > 0:
        successful = int(team_summary.get("successful_count") or 0)
        failed = int(team_summary.get("failure_count") or 0)
        pending = int(team_summary.get("pending_count") or 0)
        return {
            "success": bool(team_summary.get("success")),
            "outcome": str(team_summary.get("outcome") or "running"),
            "partial_failure": bool(team_summary.get("partial_failure")),
            "successful_count": successful,
            "failure_count": failed,
            "pending_count": pending,
            "work_count": int(team_summary.get("work_count") or (successful + failed + pending)),
            "terminal_count": successful + failed,
            "failure_reasons": list(team_summary.get("failure_reasons") or []),
        }
    return _agent_wait_snapshot(states)


def wait_agents(
    settings: Settings,
    team_id: Optional[str] = None,
    agent_ids: Optional[List[str]] = None,
    mode: str = "all",
    timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    include_results: bool = True,
) -> Dict[str, Any]:
    mode = mode.lower().strip()
    if mode not in _WAIT_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mode must be all, any, or majority.")
    if bool(team_id) == bool(agent_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of team_id or agent_ids.")
    ids = list(agent_ids or [])
    if team_id:
        team = _authorize_team_control(str(team_id), "wait_agents")
        team = _team_tick(str(team_id))
        ids = list(team.get("agent_ids") or [])
    else:
        for target_agent_id in ids:
            _authorize_agent_control(str(target_agent_id), "wait_agents")
    if not ids and not team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No agents to wait for.")
    bounded_timeout = min(max(0, int(timeout_s)), MAX_WAIT_TIMEOUT_S)
    deadline = time.monotonic() + bounded_timeout
    condition_met = False
    waiter_timed_out = False
    quorum_possible = True
    states: List[Dict[str, Any]] = []
    team_summary: Optional[Dict[str, Any]] = None
    wait_state: Dict[str, Any] = _aggregate_work_outcome(0, 0, 0)
    wait_state.update({"terminal_count": 0, "failure_reasons": []})
    quorum_total = 0
    while True:
        if team_id:
            team_summary = _team_summary(str(team_id))
            ids = list(team_summary.get("agent_ids") or [])
        states = [get_agent(settings, agent_id, include_logs=False) for agent_id in ids]
        wait_state = _wait_snapshot(states, team_summary)
        quorum_total = int(wait_state.get("work_count") or 0)
        quorum_terminal_count = int(wait_state.get("terminal_count") or 0)
        successful_count = int(wait_state.get("successful_count") or 0)
        pending_count = int(wait_state.get("pending_count") or 0)
        condition_met = _wait_condition(successful_count, quorum_terminal_count, quorum_total, mode)
        quorum_possible = _wait_quorum_possible(successful_count, pending_count, quorum_total, mode)
        if condition_met or not quorum_possible:
            break
        if time.monotonic() >= deadline:
            waiter_timed_out = True
            break
        time.sleep(0.25)
    compact: List[Dict[str, Any]] = []
    for item in states:
        row = {
            "agent_id": item.get("agent_id"),
            "team_task_id": item.get("team_task_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "phase": item.get("phase"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "role": item.get("role"),
            "injected_lesson_ids": list(item.get("injected_lesson_ids") or []),
            "lesson_candidate_ids": list(item.get("lesson_candidate_ids") or []),
            "duration_ms": item.get("duration_ms"),
            "first_event_latency_ms": item.get("first_event_latency_ms"),
            "idle_seconds": item.get("idle_seconds"),
            "tool_call_count": item.get("tool_call_count"),
            "last_tool": item.get("last_tool"),
            "retry_count": item.get("retry_count"),
            "last_retry_reason": item.get("last_retry_reason"),
            "last_retry_classification": item.get("last_retry_classification"),
            "last_retryable": item.get("last_retryable"),
            "retry_blocked_reason": item.get("retry_blocked_reason"),
            "team_retry_remaining": item.get("team_retry_remaining"),
            "workflow_id": item.get("workflow_id"),
            "resume_generation": item.get("resume_generation"),
            "checkpoint_state": item.get("checkpoint_state"),
            "checkpoint_safety": item.get("checkpoint_safety"),
            "checkpoint_reason": item.get("checkpoint_reason"),
            "side_effect_receipt_count": item.get("side_effect_receipt_count"),
            "pending_side_effect_count": item.get("pending_side_effect_count"),
            "checkpoint_cursor": item.get("checkpoint_cursor"),
            "last_durable_checkpoint_at": item.get("last_durable_checkpoint_at"),
            "resumable": item.get("resumable"),
            "failure_reason": (
                str(item.get("status"))
                if item.get("status") in TERMINAL_STATUSES and item.get("status") != "completed"
                else None
            ),
        }
        if include_results and "result" in item:
            row["result"] = truncate(str(item.get("result") or ""), TEAM_RESULT_LIMIT)[0]
        compact.append(row)
    successful_count = int(wait_state.get("successful_count") or 0)
    failure_count = int(wait_state.get("failure_count") or 0)
    pending_count = int(wait_state.get("pending_count") or 0)
    quorum_terminal_count = int(wait_state.get("terminal_count") or 0)
    required_successes = _wait_success_threshold(quorum_total, mode)
    wait_success = bool(condition_met and (mode != "all" or successful_count >= quorum_total))
    response: Dict[str, Any] = {
        "ok": True,
        "team_id": team_id,
        "mode": mode,
        "condition_met": condition_met,
        "success": wait_success,
        "outcome": str(wait_state.get("outcome") or "running"),
        "partial_failure": bool(wait_state.get("partial_failure")),
        "timed_out": waiter_timed_out,
        "quorum_possible": quorum_possible,
        "required_successes": required_successes,
        "quorum_total": quorum_total,
        "quorum_terminal_count": quorum_terminal_count,
        "successful_count": successful_count,
        "failure_count": failure_count,
        "pending_count": pending_count,
        "failure_reasons": list(wait_state.get("failure_reasons") or []),
        "count": len(ids),
        "terminal_count": sum(1 for item in states if item.get("status") in TERMINAL_STATUSES),
        "agents": compact,
    }
    if team_id:
        response["team"] = team_summary or _team_summary(str(team_id))
    return response

def get_agent(
    settings: Settings,
    agent_id: str,
    include_logs: bool = False,
    tail_lines: int = 40,
) -> Dict[str, Any]:
    meta = _authorize_agent_control(agent_id, "get_agent")
    meta = _normalize(agent_id, meta)
    result: Dict[str, Any] = {"ok": True, **_public_meta(agent_id, meta)}
    result_path = _agent_dir(agent_id) / "result.txt"
    if meta.get("status") in TERMINAL_STATUSES and result_path.exists():
        text = result_path.read_text(encoding="utf-8", errors="replace").strip()
        limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
        result["result"] = truncate(text, limit)[0]
    if include_logs:
        result["logs"] = {
            "stdout": _tail_text(_agent_dir(agent_id) / "stdout.log", tail_lines),
            "stderr": _tail_text(_agent_dir(agent_id) / "stderr.log", tail_lines),
            "worker": _tail_text(_agent_dir(agent_id) / "worker.log", tail_lines),
        }
    return result


def _recover_provider_session_id(meta: Dict[str, Any], agent_id: str) -> Optional[str]:
    existing = str(meta.get("session_id") or meta.get("resume_session_id") or "").strip()
    if existing:
        return existing
    if str(meta.get("provider") or "").lower() == "chatgpt":
        recovered = _chatgpt_session_for_job(meta)
        if recovered:
            return str(recovered)

    stdout_path = _agent_dir(agent_id) / "stdout.log"
    if not stdout_path.exists():
        return None

    def find(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key in ("sessionID", "sessionId", "session_id", "thread_id"):
                found = value.get(key)
                if found:
                    return str(found)
            for child in value.values():
                found = find(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find(child)
                if found:
                    return found
        return None

    lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = find(event)
        if found:
            return found
    return None


def _agent_action_single(
    settings: Settings,
    agent_id: str,
    action: str,
    message: Optional[str] = None,
    signal: str = "TERM",
) -> Dict[str, Any]:
    action = action.lower().strip()
    if action not in {"cancel", "message", "retry", "resume", "despawn", "apply", "discard"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be cancel, message, retry, resume, despawn, apply, or discard.")
    meta = _authorize_agent_control(agent_id, f"agent_action:{action}")
    meta = _normalize(agent_id, meta)

    if action in {"apply", "discard"}:
        if action == "apply" and current_policy_context().agent_id is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, {
                "error": "git_apply_root_required",
                "message": "Applying an isolated worktree to its source checkout requires the local/root control plane.",
            })
        if meta.get("status") not in TERMINAL_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Wait for or cancel the agent before action={action}.")
        state = _refresh_agent_worktree(agent_id)
        if not state.get("enabled"):
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "error": "git_isolation_disabled",
                "message": "This agent has no isolated Git worktree.",
                "reason": state.get("reason"),
            })
        if action == "apply":
            path = str(state.get("path") or "")
            active_refs = _active_worktree_referrers(path, exclude_agent_id=agent_id) if path else []
            if active_refs:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "error": "git_worktree_in_use",
                    "message": "A resumed/revision agent is still running in this isolated worktree.",
                    "active_referrers": active_refs,
                })
            try:
                updated, result = apply_worktree(state)
            except AgentWorktreeError as exc:
                raise _worktree_error_http(exc) from exc
            _persist_shared_worktree_state(agent_id, updated)
            latest = _read_meta(agent_id)
            return {"ok": bool(result.get("ok")), "action": "apply", **_public_meta(agent_id, latest), "apply": result}
        path = str(state.get("path") or "")
        active_refs = _active_worktree_referrers(path, exclude_agent_id=agent_id) if path else []
        if active_refs:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "error": "git_worktree_in_use",
                "message": "The isolated worktree is still used by a running resumed/revision agent.",
                "active_referrers": active_refs,
            })
        try:
            cleaned = cleanup_worktree(state, force=True)
        except AgentWorktreeError as exc:
            raise _worktree_error_http(exc) from exc
        _persist_shared_worktree_state(agent_id, cleaned)
        latest = _read_meta(agent_id)
        return {"ok": True, "action": "discard", **_public_meta(agent_id, latest)}

    if action == "cancel":
        if meta.get("status") in TERMINAL_STATUSES:
            _release_agent_admission(agent_id, meta)
            try:
                meta = _read_meta(agent_id)
                _wake_global_admission_queue(exclude_team_id=str(meta.get("team_id") or "") or None)
            except Exception:
                pass
            return {"ok": True, **_public_meta(agent_id, meta), "message": "Agent is already finished."}
        sig_name = signal.upper()
        allowed = {"TERM": signal_module.SIGTERM, "KILL": signal_module.SIGKILL, "INT": signal_module.SIGINT}
        if sig_name not in allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "signal must be TERM, KILL, or INT.")
        def mark_cancelled(current: Dict[str, Any]) -> None:
            now = _now()
            current.update({
                "status": "cancelled",
                "phase": "cancelled",
                "ended_at": now,
                "updated_at": now,
                "note": f"Cancelled with {sig_name}.",
            })

        meta = _update_meta(agent_id, mark_cancelled)
        try:
            workflow_mark_terminal(agent_id, "cancelled")
        except WorkflowCheckpointError:
            pass
        get_scoped_credential_store().revoke_agent(agent_id)
        browser_tabs.release_agent_leases(agent_id)
        provider_signal = allowed[sig_name]
        if meta.get("provider") == "chatgpt" and sig_name == "TERM":
            provider_signal = signal_module.SIGINT
        _kill_group(meta.get("provider_pid"), provider_signal)
        if meta.get("provider") == "chatgpt" and provider_signal == signal_module.SIGINT:
            time.sleep(0.5)
        _kill_group(meta.get("worker_pid"), allowed[sig_name])
        with _WORKERS_LOCK:
            worker_proc = _WORKERS.get(agent_id)
        if worker_proc is not None:
            try:
                worker_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(meta.get("worker_pid"), signal_module.SIGKILL)
                try:
                    worker_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        _release_agent_admission(agent_id, meta)
        try:
            _refresh_agent_worktree(agent_id)
            meta = _read_meta(agent_id)
        except Exception:
            pass
        try:
            _wake_global_admission_queue(exclude_team_id=str(meta.get("team_id") or "") or None)
        except Exception:
            pass
        return {"ok": True, **_public_meta(agent_id, meta)}

    if action == "despawn":
        if meta.get("status") not in TERMINAL_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cancel a running agent before despawn.")
        blocker = _worktree_despawn_blocker(agent_id, meta)
        if blocker is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "error": "unapplied_worktree_changes",
                "message": "Isolated Git changes must be applied or explicitly discarded before despawn.",
                **blocker,
                "next_actions": ["agent_action(action=apply)", "agent_action(action=discard)"],
            })
        state = _refresh_agent_worktree(agent_id) if isinstance(meta.get("worktree"), dict) and (meta.get("worktree") or {}).get("enabled") else {}
        path = str(state.get("path") or "")
        refs = _worktree_referrers(path, exclude_agent_id=agent_id) if path else []
        if state.get("enabled") and not refs and state.get("status") not in {"discarded", "cleaned", "missing"}:
            try:
                cleanup_worktree(state, force=True)
            except AgentWorktreeError as exc:
                raise _worktree_error_http(exc) from exc
        _release_agent_admission(agent_id, meta)
        get_scoped_credential_store().revoke_agent(agent_id)
        browser_tabs.release_agent_leases(agent_id)
        shutil.rmtree(_agent_dir(agent_id))
        return {"ok": True, "agent_id": agent_id, "status": "despawned", "worktree_preserved_for": refs}

    original_prompt = (_agent_dir(agent_id) / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    if action == "retry":
        try:
            checkpoint = workflow_for_agent(agent_id)
        except WorkflowCheckpointError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"retry_replay_unsafe: durable checkpoint is {exc.code}; use action=resume only after the checkpoint is trustworthy.",
            ) from exc
        if checkpoint is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "retry_replay_unsafe: this legacy agent has no durable checkpoint, so original-prompt replay is outcome-unknown.",
            )
        unsafe = (
            str(checkpoint.get("safety") or "unknown") != "verified"
            or int(checkpoint.get("receipt_count") or 0) > 0
            or int(checkpoint.get("resume_generation") or 0) > 0
            or bool(checkpoint.get("pending_effects"))
        )
        if unsafe:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "retry_replay_unsafe: this task crossed a durable side-effect/resume boundary; use action=resume so the existing provider session and receipts are preserved.",
            )
        return _spawn_internal(
            settings=settings,
            provider=meta["provider"],
            prompt=original_prompt,
            model=meta.get("model"),
            reasoning=meta.get("reasoning"),
            cwd=_source_cwd_from_meta(meta),
            timeout_s=meta.get("timeout_s"),
            title=f"Retry: {meta.get('title') or agent_id}",
            result_style=meta.get("result_style", "concise"),
            access_mode=meta.get("access_mode", "workspace_write"),
            scope=_source_scope_from_meta(meta),
            permission_profile=str(meta.get("permission_profile") or "trusted"),
            capability_profile=str(meta.get("capability_profile") or "legacy"),
            parent_agent_id=agent_id,
            attempt=int(meta.get("attempt", 1)) + 1,
            idle_timeout_s=meta.get("idle_timeout_s"),
            retries=int(meta.get("retries") or 0),
            project=meta.get("project"),
            role=meta.get("role"),
            provenance_class=str(meta.get("provenance_class") or "local"),
            git_isolation=str(meta.get("git_isolation") or "auto"),
            git_base_commit=(meta.get("worktree") or {}).get("base_commit") if isinstance(meta.get("worktree"), dict) else None,
        )

    if action == "resume":
        if meta.get("status") not in {"failed", "timeout", "stalled", "cancelled"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Durable resume is only available for an interrupted failed/timeout/stalled/cancelled agent.",
            )
        input_hash = str(meta.get("workflow_input_hash") or "").strip()
        if not input_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "resume_outcome_unknown: this legacy agent has no durable workflow input hash.",
            )
        session_id = _recover_provider_session_id(meta, agent_id)
        if not session_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "resume_outcome_unknown: provider session id could not be recovered safely.",
            )
        try:
            _update_meta(agent_id, lambda current: current.update({"session_id": session_id, "updated_at": _now()}))
            update_provider_state(agent_id, session_id=session_id)
            checkpoint = prepare_resume(
                agent_id, expected_input_hash=input_hash, session_id=session_id,
            )
        except CheckpointUnknownError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{exc.code}: {exc}") from exc
        except CheckpointConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{exc.code}: {exc}") from exc
        except WorkflowCheckpointError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"resume_outcome_unknown: checkpoint {exc.code}; refusing replay."
            ) from exc
        try:
            return _spawn_internal(
                settings=settings,
                provider=meta["provider"],
                prompt=durable_resume_prompt(checkpoint),
                model=meta.get("model"),
                reasoning=meta.get("reasoning"),
                cwd=meta.get("cwd"),
                timeout_s=meta.get("timeout_s"),
                title=f"Resume: {meta.get('title') or agent_id}",
                result_style=meta.get("result_style", "concise"),
                access_mode=meta.get("access_mode", "workspace_write"),
                scope=ResourceScope.from_dict(meta.get("scope")),
                permission_profile=str(meta.get("permission_profile") or "trusted"),
                capability_profile=str(meta.get("capability_profile") or "legacy"),
                parent_agent_id=agent_id,
                resume_session_id=session_id,
                attempt=int(meta.get("attempt", 1)) + 1,
                idle_timeout_s=meta.get("idle_timeout_s"),
                retries=int(meta.get("retries") or 0),
                project=meta.get("project"),
                role=meta.get("role"),
                provenance_class=str(meta.get("provenance_class") or "local"),
                workflow_id=str(checkpoint["workflow_id"]),
                workflow_input_hash_value=str(checkpoint["input_hash"]),
                resume_generation=int(checkpoint["resume_generation"]),
                resume_token=str(checkpoint["resume_token"]),
                resume_parent_agent_id=agent_id,
                git_isolation=str(meta.get("git_isolation") or "auto"),
                reuse_worktree_agent_id=agent_id if (meta.get("worktree") or {}).get("enabled") else None,
            )
        except Exception:
            abort_resume(
                str(checkpoint["workflow_id"]), resume_token=str(checkpoint["resume_token"]),
                reason="resume_spawn_failed_before_provider",
            )
            raise

    if not message or not message.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message is required for action=message.")
    if meta.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for or cancel the current agent before sending a follow-up.")
    session_id = meta.get("session_id")
    if not session_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This agent has no resumable provider session_id.")
    return _spawn_internal(
        settings=settings,
        provider=meta["provider"],
        prompt=message.strip(),
        model=meta.get("model"),
        reasoning=meta.get("reasoning"),
        cwd=meta.get("cwd"),
        timeout_s=meta.get("timeout_s"),
        title=f"Follow-up: {meta.get('title') or agent_id}",
        result_style=meta.get("result_style", "concise"),
        access_mode=meta.get("access_mode", "workspace_write"),
        scope=ResourceScope.from_dict(meta.get("scope")),
        permission_profile=str(meta.get("permission_profile") or "trusted"),
        capability_profile=str(meta.get("capability_profile") or "legacy"),
        parent_agent_id=agent_id,
        resume_session_id=session_id,
        attempt=int(meta.get("attempt", 1)) + 1,
        idle_timeout_s=meta.get("idle_timeout_s"),
        retries=int(meta.get("retries") or 0),
        project=meta.get("project"),
        role=meta.get("role"),
        provenance_class=str(meta.get("provenance_class") or "local"),
        git_isolation=str(meta.get("git_isolation") or "auto"),
        reuse_worktree_agent_id=agent_id if (meta.get("worktree") or {}).get("enabled") else None,
    )


def agent_action(
    settings: Settings,
    action: str,
    agent_id: Optional[str] = None,
    team_id: Optional[str] = None,
    message: Optional[str] = None,
    signal: str = "TERM",
) -> Dict[str, Any]:
    if bool(agent_id) == bool(team_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of agent_id or team_id.")
    if agent_id:
        return _agent_action_single(settings, agent_id=agent_id, action=action, message=message, signal=signal)

    normalized_action = action.lower().strip()
    team = _authorize_team_control(str(team_id), f"agent_action:{normalized_action}")
    ids = list(team.get("agent_ids") or [])
    if normalized_action in {"message", "resume", "apply", "discard"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{normalized_action} is only supported for an individual agent session.")
    if normalized_action == "cancel":
        if int(team.get("scheduler_version") or 0) >= 1:
            def mark_team_cancelled(current: Dict[str, Any]) -> None:
                current["cancelled"] = True
                current["updated_at"] = _now()
                for task in current.get("tasks") or []:
                    if str(task.get("state") or "") not in _GRAPH_TASK_TERMINAL:
                        task["state"] = "cancelled"
                        task["failure_reason"] = "team_cancelled"
            team = _update_team(str(team_id), mark_team_cancelled)
            ids = list(team.get("agent_ids") or [])
            try:
                admission_cancel_queued(AGENTS_DIR, team_id=str(team_id))
            except Exception:
                pass
        results = []
        for child_id in ids:
            try:
                results.append(_agent_action_single(settings, child_id, "cancel", signal=signal))
            except HTTPException as exc:
                results.append({"agent_id": child_id, "ok": False, "error": str(exc.detail)})
        team["updated_at"] = _now()
        _write_team(str(team_id), team)
        try:
            _wake_global_admission_queue(exclude_team_id=str(team_id))
        except Exception:
            pass
        return {"ok": True, "action": "cancel", "team": _team_summary(str(team_id)), "results": results}
    if normalized_action == "despawn":
        summary = _team_summary(str(team_id), team)
        if summary["terminal_count"] < summary["count"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cancel or wait for all team agents before despawn.")
        blockers = []
        for child_id in ids:
            try:
                blocker = _worktree_despawn_blocker(child_id)
            except HTTPException:
                blocker = None
            if blocker is not None:
                blockers.append(blocker)
        if blockers:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "error": "unapplied_worktree_changes",
                "message": "Apply or discard isolated child changes before team despawn.",
                "agents": blockers,
            })
        results = []
        for child_id in ids:
            try:
                results.append(_agent_action_single(settings, child_id, "despawn"))
            except HTTPException as exc:
                results.append({"agent_id": child_id, "ok": False, "error": str(exc.detail)})
        try:
            admission_release(AGENTS_DIR, team_id=str(team_id))
        except Exception:
            pass
        shutil.rmtree(_team_dir(str(team_id)))
        return {"ok": True, "team_id": team_id, "status": "despawned", "results": results}
    if normalized_action == "retry":
        if int(team.get("scheduler_version") or 0) >= 1:
            for child_id in ids:
                try:
                    checkpoint = workflow_for_agent(child_id)
                except WorkflowCheckpointError as exc:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"retry_replay_unsafe: child {child_id} checkpoint is {exc.code}; resume the child explicitly instead.",
                    ) from exc
                if checkpoint is not None and (
                    str(checkpoint.get("safety") or "unknown") != "verified"
                    or int(checkpoint.get("receipt_count") or 0) > 0
                    or int(checkpoint.get("resume_generation") or 0) > 0
                    or bool(checkpoint.get("pending_effects"))
                ):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"retry_replay_unsafe: child {child_id} crossed a durable side-effect/resume boundary; resume that child explicitly.",
                    )
            tasks = []
            for task in team.get("tasks") or []:
                tasks.append({
                    "id": task.get("id"),
                    "prompt": task.get("prompt"),
                    "title": task.get("title"),
                    "scope": task.get("scope"),
                    "project": task.get("project"),
                    "role": task.get("role"),
                    "depends_on": list(task.get("depends_on") or []),
                    "review_of": task.get("review_of"),
                    "max_revisions": int(task["max_revisions"] if task.get("max_revisions") is not None else (team.get("max_revisions") or 0)),
                })
            return spawn_agents(
                settings=settings, tasks=tasks, provider=team["provider"], model=team.get("model"),
                reasoning=team.get("reasoning"), cwd=team.get("cwd"), timeout_s=team.get("timeout_s"),
                idle_timeout_s=team.get("idle_timeout_s"), retries=int(team.get("retries") or 0),
                result_style=team.get("result_style", "concise"), access_mode=team.get("access_mode", "read_only"),
                title=f"Retry: {team.get('title') or team_id}", parent_team_id=str(team_id),
                scope=team.get("scope"), parent_profile="trusted", project=team.get("project"),
                role=team.get("role"), provenance_class=str(team.get("provenance_class") or "local"),
                max_parallel=int(team.get("max_parallel") or len(tasks) or 1),
                max_revisions=int(team.get("max_revisions") or 0),
                team_timeout_s=team.get("team_timeout_s"),
                max_team_retries=team.get("max_team_retries"),
                max_total_tool_calls=team.get("max_total_tool_calls"),
                max_total_tokens=team.get("max_total_tokens"),
                git_isolation=str(team.get("git_isolation") or "auto"),
            )
        tasks = []
        for index, child_id in enumerate(ids, start=1):
            prompt_path = _agent_dir(child_id) / "prompt.txt"
            child = _read_meta(child_id)
            try:
                checkpoint = workflow_for_agent(child_id)
            except WorkflowCheckpointError as exc:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"retry_replay_unsafe: child {child_id} checkpoint is {exc.code}; resume the child explicitly instead.",
                ) from exc
            if checkpoint is not None and (
                str(checkpoint.get("safety") or "unknown") != "verified"
                or int(checkpoint.get("receipt_count") or 0) > 0
                or int(checkpoint.get("resume_generation") or 0) > 0
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"retry_replay_unsafe: child {child_id} crossed a durable side-effect/resume boundary; resume that child explicitly.",
                )
            tasks.append({
                "prompt": prompt_path.read_text(encoding="utf-8", errors="replace"),
                "title": child.get("title") or f"Agent {index}",
                "project": child.get("project"),
                "role": child.get("role"),
            })
        return spawn_agents(
            settings=settings, tasks=tasks, provider=team["provider"], model=team.get("model"),
            reasoning=team.get("reasoning"), cwd=team.get("cwd"), timeout_s=team.get("timeout_s"),
            idle_timeout_s=team.get("idle_timeout_s"), retries=int(team.get("retries") or 0),
            result_style=team.get("result_style", "concise"), access_mode=team.get("access_mode", "read_only"),
            title=f"Retry: {team.get('title') or team_id}", parent_team_id=str(team_id),
            scope=team.get("scope"), parent_profile="trusted", project=team.get("project"),
            role=team.get("role"), provenance_class=str(team.get("provenance_class") or "local"),
            git_isolation=str(team.get("git_isolation") or "auto"),
        )
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be cancel, message, retry, resume, despawn, apply, or discard.")


def _extract_opencode(path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    current: List[str] = []
    candidate = ""
    final = ""
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    if not path.exists():
        return "", None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("sessionID") or (event.get("part") or {}).get("sessionID")
        event_type = event.get("type")
        part = event.get("part") or {}
        if event_type == "step_start":
            current = []
        elif event_type == "text":
            text = part.get("text")
            if text:
                current.append(str(text))
        elif event_type == "step_finish":
            if current:
                candidate = "".join(current).strip()
            if part.get("reason") == "stop" and candidate:
                final = candidate
            if isinstance(part.get("tokens"), dict):
                usage = part["tokens"]
    return (final or candidate).strip(), session_id, usage


def _extract_chatgpt(path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    final = ""
    session_id: Optional[str] = None
    if not path.exists():
        return "", None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        found_session = event.get("sessionId") or event.get("session_id")
        if found_session:
            session_id = str(found_session)
        if str(event.get("type") or "") == "final":
            text = event.get("text")
            if text:
                final = str(text).strip()
    return final, session_id, None


def _find_key_recursive(value: Any, keys: set) -> Optional[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item:
                return item
            found = _find_key_recursive(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key_recursive(item, keys)
            if found:
                return found
    return None


def _extract_codex_session(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_key_recursive(event, {"thread_id", "session_id", "sessionID"})
        if found:
            return str(found)
    return None


def _build_provider_command(meta: Dict[str, Any], prompt: str, result_path: Path) -> List[str]:
    provider = meta["provider"]
    binary = meta["binary"]
    model = meta.get("model")
    reasoning = meta.get("reasoning")
    resume_session_id = meta.get("resume_session_id")
    access_mode = meta.get("access_mode", "workspace_write")

    if provider == "opencode":
        _validate_provider_access_mode(provider, access_mode)
        cmd = [binary, "run", "--format", "json", "--auto", "--dir", meta["cwd"]]
        if access_mode != "full":
            cmd.insert(2, "--pure")
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--variant", reasoning]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        cmd.append(prompt)
        return cmd

    if provider == "chatgpt":
        if resume_session_id:
            cmd = [binary, "resume", str(resume_session_id)]
        else:
            project = str(meta.get("project") or _chatgpt_default_project() or "").strip()
            cmd = [binary, "project", project, "new"] if project else [binary, "new"]
        cmd += ["--json-stream", "--timeout", str(int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S))]
        if model:
            cmd += ["--model", str(model)]
        effort = _chatgpt_effort(reasoning)
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        return cmd

    if resume_session_id:
        cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check", "-o", str(result_path)]
        sandbox_map = {"read_only": "read-only", "workspace_write": "workspace-write", "full": "danger-full-access"}
        cmd += [
            "--config", 'approval_policy="never"',
            "--config", f'sandbox_mode="{sandbox_map[access_mode]}"',
            "--config", 'shell_environment_policy.inherit="none"',
            "--config", 'shell_environment_policy.set.PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
        ]
        cmd += _codex_scoped_mcp_args(meta)
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
        cmd += [resume_session_id, prompt]
        return cmd

    cmd = [
        binary, "exec", "--json", "--color", "never", "--skip-git-repo-check",
        "-C", meta["cwd"], "-o", str(result_path),
        "--config", 'approval_policy="never"',
        "--config", 'shell_environment_policy.inherit="none"',
        "--config", 'shell_environment_policy.set.PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
    ]
    sandbox_map = {"read_only": "read-only", "workspace_write": "workspace-write", "full": "danger-full-access"}
    cmd += ["--sandbox", sandbox_map[access_mode]]
    cmd += _codex_scoped_mcp_args(meta)
    if model:
        cmd += ["--model", model]
    if reasoning:
        cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
    cmd.append(prompt)
    return cmd


def _codex_tool_name(item: Dict[str, Any]) -> Optional[str]:
    item_type = str(item.get("type") or "").strip()
    if item_type not in {"command_execution", "mcp_tool_call", "file_change", "web_search"}:
        return None
    explicit = item.get("name") or item.get("tool")
    return str(explicit).strip() if explicit else item_type


def _apply_provider_event(meta: Dict[str, Any], event: Dict[str, Any], now: float) -> Optional[bool]:
    if meta.get("status") == "cancelled":
        return False
    event_type = str(event.get("type") or "output")
    provider = str(meta.get("provider") or "opencode").lower()
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    generic_session = event.get("sessionID") or event.get("sessionId") or event.get("session_id") or part.get("sessionID")
    if generic_session:
        meta["session_id"] = str(generic_session)
    if not meta.get("first_event_at"):
        meta["first_event_at"] = now
    meta["last_activity_at"] = now
    meta["last_event_type"] = event_type

    if provider == "chatgpt":
        session_id = event.get("sessionId") or event.get("session_id")
        if session_id:
            meta["session_id"] = str(session_id)
        provider_job_id = event.get("jobId") or event.get("job_id")
        if provider_job_id:
            meta["provider_job_id"] = str(provider_job_id)
        if event_type == "job_started":
            meta["phase"] = "starting"
            meta["turn_count"] = max(1, int(meta.get("turn_count") or 0))
            meta["turn_started_at"] = now
        elif event_type == "status":
            if int(meta.get("step_count") or 0) == 0:
                meta["step_count"] = 1
            meta["phase"] = "reasoning"
        elif event_type == "tool_activity":
            meta["phase"] = "tool" if bool(event.get("active")) else "reasoning"
        elif event_type == "tool_update":
            text = str(event.get("text") or "").strip()
            if text:
                meta["last_tool"] = text
            meta["phase"] = "tool"
        elif event_type == "tool_start":
            meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
            meta["last_tool"] = str(event.get("text") or event.get("toolId") or "tool")
            if not meta.get("first_tool_at"):
                meta["first_tool_at"] = now
            meta["active_tool_started_at"] = now
            meta["phase"] = "tool"
        elif event_type == "tool_end":
            tool_started = meta.get("active_tool_started_at")
            if tool_started:
                meta["last_tool_duration_ms"] = max(0, int((now - float(tool_started)) * 1000))
            meta["active_tool_started_at"] = None
            meta["checkpoint_waiting_for_tool"] = False
            meta["phase"] = "reasoning"
        elif event_type == "interrupting":
            meta["phase"] = "checkpointing"
        elif event_type == "interrupted":
            meta["turn_count"] = int(meta.get("turn_count") or 0) + 1
            meta["turn_started_at"] = now
            meta["active_tool_started_at"] = None
            meta["checkpoint_pending"] = False
            meta["checkpoint_waiting_for_tool"] = False
            meta["note"] = "ChatGPT checkpoint resumed in a fresh turn."
            meta["phase"] = "reasoning"
        elif event_type == "assistant_delta":
            meta["phase"] = "finalizing"
        elif event_type == "final":
            meta["phase"] = "finalizing"
        elif event_type in {"stopped", "error"}:
            meta["phase"] = "failed" if event_type == "error" else "cancelled"
        else:
            meta["phase"] = meta.get("phase") or "working"
    elif provider == "codex":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event_type == "thread.started":
            meta["phase"] = "starting"
            thread_id = event.get("thread_id")
            if thread_id:
                meta["session_id"] = thread_id
        elif event_type == "turn.started":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            meta["phase"] = "reasoning"
        elif event_type == "item.started":
            tool_name = _codex_tool_name(item)
            if tool_name:
                meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
                meta["last_tool"] = tool_name
                if not meta.get("first_tool_at"):
                    meta["first_tool_at"] = now
                meta["phase"] = "tool"
            elif item.get("type") == "agent_message":
                meta["phase"] = "finalizing"
            else:
                meta["phase"] = "reasoning"
        elif event_type == "item.completed":
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                meta["phase"] = "finalizing"
            elif _codex_tool_name(item):
                meta["phase"] = "reasoning"
        elif event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage:
                meta["usage"] = {
                    "input": int(usage.get("input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0),
                    "reasoning": int(usage.get("reasoning_output_tokens") or 0),
                    "cache": {"read": int(usage.get("cached_input_tokens") or 0), "write": int(usage.get("cache_write_input_tokens") or 0)},
                }
            meta["phase"] = "finalizing"
        elif event_type in {"turn.failed", "error"}:
            meta["phase"] = "failed"
        else:
            meta["phase"] = meta.get("phase") or "reasoning"
    else:
        if event_type == "step_start":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            meta["phase"] = "reasoning"
        elif event_type == "tool_use":
            meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
            meta["last_tool"] = part.get("tool")
            if not meta.get("first_tool_at"):
                meta["first_tool_at"] = now
            timing = part.get("time") if isinstance(part.get("time"), dict) else {}
            start_ms, end_ms = timing.get("start"), timing.get("end")
            if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)):
                meta["last_tool_duration_ms"] = max(0, int(end_ms - start_ms))
            meta["phase"] = "tool"
        elif event_type == "text":
            meta["phase"] = "finalizing"
        elif event_type == "step_finish":
            reason = part.get("reason")
            meta["phase"] = "finalizing" if reason == "stop" else "reasoning"
        else:
            meta["phase"] = meta.get("phase") or "working"

    meta["updated_at"] = now
    return True


def _record_provider_event(agent_id: str, raw_line: str) -> None:
    now = _now()
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        event = {"type": "output"}
    try:
        saved = _update_meta(agent_id, lambda meta: _apply_provider_event(meta, event, now))
        try:
            update_provider_state(
                agent_id, session_id=str(saved.get("session_id") or "") or None,
                provider_job_id=str(saved.get("provider_job_id") or "") or None,
            )
            note_provider_event(agent_id, str(saved.get("provider") or ""), event)
        except Exception:
            pass
    except HTTPException:
        return

def _capture_provider_stream(agent_id: str, stream, path: Path, parse_events: bool) -> None:
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        for line in iter(stream.readline, ""):
            if not line:
                break
            handle.write(line)
            handle.flush()
            if parse_events:
                _record_provider_event(agent_id, line)
    try:
        stream.close()
    except Exception:
        pass


def _run_provider_attempt(agent_id: str, meta: Dict[str, Any], prompt: str, attempt_index: int) -> Tuple[int, Optional[str]]:
    path = _agent_dir(agent_id)
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    result_path = path / "result.txt"
    if meta.get("provider") == "codex":
        result_path.write_text("", encoding="utf-8")
    marker = f"\n--- provider attempt {attempt_index + 1} ---\n"
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write(marker)
    with stderr_path.open("a", encoding="utf-8") as handle:
        handle.write(marker)

    scope = ResourceScope.from_dict(meta.get("scope"))
    store = get_scoped_credential_store()
    scoped_token = ""
    credential_id: Optional[str] = None
    if meta.get("scoped_mcp"):
        scoped_token, credential_id = store.issue(
            agent_id=agent_id,
            team_id=meta.get("team_id"),
            profile=str(meta.get("permission_profile") or "trusted"),
            scope=scope,
            ttl_s=int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S) + 300,
        )

        def record_credential(current: Dict[str, Any]) -> None:
            current["scoped_credential_id"] = credential_id
            current["updated_at"] = _now()

        _update_meta(agent_id, record_credential)
    env, cleanup_root = _provider_env(agent_id, meta, scoped_token)
    boundary_profile: Optional[Path] = None
    try:
        cmd = _build_provider_command(meta, prompt, result_path)
        cmd, boundary_profile = _provider_process_command(agent_id, meta, cmd)
        proc = subprocess.Popen(
            cmd, cwd=meta["cwd"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
        )
        now = _now()
        cancelled_at_start = False

        def record_provider_start(current: Dict[str, Any]) -> Optional[bool]:
            nonlocal cancelled_at_start
            if current.get("status") == "cancelled":
                cancelled_at_start = True
                return False
            current.update({
                "provider_pid": proc.pid, "provider_started_at": now, "last_activity_at": now,
                "phase": "provider_starting", "updated_at": now, "retry_count": attempt_index,
            })
            if str(current.get("provider") or "").lower() == "chatgpt" and not current.get("turn_started_at"):
                current["turn_started_at"] = now
                current["turn_count"] = max(1, int(current.get("turn_count") or 0))
            return True

        latest = _update_meta(agent_id, record_provider_start)
        if cancelled_at_start:
            _kill_group(proc.pid, signal_module.SIGTERM)
            return proc.wait(), "cancelled"
        out_thread = threading.Thread(target=_capture_provider_stream, args=(agent_id, proc.stdout, stdout_path, True), daemon=True)
        err_thread = threading.Thread(target=_capture_provider_stream, args=(agent_id, proc.stderr, stderr_path, False), daemon=True)
        out_thread.start(); err_thread.start()
        attempt_started = time.monotonic()
        last_admission_heartbeat = 0.0
        stop_reason: Optional[str] = None
        timeout_s = int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S)
        idle_timeout_s = meta.get("idle_timeout_s")
        while proc.poll() is None:
            latest = _read_meta(agent_id)
            heartbeat_now = time.monotonic()
            lease_id = str(latest.get("admission_lease_id") or "").strip()
            if lease_id and heartbeat_now - last_admission_heartbeat >= 15.0:
                try:
                    admission_heartbeat(AGENTS_DIR, lease_id=lease_id)
                except Exception:
                    pass
                last_admission_heartbeat = heartbeat_now
            if latest.get("status") == "cancelled":
                stop_reason = "cancelled"
            elif meta.get("provider") == "chatgpt":
                budget_action = _chatgpt_turn_budget_action(latest)
                if budget_action == "wait_for_tool":
                    if not latest.get("checkpoint_waiting_for_tool"):
                        def mark_waiting(current: Dict[str, Any]) -> None:
                            current["checkpoint_waiting_for_tool"] = True
                            current["note"] = "ChatGPT turn budget reached; waiting for the active tool to finish before checkpointing."
                            current["updated_at"] = _now()
                        _update_meta(agent_id, mark_waiting)
                elif budget_action in {"turn_budget", "hard_tool_budget"}:
                    _request_chatgpt_checkpoint(agent_id, latest, budget_action)
            if stop_reason is None and time.monotonic() - attempt_started >= timeout_s:
                stop_reason = "timeout"
            elif stop_reason is None and idle_timeout_s and _now() - float(latest.get("last_activity_at") or latest.get("provider_started_at") or _now()) >= int(idle_timeout_s):
                stop_reason = "stalled"
            if stop_reason:
                provider_signal = signal_module.SIGINT if meta.get("provider") == "chatgpt" else signal_module.SIGTERM
                _kill_group(proc.pid, provider_signal)
                try:
                    proc.wait(timeout=4 if meta.get("provider") == "chatgpt" else 3)
                except subprocess.TimeoutExpired:
                    _kill_group(proc.pid, signal_module.SIGKILL)
                break
            time.sleep(0.2)
        exit_code = proc.wait()
        out_thread.join(timeout=2); err_thread.join(timeout=2)
        return exit_code, stop_reason
    finally:
        if credential_id:
            store.revoke_token_id(credential_id)
        if boundary_profile is not None:
            boundary_profile.unlink(missing_ok=True)
        _cleanup_provider_config(cleanup_root)

        if credential_id:
            def clear_credential(current: Dict[str, Any]) -> Optional[bool]:
                if current.get("scoped_credential_id") == credential_id:
                    current["scoped_credential_id"] = None
                    current["updated_at"] = _now()
                    return True
                return False

            try:
                _update_meta(agent_id, clear_credential)
            except HTTPException:
                pass


def _worker(agent_id: str) -> int:
    meta = _read_meta(agent_id)
    path = _agent_dir(agent_id)
    prompt = (path / "effective_prompt.txt").read_text(encoding="utf-8", errors="replace")
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    result_path = path / "result.txt"
    now = _now()

    def record_worker_start(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current.update({
            "status": "running", "phase": "worker_starting", "worker_started_at": now,
            "last_activity_at": now, "updated_at": now,
        })
        return True

    meta = _update_meta(agent_id, record_worker_start)
    if meta.get("status") == "cancelled":
        return 0

    final_reason: Optional[str] = None
    retry_delay_s = 0.0
    exit_code = 1
    max_attempts = int(meta.get("retries") or 0) + 1
    try:
        for attempt_index in range(max_attempts):
            latest = _read_meta(agent_id)
            if latest.get("status") == "cancelled":
                return 0
            attempt_prompt = prompt
            if attempt_index > 0:
                if latest.get("provider") != "chatgpt":
                    try:
                        checkpoint = workflow_for_agent(agent_id)
                    except WorkflowCheckpointError:
                        checkpoint = {"safety": "unknown", "receipt_count": 0}
                    if checkpoint is None:
                        final_reason = "outcome_unknown"
                        break
                    if str(checkpoint.get("safety") or "unknown") != "verified" or bool(checkpoint.get("pending_effects")):
                        final_reason = "outcome_unknown"
                        break
                    if int(checkpoint.get("receipt_count") or 0) > 0:
                        final_reason = "resume_required"
                        break
                recovered_session_id = _chatgpt_session_for_job(latest) if latest.get("provider") == "chatgpt" else None
                def record_retry(current: Dict[str, Any]) -> Optional[bool]:
                    if current.get("status") == "cancelled":
                        return False
                    now = _now()
                    if recovered_session_id:
                        current["session_id"] = recovered_session_id
                        current["resume_session_id"] = recovered_session_id
                    current.update({
                        "phase": "retrying", "retry_count": attempt_index, "provider_pid": None,
                        "provider_started_at": None, "last_activity_at": now,
                        "turn_started_at": None if current.get("provider") == "chatgpt" else current.get("turn_started_at"),
                        "checkpoint_pending": False if current.get("provider") == "chatgpt" else current.get("checkpoint_pending", False),
                        "checkpoint_waiting_for_tool": False if current.get("provider") == "chatgpt" else current.get("checkpoint_waiting_for_tool", False),
                        "note": f"Retrying same task after {final_reason or 'provider_error'}.", "updated_at": now,
                    })
                    return True

                latest = _update_meta(agent_id, record_retry)
                if latest.get("status") == "cancelled":
                    return 0
                if latest.get("provider") == "chatgpt" and latest.get("resume_session_id"):
                    attempt_prompt = _chatgpt_checkpoint_prompt("rate_limit" if final_reason == "rate_limited" else "turn_budget")
                if retry_delay_s > 0:
                    time.sleep(retry_delay_s)
            if latest.get("provider") == "chatgpt" and not _wait_chatgpt_provider_gate(agent_id):
                return 0
            stdout_offset = stdout_path.stat().st_size if stdout_path.exists() else 0
            stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
            exit_code, stop_reason = _run_provider_attempt(agent_id, latest, attempt_prompt, attempt_index)
            final_reason = stop_reason
            latest = _read_meta(agent_id)
            if latest.get("provider") == "chatgpt" and exit_code != 0 and stop_reason is None:
                throttle_reason = _chatgpt_rate_limit_reason(stdout_path, stderr_path, stdout_offset, stderr_offset)
                if throttle_reason:
                    def record_throttle(current: Dict[str, Any]) -> None:
                        count = int(current.get("throttle_count") or 0) + 1
                        cooldown_s = _chatgpt_cooldown_seconds(count)
                        now = _now()
                        current.update({
                            "phase": "throttled", "throttle_count": count, "last_throttled_at": now,
                            "last_throttle_reason": throttle_reason, "cooldown_until": now + cooldown_s,
                            "note": f"ChatGPT web throttled ({throttle_reason}); cooling down {cooldown_s}s before retry.",
                            "updated_at": now,
                        })
                    latest = _update_meta(agent_id, record_throttle)
                    _record_chatgpt_shared_throttle(float(latest.get("cooldown_until") or _now()), throttle_reason)
                    final_reason = "rate_limited"
            if latest.get("status") == "cancelled" or stop_reason == "cancelled":
                return 0
            if exit_code == 0 and stop_reason is None:
                break
            if attempt_index + 1 >= max_attempts:
                break

            # Durable replay safety has precedence over provider error classification.
            # This preserves the stronger parent-visible reason when side effects occurred.
            if latest.get("provider") != "chatgpt":
                try:
                    checkpoint = workflow_for_agent(agent_id)
                except WorkflowCheckpointError:
                    checkpoint = {"safety": "unknown", "receipt_count": 0}
                if checkpoint is None or str(checkpoint.get("safety") or "unknown") != "verified" or bool(checkpoint.get("pending_effects")):
                    final_reason = "outcome_unknown"
                    _update_meta(agent_id, lambda current: current.update({
                        "retry_blocked_reason": "outcome_unknown",
                        "note": "Automatic replay stopped because the side-effect outcome is unknown.",
                        "updated_at": _now(),
                    }))
                    break
                if int(checkpoint.get("receipt_count") or 0) > 0:
                    final_reason = "resume_required"
                    _update_meta(agent_id, lambda current: current.update({
                        "retry_blocked_reason": "resume_required",
                        "note": "Automatic replay stopped after a verified side-effect boundary; use agent_action(action=resume).",
                        "updated_at": _now(),
                    }))
                    break

            decision = _adaptive_retry_decision(
                latest, exit_code, final_reason, stdout_path, stderr_path, stdout_offset, stderr_offset,
            )
            classification = str(decision.get("reason") or "provider_error_nonretryable")
            retryable = bool(decision.get("retryable"))
            def record_retry_decision(current: Dict[str, Any]) -> None:
                current["last_retry_classification"] = classification
                current["last_retryable"] = retryable
                current["last_retry_decision_at"] = _now()
                current["updated_at"] = _now()
            latest = _update_meta(agent_id, record_retry_decision)
            if not retryable:
                final_reason = classification
                def record_retry_block(current: Dict[str, Any]) -> None:
                    current["retry_blocked_reason"] = classification
                    current["note"] = f"Automatic retry stopped: {classification}."
                    current["updated_at"] = _now()
                _update_meta(agent_id, record_retry_block)
                break

            reservation = _reserve_team_retry(
                latest.get("team_id"), agent_id, classification, float(decision.get("backoff_s") or 0.0),
            )
            if not reservation.get("allowed"):
                blocked = str(reservation.get("reason") or "retry_budget")
                final_reason = f"team_{blocked}"
                def record_team_retry_block(current: Dict[str, Any]) -> None:
                    current["retry_blocked_reason"] = blocked
                    current["team_retry_remaining"] = reservation.get("remaining")
                    current["note"] = f"Automatic retry stopped by team budget: {blocked}."
                    current["updated_at"] = _now()
                _update_meta(agent_id, record_team_retry_block)
                break
            retry_delay_s = max(0.0, float(reservation.get("delay_s") or 0.0))
            final_reason = classification
            def record_retry_slot(current: Dict[str, Any]) -> None:
                current["last_retry_reason"] = classification
                current["last_retry_delay_s"] = round(retry_delay_s, 3)
                current["team_retry_remaining"] = reservation.get("remaining")
                current["retry_blocked_reason"] = None
                current["updated_at"] = _now()
            _update_meta(agent_id, record_retry_slot)
    except Exception as exc:
        worker_error = f"Agent worker error: {exc}"
        def record_worker_failure(current: Dict[str, Any]) -> Optional[bool]:
            if current.get("status") == "cancelled":
                return False
            now = _now()
            current.update({
                "status": "failed", "phase": "failed", "exit_code": None, "ended_at": now, "updated_at": now,
                "note": worker_error,
            })
            return True

        meta = _update_meta(agent_id, record_worker_failure)
        if meta.get("status") != "cancelled":
            result_path.write_text(worker_error, encoding="utf-8")
            try:
                workflow_mark_terminal(agent_id, "failed")
            except WorkflowCheckpointError:
                pass
        return 1

    meta = _read_meta(agent_id)
    if meta.get("status") == "cancelled":
        return 0

    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    result = ""
    if meta["provider"] == "opencode":
        result, session_id, usage = _extract_opencode(stdout_path)
    elif meta["provider"] == "chatgpt":
        result, session_id, usage = _extract_chatgpt(stdout_path)
    else:
        session_id = _extract_codex_session(stdout_path)
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else None
        if result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()

    lesson_candidate_ids: List[str] = []
    lesson_candidate_error: Optional[str] = None
    role = str(meta.get("role") or "").strip().lower() or None
    if result and role in VALID_ROLES:
        clean_result, candidates = extract_lesson_candidates(result, role)
        result = clean_result
        evidence_refs = [f"agent:{agent_id}", f"attempt:{int(meta.get('attempt') or 1)}", f"provider:{meta.get('provider')}"]
        if meta.get("team_id"):
            evidence_refs.append(f"team:{meta.get('team_id')}")
        if session_id:
            evidence_refs.append(f"session:{session_id}")
        for candidate in candidates:
            try:
                recorded = lesson_record_agent_candidate(
                    **candidate, evidence_refs=evidence_refs, source="agent_run",
                    provenance_class=str(meta.get("provenance_class") or "local"),
                )
                lesson_candidate_ids.append(str(recorded["lesson"]["lesson_id"]))
            except Exception as exc:
                lesson_candidate_error = str(exc)[:300]

    if not result:
        error_tail = _tail_text(stderr_path, max_lines=30, max_chars=3000)
        result = error_tail or "Agent finished without a final handoff. Check logs with get_agent(include_logs=true)."

    limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
    result, was_truncated = truncate(result, limit)
    result_path.write_text(result, encoding="utf-8")
    if final_reason == "timeout":
        final_status = "timeout"
    elif final_reason == "stalled":
        final_status = "stalled"
    else:
        final_status = "completed" if exit_code == 0 else "failed"
    def record_completion(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        now = _now()
        current.update({
            "status": final_status, "phase": "completed" if final_status == "completed" else final_status,
            "exit_code": exit_code, "ended_at": now, "updated_at": now,
            "session_id": session_id or current.get("resume_session_id"), "usage": usage,
            "result_truncated": was_truncated, "result_chars": len(result), "provider_pid": None,
            "lesson_candidate_ids": lesson_candidate_ids, "lesson_candidate_error": lesson_candidate_error,
        })
        if final_status != "completed":
            if final_reason == "rate_limited":
                current["note"] = f"ChatGPT remained rate-limited after bounded retries; last provider code {exit_code}."
            elif final_reason == "resume_required":
                current["note"] = "Automatic replay stopped after a verified side-effect boundary; use agent_action(action=resume)."
            elif final_reason == "outcome_unknown":
                current["note"] = "Automatic replay stopped because provider-native activity made the side-effect outcome unknown."
            elif str(final_reason or "").startswith("team_"):
                current["note"] = f"Automatic retry stopped by team budget: {str(final_reason)[5:]}."
            elif final_reason in {
                "auth_error", "permission_error", "quota_exhausted", "invalid_model", "invalid_request",
                "provider_error_nonretryable",
            }:
                current["note"] = f"Provider failure is non-retryable ({final_reason}); last provider code {exit_code}."
            else:
                current["note"] = f"Provider ended as {final_status} with code {exit_code}."
        return True

    meta = _update_meta(agent_id, record_completion)
    try:
        update_provider_state(
            agent_id, session_id=str(meta.get("session_id") or "") or None,
            provider_job_id=str(meta.get("provider_job_id") or "") or None,
        )
        workflow_mark_terminal(agent_id, final_status)
    except WorkflowCheckpointError:
        pass
    try:
        _refresh_agent_worktree(agent_id)
        meta = _read_meta(agent_id)
    except Exception:
        pass
    if meta.get("status") == "cancelled":
        return 0
    return 0 if final_status == "completed" else 1


def _main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        agent_id = sys.argv[2]
        rc = _worker(agent_id)
        try:
            _refresh_agent_worktree(agent_id)
        except Exception:
            pass
        team_id = ""
        try:
            meta = _read_meta(agent_id)
            team_id = str(meta.get("team_id") or "").strip()
            _release_agent_admission(agent_id, meta)
            if team_id:
                _team_tick(team_id)
            _wake_global_admission_queue(exclude_team_id=team_id or None)
        except Exception as exc:
            try:
                with (_agent_dir(agent_id) / "worker.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"\n[team scheduler] {type(exc).__name__}: {exc}\n")
            except OSError:
                pass
        return rc
    print("tools_agents is an internal module; use the MCP agent tools.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
