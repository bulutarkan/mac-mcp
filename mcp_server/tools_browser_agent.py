from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi import HTTPException, status
from mcp.server.fastmcp.utilities.types import Image

from .security import Settings, truncate
from .computer_use_perf import record_computer_use_sample
from . import decision_engine
from .data_guard import redact_sensitive_text
from .perception import estimate_payload_tokens, finalize_perception_telemetry, json_bytes, refresh_perception_size
from . import browser_activity, browser_tabs, safari_frames
from .chrome_background_bridge import chrome_background_bridge
from .tool_cancellation import cancellable_sleep, cancellation_checkpoint
from .workspace_arbitration import delegated_agent_identity, owner_takeover_block
from .workflow_checkpoints import mark_not_executed
from .tools_browser import (
    current_frame_scope,
    frame_scope,
    _execute_js_for_target as _raw_execute_js_for_target,
    _norm_browser,
    _require_stable_handle_for_mutation,
    _resolve_tab_target,
    _run_osascript,
    _js_escape,
    _visual_companion_source,
    _tab_identity_guard,
    _tab_lease,
    browser_execute_js,
    browser_press_key,
)

_MAX_OBSERVE_ELEMENTS = 240
_DEFAULT_OBSERVE_ELEMENTS = 40
_MAX_ACTIONS = 20
_VISUAL_MODES = {"none", "viewport", "element", "full_page"}
_RETURN_STATE_MODES = {"none", "compact", "full"}

MutationRevalidator = Callable[
    [str],
    Tuple[Optional[browser_tabs.TabTarget], Optional[Dict[str, Any]]],
]

_BROWSER_OBSERVATION_OWNER_LOCK = threading.RLock()
_BROWSER_OBSERVATION_OWNERS: Dict[str, str] = {}
_MAX_BROWSER_OBSERVATION_OWNERS = 512


def _perception_owner_key() -> str:
    identity = delegated_agent_identity()
    if identity is None:
        return "local"
    agent_id = str(identity.get("agent_id") or "").strip()
    return f"agent:{agent_id}" if agent_id else "local"


def _remember_browser_observation(observation_id: Optional[str]) -> None:
    key = str(observation_id or "").strip()
    if not key:
        return
    with _BROWSER_OBSERVATION_OWNER_LOCK:
        _BROWSER_OBSERVATION_OWNERS[key] = _perception_owner_key()
        while len(_BROWSER_OBSERVATION_OWNERS) > _MAX_BROWSER_OBSERVATION_OWNERS:
            oldest = next(iter(_BROWSER_OBSERVATION_OWNERS))
            _BROWSER_OBSERVATION_OWNERS.pop(oldest, None)


def _browser_observation_owned_by_current(observation_id: Optional[str]) -> bool:
    key = str(observation_id or "").strip()
    if not key:
        return False
    with _BROWSER_OBSERVATION_OWNER_LOCK:
        return _BROWSER_OBSERVATION_OWNERS.get(key) == _perception_owner_key()


# What each element_id meant when observe/find returned it (role and label, never a field value),
# so browser_act can find a target again after the page re-rendered it (#137). Bounded LRU.
_TARGET_IDENTITY_LOCK = threading.Lock()
_TARGET_IDENTITIES: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
_MAX_TARGET_IDENTITIES = 2_000
_IDENTITY_KEYS = ("role", "tag", "text", "aria_label", "placeholder", "name", "title")
_IDENTITY_LABEL_KEYS = ("aria_label", "text", "placeholder", "name", "title")


def _remember_target_identities(browser: str, tab_handle: Optional[str], elements: Any) -> None:
    if not tab_handle or not isinstance(elements, list):
        return
    rows = []
    for element in elements:
        if not isinstance(element, dict) or not element.get("element_id"):
            continue
        identity = {key: str(element[key])[:200] for key in _IDENTITY_KEYS if element.get(key)}
        if any(identity.get(key) for key in _IDENTITY_LABEL_KEYS):
            rows.append((str(element["element_id"]), identity))
    # A label shared by several controls in the same read (one Delete per row) cannot be told apart later.
    labels: Dict[Tuple[str, str], int] = {}
    for _, identity in rows:
        label = (identity.get("role", ""), next((identity[k] for k in _IDENTITY_LABEL_KEYS if identity.get(k)), ""))
        labels[label] = labels.get(label, 0) + 1
    b = _norm_browser(browser)
    with _TARGET_IDENTITY_LOCK:
        for element_id, identity in rows:
            label = (identity.get("role", ""), next((identity[k] for k in _IDENTITY_LABEL_KEYS if identity.get(k)), ""))
            identity["unique"] = labels.get(label, 0) == 1
            key = (b, str(tab_handle), element_id)
            _TARGET_IDENTITIES[key] = identity
            _TARGET_IDENTITIES.move_to_end(key)
        while len(_TARGET_IDENTITIES) > _MAX_TARGET_IDENTITIES:
            _TARGET_IDENTITIES.popitem(last=False)


def _remembered_target_identity(browser: str, tab_handle: Optional[str], element_id: Any) -> Optional[Dict[str, Any]]:
    if not tab_handle or not element_id:
        return None
    with _TARGET_IDENTITY_LOCK:
        found = _TARGET_IDENTITIES.get((_norm_browser(browser), str(tab_handle), str(element_id)))
        return dict(found) if found else None


_VISUAL_ENSURE_CACHE: Dict[Tuple[str, str, str], float] = {}
# Guards cache metadata only; browser I/O runs outside it. Injection into one
# tab is already single-flight because _tab_lease is exclusive per tab.
_VISUAL_ENSURE_LOCK = threading.Lock()
_VISUAL_ENSURE_TTL_S = 12.0
_DOM_RASTERIZER_PATH = Path(__file__).resolve().parent / "vendor" / "html2canvas.min.js"
_DOM_CAPTURE_STATE_PREFIX = "__macMcpVisualCapture"
_DOM_RASTERIZER_GLOBAL = "__macMcpHtml2Canvas"
_CLONE_INTERACTIVE_GRACE_MS = 1000
# html2canvas writes two fixed strings into DOM sinks. Pages that enforce Trusted
# Types (Google Sheets, Docs) reject plain strings there, so both sinks go through
# a Mac MCP policy that accepts only those two strings. The helper is a page
# global, so it must never turn arbitrary markup into TrustedHTML.
_TRUSTED_TYPES_SINK_PATCHES = (
    (
        'o.write(mn(document.doctype)+"<html></html>")',
        'o.write(__macMcpTrustedHTML(mn(document.doctype)+"<html></html>"))',
    ),
    (
        'e.innerHTML="function"==typeof"".repeat?"&#128104;".repeat(10):""',
        'e.innerHTML=__macMcpTrustedHTML("function"==typeof"".repeat?"&#128104;".repeat(10):"")',
    ),
    # html2canvas 1.4.1 aborts the whole capture on CSS Color 4 functions such
    # as color(srgb ...), which Google Sheets uses; fall back per color instead.
    (
        """if(void 0===t)throw new Error('Attempting to parse an unsupported color function "'+e.name+'"');""",
        "if(void 0===t)return __macMcpUnsupportedColor(e);",
    ),
    # html2canvas waits for the cloned document's onload before it starts polling
    # for "complete". On some pages (Medium) that load never fires and the clone
    # stays "interactive", and a background Safari tab throttles timers to about a
    # tick per second, so the capture hung until its 18 s timeout. The check reads
    # the live clone document, accepts a settled "interactive" clone after a grace
    # period, and is exposed as __macMcpClonePump so every status call drives it
    # regardless of timer throttling.
    (
        't.onload=B.onload=function(){t.onload=B.onload=null;var A=setInterval(function(){0<r.body.childNodes.length&&"complete"===r.readyState&&(clearInterval(A),e(B))},50)}',
        'var __mcpT0=Date.now(),A=null,__mcpDone=!1,__mcpCheck=function(){if(__mcpDone)return!0;'
        'var d=B.contentWindow&&B.contentWindow.document||r,s=d&&d.readyState;'
        'return d&&d.body&&0<d.body.childNodes.length&&("complete"===s||"interactive"===s&&Date.now()-__mcpT0>'
        + str(_CLONE_INTERACTIVE_GRACE_MS)
        + ')?(__mcpDone=!0,A&&clearInterval(A),window.__macMcpClonePump=null,e(B),!0):!1};'
        'window.__macMcpClonePump=__mcpCheck;t.onload=B.onload=function(){t.onload=B.onload=null;A||(A=setInterval(__mcpCheck,50))}',
    ),
)
_TRUSTED_TYPES_PRELUDE = r"""var __macMcpTrustedHTML=(function(){
var shell=/^(<!DOCTYPE [^<>]*>)?<html><\/html>$/,emoji=new Array(11).join("&#128104;"),policy=null;
try{if(window.trustedTypes&&typeof window.trustedTypes.createPolicy==="function"){
policy=window.trustedTypes.createPolicy("mac-mcp-capture",{createHTML:function(value){
if(value===""||value===emoji||shell.test(value))return value;
throw new TypeError("mac-mcp-capture accepts only the rasterizer document shell");}});}}catch(e){policy=null;}
return function(value){value=String(value);return policy?policy.createHTML(value):value;};
})();
var __macMcpUnsupportedColor=function(fn){
var gray=((128<<24)|(128<<16)|(128<<8)|255)>>>0;
try{var parts=(fn&&fn.values||[]).filter(function(t){return t&&(t.type===17||t.type===16);}).map(function(t){return t.type===16?t.number/100:t.number;});
if(String(fn&&fn.name||"").toLowerCase()!=="color"||parts.length<3)return gray;
var c=function(v){return Math.max(0,Math.min(255,Math.round(v*255)));},a=parts.length>3?Math.max(0,Math.min(1,parts[3])):1;
return ((c(parts[0])<<24)|(c(parts[1])<<16)|(c(parts[2])<<8)|Math.round(255*a))>>>0;}catch(e){return gray;}
};
"""
_DOM_RASTERIZER_RUNTIME_LOCK = threading.Lock()
_DOM_RASTERIZER_RUNTIME: Dict[str, Any] = {}
_DOM_CAPTURE_VIEWPORT_TIMEOUT_S = 18.0
_DOM_CAPTURE_FULL_PAGE_TIMEOUT_S = 30.0
_DOM_CAPTURE_MAX_CSS_HEIGHT = 20_000
_DOM_CAPTURE_MAX_DATA_URL_CHARS = 1_800_000
_DOM_CAPTURE_STATUS_WAIT_S = 8.0
_DOM_CAPTURE_STATUS_POLL_S = 0.12
_RENDER_READINESS_TIMEOUT_S = 1.5
_RENDER_READINESS_POLL_S = 0.08
_RENDER_READINESS_STABLE_MS = 120
_ELEMENT_READINESS_TIMEOUT_S = 0.8
_ELEMENT_READINESS_POLL_S = 0.06
_ELEMENT_READINESS_STABLE_MS = 300
_ACTION_VERIFY_TIMEOUT_S = 0.55
_ACTION_VERIFY_POLL_S = 0.07
_TYPE_VERIFY_TIMEOUT_S = 1.0
# How long a rich editor gets to re-render before typed text is read again (#205).
_RENDER_SETTLE_S = 0.2
_TYPE_VERIFY_POLL_S = 0.05
_TYPE_FOCUS_SETTLE_S = 0.05
# A DOM mutation after a click counts as its effect only when the page was quiet
# this long before the click, so background animation cannot fake an effect.
_DOM_EFFECT_QUIET_MS = 250
_GENERIC_QUERY_WORDS = {
    "button", "link", "input", "field", "select", "dropdown", "combobox", "option",
    "filter", "control", "element", "box", "menu", "tab", "checkbox", "radio",
}

_SEMANTIC_EXTRACT_ALIASES = {
    "price": ["price", "fiyat", "tutar", "total", "toplam", "₺", "tl", "try", "€", "eur", "$", "usd"],
    "cancellation": ["cancellation", "cancel", "refundable", "refund", "free cancellation", "iptal", "ücretsiz iptal", "ucretsiz iptal", "iade", "iade edilebilir"],
    "parking": ["parking", "car park", "parking lot", "otopark", "park yeri", "vale", "valet"],
    "rating": ["rating", "score", "review score", "puan", "değerlendirme", "degerlendirme", "yorum puanı", "yorum puani"],
    "breakfast": ["breakfast", "kahvaltı", "kahvalti"],
    "payment": ["payment", "pay at property", "pay later", "ödeme", "odeme", "otelde ödeme", "otele ödeme", "tesiste ödeme"],
    "location": ["location", "address", "konum", "adres"],
    "address": ["address", "street address", "adres", "konum", "mahalle", "cadde", "sokak", "bulvar", "boulevard"],
    "hours": ["hours", "opening hours", "open", "closed", "closes", "opens", "çalışma saatleri", "calisma saatleri", "açık", "acik", "kapalı", "kapali", "kapanış saati", "kapanis saati"],
    "website": ["website", "web site", "web sitesi", "official website", "official site", "resmi site", "homepage"],
    "distance": ["distance", "away", "walking", "walk", "mesafe", "uzaklık", "uzaklik", "yürüme", "yurume"],
    "availability": ["availability", "available", "rooms left", "müsait", "musait", "son oda", "son odalar"],
    "checkin": ["check-in", "check in", "giriş", "giris"],
    "checkout": ["check-out", "check out", "çıkış", "cikis"],
}


def semantic_extract_fields(targets: List[str]) -> List[Dict[str, Any]]:
    """Build compact semantic field specs for browser extract actions."""
    if not isinstance(targets, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract must be a list of semantic field names.")
    if len(targets) > 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract may contain at most 12 semantic field names.")
    fields: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in targets:
        if not isinstance(raw, str):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract values must be strings.")
        target = raw.strip()
        if not target:
            continue
        if len(target) > 80:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract field names may contain at most 80 characters.")
        key = target.casefold()
        if key in seen:
            continue
        seen.add(key)
        max_items = 1 if key in {"address", "location", "website"} else 2
        fields.append({"name": target, "semantic": target, "all": True, "max_items": max_items})
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "extract must contain at least one non-empty field name.")
    return fields


def _decode_js_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    value = str(raw.get("result") or "")
    if raw.get("truncated"):
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Browser payload was truncated. Reduce max_elements or use scope='interactive'.",
        )
    if not value:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Browser returned an empty payload.")
    try:
        decoded = base64.b64decode(value).decode("utf-8")
        return json.loads(decoded)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not decode browser payload: {exc}") from exc


def _run_json_js(
    settings: Settings,
    browser: str,
    js: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    prevalidated_target: Optional[browser_tabs.TabTarget] = None,
) -> Dict[str, Any]:
    # Internal semantic reads/actions already execute under their outer operation's
    # tab lease. Keep ordinary calls non-mutating so delegated agents may still
    # observe/find the tab the human is looking at. A delegated mutation may pass the
    # fresh target returned by the immediate ownership revalidation, avoiding a
    # second tab scan before executing the side effect.
    b = _norm_browser(browser)
    if prevalidated_target is None:
        for attempt in range(2):
            try:
                with _tab_lease(b, tab_handle, window_index, tab_index) as target:
                    value = _execute_js_for_target(
                        b,
                        js,
                        target,
                        timeout_s=min(60, settings.max_wait_s),
                    )
                break
            except HTTPException as exc:
                # The identity guard refuses before any JavaScript runs, so one
                # retry against a freshly resolved tab (for example after Safari
                # swapped the tab's process on navigation) cannot repeat an effect.
                if attempt or not tab_handle or not browser_tabs.is_tab_identity_failure(exc):
                    raise
                browser_tabs.invalidate_scoped_tab(b, tab_handle)
    else:
        target = prevalidated_target
        if _norm_browser(target.browser) != b:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Prevalidated browser target no longer matches the requested browser.",
            )
        value = _execute_js_for_target(
            b,
            js,
            target,
            timeout_s=min(60, settings.max_wait_s),
        )
    # These payloads are read by the server, which bounds what reaches the caller by the
    # perception budget; the raw browser_execute_js cap would cut an observe at ~30 elements.
    value, truncated = truncate(value, max(settings.max_js_result_chars, _AGENT_PAYLOAD_MAX_CHARS))
    return _decode_js_payload({
        "ok": True,
        "browser": b,
        "result": value,
        "truncated": truncated,
    })


def _ensure_visual_companion(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> bool:
    """Ensure the optional Visual Companion exists once per live tab document."""
    if current_frame_scope():
        return False  # the companion overlay belongs to the top-level page, not a child frame
    b = _norm_browser(browser)
    known = browser_tabs.registry_snapshot().get(str(tab_handle or "")) if tab_handle else None
    if known:
        # A companion confirmed moments ago for this exact document needs no tab scan.
        known_key = (b, str(known.get("native_id") or tab_handle), str(known.get("url") or ""))
        with _VISUAL_ENSURE_LOCK:
            last = _VISUAL_ENSURE_CACHE.get(known_key, 0.0)
        if last and time.monotonic() - last < _VISUAL_ENSURE_TTL_S:
            return True
    try:
        with _tab_lease(b, tab_handle, window_index, tab_index, allow_rebind=True) as target:
            key = (b, str(target.native_id or target.tab_handle), str(target.url or ""))
            now = time.monotonic()
            with _VISUAL_ENSURE_LOCK:
                last = _VISUAL_ENSURE_CACHE.get(key, 0.0)
            if last and now - last < _VISUAL_ENSURE_TTL_S:
                return True
            probe = _execute_js_for_target(
                b, "window.__macMcpVisualCompanionLoaded ? '1' : '0'", target, timeout_s=8,
            )
            if str(probe or "").strip() != "1":
                _execute_js_for_target(b, _visual_companion_source(), target, timeout_s=10)
                probe = _execute_js_for_target(
                    b, "window.__macMcpVisualCompanionLoaded ? '1' : '0'", target, timeout_s=8,
                )
            ok = str(probe or "").strip() == "1"
            if ok:
                with _VISUAL_ENSURE_LOCK:
                    _VISUAL_ENSURE_CACHE[key] = now
                    # Drop old cache entries for the same native tab after navigation/reload.
                    for old_key in list(_VISUAL_ENSURE_CACHE):
                        if old_key != key and old_key[:2] == key[:2]:
                            _VISUAL_ENSURE_CACHE.pop(old_key, None)
            return ok
    except Exception:
        # Companion is UX only. Browser automation must continue even if browser policy
        # blocks the visual injection. The action script still emits private metadata.
        return False


def _execute_js_unbounded(
    browser: str,
    js: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    timeout_s: int = 30,
) -> str:
    """Execute JS without the normal model-facing result truncation.

    This is intentionally private to the browser visual pipeline so a compressed image
    data URL can cross the local AppleEvent boundary once, then be decoded to MCP image
    content. The data URL is never returned in the text payload.
    """
    b = _norm_browser(browser)
    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        return _execute_js_for_target(
            b,
            js,
            target,
            timeout_s=max(1, min(int(timeout_s), 60)),
        )


def _dom_rasterizer_source() -> str:
    """Vendored html2canvas with its two DOM sinks routed through the Trusted Types helper."""
    with _DOM_RASTERIZER_RUNTIME_LOCK:
        cached = _DOM_RASTERIZER_RUNTIME.get("source")
        if cached:
            return str(cached)
        source = _DOM_RASTERIZER_PATH.read_text(encoding="utf-8")
        for original, patched in _TRUSTED_TYPES_SINK_PATCHES:
            if source.count(original) != 1:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "DOM screenshot rasterizer does not match the expected html2canvas build.",
                )
            source = source.replace(original, patched)
        source = _TRUSTED_TYPES_PRELUDE + source
        _DOM_RASTERIZER_RUNTIME["source"] = source
        return source


def _dom_rasterizer_runtime_path() -> Path:
    """Private on-disk copy of the patched rasterizer for Safari's AppleScript loader."""
    source = _dom_rasterizer_source()
    with _DOM_RASTERIZER_RUNTIME_LOCK:
        cached = _DOM_RASTERIZER_RUNTIME.get("path")
        if cached and Path(cached).is_file():
            return Path(cached)
        directory = Path(tempfile.mkdtemp(prefix="mac-mcp-rasterizer-"))
        path = directory / "html2canvas-trusted-types.js"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
        _DOM_RASTERIZER_RUNTIME["path"] = str(path)
        return path


def _ensure_dom_rasterizer(
    browser: str,
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
) -> None:
    marker = _execute_js_unbounded(
        browser,
        f"typeof window.{_DOM_RASTERIZER_GLOBAL}",
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
        timeout_s=10,
    )
    if marker.strip() == "function":
        return
    if not _DOM_RASTERIZER_PATH.exists():
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"DOM screenshot rasterizer is missing: {_DOM_RASTERIZER_PATH}",
        )

    b = _norm_browser(browser)
    pre_raw = (
        "window.__macMcpHadHtml2Canvas=Object.prototype.hasOwnProperty.call(window,'html2canvas');"
        "window.__macMcpPreviousHtml2Canvas=window.html2canvas;"
    )
    post_raw = (
        f"window.{_DOM_RASTERIZER_GLOBAL}=window.html2canvas;"
        "if(window.__macMcpHadHtml2Canvas){window.html2canvas=window.__macMcpPreviousHtml2Canvas;}"
        "else{try{delete window.html2canvas;}catch(e){window.html2canvas=undefined;}}"
        "delete window.__macMcpHadHtml2Canvas;delete window.__macMcpPreviousHtml2Canvas;"
        f"typeof window.{_DOM_RASTERIZER_GLOBAL};"
    )
    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        if b == "Google Chrome":
            source = _dom_rasterizer_source()
            _execute_js_for_target(b, pre_raw, target, timeout_s=10)
            _execute_js_for_target(b, source, target, timeout_s=30)
            loaded = _execute_js_for_target(b, post_raw, target, timeout_s=10)
        else:
            path_literal = json.dumps(str(_dom_rasterizer_runtime_path()))
            pre = _js_escape(pre_raw)
            post = _js_escape(post_raw)
            guard = _tab_identity_guard(target)
            script = f'''set js to read POSIX file {path_literal} as «class utf8»
tell application "Safari"
    tell window {target.window_index}
        {guard}
        do JavaScript "{pre}" in targetTab
        do JavaScript js in targetTab
        set r to do JavaScript "{post}" in targetTab
        return r
    end tell
end tell'''
            loaded = _run_osascript(script, timeout_s=30)
    if loaded.strip() != "function":
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Could not initialize the DOM screenshot rasterizer in the target tab.",
        )


def _render_readiness_js(mode: str, element_id: Optional[str]) -> str:
    mode_js = json.dumps(mode)
    element_js = json.dumps(element_id)
    return f'''(function(){{
var mode={mode_js},elementId={element_js};
var de=document.documentElement,body=document.body;
var alive=!!(de&&de.isConnected&&body&&body.isConnected);
var vw=Number(innerWidth||0),vh=Number(innerHeight||0);
var fullW=alive?Math.max(Number(de.scrollWidth||0),Number(de.clientWidth||0),Number(body.scrollWidth||0),Number(body.clientWidth||0),vw):0;
var fullH=alive?Math.max(Number(de.scrollHeight||0),Number(de.clientHeight||0),Number(body.scrollHeight||0),Number(body.clientHeight||0),vh):0;
var target=null,rect=null,connected=true;
if(mode==='element'){{
  var agent=window.__macMcpBrowserAgent;
  target=agent&&agent.elements&&elementId?agent.elements[elementId]:null;
  connected=!!(target&&target.isConnected);
  if(connected){{try{{rect=target.getBoundingClientRect();}}catch(e){{rect=null;}}}}
}}
var rawW=mode==='element'?(rect?Number(rect.width||0):0):(mode==='viewport'?vw:fullW);
var rawH=mode==='element'?(rect?Number(rect.height||0):0):(mode==='viewport'?vh:fullH);
var finite=isFinite(rawW)&&isFinite(rawH),positive=finite&&rawW>0&&rawH>0;
var readyState=String(document.readyState||'');
var reason='';
if(!alive)reason='RENDER_NOT_READY';
else if(mode==='element'&&!connected)reason='ELEMENT_NOT_READY';
else if(!positive)reason=mode==='element'?'ELEMENT_ZERO_BOUNDS':'ZERO_CONTENT_BOUNDS';
else if(readyState==='loading')reason='RENDER_NOT_READY';
var loadAge=-1;
try{{var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];if(nav&&nav.loadEventEnd>0)loadAge=Math.max(0,performance.now()-nav.loadEventEnd);}}catch(e){{}}
var ready=!reason;
return JSON.stringify({{ok:true,ready:ready,reason_code:reason||null,retryable:!ready,mode:mode,page_alive:alive,ready_state:readyState,raw_width:rawW,raw_height:rawH,viewport_width:vw,viewport_height:vh,full_width:fullW,full_height:fullH,element_connected:connected,load_age_ms:loadAge,signature:[readyState,Math.round(rawW*10)/10,Math.round(rawH*10)/10,Math.round(fullW),Math.round(fullH),connected].join('|')}});
}})()'''


def _wait_for_render_readiness(
    browser: str,
    mode: str,
    element_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: str,
) -> Dict[str, Any]:
    started = time.perf_counter()
    deadline = started + _RENDER_READINESS_TIMEOUT_S
    last_signature: Optional[str] = None
    stable_since = started
    attempts = 0
    last: Dict[str, Any] = {}
    while True:
        attempts += 1
        raw = _execute_js_unbounded(
            browser,
            _render_readiness_js(mode, element_id),
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
            timeout_s=10,
        )
        try:
            state = json.loads(raw or "{}")
        except json.JSONDecodeError:
            state = {"ready": False, "reason_code": "RENDER_NOT_READY", "retryable": True}
        last = state if isinstance(state, dict) else {}
        now = time.perf_counter()
        if last.get("ready"):
            signature = str(last.get("signature") or "")
            load_age = float(last.get("load_age_ms") or -1)
            if load_age >= _ELEMENT_READINESS_STABLE_MS:
                last.update({"attempts": attempts, "duration_ms": int((now - started) * 1000), "settled_by": "loaded"})
                return last
            if signature != last_signature:
                last_signature = signature
                stable_since = now
            elif (now - stable_since) * 1000 >= _RENDER_READINESS_STABLE_MS:
                last.update({"attempts": attempts, "duration_ms": int((now - started) * 1000), "settled_by": "stable_bounds"})
                return last
        else:
            last_signature = None
            stable_since = now
        if now >= deadline:
            last.update({"ready": False, "attempts": attempts, "duration_ms": int((now - started) * 1000), "timed_out": True})
            if not last.get("reason_code"):
                last["reason_code"] = "RENDER_NOT_READY"
            return last
        cancellable_sleep(_RENDER_READINESS_POLL_S)


def _dom_capture_start_js(
    mode: str,
    element_id: Optional[str],
    state_key: Optional[str] = None,
) -> str:
    state_key = state_key or f"{_DOM_CAPTURE_STATE_PREFIX}_{uuid.uuid4().hex}"
    mode_js = json.dumps(mode)
    element_js = json.dumps(element_id)
    return _fill_js(_browser_js("dom_capture.js"), {
        "__RASTERIZER__": _DOM_RASTERIZER_GLOBAL,
        "__STATE_KEY__": json.dumps(state_key),
        "__MODE__": mode_js,
        "__ELEMENT_ID__": element_js,
        "__MAX_CSS_HEIGHT__": str(_DOM_CAPTURE_MAX_CSS_HEIGHT),
        "__MAX_DATA_URL_CHARS__": str(_DOM_CAPTURE_MAX_DATA_URL_CHARS),
    })


def _dom_capture_status_js(state_key: str, wait_ms: int = 0) -> str:
    """Capture status; when finished it carries the data URL. With wait_ms it returns a
    promise that settles once the capture leaves "running" or the wait runs out."""
    return f'''(function(){{
var key={json.dumps(state_key)};
function pack(){{try{{window.__macMcpClonePump&&window.__macMcpClonePump();}}catch(e){{}}var s=window[key];if(!s)return {{status:"missing"}};
  var o={{status:s.status,error:s.error||"",meta:s.meta||{{}}}};if(s.status==="done")o.data=s.data||"";return o;}}
var first=pack(),waitMs={max(0, int(wait_ms))};
if(first.status!=="running"||!waitMs)return JSON.stringify(first);
return new Promise(function(resolve){{var until=Date.now()+waitMs;(function tick(){{var o=pack();
  if(o.status!=="running"||Date.now()>=until)resolve(JSON.stringify(o));else setTimeout(tick,40);}})();}});
}})()'''


def _capture_dom_visual(
    browser: str,
    mode: str,
    element_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str], Dict[str, Any]]:
    with _tab_lease(browser, tab_handle, window_index, tab_index) as target:
        return _capture_dom_visual_locked(
            browser=target.browser,
            mode=mode,
            element_id=element_id,
            window_index=target.window_index,
            tab_index=target.tab_index,
            tab_handle=target.tab_handle,
        )


def _capture_dom_visual_locked(
    browser: str,
    mode: str,
    element_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: str,
) -> Tuple[Optional[bytes], Optional[str], Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "mode": mode,
        "capture_method": "dom_rasterizer",
        "background_safe": True,
        "tab_activated": False,
        "disk_write": False,
    }
    state_key = f"{_DOM_CAPTURE_STATE_PREFIX}_{uuid.uuid4().hex}"
    state_key_js = json.dumps(state_key)
    try:
        _ensure_dom_rasterizer(
            browser,
            window_index,
            tab_index,
            tab_handle=tab_handle,
        )
        readiness = _wait_for_render_readiness(
            browser=browser,
            mode=mode,
            element_id=element_id,
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
        )
        meta["readiness"] = readiness
        meta["readiness_attempts"] = readiness.get("attempts")
        meta["readiness_duration_ms"] = readiness.get("duration_ms")
        if not readiness.get("ready"):
            reason_code = str(readiness.get("reason_code") or "RENDER_NOT_READY")
            meta["reason_code"] = reason_code
            return None, f"Render not ready: {reason_code}", meta
        started_raw = _execute_js_unbounded(
            browser,
            _dom_capture_start_js(mode, element_id, state_key),
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
            timeout_s=15,
        )
        try:
            started = json.loads(started_raw or "{}")
        except json.JSONDecodeError:
            started = {}
        if started.get("ok") is False:
            reason_code = str(started.get("reason_code") or "RENDER_NOT_READY")
            meta["reason_code"] = reason_code
            if started.get("raw_width") is not None:
                meta["raw_width"] = started.get("raw_width")
            if started.get("raw_height") is not None:
                meta["raw_height"] = started.get("raw_height")
            return None, str(started.get("error") or "Could not start DOM screenshot capture."), meta

        timeout_s = (
            _DOM_CAPTURE_FULL_PAGE_TIMEOUT_S
            if mode == "full_page"
            else _DOM_CAPTURE_VIEWPORT_TIMEOUT_S
        )
        deadline = time.monotonic() + timeout_s
        # The Chrome companion awaits a returned promise, so one status call can wait in
        # the page until the capture settles; other transports poll. Either way the
        # finished status carries the image, so no separate read follows.
        awaits = browser == "Google Chrome" and chrome_background_bridge.is_connected()
        finished: Dict[str, Any] = {}
        meta["status_calls"] = 0
        while time.monotonic() < deadline:
            wait_ms = int(min(_DOM_CAPTURE_STATUS_WAIT_S, max(0.0, deadline - time.monotonic() - 1.0)) * 1000) if awaits else 0
            raw = _execute_js_unbounded(
                browser,
                _dom_capture_status_js(state_key, wait_ms),
                window_index=window_index,
                tab_index=tab_index,
                tab_handle=tab_handle,
                timeout_s=10 + wait_ms // 1000,
            )
            meta["status_calls"] += 1
            try:
                finished = json.loads(raw or "{}")
            except json.JSONDecodeError:
                finished = {}
            if not isinstance(finished, dict):
                finished = {}
            if awaits and not finished:
                awaits = False  # the call fell back to a transport that cannot await; poll instead
            state = str(finished.get("status") or "")
            if state == "done":
                break
            if state in {"error", "missing"}:
                message = str(finished.get("error") or f"DOM screenshot state became {state}.")
                if "trusted" in message.lower():
                    meta["reason_code"] = "TRUSTED_TYPES_BLOCKED"
                    message = (
                        "The page's Trusted Types policy blocked the DOM screenshot. "
                        "Use semantic observe or browser_find instead."
                    )
                return None, message, meta
            if not awaits:
                cancellable_sleep(_DOM_CAPTURE_STATUS_POLL_S)
        else:
            return None, f"DOM screenshot timed out after {timeout_s:.0f}s.", meta

        if isinstance(finished.get("meta"), dict):
            meta.update(finished["meta"])
        data_url = str(finished.get("data") or "")
        prefix = "data:image/jpeg;base64,"
        if not data_url.startswith(prefix):
            return None, "DOM screenshot did not return a JPEG data URL.", meta
        try:
            image_data = base64.b64decode(data_url[len(prefix):], validate=False)
        except Exception as exc:
            return None, f"Could not decode DOM screenshot: {exc}", meta
        if not image_data:
            return None, "DOM screenshot returned empty image data.", meta
        meta["bytes"] = len(image_data)
        return image_data, None, meta
    except HTTPException as exc:
        return None, str(exc.detail), meta
    except Exception as exc:
        return None, f"Could not capture DOM screenshot: {exc}", meta
    finally:
        try:
            _execute_js_unbounded(
                browser,
                f"try{{if(window[{state_key_js}]){{window[{state_key_js}].data=null;delete window[{state_key_js}];}}}}catch(e){{}}'cleaned'",
                window_index=window_index,
                tab_index=tab_index,
                tab_handle=tab_handle,
                timeout_s=10,
            )
        except Exception:
            pass


def _b64_return(expression: str) -> str:
    return (
        "(function(){"
        "function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}"
        f"return __mcpB64({expression});"
        "})()"
    )


# The page-side helper library (about 36 KB of function declarations) used to be
# sent with every browser script. It is now installed once per document under a
# versioned, per-process window property; later scripts bind its functions in a
# few hundred bytes. Builders still emit complete, standalone scripts: the helper
# sits between two markers that the executor swaps for the short binding when the
# document already has the library, and a missing library is reported with a
# sentinel so the complete script runs instead.
_BOOT_BEGIN = "/*__MAC_MCP_BOOT_BEGIN__*/"
_BOOT_END = "/*__MAC_MCP_BOOT_END__*/"
_AGENT_API_MISSING = "__MAC_MCP_AGENT_API_MISSING__"
# Base64 of a full perception-budget payload (64 KB of JSON) plus headroom.
_AGENT_PAYLOAD_MAX_CHARS = 96_000
_AGENT_API_NONCE = secrets.token_hex(4)
_AGENT_API_LOCK = threading.Lock()
_AGENT_API_CACHE: Dict[str, Any] = {}
_WARM_DOCUMENTS: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
_WARM_DOCUMENT_LIMIT = 256
_TRANSPORT = threading.local()


def _top_level_function_names(source: str) -> List[str]:
    names: List[str] = []
    depth = 0
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(source):
        ch = source[index]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and source.startswith("function ", index):
            match = re.match(r"function\s+([A-Za-z_$][\w$]*)", source[index:])
            if match:
                names.append(match.group(1))
        index += 1
    return names


def _agent_api() -> Dict[str, Any]:
    with _AGENT_API_LOCK:
        if not _AGENT_API_CACHE:
            source = _bootstrap_functions_source()
            version = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
            names = _top_level_function_names(source)
            key = f"__macMcpAgentApi_{version}_{_AGENT_API_NONCE}"
            exports = ",".join(f"{name}:{name}" for name in names)
            install = (
                "(function(){try{if(window['" + key + "'])return;"
                "var api={" + exports + ",__mcpVersion:'" + version + "'};"
                "try{Object.defineProperty(window,'" + key + "',{value:api,configurable:true});}"
                "catch(e){window['" + key + "']=api;}}catch(e){}})();"
            )
            binding = (
                "var __mcpApi=window['" + key + "'];"
                "if(!__mcpApi||__mcpApi.__mcpVersion!=='" + version + "')return '" + _AGENT_API_MISSING + "';"
                "var " + ",".join(f"{name}=__mcpApi.{name}" for name in names) + ";"
            )
            _AGENT_API_CACHE.update(
                full=_BOOT_BEGIN + "\n" + source + "\n" + install + "\n" + _BOOT_END,
                binding=_BOOT_BEGIN + binding + _BOOT_END,
                names=names,
                version=version,
            )
        return _AGENT_API_CACHE


