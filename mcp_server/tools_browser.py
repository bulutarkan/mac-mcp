from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import HTTPException, status

from .security import Settings, truncate, validate_browser_url as _validate_browser_destination
from . import browser_tabs
from .chrome_background_bridge import chrome_background_bridge
from .artifact_pipeline import (
    ArtifactError, drive_native_file_dialog, register_artifact, resolve_artifact, wait_for_file_dialog,
)
from .context_handoff import (
    HandoffError, mark_handoff_consumed, resolve_browser_upload_handoff,
)
from .foreground_guard import require_foreground_authorization
from .tool_cancellation import (
    ToolCancelledError, cancellation_checkpoint, register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)


def validate_url(settings: Settings, url: str) -> None:
    """Validate a browser destination with browser-specific allow/private policy."""
    _validate_browser_destination(settings, url)


def _close_unsafe_new_tab_best_effort(browser: str, row: Dict[str, Any]) -> None:
    try:
        wi = int(row.get("window_index") or 1)
        ti = int(row.get("tab_index") or 1)
        script = f'''
        tell application "{browser}"
            if (count of windows) >= {wi} then
                tell window {wi}
                    if (count of tabs) >= {ti} then close tab {ti}
                end tell
            end if
        end tell
        '''
        _run_osascript(script, timeout_s=10)
    except Exception:
        pass
    browser_tabs.forget(str(row.get("tab_handle") or "") or None)


