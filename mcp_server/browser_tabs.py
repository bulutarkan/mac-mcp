from __future__ import annotations

import hashlib
import subprocess
import threading
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple


_LOCK = threading.RLock()
_REGISTRY: Dict[str, Dict[str, Any]] = {}
_RESOURCE_LOCKS_LOCK = threading.Lock()
_RESOURCE_LOCKS: weakref.WeakValueDictionary[Tuple[str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
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


def list_tabs(browser: str) -> List[Dict[str, Any]]:
    rows = _scan(browser)
    app = _browser_key(browser)
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
    return rows


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


def _target_from_row(row: Dict[str, Any]) -> TabTarget:
    return TabTarget(
        browser=_browser_key(str(row.get("browser") or "")),
        window_index=int(row["window_index"]),
        tab_index=int(row["tab_index"]),
        tab_handle=str(row["tab_handle"]),
        native_id=str(row.get("native_id") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
    )


@contextmanager
def tab_lease(
    browser: str,
    tab_handle: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
) -> Iterator[TabTarget]:
    """Serialize work for one logical tab and refresh its location after waiting.

    Index-only callers are first bound to the stable handle currently occupying that
    location. Once the per-handle lock is acquired, the handle is resolved again so a
    tab move while waiting cannot redirect the operation to its old index.
    """
    handle = str(tab_handle or "").strip()
    if not handle:
        _, _, row = resolve_location(browser, window_index, tab_index)
        handle = str(row["tab_handle"])

    lock = _resource_lock(browser, handle)
    with lock:
        _, _, row = resolve_tab(browser, handle)
        yield _target_from_row(row)


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
    with _LOCK:
        _REGISTRY.pop(str(tab_handle), None)


def registry_snapshot() -> Dict[str, Dict[str, Any]]:
    with _LOCK:
        return {key: dict(value) for key, value in _REGISTRY.items()}
