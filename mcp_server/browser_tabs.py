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

from . import applescript_host

from .workspace_arbitration import (
    browser_human_takeover,
    claim_delegated_resource,
    recent_user_input,
)


_LOCK = threading.RLock()
_REGISTRY: Dict[str, Dict[str, Any]] = {}
# Per-thread record of the tab rows already resolved by an outer tab_lease. Nested
# helpers in the same transaction reuse it instead of rescanning every tab; each
# AppleScript still re-checks the tab's native identity before it runs.
_SCOPE = threading.local()


def _scope_entries() -> Dict[Tuple[str, str], Dict[str, Any]]:
    entries = getattr(_SCOPE, "entries", None)
    if entries is None:
        entries = {}
        _SCOPE.entries = entries
    return entries


def is_tab_identity_failure(exc: BaseException) -> bool:
    """True when an AppleScript identity guard refused a tab before running anything."""
    if not isinstance(exc, HTTPException) or exc.status_code != status.HTTP_409_CONFLICT:
        return False
    detail = exc.detail
    if isinstance(detail, dict):
        return detail.get("error") == "tab_target_closed"
    return str(detail or "").startswith("Target tab identity changed")


def scoped_row(browser: str, tab_handle: str) -> Optional[Dict[str, Any]]:
    entry = _scope_entries().get((_browser_key(browser), str(tab_handle or "").strip()))
    row = entry.get("row") if entry else None
    return dict(row) if row else None


def invalidate_scoped_tab(browser: str, tab_handle: str) -> None:
    entry = _scope_entries().get((_browser_key(browser), str(tab_handle or "").strip()))
    if entry is not None:
        entry["row"] = None
