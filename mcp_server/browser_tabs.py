from __future__ import annotations

import contextvars
import hashlib
import os
import subprocess
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, status


_LOCK = threading.RLock()
_REGISTRY: Dict[str, Dict[str, Any]] = {}
_RESOURCE_LOCKS_LOCK = threading.Lock()
_RESOURCE_LOCKS: weakref.WeakValueDictionary[Tuple[str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)
_LOGICAL_LEASES: Dict[str, Dict[str, Any]] = {}
_LEASE_HISTORY: Dict[str, Dict[str, Any]] = {}
_LEASE_LOCK = threading.RLock()
_OWNER_OVERRIDE: contextvars.ContextVar[Optional[tuple[str, Optional[str], Optional[str]]]] = contextvars.ContextVar(
    "mac_mcp_browser_owner_override", default=None
)


@dataclass(frozen=True)
class TabTarget:
    browser: str
    window_index: int
    tab_index: int
    tab_handle: str
    native_id: str
    title: str
    url: str
    lease_generation: int = 0
    logical_owner: Optional[str] = None
    lease_rebound: bool = False
    previous_origin: Optional[str] = None


def _osascript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "AppleScript error").strip())
    return (proc.stdout or "").strip()


def _browser_key(browser: str) -> str:
    key = (browser or "").strip().lower()
    if key == "safari":
        return "Safari"
    if key in {"chrome", "google chrome"}:
        return "Google Chrome"
    return browser


def _resource_lock(browser: str, tab_handle: str) -> threading.RLock:
    key = (_browser_key(browser), str(tab_handle))
    with _RESOURCE_LOCKS_LOCK:
        lock = _RESOURCE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RESOURCE_LOCKS[key] = lock
        return lock




def _lease_ttl_s() -> float:
    try:
        value = float(os.getenv("MAC_MCP_TAB_LEASE_TTL_S", "300"))
    except ValueError:
        value = 300.0
    return max(30.0, min(value, 3600.0))


def _origin(url: Any) -> Optional[str]:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port
    except ValueError:
        return None
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


@contextmanager
def logical_owner_scope(
    owner: Optional[str], *, agent_id: Optional[str] = None, profile: Optional[str] = None,
) -> Iterator[None]:
    token = _OWNER_OVERRIDE.set((str(owner), agent_id, profile) if owner else None)
    try:
        yield
    finally:
        _OWNER_OVERRIDE.reset(token)


def _logical_owner() -> tuple[Optional[str], Optional[str], Optional[str]]:
    override = _OWNER_OVERRIDE.get()
    if override is not None:
        return override
    # Imported lazily to avoid creating a browser_tabs -> policy import cycle at module load.
    try:
        from .policy import current_policy_context
        context = current_policy_context()
    except Exception:
        return None, None, None
    if not context.agent_id:
        return None, None, context.profile
    return f"agent:{context.agent_id}", context.agent_id, context.profile


def _prune_logical_leases_locked(now: Optional[float] = None) -> None:
    current = time.time() if now is None else now
    for handle, lease in list(_LOGICAL_LEASES.items()):
        if float(lease.get("expires_at") or 0) <= current:
            _LEASE_HISTORY[handle] = dict(lease)
            _LOGICAL_LEASES.pop(handle, None)