def _restore_previous_tab_url_best_effort(
    settings: Settings, browser: str, row: Dict[str, Any], previous_url: Optional[str],
) -> None:
    if not previous_url:
        return
    try:
        validate_url(settings, previous_url)
        wi = int(row.get("window_index") or 1)
        ti = int(row.get("tab_index") or 1)
        escaped = previous_url.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "{browser}"
            if (count of windows) >= {wi} then
                tell window {wi}
                    if (count of tabs) >= {ti} then set URL of tab {ti} to "{escaped}"
                end tell
            end if
        end tell
        '''
        _run_osascript(script, timeout_s=10)
    except Exception:
        pass


def _validate_observed_navigation(
    settings: Settings, browser: str, requested_url: str, row: Optional[Dict[str, Any]],
    *, new_tab: bool, previous_url: Optional[str] = None,
) -> str:
    observed = str((row or {}).get("url") or requested_url)
    try:
        validate_url(settings, observed)
    except HTTPException as exc:
        if row is not None:
            if new_tab:
                _close_unsafe_new_tab_best_effort(browser, row)
            else:
                _restore_previous_tab_url_best_effort(settings, browser, row, previous_url)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "ok": False,
                "error": "browser_redirect_blocked",
                "message": "Browser navigation resolved to a destination outside the allowed network trust boundary.",
            },
        ) from exc
    return observed

_VISUAL_COMPANION_PATH = Path(__file__).resolve().parents[1] / "menu_app" / "BrowserVisualCompanion" / "visual.js"

def _visual_companion_source() -> str:
    try:
        return _VISUAL_COMPANION_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""

BROWSERS = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
}

_TAB_IDENTITY_CHANGED = "MAC_MCP_TAB_IDENTITY_CHANGED"
_CHROME_NATIVE_JS_DENIED = False
_CHROME_BRIDGE_INLINE_LIMIT = 2400
_CHROME_BACKGROUND_OPEN_LOCK = threading.Lock()


def _norm_browser(browser: str) -> str:
    key = (browser or "").strip().lower()
    if key in BROWSERS:
        return BROWSERS[key]
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser must be 'Safari' or 'Google Chrome'.")


def _require_stable_handle_for_mutation(
    browser: str, tab_handle: Optional[str], window_index: int, operation: str,
) -> None:
    if str(tab_handle or "").strip() or not browser_tabs._logical_owner()[0]:
        return
    rows = [row for row in browser_tabs.list_tabs(browser) if int(row.get("window_index") or 0) == int(window_index)]
    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "error": "stable_tab_handle_required",
                "retryable": True,
                "operation": operation,
                "message": "This mutation is ambiguous while multiple tabs are open; list/observe tabs and retry with tab_handle.",
            },
        )


def _stale_tab_http_error(tab_handle: Optional[str]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "ok": False,
            "error": "stale_tab_handle",
            "retryable": True,
            "tab_handle": str(tab_handle or ""),
            "required_action": "browser_list_tabs",
            "do_not_fallback_to_active_tab": True,
            "message": "The requested tab no longer has the same stable identity. Refresh tabs and retry with the new tab_handle; never fall back to the active tab.",
        },
    )


def _new_tab_window(
    browser: str, window_index: int, tab_handle: Optional[str],
) -> int:
    if str(tab_handle or "").strip():
        with _tab_lease(browser, tab_handle, window_index, None, allow_rebind=True) as anchor:
            return int(anchor.window_index)
    preferred = browser_tabs.preferred_window_for_owner(browser)
    return int(preferred if preferred is not None else window_index)


def _resolve_tab_target(
    browser: str,
    tab_handle: Optional[str],
    window_index: int,
    tab_index: Optional[int],
) -> Tuple[int, Optional[int]]:
    if not tab_handle:
        return window_index, tab_index
    try:
        wi, ti, _ = browser_tabs.resolve_tab(browser, tab_handle)
        return wi, ti
    except KeyError as exc:
        raise _stale_tab_http_error(tab_handle) from exc


@contextmanager
def _tab_lease(
    browser: str,
    tab_handle: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    *,
    allow_rebind: bool = False,
) -> Iterator[browser_tabs.TabTarget]:
    with ExitStack() as stack:
        try:
            target = stack.enter_context(
                browser_tabs.tab_lease(
                    browser,
                    tab_handle=tab_handle,
                    window_index=window_index,
                    tab_index=tab_index,
                    allow_rebind=allow_rebind,
                )
            )
        except KeyError as exc:
            raise _stale_tab_http_error(tab_handle) from exc
        yield target


def _tab_identity_guard(target: browser_tabs.TabTarget) -> str:
    """Resolve and validate the native tab inside the same AppleScript as its action."""
    native_id = _js_escape(target.native_id)
    url = _js_escape(target.url)
    title = _js_escape(target.title)
    lines = [f"set targetTab to tab {target.tab_index}"]
    if native_id and native_id != "0":
        native_property = "id" if target.browser == "Google Chrome" else "pid"
        lines.extend(
            [
                'set actualNativeId to ""',
                f"try\nset actualNativeId to ({native_property} of targetTab) as text\nend try",
                f'if actualNativeId is not "{native_id}" then error "{_TAB_IDENTITY_CHANGED}"',
            ]
        )
    else:
        title_property = "title" if target.browser == "Google Chrome" else "name"
        lines.extend(
            [
                f'if ((URL of targetTab) as text) is not "{url}" then error "{_TAB_IDENTITY_CHANGED}"',
                f'if (({title_property} of targetTab) as text) is not "{title}" then error "{_TAB_IDENTITY_CHANGED}"',
            ]
        )
    return "\n".join(lines)


def _terminate_process_group(proc: subprocess.Popen[str], grace_s: float = 0.5) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_osascript(script: str, timeout_s: int = 30) -> str:
    timeout_s = max(1, min(int(timeout_s), 120))
    cancellation_checkpoint()
    proc = subprocess.Popen(
        ["osascript", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cleanup_token = register_cancellation_cleanup(lambda: _terminate_process_group(proc))
    cancellation_checkpoint()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        cancellation_checkpoint()
    except ToolCancelledError:
        _terminate_process_group(proc)
        proc.wait()
        raise
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "AppleScript timed out.")
    finally:
        unregister_cancellation_cleanup(cleanup_token)

    if proc.returncode != 0:
        msg = (stderr or stdout or "AppleScript error").strip()
        if _TAB_IDENTITY_CHANGED in msg:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Target tab identity changed before the operation; resolve or observe the tab again.",
            )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, msg)
    return (stdout or "").strip()

def _visual_claim_event_js(expected_url: str) -> str:
    expected = json.dumps(str(expected_url or ""))
    return (
        "(()=>{try{"
        f"const expected={expected};"
        "if(document.readyState==='loading')return false;"
        "const want=new URL(expected),current=new URL(location.href);"
        "if(want.origin!==current.origin||want.pathname!==current.pathname)return false;"
        "const p={seq:Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7),"
        "claim:true,phase:'working',action:'Opened',target_kind:'Page',ttl_ms:1800};"
        "const raw=btoa(unescape(encodeURIComponent(JSON.stringify(p))));"
        "(document.documentElement||document.body).setAttribute('data-mac-mcp-visual-event',raw);"
        "try{window.dispatchEvent(new Event('mac-mcp-visual'));}catch(_){}"
        "return true;}catch(_){return false;}})()"
    )


def _visual_claim_js(expected_url: str) -> str:
    return _visual_companion_source() + "\n" + _visual_claim_event_js(expected_url)


def _visual_claim_script(browser: str, tab_index: int, expected_url: str) -> str:
    """Best-effort Visual Companion claim for the final browser document opened by Mac MCP."""
    b = _norm_browser(browser)
    js_escaped = _js_escape(_visual_claim_js(expected_url))
    if b == "Safari":
        execute = f'set claimed to do JavaScript "{js_escaped}" in tab {int(tab_index)} of window 1'
    else:
        execute = f'set claimed to execute javascript "{js_escaped}" in tab {int(tab_index)} of window 1'
    return (
        f'tell application "{b}"\n'
        'repeat with attempt from 1 to 30\n'
        'try\n'
        f'{execute}\n'
        'if claimed is true then return true\n'
        'end try\n'
        'delay 0.15\n'
        'end repeat\n'
        'return false\n'
        'end tell'
    )


def _claim_tab_visual(
    browser: str,
    tab_index: int,
    expected_url: str,
    tab_handle: Optional[str] = None,
) -> bool:
    try:
        b = _norm_browser(browser)
        if b == "Google Chrome":
            with _tab_lease(b, tab_handle, 1, tab_index) as target:
                # Self-inject the exact shared source first; the extension remains optional.
                _execute_js_for_target(b, _visual_companion_source(), target, 6)
                raw = _execute_js_for_target(b, _visual_claim_event_js(expected_url), target, 6)
        else:
            raw = _run_osascript(_visual_claim_script(b, tab_index, expected_url), timeout_s=6)
        return str(raw).strip().lower() in {"true", "1"}
    except Exception:
        # Visual Companion is optional UX; opening the page must never fail because
        # the browser denied or delayed the visual claim.
        return False


# Compatibility wrappers retained for existing callers/tests.
def _safari_visual_claim_js(expected_url: str) -> str:
    return _visual_claim_js(expected_url)


def _safari_visual_claim_script(tab_index: int, expected_url: str) -> str:
    return _visual_claim_script("Safari", tab_index, expected_url)


def _claim_safari_tab_visual(tab_index: int, expected_url: str) -> bool:
    return _claim_tab_visual("Safari", tab_index, expected_url)


def _chrome_is_running() -> bool:
    try:
        proc = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Google Chrome"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _chrome_cold_launch_command(url: str) -> list[str]:
    # The optional user-data-dir override is intentionally argv-only (never shell
    # parsed). It is useful for isolated Chrome profiles and lets the production
    # cold-start path be exercised without touching a user's normal profile.
    profile = str(os.getenv("MAC_MCP_CHROME_USER_DATA_DIR", "") or "").strip()
    if profile:
        profile_path = str(Path(profile).expanduser().resolve())
        chrome_args = [
            f"--user-data-dir={profile_path}", "--use-mock-keychain",
            "--no-first-run", "--no-default-browser-check",
        ]
        debug_port = str(os.getenv("MAC_MCP_CHROME_REMOTE_DEBUGGING_PORT", "") or "").strip()
        if debug_port:
            try:
                port = int(debug_port)
            except ValueError:
                port = 0
            if 1 <= port <= 65535:
                chrome_args.extend([f"--remote-debugging-port={port}", "--enable-unsafe-extension-debugging"])
        return [
            "/usr/bin/open", "-g", "-n", "-a", "Google Chrome", "--args",
            *chrome_args, str(url),
        ]
    return ["/usr/bin/open", "-g", "-a", "Google Chrome", str(url)]


def _open_chrome_cold_background(url: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Launch Chrome directly on the first requested URL without taking focus.

    This path is used only when the Chrome application process is not running, so
    there is no extension worker available yet to create an inactive tab. The URL
    is therefore supplied to LaunchServices at process launch time; creating an
    about:blank window first and navigating afterward can foreground Chrome.
    """
    with _CHROME_BACKGROUND_OPEN_LOCK:
        if _chrome_is_running():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"ok": False, "error": "chrome_cold_start_raced", "retryable": True},
            )
        try:
            proc = subprocess.run(
                _chrome_cold_launch_command(url),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Could not launch Chrome in the background: {exc}",
            ) from exc
        if proc.returncode != 0:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Could not launch Chrome in the background: {(proc.stderr or '').strip() or 'open failed'}",
            )

        deadline = time.monotonic() + 8.0
        last_error = ""
        rows: list[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                rows = browser_tabs.list_tabs("Google Chrome")
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.08)
                continue
            exact = [row for row in rows if str(row.get("url") or "") == str(url)]
            resolved = exact[0] if len(exact) == 1 else None
            if resolved is None and len(rows) == 1 and str(rows[0].get("url") or ""):
                # Redirecting first loads are unambiguous because Chrome had no
                # process before this launch and the open lock excludes another
                # Mac MCP cold-start request.
                resolved = rows[0]
            if resolved is not None:
                companion_deadline = time.monotonic() + 8.0
                while not chrome_background_bridge.is_connected() and time.monotonic() < companion_deadline:
                    time.sleep(0.05)
                return resolved, {
                    "chrome_tab_id": resolved.get("native_id"),
                    "cold_start": True,
                    "companion_connected": chrome_background_bridge.is_connected(),
                }
            time.sleep(0.08)

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "error": "chrome_cold_start_tab_not_resolved",
                "retryable": True,
                "message": "Chrome launched in the background but its first tab could not be resolved safely.",
                "candidate_count": len(rows),
                "last_error": last_error or None,
            },
        )


def _open_chrome_background_tab_via_extension(url: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Create an inactive Chrome tab through the MV3 companion without foregrounding Chrome.

    New-tab discovery is serialized so two agents opening different tabs concurrently
    cannot confuse each other's newly-created native tab. This lock is deliberately
    separate from per-tab leases: after discovery the normal stable-handle ownership
    system remains authoritative for all actions on the created tab.
    """
    with _CHROME_BACKGROUND_OPEN_LOCK:
        before = browser_tabs.list_tabs("Google Chrome")
        before_ids = {str(row.get("native_id") or "") for row in before}
        bridge_result = chrome_background_bridge.request_open_tab(url, timeout_s=6.0)
        chrome_tab_id = str(bridge_result.get("chrome_tab_id") or "")
        deadline = time.monotonic() + 4.0
        latest_new: list[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = browser_tabs.list_tabs("Google Chrome")
            latest_new = [
                row for row in rows
                if str(row.get("native_id") or "") not in before_ids
            ]
            if chrome_tab_id:
                native_match = [
                    row for row in latest_new
                    if str(row.get("native_id") or "") == chrome_tab_id
                ]
                if len(native_match) == 1:
                    return native_match[0], bridge_result
            exact_url = [row for row in latest_new if str(row.get("url") or "") == str(url)]
            if len(exact_url) == 1:
                return exact_url[0], bridge_result
            if len(latest_new) == 1 and str(latest_new[0].get("url") or ""):
                # Redirecting pages can replace the requested URL almost immediately.
                # A single newly-created native tab is unambiguous while this transport
                # lock excludes other Mac MCP background-open requests.
                return latest_new[0], bridge_result
            time.sleep(0.05)

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "ok": False,
                "error": "chrome_background_tab_not_resolved",
                "retryable": True,
                "message": "Chrome created the background tab but Mac MCP could not resolve its stable native identity safely.",
                "candidate_count": len(latest_new),
            },
        )


def browser_open_url(
    settings: Settings,
    browser: str,
    url: str,
    new_tab: bool = True,
    background: bool = True,
    activate: Optional[bool] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    validate_url(settings, url)

    if activate is not None:
        background = not bool(activate)
    if not background:
        require_foreground_authorization("browser_open_url", browser=b)
    activate_line = "" if background else "activate"
    escaped_url = _js_escape(url)
    target_window = int(window_index)
    if new_tab and b == "Safari":
        target_window = _new_tab_window(b, window_index, tab_handle)

    if not new_tab:
        current_tabs = browser_tabs.list_tabs(b)
        if not current_tabs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No existing browser tab is available for navigation.")
        selected_handle = str(tab_handle or "").strip()
        if not selected_handle:
            window_tabs = [
                row for row in current_tabs
                if int(row.get("window_index") or 0) == int(window_index)
            ]
            if not window_tabs:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Browser window {window_index} has no tabs.")
            owner = browser_tabs._logical_owner()[0]
            if len(window_tabs) != 1 and owner:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "ok": False,
                        "error": "stable_tab_handle_required",
                        "retryable": True,
                        "message": "Existing-tab navigation is ambiguous while multiple tabs are open; list/observe tabs and retry with tab_handle.",
                    },
                )
            selected = (
                window_tabs[0] if len(window_tabs) == 1 else
                next((row for row in window_tabs if row.get("active")), window_tabs[0])
            )
            selected_handle = str(selected.get("tab_handle") or "")

        with _tab_lease(b, selected_handle, window_index, tab_index) as target:
            guard = _tab_identity_guard(target)
            native_property = "id" if b == "Google Chrome" else "pid"
            script = f'''
            tell application "{b}"
                {activate_line}
                tell window {target.window_index}
                    {guard}
                    set URL of targetTab to "{escaped_url}"
                    set newIndex to index of targetTab
                    set newNativeId to ""
                    try
                        set newNativeId to ({native_property} of targetTab) as text
                    end try
                    return (newIndex as text) & "|" & newNativeId
                end tell
            end tell
            '''
            previous_url = target.url
            raw = _run_osascript(script, timeout_s=30)
            parts = str(raw or "").strip().split("|", 1)
            try:
                resolved_index = int(parts[0])
            except (TypeError, ValueError, IndexError) as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Browser did not return the navigated tab identity.") from exc
            returned_native = parts[1].strip() if len(parts) > 1 else ""
            scanned = browser_tabs._scan(b)
            candidates = [
                row for row in scanned
                if int(row.get("window_index") or 0) == target.window_index
                and int(row.get("tab_index") or 0) == resolved_index
            ]
            if returned_native and returned_native != "0":
                native_matches = [row for row in scanned if str(row.get("native_id") or "") == returned_native]
                if len(native_matches) == 1:
                    candidates = native_matches
            if len(candidates) != 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "ok": False,
                        "error": "navigated_tab_identity_unresolved",
                        "retryable": True,
                        "tab_handle": target.tab_handle,
                        "message": "The target tab changed during navigation and could not be re-identified safely.",
                    },
                )
            row = dict(candidates[0])
            row["tab_handle"] = target.tab_handle
            if b == "Safari":
                row = browser_tabs.rebind_safari_handle(target.tab_handle, row)
            elif returned_native and returned_native != "0" and str(row.get("native_id") or "") != target.native_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"ok": False, "error": "tab_identity_changed", "retryable": True, "tab_handle": target.tab_handle},
                )
            observed_url = _validate_observed_navigation(
                settings, b, url, row, new_tab=False, previous_url=previous_url,
            )
            visual_claimed = _claim_tab_visual(b, resolved_index, observed_url, target.tab_handle)
            return {
                "ok": True,
                "browser": b,
                "url": observed_url,
                "requested_url": url,
                "background": bool(background),
                "window_index": int(row.get("window_index") or target.window_index),
                "tab_index": int(row.get("tab_index") or resolved_index),
                "tab_handle": target.tab_handle,
                "lease_generation": target.lease_generation,
                "visual_claimed": visual_claimed,
                "foreground_forced": False if background else True,
            }

    if b == "Safari":
        script = f'''
        tell application "Safari"
            if (count of windows) = 0 then
                make new document
            end if
            {activate_line}
            tell window {target_window}
                set newTab to make new tab with properties {{URL:"{escaped_url}"}}
                set newIndex to index of newTab
                if {str(not background).lower()} then set current tab to newTab
            end tell
            return newIndex
        end tell
        '''
    else:
        if background and not _chrome_is_running():
            created, transport = _open_chrome_cold_background(url)
            opened_index = int(created["tab_index"])
            observed_url = _validate_observed_navigation(settings, b, url, created, new_tab=True)
            handle = str(created.get("tab_handle") or "") or None
            lease = browser_tabs.claim_created_tab(b, handle) if handle else None
            visual_claimed = _claim_tab_visual(b, opened_index, observed_url, handle)
            return {
                "ok": True, "browser": b, "url": observed_url, "requested_url": url,
                "background": True, "window_index": int(created["window_index"]),
                "tab_index": opened_index, "tab_handle": handle,
                "lease_generation": (lease or {}).get("generation"), "visual_claimed": visual_claimed,
                "foreground_forced": False, "background_transport": "chrome_cold_launch",
                "chrome_tab_id": transport.get("chrome_tab_id"),
                "companion_connected": bool(transport.get("companion_connected")),
            }
        if background:
            created, transport = _open_chrome_background_tab_via_extension(url)
            opened_index = int(created["tab_index"])
            observed_url = _validate_observed_navigation(settings, b, url, created, new_tab=True)
            handle = str(created.get("tab_handle") or "") or None
            lease = browser_tabs.claim_created_tab(b, handle) if handle else None
            visual_claimed = _claim_tab_visual(b, opened_index, observed_url, handle)
            return {
                "ok": True, "browser": b, "url": observed_url, "requested_url": url,
                "background": True, "window_index": int(created["window_index"]),
                "tab_index": opened_index, "tab_handle": handle,
                "lease_generation": (lease or {}).get("generation"), "visual_claimed": visual_claimed,
                "foreground_forced": False, "background_transport": "chrome_extension",
                "chrome_tab_id": transport.get("chrome_tab_id"),
            }
        script = f'''
        tell application "Google Chrome"
            if (count of windows) = 0 then
                make new window
            end if
            {activate_line}
            tell window 1
                set previousIndex to active tab index
                set newTab to make new tab with properties {{URL:"{escaped_url}"}}
                set newIndex to count of tabs
                if {str(not background).lower()} then
                    set active tab index to newIndex
                else
                    set active tab index to previousIndex
                end if
            end tell
            return newIndex
        end tell
        '''

    raw = _run_osascript(script, timeout_s=30)
    try:
        opened_index = int(str(raw).strip())
    except Exception:
        opened_index = 1
    created_window = target_window if b == "Safari" else 1
    created = browser_tabs.find_created(b, created_window, opened_index)
    observed_url = _validate_observed_navigation(settings, b, url, created, new_tab=True)
    handle = created.get("tab_handle") if created else None
    lease = browser_tabs.claim_created_tab(b, handle) if handle else None
    visual_claimed = _claim_tab_visual(b, opened_index, observed_url, handle)
    return {
        "ok": True,
        "browser": b,
        "url": observed_url,
        "requested_url": url,
        "background": bool(background),
        "window_index": created_window,
        "tab_index": opened_index,
        "tab_handle": handle,
        "lease_generation": (lease or {}).get("generation"),
        "visual_claimed": visual_claimed,
        "foreground_forced": False if background else True,
    }


def browser_list_tabs(settings: Settings, browser: str) -> Dict[str, Any]:
    b = _norm_browser(browser)
    try:
        tabs = browser_tabs.list_tabs(b)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Could not enumerate browser tabs: {exc}",
        ) from exc
    return {"ok": True, "browser": b, "tabs": tabs}


def browser_activate_tab(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: int = 1,
    tab_handle: Optional[str] = None,
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    require_foreground_authorization("browser_activate_tab", browser=b)
    if not tab_handle and (window_index < 1 or tab_index < 1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index and tab_index must be >= 1")
    _require_stable_handle_for_mutation(b, tab_handle, window_index, "activate_tab")

    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        guard = _tab_identity_guard(target)
        if b == "Safari":
            script = f'''
            tell application "Safari"
                {"activate" if allow_foreground else ""}
                tell window {target.window_index}
                    {guard}
                    set current tab to targetTab
                end tell
            end tell
            '''
        else:
            script = f'''
            tell application "Google Chrome"
                {"activate" if allow_foreground else ""}
                tell window {target.window_index}
                    {guard}
                    set active tab index to {target.tab_index}
                end tell
            end tell
            '''
        _run_osascript(script, timeout_s=30)
    return {
        "ok": True,
        "browser": b,
        "window_index": target.window_index,
        "tab_index": target.tab_index,
        "tab_handle": target.tab_handle,
        "foreground_forced": bool(allow_foreground),
    }


def browser_close_tab(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: int = 1,
    tab_handle: Optional[str] = None,
    tab_handles: Optional[List[str]] = None,
) -> Dict[str, Any]:
    b = _norm_browser(browser)

    requested_handles: List[str] = []
    if tab_handle:
        requested_handles.append(str(tab_handle).strip())
    if tab_handles is not None:
        for value in tab_handles:
            handle = str(value or "").strip()
            if handle:
                requested_handles.append(handle)
        if not requested_handles:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tab_handles must contain at least one tab_handle")

    if requested_handles:
        # Preserve caller order while preventing accidental duplicate closes. Resolve
        # the full selection first so a stale/unknown handle cannot cause a partial
        # multi-tab close. Each close still re-resolves its stable handle afterward,
        # so shifting tab/window indices are safe.
        requested_handles = list(dict.fromkeys(requested_handles))
        current_tabs = {str(row.get("tab_handle") or ""): row for row in browser_tabs.list_tabs(b)}
        missing = [handle for handle in requested_handles if handle not in current_tabs]
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {
                    "ok": False,
                    "error": "unknown_tab_handle",
                    "missing_tab_handles": missing,
                    "message": "One or more selected tabs are unknown or already closed; no tabs were closed.",
                },
            )

        closed: List[Dict[str, Any]] = []
        for handle in requested_handles:
            with _tab_lease(b, handle, window_index, tab_index) as target:
                guard = _tab_identity_guard(target)
                script = f'''
                tell application "{b}"
                    tell window {target.window_index}
                        {guard}
                        close targetTab
                    end tell
                end tell
                '''
                _run_osascript(script, timeout_s=30)
                browser_tabs.forget(target.tab_handle)
                closed.append({
                    "window_index": target.window_index,
                    "tab_index": target.tab_index,
                    "tab_handle": target.tab_handle,
                    "title": target.title,
                    "url": target.url,
                })

        result: Dict[str, Any] = {
            "ok": True,
            "browser": b,
            "closed_count": len(closed),
            "closed": closed,
        }
        if len(closed) == 1:
            result.update({
                "window_index": closed[0]["window_index"],
                "tab_index": closed[0]["tab_index"],
                "tab_handle": closed[0]["tab_handle"],
            })
        return result

    if window_index < 1 or tab_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index and tab_index must be >= 1")

    with _tab_lease(b, None, window_index, tab_index) as target:
        guard = _tab_identity_guard(target)
        script = f'''
        tell application "{b}"
            tell window {target.window_index}
                {guard}
                close targetTab
            end tell
        end tell
        '''
        _run_osascript(script, timeout_s=30)
        browser_tabs.forget(target.tab_handle)
    return {
        "ok": True,
        "browser": b,
        "window_index": target.window_index,
        "tab_index": target.tab_index,
        "tab_handle": target.tab_handle,
        "closed_count": 1,
        "closed": [{
            "window_index": target.window_index,
            "tab_index": target.tab_index,
            "tab_handle": target.tab_handle,
            "title": target.title,
            "url": target.url,
        }],
    }


def _js_escape(js: str) -> str:
    # Safe AppleScript string literal: escape backslashes and quotes
    return (js or "").replace("\\", "\\\\").replace('"', '\\"')


def _chrome_execute_js_via_url_bridge(
    js: str,
    target: browser_tabs.TabTarget,
    timeout_s: int,
) -> str:
    """Fallback for Chrome builds where AppleScript `execute javascript` is broken.

    The bridge still depends on Chrome's user-controlled View → Developer →
    Allow JavaScript from Apple Events setting. If that setting is off, the
    javascript: URL itself is rejected by Chrome.
    """
    token = uuid.uuid4().hex[:12]
    marker = f"__MAC_MCP_BRIDGE_{token}__"
    code = (js or "").strip()
    if not code:
        return ""
    # Generated browser-agent programs are expressions/IIFEs; tolerate one final
    # semicolon so a standalone shared content script can use the same bridge.
    code = code[:-1].rstrip() if code.endswith(";") else code

    stage_js = (
        "javascript:(()=>{try{"
        "window.__macMcpBridgeOriginalTitle=document.title;"
        f"const __mcpValue=({code});"
        "const __mcpText=String(__mcpValue==null?'':__mcpValue);"
        "window.__macMcpBridgeResult=btoa(unescape(encodeURIComponent(__mcpText)));"
        f"document.title='{marker}'+(window.__macMcpBridgeResult.length<={_CHROME_BRIDGE_INLINE_LIMIT}?'INLINE:'+window.__macMcpBridgeResult:'READY:'+window.__macMcpBridgeResult.length);"
        "}catch(e){"
        "const __mcpErr='__MCPERR__'+String(e&&e.name||'Error')+':'+String(e&&e.message||e||'unknown');"
        "window.__macMcpBridgeResult=btoa(unescape(encodeURIComponent(__mcpErr)));"
        f"document.title='{marker}'+(window.__macMcpBridgeResult.length<={_CHROME_BRIDGE_INLINE_LIMIT}?'INLINE:'+window.__macMcpBridgeResult:'READY:'+window.__macMcpBridgeResult.length);"
        "}})();void(0)"
    )
    guard = _tab_identity_guard(target)
    stage_script = f'''tell application "Google Chrome"
    tell window {target.window_index}
        {guard}
        set URL of targetTab to "{_js_escape(stage_js)}"
        repeat with attempt from 1 to 80
            delay 0.025
            set bridgeTitle to (title of targetTab) as text
            if bridgeTitle starts with "{marker}READY:" or bridgeTitle starts with "{marker}INLINE:" then return bridgeTitle
        end repeat
        return "{marker}TIMEOUT"
    end tell
