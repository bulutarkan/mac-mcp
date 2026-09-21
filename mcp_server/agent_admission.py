from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence

from .file_transactions import path_revision

SCHEMA_VERSION = 1
DEFAULT_GLOBAL_LIMIT = 8
DEFAULT_PROVIDER_LIMIT = 8
DEFAULT_LEASE_TTL_S = 900
DEFAULT_QUEUE_LIMIT = 256
MAX_LIMIT = 64
MAX_QUEUE_LIMIT = 4096

_RESOURCE_KINDS = {
    "workspace", "path", "file", "browser_tab", "native_app", "native_window",
    "process", "clipboard",
}
_PATH_KINDS = {"workspace", "path", "file"}
_MODES = {"read", "write"}


class AdmissionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def global_limit() -> int:
    return _env_int("MAC_MCP_AGENT_GLOBAL_ACTIVE_LIMIT", DEFAULT_GLOBAL_LIMIT, 1, MAX_LIMIT)


def provider_limit(provider: str) -> int:
    key = str(provider or "").strip().upper().replace("-", "_") or "DEFAULT"
    fallback = _env_int("MAC_MCP_AGENT_PROVIDER_LIMIT", DEFAULT_PROVIDER_LIMIT, 1, MAX_LIMIT)
    return _env_int(f"MAC_MCP_AGENT_PROVIDER_LIMIT_{key}", fallback, 1, MAX_LIMIT)


def lease_ttl_s() -> int:
    return _env_int("MAC_MCP_AGENT_ADMISSION_TTL_S", DEFAULT_LEASE_TTL_S, 5, 86400)


def queue_limit() -> int:
    return _env_int("MAC_MCP_AGENT_ADMISSION_QUEUE_LIMIT", DEFAULT_QUEUE_LIMIT, 1, MAX_QUEUE_LIMIT)


def _now() -> float:
    return time.time()


def _state_path(root: Path) -> Path:
    return root / ".global-admission.json"


def _lock_path(root: Path) -> Path:
    return root / ".global-admission.lock"