def _browser_state_bootstrap() -> str:
    return str(_agent_api()["full"])


def _with_binding_only(js: str) -> Optional[str]:
    """Replace every marked helper region (a composed script can hold several)."""
    binding = str(_agent_api()["binding"])
    out: List[str] = []
    position = 0
    replaced = False
    while True:
        start = js.find(_BOOT_BEGIN, position)
        end = js.find(_BOOT_END, start + len(_BOOT_BEGIN)) if start >= 0 else -1
        if start < 0 or end < 0:
            break
        out.append(js[position:start])
        out.append(binding)
        position = end + len(_BOOT_END)
        replaced = True
    if not replaced:
        return None
    out.append(js[position:])
    return "".join(out)


def _document_key(browser: str, target: browser_tabs.TabTarget) -> Tuple[str, str, str]:
    # A child frame is a separate document with its own copy of the helper library.
    return (str(browser), str(target.native_id or target.tab_handle or ""), current_frame_scope() or "")


def _note_transport(sent: int, received: int, injected: bool) -> None:
    stats = getattr(_TRANSPORT, "stats", None)
    if stats is not None:
        stats["js_calls"] += 1
        stats["sent_bytes"] += sent
        stats["received_bytes"] += received
        stats["helper_injections"] += 1 if injected else 0


def _execute_js_for_target(browser: str, js: str, target: browser_tabs.TabTarget, timeout_s: int) -> str:
    """Run page JavaScript, sending the helper library only when the document lacks it."""
    light = _with_binding_only(js)
    key = _document_key(browser, target)
    if light is not None:
        # The overlay names the agent acting here (#124); a chat's scripts are unchanged.
        who = browser_activity.visual_who_prefix(key)
        if who:
            js, light = who + js, who + light
    if light is not None:
        with _AGENT_API_LOCK:
            warm = key in _WARM_DOCUMENTS
        if warm:
            out = _raw_execute_js_for_target(browser, light, target, timeout_s)
            if str(out or "").strip() != _AGENT_API_MISSING:
                _note_transport(len(light.encode("utf-8")), len(str(out or "")), False)
                return out
            # Reloaded or navigated: the document no longer has the library.
            _note_transport(len(light.encode("utf-8")), len(str(out or "")), False)
            with _AGENT_API_LOCK:
                _WARM_DOCUMENTS.pop(key, None)
    out = _raw_execute_js_for_target(browser, js, target, timeout_s)
    _note_transport(len(js.encode("utf-8")), len(str(out or "")), light is not None)
    if light is not None:
        with _AGENT_API_LOCK:
            _WARM_DOCUMENTS[key] = time.monotonic()
            _WARM_DOCUMENTS.move_to_end(key)
            while len(_WARM_DOCUMENTS) > _WARM_DOCUMENT_LIMIT:
                _WARM_DOCUMENTS.popitem(last=False)
    return out


@contextmanager
def _measure_transport() -> Iterator[Dict[str, int]]:
    """Count JavaScript bridge traffic for one browser operation on this thread."""
    previous = getattr(_TRANSPORT, "stats", None)
    stats = {"js_calls": 0, "sent_bytes": 0, "received_bytes": 0, "helper_injections": 0}
    _TRANSPORT.stats = stats
    try:
        yield stats
    finally:
        _TRANSPORT.stats = previous
        if previous is not None:
            for name, value in stats.items():
                previous[name] += value


_AGENT_HELPERS_JS = Path(__file__).with_name("browser_js") / "agent_helpers.js"


@functools.lru_cache(maxsize=None)
def _browser_js(name: str) -> str:
    """An authored page-script template from browser_js/; data goes in through its __PLACEHOLDERS__."""
    return (Path(__file__).with_name("browser_js") / name).read_text(encoding="utf-8")


_PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


def _fill_js(template: str, values: Dict[str, str]) -> str:
    """Fill a template's __PLACEHOLDERS__ in one pass.

    Inserted values are never scanned again, so data that happens to contain a placeholder
    name (an observation id like "bobs__ACTIONS__") stays inside its JSON literal.
    """
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(0), match.group(0)), template)


@functools.lru_cache(maxsize=1)
def _bootstrap_functions_source() -> str:
    """The page helper library (browser_js/agent_helpers.js), installed once per document."""
    return _AGENT_HELPERS_JS.read_text(encoding="utf-8")


def _observe_js(scope: str, max_elements: int) -> str:
    scope_js = json.dumps(scope)
    return _fill_js(_browser_js("observe.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__SCOPE__": scope_js,
        "__MAX_ELEMENTS__": str(max_elements),
    })


def _conditional_observe_js(
    previous_observation_id: str,
    scope: str,
    max_elements: int,
) -> str:
    previous = json.dumps(str(previous_observation_id or ""))
    wanted_scope = json.dumps(str(scope or "interactive"))
    wanted_max = max(1, min(int(max_elements), _MAX_OBSERVE_ELEMENTS))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),prev={previous};
var known=Object.prototype.hasOwnProperty.call(s.observations,prev);
var meta=known&&s.observationMeta?s.observationMeta[prev]:null;
var compatible=!!(meta&&String(meta.scope||'')==={wanted_scope}&&Number(meta.max_elements||0)==={wanted_max});
var previousRevision=known?Number(s.observations[prev]):null;
var currentRevision=Number(s.mutationRevision||0);
/* #393: an unchanged revision only proves an unchanged page while changes were being counted all along,
   from that observation until now; a scroll, a resize or another URL shows other elements as well. */
var view=meta&&meta.view,stale=null;
if(!s.observerTimer||!s.watchSince||!meta||!meta.at||s.watchSince>meta.at)stale='changes_not_watched';
else if(!view||view.sx!==scrollX||view.sy!==scrollY||view.w!==innerWidth||view.h!==innerHeight)stale='viewport_changed';
else if(meta.url&&meta.url!==String(location.href))stale='url_changed';
/* Still current: keep counting, so the next repeat can be answered from this observation as well. */
if(!stale&&known&&compatible)__mcpStartMutationWatch(s,5000);
return __mcpB64({{
  ok:true,
  known:known,
  compatible:compatible,
  /* A typed or chosen value moves no DOM node: the controls' values must match too. */
  not_modified:known&&compatible&&!stale&&previousRevision===currentRevision&&(!meta||!meta.values||meta.values===__mcpValuesFp()),
  stale_reason:stale,
  previous_observation_id:prev,
  dom_revision:currentRevision,
  url:String(location.href),
  title:String(document.title)
}});
}})()'''


def _observe_payload(
    settings: Settings, browser: str, scope: str, max_elements: int,
    window_index: int, tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    requested = max_elements
    attempt = max_elements
    remote_js_calls = 0
    while True:
        try:
            remote_js_calls += 1
            payload = _run_json_js(
                settings, browser, _observe_js(scope, attempt),
                window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
            )
            payload["requested_max_elements"] = requested
            payload["_remote_js_calls"] = remote_js_calls
            canvas = payload.get("canvas")
            if isinstance(canvas, dict) and canvas.get("canvas_heavy") and _norm_browser(browser) != "Google Chrome":
                # x,y clicks and hovers are Chrome-only; never point a Safari agent at them (#386).
                canvas["hint"] = ("Controls are drawn in a canvas, which Safari cannot click into in the background. "
                                  + _SAFARI_POINTER_RECOVERY)
            if attempt != requested:
                payload["payload_limited"] = True
                payload["effective_max_elements"] = attempt
            return payload
        except HTTPException as exc:
            if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE or attempt <= 20:
                raise
            attempt = max(20, attempt // 2)


def _capture_region(rect: Dict[str, Any], max_dimension: int = 1280) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        raw_x = int(round(float(rect["x"])))
        raw_y = int(round(float(rect["y"])))
        w = max(1, int(round(float(rect["w"]))))
        h = max(1, int(round(float(rect["h"]))))
        x = max(0, raw_x)
        y = max(0, raw_y)
        if raw_x < 0:
            w = max(1, w + raw_x)
        if raw_y < 0:
            h = max(1, h + raw_y)
    except Exception as exc:
        return None, f"Invalid screenshot rect: {exc}"
    fd, path = tempfile.mkstemp(prefix="mac-mcp-browser-", suffix=".jpg")
    os.close(fd)
    try:
        proc = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "jpg", "-R", f"{x},{y},{w},{h}", path],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None, (proc.stderr or "screencapture failed").strip()
        if max(w, h) > max_dimension:
            subprocess.run(
                ["/usr/bin/sips", "-Z", str(max_dimension), "-s", "formatOptions", "65", path],
                capture_output=True, text=True, timeout=10,
            )
        data = Path(path).read_bytes()
        if not data:
            return None, "screencapture returned an empty image"
        return data, None
    except Exception as exc:
        return None, f"Could not capture browser region: {exc}"
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _format_observation(payload: Dict[str, Any], image_data: Optional[bytes]) -> Any:
    template = _fill_template(payload.get("elements"))
    if template:
        # Tool output is what the model reads most closely: hand it the one-call form fill.
        payload = {**payload, "fill_template": template}
    if image_data:
        visual = payload.get("visual") or {}
        compact_elements: List[Dict[str, Any]] = []
        for element in payload.get("elements") or []:
            if not isinstance(element, dict):
                continue
            compact_element = {
                key: element.get(key)
                for key in (
                    "element_id", "tag", "role", "text", "aria_label", "placeholder",
                    "name", "title", "value", "href", "actionable", "ready", "readiness_reason", "enabled", "focused",
                    "checked", "input_type", "viewport_rect", "modal_scope", "association_text",
                    "associated_control", "associated_label", "association_ambiguous", "hit_target",
                )
                if element.get(key) is not None
            }
            compact_elements.append(compact_element)
        compact = {
            "ok": bool(payload.get("ok")),
            "observation_id": payload.get("observation_id"),
            "dom_revision": payload.get("dom_revision"),
            "url": payload.get("url"),
            "title": payload.get("title"),
            "scope": payload.get("scope"),
            "modal_scope": payload.get("modal_scope"),
            "element_count": payload.get("element_count"),
            "elements": compact_elements,
            "viewport": payload.get("viewport"),
            "scroll": payload.get("scroll"),
            "duration_ms": payload.get("duration_ms"),
            "state_mode": payload.get("state_mode"),
            "not_modified": payload.get("not_modified"),
            "previous_observation_id": payload.get("previous_observation_id"),
            "telemetry": payload.get("telemetry"),
            **({"fill_template": payload["fill_template"]} if payload.get("fill_template") else {}),
            "visual": {
                "mode": visual.get("mode"),
                "w": visual.get("output_width"),
                "h": visual.get("output_height"),
                "truncated": visual.get("truncated"),
                "background_safe": visual.get("background_safe"),
                "tab_activated": visual.get("tab_activated"),
            },
        }
        text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        return [text, Image(data=image_data, format="jpeg")]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _browser_observe_impl(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    scope: str = "interactive",
    max_elements: int = _DEFAULT_OBSERVE_ELEMENTS,
    visual: str = "none",
    element_id: Optional[str] = None,
    previous_observation_id: Optional[str] = None,
) -> Any:
    """Compact DOM observation with stable element IDs and optional background-safe page image."""
    b = _norm_browser(browser)
    _ensure_visual_companion(settings, b, window_index, tab_index, tab_handle)
    with _tab_lease(b, tab_handle, window_index, tab_index, allow_rebind=True) as target:
        if target.lease_rebound:
            _execute_js_for_target(
                target.browser,
                "try{delete window.__macMcpBrowserAgent;}catch(e){window.__macMcpBrowserAgent=undefined;} 'OK';",
                target,
                timeout_s=10,
            )
        observed = _browser_observe_locked(
            settings=settings,
            browser=target.browser,
            window_index=target.window_index,
            tab_index=target.tab_index,
            tab_handle=target.tab_handle,
            scope=scope,
            max_elements=max_elements,
            visual=visual,
            element_id=element_id,
            previous_observation_id=previous_observation_id,
        )
        lease_meta = {"lease_generation": target.lease_generation}
        if target.lease_rebound:
            lease_meta.update({"lease_rebound": True, "previous_origin": target.previous_origin})
        if isinstance(observed, str):
            try:
                payload = json.loads(observed)
            except json.JSONDecodeError:
                return observed
            if isinstance(payload, dict):
                payload.update(lease_meta)
                if isinstance(payload.get("telemetry"), dict):
                    refresh_perception_size(payload)
                return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if isinstance(observed, list) and observed and isinstance(observed[0], str):
            try:
                payload = json.loads(observed[0])
            except json.JSONDecodeError:
                return observed
            if isinstance(payload, dict):
                payload.update(lease_meta)
                if isinstance(payload.get("telemetry"), dict):
                    refresh_perception_size(payload)
                observed = list(observed)
                observed[0] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return observed


def _browser_observe_locked(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    scope: str = "interactive",
    max_elements: int = _DEFAULT_OBSERVE_ELEMENTS,
    visual: str = "none",
    element_id: Optional[str] = None,
    previous_observation_id: Optional[str] = None,
) -> Any:
    _norm_browser(browser)
    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    scope = str(scope or "interactive").lower().strip()
    # Agents keep asking for the whole page; that is what content returns.
    scope = {"page": "content", "full": "content", "all": "content"}.get(scope, scope)
    if scope not in {"interactive", "visible", "content", "leaf"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be interactive, visible, content, or leaf.")
    visual = str(visual or "none").lower().strip()
    if visual not in _VISUAL_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visual must be none, viewport, element, or full_page.")
    max_elements = max(1, min(int(max_elements), _MAX_OBSERVE_ELEMENTS))
    started = time.perf_counter()
    conditional_js_calls = 0
    conditional_stale_reason: Optional[str] = None
    if (
        previous_observation_id
        and visual == "none"
        and _browser_observation_owned_by_current(previous_observation_id)
    ):
        conditional_js_calls = 1
        conditional = _run_json_js(
            settings,
            browser,
            _conditional_observe_js(
                previous_observation_id,
                scope,
                max_elements,
            ),
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
        )
        conditional_stale_reason = conditional.get("stale_reason") if conditional.get("known") else None
        if bool(conditional.get("known")) and bool(conditional.get("not_modified")):
            out: Dict[str, Any] = {
                "ok": True,
                "state_mode": "not_modified",
                "not_modified": True,
                "observation_id": str(previous_observation_id),
                "previous_observation_id": str(previous_observation_id),
                "dom_revision": conditional.get("dom_revision"),
                "url": conditional.get("url"),
                "title": conditional.get("title"),
                "element_count": 0,
                "elements": [],
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "cache_validation": "dom_mutation_revision",
                "telemetry": {
                    "remote_js_calls": 1,
                    "payload_mode": "not_modified",
                },
            }
            finalize_perception_telemetry(
                out,
                stage="conditional",
                state_mode="not_modified",
                node_count=0,
                duration_ms=int(out["duration_ms"]),
                context_budget_bytes=64_000,
                measure=False,
            )
            metrics = out["telemetry"]
            sample_bytes = json_bytes(out)
            metrics["benchmark"] = record_computer_use_sample(
                "browser_observe",
                duration_ms=int(metrics.get("duration_ms") or 0),
                payload_bytes=sample_bytes,
                payload_tokens_estimate=estimate_payload_tokens(sample_bytes),
                remote_js_calls=1,
                ax_traversals=0,
                visual_bytes=0,
                node_count=0,
                state_mode="not_modified",
            )
            refresh_perception_size(out)
            return _format_observation(out, None)

    payload = _observe_payload(
        settings,
        browser,
        scope,
        max_elements,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )
    payload["duration_ms"] = int((time.perf_counter() - started) * 1000)
    _remember_browser_observation(payload.get("observation_id"))
    _remember_target_identities(browser, tab_handle, payload.get("elements"))

    image_data: Optional[bytes] = None
    if visual != "none":
        target_rect: Optional[Dict[str, Any]] = None
        if visual == "element":
            if not element_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "element_id is required when visual='element'.")
            match = next((e for e in payload.get("elements", []) if e.get("element_id") == element_id), None)
            if not match:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"element_id not found in this observation: {element_id}")
            target_rect = match.get("viewport_rect") or None
        elif visual == "viewport":
            target_rect = {
                "x": 0,
                "y": 0,
                "w": int((payload.get("viewport") or {}).get("w") or 1),
                "h": int((payload.get("viewport") or {}).get("h") or 1),
            }

        image_data, image_error, capture_meta = _capture_dom_visual(
            browser=browser,
            mode=visual,
            element_id=element_id,
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
        )
        payload["visual"] = {
            "mode": visual,
            "ok": image_data is not None,
            "rect": target_rect,
            "mime_type": "image/jpeg" if image_data else None,
            "capture_method": capture_meta.get("capture_method"),
            "background_safe": bool(capture_meta.get("background_safe", True)),
            "tab_activated": bool(capture_meta.get("tab_activated", False)),
            "disk_write": bool(capture_meta.get("disk_write", False)),
        }
        for key in (
            "source_width", "source_height", "actual_height", "output_width", "output_height",
            "scale", "truncated", "elapsed_ms", "bytes", "reason_code", "readiness_attempts",
            "readiness_duration_ms", "raw_width", "raw_height",
        ):
            if key in capture_meta:
                payload["visual"][key] = capture_meta[key]
        if image_error:
            payload["visual"]["error"] = image_error

    payload["state_mode"] = "full"
    payload["not_modified"] = False
    if previous_observation_id:
        payload["previous_observation_id"] = str(previous_observation_id)
        if conditional_stale_reason:
            payload["refreshed_because"] = conditional_stale_reason
    remote_js_calls = int(payload.pop("_remote_js_calls", 1) or 1) + conditional_js_calls
    visual_meta = payload.get("visual") if isinstance(payload.get("visual"), dict) else {}
    visual_bytes = int((visual_meta or {}).get("bytes") or (len(image_data) if image_data else 0))
    context_truncated = bool(payload.get("payload_limited"))
    expand_hint = None
    if context_truncated:
        expand_hint = (
            "Controls not listed can still be targeted: pass query/role/text_match straight to browser_act, which finds "
            "them itself (no separate search); or observe a smaller scope/max_elements; "
            "use visual='element' only for the specific element that needs visual grounding."
        )
    finalize_perception_telemetry(
        payload,
        stage="targeted_visual" if visual != "none" else "semantic",
        state_mode="full",
        node_count=int(payload.get("element_count") or len(payload.get("elements") or [])),
        duration_ms=int(payload.get("duration_ms") or 0),
        visual_bytes=visual_bytes,
        visual_width=(visual_meta or {}).get("output_width"),
        visual_height=(visual_meta or {}).get("output_height"),
        ocr_used=False,
        context_budget_bytes=64_000,
        context_truncated=context_truncated,
        expand_hint=expand_hint,
        measure=False,
    )
    metrics = payload["telemetry"]
    metrics["remote_js_calls"] = remote_js_calls
    if visual_meta.get("elapsed_ms") is not None:
        metrics["capture_duration_ms"] = int(visual_meta.get("elapsed_ms") or 0)
    sample_bytes = json_bytes(payload)
    metrics["benchmark"] = record_computer_use_sample(
        "browser_observe",
        duration_ms=int(metrics.get("duration_ms") or 0),
        payload_bytes=sample_bytes,
        payload_tokens_estimate=estimate_payload_tokens(sample_bytes),
        remote_js_calls=remote_js_calls,
        ax_traversals=0,
        visual_bytes=visual_bytes,
        node_count=int(metrics.get("node_count") or 0),
        state_mode="full",
    )
    refresh_perception_size(payload)
    return _format_observation(payload, image_data)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9çğıöşü]+", " ", text).strip()


def _match_level(actual: Any, wanted: Any) -> int:
    a = _normalize_text(actual)
    w = _normalize_text(wanted)
    if not a or not w:
        return 0
    if a == w:
        return 5
    if a.startswith(w + " ") or a.startswith(w + "-"):
        return 4
    a_tokens = a.split()
    w_tokens = w.split()
    if w_tokens and all(token in a_tokens for token in w_tokens):
        return 3
    if len(w) >= 4 and w in a:
        return 2
    return 0


def _score_candidate(element: Dict[str, Any], query: str, role: Optional[str], text: Optional[str]) -> float:
    element_role = _normalize_text(element.get("role"))
    if role and element_role != _normalize_text(role):
        return 0.0

    primary = [
        element.get("text"), element.get("aria_label"), element.get("placeholder"),
        element.get("name"), element.get("title"), element.get("value"), element.get("context"),
        element.get("association_text"),
    ]
    combined_primary = " ".join(str(value or "") for value in primary if value)
    if combined_primary:
        primary.append(combined_primary)
    if text:
        text_levels = [_match_level(value, text) for value in primary]
        best_text = max(text_levels or [0])
        if best_text == 0:
            return 0.0
    else:
        best_text = 0

    option_values: List[str] = []
    for item in (element.get("options") or []):
        if isinstance(item, dict):
            option_values.extend([str(item.get("text") or ""), str(item.get("value") or "")])

    q_raw = _normalize_text(query)
    q_tokens = [token for token in q_raw.split() if token not in _GENERIC_QUERY_WORDS]
    q = " ".join(q_tokens) if q_tokens else q_raw
    query_levels = [_match_level(value, q) for value in primary + option_values] if q else [0]
    best_query = max(query_levels or [0])

    combined_tokens = set(_normalize_text(" ".join(str(v or "") for v in primary + option_values)).split())
    token_ratio = (sum(1 for token in q_tokens if token in combined_tokens) / len(q_tokens)) if q_tokens else 0.0

    level_score = {0: 0.0, 2: 0.48, 3: 0.68, 4: 0.84, 5: 0.98}
    role_only = bool(role) and not q and not text
    score = 0.70 if role_only else max(level_score.get(best_query, 0.0), level_score.get(best_text, 0.0))
    if token_ratio == 1.0 and q_tokens:
        score = max(score, 0.86)
    elif token_ratio >= 0.5:
        score = max(score, 0.64)
    if text:
        score = max(score, {2: 0.62, 3: 0.78, 4: 0.90, 5: 1.0}.get(best_text, 0.0))
    if role:
        score += 0.04
    tag = str(element.get("tag") or "").lower()
    if element.get("actionable"):
        score += 0.03
    if tag in {"a", "button", "input", "select", "summary"}:
        score += 0.03
    elif tag in {"dt", "label"}:
        score += 0.02
    elif tag in {"html", "body", "main", "section", "div", "dl", "ul"}:
        score -= 0.12
    return max(0.0, min(1.0, score))


_REDUNDANT_CANDIDATE_PENALTY = 0.15


def _demote_redundant_candidates(
    scored: List[Tuple[float, Dict[str, Any]]],
) -> List[Tuple[float, Dict[str, Any]]]:
    """Separate the element that carries the match from candidates that only echo it.

    A container whose aggregated text merely includes a matching descendant, the
    inner text node of a control that already matches, and a label whose control
    also matches all describe one target. Within the ambiguity margin, keep the
    innermost match unless only its container is actionable, in which case keep
    the container so the click lands on the control. Distinct targets keep their
    scores, and scores are compared before any demotion.
    """
    raw = {str(element.get("element_id")): score for score, element in scored if element.get("element_id")}
    by_id = {str(element.get("element_id")): element for _, element in scored if element.get("element_id")}
    demoted: set[str] = set()

    def linked(label: Dict[str, Any], control: Dict[str, Any]) -> bool:
        return (
            str((label.get("associated_control") or {}).get("element_id") or "") == str(control.get("element_id"))
            or str((control.get("associated_label") or {}).get("element_id") or "") == str(label.get("element_id"))
        )

    for container_id, container in by_id.items():
        if str(container.get("tag") or "").lower() == "label" and any(
            linked(container, other) for other_id, other in by_id.items() if other_id != container_id
        ):
            # The control is the target; its label is another way to reach it.
            demoted.add(container_id)
            continue
        for nested_id in container.get("nested_match_ids") or []:
            nested = by_id.get(str(nested_id))
            if nested is None or raw[str(nested_id)] < raw[container_id] - decision_engine.AMBIGUITY_MARGIN:
                continue
            if container.get("actionable") and not nested.get("actionable"):
                demoted.add(str(nested_id))
            else:
                demoted.add(container_id)
    return [
        (max(0.0, score - _REDUNDANT_CANDIDATE_PENALTY) if str(element.get("element_id")) in demoted else score, element)
        for score, element in scored
    ]


_WITHIN_DEFAULT_LEVELS = 6
_WITHIN_MAX_LEVELS = 30
_WITHIN_SCAN_LIMIT = 3000


def _within_scope_js(within: Optional[str], within_element_id: Optional[str], within_levels: int) -> str:
    """JS that scopes collected candidates to the item around an anchor.

    The anchor is the element holding the `within` text (or within_element_id).
    A candidate stays in scope when its nearest common ancestor with the anchor
    is at most `levels` steps above the anchor, so the Reply link of a comment
    matches while the Reply links of parent comments and the page-level comment
    box do not. Candidates are ordered nearest first. A `within` text found in
    several places is reported as ambiguous instead of guessed.
    """
    within_js = json.dumps(str(within or ""))
    within_id_js = json.dumps(str(within_element_id or ""))
    levels = max(1, min(int(within_levels or _WITHIN_DEFAULT_LEVELS), _WITHIN_MAX_LEVELS))
    return f'''
var withinRaw={within_js}, withinId={within_id_js}, withinLevels={levels};
if(withinRaw||withinId){{
  function cparent(n){{return n.parentNode||(n.host||null);}}
  function chain(n){{var a=[];while(n){{a.push(n);n=cparent(n);}}return a;}}
  var anchors=[];
  if(withinId){{var ae=__mcpRecoverElement(withinId,s);if(ae)anchors.push(ae);}}
  else{{
    var w=norm(withinRaw);
    if(w){{
      var tw=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),tn;
      while((tn=tw.nextNode())){{var pe=tn.parentElement;if(pe&&rendered(pe)&&norm(tn.data).indexOf(w)>=0&&anchors.indexOf(pe)<0)anchors.push(pe);}}
      if(!anchors.length){{
        var every=__mcpQueryAll('*');
        for(var wi=0;wi<every.length;wi++){{var we=every[wi];if(!rendered(we))continue;var lab=norm([we.getAttribute('aria-label'),we.getAttribute('title'),(we.textContent||'').length<3000?we.textContent:''].join(' '));if(lab.indexOf(w)>=0)anchors.push(we);}}
      }}
      // Keep the innermost matches: an ancestor of another match is the same anchor.
      anchors=anchors.filter(function(a){{return !anchors.some(function(b){{return b!==a&&contains(a,b);}});}});
    }}
  }}
  withinInfo={{levels:withinLevels,anchor_count:anchors.length,anchors:anchors.slice(0,5).map(function(a){{return __mcpText(a).slice(0,100);}})}};
  if(!anchors.length){{withinInfo.status='anchor_not_found';out=[];els=[];}}
  else if(anchors.length>1){{withinInfo.status='anchor_ambiguous';out=[];els=[];}}
  else{{
    var anchorChain=chain(anchors[0]);withinInfo.status='ok';withinInfo.anchor_element_id=__mcpId(anchors[0],s);
    var scoped=[];
    for(var si=0;si<els.length;si++){{
      var cc=chain(els[si]),up=-1,down=-1;
      for(var ai=0;ai<anchorChain.length;ai++){{var k=cc.indexOf(anchorChain[ai]);if(k>=0){{up=ai;down=k;break;}}}}
      if(up<0||up>withinLevels)continue;
      out[si].within_up=up;out[si].within_down=down;scoped.push([up,down,out[si],els[si]]);
    }}
    scoped.sort(function(a,b){{return a[0]-b[0]||a[1]-b[1];}});
    out=scoped.map(function(r){{return r[2];}});els=scoped.map(function(r){{return r[3];}});
  }}
}}
'''


def _find_candidates_js(
    query: str, role: Optional[str], text: Optional[str], max_candidates: int = 80, actionable_only: bool = False,
    within: Optional[str] = None, within_element_id: Optional[str] = None, within_levels: int = _WITHIN_DEFAULT_LEVELS,
) -> str:
    q_raw = _normalize_text(query)
    q_tokens = [token for token in q_raw.split() if token not in _GENERIC_QUERY_WORDS]
    q = " ".join(q_tokens) if q_tokens else q_raw
    q_js = json.dumps(q)
    role_js = json.dumps(_normalize_text(role) if role else "")
    text_js = json.dumps(_normalize_text(text) if text else "")
    actionable_js = "true" if actionable_only else "false"
    scoped = bool(within or within_element_id)
    # A scoped search must see every match before choosing the nearest one,
    # so collection is capped by the scan limit and trimmed after scoping.
    collect_limit = _WITHIN_SCAN_LIMIT if scoped else max_candidates
    within_block = _within_scope_js(within, within_element_id, within_levels) if scoped else ""
    return _fill_js(_browser_js("find.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__QUERY__": q_js,
        "__ROLE__": role_js,
        "__TEXT__": text_js,
        "__ACTIONABLE_ONLY__": actionable_js,
        "__COLLECT_LIMIT__": str(collect_limit),
        "__WITHIN_BLOCK__": within_block,
        "__MAX_CANDIDATES__": str(max_candidates),
    })


def _browser_find_impl(
    settings: Settings,
    browser: str,
    query: str,
    role: Optional[str] = None,
    text: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    max_results: int = 5,
    actionable_only: bool = False,
    wait_timeout_s: float = 0.0,
    within: Optional[str] = None,
    within_element_id: Optional[str] = None,
    within_levels: Optional[int] = None,
) -> Dict[str, Any]:
    """Find a rendered DOM target with exact-first ranking and hard role/text constraints.

    within/within_element_id limit matches to the item around an anchor (for
    example one comment) and rank them nearest first.
    """
    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    _ensure_visual_companion(settings, browser, window_index, tab_index, tab_handle)
    if not str(query or "").strip() and not text and not role:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query, text, or role is required.")
    started = time.perf_counter()
    wait_meta: Optional[Dict[str, Any]] = None
    wait_timeout_s = max(0.0, min(float(wait_timeout_s or 0.0), 60.0))
    if wait_timeout_s > 0:
        wait_meta = _wait_action(
            settings,
            browser,
            {
                "for": "semantic",
                "query": str(query or ""),
                "text": str(text or ""),
                "role": str(role or ""),
                "actionable_only": bool(actionable_only),
                "timeout_s": wait_timeout_s,
            },
            window_index,
            tab_index,
            initial_url="",
            tab_handle=tab_handle,
        )
    max_results = max(1, min(int(max_results), 10))
    candidate_limit = 60
    payload_limited = False
    while True:
        try:
            payload = _run_json_js(
                settings, browser, _find_candidates_js(
                    str(query or ""), role, text, candidate_limit, actionable_only=actionable_only,
                    within=within, within_element_id=within_element_id,
                    within_levels=int(within_levels or _WITHIN_DEFAULT_LEVELS),
                ),
                window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
            )
            break
        except HTTPException as exc:
            if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE or candidate_limit <= 10:
                raise
            candidate_limit = max(10, candidate_limit // 2)
            payload_limited = True
    scoped = bool(within or within_element_id)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for element in payload.get("elements", []):
        score = _score_candidate(element, str(query or ""), role, text)
        if element.get("offscreen") and not scoped:
            # Reachable by scrolling, but an on-screen twin wins. Inside one item locality
            # decides instead: the Reply of an off-screen comment is still that comment's Reply.
            score *= 0.9
        if score >= 0.30:
            scored.append((score, element))
    scored = _demote_redundant_candidates(scored)
    def control_priority(element: Dict[str, Any]) -> int:
        tag = str(element.get("tag") or "").lower()
        role_name = str(element.get("role") or "").lower()
        if tag in {"a", "button", "input", "select", "summary"} or role_name in {"button", "link", "combobox", "option", "menuitem"}:
            return 4
        if tag in {"dt", "label"}:
            return 3
        if element.get("actionable"):
            return 2
        return 1

    if scoped:
        # Locality decides among equally good text matches: the nearest common
        # ancestor with the anchor first, then the shortest path below it.
        scored.sort(
            key=lambda item: (
                round(item[0], 1), -int(item[1].get("within_up") or 0), -int(item[1].get("within_down") or 0),
                control_priority(item[1]), -len(str(item[1].get("text") or "")),
            ),
            reverse=True,
        )
    else:
        scored.sort(
            key=lambda item: (item[0], control_priority(item[1]), -len(str(item[1].get("text") or ""))),
            reverse=True,
        )
    matches = []
    for score, element in scored[:max_results]:
        matches.append({
            "element_id": element.get("element_id"),
            "confidence": round(score, 3),
            "tag": element.get("tag"), "role": element.get("role"),
            "text": element.get("text"), "aria_label": element.get("aria_label"),
            "placeholder": element.get("placeholder"), "name": element.get("name"),
            "title": element.get("title"), "value": element.get("value"), "context": element.get("context"),
            "href": element.get("href"), "input_type": element.get("input_type"), "checked": element.get("checked"),
            "viewport_rect": element.get("viewport_rect"),
            "screen_rect": element.get("screen_rect"), "actionable": element.get("actionable"),
            "ready": element.get("ready"), "readiness_reason": element.get("readiness_reason"),
            "modal_scope": element.get("modal_scope"), "association_text": element.get("association_text"),
            "associated_control": element.get("associated_control"), "associated_label": element.get("associated_label"),
            "association_ambiguous": element.get("association_ambiguous"), "hit_target": element.get("hit_target"),
            **({"offscreen": True} if element.get("offscreen") else {}),
            **({"suggests": True} if element.get("suggests") else {}),
            **({"within_up": element.get("within_up"), "within_down": element.get("within_down")} if scoped else {}),
        })
    _remember_target_identities(browser, tab_handle, matches)
    return {
        "ok": True,
        "observation_id": payload.get("observation_id"),
        "dom_revision": payload.get("dom_revision"),
        "url": payload.get("url"),
        "title": payload.get("title"),
        "query": query,
        "search_scope": "targeted_scan",
        "modal_scope": payload.get("modal_scope"),
        "actionable_only": actionable_only,
        "candidate_limit": candidate_limit,
        "payload_limited": payload_limited,
        "wait": {
            "requested": bool(wait_timeout_s > 0),
            "matched": bool((wait_meta or {}).get("matched")) if wait_meta is not None else None,
            "strategy": (wait_meta or {}).get("wait_strategy") if wait_meta is not None else None,
            "remote_js_calls": int((wait_meta or {}).get("_js_calls") or 0) if wait_meta is not None else 0,
            "event_count": int(((wait_meta or {}).get("telemetry") or {}).get("event_count") or 0) if wait_meta is not None else 0,
            "fallback_polls": int(((wait_meta or {}).get("telemetry") or {}).get("fallback_polls") or 0) if wait_meta is not None else 0,
            "benchmark": ((wait_meta or {}).get("telemetry") or {}).get("benchmark") if wait_meta is not None else None,
        },
        **({"within": payload.get("within")} if scoped else {}),
        # Tool output is what the model reads most closely; point at the shortcut.
        "to_act": "Pass the same query/role/within to browser_act directly; it resolves the target, no element_id needed.",
        "best_match": matches[0] if matches else None,
        "matches": matches,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _batch_js(actions: List[Dict[str, Any]], observation_id: Optional[str]) -> str:
    actions_json = json.dumps(actions, ensure_ascii=False)
    obs_json = json.dumps(observation_id)
    return _fill_js(_browser_js("batch.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__QUIET_MS__": str(_DOM_EFFECT_QUIET_MS),
        "__OBS__": obs_json,
        "__ACTIONS__": actions_json,
    })


# Containers a custom select or menu draws its options in (#389): options are looked for only inside the
# list the control opened, never among the page's other links and buttons.
_POPUP_SELECTOR = (
    '[role="listbox"],[role="menu"],[role="tree"],[role="grid"],[role="dialog"],[class*="dropdown"],'
    '[class*="listbox"],[class*="option"],[class*="suggest"],[class*="menu"],[class*="popover"],[class*="popup"],'
    '[class*="results"],[class*="autocomplete"],[class*="select"],[class*="choices"],[class*="picker"],'
    '[class*="combobox"],[class*="list"]'
)


def _select_prepare_js(element_id: str, observation_id: Optional[str], option: Any) -> str:
    eid = json.dumps(str(element_id or ""))
    obs = json.dumps(observation_id)
    wanted = json.dumps(str(option if option is not None else ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
function norm(v){{return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/\\s+/g,' ').trim();}}
var s=__mcpState(), expected={obs}, eid={eid}, wanted=norm({wanted});
__mcpStartMutationWatch(s,3000);
if(expected && !(expected in s.observations)) return __mcpB64({{ok:false,error:'stale_observation',observe_again:true}});
var fpWas=s.targetFp?s.targetFp[eid]:null;
if(fpWas&&fpWas.route!==__mcpRoute()) return __mcpB64({{ok:false,error:'stale_target',reason_code:'ROUTE_CHANGED',observe_again:true,no_side_effect:true,element_id:eid}});
var el=s.elements[eid];
if(!el||!el.isConnected) return __mcpB64({{ok:false,error:'stale_element',observe_again:true,element_id:eid}});
if(fpWas){{var fpNow=__mcpTargetFp(el);if(fpNow.role!==fpWas.role||fpNow.tag!==fpWas.tag||fpNow.name!==fpWas.name)return __mcpB64({{ok:false,error:'stale_target',reason_code:'TARGET_CHANGED',observe_again:true,no_side_effect:true,element_id:eid}});}}
__mcpVisual('Selecting',el,'',2200);
if((el.tagName||'').toLowerCase()==='select'){{
  var opts=Array.from(el.options||[]);
  var chosen=opts.find(function(o){{return norm(o.value)===wanted||norm(o.text)===wanted;}}) ||
             opts.find(function(o){{var t=norm(o.text);return t.indexOf(wanted+' ')===0;}}) ||
             opts.find(function(o){{return norm(o.text).split(' ').indexOf(wanted)>=0;}});
  if(!chosen) return __mcpB64({{ok:false,error:'option_not_found',native:true,element_id:eid}});
  el.value=chosen.value;
  try{{el.dispatchEvent(new Event('input',{{bubbles:true}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));}}catch(e){{}}
  return __mcpB64({{ok:true,native:true,selected:String(chosen.text||chosen.value),value:String(chosen.value),element_id:eid}});
}}
__mcpScrollIntoView(el);
/* What the control showed before the choice, to tell a choice the page kept from one it undid (#391). */
s.selectBefore=s.selectBefore||{{}};s.selectBefore[eid]={{text:__mcpText(el),value:String(el.value==null?'':el.value)}};
/* The lists already shown before the press: one that appears after it is the list this control opened. */
s.popupsBefore=s.popupsBefore||{{}};
s.popupsBefore[eid]=__mcpQueryAll({json.dumps(_POPUP_SELECTOR)}).filter(function(p){{return __mcpVisible(p);}});
__mcpActivate(el);
return __mcpB64({{ok:true,native:false,needs_option_wait:true,element_id:eid,revision:s.mutationRevision}});
}})()'''