end tell'''

    restore_js = (
        "javascript:(()=>{try{"
        "if(window.__macMcpBridgeOriginalTitle!==undefined)document.title=window.__macMcpBridgeOriginalTitle;"
        "delete window.__macMcpBridgeOriginalTitle;delete window.__macMcpBridgeResult;"
        "}catch(_){}})();void(0)"
    )
    restore_script = f'''tell application "Google Chrome"
    tell window {target.window_index}
        {guard}
        set URL of targetTab to "{_js_escape(restore_js)}"
    end tell
end tell'''

    try:
        prefix = f"{marker}READY:"
        inline_prefix = f"{marker}INLINE:"
        ready = ""
        # A click may start navigation immediately before the next state/read call.
        # In that narrow window Chrome can discard a javascript: URL with the old
        # document. Retry against the same native tab identity after navigation.
        for bridge_attempt in range(3):
            ready = _run_osascript(stage_script, timeout_s=max(3, min(timeout_s, 10)))
            if ready.startswith(prefix) or ready.startswith(inline_prefix):
                break
            if bridge_attempt < 2:
                time.sleep(0.12)
        if not (ready.startswith(prefix) or ready.startswith(inline_prefix)):
            raise HTTPException(
                status.HTTP_412_PRECONDITION_FAILED,
                "Chrome JavaScript automation is unavailable. Manually enable View → Developer → "
                "Allow JavaScript from Apple Events, then retry.",
            )

        encoded = ""
        if ready.startswith(inline_prefix):
            encoded = ready[len(inline_prefix):]
            encoded_len = len(encoded)
        else:
            try:
                encoded_len = int(ready[len(prefix):])
            except ValueError as exc:
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Chrome JavaScript bridge returned an invalid length.") from exc
            if encoded_len < 0 or encoded_len > 8_000_000:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Chrome JavaScript bridge result exceeded the safety limit.")

        chunk_size = 3000
        chunks: list[str] = []
        for start in range(0 if not encoded else encoded_len, encoded_len, chunk_size):
            end = min(start + chunk_size, encoded_len)
            chunk_js = (
                "javascript:(()=>{try{"
                f"document.title='{marker}CHUNK:'+String(window.__macMcpBridgeResult||'').slice({start},{end});"
                "}catch(_){}})();void(0)"
            )
            chunk_script = f'''tell application "Google Chrome"
    tell window {target.window_index}
        {guard}
        set URL of targetTab to "{_js_escape(chunk_js)}"
        repeat with attempt from 1 to 40
            delay 0.015
            set bridgeTitle to (title of targetTab) as text
            if bridgeTitle starts with "{marker}CHUNK:" then return bridgeTitle
        end repeat
        return "{marker}TIMEOUT"
    end tell