def _ensure_root(root: Path) -> Path:
    path = Path(root).expanduser().resolve(strict=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _locked(root: Path) -> Iterator[Path]:
    path = _ensure_root(root)
    lock = _lock_path(path)
    with lock.open("a", encoding="utf-8") as handle:
        try:
            os.chmod(lock, 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "next_sequence": 1,
        "queue": {},
        "leases": {},
        "updated_at": _now(),
    }


def _read_unlocked(root: Path) -> Dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        return _empty_state()
    if not isinstance(payload.get("queue"), dict):
        payload["queue"] = {}
    if not isinstance(payload.get("leases"), dict):
        payload["leases"] = {}
    payload["next_sequence"] = max(1, int(payload.get("next_sequence") or 1))
    return payload


def _write_unlocked(root: Path, state: Mapping[str, Any]) -> None:
    path = _state_path(root)
    payload = dict(state)
    payload["schema_version"] = SCHEMA_VERSION
    payload["updated_at"] = _now()
    fd, tmp_name = tempfile.mkstemp(prefix=".global-admission.", suffix=".tmp", dir=root, text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def _canonical_path(value: str) -> str:
    return str(Path(str(value)).expanduser().resolve(strict=False))


def normalize_claims(claims: Optional[Iterable[Mapping[str, Any]]]) -> list[Dict[str, str]]:
    result: list[Dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in claims or ():
        if not isinstance(raw, Mapping):
            raise AdmissionError("invalid_resource_claim", "resource claims must be objects")
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in _RESOURCE_KINDS:
            raise AdmissionError(
                "invalid_resource_kind",
                f"resource kind must be one of: {', '.join(sorted(_RESOURCE_KINDS))}",
            )
        mode = str(raw.get("mode") or "write").strip().lower()
        if mode not in _MODES:
            raise AdmissionError("invalid_resource_mode", "resource mode must be read or write")
        identifier = str(raw.get("id") or raw.get("resource") or "").strip()
        if kind == "clipboard" and not identifier:
            identifier = "system"
        if not identifier:
            raise AdmissionError("invalid_resource_id", f"resource claim {kind!r} requires id")
        if kind in _PATH_KINDS:
            identifier = _canonical_path(identifier)
        else:
            identifier = identifier.lower() if kind in {"native_app"} else identifier
        expected_revision = str(raw.get("expected_revision") or "").strip() or None
        if expected_revision and kind != "file":
            raise AdmissionError("invalid_expected_revision", "expected_revision is only supported for kind=file")
        if expected_revision and (len(expected_revision) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in expected_revision)):
            raise AdmissionError("invalid_expected_revision", "expected_revision must be a 64-character hexadecimal revision")
        key = (kind, identifier, mode, expected_revision or "")
        if key in seen:
            continue
        seen.add(key)
        item: Dict[str, str] = {"kind": kind, "id": identifier, "mode": mode}
        if expected_revision:
            item["expected_revision"] = expected_revision.lower()
        result.append(item)
    return sorted(result, key=lambda item: (item["kind"], item["id"], item["mode"]))


def _path_overlap(left: str, right: str) -> bool:
    a = Path(left).resolve(strict=False)
    b = Path(right).resolve(strict=False)
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def claims_conflict(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    if str(left.get("mode")) == "read" and str(right.get("mode")) == "read":
        return False
    lk = str(left.get("kind") or "")
    rk = str(right.get("kind") or "")
    lid = str(left.get("id") or "")
    rid = str(right.get("id") or "")
    if lk in _PATH_KINDS and rk in _PATH_KINDS:
        return _path_overlap(lid, rid)
    if lk == rk:
        return lid == rid
    # A whole native application claim intentionally conflicts with any explicit
    # window id namespaced as "<app>:<window>" for that application.
    if lk == "native_app" and rk == "native_window":
        return rid.startswith(lid + ":")
    if rk == "native_app" and lk == "native_window":
        return lid.startswith(rid + ":")
    return False


def resources_conflict(left: Sequence[Mapping[str, str]], right: Sequence[Mapping[str, str]]) -> list[Dict[str, Any]]:
    conflicts: list[Dict[str, Any]] = []
    for a in left:
        for b in right:
            if claims_conflict(a, b):
                conflicts.append({"left": dict(a), "right": dict(b)})
    return conflicts


def _prune_expired_unlocked(state: Dict[str, Any], now: float) -> list[str]:
    expired: list[str] = []
    leases = state.setdefault("leases", {})
    for lease_id, lease in list(leases.items()):
        if float(lease.get("expires_at") or 0) > now:
            continue
        leases.pop(lease_id, None)
        expired.append(str(lease_id))
    return expired


def _active_counts(state: Mapping[str, Any]) -> tuple[int, Dict[str, int]]:
    leases = list((state.get("leases") or {}).values())
    by_provider: Dict[str, int] = {}
    total = 0
    for lease in leases:
        weight = int(lease.get("capacity_weight") if lease.get("capacity_weight") is not None else 1)
        if weight <= 0:
            continue
        provider = str(lease.get("provider") or "unknown")
        by_provider[provider] = by_provider.get(provider, 0) + weight
        total += weight
    return total, by_provider


def _resource_blockers(state: Mapping[str, Any], resources: Sequence[Mapping[str, str]]) -> list[Dict[str, Any]]:
    blockers: list[Dict[str, Any]] = []
    for lease_id, lease in (state.get("leases") or {}).items():
        conflicts = resources_conflict(resources, lease.get("resources") or [])
        if not conflicts:
            continue
        blockers.append({
            "lease_id": lease_id,
            "team_id": lease.get("team_id"),
            "task_id": lease.get("task_id"),
            "agent_id": lease.get("agent_id"),
            "provider": lease.get("provider"),
            "conflicts": conflicts,
        })
    return blockers


def _effective_provider_limit(provider: str, override: Optional[int] = None) -> int:
    configured = provider_limit(provider)
    if override is None:
        return configured
    return max(1, min(configured, int(override)))


def _capacity_reason(
    state: Mapping[str, Any], provider: str, provider_limit_override: Optional[int] = None,
) -> Optional[str]:
    total, by_provider = _active_counts(state)
    if total >= global_limit():
        return "global_capacity"
    if int(by_provider.get(provider, 0)) >= _effective_provider_limit(provider, provider_limit_override):
        return "provider_capacity"
    return None


def _request_runnable_against_active(state: Mapping[str, Any], request: Mapping[str, Any]) -> bool:
    provider = str(request.get("provider") or "")
    blocked_until = float(request.get("provider_blocked_until") or 0.0)
    if blocked_until > _now():
        return False
    if _capacity_reason(state, provider, request.get("provider_limit_override")):
        return False
    return not _resource_blockers(state, request.get("resources") or [])


def _fairness_reason(state: Mapping[str, Any], current: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    sequence = int(current.get("sequence") or 0)
    provider = str(current.get("provider") or "")
    resources = current.get("resources") or []
    earlier = sorted(
        (
            request for request in (state.get("queue") or {}).values()
            if int(request.get("sequence") or 0) < sequence
            and str(request.get("status") or "queued") == "queued"
        ),
        key=lambda item: int(item.get("sequence") or 0),
    )
    if not earlier:
        return None

    total_active, by_provider = _active_counts(state)
    runnable = [item for item in earlier if _request_runnable_against_active(state, item)]
    if not runnable:
        return None

    # Reserve capacity for older runnable work without forcing unrelated resources
    # into strict head-of-line blocking. Older conflicting claims always win.
    for item in runnable:
        conflicts = resources_conflict(resources, item.get("resources") or [])
        if conflicts:
            return {
                "reason": "fair_queue_resource",
                "ahead_request_id": item.get("request_id"),
                "ahead_team_id": item.get("team_id"),
                "ahead_task_id": item.get("task_id"),
            }

    older_global = len(runnable)
    if total_active + older_global >= global_limit():
        item = runnable[0]
        return {
            "reason": "fair_queue_global",
            "ahead_request_id": item.get("request_id"),
            "ahead_team_id": item.get("team_id"),
            "ahead_task_id": item.get("task_id"),
        }
    older_provider = [item for item in runnable if str(item.get("provider") or "") == provider]
    if int(by_provider.get(provider, 0)) + len(older_provider) >= _effective_provider_limit(
        provider, current.get("provider_limit_override")
    ):
        item = older_provider[0]
        return {
            "reason": "fair_queue_provider",
            "ahead_request_id": item.get("request_id"),
            "ahead_team_id": item.get("team_id"),
            "ahead_task_id": item.get("task_id"),
        }
    return None


def _queued_reason(
    state: Mapping[str, Any], request: Mapping[str, Any], *, provider_blocked_until: Optional[float] = None,
) -> tuple[Optional[str], Dict[str, Any]]:
    now = _now()
    if provider_blocked_until and float(provider_blocked_until) > now:
        return "provider_cooldown", {"retry_at": float(provider_blocked_until)}
    capacity = _capacity_reason(
        state, str(request.get("provider") or ""), request.get("provider_limit_override"),
    )
    if capacity:
        total, by_provider = _active_counts(state)
        return capacity, {
            "global_active": total,
            "global_limit": global_limit(),
            "provider_active": int(by_provider.get(str(request.get("provider") or ""), 0)),
            "provider_limit": _effective_provider_limit(
                str(request.get("provider") or ""), request.get("provider_limit_override"),
            ),
        }
    blockers = _resource_blockers(state, request.get("resources") or [])
    if blockers:
        return "resource_busy", {"blockers": blockers}
    fair = _fairness_reason(state, request)
    if fair:
        return str(fair.pop("reason")), fair
    return None, {}


def _validate_expected_revisions(resources: Sequence[Mapping[str, str]]) -> None:
    for claim in resources:
        expected = str(claim.get("expected_revision") or "").strip().lower()
        if not expected:
            continue
        path = Path(str(claim.get("id") or ""))
        try:
            current = path_revision(path, None)
        except Exception as exc:
            raise AdmissionError(
                "file_revision_unavailable",
                f"Could not verify expected revision for {path}.",
                details={"path": str(path), "retryable": True},
            ) from exc
        if current.lower() != expected:
            raise AdmissionError(
                "file_revision_conflict",
                f"File revision changed before agent admission: {path}",
                details={
                    "path": str(path),
                    "expected_revision": expected,
                    "current_revision": current.lower(),
                    "retryable": False,
                },
            )


def request_admission(
    root: Path,
    *,
    request_id: str,
    team_id: str,
    task_id: str,
    provider: str,
    resources: Optional[Iterable[Mapping[str, Any]]] = None,
    provider_blocked_until: Optional[float] = None,
    provider_limit_override: Optional[int] = None,
) -> Dict[str, Any]:
    rid = str(request_id or "").strip()
    if not rid:
        raise AdmissionError("invalid_request_id", "request_id is required")
    claims = normalize_claims(resources)
    _validate_expected_revisions(claims)
    current = _now()
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        expired = _prune_expired_unlocked(state, current)
        queue = state.setdefault("queue", {})
        leases = state.setdefault("leases", {})

        # Idempotent already-admitted lookup.
        for lease_id, lease in leases.items():
            if str(lease.get("request_id") or "") == rid:
                lease["expires_at"] = current + lease_ttl_s()
                lease["last_heartbeat_at"] = current
                _write_unlocked(locked_root, state)
                return {"admitted": True, "lease_id": lease_id, "queued": False, "expired_leases": expired}

        request = queue.get(rid)
        if request is None:
            if len(queue) >= queue_limit():
                raise AdmissionError(
                    "admission_queue_full",
                    "Global agent admission queue is full.",
                    details={"queue_limit": queue_limit()},
                )
            sequence = int(state.get("next_sequence") or 1)
            state["next_sequence"] = sequence + 1
            request = {
                "request_id": rid,
                "sequence": sequence,
                "team_id": str(team_id),
                "task_id": str(task_id),
                "provider": str(provider),
                "resources": claims,
                "status": "queued",
                "enqueued_at": current,
                "updated_at": current,
                "queued_reason": None,
                "queued_details": {},
                "attempts": 0,
            }
            queue[rid] = request
        else:
            # Request identity/resources are immutable while queued.
            if (
                str(request.get("team_id")) != str(team_id)
                or str(request.get("task_id")) != str(task_id)
                or str(request.get("provider")) != str(provider)
                or normalize_claims(request.get("resources") or []) != claims
            ):
                raise AdmissionError("admission_request_mismatch", "Queued admission request identity changed.")

        request["attempts"] = int(request.get("attempts") or 0) + 1
        request["updated_at"] = current
        request["provider_blocked_until"] = (
            float(provider_blocked_until) if provider_blocked_until and float(provider_blocked_until) > current else None
        )
        request["provider_limit_override"] = (
            max(1, int(provider_limit_override)) if provider_limit_override is not None else None
        )
        reason, details = _queued_reason(state, request, provider_blocked_until=provider_blocked_until)
        if reason:
            request["queued_reason"] = reason
            request["queued_details"] = details
            _write_unlocked(locked_root, state)
            return {
                "admitted": False,
                "queued": True,
                "request_id": rid,
                "sequence": int(request["sequence"]),
                "reason": reason,
                "details": details,
                "queued_since": float(request.get("enqueued_at") or current),
                "queue_position": 1 + sum(
                    1 for item in queue.values()
                    if int(item.get("sequence") or 0) < int(request["sequence"])
                ),
                "expired_leases": expired,
            }

        lease_id = "lease_" + uuid.uuid4().hex[:20]
        lease = {
            "lease_id": lease_id,
            "request_id": rid,
            "team_id": str(team_id),
            "task_id": str(task_id),
            "provider": str(provider),
            "resources": claims,
            "agent_id": None,
            "acquired_at": current,
            "last_heartbeat_at": current,
            "expires_at": current + lease_ttl_s(),
        }
        leases[lease_id] = lease
        queue.pop(rid, None)
        _write_unlocked(locked_root, state)
        return {
            "admitted": True,
            "queued": False,
            "request_id": rid,
            "lease_id": lease_id,
            "resources": claims,
            "global_limit": global_limit(),
            "provider_limit": _effective_provider_limit(str(provider), provider_limit_override),
            "expired_leases": expired,
        }


def request_resource_lease(
    root: Path, *, owner_id: str, resources: Optional[Iterable[Mapping[str, Any]]] = None,
    ttl_s: int = 90,
) -> Dict[str, Any]:
    claims = normalize_claims(resources)
    current = _now()
    ttl = max(5, min(int(ttl_s), 600))
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        expired = _prune_expired_unlocked(state, current)
        blockers = _resource_blockers(state, claims)
        if blockers:
            return {
                "admitted": False, "reason": "resource_busy", "blockers": blockers,
                "expired_leases": expired,
            }
        lease_id = "lease_" + uuid.uuid4().hex[:20]
        state.setdefault("leases", {})[lease_id] = {
            "lease_id": lease_id, "request_id": f"computer-plan:{owner_id}",
            "team_id": None, "task_id": None, "provider": "computer_plan",
            "resources": claims, "agent_id": None, "owner_id": str(owner_id),
            "capacity_weight": 0, "acquired_at": current,
            "last_heartbeat_at": current, "expires_at": current + ttl,
        }
        _write_unlocked(locked_root, state)
        return {
            "admitted": True, "lease_id": lease_id, "resources": claims,
            "expired_leases": expired,
        }


def bind_agent(root: Path, lease_id: str, agent_id: str) -> Dict[str, Any]:
    lid = str(lease_id or "").strip()
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        _prune_expired_unlocked(state, _now())
        lease = (state.get("leases") or {}).get(lid)
        if lease is None:
            raise AdmissionError("admission_lease_missing", "Admission lease expired before agent bind.")
        lease["agent_id"] = str(agent_id)
        lease["last_heartbeat_at"] = _now()
        lease["expires_at"] = _now() + lease_ttl_s()
        _write_unlocked(locked_root, state)
        return dict(lease)


def heartbeat(root: Path, *, lease_id: Optional[str] = None, agent_id: Optional[str] = None) -> bool:
    if not lease_id and not agent_id:
        return False
    current = _now()
    found = False
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        _prune_expired_unlocked(state, current)
        for lid, lease in (state.get("leases") or {}).items():
            if lease_id and lid != lease_id:
                continue
            if agent_id and str(lease.get("agent_id") or "") != str(agent_id):
                continue
            lease["last_heartbeat_at"] = current
            lease["expires_at"] = current + lease_ttl_s()
            found = True
        if found:
            _write_unlocked(locked_root, state)
    return found


def cancel_queued(
    root: Path, *, team_id: Optional[str] = None, task_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> int:
    if not any((team_id, task_id, request_id)):
        return 0
    removed = 0
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        queue = state.setdefault("queue", {})
        for rid, request in list(queue.items()):
            if request_id and rid != request_id:
                continue
            if team_id and str(request.get("team_id") or "") != str(team_id):
                continue
            if task_id and str(request.get("task_id") or "") != str(task_id):
                continue
            queue.pop(rid, None)
            removed += 1
        if removed:
            _write_unlocked(locked_root, state)
    return removed


def release(
    root: Path,
    *,
    lease_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    team_id: Optional[str] = None,
    task_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, int]:
    removed_leases = 0
    removed_queue = 0
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        leases = state.setdefault("leases", {})
        queue = state.setdefault("queue", {})
        for lid, lease in list(leases.items()):
            if lease_id and lid != lease_id:
                continue
            if agent_id and str(lease.get("agent_id") or "") != str(agent_id):
                continue
            if team_id and str(lease.get("team_id") or "") != str(team_id):
                continue
            if task_id and str(lease.get("task_id") or "") != str(task_id):
                continue
            if request_id and str(lease.get("request_id") or "") != str(request_id):
                continue
            leases.pop(lid, None)
            removed_leases += 1
        queue_selector = bool(request_id or team_id or task_id) and not bool(agent_id or lease_id)
        if queue_selector:
            for rid, request in list(queue.items()):
                if request_id and rid != request_id:
                    continue
                if team_id and str(request.get("team_id") or "") != str(team_id):
                    continue
                if task_id and str(request.get("task_id") or "") != str(task_id):
                    continue
                queue.pop(rid, None)
                removed_queue += 1
        if removed_leases or removed_queue:
            _write_unlocked(locked_root, state)
    return {"leases": removed_leases, "queue": removed_queue}


def snapshot(root: Path) -> Dict[str, Any]:
    current = _now()
    with _locked(root) as locked_root:
        state = _read_unlocked(locked_root)
        expired = _prune_expired_unlocked(state, current)
        if expired:
            _write_unlocked(locked_root, state)
        total, by_provider = _active_counts(state)
        queued = sorted(
            (dict(item) for item in (state.get("queue") or {}).values()),
            key=lambda item: int(item.get("sequence") or 0),
        )
        leases = [dict(item) for item in (state.get("leases") or {}).values()]
        return {
            "global_active": total,
            "global_limit": global_limit(),
            "provider_active": by_provider,
            "provider_limits": {provider: provider_limit(provider) for provider in sorted(set(by_provider) | {"opencode", "codex", "chatgpt"})},
            "queued_count": len(queued),
            "queued": queued,
            "leases": leases,
            "expired_leases": expired,
            "updated_at": state.get("updated_at"),
        }
