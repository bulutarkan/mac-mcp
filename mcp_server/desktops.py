"""Mission Control desktops (macOS Spaces) for an agent asked to work apart from the owner.

By default agents work alongside the owner on the owner's desktops. Only when the owner
asks for it does an agent take a desktop of its own: it reads which displays and desktops
exist and what is open on each, picks an empty one (the owner adds desktops in Mission
Control, or the agent adds one), and opens apps straight onto it, so nothing appears on a
desktop the owner is looking at and focus stays where it is.

The work happens in spaces_helper.swift, compiled on first use and cached by source hash.
An app with no windows on the owner's desktops is assigned to the agent's desktop while its
window is made; one that has some (the owner's Safari) is not, since assigning would move
those too: its new window is caught before it is drawn and moved. Chrome brings itself to
the front for every window opened from outside, so its window comes from the Mac MCP
Chrome Companion (chrome.windows.create with focused=false), by either route.
"""
from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import swift_build

_SOURCE = Path(__file__).resolve().with_name("spaces_helper.swift")
_build_lock = threading.Lock()
_executable: Optional[str] = None

CHROME_BUNDLE_ID = "com.google.Chrome"
_CHROME_NAMES = {"chrome", "google chrome", CHROME_BUNDLE_ID.lower()}
# Launched with no document, TextEdit shows its Open panel instead of a page to write on,
# and the panel's New Document button does not respond to Accessibility in the background.
_TEXTEDIT_NAMES = {"textedit", "com.apple.textedit", "/system/applications/textedit.app"}
_AGENT_DOCUMENTS = Path("~/.mac-mcp/agent-desktop").expanduser()


def _helper() -> str:
    global _executable
    with _build_lock:
        if _executable is None:
            directory = swift_build.cache_dir("MAC_MCP_SPACES_CACHE", "~/.mac-mcp/cache/spaces-helper")
            _executable = str(swift_build.compile_cached(_SOURCE, directory, "spaces-helper"))
        return _executable