def _claim_logical_lease(
    row: Dict[str, Any], *, allow_rebind: bool = False, created_by_owner: bool = False,
) -> Dict[str, Any]:
    owner, agent_id, profile = _logical_owner()
    handle = str(row.get("tab_handle") or "")
    if not owner or not handle:
        return {"generation": 0, "owner": owner, "rebound": False, "previous_origin": None}

    now = time.time()
    current_origin = _origin(row.get("url"))
    with _LEASE_LOCK:
        _prune_logical_leases_locked(now)
        active = _LOGICAL_LEASES.get(handle)
        if active is not None and active.get("owner") != owner:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "ok": False,
                    "error": "tab_owned_by_other_agent",
                    "retryable": True,
                    "retry_after_ms": 1000,
                    "tab_handle": handle,
                    "owner": "another_agent",
                    "message": "The tab is still leased by another delegated agent.",
                },
                headers={"Retry-After": "1"},
            )
        if active is not None:
            active["expires_at"] = now + _lease_ttl_s()
            active["last_seen_at"] = now
            active["origin"] = current_origin or active.get("origin")
            active["profile"] = profile or active.get("profile")
            return {
                "generation": int(active.get("generation") or 0),
                "owner": owner,
                "rebound": False,
                "previous_origin": active.get("origin"),
            }

        previous = _LEASE_HISTORY.get(handle)
        if not allow_rebind and not created_by_owner:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "ok": False,
                    "error": "tab_rebind_required",
                    "retryable": True,
                    "tab_handle": handle,
                    "required_action": "browser_observe",
                    "message": "Fresh browser_observe is required before this agent can act on the tab.",
                },
            )
        generation = int((previous or {}).get("generation") or 0) + 1
        lease = {
            "owner": owner,
            "agent_id": agent_id,
            "profile": profile,
            "origin": current_origin,
            "generation": generation,
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + _lease_ttl_s(),
        }
        _LOGICAL_LEASES[handle] = lease
        _LEASE_HISTORY[handle] = dict(lease)
        return {
            "generation": generation,
            "owner": owner,
            "rebound": bool(previous is not None),
            "previous_origin": (previous or {}).get("origin"),
        }


def claim_created_tab(browser: str, tab_handle: Optional[str]) -> Optional[Dict[str, Any]]:
    if not tab_handle:
        return None
    try:
        _, _, row = resolve_tab(browser, str(tab_handle))
    except KeyError:
        return None
    return _claim_logical_lease(row, allow_rebind=True, created_by_owner=True)


def release_agent_leases(agent_id: Optional[str]) -> int:
    if not agent_id:
        return 0
    owner = f"agent:{agent_id}"
    released = 0
    with _LEASE_LOCK:
        _prune_logical_leases_locked()
        for handle, lease in list(_LOGICAL_LEASES.items()):
            if lease.get("owner") != owner:
                continue
            _LEASE_HISTORY[handle] = dict(lease)
            _LOGICAL_LEASES.pop(handle, None)
            released += 1
    return released


def logical_lease_snapshot() -> Dict[str, Dict[str, Any]]:
    with _LEASE_LOCK:
        _prune_logical_leases_locked()
        return {handle: dict(lease) for handle, lease in _LOGICAL_LEASES.items()}


def preferred_window_for_owner(browser: str) -> Optional[int]:
    """Return the one browser window currently associated with this logical owner.

    Multiple owned windows are intentionally ambiguous and return ``None`` so callers
    do not guess. The tab registry is refreshed before consulting active leases.
    """
    owner, _, _ = _logical_owner()
    if not owner:
        return None
    rows = list_tabs(browser)
    by_handle = {str(row.get("tab_handle") or ""): row for row in rows}
    app = _browser_key(browser)
    with _LEASE_LOCK:
        _prune_logical_leases_locked()
        owned = [
            handle for handle, lease in _LOGICAL_LEASES.items()
            if lease.get("owner") == owner
        ]
    windows = {
        int(by_handle[handle]["window_index"])
        for handle in owned
        if handle in by_handle and _browser_key(str(by_handle[handle].get("browser") or "")) == app
    }
    if len(windows) == 1:
        return next(iter(windows))
    return None


def _scan(browser: str) -> List[Dict[str, Any]]:
    app = _browser_key(browser)
    if app == "Safari":
        script = r'''
set out to ""
tell application "Safari"
    set wCount to count of windows
    repeat with wi from 1 to wCount
        tell window wi
            set cur to index of current tab
            set tCount to count of tabs
            repeat with ti from 1 to tCount
                set t to tab ti
                set p to 0
                try
                    set p to pid of t
                end try
                set out to out & wi & "\t" & ti & "\t" & (ti = cur) & "\t" & p & "\t" & (name of t) & "\t" & (URL of t) & "\n"
            end repeat
        end tell
    end repeat
end tell
return out
'''
    else:
        script = r'''
set out to ""
tell application "Google Chrome"
    set wCount to count of windows
    repeat with wi from 1 to wCount
        tell window wi
            set cur to active tab index
            set tCount to count of tabs
            repeat with ti from 1 to tCount
                set t to tab ti
                set out to out & wi & "\t" & ti & "\t" & (ti = cur) & "\t" & (id of t) & "\t" & (title of t) & "\t" & (URL of t) & "\n"
            end repeat
        end tell
    end repeat
end tell
return out
'''

    rows: List[Dict[str, Any]] = []
    for line in _osascript(script).splitlines():
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        wi, ti, active, native_id, title, url = parts
        rows.append(
            {
                "browser": app,
                "window_index": int(wi),
                "tab_index": int(ti),
                "active": active.strip().lower() == "true",
                "native_id": native_id.strip(),
                "title": title,
                "url": url,
            }
        )
    return rows