def _select_option_js(element_id: str, option: Any) -> str:
    eid = json.dumps(str(element_id or ""))
    wanted = json.dumps(str(option if option is not None else ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
function norm(v){{return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/\\s+/g,' ').trim();}}
function rendered(el){{
  if(!el||el.nodeType!==1) return false;
  var st=getComputedStyle(el); if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0) return false;
  var r=el.getBoundingClientRect(); return r.width>0&&r.height>0;
}}
var s=__mcpState(), origin=s.elements[{eid}], wanted=norm({wanted});
__mcpStartMutationWatch(s,3000);
var originRect=origin&&origin.getBoundingClientRect?origin.getBoundingClientRect():{{left:0,top:0,width:0,height:0}};
var selectors='[role="option"],[role="menuitem"],option,li,[class*="option"],[class*="suggest"],[class*="dropdown"] a,[class*="menu"] a,button,a';
function holdsOrigin(el){{return !!origin&&(el===origin||el.contains(origin));}}
function outermost(list){{return list.filter(function(el){{return !list.some(function(o){{return o!==el&&o.contains(el);}});}});}}
function gap(el){{var r=el.getBoundingClientRect();return Math.abs((r.left+r.width/2)-(originRect.left+originRect.width/2))+Math.abs((r.top+r.height/2)-(originRect.top+originRect.height/2));}}
/* #389: the list this control opened. First what it names (aria-controls/aria-owns, also on its inner box or
   its wrapper), else a list shown since it was pressed, else an open list nearest to it; never the page at large. */
var roots=[],how='';
var named=origin?[origin]:[];
if(origin){{try{{named=named.concat(Array.from(origin.querySelectorAll('[aria-controls],[aria-owns]')));}}catch(e){{}}
  for(var up=origin.parentElement,k=0;up&&k<3;up=up.parentElement,k++){{if(up.hasAttribute('aria-controls')||up.hasAttribute('aria-owns'))named.push(up);}}}}
named.forEach(function(n){{['aria-controls','aria-owns'].forEach(function(attr){{String(n.getAttribute(attr)||'').split(/\\s+/).forEach(function(id){{
  var r=id?document.getElementById(id):null;if(r&&rendered(r)&&roots.indexOf(r)<0)roots.push(r);}});}});}});
if(roots.length)how='named';
var lists=outermost(__mcpQueryAll({json.dumps(_POPUP_SELECTOR)}).filter(function(el){{return rendered(el)&&!holdsOrigin(el);}}));
if(!roots.length){{
  var before=(s.popupsBefore&&s.popupsBefore[{eid}])||[];
  roots=lists.filter(function(el){{return before.indexOf(el)<0&&!before.some(function(b){{return b.contains(el);}});}});
  if(roots.length)how='opened';
}}
if(!roots.length){{
  roots=lists.filter(function(el){{var o=el.closest('[aria-expanded="true"],.open,.show,.active,.is-open,.is-active,[data-state="open"]');
    var role=String(el.getAttribute('role')||'');return !!o||role==='listbox'||role==='menu';}});
  if(roots.length)how='open_nearby';
}}
roots.sort(function(a,b){{return gap(a)-gap(b);}});
var all=[];
/* Inside the list any shown element can be an option (plain divs too); a very large container keeps to option-like ones. */
roots.forEach(function(root){{var pool=root.querySelectorAll('*');if(pool.length>2000)pool=root.querySelectorAll(selectors);
  Array.from(pool).filter(rendered).forEach(function(el){{if(all.indexOf(el)<0&&!holdsOrigin(el))all.push(el);}});}});
function rootOf(el){{for(var i=0;i<roots.length;i++)if(roots[i].contains(el))return i;return -1;}}
function label(el){{return norm(__mcpText(el)||el.getAttribute('aria-label')||el.getAttribute('title')||'');}}
function clickPriority(el){{
  var tag=(el.tagName||'').toLowerCase(), role=(el.getAttribute('role')||'').toLowerCase();
  if(tag==='a'||tag==='button'||tag==='option'||role==='option'||role==='menuitem') return 5;
  if(typeof el.onclick==='function'||el.hasAttribute('onclick')) return 4;
  if(tag==='li' && el.querySelector('a,button,[role="option"],[role="menuitem"]')) return 0;
  return 1;
}}
function openBoost(el){{return el.closest('.active,.open,.show,[aria-expanded="true"],.address-pane.active,.select2-container--open,.dropdown-menu')?3:0;}}
function distance(el){{var r=el.getBoundingClientRect();return Math.abs((r.left+r.width/2)-(originRect.left+originRect.width/2))+Math.abs((r.top+r.height/2)-(originRect.top+originRect.height/2));}}
function best(arr){{return arr.sort(function(a,b){{var d=(clickPriority(b)+openBoost(b))-(clickPriority(a)+openBoost(a));if(d)return d;var da=distance(a),db=distance(b);if(da!==db)return da-db;return label(a).length-label(b).length;}})[0]||null;}}
if(!roots.length) return __mcpB64({{ok:true,found:false,candidate_count:0,list_found:false}});
/* The nearest list holding the option wins; inside it, an option and the link wrapped in it are one choice,
   and two separate options with the same name are a tie nothing is clicked for. */
function inNearest(arr){{if(!arr.length)return arr;var first=Math.min.apply(null,arr.map(rootOf));return arr.filter(function(el){{return rootOf(el)===first;}});}}
function distinct(arr){{return arr.filter(function(el){{return !arr.some(function(o){{return o!==el&&(el.contains(o)||o.contains(el))&&!el.contains(o);}});}});}}
var exact=inNearest(all.filter(function(el){{return label(el)===wanted;}}));
var prefix=inNearest(all.filter(function(el){{var t=label(el);return t.indexOf(wanted+' ')===0||t.indexOf(wanted+' (')===0;}}));
if(distinct(exact).length>1) return __mcpB64({{ok:true,found:false,ambiguous:true,matches:distinct(exact).length,list:how}});
var chosen=(best(exact)||best(prefix)||null);
if(!chosen) return __mcpB64({{ok:true,found:false,candidate_count:all.length,list:how}});
var txt=__mcpText(chosen);
__mcpScrollIntoView(chosen);
__mcpActivate(chosen);
return __mcpB64({{ok:true,found:true,selected:txt,tag:(chosen.tagName||'').toLowerCase(),role:chosen.getAttribute('role')||'',list:how}});
}})()'''


def _select_kept_js(element_id: str, chosen: Any, native_value: Optional[str] = None) -> str:
    """Whether the page kept a choice (#391): a native select still holds the value; a control that shows
    its choice (a button or combobox box) shows it, or shows what it did before. kept=null: cannot tell."""
    eid = json.dumps(str(element_id or ""))
    wanted = json.dumps(str(chosen if chosen is not None else ""))
    value = json.dumps(native_value)
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=s.elements[{eid}],native={value};
if(!el||!el.isConnected)return __mcpB64({{ok:true,kept:null}});
if(native!==null)return __mcpB64({{ok:true,kept:String(el.value)===native,now:String(el.value)}});
var tag=String(el.tagName||'').toLowerCase();
if(tag==='input'||tag==='textarea'||el.isContentEditable)return __mcpB64({{ok:true,kept:null}});
var now=__mcpNorm(__mcpText(el)),want=__mcpNorm({wanted}),was=(s.selectBefore||{{}})[{eid}];
if(want&&now.indexOf(want)>=0)return __mcpB64({{ok:true,kept:true}});
if(was&&__mcpNorm(was.text)===now&&now!==want)return __mcpB64({{ok:true,kept:false,now:__mcpText(el).slice(0,120)}});
return __mcpB64({{ok:true,kept:null}});
}})()'''


def _select_widget_js(element_id: str) -> str:
    """A native select that a script hid (Select2, Choices.js, Tom Select) is chosen in through the box
    drawn in its place; the script copies that choice back to the select. Returns the box's element id,
    or nothing when the select is shown itself."""
    eid = json.dumps(str(element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),sel=s.elements[{eid}];
if(!sel||!sel.isConnected||String(sel.tagName||'').toLowerCase()!=='select')return __mcpB64({{ok:true}});
var st=__mcpStyle(sel),r=sel.getBoundingClientRect();
var hidden=st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0||r.width<=2||r.height<=2||sel.getAttribute('aria-hidden')==='true';
if(!hidden)return __mcpB64({{ok:true}});
function shown(e){{return e&&e.nodeType===1&&__mcpVisible(e);}}
function selects(e){{try{{return e.querySelectorAll('select').length;}}catch(x){{return 9;}}}}
var box=null;
/* A combobox wrapped around the select (Choices.js) is the box; else one drawn right after it (Select2, Tom Select). */
for(var a=__mcpParent(sel),k=0;a&&k<3&&!box;a=__mcpParent(a),k++){{if(selects(a)>1)break;if(shown(a)&&String(a.getAttribute('role')||'')==='combobox')box=a;}}
for(var n=sel.nextElementSibling,g=0;n&&g<2&&!box;n=n.nextElementSibling,g++){{if(shown(n)&&!/^(select|input|textarea|label)$/i.test(n.tagName)&&!selects(n))box=n;}}
for(var p=__mcpParent(sel),h=0;p&&h<3&&!box;p=__mcpParent(p),h++){{if(selects(p)>1)break;if(shown(p)&&p.querySelector('[role="combobox"],[aria-haspopup],[class*="select"],[class*="choice"]'))box=p;}}
if(!box)return __mcpB64({{ok:true,hidden:true}});
var trigger=null;try{{trigger=Array.from(box.querySelectorAll('[role="combobox"],[aria-haspopup="listbox"],[aria-haspopup="true"]')).filter(shown)[0]||null;}}catch(x){{}}
trigger=trigger||(box.matches('[role="combobox"]')?box:(Array.from(box.children).filter(shown)[0]||box));
var id=__mcpId(trigger,s);s.elements[id]=trigger;
return __mcpB64({{ok:true,hidden:true,widget_id:id}});
}})()'''


def _select_toggle_js(element_id: str, want: str) -> str:
    """Press a custom select whose aria-expanded says the opposite of want ("open"/"closed"): a press can
    be lost while another list script takes focus back (Choices.js after Tom Select), and a list left
    open after a miss covers the fields below it (Select2). Also says whether the tab is hidden."""
    eid = json.dumps(str(element_id or ""))
    pressed_when = json.dumps("false" if want == "open" else "true")
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var el=__mcpState().elements[{eid}],hidden=!!document.hidden;
if(!el||!el.isConnected)return __mcpB64({{ok:true,pressed:false,hidden:hidden}});
var marked=el.hasAttribute('aria-expanded')?el:el.querySelector('[aria-expanded]');
var expanded=marked?String(marked.getAttribute('aria-expanded')):null;
if(expanded!=={pressed_when})return __mcpB64({{ok:true,pressed:false,hidden:hidden,expanded:expanded}});
__mcpActivate(el);
return __mcpB64({{ok:true,pressed:true,hidden:hidden,expanded:expanded}});
}})()'''


def _select_chosen_js(element_id: str) -> str:
    """The option a native select holds now."""
    eid = json.dumps(str(element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var sel=__mcpState().elements[{eid}],o=sel&&sel.selectedOptions&&sel.selectedOptions[0];
return __mcpB64({{ok:true,value:o?String(o.value):null,text:o?String(o.text||'').trim():null}});
}})()'''


def _fill_suggestion_js(element_id: str, value: Any, mode: str) -> str:
    """mode "before": note the lists open ahead of typing; "pick": press the option the list typing
    opened shows for value; "check": did it stay."""
    return _fill_js(_browser_js("fill_suggestion.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__ELEMENT_ID__": json.dumps(str(element_id or "")),
        "__TEXT__": json.dumps(str(value if value is not None else ""), ensure_ascii=False),
        "__MODE__": json.dumps(mode),
    })


def _fill_kept_js(element_ids: List[str], leave_ids: List[str]) -> str:
    """The value each filled box holds now. A box in leave_ids that still has focus is left first, as a
    person moving on would, so a page that keeps only list choices clears what was merely typed."""
    ids = json.dumps([str(item) for item in element_ids])
    leave = json.dumps([str(item) for item in leave_ids])
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),out={{}},left=[];
{leave}.forEach(function(id){{var e=s.elements[id];if(e&&e.isConnected&&document.activeElement===e){{try{{e.blur();left.push(id);}}catch(x){{}}}}}});
{ids}.forEach(function(id){{var e=s.elements[id];out[id]=e&&e.isConnected?String(e.value==null?'':e.value):null;}});
return __mcpB64({{ok:true,values:out,left:left}});
}})()'''


def _select_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
    mutation_revalidator: Optional[MutationRevalidator] = None,
) -> Dict[str, Any]:
    element_id = str(action.get("element_id") or "")
    if not element_id:
        return {"ok": False, "type": "select", "error": "element_id is required", "_js_calls": 0}
    # Agents often pass the choice as value=; it means the same as option=.
    option = action.get("option") if action.get("option") is not None else action.get("value")
    timeout_s = max(0.2, min(float(action.get("timeout_s", 2.0)), 5.0))
    poll_s = max(0.05, min(float(action.get("poll_ms", 100)) / 1000.0, 0.5))
    started = time.perf_counter()
    js_calls = 0
    # fill already knows whether its select is shown (find's readiness); a plain select action looks.
    if not action.get("_widget_of") and action.get("_widget_check", True):
        widget = _run_json_js(settings, browser, _select_widget_js(element_id), window_index, tab_index, tab_handle)
        js_calls += 1
        if widget.get("widget_id"):
            chosen = _select_action(
                settings, browser, {**action, "element_id": widget["widget_id"], "option": option, "_widget_of": element_id},
                None, window_index, tab_index, tab_handle, mutation_revalidator=mutation_revalidator,
            )
            js_calls += int(chosen.pop("_js_calls", 0))
            if chosen.get("ok"):
                # The select itself says what was chosen; a press the script ignored leaves it unchanged.
                held = _run_json_js(settings, browser, _select_chosen_js(element_id), window_index, tab_index, tab_handle)
                js_calls += 1
                said = re.sub(r"\s+", " ", str(chosen.get("selected") or "")).strip().lower()
                if not held.get("text") or str(held["text"]).strip().lower() != said:
                    chosen = {"ok": False, "type": "select", "error": "choice_not_kept", "reason_code": "SELECT_CHOICE_NOT_KEPT",
                              "picked": chosen.get("selected"), "select_holds": held.get("text")}
                else:
                    chosen["selected"] = held["text"]
            chosen.update(element_id=element_id, widget_element_id=widget["widget_id"],
                          duration_ms=int((time.perf_counter()-started)*1000), _js_calls=js_calls)
            return chosen
    readiness = _wait_for_element_readiness(
        settings, browser, action, window_index, tab_index, tab_handle,
    )
    js_calls += int(readiness.pop("_js_calls", 0))
    if not readiness.get("ready"):
        return {
            "ok": False, "type": "select", "element_id": element_id,
            "error": "element_not_ready", "reason_code": readiness.get("reason_code") or "ELEMENT_NOT_READY",
            "readiness": readiness, "observe_again": True,
            "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
        }
    mutation_target = None
    if mutation_revalidator is not None:
        mutation_target, blocked = mutation_revalidator("select")
        if blocked is not None:
            return {
                "type": "select", "element_id": element_id,
                "duration_ms": int((time.perf_counter()-started)*1000),
                "_js_calls": js_calls, **blocked,
            }
    prep = _run_json_js(
        settings, browser, _select_prepare_js(element_id, observation_id, option),
        window_index, tab_index, tab_handle,
        prevalidated_target=mutation_target,
    )
    js_calls += 1
    if not prep.get("ok"):
        prep.update({"type": "select", "_js_calls": js_calls, "duration_ms": int((time.perf_counter()-started)*1000)})
        return prep
    if prep.get("native"):
        # A change handler can put the old value back: the choice counts once it is still there (#391).
        cancellable_sleep(0.15)
        kept = _run_json_js(settings, browser, _select_kept_js(element_id, option, str(prep.get("value") or "")),
                            window_index, tab_index, tab_handle)
        js_calls += 1
        if kept.get("kept") is False:
            return {
                "ok": False, "type": "select", "element_id": element_id, "native": True,
                "error": "choice_not_kept", "reason_code": "SELECT_CHOICE_NOT_KEPT", "selected": prep.get("selected"),
                "now": kept.get("now"), "automatic_retry": False,
                "hint": "The page put another value back after the choice; observe the field before choosing again.",
                "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
            }
        return {
            "ok": True, "type": "select", "element_id": element_id,
            "selected": prep.get("selected"), "native": True, "verified": True,
            "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
        }
    reopened = False
    page_hidden = False
    while time.perf_counter() - started < timeout_s:
        mutation_target = None
        if mutation_revalidator is not None:
            mutation_target, blocked = mutation_revalidator("select")
            if blocked is not None:
                return {
                    "type": "select", "element_id": element_id,
                    "duration_ms": int((time.perf_counter()-started)*1000),
                    "_js_calls": js_calls, **blocked,
                }
        found = _run_json_js(
            settings, browser, _select_option_js(element_id, option),
            window_index, tab_index, tab_handle,
            prevalidated_target=mutation_target,
        )
        js_calls += 1
        if found.get("found"):
            stable_ms = max(100, min(int(action.get("stable_ms", 250)), 1000))
            settle_deadline = min(started + timeout_s, time.perf_counter() + 1.0)
            last_revision = None
            stable_since = time.perf_counter()
            while time.perf_counter() < settle_deadline:
                state = _run_json_js(
                    settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
                )
                js_calls += 1
                revision = state.get("dom_revision")
                if revision != last_revision:
                    last_revision = revision
                    stable_since = time.perf_counter()
                elif (time.perf_counter() - stable_since) * 1000 >= stable_ms:
                    break
                cancellable_sleep(0.06)
            kept = _run_json_js(settings, browser, _select_kept_js(element_id, found.get("selected") or option),
                                window_index, tab_index, tab_handle)
            js_calls += 1
            if kept.get("kept") is False:
                return {
                    "ok": False, "type": "select", "element_id": element_id, "native": False,
                    "error": "choice_not_kept", "reason_code": "SELECT_CHOICE_NOT_KEPT",
                    "selected": found.get("selected"), "now": kept.get("now"), "automatic_retry": False,
                    "hint": "The option was pressed but the field still shows what it did before; observe it before "
                            "choosing again.",
                    "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
                }
            return {
                "ok": True, "type": "select", "element_id": element_id,
                "selected": found.get("selected"), "native": False,
                **({"verified": True} if kept.get("kept") else {}),
                "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
            }
        if found.get("ambiguous"):
            # Two options in the list this control opened have the asked name: nothing is chosen.
            _run_json_js(settings, browser, _select_toggle_js(element_id, "closed"), window_index, tab_index, tab_handle)
            js_calls += 1
            return {
                "ok": False, "type": "select", "element_id": element_id, "native": False,
                "error": "option_ambiguous", "reason_code": "OPTION_AMBIGUOUS", "matches": found.get("matches"),
                "hint": "The list holds more than one option with that name; give the option's full text.",
                "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
            }
        if not reopened and time.perf_counter() - started >= _SELECT_REOPEN_AFTER_S:
            reopened = True
            toggled = _run_json_js(settings, browser, _select_toggle_js(element_id, "open"), window_index,
                                   tab_index, tab_handle, prevalidated_target=mutation_target)
            js_calls += 1
            # A list that says it is open and lacks the option is a plain miss, hidden tab or not.
            page_hidden = bool(toggled.get("hidden")) and toggled.get("expanded") != "true"
        cancellable_sleep(poll_s)
    _run_json_js(settings, browser, _select_toggle_js(element_id, "closed"), window_index, tab_index, tab_handle)
    js_calls += 1
    missed: Dict[str, Any] = {
        "ok": False, "type": "select", "element_id": element_id,
        "error": "option_not_found", "timed_out": True, "native": False,
        "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
    }
    if page_hidden:
        # Some list scripts open on the next screen refresh, which a background tab never gets.
        missed.update(reason_code="LIST_NEEDS_VISIBLE_TAB",
                      hint="The list did not open while the tab is in the background. Ask the owner to show the "
                           "tab (Show Tab in Mac MCP) and choose again, or check the option text.")
    return missed


def _changes_js(observation_id: Optional[str]) -> str:
    """What the batch changed, relative to the observation it acted on (or the previous act).

    Returns the light state (url/title/dom_revision) plus: url/title changes,
    actionable elements that appeared (with ids usable in the next act),
    how many disappeared, visible alert/validation messages, and an open
    modal. It replaces the final light-state read, so it costs no extra call.
    """
    obs = json.dumps(str(observation_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
__mcpSettleHidden();var s=__mcpState(),meta=(s.observationMeta||{{}})[{obs}],base=(meta&&meta.ids)?meta:s.lastSeen;
var now=__mcpVisibleControls(s),seen=Object.create(null);now.forEach(function(x){{seen[x.id]=1;}});
var out={{ok:true,url:location.href,title:document.title,dom_revision:s.mutationRevision}};
var animating=__mcpAnimatingMs();if(animating)out.animating_ms=animating;
if(base){{
  var had=Object.create(null);(base.ids||[]).forEach(function(id){{had[id]=1;}});
  var appeared=now.filter(function(x){{return !had[x.id];}});
  /* List what just opened in a floating layer (composer, dropdown, drawer) first, and its fields and
     text buttons before its icon toolbar; page content that scrolled into view comes after. */
  var layered=function(e){{for(var c=e,g=0;c&&c.nodeType===1&&g<40;c=__mcpParent(c),g++){{var p='';try{{p=__mcpStyle(c).position;}}catch(_){{}}if(p==='fixed'||p==='sticky')return true;}}return false;}};
  var weight=function(e){{var base=layered(e)?0:3;if(/^(input|textarea|select)$/i.test(e.tagName)||e.isContentEditable)return base;
    return base+(String(e.textContent||'').replace(/[\\s\\u200b\\u00a0]+/g,'')?0:(e.getAttribute('aria-label')||e.getAttribute('title')?1:2));}};
  appeared.forEach(function(x){{x.w=weight(x.el);}});
  appeared.sort(function(x,y){{return x.w-y.w;}});
  out.changes={{baseline:meta&&meta.ids?'observation':'previous_act',
    url_changed:base.url?base.url!==location.href:undefined,
    appeared_count:appeared.length,
    appeared:appeared.slice(0,15).map(function(x){{var e=x.el;return {{element_id:x.id,tag:e.tagName.toLowerCase(),role:__mcpRole(e)||undefined,
      text:__mcpText(e).slice(0,80)||String(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.name||'').slice(0,80)||undefined}};}}),
    disappeared_count:(base.ids||[]).filter(function(id){{return !seen[id];}}).length}};
}}
var alerts=[];
__mcpQueryAll('[role=alert],[role=alertdialog],[aria-live=assertive],[aria-live=polite],.error,.invalid-feedback,.field-error,.alert').forEach(function(n){{
  if(alerts.length>=5||!__mcpVisible(n))return;var t=__mcpText(n).trim();if(t&&alerts.indexOf(t.slice(0,160))<0)alerts.push(t.slice(0,160));}});
var invalid=[];__mcpQueryAll('input,select,textarea').forEach(function(f){{
  if(invalid.length>=5)return;try{{if(f.willValidate&&!f.checkValidity()&&__mcpVisible(f))invalid.push({{element_id:__mcpId(f,s),message:String(f.validationMessage||'').slice(0,120)}});}}catch(e){{}}}});
var modal=__mcpTopBlockingModal();
if(alerts.length||invalid.length||modal){{out.changes=out.changes||{{}};
  if(alerts.length)out.changes.messages=alerts;if(invalid.length)out.changes.invalid_fields=invalid;
  if(modal)out.changes.modal={{element_id:__mcpId(modal,s),text:__mcpText(modal).slice(0,200)}};}}
// While a panel is still animating open, keep the baseline: the caller reads again once it settles.
if(!animating)s.lastSeen={{ids:now.map(function(x){{return x.id;}}),url:location.href,title:document.title}};
return __mcpB64(out);
}})()'''


def _light_state_js() -> str:
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(), a=document.activeElement;
return __mcpB64({{ok:true,url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}},dom_revision:s.mutationRevision,active_element:a&&a.nodeType===1?__mcpDescribe(a,s):null}});
}})()'''



def _element_readiness_js(element_id: str, action_type: str, stable_ms: int) -> str:
    eid = json.dumps(str(element_id or ""))
    typ = json.dumps(str(action_type or "click").lower().replace("-", "_"))
    stable = max(0, min(int(stable_ms), 1500))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState();__mcpFlushMutations(s);__mcpStartMutationWatch(s,2000);
var el=__mcpRecoverElement({eid},s);
if(!el)return __mcpB64({{ok:true,ready:false,reason_code:'ELEMENT_DETACHED',element_id:{eid},dom_revision:s.mutationRevision}});
__mcpScrollIntoView(el);
var rd=__mcpElementReadiness(el,{typ},{stable});rd.ok=true;return __mcpB64(rd);
}})()'''


def _wait_for_element_readiness(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
) -> Dict[str, Any]:
    element_id = str(action.get("element_id") or "")
    typ = str(action.get("type") or "click").lower().replace("-", "_")
    if not element_id:
        return {"ready": False, "reason_code": "ELEMENT_NOT_READY", "error": "element_id is required", "_js_calls": 0}
    timeout_s = max(0.1, min(float(action.get("readiness_timeout_s", _ELEMENT_READINESS_TIMEOUT_S)), 2.5))
    stable_ms = max(0, min(int(action.get("readiness_stable_ms", _ELEMENT_READINESS_STABLE_MS)), 1500))
    poll_s = max(0.03, min(float(action.get("readiness_poll_ms", _ELEMENT_READINESS_POLL_S * 1000)) / 1000.0, 0.25))
    started = time.perf_counter()
    deadline = started + timeout_s
    js_calls = 0
    last: Dict[str, Any] = {}
    while True:
        last = _run_json_js(
            settings,
            browser,
            _element_readiness_js(element_id, typ, stable_ms),
            window_index,
            tab_index,
            tab_handle,
        )
        js_calls += 1
        now = time.perf_counter()
        if last.get("ready") or last.get("reason_code") == "AMBIGUOUS_ACTIVATION_TARGET":
            # A box with several controls stays one: waiting would not choose between them (#390).
            last.update({"duration_ms": int((now - started) * 1000), "_js_calls": js_calls})
            return last
        if action.get("_list_cover_ok") and last.get("reason_code") == "ELEMENT_OCCLUDED" \
                and str((last.get("hit_target") or {}).get("role") or "").lower() in _LIST_COVER_ROLES:
            # fill: another field's dropdown list still lies over this field. A page script can keep it
            # open in a background tab (Tom Select reopens it on a throttled timer); the DOM action
            # reaches the field under it all the same. A dialog or banner over it still stops the action.
            last.update(ready=True, list_cover_ignored=True, duration_ms=int((now - started) * 1000), _js_calls=js_calls)
            return last
        if now >= deadline:
            last.update({"ready": False, "timed_out": True, "duration_ms": int((now - started) * 1000), "_js_calls": js_calls})
            if not last.get("reason_code"):
                last["reason_code"] = "ELEMENT_NOT_READY"
            return last
        cancellable_sleep(poll_s)


def _element_effect_state_js(element_id: str) -> str:
    eid = json.dumps(str(element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=__mcpRecoverElement({eid},s);
if(!el) return __mcpB64({{ok:true,connected:false,related_mutation:null,url:location.href,title:document.title,dom_revision:s.mutationRevision,modal_fingerprint:__mcpModalEffectFingerprint(),activation_network_count:Number(s.lastActivationTrace&&s.lastActivationTrace.network_count||0),activation_trace:s.lastActivationTrace||null}});
var tag=String(el.tagName||'').toLowerCase(),role=__mcpRole(el),value='',text='';
try{{value=('value' in el)?__mcpSafeValue(el):'';}}catch(e){{}}
try{{if(!value&&(el.isContentEditable||role==='textbox'||role==='searchbox'))text=String(el.textContent||'');}}catch(e){{}}
var focused=false;try{{focused=!!(el.ownerDocument&&el.ownerDocument.activeElement===el);}}catch(e){{}}
return __mcpB64({{ok:true,connected:!!el.isConnected,related_mutation:__mcpRelatedMutation(el,Number(s.lastActivationAt||0)),url:location.href,title:document.title,dom_revision:s.mutationRevision,tag:tag,role:role,value:value,text:text,
checked:typeof el.checked==='boolean'?!!el.checked:null,aria_expanded:el.getAttribute('aria-expanded'),aria_selected:el.getAttribute('aria-selected'),aria_pressed:el.getAttribute('aria-pressed'),aria_checked:el.getAttribute('aria-checked'),class_name:String(el.className||''),focused:focused,modal_fingerprint:__mcpModalEffectFingerprint(),activation_network_count:Number(s.lastActivationTrace&&s.lastActivationTrace.network_count||0),activation_trace:s.lastActivationTrace||null}});
}})()'''


def _trusted_activation_prepare_js(element_id: str, stable_ms: int = 0) -> str:
    eid = json.dumps(str(element_id or ""))
    stable = max(0, min(int(stable_ms), 1500))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=__mcpRecoverElement({eid},s);
if(!el)return __mcpB64({{ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true}});
__mcpFlushMutations(s);__mcpStartMutationWatch(s,2000);
var target=__mcpActivationTarget(el);if(!target||!target.isConnected)return __mcpB64({{ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true}});
var rd=__mcpElementReadiness(target,'click',{stable});
if(!rd.ready)return __mcpB64({{ok:false,error:rd.reason_code==='ELEMENT_DETACHED'?'stale_element':'element_not_ready',reason_code:rd.reason_code||'ELEMENT_NOT_READY',observe_again:true,readiness:rd}});
el=target;
var win=__mcpOwnerWindow(el),trace={{mode:'trusted_chrome_cdp',target_click_seen:false,window_click_seen:false,click_is_trusted:null,click_default_prevented:null,dispatch_canceled:[]}};
s.lastActivationTrace=trace;__mcpStartActionNetworkProbe(s,trace);
var targetRecorder=function(ev){{trace.target_click_seen=true;trace.click_is_trusted=!!ev.isTrusted;}};
var windowRecorder=function(ev){{try{{var path=typeof ev.composedPath==='function'?ev.composedPath():[];if(ev.target===el||path.indexOf(el)>=0||__mcpComposedContains(el,ev.target)){{trace.window_click_seen=true;trace.click_is_trusted=!!ev.isTrusted;trace.click_default_prevented=!!ev.defaultPrevented;}}}}catch(e){{}}}};
try{{el.addEventListener('click',targetRecorder,{{capture:true,once:true}});win.addEventListener('click',windowRecorder,{{capture:false,once:true}});setTimeout(function(){{try{{el.removeEventListener('click',targetRecorder,true);win.removeEventListener('click',windowRecorder,false);}}catch(e){{}}}},750);}}catch(e){{}}
var role=__mcpRole(el),value='',text='';try{{value=('value' in el)?__mcpSafeValue(el):'';}}catch(e){{}}try{{if(!value&&(el.isContentEditable||role==='textbox'||role==='searchbox'))text=String(el.textContent||'');}}catch(e){{}}
return __mcpB64({{ok:true,connected:!!el.isConnected,element_id:rd.element_id,rect:rd.rect,stable_for_ms:rd.stable_for_ms,hit_target:rd.hit_target,url:location.href,title:document.title,dom_revision:rd.dom_revision,value:value,text:text,checked:typeof el.checked==='boolean'?!!el.checked:null,aria_expanded:el.getAttribute('aria-expanded'),aria_selected:el.getAttribute('aria-selected'),aria_pressed:el.getAttribute('aria-pressed'),aria_checked:el.getAttribute('aria-checked'),class_name:String(el.className||''),modal_fingerprint:__mcpModalEffectFingerprint(),activation_network_count:0,activation_trace:trace}});
}})()'''


def _keyboard_activation_js(element_id: str) -> str:
    eid = json.dumps(str(element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=__mcpRecoverElement({eid},s);if(!el)return __mcpB64({{ok:false,error:'stale_element'}});
var role=String(__mcpRole(el)||'').toLowerCase(),key=(role==='combobox'||role==='listbox')?'ArrowDown':((role==='checkbox'||role==='switch'||role==='radio')?' ':'Enter');
try{{el.focus({{preventScroll:true}});}}catch(e){{try{{el.focus();}}catch(_){{}}}}
var win=__mcpOwnerWindow(el);
function fire(type){{try{{el.dispatchEvent(new win.KeyboardEvent(type,{{bubbles:true,cancelable:true,composed:true,key:key,code:key===' '?'Space':key}}));}}catch(e){{}}}}
fire('keydown');fire('keypress');fire('keyup');return __mcpB64({{ok:true,key:key}});
}})()'''


