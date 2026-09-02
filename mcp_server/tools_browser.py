from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

from .security import Settings, truncate, validate_url as _http_validate_url


def validate_url(settings: Settings, url: str) -> None:
    """Validate URL using browser_allowlist and browser_https_only settings."""
    from urllib.parse import urlparse
    import ipaddress, socket
    from fastapi import HTTPException, status as st
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(st.HTTP_400_BAD_REQUEST, "URL must include scheme and host.")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(st.HTTP_400_BAD_REQUEST, "Only http/https URLs are allowed.")
    if settings.browser_https_only and scheme != "https":
        raise HTTPException(st.HTTP_400_BAD_REQUEST, "Only HTTPS URLs are allowed.")
    allowlist = settings.browser_allowlist
    if "*" not in allowlist:
        host = hostname.rstrip(".")
        if not any(host == a.rstrip(".") or host.endswith("." + a.rstrip(".")) for a in allowlist):
            raise HTTPException(st.HTTP_400_BAD_REQUEST, "Hostname not in BROWSER_ALLOWLIST.")
    try:
        ipaddress.ip_address(hostname)
        raise HTTPException(st.HTTP_400_BAD_REQUEST, "Direct IP URLs are not allowed.")
    except ValueError:
        pass


BROWSERS = {
    "safari": "Safari",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
}