end tell'''
            row = _run_osascript(chunk_script, timeout_s=max(2, min(timeout_s, 10)))
            chunk_prefix = f"{marker}CHUNK:"
            if not row.startswith(chunk_prefix):
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Chrome JavaScript bridge timed out while reading a result chunk.")
            chunks.append(row[len(chunk_prefix):])

        if not encoded:
            encoded = "".join(chunks)
        if len(encoded) != encoded_len:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Chrome JavaScript bridge returned an incomplete result.")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8") if encoded else ""
        except Exception as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Chrome JavaScript bridge returned invalid encoded data.") from exc
        if raw.startswith("__MCPERR__"):
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, raw[len("__MCPERR__"):])
        return raw
    finally:
        try:
            _run_osascript(restore_script, timeout_s=3)
        except Exception:
            pass


def _execute_js_for_target(
    browser: str,
    js: str,
    target: browser_tabs.TabTarget,
    timeout_s: int,
) -> str:
    global _CHROME_NATIVE_JS_DENIED
    if browser == "Google Chrome" and chrome_background_bridge.is_connected() and target.native_id:
        return chrome_background_bridge.request_execute_js(target.native_id, js, timeout_s=timeout_s)
    if browser == "Google Chrome" and _CHROME_NATIVE_JS_DENIED:
        return _chrome_execute_js_via_url_bridge(js, target, timeout_s)
    js_escaped = _js_escape(js)
    guard = _tab_identity_guard(target)
    if browser == "Safari":
        script = f'''tell application "Safari"
    tell window {target.window_index}
        {guard}
        set r to ""
        set r to do JavaScript "{js_escaped}" in targetTab
        return r
    end tell