_DOM_KEYS: Dict[str, Tuple[str, str, int]] = {
    "enter": ("Enter", "Enter", 13), "return": ("Enter", "Enter", 13),
    "escape": ("Escape", "Escape", 27), "esc": ("Escape", "Escape", 27),
    "tab": ("Tab", "Tab", 9), "space": (" ", "Space", 32),
    "backspace": ("Backspace", "Backspace", 8), "delete": ("Delete", "Delete", 46),
    "arrowup": ("ArrowUp", "ArrowUp", 38), "up": ("ArrowUp", "ArrowUp", 38),
    "arrowdown": ("ArrowDown", "ArrowDown", 40), "down": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37), "left": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39), "right": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36), "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33), "pagedown": ("PageDown", "PageDown", 34),
}
_DOM_KEY_MODIFIERS = {
    "ctrl": "ctrlKey", "control": "ctrlKey", "shift": "shiftKey", "alt": "altKey",
    "option": "altKey", "meta": "metaKey", "cmd": "metaKey", "command": "metaKey",
}


def _dom_key_spec(key: str) -> Optional[Tuple[str, str, int]]:
    raw = str(key or "")
    normalized = raw.strip().lower().replace("_", "").replace("-", "")
    if normalized in _DOM_KEYS:
        return _DOM_KEYS[normalized]
    if len(raw) == 1 and raw.isprintable():
        upper = raw.upper()
        if upper.isalpha() and upper.isascii():
            return raw, f"Key{upper}", ord(upper)
        if raw.isdigit():
            return raw, f"Digit{raw}", ord(raw)
        return raw, "", ord(raw)
    return None


def _dom_key_js(element_id: Optional[str], key: Tuple[str, str, int], modifiers: List[str]) -> str:
    """Dispatch an untrusted keyboard sequence without changing app focus.

    Untrusted Enter never triggers a browser's implicit form submission, so it
    is emulated with requestSubmit() when the page did not handle the keydown.
    """
    eid = json.dumps(str(element_id or ""))
    key_name, code, key_code = key
    flags = {name: False for name in ("ctrlKey", "shiftKey", "altKey", "metaKey")}
    for modifier in modifiers or []:
        mapped = _DOM_KEY_MODIFIERS.get(str(modifier or "").strip().lower())
        if mapped:
            flags[mapped] = True
    spec = json.dumps({"key": key_name, "code": code, "keyCode": key_code, **flags})
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),spec={spec},eid={eid},el=null;
if(eid){{el=__mcpRecoverElement(eid,s);if(!el)return __mcpB64({{ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true}});}}
else{{el=document.activeElement;while(el&&el.shadowRoot&&el.shadowRoot.activeElement)el=el.shadowRoot.activeElement;}}
if(!el||el===document.body||el===document.documentElement)return __mcpB64({{ok:false,error:'no_focused_element',reason_code:'DOM_KEY_TARGET_REQUIRED',observe_again:true}});
__mcpFlushMutations(s);__mcpStartMutationWatch(s,3000);
var before=__mcpEffectState(el),revision=s.mutationRevision,url=location.href,title=document.title;s.lastActivationAt=Date.now();
try{{if(el.ownerDocument.activeElement!==el)el.focus({{preventScroll:true}});}}catch(e){{}}
var win=__mcpOwnerWindow(el);
function make(type){{
  var code=type==='keypress'?(spec.key.length===1?spec.key.charCodeAt(0):spec.keyCode):spec.keyCode;
  var ev=new win.KeyboardEvent(type,{{bubbles:true,cancelable:true,composed:true,key:spec.key,code:spec.code,ctrlKey:spec.ctrlKey,shiftKey:spec.shiftKey,altKey:spec.altKey,metaKey:spec.metaKey}});
  try{{Object.defineProperty(ev,'keyCode',{{get:function(){{return code;}}}});Object.defineProperty(ev,'which',{{get:function(){{return code;}}}});Object.defineProperty(ev,'charCode',{{get:function(){{return type==='keypress'?code:0;}}}});}}catch(e){{}}
  return ev;
}}
var downAllowed=el.dispatchEvent(make('keydown')),pressAllowed=true;
if(downAllowed&&(spec.key.length===1||spec.key==='Enter'))pressAllowed=el.dispatchEvent(make('keypress'));
el.dispatchEvent(make('keyup'));
var submitted=false,tag=String(el.tagName||'').toLowerCase(),itype=String(el.type||'').toLowerCase();
var plain=!spec.ctrlKey&&!spec.altKey&&!spec.metaKey;
if(spec.key==='Enter'&&plain&&downAllowed&&pressAllowed&&tag==='input'&&el.form&&['button','submit','reset','checkbox','radio','file','image','hidden'].indexOf(itype)<0){{
  var form=el.form,hasSubmit=!!form.querySelector('button:not([type]),button[type=submit],input[type=submit],input[type=image]');
  var blocking=Array.prototype.filter.call(form.elements||[],function(f){{var t=String(f.type||'').toLowerCase();return String(f.tagName||'').toLowerCase()==='input'&&['text','search','url','tel','email','password','date','datetime-local','month','week','time','number'].indexOf(t)>=0;}}).length;
  if(hasSubmit||blocking===1){{try{{if(typeof form.requestSubmit==='function')form.requestSubmit();else form.submit();submitted=true;}}catch(e){{}}}}
}}
return __mcpB64({{ok:true,element_id:__mcpId(el,s),key:spec.key,keydown_handled:!downAllowed,form_submitted:submitted,_verify_revision:revision,_verify_url:url,_verify_title:title,_verify_state:before}});
}})()'''


def _dom_key_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    element_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
    *,
    prevalidated_target: Any = None,
) -> Dict[str, Any]:
    """Background-safe key press: DOM events plus bounded, read-only effect check."""
    key_label = str(action.get("key") or "")
    spec = _dom_key_spec(key_label)
    base = {"type": "key", "key": key_label, "input_mode": "dom", "input_trust": "untrusted"}
    if spec is None:
        return {
            **base, "ok": False, "error": "unsupported_dom_key", "reason_code": "UNSUPPORTED_DOM_KEY",
            "reason": "This key has no DOM equivalent. Use a single character or a named key such as Enter, Escape, Tab or ArrowDown.",
            "_js_calls": 0,
        }
    out = _run_json_js(
        settings, browser, _dom_key_js(element_id, spec, list(action.get("modifiers") or [])),
        window_index, tab_index, tab_handle, prevalidated_target=prevalidated_target,
    )
    js_calls = 1
    if not out.get("ok"):
        return {**base, **{k: v for k, v in out.items() if not k.startswith("_")}, "ok": False, "_js_calls": js_calls}
    target_id = str(out.get("element_id") or element_id or "")
    result: Dict[str, Any] = {
        **base, "ok": True, "element_id": target_id or None,
        "keydown_handled": bool(out.get("keydown_handled")), "form_submitted": bool(out.get("form_submitted")),
        "effect_observed": False,
    }
    before_state = out.get("_verify_state") if isinstance(out.get("_verify_state"), dict) else {}
    before = {
        "url": out.get("_verify_url"), "title": out.get("_verify_title"),
        "connected": before_state.get("connected", True), "value": before_state.get("value"),
        "text": before_state.get("text"), "checked": before_state.get("checked"),
        "aria_expanded": before_state.get("expanded"), "aria_selected": before_state.get("selected"),
        "aria_pressed": before_state.get("pressed"), "aria_checked": before_state.get("ariaChecked"),
        "class_name": before_state.get("cls"), "modal_fingerprint": before_state.get("modalFingerprint"),
        "activation_network_count": before_state.get("activationNetworkCount", 0),
    }
    before_revision = out.get("_verify_revision")
    compact_state: Optional[Dict[str, Any]] = None
    deadline = time.perf_counter() + max(
        0.1, min(float(action.get("verify_timeout_s", _ACTION_VERIFY_TIMEOUT_S)), 2.0),
    )
    while time.perf_counter() < deadline:
        cancellable_sleep(_ACTION_VERIFY_POLL_S)
        try:
            post = _run_json_js(
                settings, browser, _element_effect_state_js(target_id),
                window_index, tab_index, tab_handle,
            )
            js_calls += 1
        except HTTPException:
            moved = _moved_after_lost_page(settings, browser, window_index, tab_index, tab_handle, before.get("url"))
            js_calls += 1
            if moved is None:
                result.update(_OUTCOME_UNKNOWN)
                break
            if moved:
                result.update({"effect_observed": True, "verification": "async_navigation"})
                break
            continue
        if _effect_changed(before, post):
            result.update({"effect_observed": True, "verification": "state_changed"})
        elif (before_revision is not None and post.get("dom_revision") != before_revision
              and post.get("related_mutation") is not False):
            result.update({"effect_observed": True, "verification": "dom_mutated"})
        if result["effect_observed"]:
            compact_state = post
            break
    if not result["effect_observed"] and not result.get("outcome_unknown") and (result["keydown_handled"] or result["form_submitted"]):
        result.update({
            "effect_observed": True,
            "verification": "form_submitted" if result["form_submitted"] else "keydown_handled_by_page",
        })
    if not result["effect_observed"] and not result.get("outcome_unknown"):
        result.update({
            "ok": False, "error": "action_no_effect", "reason_code": "ACTION_NO_EFFECT",
            "verification": "no_effect_after_bounded_wait", "observe_again": True, "automatic_retry": False,
            "reason": (
                "The page did not react to the DOM key event. Some pages ignore untrusted keyboard input; "
                "click the confirming control instead. Native keys need user-granted foreground authorization."
            ),
        })
    result["_js_calls"] = js_calls
    if compact_state is not None:
        result["_compact_state"] = compact_state
    return result


def _moved_after_lost_page(
    settings: Settings, browser: str, window_index: int, tab_index: Optional[int], tab_handle: Optional[str],
    before_url: Optional[str],
) -> Optional[bool]:
    """After the page stopped answering during verification (#391): True when the tab now shows another
    URL, False when the same page answers again, None when it still cannot be read. A lost bridge, a 403
    or a timeout alone is no proof that the action navigated."""
    cancellable_sleep(0.3)
    try:
        state = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index, tab_handle)
    except HTTPException:
        return None
    url = state.get("url")
    return bool(before_url is not None and url and url != before_url)


_OUTCOME_UNKNOWN = {
    "ok": False, "effect_observed": False, "error": "action_outcome_unknown", "reason_code": "ACTION_OUTCOME_UNKNOWN",
    "verification": "verification_unavailable", "outcome_unknown": True, "automatic_retry": False,
    "observe_again": True,
    "reason": "The action was sent, then the page could not be read to see what it did. Observe before acting again.",
}


def _effect_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    if not before or not after:
        return False
    if before.get("url") != after.get("url") or before.get("title") != after.get("title"):
        return True
    if not after.get("connected", True):
        return True
    for key in ("value", "text", "checked", "aria_expanded", "aria_selected", "aria_pressed", "aria_checked", "class_name", "modal_fingerprint"):
        if before.get(key) != after.get(key):
            return True
    if int(before.get("activation_network_count") or 0) != int(after.get("activation_network_count") or 0):
        return True
    return False


def _ready_then_batch_js(action: Dict[str, Any], observation_id: Optional[str], action_type: str, stable_ms: int) -> str:
    """Check readiness and, only if ready, run the action in the same bridge call.

    Not ready means nothing ran: the host then falls back to its bounded readiness
    polling before acting, exactly as before.
    """
    readiness = _element_readiness_js(str(action.get("element_id") or ""), action_type, stable_ms)
    batch = _batch_js([action], observation_id)
    return (
        "(function(){"
        "function __mcpDecode(v){return JSON.parse(decodeURIComponent(escape(atob(v))));}"
        "function __mcpEncode(o){return btoa(unescape(encodeURIComponent(JSON.stringify(o))));}"
        "var raw=" + readiness + ";"
        "if(raw==='" + _AGENT_API_MISSING + "')return raw;"
        "var ready=__mcpDecode(raw);"
        "if(!ready.ready)return __mcpEncode({fused:true,ready:false,readiness:ready});"
        "var done=" + batch + ";"
        "if(done==='" + _AGENT_API_MISSING + "')return done;"
        "return __mcpEncode({fused:true,ready:true,readiness:ready,batch:__mcpDecode(done)});"
        "})()"
    )


# Phase timings use their own clock so tests that script time.perf_counter keep working.
_phase_clock = time.perf_counter


def _try_fused_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    action_type: str,
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
    mutation_revalidator: Optional[MutationRevalidator],
) -> Dict[str, Any]:
    """Most targets are already stable: check readiness and act in one bridge call."""
    target = None
    if mutation_revalidator is not None:
        target, blocked = mutation_revalidator(action_type)
        if blocked is not None:
            return {"blocked": blocked, "js_calls": 0}
    stable_ms = max(0, min(int(action.get("readiness_stable_ms", _ELEMENT_READINESS_STABLE_MS)), 1500))
    attempt = _run_json_js(
        settings, browser, _ready_then_batch_js(action, observation_id, action_type, stable_ms),
        window_index, tab_index, tab_handle, prevalidated_target=target,
    )
    if attempt.get("ready") and isinstance(attempt.get("batch"), dict):
        return {"out": attempt["batch"], "readiness": attempt.get("readiness"), "js_calls": 1}
    return {"js_calls": 1}


def _verified_dom_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
    mutation_revalidator: Optional[MutationRevalidator] = None,
) -> Dict[str, Any]:
    typ = str(action.get("type") or "").lower().replace("-", "_")
    element_id = str(action.get("element_id") or "")
    input_mode = str(action.get("input_mode") or "synthetic").strip().lower()
    js_calls = 0
    if input_mode not in {"synthetic", "trusted"}:
        return {
            "ok": False, "type": typ, "element_id": element_id or None,
            "error": "invalid_input_mode", "reason_code": "INVALID_INPUT_MODE",
            "message": "input_mode must be synthetic or trusted.", "_js_calls": 0,
        }
    if input_mode == "trusted" and typ not in {"click", "double_click"}:
        return {
            "ok": False, "type": typ, "element_id": element_id or None,
            "error": "trusted_input_not_supported_for_action",
            "reason_code": "TRUSTED_INPUT_NOT_SUPPORTED_FOR_ACTION",
            "message": "input_mode=trusted is supported only for click and double_click.", "_js_calls": 0,
        }

    phase_started = _phase_clock()
    phases: Dict[str, int] = {}
    fused_out: Optional[Dict[str, Any]] = None
    readiness: Dict[str, Any] = {}
    if input_mode == "synthetic" and element_id:
        fused = _try_fused_action(
            settings, browser, action, observation_id, typ, window_index, tab_index, tab_handle,
            mutation_revalidator,
        )
        js_calls += int(fused.get("js_calls") or 0)
        if fused.get("blocked") is not None:
            return {"type": typ, "element_id": element_id or None, "_js_calls": js_calls, **fused["blocked"]}
        if fused.get("out") is not None:
            readiness = dict(fused.get("readiness") or {})
            readiness["duration_ms"] = int((_phase_clock() - phase_started) * 1000)
            fused_out = fused["out"]
    if fused_out is None:
        readiness = _wait_for_element_readiness(
            settings, browser, action, window_index, tab_index, tab_handle,
        )
        js_calls += int(readiness.pop("_js_calls", 0))
    phases["readiness_ms"] = int((_phase_clock() - phase_started) * 1000)
    if readiness.get("reason_code") == "AMBIGUOUS_ACTIVATION_TARGET":
        return {
            "ok": False, "type": typ, "element_id": element_id or None,
            "error": "ambiguous_activation_target", "reason_code": "AMBIGUOUS_ACTIVATION_TARGET",
            "controls": readiness.get("controls") or [],
            "message": "That element holds several separate controls; nothing was clicked. Target the one you "
                       "mean (its own element_id, or query with its label).",
            "observe_again": True, "retryable": False, "_js_calls": js_calls,
        }
    if not readiness.get("ready"):
        return {
            "ok": False,
            "type": typ,
            "element_id": element_id or None,
            "error": "element_not_ready",
            "reason_code": readiness.get("reason_code") or "ELEMENT_NOT_READY",
            "readiness": readiness,
            "observe_again": True,
            "retryable": True,
            "_js_calls": js_calls,
        }

    if input_mode == "trusted":
        if browser != "Google Chrome":
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": "trusted_input_unavailable", "reason_code": "TRUSTED_INPUT_UNAVAILABLE",
                "message": "Background trusted pointer input is unavailable for Safari. No foreground or coordinate fallback was attempted.",
                "activation_mode": "trusted_requested", "automatic_retry": False,
                "foreground_fallback": False, "readiness": readiness, "_js_calls": js_calls,
            }
        if current_frame_scope():
            return {**{"ok": False, "type": typ, "error": "trusted_input_in_frame_unsupported", "reason_code": "TRUSTED_INPUT_IN_FRAME",
                "message": "Trusted pointer input uses top-level page coordinates; inside a frame use DOM actions (the default).",
                "_js_calls": 0}, "element_id": element_id or None, "_js_calls": js_calls}
        if not chrome_background_bridge.is_connected():
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": "trusted_input_unavailable", "reason_code": "TRUSTED_INPUT_UNAVAILABLE",
                "message": "Chrome Background Companion is not connected, so trusted background pointer input is unavailable.",
                "activation_mode": "trusted_requested", "automatic_retry": False,
                "foreground_fallback": False, "retryable": True, "readiness": readiness, "_js_calls": js_calls,
            }
        stable_ms = max(0, min(int(action.get("readiness_stable_ms", _ELEMENT_READINESS_STABLE_MS)), 1500))
        before = _run_json_js(
            settings, browser, _trusted_activation_prepare_js(element_id, stable_ms),
            window_index, tab_index, tab_handle,
        )
        js_calls += 1
        if not before.get("ok") or not before.get("connected", True):
            error = str(before.get("error") or "stale_element")
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": error,
                "reason_code": str(before.get("reason_code") or ("ELEMENT_DETACHED" if error == "stale_element" else "ELEMENT_NOT_READY")),
                "observe_again": True, "retryable": True,
                "readiness": before.get("readiness") if isinstance(before.get("readiness"), dict) else readiness,
                "_js_calls": js_calls,
            }
        ready_element_id = readiness.get("element_id")
        fresh_element_id = before.get("element_id")
        if ready_element_id and fresh_element_id and ready_element_id != fresh_element_id:
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": "stale_element", "reason_code": "TARGET_CHANGED",
                "observe_again": True, "retryable": True,
                "readiness": readiness, "_js_calls": js_calls,
            }
        refreshed_readiness = dict(readiness)
        for key in ("element_id", "rect", "stable_for_ms", "hit_target", "dom_revision"):
            if before.get(key) is not None:
                refreshed_readiness[key] = before.get(key)
        readiness = refreshed_readiness
        rect = readiness.get("rect") if isinstance(readiness.get("rect"), dict) else {}
        if float(rect.get("w") or 0) <= 0 or float(rect.get("h") or 0) <= 0:
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": "element_not_ready", "reason_code": "ELEMENT_ZERO_BOUNDS",
                "observe_again": True, "retryable": True,
                "readiness": readiness, "_js_calls": js_calls,
            }
        x = float(rect.get("x") or 0) + float(rect.get("w") or 0) / 2.0
        y = float(rect.get("y") or 0) + float(rect.get("h") or 0) / 2.0
        mutation_target = None
        if mutation_revalidator is not None:
            mutation_target, blocked = mutation_revalidator(typ)
            if blocked is not None:
                return {
                    "type": typ, "element_id": element_id or None,
                    "readiness": readiness, "_js_calls": js_calls, **blocked,
                }
        if mutation_target is not None:
            native_id = mutation_target.native_id
        else:
            try:
                _, _, row = browser_tabs.resolve_tab(browser, str(tab_handle or ""))
                native_id = row.get("native_id")
            except KeyError:
                native_id = None
        if not native_id:
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE",
                "observe_again": True, "readiness": readiness, "_js_calls": js_calls,
            }
        try:
            chrome_background_bridge.request_dispatch_mouse(
                native_id, x, y, click_count=2 if typ == "double_click" else 1, timeout_s=8.0,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return {
                "ok": False, "type": typ, "element_id": element_id or None,
                "error": str(detail.get("error") or "trusted_input_dispatch_failed"),
                "reason_code": "TRUSTED_INPUT_DISPATCH_FAILED",
                "message": str(detail.get("message") or "Chrome trusted background pointer dispatch failed."),
                "activation_mode": "trusted_chrome_cdp", "automatic_retry": False,
                "foreground_fallback": False, "readiness": readiness, "_js_calls": js_calls,
            }
        verify_state = {
            "connected": before.get("connected", True), "value": before.get("value"),
            "text": before.get("text"), "checked": before.get("checked"),
            "expanded": before.get("aria_expanded"), "selected": before.get("aria_selected"),
            "pressed": before.get("aria_pressed"), "ariaChecked": before.get("aria_checked"),
            "cls": before.get("class_name"), "modalFingerprint": before.get("modal_fingerprint"),
            "activationNetworkCount": before.get("activation_network_count", 0),
        }
        result = {
            "ok": True, "type": typ, "element_id": element_id or None,
            "effect_observed": False, "verification": "trusted_input_dispatched",
            "activation_mode": "trusted_chrome_cdp", "input_trust": "browser_debugger",
            "activation_trace": {"mode": "trusted_chrome_cdp", "dispatched": True},
            "_verify_revision": before.get("dom_revision"), "_verify_url": before.get("url"),
            "_verify_title": before.get("title"), "_verify_state": verify_state,
        }
        out = {"ok": True, "actions": [result]}
    else:
        action_started = _phase_clock()
        if fused_out is not None:
            out = fused_out
            phases["action_ms"] = 0  # ran inside the readiness call
        else:
            mutation_target = None
            if mutation_revalidator is not None:
                mutation_target, blocked = mutation_revalidator(typ)
                if blocked is not None:
                    return {
                        "type": typ, "element_id": element_id or None,
                        "readiness": readiness, "_js_calls": js_calls, **blocked,
                    }
            out = _run_json_js(
                settings, browser, _batch_js([action], observation_id),
                window_index, tab_index, tab_handle,
                prevalidated_target=mutation_target,
            )
            js_calls += 1
            phases["action_ms"] = int((_phase_clock() - action_started) * 1000)
        result = dict((out.get("actions") or [out])[0])
        if typ in {"type", "type_text", "paste"} and result.get("verification") == "focus_settling":
            # The field took focus and its editor is re-rendering; nothing was typed yet.
            # Type once more after it settles (the second call never re-prepares).
            cancellable_sleep(_TYPE_FOCUS_SETTLE_S)
            settle_target = None
            if mutation_revalidator is not None:
                settle_target, blocked = mutation_revalidator(typ)
                if blocked is not None:
                    return {"type": typ, "element_id": element_id or None,
                            "readiness": readiness, "_js_calls": js_calls, **blocked}
            out = _run_json_js(
                settings, browser, _batch_js([{**action, "_focus_settled": True}], None),
                window_index, tab_index, tab_handle,
                prevalidated_target=settle_target,
            )
            js_calls += 1
            result = dict((out.get("actions") or [out])[0])
            result["focus_settled"] = True
        if typ in {"click", "double_click"}:
            result.setdefault("activation_mode", "synthetic_dom")
            trace = result.get("activation_trace") if isinstance(result.get("activation_trace"), dict) else {}
            if trace.get("click_is_trusted") is True:
                result.setdefault("input_trust", "trusted")
            elif trace.get("target_click_seen") or trace.get("window_click_seen"):
                result.setdefault("input_trust", "untrusted")
            else:
                result.setdefault("input_trust", "unknown")
            result.setdefault("trusted_input_available", bool(browser == "Google Chrome" and chrome_background_bridge.is_connected()))
            result.setdefault("trusted_input_required", "unknown")
    if "type" not in result:
        result["type"] = typ
    if element_id and "element_id" not in result:
        result["element_id"] = element_id
    result["readiness"] = {
        key: readiness.get(key)
        for key in ("ready", "reason_code", "stable_for_ms", "dom_revision", "rect", "pointer_events", "hit_tag", "hit_target", "modal_scope", "associated_control", "associated_label", "association_ambiguous", "pointer_events_association_fallback", "duration_ms")
        if readiness.get(key) is not None
    }

    before_revision = result.pop("_verify_revision", None)
    before_url = result.pop("_verify_url", None)
    before_title = result.pop("_verify_title", None)
    before_state = result.pop("_verify_state", None)
    before_quiet_ms = result.pop("_verify_quiet_ms", None)
    page_was_quiet = before_quiet_ms is not None and float(before_quiet_ms) >= _DOM_EFFECT_QUIET_MS
    normalized_before_state: Dict[str, Any] = {
        "url": before_url,
        "title": before_title,
        "dom_revision": before_revision,
    }
    if isinstance(before_state, dict):
        normalized_before_state.update({
            "connected": before_state.get("connected", True),
            "value": before_state.get("value"),
            "text": before_state.get("text"),
            "checked": before_state.get("checked"),
            "aria_expanded": before_state.get("expanded"),
            "aria_selected": before_state.get("selected"),
            "aria_pressed": before_state.get("pressed"),
            "aria_checked": before_state.get("ariaChecked"),
            "class_name": before_state.get("cls"),
            "modal_fingerprint": before_state.get("modalFingerprint"),
            "activation_network_count": before_state.get("activationNetworkCount", 0),
        })
    compact_state = out.get("state") if isinstance(out.get("state"), dict) else None

    # A real click is emitted only once. Verification is read-only and bounded so a
    # delayed SPA commit can be observed without risking a duplicate destructive action.
    verify_started = _phase_clock()
    if result.get("ok") and typ in {"click", "double_click"} and not result.get("effect_observed"):
        deadline = time.perf_counter() + max(
            0.1,
            min(float(action.get("verify_timeout_s", _ACTION_VERIFY_TIMEOUT_S)), 2.0),
        )
        poll_s = max(
            0.03,
            min(float(action.get("verify_poll_ms", _ACTION_VERIFY_POLL_S * 1000)) / 1000.0, 0.25),
        )
        while time.perf_counter() < deadline:
            cancellable_sleep(poll_s)
            try:
                post = _run_json_js(
                    settings, browser, _element_effect_state_js(element_id),
                    window_index, tab_index, tab_handle,
                )
                js_calls += 1
                post_trace = post.get("activation_trace") if isinstance(post.get("activation_trace"), dict) else None
                if post_trace:
                    result["activation_trace"] = post_trace
                    if post_trace.get("click_is_trusted") is True:
                        result["input_trust"] = "trusted"
                    elif post_trace.get("target_click_seen") or post_trace.get("window_click_seen"):
                        result["input_trust"] = "untrusted"
                network_progressed = int(post.get("activation_network_count") or 0) > int(normalized_before_state.get("activation_network_count") or 0)
                progressed = (
                    _effect_changed(normalized_before_state, post)
                    or (before_url is not None and post.get("url") != before_url)
                    or (before_title is not None and post.get("title") != before_title)
                )
                # A change elsewhere on the page (a clock, a feed) is not this click's effect (#391).
                dom_progressed = (
                    page_was_quiet and before_revision is not None
                    and post.get("dom_revision") is not None and post.get("dom_revision") != before_revision
                    and post.get("related_mutation") is not False
                )
                if progressed or dom_progressed:
                    result["effect_observed"] = True
                    result["verification"] = (
                        "async_network_activity" if network_progressed
                        else "async_state_changed" if progressed else "async_dom_mutated"
                    )
                    result.pop("observe_again", None)
                    compact_state = post
                    break
            except HTTPException:
                # Navigation can drop the old document while the click takes effect, but so can a lost
                # bridge or a timeout: only another URL afterwards counts as the click's effect (#391).
                moved = _moved_after_lost_page(settings, browser, window_index, tab_index, tab_handle, before_url)
                js_calls += 1
                if moved is None:
                    result.update(_OUTCOME_UNKNOWN)
                    break
                if moved:
                    result["effect_observed"] = True
                    result["verification"] = "async_navigation"
                    result.pop("observe_again", None)
                    break
                continue

        if not result.get("effect_observed") and not result.get("outcome_unknown"):
            result.update({
                "ok": False,
                "error": "action_no_effect",
                "reason_code": "ACTION_NO_EFFECT",
                "verification": "no_effect_after_bounded_wait",
                "observe_again": True,
                "automatic_retry": False,
            })

    expected_text = result.pop("_verify_text", None)
    model_unconfirmed = bool(result.pop("_model_unconfirmed", False))
    if result.get("verification") == "editor_pending" and typ in {"type", "type_text", "paste"}:
        # The editor accepted the input into its model and renders it shortly; confirm the
        # text arrived (read-only, bounded) instead of reporting success on faith.
        wanted = _fold_typed_text(str(expected_text or ""))
        deadline = time.perf_counter() + _TYPE_VERIFY_TIMEOUT_S
        seen = ""
        while time.perf_counter() < deadline:
            cancellable_sleep(_TYPE_VERIFY_POLL_S)
            post = _run_json_js(
                settings, browser, _element_effect_state_js(element_id),
                window_index, tab_index, tab_handle,
            )
            js_calls += 1
            seen = str(post.get("value") or post.get("text") or "")
            if _fold_typed_text(seen) == wanted:
                result.update({"effect_observed": True, "verification": "value_applied_async", "value": seen[:200]})
                result.pop("observe_again", None)
                if model_unconfirmed:
                    # Visible, but the page wrote it without telling its framework; do not call it accepted.
                    result.update({"verification": "editor_model_unconfirmed", "editor_state_unverified": True,
                                   "observe_again": True})
                compact_state = post
                break
        else:
            # The editor took the input event (its own paste or beforeinput handling) but the text never
            # appeared. It may hold the text without drawing it, or have dropped it: nothing seen here
            # tells which (#205). Stop the batch, and do not invite a second paste that could enter it twice.
            result.update({
                "ok": False, "error": "input_unconfirmed", "reason_code": "EDITOR_RENDER_UNCONFIRMED",
                "verification": "editor_render_unconfirmed", "value": seen[:200], "observe_again": True,
                "outcome_unknown": True, "automatic_retry": False,
                "hint": "The editor took the input but has not shown the text. Observe the field (or reload the "
                        "draft) before typing again; typing again could enter the text twice.",
            })

    settle_text = result.pop("_settle_text", None)
    prior_text = result.pop("_prior_text", None)
    if settle_text is not None and result.get("verification") == "value_applied" and typ in {"type", "type_text", "paste"}:
        # The editor drew the text at once. A controlled editor (React) can re-render from a model that
        # never took it and put the old text back, so read it once more after it settles (#205).
        cancellable_sleep(_RENDER_SETTLE_S)
        post = _run_json_js(
            settings, browser, _element_effect_state_js(element_id),
            window_index, tab_index, tab_handle,
        )
        js_calls += 1
        seen = str(post.get("value") or post.get("text") or "")
        folded = _fold_typed_text(seen)
        if post.get("connected") is False:
            result.update({"verification": "editor_rerendered_text", "editor_state_unverified": True,
                           "observe_again": True, "rendered_after_settle": None})
        elif folded == _fold_typed_text(str(settle_text)):
            result["rendered_after_settle"] = True
            compact_state = post
        elif not folded.strip() or folded == _fold_typed_text(str(prior_text or "")):
            # Empty again, or back to what was there before: the text is gone from the page.
            result.update({
                "ok": False, "error": "input_lost_after_render", "reason_code": "INPUT_LOST_AFTER_RENDER",
                "verification": "editor_rerender_lost", "value": seen[:200], "rendered_after_settle": False,
                "observe_again": True, "automatic_retry": False,
                "hint": "The editor showed the text, then re-rendered without it. Observe the field before "
                        "typing again; check the draft before any submit.",
            })
        else:
            # Still there in the editor's own form (a list item, a link): not lost, not confirmed either.
            result.update({"verification": "editor_rerendered_text", "value": seen[:200],
                           "editor_state_unverified": True, "observe_again": True, "rendered_after_settle": False})

    phases["verify_ms"] = int((_phase_clock() - verify_started) * 1000)
    result["phase_ms"] = phases
    if fused_out is not None:
        result["readiness_fused"] = True
    result["_js_calls"] = js_calls
    if isinstance(compact_state, dict):
        result["_compact_state"] = compact_state
    return result


_TYPOGRAPHIC_FOLDS = (
    (re.compile("[\u2018\u2019\u201a\u201b\u2032]"), "'"),
    (re.compile("[\u201c\u201d\u201e\u201f\u2033]"), '"'),
    (re.compile("\u2026"), "..."),
    (re.compile("[\u2013\u2014]|--"), "-"),
    (re.compile("[\\s\u00a0\u200b]+"), ""),
)


def _fold_typed_text(text: str) -> str:
    """Text compared the way an editor may have rewritten it: smart quotes, dashes and
    ellipses count as what was typed (Medium, Google Docs and Notion convert them), and
    whitespace is ignored. Mirrors __mcpTextMatches in agent_helpers.js."""
    for pattern, replacement in _TYPOGRAPHIC_FOLDS:
        text = pattern.sub(replacement, text)
    return text


def _event_wait_js(
    action: Dict[str, Any],
    initial_url: str,
    timeout_s: float,
    *,
    return_mode: str = "promise",
) -> str:
    """Install an in-page event-driven waiter.

    Chrome can await the returned Promise in one debugger round-trip. Safari's
    Apple Events JavaScript bridge cannot await Promises, so return_mode=token
    installs the same waiter and lets the host read only its compact status.
    """
    kind = str(action.get("for") or action.get("condition") or "selector").lower().strip()
    stable_ms = int(action.get("stable_ms") or (300 if kind == "network_idle" else 500))
    if kind == "network_idle":
        stable_ms = max(150, min(stable_ms, 2000))
    elif kind == "dom_stable":
        stable_ms = max(100, min(stable_ms, 5000))
    else:
        stable_ms = max(0, min(stable_ms, 5000))
    spec = {
        "kind": kind,
        "selector": str(action.get("selector") or ""),
        "text": str(action.get("text") or "").lower(),
        "element_id": str(action.get("element_id") or ""),
        "query": str(action.get("query") or ""),
        "role": str(action.get("role") or ""),
        "actionable_only": bool(action.get("actionable_only", False)),
        "initial_url": str(initial_url or ""),
        "timeout_ms": max(100, min(int(float(timeout_s) * 1000), 60_000)),
        "stable_ms": stable_ms,
        "fallback_ms": 750,
        "return_mode": "token" if return_mode == "token" else "promise",
    }
    return _fill_js(_browser_js("event_wait.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__SPEC__": json.dumps(spec, ensure_ascii=False),
    })


def _event_wait_status_js(token: str) -> str:
    tok = json.dumps(str(token or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),w=s.eventWaiters&&s.eventWaiters[{tok}];
if(!w)return __mcpB64({{ok:false,error:'event_waiter_missing',wait_token:{tok}}});
if(w.done)return __mcpB64(w.result);
return __mcpB64({{ok:true,type:'wait',pending:true,matched:false,wait_token:{tok},
  wait_strategy:'event_driven',url:location.href,title:document.title,dom_revision:s.mutationRevision,
  event_count:Number(w.event_count||0),fallback_ticks:Number(w.fallback_ticks||0),
  duration_ms:Math.max(0,Date.now()-Number(w.started_at||Date.now()))}});
}})()'''