def _norm_browser(browser: str) -> str:
    key = (browser or "").strip().lower()
    if key in BROWSERS:
        return BROWSERS[key]
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "browser must be 'Safari' or 'Google Chrome'.")


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
    proc = subprocess.Popen(
        ["osascript", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        raise HTTPException(status.HTTP_408_REQUEST_TIMEOUT, "AppleScript timed out.")

    if proc.returncode != 0:
        msg = (stderr or stdout or "AppleScript error").strip()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, msg)
    return (stdout or "").strip()

def browser_open_url(
    settings: Settings,
    browser: str,
    url: str,
    new_tab: bool = True,
    activate: bool = True,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    validate_url(settings, url)

    if b == "Safari":
        # Safari: make new document if no window exists
        script = f'''
        tell application "Safari"
            activate
            if (count of windows) = 0 then
                make new document
            end if
            if {str(new_tab).lower()} then
                tell window 1
                    set newTab to make new tab with properties {{URL:"{url}"}}
                    set current tab to newTab
                end tell
            else
                set URL of current tab of window 1 to "{url}"
            end if
        end tell
        '''
        _run_osascript(script, timeout_s=30)
        return {"ok": True, "browser": b, "url": url}

    # Chrome
    script = f'''
    tell application "Google Chrome"
        activate
        if (count of windows) = 0 then
            make new window
        end if
        if {str(new_tab).lower()} then
            tell window 1
                set newTab to make new tab with properties {{URL:"{url}"}}
                set active tab index to (index of newTab)
            end tell
        else
            set URL of active tab of window 1 to "{url}"
        end if
    end tell
    '''
    _run_osascript(script, timeout_s=30)
    return {"ok": True, "browser": b, "url": url}


def browser_list_tabs(settings: Settings, browser: str) -> Dict[str, Any]:
    b = _norm_browser(browser)

    if b == "Safari":
        script = r'''
        set out to ""
        tell application "Safari"
            set wCount to count of windows
            repeat with wi from 1 to wCount
                tell window wi
                    set tCount to count of tabs
                    set cur to index of current tab
                    repeat with ti from 1 to tCount
                        set t to tab ti
                        set isActive to (ti = cur)
                        set out to out & wi & "\t" & ti & "\t" & isActive & "\t" & (name of t) & "\t" & (URL of t) & "\n"
                    end repeat
                end tell
            end repeat
        end tell
        return out
        '''
        raw = _run_osascript(script, timeout_s=30)

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
                        set isActive to (ti = cur)
                        set out to out & wi & "\t" & ti & "\t" & isActive & "\t" & (title of t) & "\t" & (URL of t) & "\n"
                    end repeat
                end tell
            end repeat
        end tell
        return out
        '''
        raw = _run_osascript(script, timeout_s=30)

    tabs: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        wi, ti, active, title, url = parts[0], parts[1], parts[2], parts[3], parts[4]
        tabs.append({
            "window_index": int(wi),
            "tab_index": int(ti),
            "active": active.strip().lower() == "true",
            "title": title,
            "url": url,
        })
    return {"ok": True, "browser": b, "tabs": tabs}


def browser_activate_tab(settings: Settings, browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    if window_index < 1 or tab_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index and tab_index must be >= 1")

    if b == "Safari":
        script = f'''
        tell application "Safari"
            activate
            tell window {window_index}
                set current tab to tab {tab_index}
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Google Chrome"
            activate
            tell window {window_index}
                set active tab index to {tab_index}
            end tell
        end tell
        '''
    _run_osascript(script, timeout_s=30)
    return {"ok": True, "browser": b, "window_index": window_index, "tab_index": tab_index}


def browser_close_tab(settings: Settings, browser: str, window_index: int = 1, tab_index: int = 1) -> Dict[str, Any]:
    b = _norm_browser(browser)
    if window_index < 1 or tab_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index and tab_index must be >= 1")

    if b == "Safari":
        script = f'''
        tell application "Safari"
            tell window {window_index}
                close tab {tab_index}
            end tell
        end tell
        '''
    else:
        script = f'''
        tell application "Google Chrome"
            tell window {window_index}
                close tab {tab_index}
            end tell
        end tell
        '''
    _run_osascript(script, timeout_s=30)
    return {"ok": True, "browser": b, "window_index": window_index, "tab_index": tab_index}


def _js_escape(js: str) -> str:
    # Safe AppleScript string literal: escape backslashes and quotes
    return (js or "").replace("\\", "\\\\").replace('"', '\\"')


def browser_execute_js(
    settings: Settings,
    browser: str,
    js: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
) -> Dict[str, Any]:
    b = _norm_browser(browser)
    js_escaped = _js_escape(js)

    if window_index < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "window_index must be >= 1")

    if b == "Safari":
        if tab_index is None:
            script = f'''
            tell application "Safari"
                set r to do JavaScript "{js_escaped}" in current tab of window {window_index}
                return r
            end tell
            '''
        else:
            if tab_index < 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "tab_index must be >= 1")
            script = f'''
            tell application "Safari"
                set r to do JavaScript "{js_escaped}" in tab {tab_index} of window {window_index}
                return r
            end tell
            '''
    else:
        if tab_index is None:
            script = f'''
            tell application "Google Chrome"
                set r to execute javascript "{js_escaped}" in active tab of window {window_index}
                return r
            end tell
            '''
        else:
            if tab_index < 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "tab_index must be >= 1")
            script = f'''
            tell application "Google Chrome"
                set r to execute javascript "{js_escaped}" in tab {tab_index} of window {window_index}
                return r
            end tell
            '''

    raw = _run_osascript(script, timeout_s=min(60, settings.max_wait_s))
    raw, truncated = truncate(raw, settings.max_js_result_chars)
    return {"ok": True, "browser": b, "result": raw, "truncated": truncated}


def browser_click_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
) -> Dict[str, Any]:
    sel = json.dumps(css_selector)
    js = f"(function(){{var el=document.querySelector({sel}); if(!el) return 'NOT_FOUND'; el.click(); return 'OK';}})()"
    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


def browser_type_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    text: str,
    clear: bool = True,
    window_index: int = 1,
    tab_index: Optional[int] = None,
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
    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


def browser_wait_for_selector(
    settings: Settings,
    browser: str,
    css_selector: str,
    timeout_s: int = 20,
    poll_ms: int = 250,
    window_index: int = 1,
    tab_index: Optional[int] = None,
) -> Dict[str, Any]:
    if timeout_s < 1:
        timeout_s = 1
    if poll_ms < 50:
        poll_ms = 50

    sel = json.dumps(css_selector)
    start = time.time()
    while True:
        js = f"(function(){{return !!document.querySelector({sel});}})()"
        res = browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)
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
) -> Dict[str, Any]:
    lim = settings.max_html_chars if max_chars is None else max(1, min(max_chars, 2_000_000))
    js = "document.documentElement.outerHTML"
    res = browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)
    html = res.get("result", "")
    html, truncated = truncate(html, lim)
    return {"ok": True, "html": html, "truncated": truncated}


def browser_wait_for_download(
    settings: Settings,
    filename_contains: Optional[str] = None,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    timeout_s = max(1, min(timeout_s, settings.max_wait_s))
    needle = (filename_contains or "").strip().lower()

    dl = settings.download_dir
    if not dl.exists() or not dl.is_dir():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Download dir not found: {dl}")

    start = time.time()
    # Snapshot existing files
    before: Dict[str, float] = {p.name: p.stat().st_mtime for p in dl.iterdir() if p.is_file()}

    while True:
        candidates: List[Tuple[float, Path]] = []
        for p in dl.iterdir():
            if not p.is_file():
                continue
            if p.name.endswith(".download") or p.name.endswith(".crdownload"):
                continue
            if needle and needle not in p.name.lower():
                continue
            m = p.stat().st_mtime
            if p.name not in before or m > before.get(p.name, 0):
                candidates.append((m, p))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            newest = candidates[0][1]
            return {"ok": True, "path": str(newest), "filename": newest.name}

        if time.time() - start >= timeout_s:
            return {"ok": True, "path": None, "filename": None}

        time.sleep(0.25)


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
        activate_script = f'tell application "Safari" to activate'
    else:
        bounds_script = f'tell application "Google Chrome" to return bounds of window {window_index}'
        activate_script = f'tell application "Google Chrome" to activate'

    bounds_raw = _run_osascript(bounds_script)
    _run_osascript(activate_script)
    time.sleep(0.25)

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

    result: Dict[str, Any] = {"ok": True, "path": path, "bounds": {"x": x, "y": y, "w": w, "h": h}}
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

    return browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)


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
) -> Dict[str, Any]:
    """Send a keyboard key. Examples: 'return', 'escape', 'a', 'tab'.
    modifiers is an optional list such as ['cmd'] or ['shift']."""
    b = _norm_browser(browser)
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
) -> Dict[str, Any]:
    """Click an absolute X/Y screen coordinate, usually using rect values from browser_get_snapshot."""
    b = _norm_browser(browser)
    process_name = "Safari" if b == "Safari" else "Google Chrome"

    # Bring the browser to the front first
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
    max_depth: int = 6,
    max_children: int = 25,
) -> Dict[str, Any]:
    """Return the visible DOM tree. Each element includes coordinates (rect).
    You can use these coordinates with browser_coordinate_click."""
    js = _SNAPSHOT_JS.replace("MAX_DEPTH", str(max_depth)).replace("MAX_CHILDREN", str(max_children))
    raw = browser_execute_js(settings, browser, js, window_index=window_index, tab_index=tab_index)
    result_str = raw.get("result", "")
    if not result_str:
        return {"ok": False, "error": "Empty snapshot"}
    try:
        data = json.loads(result_str)
        data["ok"] = True
        return data
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}", "raw": result_str[:500]}
