from __future__ import annotations

import hashlib
import re
import subprocess
import threading
import time
from collections import Counter
from typing import Any, Dict, Iterable, Optional

_APP_HANDLE_RE = re.compile(r"^mapp_[0-9a-f]{20}$")
_WINDOW_HANDLE_RE = re.compile(r"^mwin_[0-9a-f]{24}$")
_MAX_REGISTRY = 512
_REGISTRY_TTL_S = 3600.0
_LOCK = threading.RLock()
_APP_REGISTRY: Dict[str, Dict[str, Any]] = {}
_WINDOW_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _digest(prefix: str, parts: Iterable[Any], length: int) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    token = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:length]
    return f"{prefix}{token}"


def app_handle(
    app_name: str, pid: int, bundle_id: str = "", process_token: str = "",
) -> str:
    return _digest("mapp_", ("app-v1", bundle_id or app_name, int(pid), process_token), 20)


def _process_instance_token(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def _identity_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"missing value", "null", "none", "<null>"}:
        return ""
    return text


def _window_candidates(window: Dict[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    document = _identity_text(window.get("document"))
    if document:
        candidates.append(("document", document))

    identifier = _identity_text(window.get("identifier"))
    if identifier:
        candidates.append(("identifier", identifier))

    title = _identity_text(window.get("title"))
    subrole = str(window.get("subrole") or "").strip()
    if title:
        candidates.append(("title", f"{subrole}|{title}"))

    position = window.get("position") if isinstance(window.get("position"), dict) else {}
    try:
        frame = tuple(int(position.get(name)) for name in ("x", "y", "width", "height"))
    except (TypeError, ValueError):
        frame = ()
    if frame and frame != (0, 0, 0, 0):
        candidates.append(("fingerprint", "|".join((title, subrole, *(str(value) for value in frame)))))
    return candidates


def _choose_window_identity(
    candidates: list[tuple[str, str]], counts: Counter[tuple[str, str]],
) -> tuple[Optional[str], Optional[str]]:
    for candidate in candidates:
        if counts[candidate] == 1:
            return candidate
    return None, None


def _prune_locked(now: Optional[float] = None) -> None:
    current = time.time() if now is None else now
    for registry in (_APP_REGISTRY, _WINDOW_REGISTRY):
        for handle, row in list(registry.items()):
            if current - float(row.get("seen_at") or 0) > _REGISTRY_TTL_S:
                registry.pop(handle, None)
        if len(registry) > _MAX_REGISTRY:
            oldest = sorted(registry.items(), key=lambda item: float(item[1].get("seen_at") or 0))
            for handle, _ in oldest[: len(registry) - _MAX_REGISTRY]:
                registry.pop(handle, None)


def decorate_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Add deterministic process/window handles to parsed AX metadata.

    Window handles are issued only when the strongest available identity is unique
    inside the process. Ambiguous windows intentionally receive no handle so callers
    can fail closed instead of targeting a same-looking sibling window by index.
    """
    result = dict(metadata)
    app_name = str(result.get("active_app") or "").strip()
    try:
        pid = int(result.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    bundle_id = str(result.get("bundle_id") or "").strip()
    windows = [dict(row) for row in (result.get("windows") or []) if isinstance(row, dict)]

    if pid <= 0 or not app_name:
        result["app_handle"] = None
        result["windows"] = windows
        return result

    process_token = _process_instance_token(pid)
    app_id = app_handle(app_name, pid, bundle_id, process_token)
    candidate_sets = [_window_candidates(row) for row in windows]
    identity_counts: Counter[tuple[str, str]] = Counter(
        candidate for candidates in candidate_sets for candidate in candidates
    )
    identities: list[tuple[Optional[str], Optional[str]]] = [
        _choose_window_identity(candidates, identity_counts) for candidates in candidate_sets
    ]
    now = time.time()
    public_windows: list[Dict[str, Any]] = []

    with _LOCK:
        _prune_locked(now)
        _APP_REGISTRY[app_id] = {
            "app_name": app_name,
            "pid": pid,
            "bundle_id": bundle_id,
            "process_token": process_token,
            "seen_at": now,
        }
        for row, candidates, (kind, value) in zip(windows, candidate_sets, identities):
            handle: Optional[str] = None
            status = "ambiguous" if candidates else "unavailable"
            if kind is not None and value is not None:
                if identity_counts[(kind, value)] == 1:
                    handle = _digest("mwin_", ("window-v1", app_id, kind, value), 24)
                    status = "stable" if kind in {"document", "identifier", "title"} else "conservative_fingerprint"
                    _WINDOW_REGISTRY[handle] = {
                        "app_handle": app_id,
                        "app_name": app_name,
                        "pid": pid,
                        "bundle_id": bundle_id,
                        "process_token": process_token,
                        "seen_at": now,
                    }
                else:
                    status = "ambiguous"
            row["window_handle"] = handle
            row["identity_kind"] = kind
            row["identity_status"] = status
            public_windows.append(row)
        _prune_locked(now)

    result["app_handle"] = app_id
    result["windows"] = public_windows
    return result


def lookup_app(handle: str) -> Optional[Dict[str, Any]]:
    if not isinstance(handle, str) or not _APP_HANDLE_RE.fullmatch(handle):
        return None
    with _LOCK:
        _prune_locked()
        row = _APP_REGISTRY.get(handle)
        return dict(row) if row else None


def lookup_window(handle: str) -> Optional[Dict[str, Any]]:
    if not isinstance(handle, str) or not _WINDOW_HANDLE_RE.fullmatch(handle):
        return None
    with _LOCK:
        _prune_locked()
        row = _WINDOW_REGISTRY.get(handle)
        return dict(row) if row else None


def window_by_handle(metadata: Dict[str, Any], handle: str) -> Optional[Dict[str, Any]]:
    for row in metadata.get("windows") or []:
        if isinstance(row, dict) and row.get("window_handle") == handle:
            return row
    return None


def window_handle_map(metadata: Dict[str, Any]) -> Dict[int, Optional[str]]:
    result: Dict[int, Optional[str]] = {}
    for row in metadata.get("windows") or []:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if index > 0:
            result[index] = row.get("window_handle") if isinstance(row.get("window_handle"), str) else None
    return result


def rebase_element_id(element_id: str, window_index: int) -> str:
    parts = str(element_id).split("/")
    if not parts or not parts[0].startswith("w"):
        return str(element_id)
    parts[0] = f"w{int(window_index)}"
    return "/".join(parts)


def public_window_rows(metadata: Dict[str, Any]) -> list[Dict[str, Any]]:
    public: list[Dict[str, Any]] = []
    for row in metadata.get("windows") or []:
        if not isinstance(row, dict):
            continue
        public.append({
            "index": row.get("index"),
            "title": row.get("title") or "",
            "position": dict(row.get("position") or {}),
            "window_handle": row.get("window_handle"),
            "identity_kind": row.get("identity_kind"),
            "identity_status": row.get("identity_status"),
        })
    return public


def reset_registries_for_tests() -> None:
    with _LOCK:
        _APP_REGISTRY.clear()
        _WINDOW_REGISTRY.clear()