_RESOURCE_LOCKS_LOCK = threading.Lock()
_RESOURCE_LOCKS: weakref.WeakValueDictionary[Tuple[str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)
_LOGICAL_LEASES: Dict[str, Dict[str, Any]] = {}
# Safari exposes no stable tab id; its WebContent pid changes when a cross-site
# navigation commits (process swap), seconds after Mac MCP started it.
_EXPECTED_NAVIGATIONS: Dict[str, Dict[str, Any]] = {}
_EXPECTED_NAVIGATION_TTL_S = 20.0
_LEASE_HISTORY: Dict[str, Dict[str, Any]] = {}
_LEASE_LOCK = threading.RLock()
_OWNER_OVERRIDE: contextvars.ContextVar[Optional[tuple[str, Optional[str], Optional[str]]]] = contextvars.ContextVar(
    "mac_mcp_browser_owner_override", default=None
)


class AmbiguousTabHandleError(KeyError):
    """A stale Safari handle cannot be rebound to one unique current tab."""


@dataclass(frozen=True)
class TabTarget:
    browser: str
    window_index: int
    tab_index: int
    tab_handle: str
    native_id: str
    title: str
    url: str
    active: bool = False
    lease_generation: int = 0
    logical_owner: Optional[str] = None
    lease_rebound: bool = False
    previous_origin: Optional[str] = None


_SCAN_ATTEMPTS = 3
# AppleScript "Invalid index" and "Can't get" errors from a tab list changing mid-scan.
_SCAN_RACE_ERRORS = ("(-1719)", "(-1728)")


def _osascript(script: str) -> str:
    # Tab scans are read-only; the reusable host avoids launching osascript each time.
    try:
        ok, stdout, stderr = applescript_host.run(script, 30)
    except applescript_host.HostUnavailable:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok, stdout, stderr = proc.returncode == 0, proc.stdout, proc.stderr
    except applescript_host.HostTimeout as exc:
        raise subprocess.TimeoutExpired(["osascript"], 30) from exc
    if not ok:
        raise RuntimeError((stderr or stdout or "AppleScript error").strip())
    return (stdout or "").strip()


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
    team_id = None
    try:
        from .policy import current_policy_context
        team_id = str(current_policy_context().team_id or "").strip() or None
    except Exception:
        team_id = None
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
            active["team_id"] = team_id or active.get("team_id")
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
            "team_id": team_id,
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
            set cur to 0
            try
                set cur to index of current tab
            end try
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
            set cur to 0
            try
                set cur to active tab index
            end try
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

    # The scan walks tabs by position, so a tab opened or closed by another agent
    # mid-scan surfaces as an index error. Re-reading is side-effect free.
    for attempt in range(_SCAN_ATTEMPTS):
        try:
            raw = _osascript(script)
            break
        except RuntimeError as exc:
            if attempt == _SCAN_ATTEMPTS - 1 or not any(code in str(exc) for code in _SCAN_RACE_ERRORS):
                raise
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
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
    if len(exact) > 1:
        return None

    same_url = [(h, r) for h, r in candidates if url and r.get("url") == url]
    if len(same_url) == 1:
        return same_url[0][0]
    if len(same_url) > 1:
        return None

    # Window/tab position is not stable Safari identity. If current semantic
    # metadata exists but does not match, a newly inserted or reordered tab may
    # now occupy the old index; never steal an existing handle by position.
    if url or title:
        return None

    # With no PID and no semantic metadata, reuse is safe only when exactly one
    # prior Safari candidate exists. Multiple anonymous tabs are ambiguous.
    if len(candidates) == 1:
        return candidates[0][0]
    return None


def _site(url: Any) -> Optional[str]:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    labels = host.split(".")
    if host.replace(".", "").isdigit() or len(labels) <= 2:
        return host
    tail = labels[-2:]
    # Two-label public suffixes such as com.tr or co.uk keep one more label.
    if len(tail[0]) <= 3 and len(tail[1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(tail)


def expect_safari_navigation(tab_handle: Optional[str], *, expected_url: Optional[str] = None) -> None:
    """Allow one guarded handle rebind if this tab's Safari process swaps soon.

    Called right after Mac MCP itself navigated or mutated the tab. The rebind
    in list_tabs still requires the same window and position, an unchanged tab
    count in that window, and the old pid gone from every tab.
    """
    handle = str(tab_handle or "").strip()
    if not handle:
        return
    with _LOCK:
        record = _REGISTRY.get(handle)
        if not record or record.get("browser") != "Safari":
            return
        window_index = int(record.get("window_index") or 0)
        _EXPECTED_NAVIGATIONS[handle] = {
            "native_id": str(record.get("native_id") or ""),
            "window_index": window_index,
            "tab_index": int(record.get("tab_index") or 0),
            "window_tab_count": sum(
                1 for other in _REGISTRY.values()
                if other.get("browser") == "Safari" and int(other.get("window_index") or 0) == window_index
            ),
            "site": _site(expected_url) if expected_url else None,
            "expires_at": time.monotonic() + _EXPECTED_NAVIGATION_TTL_S,
        }


def _expected_navigation_handle(row: Dict[str, Any], rows: List[Dict[str, Any]], used: set[str]) -> Optional[str]:
    now = time.monotonic()
    for handle, marker in list(_EXPECTED_NAVIGATIONS.items()):
        if float(marker.get("expires_at") or 0) <= now or handle not in _REGISTRY:
            _EXPECTED_NAVIGATIONS.pop(handle, None)
    current_pids = {str(item.get("native_id") or "") for item in rows}
    window_index = int(row.get("window_index") or 0)
    window_tab_count = sum(1 for item in rows if int(item.get("window_index") or 0) == window_index)
    row_site = _site(row.get("url"))
    matches = [
        handle for handle, marker in _EXPECTED_NAVIGATIONS.items()
        if handle not in used
        and marker.get("native_id") not in current_pids
        and int(marker.get("window_index") or 0) == window_index
        and int(marker.get("tab_index") or 0) == int(row.get("tab_index") or 0)
        and int(marker.get("window_tab_count") or 0) == window_tab_count
        and (not marker.get("site") or marker.get("site") == row_site)
    ]
    if len(matches) != 1:
        return None
    handle = matches[0]
    _EXPECTED_NAVIGATIONS[handle]["native_id"] = str(row.get("native_id") or "")
    return handle


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
                handle = (
                    _best_existing_safari(row, used)
                    or _expected_navigation_handle(row, rows, used)
                    or _new_safari_handle()
                )
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


def _safari_rebind_is_ambiguous(previous: Dict[str, Any], rows: List[Dict[str, Any]]) -> bool:
    native_id = str(previous.get("native_id") or "")
    if native_id and native_id != "0":
        return False
    url = str(previous.get("url") or "")
    title = str(previous.get("title") or "")
    exact = [row for row in rows if str(row.get("url") or "") == url and str(row.get("title") or "") == title]
    if len(exact) > 1:
        return True
    if len(exact) == 1:
        return False
    if url:
        same_url = [row for row in rows if str(row.get("url") or "") == url]
        if len(same_url) > 1:
            return True
    if not url and not title:
        anonymous = [
            row for row in rows
            if not str(row.get("native_id") or "") or str(row.get("native_id") or "") == "0"
        ]
        return len(anonymous) > 1
    return False


def resolve_tab(browser: str, tab_handle: str) -> Tuple[int, int, Dict[str, Any]]:
    handle = str(tab_handle or "").strip()
    if not handle:
        raise KeyError("tab_handle is empty")
    app = _browser_key(browser)
    with _LOCK:
        previous = dict(_REGISTRY.get(handle) or {})
    rows = list_tabs(browser)
    for row in rows:
        if row.get("tab_handle") == handle:
            return int(row["window_index"]), int(row["tab_index"]), row
    if app == "Safari" and previous and _safari_rebind_is_ambiguous(previous, rows):
        raise AmbiguousTabHandleError(f"Ambiguous Safari tab_handle after refresh: {handle}")
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
        active=bool(row.get("active")),
        lease_generation=int(lease.get("generation") or 0),
        logical_owner=lease.get("owner"),
        lease_rebound=bool(lease.get("rebound")),
        previous_origin=lease.get("previous_origin"),
    )


def revalidate_mutation_lease(
    browser: str,
    tab_handle: str,
    expected_generation: Optional[int],
) -> Tuple[Optional[TabTarget], Optional[Dict[str, Any]]]:
    """Revalidate one delegated browser mutation immediately before its side effect.

    Return the freshly resolved target on success so the caller can execute the
    side effect against that exact identity without performing a second tab scan.
    """
    owner, _, _ = _logical_owner()
    if not owner:
        return None, None

    handle = str(tab_handle or "").strip()
    if not handle:
        return None, {
            "ok": False,
            "error": "stale_tab_handle",
            "reason_code": "STALE_TAB_HANDLE",
            "retryable": True,
            "observe_again": True,
            "resource_kind": "browser_tab",
            "message": "The browser tab identity is unavailable; observe the target tab again before mutating it.",
        }

    try:
        _, _, row = resolve_tab(browser, handle)
    except AmbiguousTabHandleError:
        return None, {
            "ok": False,
            "error": "ambiguous_tab_handle",
            "reason_code": "AMBIGUOUS_TAB_HANDLE",
            "retryable": True,
            "observe_again": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "message": "The browser tab identity became ambiguous; observe the target tab again before mutating it.",
        }
    except KeyError:
        return None, {
            "ok": False,
            "error": "stale_tab_handle",
            "reason_code": "STALE_TAB_HANDLE",
            "retryable": True,
            "observe_again": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "message": "The browser tab changed or closed; observe the target tab again before mutating it.",
        }

    human = browser_human_takeover(browser, row)
    if human is not None:
        reason_code = str(human.get("reason_code") or "HUMAN_ACTIVE_RESOURCE")
        result: Dict[str, Any] = {
            "ok": False,
            "error": reason_code.lower(),
            "reason_code": reason_code,
            "retryable": bool(human.get("retryable", True)),
            "human_priority": True,
            "yielded": True,
            "human_takeover_during_action": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "message": (
                "The user took ownership of this browser tab while the delegated "
                "transaction was running; the agent yielded before the next mutation."
            ),
        }
        if reason_code == "HUMAN_ACTIVE_RESOURCE":
            recent, probe_error, age = recent_user_input()
            if recent is not None:
                result["human_input_recent"] = bool(recent)
            if age is not None and age != float("inf"):
                result["human_input_age_ms"] = int(max(0.0, age) * 1000)
            if probe_error:
                result["human_input_probe_error"] = probe_error
        elif human.get("probe_error"):
            result["probe_error"] = human.get("probe_error")
        return None, result

    now = time.time()
    with _LEASE_LOCK:
        _prune_logical_leases_locked(now)
        active = _LOGICAL_LEASES.get(handle)
        if active is None:
            lease = None
        else:
            lease = dict(active)

        if lease is not None and lease.get("owner") == owner:
            actual_generation = int(lease.get("generation") or 0)
            if expected_generation is None or int(expected_generation) == actual_generation:
                active["last_seen_at"] = now
                active["expires_at"] = now + _lease_ttl_s()
                lease = dict(active)

    if lease is None:
        return None, {
            "ok": False,
            "error": "stale_tab_lease",
            "reason_code": "STALE_TAB_LEASE",
            "retryable": True,
            "observe_again": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "expected_lease_generation": expected_generation,
            "actual_lease_generation": None,
            "message": "The browser tab lease expired or was released; observe the target tab again before mutating it.",
        }

    if lease.get("owner") != owner:
        return None, {
            "ok": False,
            "error": "tab_owned_by_other_agent",
            "reason_code": "TAB_OWNED_BY_OTHER_AGENT",
            "retryable": True,
            "yielded": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "message": "Another delegated agent owns this browser tab; this transaction yielded before mutation.",
        }

    actual_generation = int(lease.get("generation") or 0)
    if expected_generation is not None and int(expected_generation) != actual_generation:
        return None, {
            "ok": False,
            "error": "stale_tab_lease",
            "reason_code": "STALE_TAB_LEASE",
            "retryable": True,
            "observe_again": True,
            "resource_kind": "browser_tab",
            "tab_handle": handle,
            "expected_lease_generation": int(expected_generation),
            "actual_lease_generation": actual_generation,
            "message": "The browser tab lease generation changed; observe the target tab again before mutating it.",
        }

    return _target_from_row(row, lease), None


@contextmanager
def tab_lease(
    browser: str,
    tab_handle: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    *,
    allow_rebind: bool = False,
    mutation: bool = False,
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
    key = (_browser_key(browser), handle)
    entries = _scope_entries()
    entry = entries.get(key)
    try:
        cached = entry.get("row") if entry else None
        if cached is not None and (entry["mutation_checked"] or not mutation):
            row, lease = cached, entry["lease"]
        else:
            row, lease = _lease_fresh_row(browser, handle, allow_rebind=allow_rebind, mutation=mutation)
            if entry is None:
                entry = {"row": row, "lease": lease, "mutation_checked": mutation, "depth": 0}
                entries[key] = entry
            else:
                entry.update(row=row, lease=lease, mutation_checked=entry["mutation_checked"] or mutation)
        entry["depth"] += 1
        try:
            yield _target_from_row(row, lease)
        except HTTPException as exc:
            if is_tab_identity_failure(exc):
                entry["row"] = None
            raise
        finally:
            entry["depth"] -= 1
            if entry["depth"] <= 0:
                entries.pop(key, None)
    finally:
        lock.release()


def _lease_fresh_row(
    browser: str, handle: str, *, allow_rebind: bool, mutation: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _, _, row = resolve_tab(browser, handle)
    if mutation:
        human = browser_human_takeover(browser, row)
        if human is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "ok": False,
                    "error": str(human.get("reason_code") or "HUMAN_ACTIVE_RESOURCE").lower(),
                    "reason_code": human.get("reason_code"),
                    "retryable": bool(human.get("retryable", True)),
                    "retry_after_ms": 750,
                    "human_priority": True,
                    "yielded": True,
                    "resource_kind": "browser_tab",
                    "message": (
                        "The user is currently on this browser tab. "
                        "The delegated agent yielded instead of mutating the visible resource."
                    ),
                },
                headers={"Retry-After": "1"},
            )
        arbitration = claim_delegated_resource(
            "browser_tab",
            str(row.get("tab_handle") or handle),
            mode="write",
        )
        if arbitration is not None and not arbitration.get("ok"):
            reason_code = str(
                arbitration.get("reason_code") or "RESOURCE_BUSY"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "ok": False,
                    "error": reason_code.lower(),
                    "reason_code": reason_code,
                    "retryable": bool(arbitration.get("retryable", True)),
                    "retry_after_ms": 750,
                    "yielded": True,
                    "resource_kind": "browser_tab",
                    "message": (
                        "Another agent owns this browser tab resource. "
                        "The delegated action yielded before mutation."
                    ),
                },
                headers={"Retry-After": "1"},
            )
    lease = _claim_logical_lease(row, allow_rebind=allow_rebind)
    return row, lease


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