def _event_wait_result(result: Dict[str, Any], *, js_calls: int, started: float) -> Dict[str, Any]:
    out = dict(result)
    out.setdefault("ok", True)
    out.setdefault("type", "wait")
    out.setdefault("duration_ms", int((time.perf_counter() - started) * 1000))
    out["wait_strategy"] = "event_driven"
    out["_js_calls"] = int(js_calls)
    out["telemetry"] = {
        "remote_js_calls": int(js_calls),
        "event_count": int(out.get("event_count") or 0),
        "fallback_polls": int(out.get("fallback_ticks") or 0),
        "duration_ms": int(out.get("duration_ms") or 0),
    }
    payload_bytes = len(
        json.dumps(
            {key: value for key, value in out.items() if key not in {"telemetry", "_compact_state"}},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    )
    out["telemetry"]["payload_bytes"] = payload_bytes
    out["telemetry"]["benchmark"] = record_computer_use_sample(
        "browser_wait",
        duration_ms=int(out.get("duration_ms") or 0),
        payload_bytes=payload_bytes,
        remote_js_calls=int(js_calls),
        ax_traversals=0,
    )
    compact = {
        key: out.get(key)
        for key in ("url", "title", "dom_revision", "matched", "timed_out")
        if out.get(key) is not None
    }
    if compact:
        out["_compact_state"] = compact
    return out


def _network_idle_state_js() -> str:
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState();
var bodyText='';
try{{bodyText=String((document.body&&document.body.innerText)||'').replace(/\\s+/g,' ').trim();}}catch(e){{}}
var controls=0;
try{{controls=__mcpQueryAll('a,button,input,textarea,select,[role="button"],[role="link"],[role="combobox"],[role="textbox"]').length;}}catch(e){{}}
var ready=location.href!=='about:blank'&&document.readyState==='complete'&&bodyText.length>0;
var signature=[location.href,document.title,bodyText.length,controls].join('|');
return __mcpB64({{ok:true,matched:ready,url:location.href,title:document.title,ready_state:document.readyState,body_text_length:bodyText.length,control_count:controls,content_signature:signature,dom_revision:s.mutationRevision,scroll:{{x:scrollX,y:scrollY}}}});
}})()'''


def _condition_js(action: Dict[str, Any], initial_url: str) -> str:
    kind = str(action.get("for") or action.get("condition") or "selector").lower().strip()
    if kind == "selector":
        selector = json.dumps(str(action.get("selector") or ""))
        expr = f"!!__mcpQueryOne({selector})"
    elif kind == "text":
        text = json.dumps(str(action.get("text") or "").lower())
        expr = f"(document.body&&String(document.body.innerText||'').toLowerCase().indexOf({text})>=0)"
    elif kind == "element_removed":
        eid = json.dumps(str(action.get("element_id") or ""))
        expr = f"(function(){{var s=__mcpState(),e=s.elements[{eid}];return !e||!e.isConnected;}})()"
    elif kind == "text_gone":
        text = json.dumps(str(action.get("text") or "").lower())
        expr = f"!(document.body&&String(document.body.innerText||'').toLowerCase().indexOf({text})>=0)"
    elif kind == "url_change":
        base = json.dumps(initial_url)
        expr = f"location.href!=={base}"
    elif kind == "network_idle":
        expr = "location.href!=='about:blank' && document.readyState==='complete' && !!(document.body&&String(document.body.innerText||'').trim())"
    else:
        expr = "false"
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
return __mcpB64({{ok:true,matched:!!({expr}),url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}},dom_revision:__mcpState().mutationRevision}});
}})()'''


def _local_copy_js(text: str) -> str:
    """Where this page keeps its own copy of text (localStorage, sessionStorage), never the values (#81).

    A site that keeps a draft in the browser shows it again after a reload, so seeing the text then does not
    show the site saved it. Bounded: at most 400 keys per store, 200 KB per value; JSON values are read for
    their text, since a stored draft escapes quotes and line breaks.
    """
    want = json.dumps(" ".join(str(text or "").split()).lower())
    return f'''(function(){{
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var want={want},found=[];
function norm(v){{return String(v||'').replace(/\\s+/g,' ').toLowerCase();}}
function leaves(v,out,depth){{if(out.length>2000||depth>12)return;if(typeof v==='string'){{out.push(v);return;}}
if(v&&typeof v==='object'){{for(var k in v){{if(Object.prototype.hasOwnProperty.call(v,k))leaves(v[k],out,depth+1);}}}}}}
function holds(raw){{if(!want||typeof raw!=='string'||raw.length>204800)return false;if(norm(raw).indexOf(want)>=0)return true;
var c=raw.charAt(0);if(c!=='{{'&&c!=='['&&c!=='"')return false;try{{var out=[];leaves(JSON.parse(raw),out,0);
return norm(out.join(' ')).indexOf(want)>=0;}}catch(e){{return false;}}}}
[['localStorage',function(){{return window.localStorage;}}],['sessionStorage',function(){{return window.sessionStorage;}}]].forEach(function(pair){{
try{{var st=pair[1]();if(!st)return;var n=Math.min(st.length,400);for(var i=0;i<n;i++){{var k=st.key(i);if(holds(st.getItem(k))){{found.push(pair[0]);return;}}}}}}catch(e){{}}}});
return __mcpB64({{ok:true,local_copy:found}});
}})()'''


def _text_present_js(text: str) -> str:
    """Whether the page shows text now, matched as an expect text wait matches it."""
    want = json.dumps(str(text or "").lower())
    return f'''(function(){{
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
return __mcpB64({{ok:true,present:!!(document.body&&String(document.body.innerText||'').toLowerCase().indexOf({want})>=0)}});
}})()'''


def _extract_fields_problem(fields: Any) -> Optional[str]:
    """Validate the shared MCP/REST extraction contract before opening a page."""
    if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
        return "fields must contain 1-20 field objects"
    names = set()
    for spec in fields:
        if not isinstance(spec, dict):
            return "each field must be an object with name and selector (or semantic)"
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or name in names:
            return "field names must be unique non-empty strings of at most 80 characters"
        names.add(name)
        targets = [spec.get("selector"), spec.get("semantic")]
        if sum(isinstance(v, str) and bool(v.strip()) for v in targets) != 1:
            return "each field needs exactly one non-empty selector or semantic target"
        if any(v is not None and (not isinstance(v, str) or len(v) > 1000) for v in targets):
            return "selector/semantic must be strings of at most 1000 characters"
        for key, maximum in (("offset", 1_000_000), ("text_offset", 10_000_000), ("max_items", 100)):
            if key in spec and (type(spec[key]) is not int or not (1 if key == "max_items" else 0) <= spec[key] <= maximum):
                return f"{key} is outside its integer bounds"
        if "all" in spec and not isinstance(spec["all"], bool):
            return "all must be true or false"
        if "attr" in spec and (not isinstance(spec["attr"], str) or not spec["attr"].strip() or len(spec["attr"]) > 100):
            return "attr must be a non-empty string of at most 100 characters"
        if "regex" in spec and (not isinstance(spec["regex"], str) or len(spec["regex"]) > 1000):
            return "regex must be a string of at most 1000 characters"
        if "flags" in spec and (not isinstance(spec["flags"], str) or len(spec["flags"]) > 10):
            return "flags must be a string of at most 10 characters"
    return None


def _extract_action_js(fields: List[Dict[str, Any]], max_chars: int) -> str:
    specs = json.dumps(fields, ensure_ascii=False)
    aliases = json.dumps(_SEMANTIC_EXTRACT_ALIASES, ensure_ascii=False)
    budget = max(256, min(int(max_chars), 20_000))
    return _fill_js(_browser_js("extract.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__SPECS__": specs,
        "__ALIASES__": aliases,
        "__BUDGET__": str(budget),
    })

def _extract_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    fields = action.get("fields")
    problem = _extract_fields_problem(fields)
    if problem:
        return {"ok": False, "type": "extract", "error": "invalid_extract_fields", "detail": problem, "_js_calls": 0}
    out = _run_json_js(
        settings, browser, _extract_action_js(fields, int(action.get("max_chars", 4000))),
        window_index, tab_index, tab_handle,
    )
    out["_js_calls"] = 1
    return out


def _chrome_native_id(browser: str, tab_handle: Optional[str], target: Any = None) -> Optional[str]:
    if target is not None and getattr(target, "native_id", None):
        return str(target.native_id)
    try:
        _, _, row = browser_tabs.resolve_tab(browser, str(tab_handle or ""))
    except KeyError:
        return None
    return str(row.get("native_id") or "") or None


def _dialog_action(browser: str, action: Dict[str, Any], tab_handle: Optional[str]) -> Dict[str, Any]:
    """Answer an open native dialog with the caller's explicit accept/dismiss decision."""
    decision = str(action.get("decision") or "").strip().lower()
    base = {"type": "dialog", "decision": decision or None}
    if decision not in {"accept", "dismiss"}:
        return {**base, "ok": False, "error": "invalid_dialog_decision",
                "message": "dialog needs decision='accept' or 'dismiss'; nothing is answered by default."}
    if _norm_browser(browser) != "Google Chrome":
        return {**base, "ok": False, "error": "dialog_unsupported", "reason_code": "DIALOG_UNSUPPORTED_SAFARI",
                "message": "Safari dialogs cannot be answered in the background; ask the user to answer it in Safari."}
    native_id = _chrome_native_id(browser, tab_handle)
    if not native_id:
        return {**base, "ok": False, "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE", "observe_again": True}
    prompt = action.get("prompt_text")
    try:
        out = chrome_background_bridge.request_handle_dialog(
            native_id, decision == "accept", str(prompt) if isinstance(prompt, str) else None,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {**base, "ok": False, "error": str(detail.get("error") or "chrome_dialog_failed"),
                "message": str(detail.get("message") or ""), "observe_again": True}
    return {**base, "ok": True, "answered": True, "dialog": out.get("dialog"), "observe_again": True}


def _element_center_js(element_id: str, scroll_into_view: bool) -> str:
    eid = json.dumps(str(element_id or ""))
    scroll = "true" if scroll_into_view else "false"
    return f"""(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(), el=__mcpRecoverElement({eid},s);
if(!el)return __mcpB64({{ok:false,error:'stale_element',reason_code:'ELEMENT_DETACHED',observe_again:true}});
if({scroll}){{try{{el.scrollIntoView({{block:'center',inline:'center'}});}}catch(e){{}}}}
var r=el.getBoundingClientRect();
return __mcpB64({{ok:true,x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height,
  in_view:r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth}});
}})()"""


# #386: what an agent can do in a background Safari tab instead of a hover or a click at x,y.
_SAFARI_POINTER_RECOVERY = (
    "In Safari act on elements, not on points: controls that show on hover are listed by browser_observe with "
    "hover_revealed:true and are clicked by their element_id or query without a hover. Content drawn in a "
    "canvas or a cross-origin frame needs Chrome with the Mac MCP companion; the agent cannot bring a tab forward, "
    "the owner can with Show Tab."
)


def _point_check_js(observation_id: str, x: float, y: float) -> str:
    obs = json.dumps(str(observation_id or ""))
    return f"""(function(){{
{_browser_state_bootstrap()}
function __mcpB64(o){{return btoa(unescape(encodeURIComponent(JSON.stringify(o))));}}
var s=__mcpState(),m=(s.observationMeta||{{}})[{obs}],x={float(x)},y={float(y)};
if(!m||!m.view)return __mcpB64({{ok:false,error:'observation_required',observe_again:true,
  message:'Coordinates must come from a browser_observe of this page (pass its observation_id).'}});
var v=m.view;
if(Math.abs(scrollX-v.sx)>1||Math.abs(scrollY-v.sy)>1||innerWidth!==v.w||innerHeight!==v.h)
  return __mcpB64({{ok:false,error:'stale_coordinates',reason_code:'VIEWPORT_CHANGED',observe_again:true,
    message:'The page scrolled or resized since that observation; observe again for fresh coordinates.'}});
if(x<0||y<0||x>=innerWidth||y>=innerHeight)return __mcpB64({{ok:false,error:'point_outside_viewport'}});
var el=document.elementFromPoint(x,y);
return __mcpB64({{ok:true,hit:el?{{tag:el.tagName.toLowerCase(),element_id:__mcpId(el,s)}}:null}});
}})()"""


def _is_point_action(action: Dict[str, Any]) -> bool:
    return (
        action.get("type") in {"click", "double_click", "hover"}
        and action.get("x") is not None and action.get("y") is not None
        and not action.get("element_id") and not _has_locator(action)
    )


def _point_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
    native_target: Any = None,
) -> Dict[str, Any]:
    """Trusted pointer input at viewport coordinates (canvas apps), bound to the observation they came from."""
    typ = str(action["type"])
    x, y = float(action["x"]), float(action["y"])
    base: Dict[str, Any] = {"type": typ, "x": x, "y": y}
    if current_frame_scope():
        return {**base, **{"ok": False, "type": typ, "error": "trusted_input_in_frame_unsupported", "reason_code": "TRUSTED_INPUT_IN_FRAME",
                "message": "Trusted pointer input uses top-level page coordinates; inside a frame use DOM actions (the default).",
                "_js_calls": 0}}
    if _norm_browser(browser) != "Google Chrome":
        return {**base, "ok": False, "error": "point_input_unsupported", "reason_code": "POINT_INPUT_UNSUPPORTED_SAFARI",
                "message": "Coordinate input needs Chrome with the Mac MCP companion; Safari has no background pointer.",
                "recovery": _SAFARI_POINTER_RECOVERY, "automatic_retry": False, "_js_calls": 0}
    obs = str(action.get("observation_id") or observation_id or "")
    check = _run_json_js(settings, browser, _point_check_js(obs, x, y), window_index, tab_index, tab_handle)
    if not check.get("ok"):
        return {**base, **check, "ok": False, "_js_calls": 1}
    native_id = _chrome_native_id(browser, tab_handle, native_target)
    if not native_id:
        return {**base, "ok": False, "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE",
                "observe_again": True, "_js_calls": 1}
    try:
        if typ == "hover":
            chrome_background_bridge.request_gesture(native_id, [{"type": "move", "x": x, "y": y,
                                                                   "delay_ms": int(action.get("hold_ms", 300))}])
        else:
            chrome_background_bridge.request_dispatch_mouse(
                native_id, x, y, click_count=2 if typ == "double_click" else 1, timeout_s=8.0,
            )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {**base, "ok": False, "error": str(detail.get("error") or "trusted_input_dispatch_failed"),
                "message": str(detail.get("message") or ""), "automatic_retry": False, "observe_again": True,
                "_js_calls": 1}
    return {**base, "ok": True, "input_trust": "trusted", "activation_mode": "trusted_chrome_cdp",
            "hit": check.get("hit"), "verification": "dispatched", "observe_again": True, "_js_calls": 1}


_CDP_MODIFIER_BITS = {"altKey": 1, "ctrlKey": 2, "metaKey": 4, "shiftKey": 8}


def _trusted_key_action(browser: str, action: Dict[str, Any], tab_handle: Optional[str], native_target: Any = None) -> Dict[str, Any]:
    """A real (trusted) key press through the Chrome companion, sent to the page's focused element."""
    label = str(action.get("key") or "")
    base = {"type": "key", "key": label, "input_mode": "trusted"}
    spec = _dom_key_spec(label)
    if spec is None:
        return {**base, "ok": False, "error": "unsupported_key", "reason_code": "UNSUPPORTED_KEY",
                "message": "Use a single character or a named key such as Enter, Escape, Tab, ArrowDown."}
    key_name, code, key_code = spec
    mask = 0
    for modifier in action.get("modifiers") or []:
        mapped = _DOM_KEY_MODIFIERS.get(str(modifier or "").strip().lower())
        if mapped:
            mask |= _CDP_MODIFIER_BITS[mapped]
    native_id = _chrome_native_id(browser, tab_handle, native_target)
    if not native_id:
        return {**base, "ok": False, "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE", "observe_again": True}
    printable = len(key_name) == 1 and not (mask & 0b0110)
    step = {"type": "key", "key": key_name, "code": code, "key_code": key_code, "modifiers": mask,
            "text": key_name if printable else ""}
    try:
        chrome_background_bridge.request_gesture(native_id, [step], feature="keys")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {**base, "ok": False, "error": str(detail.get("error") or "trusted_key_failed"),
                "message": str(detail.get("message") or ""), "automatic_retry": False}
    return {**base, "ok": True, "input_trust": "trusted", "activation_mode": "trusted_chrome_cdp",
            "verification": "dispatched", "observe_again": True}


_DRAG_STEPS = 10