end tell'''
    else:
        script = f'''tell application "Google Chrome"
    tell window {target.window_index}
        {guard}
        set r to ""
        set r to execute javascript "{js_escaped}" in targetTab
        return r
    end tell
end tell'''
    try:
        return _run_osascript(script, timeout_s=timeout_s)
    except HTTPException as exc:
        detail = str(getattr(exc, "detail", "") or "")
        if browser == "Google Chrome" and (
            "Access not allowed" in detail
            or "Executing JavaScript through AppleScript is turned off" in detail
        ):
            _CHROME_NATIVE_JS_DENIED = True
            try:
                return _chrome_execute_js_via_url_bridge(js, target, timeout_s)
            except HTTPException as bridge_exc:
                if bridge_exc.status_code == status.HTTP_412_PRECONDITION_FAILED:
                    raise HTTPException(
                        status.HTTP_412_PRECONDITION_FAILED,
                        "Chrome JavaScript automation is disabled. In Chrome, manually enable "
                        "View → Developer → Allow JavaScript from Apple Events, then retry. "
                        "Chrome intentionally requires a real user input for this secure setting.",
                    ) from exc
                raise
        raise


def browser_execute_js(
    settings: Settings,
    browser: str,
    js: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    if not tab_handle and window_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index must be >= 1")
    if not tab_handle and tab_index is not None and tab_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tab_index must be >= 1")

    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        raw = _execute_js_for_target(
            b,
            js,
            target,
            timeout_s=min(60, settings.max_wait_s),
        )
    raw, truncated = truncate(raw, settings.max_js_result_chars)
    return {"ok": True, "browser": b, "result": raw, "truncated": truncated}


def browser_click_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    started_at_epoch_ms = int(time.time() * 1000)
    sel = json.dumps(css_selector)
    js = f"(function(){{var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.click(); return 'OK';}})()"
    result = browser_execute_js(
        settings,
        browser,
        js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )
    result["started_at_epoch_ms"] = started_at_epoch_ms
    return result


def browser_type_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    text: str,
    clear: bool = True,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    sel = json.dumps(css_selector)
    txt = json.dumps(text)
    clr = "true" if clear else "false"
    js = (
        "(function(){"
        f"var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND';"
        "try{el.focus();}catch(e){}"
        f"if({clr}) el.value='';"
        f"el.value = {txt};"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return 'OK';"
        "})()"
    )
    return browser_execute_js(
        settings,
        browser,
        js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )


def browser_wait_for_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    timeout_s: int = 20,
    poll_ms: int = 250,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    if timeout_s < 1:
        timeout_s = 1
    if poll_ms < 50:
        poll_ms = 50

    sel = json.dumps(css_selector)
    start = time.time()
    while True:
        js = f"(function(){{return !!document.querySelector({sel});}})()"
        res = browser_execute_js(
            settings,
            browser,
            js,
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
        )
        ok = (str(res.get("result", "")).strip().lower() in {"true", "1", "ok"})
        if ok:
            return {"ok": True, "found": True, "elapsed_s": round(time.time() - start, 3)}
        if time.time() - start >= min(timeout_s, settings.max_wait_s):
            return {"ok": True, "found": False, "elapsed_s": round(time.time() - start, 3)}
        time.sleep(poll_ms / 1000.0)


def browser_get_html(
    settings: Settings,
    browser: str,
    max_chars: Optional[int] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    lim = settings.max_html_chars if max_chars is None else max(1, min(max_chars, 2_000_000))
    js = "document.documentElement.outerHTML"
    res = browser_execute_js(
        settings,
        browser,
        js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )
    html = res.get("result", "")
    html, truncated = truncate(html, lim)
    return {"ok": True, "html": html, "truncated": truncated}


def browser_wait_for_download(
    settings: Settings,
    filename_contains: Optional[str] = None,
    timeout_s: int = 60,
    started_after_epoch_ms: Optional[int] = None,
    stable_ms: int = 500,
) -> Dict[str, Any]:
    timeout_s = max(1, min(timeout_s, settings.max_wait_s))
    stable_ms = max(100, min(int(stable_ms), 5_000))
    needle = (filename_contains or "").strip().lower()
    started_after = int(started_after_epoch_ms) if started_after_epoch_ms is not None else None

    dl = settings.download_dir
    if not dl.exists() or not dl.is_dir():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Download dir not found: {dl}")

    before: Dict[str, Tuple[int, int]] = {}
    for p in dl.iterdir():
        try:
            if p.is_file():
                stat = p.stat()
                before[p.name] = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            continue

    started = time.monotonic()
    stable: Dict[str, Tuple[Tuple[int, int], float]] = {}
    while True:
        partial_names = {
            p.name.lower()
            for p in dl.iterdir()
            if p.name.lower().endswith((".download", ".crdownload", ".part", ".tmp"))
        }
        candidates: List[Tuple[int, Path, Tuple[int, int]]] = []
        for p in dl.iterdir():
            try:
                if not p.is_file():
                    continue
                lowered = p.name.lower()
                if lowered.endswith((".download", ".crdownload", ".part", ".tmp")):
                    continue
                if needle and needle not in lowered:
                    continue
                stat = p.stat()
                signature = (int(stat.st_size), int(stat.st_mtime_ns))
                changed = p.name not in before or signature != before.get(p.name)
                since_match = started_after is not None and int(stat.st_mtime_ns // 1_000_000) >= started_after
                if not changed and not since_match:
                    continue
                # A browser partial artifact that still names the final file means completion is not verified yet.
                if any(name.startswith(lowered) or lowered.startswith(name.rsplit(".", 1)[0]) for name in partial_names):
                    continue
                candidates.append((int(stat.st_mtime_ns), p, signature))
            except OSError:
                continue

        candidates.sort(key=lambda item: item[0], reverse=True)
        now = time.monotonic()
        for _, candidate, signature in candidates:
            key = str(candidate)
            previous = stable.get(key)
            if previous is None or previous[0] != signature:
                stable[key] = (signature, now)
                continue
            if (now - previous[1]) * 1000 < stable_ms:
                continue
            try:
                artifact = register_artifact(candidate, source="browser_download")
            except ArtifactError:
                stable.pop(key, None)
                continue
            return {
                "ok": True,
                "completed": True,
                "path": str(candidate),
                "filename": candidate.name,
                "artifact_id": artifact["artifact_id"],
                "artifact": artifact,
                "stable_ms": stable_ms,
                "elapsed_s": round(now - started, 3),
            }

        if now - started >= timeout_s:
            return {
                "ok": True,
                "completed": False,
                "path": None,
                "filename": None,
                "artifact_id": None,
                "artifact": None,
                "stable_ms": stable_ms,
                "elapsed_s": round(now - started, 3),
            }
        time.sleep(0.1)


def _browser_file_metadata_js(css_selector: str) -> str:
    sel = json.dumps(css_selector)
    return (
        "(()=>{const el=document.querySelector(" + sel + ");"
        "if(!el)return JSON.stringify({ok:false,error:'not_found'});"
        "if(!(el instanceof HTMLInputElement)||el.type!=='file')return JSON.stringify({ok:false,error:'not_file_input'});"
        "const f=el.files&&el.files[0];"
        "return JSON.stringify({ok:true,count:el.files?el.files.length:0,name:f?f.name:null,size:f?f.size:null,lastModified:f?f.lastModified:null});})()"
    )


def _prepare_safari_upload_tab(target: browser_tabs.TabTarget) -> Dict[str, Any]:
    rows = browser_tabs.list_tabs("Safari")
    previous = next(
        (row for row in rows if int(row.get("window_index") or 0) == int(target.window_index) and bool(row.get("active"))),
        None,
    )
    previous_handle = str((previous or {}).get("tab_handle") or "") or None
    changed = bool(previous_handle and previous_handle != target.tab_handle)
    if changed:
        leases = browser_tabs.logical_lease_snapshot()
        previous_lease = leases.get(str(previous_handle)) or {}
        current_owner = browser_tabs._logical_owner()[0]
        lease_owner = previous_lease.get("owner")
        if lease_owner and lease_owner != current_owner:
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "previous_safari_tab_busy", "retryable": True,
                "tab_handle": previous_handle,
                "message": "Safari's current tab in the target window is owned by another agent; upload will not replace it.",
            })
    guard = _tab_identity_guard(target)
    script = f'''
    tell application "Safari"
        tell window {target.window_index}
            {guard}
            set current tab to targetTab
        end tell
        activate
    end tell
    '''
    _run_osascript(script, timeout_s=10)
    return {
        "changed": changed,
        "previous_tab_handle": previous_handle,
        "target_tab_handle": target.tab_handle,
        "window_index": target.window_index,
    }


def _restore_safari_upload_tab(state: Dict[str, Any]) -> Dict[str, Any]:
    if not state.get("changed"):
        return {"attempted": False, "ok": True, "exact": True, "skipped_user_change": False}
    previous_handle = str(state.get("previous_tab_handle") or "")
    target_handle = str(state.get("target_tab_handle") or "")
    original_window = int(state.get("window_index") or 0)
    rows = browser_tabs.list_tabs("Safari")
    active = next(
        (row for row in rows if int(row.get("window_index") or 0) == original_window and bool(row.get("active"))),
        None,
    )
    active_handle = str((active or {}).get("tab_handle") or "") or None
    if active_handle == previous_handle:
        return {"attempted": False, "ok": True, "exact": True, "skipped_user_change": False}
    if active_handle != target_handle:
        return {
            "attempted": False, "ok": True, "exact": False, "skipped_user_change": True,
            "message": "Safari tab restoration skipped because the current tab changed during upload.",
        }
    try:
        wi, _, row = browser_tabs.resolve_tab("Safari", previous_handle)
    except KeyError:
        return {"attempted": True, "ok": False, "exact": False, "skipped_user_change": False, "message": "Previous Safari tab closed before restoration."}
    if int(wi) != original_window:
        return {
            "attempted": False, "ok": True, "exact": False, "skipped_user_change": True,
            "message": "Safari tab restoration skipped because the previous tab moved to another window.",
        }
    previous_target = browser_tabs._target_from_row(row)
    guard = _tab_identity_guard(previous_target)
    script = f'''
    tell application "Safari"
        tell window {previous_target.window_index}
            {guard}
            set current tab to targetTab
        end tell
    end tell
    '''
    try:
        _run_osascript(script, timeout_s=10)
        _, _, verified = browser_tabs.resolve_tab("Safari", previous_handle)
        exact = bool(verified.get("active"))
        return {
            "attempted": True, "ok": exact, "exact": exact, "skipped_user_change": False,
            "message": "previous Safari tab restored" if exact else "Previous Safari tab could not be verified after restoration.",
        }
    except Exception as exc:
        return {"attempted": True, "ok": False, "exact": False, "skipped_user_change": False, "message": str(exc)}


def browser_upload_artifact(
    settings: Settings,
    browser: str,
    css_selector: str,
    artifact_id: str,
    path: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    timeout_s: int = 20,
    preserve_focus: bool = True,
    handoff_id: Optional[str] = None,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    if b == "Safari":
        require_foreground_authorization("browser_upload_artifact", browser=b)
    artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
    timeout_s = max(2, min(int(timeout_s), settings.max_wait_s, 60))
    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        if handoff_id:
            try:
                resolve_browser_upload_handoff(
                    handoff_id, browser=b, tab_handle=target.tab_handle, current_url=target.url,
                    css_selector=css_selector, artifact_id=artifact_id, path=path,
                )
            except HandoffError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": exc.code.lower(), "reason_code": exc.code,
                    "message": str(exc), **exc.extra,
                }) from exc
        if b == "Google Chrome":
            try:
                if handoff_id:
                    mark_handoff_consumed(handoff_id, consumer="browser_upload_artifact:chrome")
                response = chrome_background_bridge.request_set_file_input(
                    target.native_id, css_selector, str(artifact["path"]), timeout_s=timeout_s,
                )
                metadata = json.loads(str(response.get("metadata") or "{}"))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Chrome file input verification failed: {exc}") from exc
            if int(metadata.get("count") or 0) < 1 or metadata.get("name") != artifact.get("filename") or int(metadata.get("size") or -1) != int(artifact.get("size") or -2):
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "artifact_upload_identity_mismatch",
                    "message": "Chrome file input metadata did not match the registered artifact.",
                })
            artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
            return {
                "ok": True, "browser": b, "tab_handle": target.tab_handle,
                "artifact": artifact, "file_input": metadata,
                "transport": "chrome_debugger_dom_set_file_input", "focus_preserved": True,
                **({"handoff_id": handoff_id, "handoff_consumed": True} if handoff_id else {}),
            }

        # Safari permits a file-input click through AppleScript JavaScript, but the actual file
        # selection is still performed by the native NSOpenPanel using exact artifact identity.
        from .tools_ui import _capture_focus_context, _post_action_focus_decision, _restore_focus_context
        focus_context = None
        if preserve_focus:
            focus_context, focus_error = _capture_focus_context()
            if focus_context is None:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "focus_snapshot_failed", "message": focus_error,
                })
        pid_text = _run_osascript('tell application "System Events" to get unix id of application process "Safari"')
        try:
            safari_pid = int(str(pid_text).strip())
        except ValueError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve Safari process identity.") from exc
        if (
            preserve_focus and focus_context is not None
            and int(focus_context.get("pid") or 0) != safari_pid
            and int(focus_context.get("window_count") or 0) > 1
            and not focus_context.get("window_handle")
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "ok": False, "error": "focus_snapshot_ambiguous", "retryable": False,
                "message": "The current user window has no stable identity, so Safari upload cannot restore focus safely.",
            })
        focus_restore_attempted = False
        focus_restore_ok: Optional[bool] = None
        focus_restore_message: Optional[str] = None
        focus_restore_exact = False
        focus_user_changed = False
        safari_tab_state: Optional[Dict[str, Any]] = None
        safari_tab_restore: Optional[Dict[str, Any]] = None
        try:
            safari_tab_state = _prepare_safari_upload_tab(target)
            sel = json.dumps(css_selector)
            open_js = (
                "(()=>{const el=document.querySelector(" + sel + ");"
                "if(!el)return JSON.stringify({ok:false,error:'not_found'});"
                "if(!(el instanceof HTMLInputElement)||el.type!=='file')return JSON.stringify({ok:false,error:'not_file_input'});"
                "el.click();return JSON.stringify({ok:true});})()"
            )
            opened_raw = _execute_js_for_target(b, open_js, target, timeout_s=min(timeout_s, 20))
            try:
                opened = json.loads(opened_raw or "{}")
            except json.JSONDecodeError:
                opened = {"ok": False, "error": "invalid_trigger_result"}
            if not opened.get("ok"):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, {
                    "ok": False, "error": str(opened.get("error") or "file_input_trigger_failed"),
                })
            if not wait_for_file_dialog(safari_pid, "open", timeout_s=min(timeout_s, 8)):
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "file_dialog_not_opened", "retryable": False,
                })
            try:
                if handoff_id:
                    mark_handoff_consumed(handoff_id, consumer="browser_upload_artifact:safari")
                dialog = drive_native_file_dialog(
                    pid=safari_pid, mode="open", artifact_id=artifact_id, path=path, timeout_s=timeout_s,
                )
            except HandoffError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": exc.code.lower(), "reason_code": exc.code,
                    "message": str(exc), **exc.extra,
                }) from exc
            except ArtifactError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": exc.code.lower(), "reason_code": exc.code,
                    "message": str(exc), **exc.extra,
                }) from exc
            verify_raw = _execute_js_for_target(b, _browser_file_metadata_js(css_selector), target, timeout_s=min(timeout_s, 20))
            try:
                metadata = json.loads(verify_raw or "{}")
            except json.JSONDecodeError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Safari file input verification returned invalid JSON.") from exc
            if not metadata.get("ok") or int(metadata.get("count") or 0) < 1 or metadata.get("name") != artifact.get("filename") or int(metadata.get("size") or -1) != int(artifact.get("size") or -2):
                raise HTTPException(status.HTTP_409_CONFLICT, {
                    "ok": False, "error": "artifact_upload_identity_mismatch",
                    "message": "Safari file input metadata did not match the registered artifact.",
                })
            artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
            result: Dict[str, Any] = {
                "ok": True, "browser": b, "tab_handle": target.tab_handle,
                "artifact": artifact, "file_input": metadata, "dialog": dialog,
                "transport": "safari_native_open_panel",
                **({"handoff_id": handoff_id, "handoff_consumed": True} if handoff_id else {}),
            }
        finally:
            if safari_tab_state is not None:
                safari_tab_restore = _restore_safari_upload_tab(safari_tab_state)
            if preserve_focus and focus_context is not None:
                decision, _ = _post_action_focus_decision(
                    focus_context, {"pid": safari_pid, "window_index": target.window_index}
                )
                if decision == "restore":
                    focus_restore_attempted = True
                    focus_restore_ok, focus_restore_message, focus_restore_exact = _restore_focus_context(focus_context)
                elif decision == "user_changed":
                    focus_user_changed = True
                else:
                    focus_restore_ok = True
        if safari_tab_restore is not None:
            result["tab_restore_attempted"] = bool(safari_tab_restore.get("attempted"))
            result["tab_restore_ok"] = bool(safari_tab_restore.get("ok"))
            result["tab_restore_exact"] = bool(safari_tab_restore.get("exact"))
            if safari_tab_restore.get("skipped_user_change"):
                result["tab_restore_skipped_user_change"] = True
            if safari_tab_restore.get("message"):
                result["tab_restore_message"] = safari_tab_restore.get("message")
            if safari_tab_restore.get("ok") is False:
                return {
                    **result, "ok": False, "reason_code": "TAB_RESTORE_FAILED",
                    "error": "artifact upload succeeded but the previous Safari tab could not be restored",
                    "automatic_retry": False,
                }
        if preserve_focus:
            result["focus_restore_attempted"] = focus_restore_attempted
            result["focus_restore_ok"] = focus_restore_ok
            result["focus_restore_exact"] = bool(focus_restore_exact)
            result["focus_user_changed"] = focus_user_changed
            if focus_restore_message:
                result["focus_restore_message"] = focus_restore_message
            result["focus_preserved"] = bool(focus_restore_ok or focus_user_changed)
            if focus_restore_attempted and focus_restore_ok is False:
                return {
                    **result, "ok": False, "reason_code": "FOCUS_RESTORE_FAILED",
                    "error": "artifact upload succeeded but previous focus could not be restored",
                    "automatic_retry": False,
                }
        return result


# Advanced browser tools


def browser_screenshot(
    settings: Settings,
    browser: str,
    path: Optional[str] = None,
    window_index: int = 1,
    return_base64: bool = True,
) -> Dict[str, Any]:
    """Capture only the browser window, not the full screen.
    If path is not provided, a temporary /tmp file is used and deleted after base64 output is produced."""
    b = _norm_browser(browser)
    save_to_tmp = path is None
    if save_to_tmp:
        path = f"/tmp/mac_mcp_shot_{int(time.time() * 1000)}.png"

    # Get window bounds with AppleScript
    if b == "Safari":
        bounds_script = f'tell application "Safari" to return bounds of window {window_index}'
    else:
        bounds_script = f'tell application "Google Chrome" to return bounds of window {window_index}'

    bounds_raw = _run_osascript(bounds_script)

    # bounds_raw: "x, y, right, bottom"
    try:
        parts = [int(v.strip()) for v in bounds_raw.split(",")]
        x, y, right, bottom = parts
        w, h = right - x, bottom - y
    except Exception:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not read window bounds: {bounds_raw!r}")

    proc = subprocess.run(
        ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"screencapture failed: {proc.stderr.strip()}")

    result: Dict[str, Any] = {
        "ok": True,
        "path": path,
        "bounds": {"x": x, "y": y, "w": w, "h": h},
        "foreground_forced": False,
        "warning": (
            "Native region capture no longer raises the browser. If it is obscured, "
            "prefer DOM observation or a protocol-level screenshot for reliable pixels."
        ),
    }
    if return_base64:
        try:
            with open(path, "rb") as f:
                result["base64"] = base64.b64encode(f.read()).decode()
            result["mime_type"] = "image/png"
        except Exception as e:
            result["base64_error"] = str(e)

    # Clean up the temporary file when no path was provided
    if save_to_tmp:
        try:
            os.remove(path)
            result["path"] = None  # No longer exists on disk
        except OSError:
            pass

    return result


def browser_scroll(
    settings: Settings,
    browser: str,
    dx: int = 0,
    dy: int = 300,
    selector: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """Scroll the page. If selector is provided, scroll that element; otherwise scroll by dx/dy."""
    if selector:
        sel = json.dumps(selector)
        js = (
            f"(function(){{"
            f"var el=document.querySelector({sel});"
            f"if(!el) return 'NOT_FOUND';"
            f"el.scrollIntoView({{behavior:'instant',block:'center',inline:'nearest'}});"
            f"return 'OK';"
            f"}})()"
        )
    else:
        js = f"(function(){{window.scrollBy({dx},{dy});return 'OK';}})()"

    return browser_execute_js(
        settings,
        browser,
        js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )


# macOS key code table
_KEY_CODES: Dict[str, int] = {
    "return": 36, "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "pageup": 116, "pagedown": 121,
    "home": 115, "end": 119,
    "forwarddelete": 117,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MODIFIER_MAP: Dict[str, str] = {
    "cmd": "command down", "command": "command down",
    "opt": "option down", "option": "option down", "alt": "option down",
    "ctrl": "control down", "control": "control down",
    "shift": "shift down",
}


def browser_press_key(
    settings: Settings,
    browser: str,
    key: str,
    modifiers: Optional[List[str]] = None,
    window_index: int = 1,
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    """Send a keyboard key. Examples: 'return', 'escape', 'a', 'tab'.
    modifiers is an optional list such as ['cmd'] or ['shift']."""
    b = _norm_browser(browser)
    if not allow_foreground:
        return {
            "ok": False,
            "foreground_required": True,
            "reason_code": "FOREGROUND_REQUIRED",
            "reason": (
                "Native keyboard events require foreground focus. Normal automation may not change user focus; "
                "prefer browser_act DOM actions. A model-set allow_foreground flag is not an authorization."
            ),
        }
    require_foreground_authorization("browser_press_key", browser=b)
    process_name = "Safari" if b == "Safari" else "Google Chrome"

    mod_strs = []
    for m in (modifiers or []):
        mapped = _MODIFIER_MAP.get(m.lower())
        if mapped:
            mod_strs.append(mapped)

    using_clause = f" using {{{', '.join(mod_strs)}}}" if mod_strs else ""
    key_lower = key.lower()

    if key_lower in _KEY_CODES:
        code = _KEY_CODES[key_lower]
        action = f"key code {code}{using_clause}"
    else:
        # Tek karakter → keystroke
        char = _js_escape(key[:1])
        action = f'keystroke "{char}"{using_clause}'

    script = f'''