def _chrome_handle(native_id: str) -> str:
    digest = hashlib.sha1(str(native_id).encode("utf-8")).hexdigest()[:16]
    return f"btab_chrome_{digest}"


def _new_safari_handle() -> str:
    return f"btab_safari_{uuid.uuid4().hex[:16]}"


def _best_existing_safari(row: Dict[str, Any], used: set[str]) -> Optional[str]:
    pid = str(row.get("native_id") or "")
    candidates = [
        (handle, record)
        for handle, record in _REGISTRY.items()
        if record.get("browser") == "Safari" and handle not in used
    ]

    if pid and pid != "0":
        for handle, record in candidates:
            if str(record.get("native_id") or "") == pid:
                return handle
        # A real Safari WebContent identity changed. Never resurrect an old handle
        # from URL/title/index heuristics; explicit guarded navigation may rebind it.
        return None

    url = str(row.get("url") or "")
    title = str(row.get("title") or "")
    exact = [(h, r) for h, r in candidates if r.get("url") == url and r.get("title") == title]
    if len(exact) == 1:
        return exact[0][0]

    same_url = [(h, r) for h, r in candidates if url and r.get("url") == url]
    if len(same_url) == 1:
        return same_url[0][0]

    # Only fall back to the old location when Safari did not expose a WebContent PID.
    # If a PID exists but is new, location matching would incorrectly steal the handle
    # from the tab that used to occupy that index after the user inserts/reorders tabs.
    if not pid or pid == "0":
        for handle, record in candidates:
            if (
                record.get("window_index") == row.get("window_index")
                and record.get("tab_index") == row.get("tab_index")
            ):
                return handle
    return None


def _retire_logical_lease(handle: str) -> None:
    with _LEASE_LOCK:
        lease = _LOGICAL_LEASES.pop(str(handle), None)
        if lease is not None:
            _LEASE_HISTORY[str(handle)] = dict(lease)


def list_tabs(browser: str) -> List[Dict[str, Any]]:
    rows = _scan(browser)
    app = _browser_key(browser)
    stale_handles: List[str] = []
    with _LOCK:
        used: set[str] = set()
        for row in rows:
            if app == "Google Chrome":
                handle = _chrome_handle(str(row.get("native_id") or ""))
            else:
                handle = _best_existing_safari(row, used) or _new_safari_handle()
            used.add(handle)
            record = dict(row)
            record["tab_handle"] = handle
            _REGISTRY[handle] = record
            row["tab_handle"] = handle
        stale_handles = [
            handle for handle, record in list(_REGISTRY.items())
            if record.get("browser") == app and handle not in used
        ]
        for handle in stale_handles:
            _REGISTRY.pop(handle, None)
    for handle in stale_handles:
        _retire_logical_lease(handle)
    return rows


def rebind_safari_handle(tab_handle: str, row: Dict[str, Any]) -> Dict[str, Any]:
    handle = str(tab_handle or "").strip()
    if not handle or _browser_key(str(row.get("browser") or "Safari")) != "Safari":
        raise ValueError("Safari tab handle and row are required")
    conflicting: List[str] = []
    native_id = str(row.get("native_id") or "")
    with _LOCK:
        if native_id and native_id != "0":
            conflicting = [
                other for other, record in _REGISTRY.items()
                if other != handle and record.get("browser") == "Safari"
                and str(record.get("native_id") or "") == native_id
            ]
            for other in conflicting:
                _REGISTRY.pop(other, None)
        record = dict(row)
        record["browser"] = "Safari"
        record["tab_handle"] = handle
        _REGISTRY[handle] = record
    for other in conflicting:
        _retire_logical_lease(other)
    return record