def _range_drag_js(element_id: str, dx: float, to_element_id: Optional[str]) -> str:
    """Drag a native range slider by setting the value its thumb would reach, then firing input/change.

    A slider's thumb follows its value, so the result is the same as a pointer drag without needing
    trusted (debugger) input: it works in Safari and in a Chrome where the debugger cannot attach."""
    eid, to_id = json.dumps(str(element_id or "")), json.dumps(str(to_element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=__mcpRecoverElement({eid},s);
if(!el||String(el.tagName||'').toLowerCase()!=='input'||String(el.type||'').toLowerCase()!=='range')return __mcpB64({{ok:true,is_range:false}});
var r=el.getBoundingClientRect(),min=Number(el.min||0),max=Number(el.max===''?100:el.max),step=el.step==='any'?0:Number(el.step||1);
if(!(max>min)||r.width<1)return __mcpB64({{ok:false,is_range:true,error:'range_not_draggable'}});
var before=Number(el.value),thumb=r.left+(before-min)/(max-min)*r.width,target=thumb+{float(dx)};
var to=__mcpRecoverElement({to_id},s);if(to){{var tr=to.getBoundingClientRect();target=tr.left+tr.width/2;}}
var ratio=Math.max(0,Math.min(1,(target-r.left)/r.width)),value=min+ratio*(max-min);
if(step>0)value=min+Math.round((value-min)/step)*step;value=Math.max(min,Math.min(max,value));
var setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;setter.call(el,String(value));
el.dispatchEvent(new Event('input',{{bubbles:true}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));
__mcpVisual('Dragging',el,'',1600);
return __mcpB64({{ok:true,is_range:true,before:String(before),after:el.value}});
}})()'''


# Chrome refuses chrome.debugger.attach when another extension's page is involved, or another
# debugger holds the tab. Trusted input cannot work then; say why instead of a bare failure.
_DEBUGGER_BLOCKED_MARKERS = (
    "Cannot access a chrome-extension:// URL of different extension",
    "Detached while handling command",
    "Another debugger is already attached",
)


def _gesture_action(
    settings: Settings,
    browser: str,
    typ: str,
    work_action: Dict[str, Any],
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
    *,
    resolve_target: Callable[[Dict[str, Any]], Tuple[Dict[str, Any], Any]],
    native_target: Any = None,
) -> Dict[str, Any]:
    """Trusted pointer gestures (Chrome): hover moves the real pointer; drag presses, moves and releases."""
    base: Dict[str, Any] = {"type": typ, "element_id": work_action.get("element_id")}
    if typ == "drag" and work_action.get("element_id") and action.get("to_query") is None and action.get("to_role") is None:
        ranged = _run_json_js(settings, browser, _range_drag_js(
            str(work_action["element_id"]), float(action.get("dx") or 0), action.get("to_element_id"),
        ), window_index, tab_index, tab_handle)
        if ranged.get("is_range"):
            if not ranged.get("ok"):
                return {**base, "ok": False, "error": str(ranged.get("error") or "range_not_draggable"), "_js_calls": 1}
            return {**base, "ok": ranged.get("after") != ranged.get("before"), "activation_mode": "dom_range_value",
                    "input_trust": "untrusted", "value_before": ranged.get("before"), "value": ranged.get("after"),
                    "steps_done": 1, "verification": "value_changed" if ranged.get("after") != ranged.get("before") else "value_unchanged",
                    **({} if ranged.get("after") != ranged.get("before") else {"error": "action_no_effect"}), "_js_calls": 1}
    if current_frame_scope():
        return {**base, **{"ok": False, "type": typ, "error": "trusted_input_in_frame_unsupported", "reason_code": "TRUSTED_INPUT_IN_FRAME",
                "message": "Trusted pointer input uses top-level page coordinates; inside a frame use DOM actions (the default).",
                "_js_calls": 0}}
    if _norm_browser(browser) != "Google Chrome":
        return {**base, "ok": False, "error": "gesture_unsupported", "reason_code": "GESTURE_UNSUPPORTED_SAFARI",
                "message": "Safari has no background pointer; hover and drag need Chrome with the Mac MCP companion.",
                "recovery": _SAFARI_POINTER_RECOVERY, "automatic_retry": False, "_js_calls": 0}
    js_calls = 0

    def center(element_id: str, scroll: bool) -> Dict[str, Any]:
        nonlocal js_calls
        js_calls += 1
        return _run_json_js(settings, browser, _element_center_js(element_id, scroll), window_index, tab_index, tab_handle)

    if not work_action.get("element_id"):
        return {**base, "ok": False, "error": "target_required", "message": f"{typ} needs element_id or query/role.",
                "_js_calls": 0}
    source = center(str(work_action["element_id"]), typ == "hover")
    if not source.get("ok") or not source.get("in_view"):
        return {**base, "ok": False, "error": source.get("error") or "endpoint_offscreen", "observe_again": True,
                "message": "The source is not visible in the viewport; scroll it into view first.", "_js_calls": js_calls}
    sx, sy = float(source["x"]), float(source["y"])
    hold_ms = max(0, min(int(action.get("hold_ms", 300)), 3000))
    if typ == "hover":
        steps = [{"type": "move", "x": sx, "y": sy, "delay_ms": hold_ms}]
        destination = None
    else:
        to_id = action.get("to_element_id")
        if not to_id and (action.get("to_query") or action.get("to_role")):
            found, resolved = resolve_target({"type": "drag", "query": action.get("to_query") or "",
                                              "role": action.get("to_role"), "within": action.get("to_within")})
            if isinstance(resolved, dict) and resolved.get("ok") is False:
                return {**base, **resolved, "type": typ, "endpoint": "destination", "_js_calls": js_calls}
            to_id = found.get("element_id")
        if to_id:
            dest = center(str(to_id), False)
            if not dest.get("ok") or not dest.get("in_view"):
                return {**base, "ok": False, "error": dest.get("error") or "endpoint_offscreen", "observe_again": True,
                        "endpoint": "destination", "message": "The drop target is not visible in the viewport.",
                        "_js_calls": js_calls}
            dx_, dy_ = float(dest["x"]), float(dest["y"])
        elif action.get("dx") is not None or action.get("dy") is not None:
            dx_, dy_ = sx + float(action.get("dx") or 0), sy + float(action.get("dy") or 0)
        else:
            return {**base, "ok": False, "error": "destination_required",
                    "message": "drag needs to_element_id, to_query/to_role or dx/dy.", "_js_calls": js_calls}
        dx_, dy_ = max(0.0, dx_), max(0.0, dy_)
        steps = [{"type": "move", "x": sx, "y": sy, "delay_ms": 60}, {"type": "down", "x": sx, "y": sy, "delay_ms": 120}]
        for index in range(1, _DRAG_STEPS + 1):
            ratio = index / _DRAG_STEPS
            steps.append({"type": "move", "x": sx + (dx_ - sx) * ratio, "y": sy + (dy_ - sy) * ratio, "delay_ms": 25})
        steps += [{"type": "move", "x": dx_, "y": dy_, "delay_ms": 120}, {"type": "up", "x": dx_, "y": dy_}]
        destination = {"x": round(dx_, 1), "y": round(dy_, 1)}
    native_id = _chrome_native_id(browser, tab_handle, native_target)
    if not native_id:
        return {**base, "ok": False, "error": "stale_tab_handle", "reason_code": "STALE_TAB_HANDLE",
                "observe_again": True, "_js_calls": js_calls}
    try:
        out = chrome_background_bridge.request_gesture(native_id, steps)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        message = str(detail.get("message") or "")
        if not detail.get("steps_done") and any(marker in message for marker in _DEBUGGER_BLOCKED_MARKERS):
            return {**base, "ok": False, "error": "chrome_debugger_blocked", "reason_code": "CHROME_DEBUGGER_BLOCKED",
                    "message": "Chrome would not let the Mac MCP companion attach its debugger to this tab "
                               f"({message}). Another extension with debugger access (or an open DevTools) usually "
                               "causes this: turn that extension off or close DevTools, then retry. Nothing was done.",
                    "steps_done": 0, "automatic_retry": False, "observe_again": False, "_js_calls": js_calls}
        return {**base, "ok": False, "error": str(detail.get("error") or "chrome_gesture_failed"),
                "message": message, "steps_done": detail.get("steps_done", 0),
                "automatic_retry": False, "observe_again": True, "_js_calls": js_calls}
    result = {**base, "ok": True, "input_trust": "trusted", "activation_mode": "trusted_chrome_cdp",
              "from": {"x": round(sx, 1), "y": round(sy, 1)}, "steps_done": out.get("steps_done"),
              "verification": "dispatched", "observe_again": True, "_js_calls": js_calls}
    if destination is not None:
        result["to"] = destination
    return result


_SCAN_MAX_ITEMS = 500
_SCAN_MAX_STEPS = 50
_SCAN_PAYLOAD_BYTES = 48_000


def _scan_step_js(container_id: str, item_selector: str, key_field: str, options: Optional[Dict[str, Any]] = None) -> str:
    """Read the items rendered in a scroll container, then scroll it by most of one screen."""
    cid, sel, field = json.dumps(container_id), json.dumps(item_selector), json.dumps(key_field)
    return _fill_js(_browser_js("scan_step.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__CONTAINER_ID__": cid,
        "__ITEM_SELECTOR__": sel,
        "__KEY_FIELD__": field,
        "__SCAN_OPTIONS__": json.dumps(options or {}, ensure_ascii=False),
    })


def _scan_action(
    settings: Settings, browser: str, action: Dict[str, Any], window_index: int,
    tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """Drain rendered rows before scrolling; resume bounded calls in the same document.

    The page holds a short-lived dedup/partial-text cursor. No pending rendered
    row is scrolled away when an item or output limit is reached.
    """
    max_items = max(1, min(int(action.get("max_items", 100)), _SCAN_MAX_ITEMS))
    max_steps = max(1, min(int(action.get("max_steps", 15)), _SCAN_MAX_STEPS))
    max_chars = max(256, min(int(action.get("max_chars", 20_000)), 20_000))
    # A caller that returns less than a whole scan (browser_do) caps what is read, so the
    # cursor never moves past records that would be cut from its answer.
    max_bytes = max(256, min(int(action.get("max_bytes", _SCAN_PAYLOAD_BYTES)), _SCAN_PAYLOAD_BYTES))
    timeout_s = max(1.0, min(float(action.get("timeout_s", 20)), 60.0))
    settle_s = max(0.1, min(float(action.get("settle_ms", 400)) / 1000.0, 3.0))
    key_field = "href" if str(action.get("key_field") or "").lower() == "href" else "text"
    selector = str(action.get("item_selector") or "")
    if len(selector) > 1000:
        return {"ok": False, "type": "scan", "error": "invalid_item_selector", "complete": False, "_js_calls": 0}
    cursor = action.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"scan_[a-f0-9]{32}", cursor)):
        return {"ok": False, "type": "scan", "error": "invalid_scan_cursor", "complete": False, "_js_calls": 0}
    new_cursor = "scan_" + uuid.uuid4().hex
    started = time.perf_counter()
    seen: Dict[str, Dict[str, Any]] = {}
    stalls = steps = used = chars = 0
    container: Dict[str, Any] = {}
    alternatives: List[Dict[str, Any]] = []
    stopped = ""
    total_seen = 0
    while not stopped:
        cancellation_checkpoint()
        container_id = str(action.get("element_id") or container.get("element_id") or "")
        step = _run_json_js(
            settings, browser, _scan_step_js(container_id, selector, key_field, {
                "cursor": cursor, "new_cursor": new_cursor,
                "max_items": max_items - len(seen), "max_chars": max_chars - chars,
                "max_bytes": max_bytes - used,
            }), window_index, tab_index, tab_handle,
        )
        steps += 1
        if steps == 1 and step.get("other_containers"):
            alternatives = step["other_containers"]
        if not step.get("ok"):
            return {"type": "scan", **step, "items": list(seen.values()), "complete": False,
                    "steps": steps, "next_cursor": cursor, "_js_calls": steps}
        cursor = step.get("next_cursor") or cursor
        total_seen = int(step.get("total_seen", total_seen))
        previous_top = container.get("scroll_top")
        container = step.get("container") or container
        moved = previous_top is not None and container.get("scroll_top") != previous_top
        new = 0
        for item in step.get("items") or []:
            key = str(item.get("key") or "")
            if not key or key in seen:
                continue
            record = {k: item[k] for k in ("text", "href", "record_id", "text_offset", "text_complete") if k in item}
            record.setdefault("text", "")
            used += len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            chars += len(record["text"])
            seen[key] = record
            new += 1
        stalls = 0 if new or moved else stalls + 1
        if step.get("stopped"):
            stopped = step["stopped"]
        elif len(seen) >= max_items:
            stopped = "max_items"
        elif used >= max_bytes or chars >= max_chars:
            stopped = "payload_budget"
        elif stalls >= 2:
            stopped = ("no_usable_candidates" if not total_seen and not seen else
                       "end_of_list" if step.get("at_end") else "no_new_items")
        elif steps >= max_steps:
            stopped = "max_steps"
        elif time.perf_counter() - started >= timeout_s:
            stopped = "timeout"
        else:
            cancellable_sleep(settle_s)
    complete = stopped == "end_of_list"
    return {
        "ok": True, "type": "scan", "items": list(seen.values()), "count": len(seen),
        "stopped": stopped, "complete": complete, "has_more": not complete,
        "next_cursor": None if complete else cursor, "total_seen": total_seen,
        "steps": steps, "key_field": key_field, "container": container,
        "other_containers": alternatives, "coverage": "observed_rendered_rows",
        "duration_ms": int((time.perf_counter() - started) * 1000), "_js_calls": steps,
    }


_READ_DEFAULT_CHARS = 40_000
_READ_MAX_CHARS = 100_000


def _read_scroll_js(options: Dict[str, Any]) -> str:
    return _fill_js(_browser_js("read_scroll.js"), {"__READ_OPTIONS__": json.dumps(options)})


def _read_text_js(options: Dict[str, Any]) -> str:
    return _fill_js(_browser_js("read_text.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__READ_OPTIONS__": json.dumps(options),
    })


def _read_action(
    settings: Settings, browser: str, action: Dict[str, Any], window_index: int,
    tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """The whole page's readable text in one call: first move through the page so content
    that loads on scroll is there (load_more, a few seconds at most), then read it once.
    A longer page comes in parts: pass next_offset back as offset."""
    max_chars = max(1_000, min(int(action.get("max_chars") or _READ_DEFAULT_CHARS), _READ_MAX_CHARS))
    offset = max(0, int(action.get("offset") or 0))
    timeout_s = max(0.5, min(float(action.get("timeout_s", 12)), 30.0))
    js_calls = 0
    loaded: Optional[Dict[str, Any]] = None
    # A continuation reads the page as it already is.
    if action.get("load_more", True) is not False and offset == 0:
        started = time.perf_counter()
        first = _run_json_js(settings, browser, _read_scroll_js({"phase": "start"}), window_index, tab_index, tab_handle)
        js_calls += 1
        origin, height, steps, settled, stopped = first.get("top", 0), int(first.get("height") or 0), 0, 0, "end_of_page"
        waited, still_loading = 0, int(first.get("busy_total", first.get("busy")) or 0)
        quiet = 0  # steps in a row where the page neither grew nor showed a loading placeholder
        while True:
            cancellation_checkpoint()
            if time.perf_counter() - started >= timeout_s:
                stopped = "time_budget"
                break
            # One screen per step while anything may still load, so a section that loads when it comes
            # into view is never jumped over; across a page where nothing loads, up to four screens.
            factor = 0.95 if still_loading else min(4.0, 0.95 * 2 ** (quiet // 2))
            step = _run_json_js(settings, browser, _read_scroll_js({"phase": "step", "factor": factor}),
                                window_index, tab_index, tab_handle)
            js_calls += 1
            steps += 1
            if not step.get("ok"):
                stopped = "scroll_failed"
                break
            # A skeleton on screen is content still arriving: stay until it is replaced (4 s at most).
            waiting_since = time.perf_counter()
            while (step.get("busy") and time.perf_counter() - waiting_since < 4.0
                   and time.perf_counter() - started < timeout_s):
                cancellable_sleep(0.25)
                step = _run_json_js(settings, browser, _read_scroll_js({"phase": "check"}),
                                    window_index, tab_index, tab_handle)
                js_calls += 1
                waited += 1
            still_loading = int(step.get("busy_total", step.get("busy")) or 0)
            grew = int(step.get("height") or 0) > height
            height = max(height, int(step.get("height") or 0))
            quiet = 0 if grew or still_loading else quiet + 1
            if step.get("at_end"):
                # At the bottom: wait for a loader; done once the page stops growing.
                settled = 0 if grew else settled + 1
                if settled >= 2:
                    break
                cancellable_sleep(0.4)
        _run_json_js(settings, browser, _read_scroll_js({"phase": "restore", "top": origin}),
                     window_index, tab_index, tab_handle)
        js_calls += 1
        # still_loading > 0: placeholders were left on the page, so part of it may not be in the text.
        loaded = {"steps": steps, "height": height, "stopped": stopped, "waits": waited,
                  "still_loading": still_loading, "duration_ms": int((time.perf_counter() - started) * 1000)}
    out = _run_json_js(settings, browser, _read_text_js({"offset": offset, "max_chars": max_chars}),
                       window_index, tab_index, tab_handle)
    js_calls += 1
    out.setdefault("type", "read")
    if loaded is not None:
        out["loaded"] = loaded
    out["_js_calls"] = js_calls
    return out


def _network_js(options: Dict[str, Any]) -> str:
    return _fill_js(_browser_js("network.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__NETWORK_OPTIONS__": json.dumps(options),
    })


def _network_action(
    settings: Settings, browser: str, action: Dict[str, Any], window_index: int,
    tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """{type:'network'}: this tab's recent requests, newest last, from a ring the page keeps
    (100 at most). URLs come back with query secrets redacted; bodies and headers never.
    Pass next_since back as cursor for only what came after."""
    cursor = str(action.get("cursor") or "0")
    options = {
        "since": int(cursor) if cursor.isdigit() else 0,
        "url_contains": str(action.get("url_contains") or "")[:200],
        "max_items": action.get("max_items") or 50,
    }
    out = _run_json_js(settings, browser, _network_js(options), window_index, tab_index, tab_handle)
    requests = []
    for row in out.get("requests") or []:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ("seq", "kind", "method", "status", "start_ms", "duration_ms", "bytes", "error")
                if row.get(key) is not None}
        item["url"] = redact_sensitive_text(str(row.get("url") or ""))
        if row.get("kind") in {"fetch", "xhr"} and row.get("duration_ms") is None and not row.get("error"):
            item["pending"] = True
        requests.append(item)
    out["requests"] = requests
    out["url"] = redact_sensitive_text(str(out.get("url") or ""))
    out["type"] = "network"
    out["_js_calls"] = 1
    return out


def _pdf_action(
    settings: Settings, browser: str, window_index: int, tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """{type:'pdf'}: save the tab as a PDF artifact in the background (browser_pdf)."""
    from . import browser_pdf

    def run_snapshot(options: Dict[str, Any]) -> Dict[str, Any]:
        return _run_json_js(settings, browser, _fill_js(_browser_js("page_snapshot.js"), {
            "__BOOTSTRAP__": _browser_state_bootstrap(),
            "__SNAPSHOT_OPTIONS__": json.dumps(options),
        }), window_index, tab_index, tab_handle)

    if (
        browser == "Google Chrome" and not current_frame_scope()
        and chrome_background_bridge.is_connected() and chrome_background_bridge.supports("print_pdf")
    ):
        native_id = _chrome_native_id(browser, tab_handle)
        if native_id:
            page = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index, tab_handle)
            try:
                out = browser_pdf.chrome_print_pdf(native_id, page)
            except HTTPException as exc:
                # Chrome could not print this tab (too large, debugger refused): the page copy still can.
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                out = browser_pdf.snapshot_pdf(run_snapshot)
                out["chrome_print_error"] = str(detail.get("error") or "chrome_print_failed")
                out["_js_calls"] = int(out.get("_js_calls") or 0) + 1
                return out
            out["_js_calls"] = 1
            return out
    return browser_pdf.snapshot_pdf(run_snapshot)


# {type:'fill', values:{label: value}}: a whole form in one action. Each label is resolved
# like a query; text boxes are typed, selects (native and custom) pick the option, and
# checkboxes/radios are set from a true/false value. Fields are filled in the given order,
# so one revealed by an earlier choice (country -> state) is awaited briefly.
_FILL_MAX_FIELDS = 30
_FILL_TEMPLATE_MAX_FIELDS = 15
_FILL_SKIP_INPUT_TYPES = frozenset({"search", "password", "hidden", "submit", "button", "reset", "file", "image"})
_FILL_TRUE = frozenset({"true", "yes", "on", "1", "checked", "evet", "x"})


def _fill_values_problem(values: Any) -> Optional[str]:
    if not isinstance(values, dict) or not values:
        return "needs values: an object of field label -> value, e.g. {'Email': 'a@b.c', 'Country': 'Turkey'}."
    if len(values) > _FILL_MAX_FIELDS:
        return f"may fill at most {_FILL_MAX_FIELDS} fields; split the form."
    for label, value in values.items():
        if not str(label or "").strip():
            return "has an empty field label."
        if not isinstance(value, (str, int, float, bool)):
            return f"value for {str(label)[:60]!r} must be text, a number or true/false."
    return None


def _fill_kind(target: Dict[str, Any]) -> str:
    tag = str(target.get("tag") or "").lower()
    role = str(target.get("role") or "").lower()
    input_type = str(target.get("input_type") or "").lower()
    if role in {"checkbox", "switch"} or input_type == "checkbox":
        return "checkbox"
    if role == "radio" or input_type == "radio":
        return "radio"
    if tag == "select" or (role in {"combobox", "listbox"} and tag not in {"input", "textarea"}):
        return "select"
    return "text"


def _fill_is_control(element: Dict[str, Any]) -> bool:
    tag = str(element.get("tag") or "").lower()
    role = str(element.get("role") or "").lower()
    return (
        _fill_kind(element) != "text" or tag in {"input", "textarea"}
        or role in {"textbox", "searchbox", "combobox", "spinbutton"}
    ) and str(element.get("input_type") or "").lower() not in _FILL_SKIP_INPUT_TYPES - {"search"}


def _pick_fill_control(matches: List[Dict[str, Any]], label: str) -> Optional[Dict[str, Any]]:
    """The form control a label names. Only the control's own label counts (its <label>, aria
    label, placeholder or name), never option text: "State" must not pick a Country select
    that lists "United States". A matched <label> or caption stands for its control."""
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for match in matches:
        if _fill_is_control(match):
            target = match
            names = [match.get(key) for key in ("association_text", "aria_label", "placeholder", "name", "title")]
            if _fill_kind(match) in {"checkbox", "radio"}:
                names.append(match.get("text"))
        elif isinstance(match.get("associated_control"), dict) and match["associated_control"].get("element_id"):
            control = match["associated_control"]
            direct = next((item for item in matches if item.get("element_id") == control.get("element_id")), None)
            target = direct or {**control, "confidence": match.get("confidence")}
            names = [match.get("text"), match.get("aria_label")]
        else:
            continue
        level = max([_match_level(name, label) for name in names if name] or [0])
        if level >= 3 and (best is None or level > best[0]):
            best = (level, target)
    return best[1] if best else None


def _fill_occluded(result: Dict[str, Any]) -> bool:
    readiness = result.get("readiness") if isinstance(result.get("readiness"), dict) else {}
    return "ELEMENT_OCCLUDED" in {str(result.get("reason_code") or ""), str(readiness.get("reason_code") or "")}


def _fill_truthy(value: Any) -> bool:
    return value is True or (not isinstance(value, bool) and str(value).strip().lower() in _FILL_TRUE)


def _fill_template(elements: Any) -> Optional[Dict[str, Any]]:
    """A ready {type:'fill'} action for the form fields an observation shows, so the next call
    fills them all at once instead of one field per call."""
    values: Dict[str, Any] = {}
    for element in elements or []:
        if not isinstance(element, dict) or not element.get("actionable") or element.get("enabled") is False:
            continue
        tag = str(element.get("tag") or "").lower()
        role = str(element.get("role") or "").lower()
        input_type = str(element.get("input_type") or "").lower()
        if input_type in _FILL_SKIP_INPUT_TYPES:
            continue
        kind = _fill_kind(element)
        if kind == "text" and not (role in {"textbox", "searchbox", "combobox"} or tag in {"input", "textarea"}):
            continue
        label = " ".join(str(
            element.get("association_text") or element.get("aria_label") or element.get("placeholder")
            or element.get("name") or ""
        ).split())[:60]
        if not label or label in values or set(_normalize_text(label).split()) & _POST_SEARCH_WORDS:
            continue
        if kind == "text" and str(element.get("value") or "").strip():
            continue
        values[label] = False if kind in {"checkbox", "radio"} else ""
        if len(values) >= _FILL_TEMPLATE_MAX_FIELDS:
            break
    if len(values) < 2:
        return None
    return {"type": "fill", "values": values}


# {type:'post'}: type into a comment/reply box, press that box's own submit button and
# confirm the text shows up on the page, in one action. Agents used to spend 8-10 calls
# on this (observe, scroll, find the box, type, observe, click, find, observe).
def _post_words(*words: str) -> frozenset:
    return frozenset(_normalize_text(word) for word in words)


_POST_SEARCH_WORDS = _post_words("search", "ara", "arama", "find", "filter")
_POST_FIELD_WORDS = _post_words(
    "comment", "comments", "reply", "replies", "conversation", "discussion", "thoughts", "write", "message",
    "post", "respond", "response", "answer", "yorum", "yorumla", "yanıt", "yanıtla", "cevap", "cevapla",
    "mesaj", "düşünce", "yaz",
)
# A box's own submit button. Reply/Yanıtla rank below the others: a thread is full of
# Reply buttons that open other boxes, and the nearest submit button wins anyway.
_POST_SUBMIT_WORDS = _post_words(
    "post", "comment", "submit", "send", "publish", "save", "tweet", "gönder", "kaydet", "yayınla", "paylaş",
)
_POST_SUBMIT_SECONDARY_WORDS = _post_words("reply", "respond", "yanıtla", "cevapla", "yorum")
_POST_CANCEL_WORDS = _post_words("cancel", "discard", "close", "delete", "remove", "iptal", "vazgeç", "kapat", "sil")
_POST_SUBMIT_LEVELS = 14
_POST_VERIFY_S = 10.0
_POST_SNIPPET_WORDS = 8


def _post_label(element: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("aria_label", "placeholder", "name", "title", "text"):
        value = str(element.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts)[:160]


def _post_boxes(matches: List[Dict[str, Any]]) -> List[Tuple[bool, Dict[str, Any]]]:
    """Textbox matches that can be a comment box (never a search field), each with whether its label names one."""
    boxes = []
    for match in matches:
        tokens = set(_normalize_text(_post_label(match)).split())
        if str(match.get("input_type") or "").lower() == "search" or tokens & _POST_SEARCH_WORDS:
            continue
        boxes.append((bool(tokens & _POST_FIELD_WORDS), match))
    return boxes


def _post_reply_opener(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The nearest Reply-style button of an item (matches come nearest first)."""
    for match in matches:
        tokens = _normalize_text(_post_label(match)).split()
        if tokens and len(tokens) <= 4 and set(tokens) & _POST_SUBMIT_SECONDARY_WORDS and not set(tokens) & _POST_CANCEL_WORDS:
            return match
    return None


def _pick_post_field(matches: List[Dict[str, Any]], *, scoped: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    """Choose the comment box among textbox matches: never a search field, and never a guess.

    Returns (match, "ok"), (None, "not_found") or (None, "ambiguous").
    """
    boxes = _post_boxes(matches)
    if not boxes:
        return None, "not_found"
    if scoped or len(boxes) == 1:
        # Inside one item the nearest box is the answer (matches come nearest first).
        return boxes[0][1], "ok"
    named = [match for is_named, match in boxes if is_named]
    if len(named) == 1:
        return named[0], "ok"
    return None, "ambiguous"


def _post_snippet(text: str) -> str:
    """The first words of the text as a page shows them (markdown link targets and URLs dropped)."""
    cleaned = re.sub(r"\]\([^)]*\)", " ", str(text or ""))
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    return " ".join(_normalize_text(cleaned).split()[:_POST_SNIPPET_WORDS])


_POST_PAGE_JS_COMMON = r"""
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
function norm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\s+/g,' ').trim();}
function cparent(n){return n.parentNode||(n.host||null);}
function shown(el){try{var st=__mcpStyle(el);if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0)return false;var r=__mcpTopRect(el);return r.width>=1&&r.height>=1;}catch(e){return false;}}
"""


def _post_submit_js(field_id: str, max_levels: int) -> str:
    """Find the submit button that belongs to the box: walk up from the box one ancestor at a
    time and stop at the first ancestor holding a submit-named button (open shadow roots included)."""
    words = {
        "primary": sorted(_POST_SUBMIT_WORDS), "secondary": sorted(_POST_SUBMIT_SECONDARY_WORDS),
        "cancel": sorted(_POST_CANCEL_WORDS),
    }
    return f'''(function(){{
{_browser_state_bootstrap()}
{_POST_PAGE_JS_COMMON}
var s=__mcpState(),field=__mcpRecoverElement({json.dumps(str(field_id or ""))},s),words={json.dumps(words)},maxLevels={int(max_levels)};
if(!field)return __mcpB64({{ok:false,error:'post_field_detached',reason_code:'POST_FIELD_DETACHED'}});
var primary=new Set(words.primary),secondary=new Set(words.secondary),cancel=new Set(words.cancel);
function buttons(root){{var out=[],stack=[root];while(stack.length&&out.length<400){{var node=stack.pop(),list=[];
  try{{list=node.querySelectorAll('button,[role=button],input[type=submit],input[type=button]');}}catch(e){{}}
  for(var i=0;i<list.length;i++)out.push(list[i]);
  if(node.shadowRoot)stack.push(node.shadowRoot);
  var kids=[];try{{kids=node.querySelectorAll('*');}}catch(e){{}}for(var k=0;k<kids.length;k++)if(kids[k].shadowRoot)stack.push(kids[k].shadowRoot);}}
  return out;}}
function info(el){{var tag=String(el.tagName||'').toLowerCase(),raw=[el.getAttribute('aria-label'),el.getAttribute('title'),tag==='input'?el.value:'',(el.innerText||el.textContent||'')].filter(Boolean).join(' ');
  var label=norm(raw).slice(0,80),toks=label?label.split(' '):[],short=toks.length&&toks.length<=4,tier=0;
  if(toks.some(function(t){{return cancel.has(t);}}))tier=-1;
  else if(short&&toks.some(function(t){{return primary.has(t);}}))tier=1;
  else if(short&&toks.some(function(t){{return secondary.has(t);}}))tier=2;
  return {{el:el,label:label,tier:tier,submit:String(el.type||'').toLowerCase()==='submit',disabled:!!el.disabled||String(el.getAttribute('aria-disabled')||'')==='true'}};}}
var cur=field,level=0,nearby=null;
while(level<maxLevels){{
  cur=cur&&cparent(cur);if(!cur||cur.nodeType===9)break;if(cur.nodeType!==1)continue;level++;
  var seen=new Set(),found=buttons(cur).filter(function(b){{if(seen.has(b)||b===field)return false;seen.add(b);return shown(b);}}).map(info);
  if(!found.length)continue;
  if(!nearby)nearby=found.slice(0,8).map(function(x){{return x.label;}}).filter(Boolean);
  var hits=found.filter(function(x){{return x.tier>0;}});
  if(!hits.length)continue;
  hits.sort(function(a,b){{return (a.tier-b.tier)||((b.submit?1:0)-(a.submit?1:0));}});
  var best=hits[0],tied=hits.filter(function(x){{return x.tier===best.tier&&x.submit===best.submit&&x.el!==best.el;}});
  if(tied.length)return __mcpB64({{ok:true,ambiguous:true,level:level,buttons:[best].concat(tied).slice(0,6).map(function(x){{return x.label;}})}});
  var id=__mcpId(best.el,s);__mcpRememberTarget(best.el,id,s);
  return __mcpB64({{ok:true,element_id:id,label:best.label,disabled:best.disabled,level:level}});
}}
return __mcpB64({{ok:true,element_id:null,buttons:nearby||[]}});
}})()'''


def _post_check_js(field_id: str, snippet: str) -> str:
    """Count where the text appears on the page outside the box itself (open shadow roots included)."""
    return f'''(function(){{
{_browser_state_bootstrap()}
{_POST_PAGE_JS_COMMON}
var s=__mcpState(),field=s.elements[{json.dumps(str(field_id or ""))}]||null,want={json.dumps(snippet)};
var connected=!!(field&&field.isConnected),fieldText='';
if(connected){{try{{fieldText=(typeof field.value==='string')?field.value:String(field.innerText||field.textContent||'');}}catch(e){{}}}}
var parts=[],roots=__mcpRoots(),vis=new Map(),visited=0;
for(var r=0;r<roots.length&&visited<60000;r++){{
  var start=roots[r].nodeType===9?roots[r].body:roots[r];if(!start)continue;
  var tw;try{{tw=(roots[r].nodeType===9?roots[r]:document).createTreeWalker(start,NodeFilter.SHOW_TEXT);}}catch(e){{continue;}}
  var tn;while((tn=tw.nextNode())&&visited++<60000){{
    var pe=tn.parentElement;if(!pe||!tn.data||!tn.data.trim())continue;
    var tag=String(pe.tagName||'').toLowerCase();if(tag==='script'||tag==='style'||tag==='textarea'||tag==='noscript'||tag==='template')continue;
    if(connected&&(pe===field||__mcpComposedContains(field,pe)))continue;
    var v=vis.get(pe);if(v===undefined){{v=shown(pe);vis.set(pe,v);}}
    if(v)parts.push(tn.data);
  }}
}}
var hay=' '+norm(parts.join(' '))+' ',needle=' '+want+' ',count=0;
if(want){{var at=hay.indexOf(needle);while(at>=0&&count<50){{count++;at=hay.indexOf(needle,at+needle.length-1);}}}}
return __mcpB64({{ok:true,count:count,field_connected:connected,field_has_text:!!(want&&(' '+norm(fieldText)+' ').indexOf(needle)>=0),field_empty:connected&&!String(fieldText).trim(),url:location.href}});
}})()'''


def _wait_action_polling(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    initial_url: str,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    kind = str(action.get("for") or action.get("condition") or "selector").lower().strip()
    timeout_s = max(0.1, min(float(action.get("timeout_s", 10)), 60.0))
    poll_s = max(0.05, min(float(action.get("poll_ms", 125)) / 1000.0, 1.0))
    started = time.perf_counter()
    js_calls = 0
    if kind == "network_idle":
        stable_ms = max(150, min(int(action.get("stable_ms", 300)), 2000))
        last_signature = None
        stable_since = time.perf_counter()
        while time.perf_counter() - started < timeout_s:
            state = _run_json_js(
                settings, browser, _network_idle_state_js(), window_index, tab_index, tab_handle,
            )
            js_calls += 1
            if not state.get("matched"):
                last_signature = None
                stable_since = time.perf_counter()
                cancellable_sleep(poll_s)
                continue
            signature = state.get("content_signature")
            if signature != last_signature:
                last_signature = signature
                stable_since = time.perf_counter()
            elif (time.perf_counter() - stable_since) * 1000 >= stable_ms:
                return {
                    "ok": True, "type": "wait", "for": kind, "matched": True,
                    "settled_by": "content_stable", "duration_ms": int((time.perf_counter()-started)*1000),
                    "url": state.get("url"), "_compact_state": state, "_js_calls": js_calls,
                }
            cancellable_sleep(poll_s)
        return {
            "ok": True, "type": "wait", "for": kind, "matched": False, "timed_out": True,
            "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
        }

    if kind == "dom_stable":
        stable_ms = max(100, min(int(action.get("stable_ms", 500)), 5000))
        last_revision = None
        stable_since = time.perf_counter()
        while time.perf_counter() - started < timeout_s:
            state = _run_json_js(
                settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
            )
            js_calls += 1
            rev = state.get("dom_revision")
            if rev != last_revision:
                last_revision = rev
                stable_since = time.perf_counter()
            elif (time.perf_counter() - stable_since) * 1000 >= stable_ms:
                return {"ok": True, "type": "wait", "for": kind, "matched": True, "duration_ms": int((time.perf_counter()-started)*1000), "_compact_state": state, "_js_calls": js_calls}
            cancellable_sleep(poll_s)
        return {"ok": True, "type": "wait", "for": kind, "matched": False, "timed_out": True, "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls}

    while time.perf_counter() - started < timeout_s:
        try:
            state = _run_json_js(
                settings,
                browser,
                _condition_js(action, initial_url),
                window_index,
                tab_index,
                tab_handle,
            )
            js_calls += 1
        except HTTPException:
            if kind == "url_change":
                cancellable_sleep(poll_s)
                continue
            raise
        if state.get("matched"):
            return {"ok": True, "type": "wait", "for": kind, "matched": True, "duration_ms": int((time.perf_counter()-started)*1000), "url": state.get("url"), "_compact_state": state, "_js_calls": js_calls}
        cancellable_sleep(poll_s)
    return {"ok": True, "type": "wait", "for": kind, "matched": False, "timed_out": True, "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls}


def _wait_kind(action: Dict[str, Any]) -> str:
    return str(action.get("for") or action.get("condition") or "selector").lower().strip()


_BLANK_URLS = {"", "about:blank"}


def _public_tab(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("tab_handle", "url", "title", "window_index", "tab_index", "active")}


def _credit_opened_tab(browser: str, action_result: Dict[str, Any], tabs_before: List[str],
                       own_handle: Optional[str]) -> None:
    """A click whose effect is a new tab changes nothing on its own page; count the new tab as its effect."""
    try:
        rows = browser_tabs.list_tabs(browser)
    except Exception:
        return
    before = set(tabs_before)
    if not any(str(row.get("tab_handle") or "") not in before and row.get("tab_handle") != own_handle for row in rows):
        return
    for key in ("error", "reason_code", "observe_again", "automatic_retry"):
        action_result.pop(key, None)
    action_result.update(ok=True, effect_observed=True, effect="new_tab_opened",
                         verification="new_tab_opened")


def _wait_new_tab(browser: str, action: Dict[str, Any], own_handle: Optional[str]) -> Dict[str, Any]:
    """Wait for a tab the page opened (target=_blank, OAuth popup) and return its handle without activating it.

    New means a handle that was not in the snapshot taken before the batch ran.
    url_contains narrows the match; several remaining candidates are reported as
    ambiguous instead of guessing, and a tab that is still about:blank is given
    until the timeout to navigate.
    """
    before = set(action.get("_tabs_before") or ())
    timeout_s = max(0.1, min(float(action.get("timeout_s", 10)), 60.0))
    poll_s = max(0.1, min(float(action.get("poll_ms", 250)) / 1000.0, 2.0))
    needle = str(action.get("url_contains") or "").strip().lower()
    started = time.perf_counter()
    scans = 0
    pending_blank: Optional[Dict[str, Any]] = None

    def finish(**fields: Any) -> Dict[str, Any]:
        return {"ok": True, "type": "wait", "for": "new_tab", "tab_scans": scans,
                "duration_ms": int((time.perf_counter() - started) * 1000), **fields}

    while True:
        cancellation_checkpoint()
        rows = browser_tabs.list_tabs(browser)
        scans += 1
        fresh = [
            row for row in rows
            if str(row.get("tab_handle") or "") not in before and row.get("tab_handle") != own_handle
        ]
        if needle:
            fresh = [row for row in fresh if needle in str(row.get("url") or "").lower()]
        elapsed = time.perf_counter() - started
        if len(fresh) > 1:
            return finish(matched=False, error="ambiguous_new_tabs", reason_code="AMBIGUOUS_NEW_TABS",
                          candidates=[_public_tab(row) for row in fresh[:10]],
                          hint="Several tabs opened; pass url_contains to pick the one this action opened.")
        if len(fresh) == 1:
            row = fresh[0]
            if str(row.get("url") or "") not in _BLANK_URLS or elapsed >= timeout_s:
                return finish(matched=True, new_tab=_public_tab(row),
                              url_pending=str(row.get("url") or "") in _BLANK_URLS)
            pending_blank = row
        if elapsed >= timeout_s:
            if pending_blank is not None:
                return finish(matched=True, new_tab=_public_tab(pending_blank), url_pending=True)
            return finish(matched=False, timed_out=True, reason_code="NO_NEW_TAB",
                          hint="No new tab appeared: the page may have blocked the popup or navigated in place "
                               "(wait for=url_change instead).")
        cancellable_sleep(poll_s)


_MAX_PAUSE_S = 30.0
# Any of these names something to wait for; without one, a wait is a plain pause.
_WAIT_CONDITION_KEYS = ("for", "condition", "selector", "text", "query", "role", "element_id", "text_match", "url_contains")


def _wait_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    initial_url: str,
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    """Prefer in-page event wakeups; preserve bounded polling as a fallback."""
    if not any(action.get(key) not in (None, "") for key in _WAIT_CONDITION_KEYS):
        # {type:'wait', wait_s: 1.5} pauses; it used to wait 10 s for a selector nobody named.
        pause = action.get("wait_s", action.get("seconds", action.get("timeout_s", 1.0)))
        try:
            pause_s = max(0.0, min(float(pause), _MAX_PAUSE_S))
        except (TypeError, ValueError):
            pause_s = 1.0
        started = time.perf_counter()
        time.sleep(pause_s)
        return {"ok": True, "type": "wait", "for": "time", "matched": True,
                "duration_ms": int((time.perf_counter() - started) * 1000)}
    kind = _wait_kind(action)
    if kind == "new_tab":
        return _wait_new_tab(browser, action, tab_handle)
    if kind not in {"selector", "text", "semantic", "element_removed", "url_change", "dom_stable", "network_idle"}:
        return _wait_action_polling(
            settings, browser, action, window_index, tab_index, initial_url, tab_handle,
        )
    timeout_s = max(0.1, min(float(action.get("timeout_s", 10)), 60.0))
    started = time.perf_counter()
    try:
        if _norm_browser(browser) == "Google Chrome":
            result = _run_json_js(
                settings, browser, _event_wait_js(action, initial_url, timeout_s),
                window_index, tab_index, tab_handle,
            )
            return _event_wait_result(result, js_calls=1, started=started)

        installed = _run_json_js(
            settings, browser,
            _event_wait_js(action, initial_url, timeout_s, return_mode="token"),
            window_index, tab_index, tab_handle,
        )
        token = str(installed.get("wait_token") or "")
        if not token:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Safari event waiter did not return a wait token.",
            )
        js_calls = 1
        status_poll_s = max(
            0.25,
            min(float(action.get("event_status_poll_ms", 500)) / 1000.0, 1.0),
        )
        deadline = started + timeout_s + 0.25
        while time.perf_counter() < deadline:
            cancellation_checkpoint()
            if js_calls > 1:
                cancellable_sleep(
                    min(status_poll_s, max(0.0, deadline - time.perf_counter()))
                )
            result = _run_json_js(
                settings, browser, _event_wait_status_js(token),
                window_index, tab_index, tab_handle,
            )
            js_calls += 1
            if not result.get("pending"):
                return _event_wait_result(result, js_calls=js_calls, started=started)
        return _event_wait_result(
            {
                "ok": True, "type": "wait", "for": kind, "matched": False,
                "timed_out": True, "settled_by": "host_deadline",
            },
            js_calls=js_calls, started=started,
        )
    except (HTTPException, ValueError, KeyError) as exc:
        fallback = _wait_action_polling(
            settings, browser, action, window_index, tab_index, initial_url, tab_handle,
        )
        fallback["wait_strategy"] = "bounded_poll_fallback"
        fallback["event_wait_fallback_reason"] = type(exc).__name__
        fallback["telemetry"] = {
            "remote_js_calls": int(fallback.get("_js_calls") or 0),
            "event_count": 0,
            "fallback_polls": int(fallback.get("_js_calls") or 0),
            "duration_ms": int(fallback.get("duration_ms") or 0),
            "event_wait_failed": True,
        }
        return fallback


_TARGET_CANDIDATE_LIMIT = 8
_LATE_TARGET_WAIT_S = 2.5
_LATE_TARGET_MAX_WAIT_S = 10.0
# A search box (role=combobox, aria-autocomplete) may open its list a moment after typing; once a list is
# open, its options may still be loading. A list whose options stopped changing has nothing more coming.
_SUGGEST_APPEAR_S = 1.5
_SUGGEST_MAX_S = 4.0
_SUGGEST_STABLE_S = 0.8
_SUGGEST_INPUT_TYPES = frozenset({"", "text", "search"})
_FILL_ESCAPE_SETTLE_S = 0.2
_LIST_COVER_ROLES = frozenset({"option", "listbox"})
_SELECT_REOPEN_AFTER_S = 0.6
_MUTATING_RESULT_TYPES = {"click", "double_click", "type", "type_text", "paste", "select", "key", "post", "fill"}


def _late_target_wait_s(action: Dict[str, Any], results: List[Dict[str, Any]]) -> float:
    """How long to wait for a missing target: explicit wait_s, else a short wait after a mutation."""
    explicit = action.get("wait_s")
    if explicit is not None:
        try:
            return max(0.0, min(float(explicit), _LATE_TARGET_MAX_WAIT_S))
        except (TypeError, ValueError):
            return 0.0
    mutated = any(
        isinstance(item, dict) and item.get("ok") and str(item.get("type") or "") in _MUTATING_RESULT_TYPES
        for item in results
    )
    return _LATE_TARGET_WAIT_S if mutated else 0.0


_INTENT_HINT_LIMIT = 120


def _within_anchor_failure(found: Dict[str, Any], within: Optional[str]) -> Optional[Dict[str, Any]]:
    """The refusal for a within anchor that is missing or found in several items, else None."""
    scope = found.get("within") if isinstance(found.get("within"), dict) else None
    if scope is None or scope.get("status") not in {"anchor_not_found", "anchor_ambiguous"}:
        return None
    ambiguous = scope.get("status") == "anchor_ambiguous"
    return {
        "ok": False,
        "error": "within_anchor_ambiguous" if ambiguous else "within_anchor_not_found",
        "reason_code": "WITHIN_ANCHOR_AMBIGUOUS" if ambiguous else "WITHIN_ANCHOR_NOT_FOUND",
        "within": within, "anchor_count": scope.get("anchor_count"),
        "anchors": scope.get("anchors"), "observe_again": False, "automatic_retry": False,
        "hint": (
            "Use a longer phrase that appears only in the intended item, or pass within_element_id."
            if ambiguous else "The within text is not on the page; check the anchor text."
        ),
    }


def _action_intent_hint(action: Dict[str, Any]) -> str:
    """The agent's optional disambiguation note, safe to send to the Decisions API.

    Values the action types into the page never leave the machine through it,
    and secrets are redacted before it is capped.
    """
    raw = action.get("intent")
    if not isinstance(raw, str):
        return ""
    hint = " ".join(raw.split())
    for key in ("text", "value"):
        typed = str(action.get(key) or "").strip()
        if len(typed) >= 3:
            hint = re.sub(re.escape(typed), "[typed value]", hint, flags=re.IGNORECASE)
    return redact_sensitive_text(hint)[:_INTENT_HINT_LIMIT].strip()


def _decide_browser_target(
    action: Dict[str, Any],
    query: str,
    role: Optional[str],
    match_text: Optional[str],
    matches: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the deterministic best match unless an enabled Decision resolves a tie.

    Only already-ranked candidates are offered, so the result is always one of
    ``matches``. Lease, takeover, readiness and stale checks still run after this.
    """
    best = matches[0]
    assessment = decision_engine.assess_ambiguity([item.get("confidence") or 0.0 for item in matches])
    if not assessment["ambiguous"]:
        decision_engine.record_resolution("browser", ambiguous=False)
        return best
    plausible = [
        item for item in matches
        if float(item.get("confidence") or 0.0) >= decision_engine.AMBIGUITY_FLOOR
    ]
    by_candidate = {f"c{index}": item for index, item in enumerate(plausible, start=1)}
    candidates = [
        decision_engine.DecisionCandidate(
            candidate_id=candidate_id,
            label=str(
                item.get("text") or item.get("aria_label") or item.get("placeholder")
                or item.get("name") or item.get("title") or ""
            ),
            role=str(item.get("role") or ""),
            tag=str(item.get("tag") or ""),
            context=str(item.get("association_text") or item.get("context") or ""),
            risky=decision_engine.is_risky_label(
                item.get("text"), item.get("aria_label"), item.get("title"),
                item.get("name"), item.get("value"),
            ),
        )
        for candidate_id, item in by_candidate.items()
    ]
    action_type = str(action.get("type") or "").lower()
    intent = f"Browser {action_type} target. query: {query}; role: {role or ''}; text: {match_text or ''}"
    hint = _action_intent_hint(action)
    if hint:
        intent += f"; intent: {hint}"
    result = decision_engine.resolve_ambiguity(
        intent, candidates, surface="browser", deterministic_id="c1",
    )
    decision_engine.record_resolution("browser", ambiguous=True, result=result)
    if result.attempted:
        record_computer_use_sample("browser_decision", duration_ms=result.latency_ms)
    chosen = by_candidate.get(str(result.selected_id)) if result.accepted else None
    out = dict(chosen or best)
    out["decision"] = {"ambiguity": assessment, **result.metadata()}
    return out


_NON_MUTATING_ACT_TYPES = {"wait", "extract", "read", "network", "pdf"}
# Failures that change nothing on the page: the target, option or field is not there, or it
# was refused before anything was sent. With every earlier step verified, the outcome of the
# call is known (outcome_unknown=false), so a delegated agent is not locked out by it.
_KNOWN_FAILURE_ERRORS = frozenset({
    "target_not_found", "within_anchor_ambiguous", "within_anchor_not_found", "option_not_found",
    "element_not_ready", "stale_observation", "field_not_found", "invalid_input_mode",
    "trusted_input_not_supported_for_action", "post_field_not_found", "post_field_ambiguous",
    "post_field_outside_item", "post_submit_not_found", "post_submit_ambiguous", "chrome_debugger_blocked",
    "stale_target_ambiguous", "choice_not_kept", "value_not_kept", "option_ambiguous",
    "ambiguous_activation_target",
})
_LOCATOR_KEYS = ("query", "target", "role", "text_match", "target_text")


def _has_locator(action: Dict[str, Any]) -> bool:
    return any(action.get(key) for key in _LOCATOR_KEYS)


def _stale_target_locator(
    browser: str, tab_handle: Optional[str], action: Dict[str, Any], result: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """How to find a re-rendered target again, when nothing was done to the old one.

    The caller's own locator wins; otherwise the role and label observe/find saw for that
    element_id, turned into a locator the way computer_plan's recovery does.
    """
    # Another route is another record: the same "Delete" on /items/B is not the one observed on /items/A,
    # so a target from an earlier page is never looked for again; the agent observes the new page (#388).
    if result.get("reason_code") == "ROUTE_CHANGED":
        return None
    untouched = bool(result.get("no_side_effect")) or (
        result.get("reason_code") == "ELEMENT_DETACHED" and result.get("error") in {"element_not_ready", "stale_element"}
    )
    if not untouched or not action.get("element_id"):
        return None
    relocated = {key: value for key, value in action.items() if key != "element_id"}
    if _has_locator(action):
        return relocated, "caller_locator"
    identity = _remembered_target_identity(browser, tab_handle, action.get("element_id"))
    if not identity:
        return None
    from .computer_plan import _semantic_target_from_browser_node

    semantic = _semantic_target_from_browser_node(identity)
    if not semantic.get("query"):
        return None
    relocated.update(query=semantic["query"], role=semantic.get("role"), text_match=semantic.get("text"))
    return relocated, ("observed_identity" if identity.get("unique") else "observed_identity_not_unique")


def _stale_rebind_refusal(action: Dict[str, Any], via: str, found: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Refuse a rebind unless exactly one current element plausibly is the old target.

    Same rule as computer_plan's recovery (decision_engine.assess_ambiguity): a weak best match is
    not found, and two close candidates are ambiguous unless an enabled Decision picked one. A
    label that several controls shared when it was observed (one Delete per row) is never rebound
    by that label alone. Nothing was done to the page either way.
    """
    stale = {"stale_element_id": action.get("element_id"), "rebind_via": via, "observe_again": True,
             "no_side_effect": True}
    if isinstance(found, dict) and found.get("ok") is False:
        return {**found, **stale}
    best = found or {}
    decision = best.get("decision") if isinstance(best.get("decision"), dict) else None
    if via == "observed_identity_not_unique" or (decision is not None and not decision.get("accepted")):
        return {
            "ok": False, "error": "stale_target_ambiguous", "reason_code": "RECOVERY_AMBIGUOUS_TARGET",
            "message": "The target re-rendered and more than one element now matches it; nothing was clicked. "
                       "Observe again and pick one (query/within).",
            **({"ambiguity": decision.get("ambiguity")} if decision else {}), **stale,
        }
    if float(best.get("confidence") or 0.0) < decision_engine.AMBIGUITY_FLOOR:
        return {
            "ok": False, "error": "target_not_found", "reason_code": "RECOVERY_TARGET_NOT_FOUND",
            "message": "The target re-rendered and nothing on the page now matches it closely enough.",
            "best_confidence": best.get("confidence"), **stale,
        }
    return None
_ACT_TYPES = (
    "click", "double_click", "type", "type_text", "paste", "select", "scroll", "focus",
    "wait", "key", "keyboard", "shortcut", "extract", "scan", "read", "dialog", "hover", "drag", "post", "fill", "autocomplete_many",
    "network", "pdf",
)
# Agents often name the action kind "action" instead of "type"; accept it.
_ACT_TYPE_ALIASES = ("action", "kind", "op")


# expect: what must be true after an action for its intended result to count as done.
_EXPECT_KINDS = {"text": "text", "gone": "text_gone", "selector": "selector", "url_change": "url_change",
                 "download": "download"}
_EXPECT_DEFAULT_TIMEOUT_S = 5.0
_EXPECT_DOWNLOAD_TIMEOUT_S = 60.0
# A page that changed is not the intended result: a menu closing is not a link copied,
# a network request is not a submission. These need a check (expect) to be confirmed.
_REACTED_VERIFICATIONS = frozenset({
    "state_changed", "dom_mutated", "async_state_changed", "async_dom_mutated", "async_network_activity",
    "async_navigation",
})
_UNCONFIRMED_VERIFICATIONS = frozenset({
    "editor_model_unconfirmed", "dom_text_only", "editor_pending", "deferred_pending", "focus_settling",
    "editor_render_unconfirmed", "editor_rerendered_text",
})
_CONFIRMED_VERIFICATIONS = frozenset({"value_applied", "value_applied_async"})
_BLOCKED_ERRORS = frozenset({
    "chrome_debugger_blocked", "foreground_not_authorized", "capability_required", "policy_denied",
    "approval_denied", "trusted_input_unavailable",
})
LOCAL_COPY_HINT = (
    "This page also keeps the text in the browser's own storage, so seeing it here, even after a reload, does not "
    "show the site saved it. To know it was saved, check where this browser cannot fill it in: the site's own list "
    "or sent view, another device, or a private window. Do not report it as saved, and do not submit it again."
)
EXPECT_ALREADY_TRUE_HINT = (
    "The page was already like this before the action, so this check does not show the action worked. Check "
    "something only the action brings about (new text, gone, url_change) or observe the page; do not repeat the "
    "action to check."
)
CONFIRM_HINT = (
    "page_reacted means the page changed after the action, not that its intended result happened (a link "
    "copied, an image inserted, a form submitted). Add expect={text|gone|selector|url_change} to the action, "
    "or observe the page, before reporting success; do not repeat the action to check."
)


def _expect_wait(index: int, expect: Any) -> Dict[str, Any]:
    if not isinstance(expect, dict):
        raise mark_not_executed(HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"actions[{index}] expect must be an object such as {{text: 'Pending review'}} or {{gone: 'Choose an image'}}.",
        ))
    kinds = [key for key in _EXPECT_KINDS if key in expect]
    if len(kinds) != 1:
        raise mark_not_executed(HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"actions[{index}] expect needs exactly one of {', '.join(_EXPECT_KINDS)}.",
        ))
    kind = kinds[0]
    value = expect[kind]
    if kind == "url_change":
        if value is not True:
            raise mark_not_executed(HTTPException(status.HTTP_400_BAD_REQUEST, f"actions[{index}] expect url_change must be true."))
    elif kind == "download":
        if value is not True and not (isinstance(value, str) and value.strip()):
            raise mark_not_executed(HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"actions[{index}] expect download must be true or part of the file name.",
            ))
    elif not isinstance(value, str) or not value.strip():
        raise mark_not_executed(HTTPException(
            status.HTTP_400_BAD_REQUEST, f"actions[{index}] expect {kind} must be non-empty text.",
        ))
    default_timeout = _EXPECT_DOWNLOAD_TIMEOUT_S if kind == "download" else _EXPECT_DEFAULT_TIMEOUT_S
    try:
        timeout_s = float(expect.get("timeout_s", default_timeout))
    except (TypeError, ValueError):
        timeout_s = default_timeout
    wait: Dict[str, Any] = {
        "type": "wait", "for": _EXPECT_KINDS[kind],
        "timeout_s": max(0.2, min(timeout_s, 120.0 if kind == "download" else 30.0)),
        "required": True, "_expect_for": index, "_expect": {kind: value},
    }
    if kind == "download" and isinstance(value, str):
        wait["filename_contains"] = value.strip()
    if kind in {"text", "gone"}:
        wait["text"] = value
    elif kind == "selector":
        wait["selector"] = value
    return wait


def _download_wait(settings: Settings, action: Dict[str, Any], since_ms: Optional[int]) -> Dict[str, Any]:
    """expect download: the file the action started, matched from just before that action ran (#34).

    Waiting from the action's own start catches a download that finished before this check began.
    """
    from .tools_browser import browser_wait_for_download

    started = time.perf_counter()
    outcome = browser_wait_for_download(
        settings, filename_contains=action.get("filename_contains") or None,
        timeout_s=max(1, int(round(float(action.get("timeout_s") or _EXPECT_DOWNLOAD_TIMEOUT_S)))),
        started_after_epoch_ms=since_ms,
    )
    result: Dict[str, Any] = {
        "ok": True, "type": "wait", "for": "download", "matched": bool(outcome.get("completed")),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    if outcome.get("completed"):
        result["download"] = {key: outcome.get(key) for key in ("path", "filename", "artifact_id")}
    elif outcome.get("error") == "ambiguous_download":
        result["ambiguous"] = {key: outcome.get(key) for key in ("error", "candidates", "hint") if outcome.get(key)}
    else:
        result["timed_out"] = True
    return result


def _caller_action_count(actions: List[Any]) -> int:
    """Actions the caller asked for: the checks expect adds do not count against the limit (#87)."""
    return sum(1 for action in actions if not (isinstance(action, dict) and "_expect_for" in action))


def _expand_expectations(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each action with expect is followed by the wait that checks it."""
    expanded: List[Dict[str, Any]] = []
    for index, action in enumerate(actions):
        expect = action.get("expect")
        if expect is None:
            expanded.append(action)
            continue
        if action.get("type") in {"wait", "extract", "scan", "read", "dialog"}:
            raise mark_not_executed(HTTPException(
                status.HTTP_400_BAD_REQUEST, f"actions[{index}] ({action.get('type')}) takes no expect; it already reports its result.",
            ))
        wait = _expect_wait(index, expect)
        follows: Dict[str, Any] = {"_expect_follows": True}
        if "text" in wait["_expect"] or "gone" in wait["_expect"]:
            follows["_expect_text"] = wait["text"]
        expanded.append({**{key: value for key, value in action.items() if key != "expect"}, **follows})
        expanded.append(wait)
    return expanded


def _fold_expectations(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Put each expect check back on the action it belongs to."""
    folded: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or "_expect_for" not in item:
            folded.append(item)
            continue
        target = folded[-1] if folded and isinstance(folded[-1], dict) else None
        expect = item.get("_expect") or {}
        met = bool(item.get("matched"))
        check = {**expect, "met": met, "waited_ms": item.get("duration_ms")}
        if met and "text" in expect and item.get("_local_copy"):
            # The text is on the page, and the page keeps it itself: it may be a draft, not what the site saved (#81).
            check.update({"evidence": "page_and_browser_storage", "local_copy": item["_local_copy"],
                          "saved_on_site": "unverified", "hint": LOCAL_COPY_HINT})
        if isinstance(item.get("download"), dict):
            check["download"] = item["download"]
        if item.get("ambiguous"):
            check["ambiguous"] = item["ambiguous"]
        if target is None:
            continue
        before = target.pop("_expect_text_present_before", None)
        if met and before is not None and before == ("text" in expect):
            # The page already showed (or already lacked) the text before the action: finding it so
            # afterwards says nothing about what the action did (#89).
            check.update({"already_true_before_action": True, "hint": EXPECT_ALREADY_TRUE_HINT})
            target["expect"] = check
            continue
        target["expect"] = check
        if met and target.get("error") == "action_no_effect":
            # Nothing changed on the page, but the expected result (a download, say) came about.
            for key in ("error", "reason_code", "observe_again"):
                target.pop(key, None)
            target.update({"ok": True, "verification": "expect_met"})
        if not met:
            # The action ran; only its intended result is missing. Never replay it automatically.
            target.update({
                "ok": False, "error": "expectation_not_met", "reason_code": "EXPECTATION_NOT_MET",
                "outcome_unknown": True, "automatic_retry": False, "observe_again": True,
            })
    for item in folded:
        if isinstance(item, dict):
            item.pop("_expect_text_present_before", None)
    return folded


def _action_outcome(result: Dict[str, Any]) -> str:
    """confirmed | page_reacted | unconfirmed | failed | blocked, for one action result."""
    error = str(result.get("error") or "")
    if result.get("human_priority") or result.get("yielded") or error in _BLOCKED_ERRORS:
        return "blocked"
    expect = result.get("expect")
    if isinstance(expect, dict) and not expect.get("already_true_before_action"):
        return "confirmed" if expect.get("met") else "unconfirmed"
    if result.get("ok") is False:
        return "unconfirmed" if result.get("outcome_unknown") or result.get("posted") is None and result.get("type") == "post" else "failed"
    typ = str(result.get("type") or "")
    verification = str(result.get("verification") or "")
    if verification in _CONFIRMED_VERIFICATIONS:
        return "confirmed"
    if verification in _UNCONFIRMED_VERIFICATIONS or result.get("editor_state_unverified"):
        return "unconfirmed"
    if typ == "post":
        return "confirmed" if result.get("posted") is True else "unconfirmed"
    if typ in {"wait"}:
        return "confirmed" if result.get("matched") else "unconfirmed"
    if typ in {"fill", "select", "autocomplete_many", "extract", "scan", "read", "scroll", "focus", "dialog"}:
        return "confirmed"
    if verification in _REACTED_VERIFICATIONS or result.get("effect_observed"):
        return "page_reacted"
    return "unconfirmed"


def _flag_unconfirmed(response: Dict[str, Any]) -> None:
    """Name the actions whose intended result nothing checked, so a caller cannot read ok as done."""
    pending = [
        index for index, item in enumerate(response.get("actions") or [])
        if isinstance(item, dict) and item.get("outcome") in {"page_reacted", "unconfirmed"}
    ]
    if pending:
        response["unconfirmed_actions"] = pending
        response["confirm_hint"] = CONFIRM_HINT


def _autocomplete_values_problem(values: Any) -> Optional[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        return "values must contain 1-10 strings"
    if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values):
        return "each value must be a nonempty string of at most 100 characters"
    if len({v.strip().casefold() for v in values}) != len(values):
        return "duplicate values are not allowed"
    return None


def _autocomplete_many_js(action: Dict[str, Any]) -> str:
    return _fill_js(_browser_js("autocomplete_many.js"), {
        "__BOOTSTRAP__": _browser_state_bootstrap(),
        "__SPEC__": json.dumps({k: action.get(k) for k in (
            "selector", "values", "option_selector", "selected_selector", "timeout_ms"
        )}, ensure_ascii=False),
    })


def _autocomplete_status_js(token: str, *, cancel: bool = False) -> str:
    return f"""(function(){{
{_browser_state_bootstrap()}
var r=__mcpState().autocompleteRuns&&__mcpState().autocompleteRuns[{json.dumps(token)}];
{"if(r)r.cancelled=true;" if cancel else ""}
return btoa(unescape(encodeURIComponent(JSON.stringify(r?(r.pending?{{ok:true,pending:true}}:r.result):{{ok:false,error:'autocomplete_run_missing',outcome_unknown:true}}))));
}})()"""


def _autocomplete_many_action(settings: Settings, browser: str, action: Dict[str, Any],
                              window_index: int, tab_index: Optional[int],
                              tab_handle: Optional[str], mutation_revalidator=None) -> Dict[str, Any]:
    target, blocked = mutation_revalidator("autocomplete_many") if mutation_revalidator else (None, None)
    if blocked is not None:
        return {"type": "autocomplete_many", **blocked, "_js_calls": 0}
    out = _run_json_js(settings, browser, _autocomplete_many_js(action),
                       window_index, tab_index, tab_handle, prevalidated_target=target)
    token = str(out.get("run_token") or "")
    if not token:
        return {"type": "autocomplete_many", **out, "_js_calls": 1, "automatic_retry": False}
    calls = 1
    deadline = time.monotonic() + (len(action["values"]) * min(5.0, max(0.1, float(action.get("timeout_ms") or 2200)/1000))*2) + 2
    while time.monotonic() < deadline:
        cancellable_sleep(0.08)
        # A delegated action cannot keep modifying a tab after the owner takes over.
        next_target, denied = mutation_revalidator("autocomplete_many") if mutation_revalidator else (None, None)
        if denied is not None:
            try:
                _run_json_js(settings, browser, _autocomplete_status_js(token, cancel=True),
                             window_index, tab_index, tab_handle)
            except Exception:
                pass
            return {"type": "autocomplete_many", **denied, "outcome_unknown": True,
                    "automatic_retry": False, "_js_calls": calls}
        state = _run_json_js(settings, browser, _autocomplete_status_js(token),
                             window_index, tab_index, tab_handle, prevalidated_target=next_target)
        calls += 1
        if not state.get("pending"):
            return {"type": "autocomplete_many", **state, "automatic_retry": False,
                    "_js_calls": calls}
    try:
        _run_json_js(settings, browser, _autocomplete_status_js(token, cancel=True),
                     window_index, tab_index, tab_handle)
    except Exception:
        pass
    return {"type": "autocomplete_many", "ok": False, "error": "autocomplete_result_timeout",
            "outcome_unknown": True, "automatic_retry": False, "_js_calls": calls}


def normalize_act_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return actions with a known lower-case type, or raise before anything runs.

    An unknown or missing type used to be queued and only fail inside the page
    batch, after earlier steps ran and with the call counted as dispatched.
    """
    normalized: List[Dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise mark_not_executed(HTTPException(status.HTTP_400_BAD_REQUEST, "Each action must be an object."))
        raw = action.get("type")
        if not raw:
            raw = next((action.get(key) for key in _ACT_TYPE_ALIASES if action.get(key)), None)
        typ = str(raw or "").strip().lower().replace("-", "_")
        if typ not in _ACT_TYPES:
            raise mark_not_executed(HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"actions[{index}] has unsupported type {str(raw or '')!r}; "
                f"set \"type\" to one of: {', '.join(_ACT_TYPES)}.",
            ))
        if typ == "post" and not str(action.get("text") or "").strip():
            raise mark_not_executed(HTTPException(
                status.HTTP_400_BAD_REQUEST, f"actions[{index}] (post) needs text: the comment or reply to post.",
            ))
        if typ == "fill":
            problem = _fill_values_problem(action.get("values"))
            if problem:
                raise mark_not_executed(HTTPException(status.HTTP_400_BAD_REQUEST, f"actions[{index}] (fill) {problem}"))
        if typ == "autocomplete_many":
            problem = _autocomplete_values_problem(action.get("values"))
            if problem or not str(action.get("selector") or "").strip() or not str(action.get("selected_selector") or "").strip():
                raise mark_not_executed(HTTPException(status.HTTP_400_BAD_REQUEST,
                    f"actions[{index}] (autocomplete_many) requires selector and 1-10 unique values: {problem or 'selector/selected_selector missing'}"))
        if typ == "extract":
            problem = _extract_fields_problem(action.get("fields"))
            if problem:
                raise mark_not_executed(HTTPException(status.HTTP_400_BAD_REQUEST, f"actions[{index}] (extract) {problem}"))
        item = dict(action)
        item["type"] = typ
        normalized.append(item)
    return normalized


def _tab_loss_detail(exc: HTTPException) -> Optional[Dict[str, Any]]:
    """Classify an error raised because the act's target tab disappeared mid-call."""
    detail = exc.detail
    if isinstance(detail, dict):
        error = str(detail.get("error") or "")
        if error in {"tab_target_closed", "stale_tab_handle", "ambiguous_tab_handle"}:
            return {
                "error": error,
                "reason_code": str(detail.get("reason_code") or error.upper()),
                "message": str(detail.get("message") or ""),
            }
        return None
    text = str(detail or "")
    if exc.status_code == status.HTTP_409_CONFLICT and text.startswith("Target tab identity changed"):
        return {"error": "tab_identity_changed", "reason_code": "TAB_IDENTITY_CHANGED", "message": text}
    if "Invalid index" in text and ("tab" in text or "window" in text):
        # Safari tab references are positional inside one AppleScript; a tab closed
        # between the identity guard and the action surfaces as an index error.
        return {
            "error": "tab_target_closed",
            "reason_code": "TAB_TARGET_CLOSED",
            "message": "The target tab moved or closed while the action was running.",
        }
    return None


def _browser_act_impl(
    settings: Settings,
    browser: str,
    actions: List[Dict[str, Any]],
    observation_id: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    return_state: str = "compact",
    allow_foreground: bool = False,
    *,
    _expanded: bool = False,
) -> Dict[str, Any]:
    """Perform one serialized action transaction against a single logical tab."""
    cancellation_checkpoint()
    started = False
    try:
        if not isinstance(actions, list) or not actions:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must be a non-empty list.")
        if not _expanded:
            if len(actions) > _MAX_ACTIONS:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"actions may contain at most {_MAX_ACTIONS} items.")
            # Keys starting with _ are this module's own bookkeeping (expect checks), never the caller's.
            actions = [{k: v for k, v in item.items() if not str(k).startswith("_")} if isinstance(item, dict) else item
                       for item in actions]
        actions = normalize_act_actions(actions)
        requested_action_count = _caller_action_count(actions)
        if not _expanded:
            actions = _expand_expectations(actions)
        normalized_return_state = str(return_state or "compact").lower().strip()
        if normalized_return_state not in _RETURN_STATE_MODES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full.")
        b = _norm_browser(browser)
        _require_stable_handle_for_mutation(b, tab_handle, window_index, "browser_act")
        if actions[0]["type"] == "dialog":
            # An open dialog blocks every page script, including the batch's own setup,
            # so leading dialog answers run first, before anything touches the page.
            leading: List[Dict[str, Any]] = []
            while actions and actions[0]["type"] == "dialog":
                leading.append(actions.pop(0))
            with _tab_lease(b, tab_handle, window_index, tab_index, mutation=True) as target:
                started = True
                answered = []
                for item in leading:
                    answered.append(_dialog_action(b, item, target.tab_handle))
                    answered[-1]["outcome"] = _action_outcome(answered[-1])
                    if not answered[-1].get("ok"):
                        break
            failed = next((item for item in answered if not item.get("ok")), None)
            if failed is not None or not actions:
                out: Dict[str, Any] = {
                    "ok": failed is None, "actions": answered, "action_count": len(answered),
                    "mutation_dispatched": any(item.get("ok") for item in answered),
                }
                if failed is not None:
                    out.update(error=failed.get("error"), observe_again=True)
                return out
            rest = _browser_act_impl(settings, b, actions, None, window_index, tab_index, tab_handle,
                                     return_state, allow_foreground, _expanded=True)
            rest["actions"] = answered + list(rest.get("actions") or [])
            rest["action_count"] = len(rest["actions"])
            rest["mutation_dispatched"] = True
            # The rest numbered its own actions; number them again in the final list (#88).
            rest.pop("unconfirmed_actions", None)
            rest.pop("confirm_hint", None)
            _flag_unconfirmed(rest)
            return rest
        _ensure_visual_companion(settings, b, window_index, tab_index, tab_handle)
        # A batch that only reads (extract, read, wait, network, pdf) leases the tab the way observe
        # does: it never takes a tab another agent holds, and needs no observe of its own first.
        read_only = all(item["type"] in _NON_MUTATING_ACT_TYPES for item in actions)
        with _measure_transport() as transport, _tab_lease(
            b, tab_handle, window_index, tab_index, allow_rebind=read_only, mutation=not read_only,
        ) as target:
            started = True
            result = _browser_act_locked(
                settings=settings,
                browser=target.browser,
                actions=actions,
                observation_id=observation_id,
                window_index=target.window_index,
                tab_index=target.tab_index,
                tab_handle=target.tab_handle,
                lease_generation=int(getattr(target, "lease_generation", 0) or 0),
                return_state=normalized_return_state,
                allow_foreground=allow_foreground,
            )
            if isinstance(result, dict):
                # Bytes sent to and returned from the page, separate from the response size.
                result["transport"] = dict(transport)
                result["action_count"] = requested_action_count
                _flag_unconfirmed(result)
            return result
    except HTTPException as exc:
        # Validation, tab-handle and lease refusals happen before any action runs;
        # tag them so a delegated agent's checkpoint is not made unknown.
        if not started:
            mark_not_executed(exc)
        raise


def _browser_act_locked(
    settings: Settings,
    browser: str,
    actions: List[Dict[str, Any]],
    observation_id: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    lease_generation: Optional[int] = None,
    return_state: str = "compact",
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    """Perform batched browser actions while the caller holds the tab lease."""
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must be a non-empty list.")
    if _caller_action_count(actions) > _MAX_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"actions may contain at most {_MAX_ACTIONS} items.")
    actions = normalize_act_actions(actions)
    return_state = str(return_state or "compact").lower().strip()
    if return_state not in _RETURN_STATE_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full.")

    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    tabs_before: Optional[List[str]] = None
    if any(item["type"] == "wait" and _wait_kind(item) == "new_tab" for item in actions):
        # A popup is recognized by a handle that did not exist before the batch.
        before = sorted(str(row.get("tab_handle") or "") for row in browser_tabs.list_tabs(browser))
        tabs_before = before
        actions = [
            {**item, "_tabs_before": before} if item["type"] == "wait" and _wait_kind(item) == "new_tab" else item
            for item in actions
        ]

    cancellation_checkpoint()
    started = time.perf_counter()
    results: List[Dict[str, Any]] = []
    internal_js_calls = 0
    current_observation_id = observation_id
    delegated_transaction = delegated_agent_identity() is not None

    def revalidate_mutation(
        action_type: str,
    ) -> Tuple[Optional[browser_tabs.TabTarget], Optional[Dict[str, Any]]]:
        if not delegated_transaction:
            # The owner may take the tab over mid-batch (#124): no caller acts on it after that.
            taken = owner_takeover_block(tab_handle)
            return None, ({**taken, "type": str(action_type or "mutation")} if taken else None)
        cancellation_checkpoint()
        fresh_target, blocked = browser_tabs.revalidate_mutation_lease(
            browser,
            str(tab_handle or ""),
            lease_generation,
        )
        if blocked is None:
            return fresh_target, None
        result = dict(blocked)
        result.setdefault("ok", False)
        result["type"] = str(action_type or "mutation")
        result["automatic_retry"] = False
        return None, result
    needs_initial_url = any(
        isinstance(a, dict) and str(a.get("type") or "").lower().replace("-", "_") == "wait"
        and str(a.get("for") or a.get("condition") or "").lower().strip() == "url_change"
        for a in actions
    )
    initial_url = ""
    if needs_initial_url:
        initial_state = _run_json_js(
            settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
        )
        internal_js_calls += 1
        initial_url = str(initial_state.get("url") or "")
    pending: List[Dict[str, Any]] = []
    in_flight: List[str] = []
    # Set just before any page-changing step runs; a result that fails without
    # it proves nothing was done, which lets a delegated agent keep working.
    mutation_dispatched = False

    def note_possible_navigation() -> None:
        # Marked before dispatch: a click, key or select may start a cross-site
        # navigation whose Safari pid swap lands while the action is still being
        # verified. The marker lets that swap rebind this same handle.
        if browser == "Safari" and tab_handle:
            browser_tabs.expect_safari_navigation(tab_handle)

    def resolve_target(action: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        nonlocal internal_js_calls
        if action.get("element_id"):
            return dict(action), None
        query = str(action.get("query") or action.get("target") or "").strip()
        role = action.get("role")
        match_text = action.get("text_match") or action.get("target_text")
        within = str(action.get("within") or "").strip() or None
        within_element_id = str(action.get("within_element_id") or "").strip() or None
        within_levels = action.get("within_levels")
        if not query and not role and not match_text:
            return dict(action), None
        find_args = dict(
            query=query, role=role, text=match_text,
            window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
            max_results=_TARGET_CANDIDATE_LIMIT,
            within=within, within_element_id=within_element_id, within_levels=within_levels,
        )
        found = browser_find(settings, browser, **find_args)
        internal_js_calls += 1
        best = found.get("best_match")
        waited_s = 0.0
        scope = found.get("within") if isinstance(found.get("within"), dict) else None
        anchor_failure = _within_anchor_failure(found, within or within_element_id)
        if anchor_failure is not None:
            return dict(action), anchor_failure
        if not best:
            wait_s = _late_target_wait_s(action, results)
            if wait_s > 0 and scope is not None:
                # The generic DOM waiter does not know the scope and would return
                # as soon as any match exists elsewhere, so poll the scoped search.
                deadline = time.monotonic() + wait_s
                while not best and time.monotonic() < deadline:
                    time.sleep(0.25)
                    found = browser_find(settings, browser, **find_args)
                    internal_js_calls += 1
                    best = found.get("best_match")
                waited_s = wait_s
            elif wait_s > 0:
                # A control revealed by an earlier step in this batch (picker confirm,
                # autocomplete option) may render a moment later; wait for it here
                # instead of failing back to the outer agent for another observe.
                found = browser_find(settings, browser, wait_timeout_s=wait_s, **find_args)
                internal_js_calls += 1
                best = found.get("best_match")
                waited_s = wait_s
        if not best:
            near: List[Dict[str, Any]] = []
            try:
                # Relax the role/text constraints once so the caller can pick a target
                # directly; with none to relax, the same search would only repeat.
                loose = {key: value for key, value in find_args.items() if key not in {"role", "text"}}
                if not str(loose.get("query") or "").strip():
                    loose["query"] = str(match_text or "")
                if (role or match_text) and str(loose.get("query") or "").strip():
                    loose["max_results"] = 5
                    relaxed = browser_find(settings, browser, **loose)
                    internal_js_calls += 1
                    near = [
                        {k: item.get(k) for k in ("element_id", "role", "tag", "text", "confidence") if item.get(k) is not None}
                        for item in (relaxed.get("matches") or [])[:5] if isinstance(item, dict)
                    ]
            except HTTPException:
                near = []
            return dict(action), {
                "ok": False, "error": "target_not_found", "query": query,
                "role": role, "text": match_text,
                **({"near_matches": near} if near else {}),
                **({"within": within or within_element_id, "within_levels": (scope or {}).get("levels")} if scope else {}),
                **({"waited_s": waited_s} if waited_s else {}),
            }
        matches = [item for item in (found.get("matches") or []) if isinstance(item, dict)] or [best]
        if scope is not None:
            # Inside a scope the nearest match is the answer; only exact
            # locality ties go to the general look-alike tie-breaker.
            nearest = (best.get("within_up"), best.get("within_down"))
            matches = [item for item in matches if (item.get("within_up"), item.get("within_down")) == nearest] or [best]
        chosen = _decide_browser_target(action, query, role, match_text, matches)
        resolved = dict(action)
        resolved["element_id"] = chosen.get("element_id")
        return resolved, chosen

    def flush_pending() -> bool:
        nonlocal pending, internal_js_calls, current_observation_id, in_flight, mutation_dispatched
        if not pending:
            return True
        mutation_target, blocked = revalidate_mutation(
            str(pending[0].get("type") or "mutation")
        )
        if blocked is not None:
            blocked["blocked_action_count"] = len(pending)
            results.append(blocked)
            pending = []
            return False
        in_flight = [str(item.get("type") or "") for item in pending]
        mutation_dispatched = True
        out = _run_json_js(
            settings,
            browser,
            _batch_js(pending, current_observation_id),
            window_index,
            tab_index,
            tab_handle,
            prevalidated_target=mutation_target,
        )
        internal_js_calls += 1
        if not out.get("ok") and out.get("error") == "stale_observation":
            results.append({"ok": False, "error": "stale_observation", "observe_again": True})
            pending = []
            return False
        results.extend(out.get("actions") or [])
        pending = []
        in_flight = []
        if out.get("ok"):
            current_observation_id = None
        return bool(out.get("ok"))

    def post_page_js(js: str) -> Dict[str, Any]:
        nonlocal internal_js_calls
        internal_js_calls += 1
        return _run_json_js(settings, browser, js, window_index, tab_index, tab_handle)

    def post_item_field(
        action: Dict[str, Any], scope_keys: Dict[str, Any], within_label: Any, notes: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The reply box of one item. It may be no farther from the item text than the item's own
        Reply button: a nearer-to-the-page box (the post's main composer, another comment's open
        box) belongs to something else. A closed box is opened with that Reply button first."""
        nonlocal internal_js_calls, mutation_dispatched

        def scoped_find(role: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
            nonlocal internal_js_calls
            found = browser_find(
                settings, browser, query="", role=role, window_index=window_index, tab_index=tab_index,
                tab_handle=tab_handle, max_results=10, **scope_keys,
            )
            internal_js_calls += 1
            return [item for item in (found.get("matches") or []) if isinstance(item, dict)], _within_anchor_failure(found, within_label)

        buttons, failure = scoped_find("button")
        if failure is not None:
            return None, failure
        opener = _post_reply_opener(buttons)
        limit = int(opener.get("within_up") or 0) if opener else None
        deadline = time.monotonic() + _late_target_wait_s(action, results)
        boxes: List[Tuple[bool, Dict[str, Any]]] = []
        while True:
            matches, failure = scoped_find("textbox")
            if failure is not None:
                return None, failure
            boxes = _post_boxes(matches)
            near = [match for _named, match in boxes if limit is None or int(match.get("within_up") or 0) <= limit]
            if near:
                return near[0], None
            if opener is not None and not notes.get("opened_reply"):
                note_possible_navigation()
                mutation_dispatched = True
                clicked = _verified_dom_action(
                    settings, browser, {"type": "click", "element_id": str(opener.get("element_id") or "")}, None,
                    window_index, tab_index, tab_handle, mutation_revalidator=revalidate_mutation,
                )
                internal_js_calls += int(clicked.pop("_js_calls", 0))
                if not clicked.get("ok") and clicked.get("error") != "action_no_effect":
                    return None, {key: value for key, value in clicked.items() if key not in {"type", "_compact_state"}}
                notes["opened_reply"] = _post_label(opener) or "reply"
                deadline = time.monotonic() + _LATE_TARGET_WAIT_S
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if boxes:
            return None, {
                "ok": False, "error": "post_field_outside_item", "reason_code": "POST_FIELD_OUTSIDE_ITEM",
                "boxes": [_post_label(match) or "textbox" for _named, match in boxes][:6],
                **({"opened_reply": notes["opened_reply"]} if notes.get("opened_reply") else {}),
                "observe_again": False, "automatic_retry": False,
                "hint": "The only boxes near this item belong to the page or another item, so nothing was typed. "
                        "If the site replies in one shared composer, open it and post with field='<its label>' instead of within.",
            }
        return None, {
            "ok": False, "error": "post_field_not_found", "reason_code": "POST_FIELD_NOT_FOUND",
            **({"opened_reply": notes["opened_reply"]} if notes.get("opened_reply") else {}),
            "observe_again": False, "automatic_retry": False,
            "hint": "No reply box opened for this item. Check the within text, or click the control that opens it first.",
        }

    def post_field(
        action: Dict[str, Any], scope_keys: Dict[str, Any], notes: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The box to type into: element_id, field=label words, or the page's (or item's) comment box."""
        nonlocal internal_js_calls
        if action.get("element_id"):
            return {"element_id": str(action["element_id"])}, None
        if action.get("field"):
            resolved, chosen = resolve_target({"type": "type", "query": str(action["field"]), **scope_keys})
            if isinstance(chosen, dict) and chosen.get("ok") is False:
                return None, chosen
            return (chosen or {"element_id": resolved.get("element_id")}), None
        within_label = scope_keys.get("within") or scope_keys.get("within_element_id")
        if scope_keys:
            return post_item_field(action, scope_keys, within_label, notes)
        deadline = time.monotonic() + _late_target_wait_s(action, results)
        while True:
            found = browser_find(
                settings, browser, query="", role="textbox", window_index=window_index, tab_index=tab_index,
                tab_handle=tab_handle, max_results=10, **scope_keys,
            )
            internal_js_calls += 1
            anchor_failure = _within_anchor_failure(found, within_label)
            if anchor_failure is not None:
                return None, anchor_failure
            matches = [item for item in (found.get("matches") or []) if isinstance(item, dict)]
            chosen, reason = _pick_post_field(matches, scoped=bool(scope_keys))
            if chosen is not None:
                return chosen, None
            if reason == "ambiguous" or time.monotonic() >= deadline:
                break
            # A box an earlier step opened (a Reply button) may render a moment later.
            time.sleep(0.25)
        boxes = [_post_label(item) or str(item.get("tag") or "textbox") for item in matches][:6]
        if reason == "ambiguous":
            return None, {
                "ok": False, "error": "post_field_ambiguous", "reason_code": "POST_FIELD_AMBIGUOUS",
                "boxes": boxes, "observe_again": False, "automatic_retry": False,
                "hint": "Several text boxes on the page; pass field='<words from the box label>', or within='<text of the item>' for a reply.",
            }
        return None, {
            "ok": False, "error": "post_field_not_found", "reason_code": "POST_FIELD_NOT_FOUND",
            **({"boxes": boxes} if boxes else {}), "observe_again": False, "automatic_retry": False,
            "hint": "No comment box found. If a button opens it (Reply, Add a comment), click it first in the same call.",
        }

    def post_submit_button(action: Dict[str, Any], field_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """The box's own submit button: submit=button text, else the nearest submit-named button."""
        levels = _POST_SUBMIT_LEVELS
        if action.get("submit"):
            resolved, chosen = resolve_target({
                "type": "click", "query": str(action["submit"]), "role": "button",
                "within_element_id": field_id, "within_levels": levels,
            })
            if isinstance(chosen, dict) and chosen.get("ok") is False:
                return None, chosen
            return (chosen or {"element_id": resolved.get("element_id")}), None
        found = post_page_js(_post_submit_js(field_id, levels))
        if not found.get("ok"):
            return None, {**found, "observe_again": True, "automatic_retry": False}
        if found.get("element_id"):
            return {"element_id": found["element_id"], "text": found.get("label")}, None
        ambiguous = bool(found.get("ambiguous"))
        return None, {
            "ok": False, "error": "post_submit_ambiguous" if ambiguous else "post_submit_not_found",
            "reason_code": "POST_SUBMIT_AMBIGUOUS" if ambiguous else "POST_SUBMIT_NOT_FOUND",
            "buttons": found.get("buttons") or [], "observe_again": False, "automatic_retry": False,
            "hint": "The text is in the box but nothing was posted. Call post again with submit='<button text>' "
                    "(it replaces the text, nothing is doubled), or click the button.",
        }

    def run_post(action: Dict[str, Any]) -> Dict[str, Any]:
        """Type into the comment box, press its submit button and confirm the text is on the page."""
        nonlocal internal_js_calls, mutation_dispatched, in_flight
        text = str(action.get("text") or "")
        scope_keys = {key: action[key] for key in ("within", "within_element_id", "within_levels") if action.get(key)}
        out: Dict[str, Any] = {"type": "post", "ok": False, "posted": False, "submitted": False}

        notes: Dict[str, Any] = {}
        field, failure = post_field(action, scope_keys, notes)
        if notes.get("opened_reply"):
            out["opened_reply"] = notes["opened_reply"]
        if failure is not None:
            return {**out, **failure, "type": "post", "stage": "field"}
        field_id = str((field or {}).get("element_id") or "")
        out["field"] = {"element_id": field_id, **({"label": _post_label(field)} if _post_label(field or {}) else {})}

        in_flight = ["post"]
        dispatched_before = mutation_dispatched
        mutation_dispatched = True
        typed = _verified_dom_action(
            settings, browser,
            {"type": "type", "element_id": field_id, "text": text, **({"clear": action["clear"]} if "clear" in action else {})},
            current_observation_id if action.get("element_id") else None,
            window_index, tab_index, tab_handle, mutation_revalidator=revalidate_mutation,
        )
        internal_js_calls += int(typed.pop("_js_calls", 0))
        typed.pop("_compact_state", None)
        if typed.get("no_side_effect"):
            mutation_dispatched = dispatched_before
        if not typed.get("ok"):
            detail = {key: value for key, value in typed.items() if key not in {"type", "ok", "value"}}
            return {**out, **detail, "type": "post", "ok": False, "stage": "type"}
        out["typed"] = True

        button, failure = post_submit_button(action, field_id)
        if failure is not None:
            return {**out, **failure, "type": "post", "stage": "submit"}
        button_id = str((button or {}).get("element_id") or "")
        out["submit"] = {"element_id": button_id, **({"label": str(button.get("text"))} if (button or {}).get("text") else {})}

        snippet = _post_snippet(text)
        before = post_page_js(_post_check_js(field_id, snippet)) if snippet else {}
        baseline = int(before.get("count") or 0)
        note_possible_navigation()
        clicked = _verified_dom_action(
            settings, browser, {"type": "click", "element_id": button_id}, None,
            window_index, tab_index, tab_handle, mutation_revalidator=revalidate_mutation,
        )
        internal_js_calls += int(clicked.pop("_js_calls", 0))
        clicked.pop("_compact_state", None)
        if clicked.get("no_side_effect") or (not clicked.get("ok") and clicked.get("error") != "action_no_effect"):
            detail = {key: value for key, value in clicked.items() if key not in {"type", "ok"}}
            return {**out, **detail, "type": "post", "ok": False, "stage": "submit"}
        out["submitted"] = True

        # The button was pressed. From here on the outcome is reported, never retried.
        timeout_s = max(1.0, min(float(action.get("timeout_s") or _POST_VERIFY_S), 30.0))
        deadline = time.monotonic() + timeout_s
        check: Dict[str, Any] = {}
        while True:
            cancellation_checkpoint()
            check = post_page_js(_post_check_js(field_id, snippet))
            seen = int(check.get("count") or 0) > baseline
            # With no words to look for (a post that is only a link), only an emptied box shows it was sent (#100).
            field_cleared = not check.get("field_connected") or (
                not check.get("field_has_text") if snippet else bool(check.get("field_empty")))
            if seen or (not snippet and field_cleared) or time.monotonic() >= deadline:
                break
            cancellable_sleep(0.4)
        out["field_cleared"] = bool(field_cleared)
        if seen:
            return {**out, "ok": True, "posted": True, "verification": "text_visible"}
        if not snippet and field_cleared:
            return {**out, "ok": True, "posted": True, "verification": "field_cleared"}
        return {
            **out, "ok": False, "posted": None, "error": "post_not_confirmed", "reason_code": "POST_NOT_CONFIRMED",
            "outcome_unknown": True, "automatic_retry": False, "observe_again": True, "waited_s": timeout_s,
            "hint": "The submit button was pressed but the text did not show up on the page. Do not post again "
                    "before checking: browser_find text='<first words>' (it may be pending, collapsed or sorted elsewhere).",
        }

    def fill_control(
        label: str, scope_keys: Dict[str, Any], wait_s: float,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        nonlocal internal_js_calls
        deadline = time.monotonic() + wait_s
        while True:
            found = browser_find(
                settings, browser, query=label, window_index=window_index, tab_index=tab_index,
                tab_handle=tab_handle, max_results=10, **scope_keys,
            )
            internal_js_calls += 1
            anchor_failure = _within_anchor_failure(found, scope_keys.get("within") or scope_keys.get("within_element_id"))
            if anchor_failure is not None:
                return None, anchor_failure
            matches = [item for item in (found.get("matches") or []) if isinstance(item, dict)]
            chosen = _pick_fill_control(matches, label)
            if chosen is not None:
                return chosen, None
            if time.monotonic() >= deadline:
                near = [
                    _post_label(item) for item in matches[:5]
                    if _fill_is_control(item) or item.get("associated_control")
                ]
                return None, {"error": "field_not_found", **({"near_matches": [n for n in near if n]} if any(near) else {})}
            time.sleep(0.25)

    def run_fill(action: Dict[str, Any]) -> Dict[str, Any]:
        """Fill every labelled field; independent fields keep going when one is missing."""
        nonlocal internal_js_calls, mutation_dispatched, in_flight
        scope_keys = {key: action[key] for key in ("within", "within_element_id", "within_levels") if action.get(key)}
        fields: List[Dict[str, Any]] = []
        changed = False
        last_filled = ""
        in_flight = ["fill"]
        # Boxes whose list choice shows as their own text: that text must outlast leaving the box.
        chosen_in_box: set = set()
        # Boxes marked as offering a list: only their typed text can be refused by the page on leaving.
        suggesting: set = set()

        def apply(kind: str, step: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal internal_js_calls
            if kind == "select":
                done = _select_action(settings, browser, step, None, window_index, tab_index, tab_handle,
                                      mutation_revalidator=revalidate_mutation)
            else:
                done = _verified_dom_action(settings, browser, step, None, window_index, tab_index, tab_handle,
                                            mutation_revalidator=revalidate_mutation)
            internal_js_calls += int(done.pop("_js_calls", 0))
            return done

        def mutating_page_js(js: str) -> Dict[str, Any]:
            nonlocal internal_js_calls
            page_target, blocked = revalidate_mutation("fill")
            if blocked is not None:
                return {**blocked, "ok": False}
            internal_js_calls += 1
            return _run_json_js(settings, browser, js, window_index, tab_index, tab_handle,
                                prevalidated_target=page_target)

        def suggestible(target: Dict[str, Any]) -> bool:
            # Only a box marked as offering a list (find's "suggests") costs the extra page reads.
            return bool(target.get("suggests")) and str(target.get("tag") or "").lower() == "input" \
                and str(target.get("input_type") or "").lower() in _SUGGEST_INPUT_TYPES

        def pick_suggestion(element_id: str, value: Any) -> Optional[Dict[str, Any]]:
            """After typing into a text box: when typing opened a list (a search box), press the option that
            names the value and confirm it stayed. None when no list came: a plain text box."""
            nonlocal internal_js_calls
            started = time.monotonic()
            options: List[str] = []
            seen_list = False
            stable_since = started
            while True:
                cancellation_checkpoint()
                found = mutating_page_js(_fill_suggestion_js(element_id, value, "pick"))
                if found.get("error") and not found.get("ok"):
                    return found if found.get("reason_code") else None
                if found.get("found"):
                    break
                if found.get("no_such_option"):
                    return {"chosen": False, "no_such_option": True, "options": [str(item) for item in found.get("options") or []]}
                now = time.monotonic()
                if found.get("popup"):
                    seen_list = True
                    shown = [str(item) for item in found.get("options") or []]
                    if shown != options:
                        options, stable_since = shown, now
                    elif not found.get("busy") and now - stable_since >= _SUGGEST_STABLE_S:
                        break
                    if now - started >= _SUGGEST_MAX_S:
                        break
                elif seen_list or not found.get("marked") or now - started >= _SUGGEST_APPEAR_S:
                    break
                cancellable_sleep(0.12)
            if not found.get("found"):
                if not seen_list:
                    return None
                return {"chosen": False, "options": options, **({"ambiguous": True} if found.get("ambiguous") else {})}
            picked = str(found.get("picked") or "")
            deadline = time.monotonic() + 1.0
            while True:
                check = post_page_js(_fill_suggestion_js(element_id, value, "check"))
                if check.get("kept"):
                    if check.get("open") or check.get("expanded") == "true":
                        # Chosen, list still open (Tom Select): close it so it does not cover the next field.
                        key_target, blocked = revalidate_mutation("key")
                        if blocked is None:
                            closed = _dom_key_action(settings, browser, {"key": "Escape"}, element_id,
                                                     window_index, tab_index, tab_handle, prevalidated_target=key_target)
                            internal_js_calls += int(closed.pop("_js_calls", 0))
                            cancellable_sleep(_FILL_ESCAPE_SETTLE_S)
                    return {"chosen": True, "picked": picked, "in_box": bool(str(check.get("value") or "").strip())}
                if time.monotonic() >= deadline:
                    return {"chosen": False, "pressed": True, "picked": picked}
                cancellable_sleep(0.1)

        for label, value in dict(action.get("values") or {}).items():
            row: Dict[str, Any] = {"field": str(label)[:80]}
            # A field revealed by an earlier choice in this fill may render a moment later.
            wait_s = float(action["wait_s"]) if action.get("wait_s") is not None else (_LATE_TARGET_WAIT_S if changed else 0.0)
            target, failure = fill_control(str(label), scope_keys, max(0.0, min(wait_s, _LATE_TARGET_MAX_WAIT_S)))
            if target is None:
                fields.append({**row, "ok": False, **(failure or {"error": "target_not_found"})})
                continue
            kind = _fill_kind(target)
            element_id = str(target.get("element_id") or "")
            row.update(kind=kind, element_id=element_id)
            if kind in {"checkbox", "radio"}:
                wanted = _fill_truthy(value)
                state = post_page_js(_element_effect_state_js(element_id))
                checked = state.get("checked") if state.get("checked") is not None else target.get("checked")
                if bool(checked) == wanted or (kind == "radio" and not wanted):
                    fields.append({**row, "ok": True, "unchanged": True, "checked": bool(checked)})
                    continue
                note_possible_navigation()
                step = {"type": "click", "element_id": element_id}
            elif kind == "select":
                note_possible_navigation()
                step = {"type": "select", "element_id": element_id, "option": str(value),
                        "_widget_check": target.get("ready") is not True, "_list_cover_ok": True}
            else:
                step = {"type": "type", "element_id": element_id, "text": str(value), "_list_cover_ok": True}
                if suggestible(target):
                    suggesting.add(element_id)
                    post_page_js(_fill_suggestion_js(element_id, value, "before"))
            mutation_dispatched = True
            done = apply(kind, step)
            if not done.get("ok") and last_filled and _fill_occluded(done):
                # Nothing was sent to a covered field. The usual cover is a picker the previous
                # field left open (a date calendar, an autocomplete list): Escape closes it.
                key_target, blocked = revalidate_mutation("key")
                if blocked is None:
                    closed = _dom_key_action(settings, browser, {"key": "Escape"}, last_filled,
                                             window_index, tab_index, tab_handle, prevalidated_target=key_target)
                    internal_js_calls += int(closed.pop("_js_calls", 0))
                    row["closed_popup_with_escape"] = True
                    done = apply(kind, step)
            if kind == "select" and done.get("error") == "option_not_found" and not done.get("native"):
                # The list this field opened stays open over the fields after it; close it as a person would.
                key_target, blocked = revalidate_mutation("key")
                if blocked is None:
                    closed = _dom_key_action(settings, browser, {"key": "Escape"}, None,
                                             window_index, tab_index, tab_handle, prevalidated_target=key_target)
                    internal_js_calls += int(closed.pop("_js_calls", 0))
            ok = bool(done.get("ok")) or (kind in {"checkbox", "radio"} and done.get("error") == "action_no_effect")
            changed = changed or ok
            if ok:
                last_filled = element_id
            if ok:
                # What the field holds now, read back by the action itself, so the caller can verify the
                # whole form from this result instead of observing it again. A password is never echoed.
                if kind in {"checkbox", "radio"}:
                    row["checked"] = wanted
                elif kind == "select" and done.get("selected") is not None:
                    row["value"] = str(done.get("selected"))[:200]
                elif str(target.get("input_type") or "").lower() == "password":
                    row["value_set"] = True
                elif done.get("value") is not None:
                    row["value"] = str(done.get("value"))[:200]
            if ok and kind == "text" and suggestible(target):
                suggestion = pick_suggestion(element_id, value) or {"plain": True}
                if suggestion.get("plain"):
                    pass  # no list came: an ordinary text box
                elif suggestion.get("chosen"):
                    row.update(value=str(suggestion.get("picked") or value)[:200], chosen_from_list=True)
                    if suggestion.get("in_box"):
                        chosen_in_box.add(element_id)
                elif suggestion.get("pressed"):
                    ok = False
                    row.pop("value", None)
                    row["picked"] = suggestion.get("picked")
                    done = {"error": "choice_not_kept", "reason_code": "FILL_CHOICE_NOT_KEPT"}
                elif "chosen" not in suggestion:
                    ok = False
                    done = suggestion
                elif suggestion.get("no_such_option"):
                    ok = False
                    row.pop("value", None)
                    row["options"] = suggestion.get("options") or []
                    done = {"error": "option_not_found", "reason_code": "FILL_NO_SUCH_OPTION"}
                else:
                    # Typed text stands where the list holds no such option (an address box suggests,
                    # it does not insist); whether the page kept it is read once every field is done.
                    row.update(suggestion_not_chosen=True, options=suggestion.get("options") or [],
                               **({"ambiguous": True} if suggestion.get("ambiguous") else {}))
            fields.append({**row, "ok": ok, **({key: done.get(key) for key in ("error", "reason_code", "hint") if done.get(key)}
                                                 if not ok else {})})
        typed = [row for row in fields if row.get("ok") and row.get("element_id") in suggesting and row.get("value")
                 and (not row.get("chosen_from_list") or row["element_id"] in chosen_in_box)]
        if typed:
            # A search box that takes only a choice from its list clears typed text once the box is left,
            # also when a press on its option did not register.
            ids = [row["element_id"] for row in typed]
            kept = mutating_page_js(_fill_kept_js(ids, ids))
            if kept.get("left"):
                cancellable_sleep(0.25)
                kept = post_page_js(_fill_kept_js(ids, []))
            values_now = kept.get("values") if isinstance(kept.get("values"), dict) else {}
            for row in typed:
                now_value = values_now.get(row["element_id"])
                if now_value is None or str(now_value).strip():
                    continue
                row.pop("value", None)
                row.pop("chosen_from_list", None)
                row["ok"] = False
                if row.get("options"):
                    row.update(error="option_not_found", reason_code="FILL_NEEDS_LIST_CHOICE")
                else:
                    row.update(error="value_not_kept", reason_code="FILL_VALUE_NOT_KEPT")
        in_flight = []
        failed = [row["field"] for row in fields if not row.get("ok")]
        out: Dict[str, Any] = {
            "type": "fill", "ok": not failed, "filled": sum(1 for row in fields if row.get("ok") and not row.get("unchanged")),
            "fields": fields,
        }
        if failed:
            known = all(str(row.get("error") or "") in _KNOWN_FAILURE_ERRORS for row in fields if not row.get("ok"))
            if known:
                out["outcome_unknown"] = False
            out.update(error="fill_incomplete", reason_code="FILL_INCOMPLETE", failed_fields=failed,
                       automatic_retry=False, observe_again=False,
                       hint="The other fields were filled. Fix only these: check the label words (near_matches), "
                            "pick a value from a field's options, or open the part of the form that shows "
                            "them, then fill just those.")
        return out

    tab_lost: Optional[Dict[str, Any]] = None
    typ = ""
    # When each action began (epoch ms): expect download matches files from the start of its action.
    action_started_ms: Optional[int] = None
    # #391: a url_change wait (or expect) compares with the URL just before the action it follows, not with
    # the URL the whole batch started on: an earlier step's navigation is not the later step's effect.
    url_baselines: Dict[int, str] = {}
    try:
        for position, action in enumerate(actions):
            if not isinstance(action, dict):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each action must be an object.")
            previous_started_ms, action_started_ms = action_started_ms, int(time.time() * 1000)
            typ = str(action.get("type") or "").lower().replace("-", "_")
            following = actions[position + 1] if position + 1 < len(actions) else None
            if (
                typ != "wait" and isinstance(following, dict)
                and str(following.get("type") or "").lower() == "wait"
                and str(following.get("for") or following.get("condition") or "").lower().strip() == "url_change"
            ):
                if not flush_pending():
                    break
                baseline = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index, tab_handle)
                internal_js_calls += 1
                url_baselines[position + 1] = str(baseline.get("url") or initial_url)
            in_flight = []
            resolved_target: Optional[Dict[str, Any]] = None
            work_action = dict(action)
            if _is_point_action(action):
                if not flush_pending():
                    break
                point_target, blocked = revalidate_mutation(typ)
                if blocked is not None:
                    results.append(blocked)
                    break
                point_result = _point_action(
                    settings, browser, action, observation_id, window_index, tab_index, tab_handle, point_target,
                )
                internal_js_calls += int(point_result.pop("_js_calls", 0))
                results.append(point_result)
                current_observation_id = None
                if not point_result.get("ok"):
                    break
                mutation_dispatched = True
                continue
            if typ == "autocomplete_many":
                if not flush_pending():
                    break
                in_flight = [typ]
                mutation_dispatched = True
                auto_result = _autocomplete_many_action(
                    settings, browser, action, window_index, tab_index, tab_handle, revalidate_mutation)
                internal_js_calls += int(auto_result.pop("_js_calls", 0))
                results.append(auto_result)
                current_observation_id = None
                in_flight = []
                if not auto_result.get("ok"):
                    break
                continue
            if typ in {"network", "pdf"}:
                if not flush_pending():
                    break
                in_flight = [typ]
                page_result = (
                    _network_action(settings, browser, action, window_index, tab_index, tab_handle)
                    if typ == "network" else _pdf_action(settings, browser, window_index, tab_index, tab_handle)
                )
                internal_js_calls += int(page_result.pop("_js_calls", 0))
                results.append(page_result)
                in_flight = []
                if not page_result.get("ok"):
                    break
                continue
            if typ == "fill":
                if not flush_pending():
                    break
                fill_result = run_fill(action)
                results.append(fill_result)
                current_observation_id = None
                if not fill_result.get("ok"):
                    break
                continue
            if typ == "post":
                if not flush_pending():
                    break
                post_result = run_post(action)
                results.append(post_result)
                current_observation_id = None
                in_flight = []
                if not post_result.get("ok"):
                    break
                continue
            if typ not in {"wait", "key", "keyboard", "shortcut", "extract", "scan", "read", "dialog"} or (
                typ == "scan" and not action.get("element_id") and _has_locator(action)
            ):
                work_action, resolved_target = resolve_target(action)
                if isinstance(resolved_target, dict) and resolved_target.get("ok") is False:
                    results.append({"type": typ, **resolved_target})
                    break

            if typ in {"wait", "key", "keyboard", "shortcut", "select", "extract", "scan", "read", "dialog", "hover", "drag",
                       "click", "double_click", "type", "type_text", "paste"}:
                if not flush_pending():
                    break
                in_flight = [typ]
                text_before: Optional[bool] = None
                if action.get("_expect_text"):
                    # Whether the page shows the expected text before the action: its expect proves nothing if so (#89).
                    try:
                        seen = _run_json_js(settings, browser, _text_present_js(action["_expect_text"]),
                                            window_index, tab_index, tab_handle)
                        internal_js_calls += 1
                        if isinstance(seen.get("present"), bool):
                            text_before = seen["present"]
                    except HTTPException:
                        text_before = None  # could not look: the expect is judged as before
                if typ in {"click", "double_click", "type", "type_text", "paste"}:
                    if typ in {"click", "double_click"}:
                        note_possible_navigation()
                    dispatched_before = mutation_dispatched
                    mutation_dispatched = True
                    action_result = _verified_dom_action(
                        settings, browser, work_action, current_observation_id,
                        window_index, tab_index, tab_handle,
                        mutation_revalidator=revalidate_mutation,
                    )
                    internal_js_calls += int(action_result.pop("_js_calls", 0))
                    if action_result.get("no_side_effect"):
                        # The page refused the remembered target before acting on it.
                        mutation_dispatched = dispatched_before
                    rebind = (
                        _stale_target_locator(browser, tab_handle, action, action_result)
                        if resolved_target is None else None
                    )
                    if rebind is not None:
                        relocated, rebind_via = rebind
                        # The page re-rendered the target: find it again once on the current page,
                        # by the caller's own locator or by what observe/find saw it as.
                        mutation_dispatched = dispatched_before
                        retry_action, retry_target = (
                            (relocated, None) if rebind_via == "observed_identity_not_unique" else resolve_target(relocated)
                        )
                        refusal = _stale_rebind_refusal(action, rebind_via, retry_target)
                        if refusal is not None:
                            results.append({"type": typ, **refusal})
                            break
                        current_observation_id = None
                        mutation_dispatched = True
                        stale_result = action_result
                        work_action, resolved_target = retry_action, retry_target
                        action_result = _verified_dom_action(
                            settings, browser, work_action, None,
                            window_index, tab_index, tab_handle,
                            mutation_revalidator=revalidate_mutation,
                        )
                        internal_js_calls += int(action_result.pop("_js_calls", 0))
                        if action_result.get("no_side_effect"):
                            mutation_dispatched = dispatched_before
                        action_result["re_resolved"] = {
                            "stale_element_id": action.get("element_id"),
                            "reason_code": stale_result.get("reason_code"),
                            **({"via": rebind_via} if rebind_via != "caller_locator" else {}),
                        }
                    action_result.pop("_compact_state", None)
                    if resolved_target:
                        action_result["resolved_target"] = {
                            k: resolved_target.get(k)
                            for k in ("element_id", "text", "role", "tag", "confidence", "within_up")
                        }
                        if resolved_target.get("decision"):
                            action_result["resolved_target"]["decision"] = resolved_target["decision"]
                    if (
                        typ in {"click", "double_click"} and action_result.get("error") == "action_no_effect"
                        and tabs_before is not None
                    ):
                        _credit_opened_tab(browser, action_result, tabs_before, tab_handle)
                    if text_before is not None:
                        action_result["_expect_text_present_before"] = text_before
                    results.append(action_result)
                    # A click whose result shows nowhere on the page (a download, a copy) is judged by its
                    # expect when it has one, not stopped as having no effect.
                    if not action_result.get("ok") and not (
                        action.get("_expect_follows") and action_result.get("error") == "action_no_effect"
                    ):
                        break
                    current_observation_id = None
                elif typ == "dialog":
                    dialog_result = _dialog_action(browser, action, tab_handle)
                    results.append(dialog_result)
                    current_observation_id = None
                    if dialog_result.get("ok"):
                        mutation_dispatched = True
                    else:
                        break
                elif typ in {"hover", "drag"}:
                    gesture_target, blocked = revalidate_mutation(typ)
                    if blocked is not None:
                        results.append(blocked)
                        break
                    gesture_result = _gesture_action(
                        settings, browser, typ, work_action, action, window_index, tab_index, tab_handle,
                        resolve_target=resolve_target, native_target=gesture_target,
                    )
                    internal_js_calls += int(gesture_result.pop("_js_calls", 0))
                    if gesture_result.get("steps_done"):
                        mutation_dispatched = True
                    results.append(gesture_result)
                    current_observation_id = None
                    if not gesture_result.get("ok"):
                        break
                elif typ == "scan":
                    scan_result = _scan_action(
                        settings, browser, work_action, window_index, tab_index, tab_handle,
                    )
                    internal_js_calls += int(scan_result.pop("_js_calls", 0))
                    results.append(scan_result)
                    current_observation_id = None
                    if not scan_result.get("ok"):
                        break
                elif typ == "read":
                    read_result = _read_action(
                        settings, browser, action, window_index, tab_index, tab_handle,
                    )
                    internal_js_calls += int(read_result.pop("_js_calls", 0))
                    results.append(read_result)
                    current_observation_id = None
                    if not read_result.get("ok"):
                        break
                elif typ == "extract":
                    extract_result = _extract_action(
                        settings, browser, action, window_index, tab_index, tab_handle,
                    )
                    internal_js_calls += int(extract_result.pop("_js_calls", 0))
                    results.append(extract_result)
                    if not extract_result.get("ok"):
                        break
                elif typ == "select":
                    note_possible_navigation()
                    dispatched_before = mutation_dispatched
                    mutation_dispatched = True
                    select_result = _select_action(
                        settings,
                        browser,
                        work_action,
                        current_observation_id,
                        window_index,
                        tab_index,
                        tab_handle,
                        mutation_revalidator=revalidate_mutation,
                    )
                    internal_js_calls += int(select_result.pop("_js_calls", 0))
                    if select_result.get("no_side_effect"):
                        mutation_dispatched = dispatched_before
                    if resolved_target:
                        select_result["resolved_target"] = {
                            k: resolved_target.get(k)
                            for k in ("element_id", "text", "role", "tag", "confidence", "within_up")
                        }
                        if resolved_target.get("decision"):
                            select_result["resolved_target"]["decision"] = resolved_target["decision"]
                    if text_before is not None:
                        select_result["_expect_text_present_before"] = text_before
                    results.append(select_result)
                    if not select_result.get("ok"):
                        break
                    current_observation_id = None
                elif typ == "wait":
                    if action.get("for") == "download":
                        wait_result = _download_wait(settings, action, previous_started_ms)
                    else:
                        wait_result = _wait_action(
                            settings,
                            browser,
                            action,
                            window_index,
                            tab_index,
                            url_baselines.get(position, initial_url),
                            tab_handle,
                        )
                    wait_result.pop("_compact_state", None)
                    internal_js_calls += int(wait_result.pop("_js_calls", 0))
                    if "_expect_for" in action:
                        wait_result["_expect_for"] = action["_expect_for"]
                        wait_result["_expect"] = action.get("_expect")
                        expected_text = (action.get("_expect") or {}).get("text")
                        if wait_result.get("matched") and expected_text:
                            # The text shows; say whether the page also keeps it itself (#81).
                            try:
                                copy = _run_json_js(settings, browser, _local_copy_js(expected_text),
                                                    window_index, tab_index, tab_handle)
                                internal_js_calls += 1
                                wait_result["_local_copy"] = list(copy.get("local_copy") or [])
                            except HTTPException:
                                wait_result["_local_copy"] = None  # could not look: nothing is claimed
                    results.append(wait_result)
                    if not wait_result.get("matched") and action.get("required", True):
                        break
                else:
                    key_action = dict(action)
                    if not key_action.get("element_id") and any(key_action.get(k) for k in ("query", "target", "role", "text_match", "target_text")):
                        key_action, resolved_target = resolve_target(key_action)
                        if isinstance(resolved_target, dict) and resolved_target.get("ok") is False:
                            results.append({"type": "key", **resolved_target})
                            break
                    eid = key_action.get("element_id")
                    key_mode = str(action.get("input_mode") or "auto").strip().lower()
                    if key_mode == "trusted" and _norm_browser(browser) == "Google Chrome":
                        # Canvas apps listen for real key events; the companion sends them in the background.
                        mutation_target, blocked = revalidate_mutation("key")
                        if blocked is not None:
                            results.append(blocked)
                            break
                        mutation_dispatched = True
                        key_result = _trusted_key_action(browser, action, tab_handle, mutation_target)
                        results.append(key_result)
                        current_observation_id = None
                        if not key_result.get("ok"):
                            break
                        continue
                    if key_mode == "dom" or (key_mode == "auto" and not allow_foreground):
                        # Background-safe default: DOM key events never change app focus.
                        mutation_target, blocked = revalidate_mutation("key")
                        if blocked is not None:
                            results.append(blocked)
                            break
                        note_possible_navigation()
                        mutation_dispatched = True
                        key_result = _dom_key_action(
                            settings, browser, action, eid, window_index, tab_index, tab_handle,
                            prevalidated_target=mutation_target,
                        )
                        internal_js_calls += int(key_result.pop("_js_calls", 0))
                        key_result.pop("_compact_state", None)
                        if resolved_target:
                            key_result["resolved_target"] = {
                                k: resolved_target.get(k)
                                for k in ("element_id", "text", "role", "tag", "confidence", "within_up")
                            }
                        results.append(key_result)
                        if not key_result.get("ok"):
                            break
                        current_observation_id = None
                        continue
                    if eid:
                        mutation_target, blocked = revalidate_mutation("key")
                        if blocked is not None:
                            results.append(blocked)
                            break
                        mutation_dispatched = True
                        focus_result = _run_json_js(
                            settings, browser, _batch_js([{"type": "focus", "element_id": eid}], current_observation_id),
                            window_index, tab_index, tab_handle,
                            prevalidated_target=mutation_target,
                        )
                        internal_js_calls += 1
                        if not focus_result.get("ok"):
                            results.extend(focus_result.get("actions") or [{"ok": False, "error": "could_not_focus"}])
                            break
                        current_observation_id = None
                    note_possible_navigation()
                    mutation_dispatched = True
                    key_result = browser_press_key(
                        settings, browser=browser, key=str(action.get("key") or ""),
                        modifiers=action.get("modifiers") or [], window_index=window_index,
                        tab_handle=tab_handle, lease_generation=lease_generation,
                        allow_foreground=allow_foreground,
                    )
                    results.append({
                        "type": "key",
                        "ok": bool(key_result.get("ok")),
                        "key": action.get("key"),
                        "foreground_required": bool(key_result.get("foreground_required")),
                        "reason_code": key_result.get("reason_code"),
                        "reason": key_result.get("reason"),
                        "tab_handle": key_result.get("tab_handle") or tab_handle,
                        "lease_generation": key_result.get("lease_generation"),
                    })
                    if not key_result.get("ok"):
                        break
            else:
                pending.append(work_action)
        else:
            flush_pending()
        if pending:
            flush_pending()
    except HTTPException as exc:
        tab_lost = _tab_loss_detail(exc)
        if tab_lost is None:
            raise
        # Earlier actions already ran; report them instead of failing the whole call so
        # the agent never replays completed side effects.
        mutated = any(item not in _NON_MUTATING_ACT_TYPES for item in in_flight)
        results.append({
            "type": in_flight[0] if len(in_flight) == 1 else ("batch" if in_flight else typ),
            **({"in_flight_types": in_flight} if len(in_flight) > 1 else {}),
            "ok": False,
            **tab_lost,
            "retryable": True,
            "automatic_retry": False,
            "observe_again": True,
            "outcome_unknown": mutated,
            "resource_kind": "browser_tab",
            "tab_handle": tab_handle,
        })

    results = _fold_expectations(results)
    for item in results:
        if isinstance(item, dict):
            item["outcome"] = _action_outcome(item)
    ok = all(bool(r.get("ok")) for r in results) if results else True
    response: Dict[str, Any] = {
        "ok": ok,
        "actions": results,
        "action_count": len(actions),
        "internal_js_calls": internal_js_calls,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "mutation_dispatched": mutation_dispatched,
    }
    if not ok:
        failed = next(
            (item for item in results if isinstance(item, dict) and item.get("ok") is False),
            None,
        )
        if (
            failed is not None and failed.get("outcome_unknown") is None
            and str(failed.get("error") or "") in _KNOWN_FAILURE_ERRORS
        ):
            failed["outcome_unknown"] = False
        if failed is not None:
            for key in (
                "error", "reason_code", "retryable", "automatic_retry", "observe_again",
                "human_priority", "yielded", "human_takeover_during_action",
                "human_input_recent", "human_input_age_ms", "human_input_probe_error",
                "resource_kind", "expected_lease_generation", "actual_lease_generation", "outcome_unknown",
            ):
                if key in failed:
                    response[key] = failed.get(key)
    if tab_lost is not None:
        response["outcome_unknown"] = bool(results[-1].get("outcome_unknown"))
        response["completed_action_count"] = sum(
            1 for item in results if isinstance(item, dict) and item.get("ok")
        )
        response["state_error"] = tab_lost["error"]
        return response
    if response.get("human_priority"):
        # The person has the tab now: run nothing more in it, not even a read.
        return response
    try:
        summary: Optional[Dict[str, Any]] = None
        if return_state in {"compact", "none"}:
            # One read gives the light state and what the batch changed, so the
            # caller rarely needs another observe to verify or continue.
            summary = _run_json_js(
                settings, browser, _changes_js(observation_id), window_index, tab_index, tab_handle,
            )
            response["internal_js_calls"] += 1
            animating_ms = int(summary.get("animating_ms") or 0)
            if animating_ms:
                # A panel the batch opened is still animating in; its controls are not laid out yet.
                cancellable_sleep(min(animating_ms, 600) / 1000 + 0.03)
                summary = _run_json_js(
                    settings, browser, _changes_js(observation_id), window_index, tab_index, tab_handle,
                )
                response["internal_js_calls"] += 1
            if isinstance(summary.get("changes"), dict):
                response["changes"] = summary["changes"]
        if return_state == "compact":
            response["state"] = {key: summary.get(key) for key in ("url", "title", "dom_revision")}
        elif return_state == "full":
            try:
                full = _observe_payload(
                    settings,
                    browser,
                    "content",
                    120,
                    window_index=window_index,
                    tab_index=tab_index,
                    tab_handle=tab_handle,
                )
                response["state"] = full
                response["internal_js_calls"] += 1
            except HTTPException as exc:
                if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
                    raise
                response["state"] = _run_json_js(
                    settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
                )
                response["state_fallback"] = "compact"
                response["full_state_error"] = "payload_too_large"
                response["internal_js_calls"] += 1
        if return_state == "none":
            progress = summary or {}
            response["progress"] = {
                key: progress.get(key) for key in ("url", "title", "dom_revision")
                if progress.get(key) is not None
            }
    except HTTPException as exc:
        state_lost = _tab_loss_detail(exc)
        if state_lost is None:
            raise
        # The last action may itself have closed the tab; the actions still completed.
        response["state_error"] = state_lost["error"]
    return response


def _in_frame(frame: Optional[str], call: Callable[[], Any]) -> Any:
    """Run one browser operation inside a child frame and say which frame answered."""
    if not str(frame or "").strip():
        return call()
    with frame_scope(frame):
        out = call()

    def tag(payload: Dict[str, Any]) -> Dict[str, Any]:
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        payload["frame"] = {"selector": str(frame).strip(), "url": payload.get("url") or progress.get("url")}
        return payload

    # Observations come back as compact JSON text (or [text, image]); actions as a dict.
    if isinstance(out, dict):
        return tag(out)
    if isinstance(out, str) or (isinstance(out, list) and out and isinstance(out[0], str)):
        text = out if isinstance(out, str) else out[0]
        try:
            payload = json.loads(text)
        except ValueError:
            return out
        if not isinstance(payload, dict):
            return out
        tagged = json.dumps(tag(payload), ensure_ascii=False, separators=(",", ":"))
        return tagged if isinstance(out, str) else [tagged, *out[1:]]
    return out


def _safari_frame_call(browser: str, frame: Optional[str], tab_handle: Optional[str], window_index: int,
                       tab_index: Optional[int], call: Callable[[browser_tabs.TabTarget, str], Dict[str, Any]],
                       mutation: bool = False) -> Optional[Dict[str, Any]]:
    """Safari has no script access to a cross-origin child frame; its Accessibility tree has the frame (#384).
    None when this is not a Safari frame call."""
    selector = str(frame or "").strip()
    b = _norm_browser(browser)
    if not selector or b != "Safari":
        return None
    if mutation:
        _require_stable_handle_for_mutation(b, tab_handle, window_index, "browser_act")
    # An observation may (re)bind the tab to this agent, as a page observation does; an action may not.
    with _tab_lease(b, tab_handle, window_index, tab_index, allow_rebind=not mutation, mutation=mutation) as target:
        if not target.active:
            payload = safari_frames.unavailable(
                "Safari shows this tab's frames to Accessibility only while the tab is the one its window shows.",
                reason="frame_tab_not_shown", hint="Ask the owner to show this tab in its window (Show Tab in Mac MCP), "
                "then observe again; Safari itself stays in the background.")
        else:
            payload = call(target, selector)
        payload["frame"] = {"selector": selector, "url": payload.get("url"), "transport": "accessibility"}
        payload["tab_handle"] = target.tab_handle
        return payload


def browser_observe(settings: Settings, browser: str, window_index: int = 1, tab_index: Optional[int] = None,
                    tab_handle: Optional[str] = None, scope: str = "interactive",
                    max_elements: int = _DEFAULT_OBSERVE_ELEMENTS, visual: str = "none",
                    element_id: Optional[str] = None, previous_observation_id: Optional[str] = None,
                    frame: Optional[str] = None) -> Any:
    """Compact DOM observation; frame='<url part>' observes inside that child frame (Chrome's companion,
    Safari's Accessibility tree)."""
    if frame and str(visual or "none").lower() != "none":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {
            "error": "frame_visual_unsupported",
            "message": "Visual capture is of the whole page; observe the frame with visual='none'.",
        })
    safari = _safari_frame_call(browser, frame, tab_handle, window_index, tab_index,
                                lambda target, selector: safari_frames.observe(target.url, selector, max_elements))
    if safari is not None:
        return json.dumps(safari, ensure_ascii=False, separators=(",", ":"))
    return _in_frame(frame, lambda: _browser_observe_impl(
        settings, browser, window_index, tab_index, tab_handle, scope, max_elements, visual, element_id,
        previous_observation_id,
    ))


def browser_find(settings: Settings, browser: str, query: str, role: Optional[str] = None, text: Optional[str] = None,
                 window_index: int = 1, tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                 max_results: int = 5, actionable_only: bool = False, wait_timeout_s: float = 0.0,
                 within: Optional[str] = None, within_element_id: Optional[str] = None,
                 within_levels: Optional[int] = None, frame: Optional[str] = None) -> Dict[str, Any]:
    """Find a rendered target; frame='<url part>' searches inside that child frame (Chrome)."""
    return _in_frame(frame, lambda: _browser_find_impl(
        settings, browser, query, role, text, window_index, tab_index, tab_handle, max_results, actionable_only,
        wait_timeout_s, within, within_element_id, within_levels,
    ))


def browser_act(settings: Settings, browser: str, actions: List[Dict[str, Any]], observation_id: Optional[str] = None,
                window_index: int = 1, tab_index: Optional[int] = None, tab_handle: Optional[str] = None,
                return_state: str = "compact", allow_foreground: bool = False,
                frame: Optional[str] = None) -> Dict[str, Any]:
    """Batched actions; frame='<url part>' acts inside that child frame with its element ids (Chrome's
    companion, Safari's Accessibility tree)."""
    safari = _safari_frame_call(
        browser, frame, tab_handle, window_index, tab_index, mutation=True,
        call=lambda target, selector: safari_frames.act(target.url, selector, actions, observation_id, return_state))
    if safari is not None:
        return safari
    return _in_frame(frame, lambda: _browser_act_impl(
        settings, browser, actions, observation_id, window_index, tab_index, tab_handle, return_state, allow_foreground,
    ))