tell application "System Events"
    tell process "{process_name}"
        set frontmost to true
        {action}
    end tell
end tell
'''
    _run_osascript(script)
    return {"ok": True, "key": key, "modifiers": modifiers or []}


def browser_coordinate_click(
    settings: Settings,
    browser: str,
    x: int,
    y: int,
    double_click: bool = False,
    window_index: int = 1,
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    """Click an absolute X/Y screen coordinate, usually using rect values from browser_get_snapshot."""
    b = _norm_browser(browser)
    if not allow_foreground:
        return {
            "ok": False,
            "foreground_required": True,
            "reason_code": "FOREGROUND_REQUIRED",
            "reason": (
                "Native coordinate clicks require foreground focus. Prefer browser_act/browser_find DOM targeting; "
                "a model-set allow_foreground flag is not an authorization."
            ),
        }
    require_foreground_authorization("browser_coordinate_click", browser=b)
    process_name = "Safari" if b == "Safari" else "Google Chrome"

    # Explicit opt-in only: native screen coordinates require the browser to be frontmost.
    _run_osascript(f'tell application "{b}" to activate')
    time.sleep(0.2)

    if double_click:
        action = f"double click at {{{x}, {y}}}"
    else:
        action = f"click at {{{x}, {y}}}"

    script = f'''