def resolve_tab(browser: str, tab_handle: str) -> Tuple[int, int, Dict[str, Any]]:
    handle = str(tab_handle or "").strip()
    if not handle:
        raise KeyError("tab_handle is empty")
    for row in list_tabs(browser):
        if row.get("tab_handle") == handle:
            return int(row["window_index"]), int(row["tab_index"]), row
    raise KeyError(f"Unknown or closed tab_handle: {handle}")


def resolve_location(
    browser: str,
    window_index: int,
    tab_index: Optional[int],
) -> Tuple[int, int, Dict[str, Any]]:
    wi = int(window_index)
    rows = list_tabs(browser)
    if tab_index is None:
        match = next(
            (row for row in rows if int(row["window_index"]) == wi and bool(row.get("active"))),
            None,
        )
    else:
        ti = int(tab_index)
        match = next(
            (
                row
                for row in rows
                if int(row["window_index"]) == wi and int(row["tab_index"]) == ti
            ),
            None,
        )
    if match is None:
        target = "active tab" if tab_index is None else f"tab {tab_index}"
        raise KeyError(f"Unknown or closed {target} in window {window_index}")
    return int(match["window_index"]), int(match["tab_index"]), match


def _target_from_row(row: Dict[str, Any], lease: Optional[Dict[str, Any]] = None) -> TabTarget:
    lease = lease or {}
    return TabTarget(
        browser=_browser_key(str(row.get("browser") or "")),
        window_index=int(row["window_index"]),
        tab_index=int(row["tab_index"]),
        tab_handle=str(row["tab_handle"]),
        native_id=str(row.get("native_id") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        lease_generation=int(lease.get("generation") or 0),
        logical_owner=lease.get("owner"),
        lease_rebound=bool(lease.get("rebound")),
        previous_origin=lease.get("previous_origin"),
    )


@contextmanager
def tab_lease(
    browser: str,
    tab_handle: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    *,
    allow_rebind: bool = False,
) -> Iterator[TabTarget]:
    """Exclusively lease one logical tab without queueing competing callers.

    Index-only callers are first bound to the stable handle currently occupying that
    location. Re-entrant acquisition from the same thread is allowed for nested browser
    helpers, while another thread fails immediately with a retryable ``tab_busy`` 409.
    """
    handle = str(tab_handle or "").strip()
    if not handle:
        _, _, row = resolve_location(browser, window_index, tab_index)
        handle = str(row["tab_handle"])

    lock = _resource_lock(browser, handle)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "error": "tab_busy",
                "retryable": True,
                "retry_after_ms": 1000,
                "tab_handle": handle,
                "message": "This browser tab is currently in use by another caller.",
            },
            headers={"Retry-After": "1"},
        )
    try:
        _, _, row = resolve_tab(browser, handle)
        lease = _claim_logical_lease(row, allow_rebind=allow_rebind)
        yield _target_from_row(row, lease)
    finally:
        lock.release()


def handle_for_location(browser: str, window_index: int, tab_index: int) -> Optional[str]:
    for row in list_tabs(browser):
        if int(row["window_index"]) == int(window_index) and int(row["tab_index"]) == int(tab_index):
            return str(row.get("tab_handle") or "") or None
    return None


def find_created(browser: str, window_index: int, tab_index: int) -> Optional[Dict[str, Any]]:
    for row in list_tabs(browser):
        if int(row["window_index"]) == int(window_index) and int(row["tab_index"]) == int(tab_index):
            return row
    return None


def forget(tab_handle: Optional[str]) -> None:
    if not tab_handle:
        return
    handle = str(tab_handle)
    with _LOCK:
        _REGISTRY.pop(handle, None)
    _retire_logical_lease(handle)


def registry_snapshot() -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        return {key: dict(value) for key, value in _REGISTRY.items()}