def _run(args: List[str], timeout_s: float = 15.0) -> Dict[str, Any]:
    try:
        executable = _helper()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"ok": False, "error": f"the desktops helper could not be built: {exc}", "reason_code": "SPACES_HELPER_UNAVAILABLE"}
    try:
        proc = subprocess.run(
            [executable, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=max(1.0, float(timeout_s)), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the desktops helper timed out", "reason_code": "SPACES_HELPER_TIMEOUT"}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "reason_code": "SPACES_HELPER_UNAVAILABLE"}
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return {"ok": False, "error": (proc.stderr or "no answer").strip()[:300], "reason_code": "SPACES_HELPER_FAILED"}
    return payload if isinstance(payload, dict) else {"ok": False, "reason_code": "SPACES_HELPER_FAILED"}


# What the macOS setting means for an agent's desktop, in the words Desktop & Dock uses.
SWITCH_SETTING = ('Desktop & Dock > Mission Control > "When switching to an application, switch to a Space '
                  'with open windows for the application"')
SWITCH_SETTING_ON_NOTE = (
    f"{SWITCH_SETTING} is on (the macOS default). A new window of the app the owner is using right now would take "
    "their display to the agent's desktop, so opening that app there is refused; Google Chrome works either way "
    "and is the browser to use. Turning the setting off lets Safari and other apps open there too; in exchange, "
    "switching to an app with Command-Tab or the Dock no longer moves the owner to the desktop with its windows."
)
# New windows appear on the display the owner is working on, and a caught window cannot
# reach another display's desktop unseen (spaces_helper refuseIfOnOtherDisplay).
DISPLAY_NOTE = (
    "New windows appear on the display marked active, the one the owner is working on; Chrome's appear on the main "
    "display. An app that already shows windows on the owner's desktops (their Safari, or their Chrome) opens only "
    "on a desktop of that display, so prefer an empty desktop there; other apps, and Chrome while the owner has no "
    "Chrome window open, open on any display's desktop."
)
SWITCH_SETTING_OFF_NOTE = (
    f"{SWITCH_SETTING} is off, so any app, Safari included, can open on the agent's desktop while the owner uses "
    "it. With it off, switching to an app with Command-Tab or the Dock does not move the owner to the desktop "
    "with its windows."
)


def list_desktops() -> Dict[str, Any]:
    """Displays with their desktops (Mission Control order, "Desktop N" labels), which one
    each display shows, the apps and windows on each desktop, which are empty, and what the
    owner's desktop-switching setting means for opening apps on an agent's desktop."""
    result = _run(["spaces"])
    if result.get("ok") and "switches_to_app_desktop" in result:
        switching = SWITCH_SETTING_ON_NOTE if result["switches_to_app_desktop"] else SWITCH_SETTING_OFF_NOTE
        result["note"] = f"{DISPLAY_NOTE} {switching}"
    return result


def create_desktop(display: str) -> Dict[str, Any]:
    """Add a desktop at the end of a display's desktops (Mission Control shows for ~0.1 s)."""
    return _run(["create", str(display)])


def remove_desktop(space_id: int) -> Dict[str, Any]:
    """Remove a desktop that is neither on screen nor holding windows."""
    return _run(["remove", str(int(space_id))])


def move_windows(space_id: int, window_ids: Iterable[int]) -> Dict[str, Any]:
    ids = [str(int(window_id)) for window_id in window_ids]
    if not ids:
        return {"ok": False, "error": "window ids required", "reason_code": "INVALID_ARGUMENT"}
    return _run(["move", str(int(space_id)), *ids])


def is_chrome(app: str) -> bool:
    return str(app or "").strip().lower() in _CHROME_NAMES


def open_on_desktop(space_id: int, app: str, targets: Iterable[str] = ()) -> Dict[str, Any]:
    """Open an app, or files or web addresses with it, straight onto a desktop."""
    items = [str(item) for item in targets if str(item or "").strip()]
    created: Optional[str] = None
    if not items and str(app or "").strip().lower() in _TEXTEDIT_NAMES:
        created = _new_text_document()
        items = [created]
    if is_chrome(app):
        if len(items) > 1:
            return {"ok": False, "error": "Chrome opens one address per window", "reason_code": "INVALID_ARGUMENT"}
        return _open_chrome_window(int(space_id), items[0] if items else None)
    result = _run(["open", str(int(space_id)), str(app), *items], timeout_s=20.0)
    if created:
        result["created_document"] = created
    return result


def _new_text_document() -> str:
    """An empty, uniquely named plain-text file for an agent to write in."""
    _AGENT_DOCUMENTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H.%M.%S")
    for suffix in ("", *(f" {n}" for n in range(2, 100))):
        path = _AGENT_DOCUMENTS / f"Untitled {stamp}{suffix}.txt"
        try:
            with path.open("x", encoding="utf-8"):
                return str(path)
        except FileExistsError:
            continue
    raise RuntimeError("could not create a new document")


def _chrome_window_ids(listing: Dict[str, Any]) -> Dict[int, List[int]]:
    found: Dict[int, List[int]] = {}
    for display in listing.get("displays") or []:
        for space in display.get("spaces") or []:
            for window in space.get("windows") or []:
                if window.get("app") == "Google Chrome":
                    found[int(window["window_id"])] = [int(space["id"])]
        for window in display.get("all_desktop_windows") or []:
            if window.get("app") == "Google Chrome":
                found[int(window["window_id"])] = []
    return found


def _chrome_running() -> bool:
    try:
        return subprocess.run(["/usr/bin/pgrep", "-x", "Google Chrome"], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _open_chrome_window(space_id: int, url: Optional[str]) -> Dict[str, Any]:
    from fastapi import HTTPException

    from .chrome_background_bridge import chrome_background_bridge as bridge

    if url and not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "Chrome opens http(s) addresses here", "reason_code": "INVALID_ARGUMENT"}
    before = list_desktops()
    if not before.get("ok"):
        return before
    if not any(int(space.get("id", -1)) == space_id and space.get("kind") == "desktop"
               for display in before.get("displays") or [] for space in display.get("spaces") or []):
        return {"ok": False, "error": f"no desktop has id {space_id}", "reason_code": "SPACE_NOT_FOUND"}
    if not bridge.is_connected():
        if _chrome_running():
            return {
                "ok": False, "reason_code": "CHROME_COMPANION_REQUIRED",
                "error": "Chrome is open but the Mac MCP Chrome Companion is not connected; without it Chrome "
                         "would come to the front. Load or reload the companion and retry.",
            }
        # Started without a window, Chrome stays in the background; its first window then
        # comes from the companion like any other.
        subprocess.run(["/usr/bin/open", "-g", "-b", CHROME_BUNDLE_ID, "--args", "--no-startup-window"],
                       capture_output=True, timeout=10, check=False)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not bridge.is_connected():
            time.sleep(0.1)
        if not bridge.is_connected():
            return {
                "ok": False, "reason_code": "CHROME_COMPANION_REQUIRED",
                "error": "Chrome started in the background but the Mac MCP Chrome Companion did not connect.",
            }
    existing = _chrome_window_ids(before)
    # Assigning Chrome would take the owner's own Chrome windows along: catch the new one instead.
    if any(spaces != [space_id] for spaces in existing.values()):
        return _catch_chrome_window(space_id, url, bridge)
    front_before = (before.get("frontmost") or {}).get("pid")
    assigned = _run(["assign", str(space_id), CHROME_BUNDLE_ID])
    if not assigned.get("ok"):
        _run(["assign", "0", CHROME_BUNDLE_ID])
        return assigned
    opened: Dict[str, Any] = {}
    new_windows: Dict[int, List[int]] = {}
    after: Dict[str, Any] = {}
    try:
        try:
            opened = bridge.request_open_window(url)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            return {**detail, "ok": False, "reason_code": "CHROME_OPEN_WINDOW_FAILED"}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            after = list_desktops()
            new_windows = {wid: spaces for wid, spaces in _chrome_window_ids(after).items() if wid not in existing}
            if new_windows:
                break
            time.sleep(0.05)
    finally:
        _run(["assign", "0", CHROME_BUNDLE_ID])
    front_after = (after.get("frontmost") or {}).get("pid")
    result: Dict[str, Any] = {
        "space_id": space_id, "app": CHROME_BUNDLE_ID,
        "chrome_window_id": opened.get("chrome_window_id"), "chrome_tab_id": opened.get("chrome_tab_id"),
        "windows": [{"window_id": wid, "app": "Google Chrome", "spaces": spaces} for wid, spaces in new_windows.items()],
        "focus_changed": bool(front_before and front_after and front_before != front_after),
    }
    if not new_windows:
        return {"ok": False, "error": "no new Chrome window appeared", "reason_code": "NO_NEW_WINDOW", **result}
    if any(spaces != [space_id] for spaces in new_windows.values()):
        return {"ok": False, "error": "the Chrome window opened outside the desktop", "reason_code": "SPACE_OPEN_NOT_APPLIED", **result}
    return {"ok": True, **result}


def _catch_chrome_window(space_id: int, url: Optional[str], bridge: Any) -> Dict[str, Any]:
    """The companion opens the window; the helper, already watching, moves it before it is drawn."""
    from fastapi import HTTPException

    try:
        proc = subprocess.Popen(
            [_helper(), "catch", str(int(space_id)), CHROME_BUNDLE_ID, "8"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc), "reason_code": "SPACES_HELPER_UNAVAILABLE"}
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        first = json.loads(proc.stdout.readline() or "{}") if ready else {}
        if not first.get("ready"):
            # A refusal (e.g. Chrome is in front with desktop switching on) arrives instead.
            proc.wait(timeout=5)
            return first or {"ok": False, "error": "the desktops helper did not start", "reason_code": "SPACES_HELPER_FAILED"}
        try:
            opened = bridge.request_open_window(url)
        except HTTPException as exc:
            proc.kill()
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            return {**detail, "ok": False, "reason_code": "CHROME_OPEN_WINDOW_FAILED"}
        out, _ = proc.communicate(timeout=15)
        try:
            result = json.loads(out or "{}")
        except json.JSONDecodeError:
            result = {"ok": False, "error": "unreadable helper answer", "reason_code": "SPACES_HELPER_FAILED"}
        result.update({"app": CHROME_BUNDLE_ID, "chrome_window_id": opened.get("chrome_window_id"),
                       "chrome_tab_id": opened.get("chrome_tab_id")})
        return result
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        proc.kill()
        return {"ok": False, "error": f"catching the Chrome window failed: {exc}", "reason_code": "SPACES_HELPER_FAILED"}