tell application "System Events"
    tell process "{process_name}"
        set frontmost to true
        {action}
    end tell
end tell
'''
    _run_osascript(script)
    return {"ok": True, "x": x, "y": y, "double_click": double_click}


_SNAPSHOT_JS = r"""
(function(maxDepth, maxChildren) {
    var scrollX = window.scrollX, scrollY = window.scrollY;
    var winH = window.innerHeight, winW = window.innerWidth;

    function isVisible(el) {
        var s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (parseFloat(s.opacity) === 0) return false;
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        return true;
    }

    function buildNode(el, depth) {
        if (depth > maxDepth) return null;
        if (!isVisible(el)) return null;
        var tag = el.tagName.toLowerCase();
        var r = el.getBoundingClientRect();
        var node = {
            tag: tag,
            rect: {
                x: Math.round(r.left + scrollX),
                y: Math.round(r.top + scrollY),
                w: Math.round(r.width),
                h: Math.round(r.height),
                vx: Math.round(r.left),
                vy: Math.round(r.top)
            }
        };
        if (el.id) node.id = el.id;
        var cls = el.className;
        if (cls && typeof cls === 'string' && cls.trim()) node.cls = cls.trim().substring(0, 80);
        var aria = el.getAttribute('aria-label');
        if (aria) node.aria = aria.substring(0, 120);
        var role = el.getAttribute('role');
        if (role) node.role = role;
        var ph = el.getAttribute('placeholder');
        if (ph) node.placeholder = ph.substring(0, 80);
        var title = el.getAttribute('title');
        if (title) node.title = title.substring(0, 80);
        if (tag === 'a' && el.href) node.href = el.href.substring(0, 200);
        if (['input','textarea','select'].includes(tag)) {
            node.value = (el.value || '').substring(0, 150);
            if (tag === 'input') node.type = el.type;
            node.name = el.name || '';
        }
        if (tag === 'button' || el.getAttribute('role') === 'button') node.isButton = true;
        var text = Array.from(el.childNodes)
            .filter(function(n){ return n.nodeType === 3; })
            .map(function(n){ return n.textContent.trim(); })
            .join(' ').trim();
        if (text) node.text = text.substring(0, 200);
        var kids = Array.from(el.children)
            .slice(0, maxChildren)
            .map(function(c){ return buildNode(c, depth + 1); })
            .filter(Boolean);
        if (kids.length) node.children = kids;
        return node;
    }
    var root = buildNode(document.body, 0);
    return JSON.stringify({
        url: location.href,
        title: document.title,
        scroll: {x: scrollX, y: scrollY},
        viewport: {w: winW, h: winH},
        tree: root
    });
})(MAX_DEPTH, MAX_CHILDREN)
"""


def browser_get_snapshot(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    max_depth: int = 6,
    max_children: int = 25,
) -> Dict[str, Any]:
    """Return the visible DOM tree. Each element includes coordinates (rect).
    You can use these coordinates with browser_coordinate_click."""
    js = _SNAPSHOT_JS.replace("MAX_DEPTH", str(max_depth)).replace("MAX_CHILDREN", str(max_children))
    raw = browser_execute_js(
        settings,
        browser,
        js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )
    result_str = raw.get("result", "")
    if not result_str:
        return {"ok": False, "error": "Empty snapshot"}
    try:
        data = json.loads(result_str)
        data["ok"] = True
        return data
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}", "raw": result_str[:500]}
