from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from mcp.server.fastmcp.utilities.types import Image

from .security import Settings
from .tools_browser import (
    _execute_js_for_target,
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
_VISUAL_ENSURE_CACHE: Dict[Tuple[str, str, str], float] = {}
_VISUAL_ENSURE_TTL_S = 12.0
_DOM_RASTERIZER_PATH = Path(__file__).resolve().parent / "vendor" / "html2canvas.min.js"
_DOM_CAPTURE_STATE_PREFIX = "__macMcpVisualCapture"
_DOM_RASTERIZER_GLOBAL = "__macMcpHtml2Canvas"
_DOM_CAPTURE_VIEWPORT_TIMEOUT_S = 18.0
_DOM_CAPTURE_FULL_PAGE_TIMEOUT_S = 30.0
_DOM_CAPTURE_MAX_CSS_HEIGHT = 20_000
_DOM_CAPTURE_MAX_DATA_URL_CHARS = 1_800_000
_RENDER_READINESS_TIMEOUT_S = 1.5
_RENDER_READINESS_POLL_S = 0.08
_RENDER_READINESS_STABLE_MS = 120
_ELEMENT_READINESS_TIMEOUT_S = 0.8
_ELEMENT_READINESS_POLL_S = 0.06
_ELEMENT_READINESS_STABLE_MS = 300
_ACTION_VERIFY_TIMEOUT_S = 0.55
_ACTION_VERIFY_POLL_S = 0.07
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
) -> Dict[str, Any]:
    raw = browser_execute_js(
        settings,
        browser=browser,
        js=js,
        window_index=window_index,
        tab_index=tab_index,
        tab_handle=tab_handle,
    )
    return _decode_js_payload(raw)


def _ensure_visual_companion(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
) -> bool:
    """Ensure the optional Visual Companion exists once per live tab document."""
    b = _norm_browser(browser)
    try:
        with _tab_lease(b, tab_handle, window_index, tab_index, allow_rebind=True) as target:
            key = (b, str(target.native_id or target.tab_handle), str(target.url or ""))
            now = time.monotonic()
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
            source = _DOM_RASTERIZER_PATH.read_text(encoding="utf-8")
            _execute_js_for_target(b, pre_raw, target, timeout_s=10)
            _execute_js_for_target(b, source, target, timeout_s=30)
            loaded = _execute_js_for_target(b, post_raw, target, timeout_s=10)
        else:
            path_literal = json.dumps(str(_DOM_RASTERIZER_PATH))
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
        time.sleep(_RENDER_READINESS_POLL_S)


def _dom_capture_start_js(
    mode: str,
    element_id: Optional[str],
    state_key: Optional[str] = None,
) -> str:
    state_key = state_key or f"{_DOM_CAPTURE_STATE_PREFIX}_{uuid.uuid4().hex}"
    mode_js = json.dumps(mode)
    element_js = json.dumps(element_id)
    return f'''(function(){{
var h2c=window.{_DOM_RASTERIZER_GLOBAL};
var stateKey={json.dumps(state_key)};
if(typeof h2c!=="function") return JSON.stringify({{ok:false,error:"rasterizer_unavailable"}});
var mode={mode_js}, elementId={element_js};
var agent=window.__macMcpBrowserAgent;
var target=document.documentElement;
if(mode==="element"){{
  target=agent&&agent.elements&&elementId?agent.elements[elementId]:null;
  if(!target||!target.isConnected) return JSON.stringify({{ok:false,error:"element_not_available",element_id:elementId}});
}}
var de=document.documentElement, body=document.body||de;
var fullW=Math.max(Number(de.scrollWidth||0),Number(de.clientWidth||0),Number(body.scrollWidth||0),Number(body.clientWidth||0),Number(innerWidth||0));
var fullH=Math.max(Number(de.scrollHeight||0),Number(de.clientHeight||0),Number(body.scrollHeight||0),Number(body.clientHeight||0),Number(innerHeight||0));
var rect=mode==="element"?target.getBoundingClientRect():null;
var rawW=mode==="element"?Number(rect.width||0):(mode==="viewport"?Number(innerWidth||0):fullW);
var rawH=mode==="element"?Number(rect.height||0):(mode==="viewport"?Number(innerHeight||0):fullH);
if(!isFinite(rawW)||!isFinite(rawH)||rawW<=0||rawH<=0) return JSON.stringify({{ok:false,error:"render_not_ready",reason_code:mode==="element"?"ELEMENT_ZERO_BOUNDS":"ZERO_CONTENT_BOUNDS",retryable:true,raw_width:rawW,raw_height:rawH}});
var sourceW=Math.max(1,Math.ceil(rawW));
var actualH=Math.max(1,Math.ceil(rawH));
var sourceH=mode==="full_page"?Math.min(actualH,{_DOM_CAPTURE_MAX_CSS_HEIGHT}):actualH;
var truncated=mode==="full_page"&&actualH>sourceH;
var pixelBudget=7500000;
var maxOutputWidth=mode==="viewport"?1100:1280;
var scale=Math.min(1,maxOutputWidth/sourceW,Math.sqrt(pixelBudget/Math.max(1,sourceW*sourceH)));
scale=Math.max(0.20,scale);
var bg=getComputedStyle(de).backgroundColor;
if(!bg||bg==="rgba(0, 0, 0, 0)"||bg==="transparent") bg=getComputedStyle(body).backgroundColor;
if(!bg||bg==="rgba(0, 0, 0, 0)"||bg==="transparent") bg="#ffffff";
var started=Date.now();
window[stateKey]={{status:"running",meta:{{mode:mode,capture_method:"dom_rasterizer",background_safe:true,tab_activated:false,disk_write:false,source_width:sourceW,source_height:sourceH,actual_height:actualH,truncated:truncated,scale:scale}}}};
var opts={{
  logging:false,useCORS:true,allowTaint:false,imageTimeout:mode==="full_page"?1500:700,removeContainer:true,
  foreignObjectRendering:false,backgroundColor:bg,scale:scale,
  windowWidth:innerWidth,windowHeight:innerHeight,
  scrollX:mode==="full_page"?0:window.scrollX,
  scrollY:mode==="full_page"?0:window.scrollY,
  ignoreElements:function(el){{
    if(mode!=="viewport") return false;
    try{{
      var r=el.getBoundingClientRect();
      return r.bottom < -120 || r.top > innerHeight+120 || r.right < -120 || r.left > innerWidth+120;
    }}catch(e){{return false;}}
  }},
  onclone:function(doc){{
    try{{
      var st=doc.createElement("style");
      st.textContent="*,*::before,*::after{{animation:none!important;transition:none!important;caret-color:transparent!important;}}";
      (doc.head||doc.documentElement).appendChild(st);
    }}catch(e){{}}
  }}
}};
if(mode==="viewport"){{opts.x=window.scrollX;opts.y=window.scrollY;opts.width=sourceW;opts.height=sourceH;}}
if(mode==="full_page"){{opts.x=0;opts.y=0;opts.width=sourceW;opts.height=sourceH;}}
h2c(target,opts).then(function(canvas){{
  try{{
    var output=canvas;
    var data=output.toDataURL("image/jpeg",0.58);
    var limit={_DOM_CAPTURE_MAX_DATA_URL_CHARS};
    if(data.length>limit&&output.width>320&&output.height>240){{
      var factor=Math.max(0.35,Math.min(0.92,Math.sqrt(limit/data.length)*0.90));
      var resized=document.createElement("canvas");
      resized.width=Math.max(1,Math.round(output.width*factor));
      resized.height=Math.max(1,Math.round(output.height*factor));
      var ctx=resized.getContext("2d",{{alpha:false}});
      ctx.fillStyle=bg;ctx.fillRect(0,0,resized.width,resized.height);
      ctx.drawImage(output,0,0,resized.width,resized.height);
      output=resized;
      data=output.toDataURL("image/jpeg",0.52);
    }}
    var meta=window[stateKey].meta;
    meta.output_width=output.width;meta.output_height=output.height;meta.elapsed_ms=Date.now()-started;meta.data_url_chars=data.length;
    window[stateKey]={{status:"done",meta:meta,data:data}};
  }}catch(e){{window[stateKey]={{status:"error",error:String(e&&e.message||e)}};}}
}}).catch(function(e){{window[stateKey]={{status:"error",error:String(e&&e.message||e)}};}});
return JSON.stringify({{ok:true,status:"running",mode:mode,source_width:sourceW,source_height:sourceH,scale:scale,truncated:truncated}});
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
        status_js = (
            f"(function(){{var s=window[{state_key_js}];"
            "return JSON.stringify(s?{status:s.status,error:s.error||'',meta:s.meta||{},data_length:s.data?s.data.length:0}:{status:'missing'});})()"
        )
        finished: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            raw = _execute_js_unbounded(
                browser,
                status_js,
                window_index=window_index,
                tab_index=tab_index,
                tab_handle=tab_handle,
                timeout_s=10,
            )
            try:
                finished = json.loads(raw or "{}")
            except json.JSONDecodeError:
                finished = {}
            state = str(finished.get("status") or "")
            if state == "done":
                break
            if state in {"error", "missing"}:
                return None, str(finished.get("error") or f"DOM screenshot state became {state}."), meta
            time.sleep(0.12)
        else:
            return None, f"DOM screenshot timed out after {timeout_s:.0f}s.", meta

        if isinstance(finished.get("meta"), dict):
            meta.update(finished["meta"])
        data_url = _execute_js_unbounded(
            browser,
            f"(window[{state_key_js}]&&window[{state_key_js}].data)||''",
            window_index=window_index,
            tab_index=tab_index,
            tab_handle=tab_handle,
            timeout_s=30,
        )
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


def _browser_state_bootstrap() -> str:
    return r'''
function __mcpInternalHost(el){try{return !!el&&el.id==='mac-mcp-visual-companion-root';}catch(e){return false;}}
function __mcpRoots(){
  var roots=[],seen=new Set();
  function visit(root,depth){
    if(!root||seen.has(root)||depth>10)return;seen.add(root);roots.push(root);
    var nodes=[];try{nodes=Array.from(root.querySelectorAll('*'));}catch(e){return;}
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      try{if(el.shadowRoot&&!__mcpInternalHost(el))visit(el.shadowRoot,depth+1);}catch(e){}
      var tag=String(el.tagName||'').toLowerCase();
      if(tag==='iframe'||tag==='frame'){try{if(el.contentDocument)visit(el.contentDocument,depth+1);}catch(e){}}
    }
  }
  visit(document,0);return roots;
}
function __mcpQueryAll(selector){
  var out=[],seen=new Set(),roots=__mcpRoots();
  for(var r=0;r<roots.length;r++){
    var nodes=[];try{nodes=Array.from(roots[r].querySelectorAll(selector));}catch(e){continue;}
    for(var i=0;i<nodes.length;i++){if(!seen.has(nodes[i])){seen.add(nodes[i]);out.push(nodes[i]);}}
  }
  return out;
}
function __mcpQueryOne(selector){var all=__mcpQueryAll(selector);return all.length?all[0]:null;}
function __mcpOwnerWindow(el){try{return (el.ownerDocument&&el.ownerDocument.defaultView)||window;}catch(e){return window;}}
function __mcpStyle(el){try{return __mcpOwnerWindow(el).getComputedStyle(el);}catch(e){return getComputedStyle(el);}}
function __mcpTopRect(el){
  var r=el.getBoundingClientRect(),left=r.left,top=r.top,w=r.width,h=r.height,win=__mcpOwnerWindow(el),guard=0;
  while(win&&win!==window&&guard++<10){var frame=null;try{frame=win.frameElement;}catch(e){}if(!frame)break;var fr=frame.getBoundingClientRect();left+=fr.left;top+=fr.top;win=__mcpOwnerWindow(frame);}
  return {left:left,top:top,right:left+w,bottom:top+h,width:w,height:h};
}
function __mcpStopMutationWatch(s){
  if(s.observerTimer){try{clearTimeout(s.observerTimer);}catch(e){}s.observerTimer=null;}
  var obs=s.rootObservers||[];for(var i=0;i<obs.length;i++){try{obs[i].disconnect();}catch(e){}}s.rootObservers=[];
}
function __mcpStartMutationWatch(s,ttl){
  __mcpStopMutationWatch(s);var roots=__mcpRoots(),bump=function(records){
    for(var j=0;j<records.length;j++){var rec=records[j];if(rec.type==='attributes'&&rec.attributeName==='data-mac-mcp-visual-event')continue;s.mutationRevision+=1;s.lastMutationAt=Date.now();break;}
  };
  for(var i=0;i<roots.length;i++){try{var ob=new MutationObserver(bump);ob.observe(roots[i],{subtree:true,childList:true,attributes:true,characterData:true});s.rootObservers.push(ob);}catch(e){}}
  s.observerTimer=setTimeout(function(){__mcpStopMutationWatch(s);},Math.max(500,Math.min(Number(ttl||3000),8000)));
}
function __mcpState(){
  var s=window.__macMcpBrowserAgent;
  if(!s){var stableAt=Date.now();try{var nav=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];if(document.readyState==='complete'&&nav&&nav.loadEventEnd>0&&performance.now()-nav.loadEventEnd>=300)stableAt=Date.now()-1000;}catch(e){}s=window.__macMcpBrowserAgent={counter:0,ids:new WeakMap(),elements:Object.create(null),pageToken:Math.random().toString(36).slice(2,10),mutationRevision:0,lastMutationAt:stableAt,observations:Object.create(null),rootObservers:[],observerTimer:null};}
  return s;
}
function __mcpVisualTarget(el){
  try{if(!el||el.nodeType!==1)return 'Page';var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase(),type=(el.getAttribute('type')||'').toLowerCase(),aria=(el.getAttribute('aria-label')||'');
    if(tag==='button'||role==='button'||(tag==='input'&&['button','submit','reset'].indexOf(type)>=0))return 'Button';
    if(tag==='a'||role==='link')return 'Link';
    if(tag==='textarea'||el.isContentEditable||role==='textbox'||role==='searchbox'||(tag==='input'&&['checkbox','radio','button','submit','reset'].indexOf(type)<0))return 'Text field';
    if(tag==='select'||role==='combobox'||role==='listbox'||role==='menu'||role==='menuitem')return 'Menu';
    if(type==='checkbox'||role==='checkbox'||role==='switch')return 'Checkbox';if(type==='radio'||role==='radio'||role==='option')return 'Option';if(role==='tab')return 'Tab';
    if(/(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday).*(?:january|february|march|april|may|june|july|august|september|october|november|december)/i.test(aria))return 'Date';return 'Item';
  }catch(e){return 'Item';}}
function __mcpVisual(action,el,effect,ttl,detail){
  try{var payload={seq:Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7),claim:true,phase:'working',action:String(action||'Working').slice(0,40),target_kind:__mcpVisualTarget(el),ttl_ms:Math.max(500,Math.min(Number(ttl||1800),30000))};
    if(el&&el.nodeType===1){var r=__mcpTopRect(el);if(isFinite(r.left)&&isFinite(r.top)&&isFinite(r.width)&&isFinite(r.height)){payload.x=Math.max(0,Math.min(innerWidth,Math.round(r.left+r.width/2)));payload.y=Math.max(0,Math.min(innerHeight,Math.round(r.top+r.height/2)));}}
    if(effect==='click')payload.effect='click';if(['Up','Down','Into view'].indexOf(String(detail||''))>=0)payload.detail=String(detail);
    var raw=btoa(unescape(encodeURIComponent(JSON.stringify(payload))));(document.documentElement||document.body).setAttribute('data-mac-mcp-visual-event',raw);try{window.dispatchEvent(new Event('mac-mcp-visual'));}catch(e){}
  }catch(e){}}
function __mcpId(el,s){var id=s.ids.get(el);if(!id){id='e_'+s.pageToken+'_'+(++s.counter);s.ids.set(el,id);}s.elements[id]=el;return id;}
function __mcpVisible(el){if(!el||el.nodeType!==1)return false;var st=__mcpStyle(el);if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0)return false;var r=__mcpTopRect(el);if(r.width<1||r.height<1)return false;return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;}
function __mcpActionable(el){
  var tag=(el.tagName||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase();
  if(['a','button','input','textarea','select','summary','details','label'].indexOf(tag)>=0)return true;
  if(['button','link','checkbox','radio','tab','menuitem','option','combobox','textbox','searchbox','switch','slider','listbox'].indexOf(role)>=0)return true;
  if(el.isContentEditable||el.hasAttribute('onclick'))return true;var cls=String(el.className||'');if(/collapseTitle|collapse-title|dropdown-toggle|select-trigger|clickable|toggle/i.test(cls))return true;
  try{
    if(__mcpStyle(el).cursor==='pointer'){
      var parent=__mcpParent(el),parentPointer=false;try{parentPointer=!!parent&&__mcpStyle(parent).cursor==='pointer';}catch(_){}
      var pointerTag=String(el.tagName||'').toLowerCase();
      if(!parentPointer&&['path','g','use','circle','rect','polygon','polyline'].indexOf(pointerTag)<0)return true;
    }
  }catch(e){}
  var ti=el.getAttribute('tabindex');return ti!==null&&Number(ti)>=0;
}
function __mcpComposedContains(root,node){
  var cur=node,guard=0;while(cur&&guard++<20){if(cur===root)return true;cur=__mcpParent(cur);}return false;
}
function __mcpElementReadiness(el,kind,minStableMs){
  kind=String(kind||'click').toLowerCase();minStableMs=Math.max(0,Number(minStableMs||0));
  if(!el||el.nodeType!==1||!el.isConnected)return {ready:false,reason_code:'ELEMENT_DETACHED'};
  var target=(kind==='click'||kind==='double_click'||kind==='select')?__mcpActivationTarget(el):el;
  if(!target||!target.isConnected)return {ready:false,reason_code:'ELEMENT_DETACHED'};
  var s=__mcpState(),st=null,r=null,topRect=null;
  try{st=__mcpStyle(target);r=target.getBoundingClientRect();topRect=__mcpTopRect(target);}catch(e){return {ready:false,reason_code:'ELEMENT_NOT_READY'};}
  var base={ready:false,reason_code:null,element_id:__mcpId(target,s),stable_for_ms:Math.max(0,Date.now()-Number(s.lastMutationAt||Date.now())),dom_revision:s.mutationRevision,
    rect:{x:Math.round(topRect.left),y:Math.round(topRect.top),w:Math.round(topRect.width),h:Math.round(topRect.height)},pointer_events:String(st.pointerEvents||'')};
  if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0){base.reason_code='ELEMENT_HIDDEN';return base;}
  if(!isFinite(r.width)||!isFinite(r.height)||r.width<=0||r.height<=0){base.reason_code='ELEMENT_ZERO_BOUNDS';return base;}
  if(topRect.bottom<=0||topRect.right<=0||topRect.top>=innerHeight||topRect.left>=innerWidth){base.reason_code='ELEMENT_OFFSCREEN';return base;}
  if(target.disabled===true||target.getAttribute('aria-disabled')==='true'||target.closest&&target.closest('[inert]')){base.reason_code='ELEMENT_DISABLED';return base;}
  if((kind==='type'||kind==='type_text'||kind==='paste')&&(target.readOnly===true||target.getAttribute('readonly')!==null)){base.reason_code='ELEMENT_READONLY';return base;}
  if((kind==='type'||kind==='type_text'||kind==='paste')){
    var tag=String(target.tagName||'').toLowerCase(),role=String(__mcpRole(target)||'').toLowerCase();
    if(!(tag==='input'||tag==='textarea'||target.isContentEditable||role==='textbox'||role==='searchbox'||tag==='select')){base.reason_code='ELEMENT_NOT_EDITABLE';return base;}
  }else if((kind==='click'||kind==='double_click'||kind==='select')&&!__mcpActionable(target)){base.reason_code='ELEMENT_NOT_ACTIONABLE';return base;}
  var p=target,depth=0;while(p&&depth++<12){try{if(__mcpStyle(p).pointerEvents==='none'){base.reason_code='ELEMENT_POINTER_EVENTS_NONE';return base;}if(p.getAttribute&&p.getAttribute('aria-busy')==='true'){base.reason_code='ELEMENT_BUSY';return base;}}catch(e){}p=__mcpParent(p);}
  if(base.stable_for_ms<minStableMs){base.reason_code='ELEMENT_UNSTABLE';return base;}
  try{
    var doc=target.ownerDocument||document,win=doc.defaultView||window,lr=target.getBoundingClientRect(),cx=lr.left+lr.width/2,cy=lr.top+lr.height/2;
    if(cx<0||cy<0||cx>=win.innerWidth||cy>=win.innerHeight){base.reason_code='ELEMENT_OFFSCREEN';return base;}
    var hit=doc.elementFromPoint(cx,cy);base.hit_tag=hit?String(hit.tagName||'').toLowerCase():null;
    if(!hit||(!__mcpComposedContains(target,hit)&&!__mcpComposedContains(hit,target))){base.reason_code='ELEMENT_OCCLUDED';return base;}
  }catch(e){base.reason_code='ELEMENT_HIT_TEST_FAILED';return base;}
  base.ready=true;base.reason_code=null;return base;
}
function __mcpText(el){var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',title=el.getAttribute('title')||'',txt='';try{txt=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}return(aria||ph||title||txt).slice(0,240);}
function __mcpRole(el){var role=el.getAttribute('role');if(role)return role;var tag=(el.tagName||'').toLowerCase();if(tag==='a')return'link';if(tag==='button')return'button';if(tag==='select')return'combobox';if(tag==='textarea')return'textbox';if(tag==='input'){var t=(el.type||'text').toLowerCase();if(t==='checkbox')return'checkbox';if(t==='radio')return'radio';if(['button','submit','reset'].indexOf(t)>=0)return'button';return'textbox';}return'';}
function __mcpContext(el){
  try{
    var own=__mcpText(el),p=__mcpParent(el),depth=0;
    while(p&&depth++<8){
      var role=String(p.getAttribute&&p.getAttribute('role')||'').toLowerCase();
      var cls=String(p.className||'').toLowerCase();
      var semantic=['grid','listbox','menu','dialog','tooltip','group','radiogroup'].indexOf(role)>=0 || /(?:^|[\s_-])(month|calendar|datepicker|date-picker|listbox|menu|option-group|suggestions?|results?)(?:[\s_-]|$)/i.test(cls);
      if(semantic){
        var labelled='';
        try{
          var labelledBy=p.getAttribute('aria-labelledby');
          if(labelledBy){var doc=p.ownerDocument||document,node=doc.getElementById(labelledBy);if(node)labelled=String(node.innerText||node.textContent||'').replace(/\s+/g,' ').trim();}
        }catch(e){}
        var candidates=[];
        if(labelled)candidates.push(labelled);
        try{
          var heads=Array.from(p.querySelectorAll('.rdp-caption_label,[data-caption],[aria-live="polite"],legend,[role="heading"],h1,h2,h3,h4,h5,h6'));
          for(var i=0;i<heads.length&&i<10;i++){
            var head=heads[i],txt=String(head.innerText||head.textContent||'').replace(/\s+/g,' ').trim();
            if(txt&&txt.length<=120)candidates.push(txt);
          }
        }catch(e){}
        for(var j=0;j<candidates.length;j++){var text=candidates[j];if(text&&text!==own)return text.slice(0,120);}
      }
      p=__mcpParent(p);
    }
  }catch(e){}
  return '';
}
function __mcpRect(el){var r=__mcpTopRect(el),ox=(window.outerWidth-window.innerWidth),oy=(window.outerHeight-window.innerHeight),viewportX=window.screenX+Math.max(0,Math.round(ox/2)),viewportY=window.screenY+Math.max(0,Math.round(oy));return{viewport:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},document:{x:Math.round(r.left+scrollX),y:Math.round(r.top+scrollY),w:Math.round(r.width),h:Math.round(r.height)},screen:{x:Math.round(viewportX+r.left),y:Math.round(viewportY+r.top),w:Math.round(r.width),h:Math.round(r.height),estimated:true}};}
function __mcpDescribe(el,s){
  var tag=(el.tagName||'').toLowerCase(),rect=__mcpRect(el),out={element_id:__mcpId(el,s),tag:tag,role:__mcpRole(el),text:__mcpText(el),viewport_rect:rect.viewport,screen_rect:rect.screen,actionable:__mcpActionable(el)};
  if(out.actionable){var rd=__mcpElementReadiness(el,'observe',0);out.ready=!!rd.ready;if(rd.reason_code)out.readiness_reason=rd.reason_code;}
  var context=__mcpContext(el);if(context)out.context=context;
  var aria=el.getAttribute('aria-label')||'',ph=el.getAttribute('placeholder')||'',name=el.getAttribute('name')||'',title=el.getAttribute('title')||'';if(aria)out.aria_label=aria.slice(0,120);if(ph)out.placeholder=ph.slice(0,100);if(name)out.name=name.slice(0,100);if(title)out.title=title.slice(0,100);if(tag==='a'&&el.href)out.href=String(el.href).slice(0,220);if(el.disabled===true||el.getAttribute('aria-disabled')==='true')out.enabled=false;
  try{if(el.ownerDocument&&el.ownerDocument.activeElement===el)out.focused=true;}catch(e){}if(['input','textarea','select'].indexOf(tag)>=0)out.value=String(el.value||'').slice(0,160);if(tag==='input'&&el.type)out.input_type=String(el.type);if(typeof el.checked==='boolean'&&el.checked)out.checked=true;if(tag==='select')out.options=Array.from(el.options||[]).slice(0,24).map(function(o){return{text:String(o.text||'').slice(0,90),value:String(o.value||'').slice(0,90),selected:!!o.selected};});return out;
}
function __mcpParent(el){if(!el)return null;if(el.parentElement)return el.parentElement;try{var root=el.getRootNode&&el.getRootNode();return root&&root.host?root.host:null;}catch(e){return null;}}
function __mcpActivationTarget(el){
  if(!el)return el;if(__mcpActionable(el))return el;
  var selector='button,a,input,textarea,select,summary,label,[role="button"],[role="link"],[role="combobox"],[role="option"],[role="menuitem"],[role="tab"],[role="checkbox"],[role="radio"],[role="switch"],[tabindex]';
  try{var child=el.querySelector(selector);if(child&&__mcpVisible(child))return child;}catch(e){}var p=__mcpParent(el),n=0;while(p&&n++<4){if(__mcpActionable(p))return p;p=__mcpParent(p);}return el;
}
function __mcpScrollIntoView(el){if(!el)return;try{el.scrollIntoView({block:'center',inline:'nearest'});}catch(e){}var win=__mcpOwnerWindow(el),guard=0;while(win&&win!==window&&guard++<10){var frame=null;try{frame=win.frameElement;}catch(e){}if(!frame)break;try{frame.scrollIntoView({block:'center',inline:'nearest'});}catch(e){}win=__mcpOwnerWindow(frame);}}
function __mcpMouseEvent(el,type){var win=__mcpOwnerWindow(el),r=el.getBoundingClientRect(),x=Math.max(0,Math.round(r.left+r.width/2)),y=Math.max(0,Math.round(r.top+r.height/2)),common={bubbles:true,cancelable:true,composed:true,view:win,clientX:x,clientY:y,button:0,buttons:(type==='pointerdown'||type==='mousedown')?1:0};try{if(type.indexOf('pointer')===0&&typeof win.PointerEvent==='function')return new win.PointerEvent(type,Object.assign({pointerId:1,pointerType:'mouse',isPrimary:true},common));return new win.MouseEvent(type,common);}catch(e){return null;}}
function __mcpActivate(el){
  el=__mcpActivationTarget(el);if(!el)throw new Error('element_not_found');if(el.disabled===true||el.getAttribute('aria-disabled')==='true')throw new Error('element_disabled');__mcpScrollIntoView(el);
  var events=['pointerover','mouseover','pointermove','mousemove','pointerdown','mousedown'];for(var i=0;i<events.length;i++){var ev=__mcpMouseEvent(el,events[i]);if(ev)try{el.dispatchEvent(ev);}catch(e){}}
  try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}events=['pointerup','mouseup'];for(var j=0;j<events.length;j++){var up=__mcpMouseEvent(el,events[j]);if(up)try{el.dispatchEvent(up);}catch(e){}}el.click();return el;
}
function __mcpDoubleActivate(el){el=__mcpActivate(el);__mcpActivate(el);var ev=__mcpMouseEvent(el,'dblclick');if(ev)try{el.dispatchEvent(ev);}catch(e){}return el;}
function __mcpInputEvent(el,type,data,inputType,cancelable){var win=__mcpOwnerWindow(el);try{if(typeof win.InputEvent==='function')return new win.InputEvent(type,{bubbles:true,cancelable:!!cancelable,composed:true,data:data,inputType:inputType});}catch(e){}try{return new win.Event(type,{bubbles:true,cancelable:!!cancelable,composed:true});}catch(e){return null;}}
function __mcpNativeValueSetter(el,value){var win=__mcpOwnerWindow(el),tag=String(el.tagName||'').toLowerCase(),proto=null;if(tag==='input')proto=win.HTMLInputElement&&win.HTMLInputElement.prototype;else if(tag==='textarea')proto=win.HTMLTextAreaElement&&win.HTMLTextAreaElement.prototype;else if(tag==='select')proto=win.HTMLSelectElement&&win.HTMLSelectElement.prototype;if(proto){try{var d=Object.getOwnPropertyDescriptor(proto,'value');if(d&&typeof d.set==='function'){d.set.call(el,value);return true;}}catch(e){}}try{el.value=value;return true;}catch(e){return false;}}
function __mcpRecoverElement(id,s){
  var old=s.elements[id];if(old&&old.isConnected)return old;if(!old)return null;
  var ident={tag:String(old.tagName||'').toLowerCase(),role:__mcpRole(old),text:__mcpText(old),aria:old.getAttribute&&old.getAttribute('aria-label')||'',name:old.getAttribute&&old.getAttribute('name')||'',ph:old.getAttribute&&old.getAttribute('placeholder')||'',title:old.getAttribute&&old.getAttribute('title')||''};
  var all=__mcpQueryAll('*'),ranked=[];
  for(var i=0;i<all.length;i++){var el=all[i];if(!el.isConnected||!__mcpVisible(el))continue;var score=0,role=__mcpRole(el),tag=String(el.tagName||'').toLowerCase();if(ident.role&&role===ident.role)score+=2;if(ident.tag&&tag===ident.tag)score+=1;
    var pairs=[['aria','aria-label'],['name','name'],['ph','placeholder'],['title','title']];for(var j=0;j<pairs.length;j++){var want=ident[pairs[j][0]];if(want&&String(el.getAttribute(pairs[j][1])||'')===want)score+=4;}if(ident.text&&__mcpText(el)===ident.text)score+=3;if(score>=5)ranked.push({el:el,score:score});}
  ranked.sort(function(a,b){return b.score-a.score;});if(!ranked.length)return null;if(ranked.length>1&&ranked[0].score===ranked[1].score&&ranked[0].score<8)return null;
  var recovered=ranked[0].el;s.elements[id]=recovered;try{s.ids.set(recovered,id);}catch(e){}return recovered;
}
function __mcpNorm(v){return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\s+/g,' ').trim();}
function __mcpRecoverAction(a,s){
  var id=String(a&&a.element_id||''), byId=id?__mcpRecoverElement(id,s):null;if(byId)return byId;
  var query=__mcpNorm(a&&(a.query||a.target||a.text_match||a.target_text)||''), wantedRole=__mcpNorm(a&&a.role||'');
  if(!query&&!wantedRole)return null;
  var qTokens=query.split(' ').filter(Boolean),all=__mcpQueryAll('*'),ranked=[];
  for(var i=0;i<all.length;i++){
    var el=all[i];if(!el.isConnected||!__mcpVisible(el)||!__mcpActionable(el))continue;
    var role=__mcpNorm(__mcpRole(el));if(wantedRole&&role!==wantedRole)continue;
    var d=__mcpDescribe(el,s),fields=[d.text||'',d.aria_label||'',d.placeholder||'',d.name||'',d.title||'',d.value||'',d.context||''],score=wantedRole?2:0;
    for(var j=0;j<fields.length;j++){
      var f=__mcpNorm(fields[j]);if(!f)continue;
      if(query&&f===query)score=Math.max(score,12);
      else if(query&&(f.indexOf(query+' ')===0||f.indexOf(query+'-')===0))score=Math.max(score,9);
      else if(query&&qTokens.length&&qTokens.every(function(t){return f.split(' ').indexOf(t)>=0;}))score=Math.max(score,8);
      else if(query&&query.length>=4&&f.indexOf(query)>=0)score=Math.max(score,6);
    }
    if(score>=6||(!query&&wantedRole))ranked.push({el:el,score:score,text:__mcpText(el)});
  }
  ranked.sort(function(x,y){var d=y.score-x.score;if(d)return d;return String(x.text||'').length-String(y.text||'').length;});
  if(!ranked.length)return null;
  if(ranked.length>1&&ranked[0].score===ranked[1].score&&ranked[0].score<10)return null;
  var recovered=ranked[0].el;if(id){s.elements[id]=recovered;try{s.ids.set(recovered,id);}catch(e){}}return recovered;
}
function __mcpFlushMutations(s){
  var changed=false,obs=s.rootObservers||[];
  for(var i=0;i<obs.length;i++){
    var records=[];try{records=obs[i].takeRecords();}catch(e){}
    for(var j=0;j<records.length;j++){var rec=records[j];if(rec.type==='attributes'&&rec.attributeName==='data-mac-mcp-visual-event')continue;changed=true;break;}
  }
  if(changed){s.mutationRevision+=1;s.lastMutationAt=Date.now();}return changed;
}
function __mcpEffectState(el){
  if(!el)return {connected:false};var role=__mcpRole(el),value='',text='';
  try{value=('value' in el)?String(el.value==null?'':el.value):'';}catch(e){}
  try{if(!value&&(el.isContentEditable||role==='textbox'||role==='searchbox'))text=String(el.textContent||'');}catch(e){}
  return {connected:!!el.isConnected,value:value,text:text,checked:typeof el.checked==='boolean'?!!el.checked:null,expanded:el.getAttribute('aria-expanded'),selected:el.getAttribute('aria-selected'),pressed:el.getAttribute('aria-pressed'),ariaChecked:el.getAttribute('aria-checked'),cls:String(el.className||'')};
}
function __mcpEffectChanged(before,after){
  if(!before||!after)return false;if(before.connected&&!after.connected)return true;
  var keys=['value','text','checked','expanded','selected','pressed','ariaChecked','cls'];for(var i=0;i<keys.length;i++){if(before[keys[i]]!==after[keys[i]])return true;}return false;
}
function __mcpKeyboardActivate(el){
  if(!el)return '';var role=String(__mcpRole(el)||'').toLowerCase(),key=(role==='combobox'||role==='listbox')?'ArrowDown':((role==='checkbox'||role==='switch'||role==='radio')?' ':'Enter');
  try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}
  var win=__mcpOwnerWindow(el);function fire(type){try{el.dispatchEvent(new win.KeyboardEvent(type,{bubbles:true,cancelable:true,composed:true,key:key,code:key===' '?'Space':key}));}catch(e){}}
  fire('keydown');fire('keypress');fire('keyup');return key;
}
function __mcpSetText(el,value,clearFirst){
  if(!el)throw new Error('element_not_found');if(el.disabled===true||el.getAttribute('aria-disabled')==='true')throw new Error('element_disabled');if(el.readOnly===true||el.getAttribute('readonly')!==null)throw new Error('element_readonly');__mcpScrollIntoView(el);try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(_){}}
  value=String(value==null?'':value);var tag=String(el.tagName||'').toLowerCase(),editable=(tag==='input'||tag==='textarea'||tag==='select'),before=__mcpInputEvent(el,'beforeinput',value,'insertText',true);if(before)try{el.dispatchEvent(before);}catch(e){}
  if(editable){if(clearFirst!==false)__mcpNativeValueSetter(el,'');__mcpNativeValueSetter(el,value);}else if(el.isContentEditable||['textbox','searchbox'].indexOf(String(el.getAttribute('role')||'').toLowerCase())>=0){try{el.textContent=value;}catch(e){}}else{if(!__mcpNativeValueSetter(el,value))try{el.textContent=value;}catch(e){}}
  var input=__mcpInputEvent(el,'input',value,'insertText',false);if(input)try{el.dispatchEvent(input);}catch(e){}try{el.dispatchEvent(new (__mcpOwnerWindow(el).Event)('change',{bubbles:true,composed:true}));}catch(e){}try{var ku=new (__mcpOwnerWindow(el).KeyboardEvent)('keyup',{bubbles:true,cancelable:true,composed:true,key:value.slice(-1)||'Unidentified'});el.dispatchEvent(ku);}catch(e){}return el;
}'''


def _observe_js(scope: str, max_elements: int) -> str:
    scope_js = json.dumps(scope)
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
function __mcpOwnText(el){{
  var out=[];
  try{{Array.from(el.childNodes||[]).forEach(function(n){{if(n.nodeType===3){{var t=String(n.textContent||'').replace(/\\s+/g,' ').trim();if(t)out.push(t);}}}});}}catch(e){{}}
  return out.join(' ').trim().slice(0,240);
}}
function __mcpContentCandidate(el,actionable){{
  if(actionable) return true;
  var tag=(el.tagName||'').toLowerCase();
  if(/^h[1-6]$/.test(tag)) return true;
  if(tag==='img' && (el.getAttribute('alt')||'').trim()) return true;
  var own=__mcpOwnText(el);
  if(own.length>=2) return true;
  var cls=String(el.className||'').toLowerCase(), id=String(el.id||'').toLowerCase();
  var cardish=/(^|[-_ ])(card|item|listing|result|row|advert|product|property)([-_ ]|$)/.test(cls+' '+id);
  if((tag==='article'||tag==='tr'||tag==='li'||cardish)){{
    var txt=__mcpText(el);
    if(txt.length>=2 && txt.length<=700) return true;
  }}
  return false;
}}
var s=__mcpState();
__mcpStartMutationWatch(s,5000);
__mcpVisual('Inspecting',null,'',1800);
Object.keys(s.elements).forEach(function(k){{var e=s.elements[k];if(!e||!e.isConnected)delete s.elements[k];}});
var scope={scope_js};
var all=__mcpQueryAll('*');
var elements=[];
for(var i=0;i<all.length && elements.length<{max_elements};i++){{
  var el=all[i];
  if(!__mcpVisible(el)) continue;
  var actionable=__mcpActionable(el);
  if(scope==='interactive' && !actionable) continue;
  if(scope==='visible' && !actionable){{
    var txt=__mcpText(el);
    if(!txt || txt.length<2) continue;
  }}
  if((scope==='content'||scope==='leaf') && !__mcpContentCandidate(el,actionable)) continue;
  var desc=__mcpDescribe(el,s);
  if((scope==='content'||scope==='leaf') && !actionable){{
    var own=__mcpOwnText(el);
    if(own) desc.text=own;
    if(!desc.text && (el.getAttribute('alt')||'')) desc.text=String(el.getAttribute('alt')).slice(0,240);
  }}
  elements.push(desc);
}}
var obs='bobs_'+s.pageToken+'_'+Date.now().toString(36);
s.observations[obs]=s.mutationRevision;
var metrics={{screenX:screenX,screenY:screenY,outerWidth:outerWidth,outerHeight:outerHeight,innerWidth:innerWidth,innerHeight:innerHeight,devicePixelRatio:devicePixelRatio}};
return __mcpB64({{
  ok:true, observation_id:obs, dom_revision:s.mutationRevision,
  url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}},
  viewport:{{w:innerWidth,h:innerHeight}},window_metrics:metrics,
  scope:scope,element_count:elements.length,elements:elements
}});
}})()'''


def _observe_payload(
    settings: Settings, browser: str, scope: str, max_elements: int,
    window_index: int, tab_index: Optional[int], tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    requested = max_elements
    attempt = max_elements
    while True:
        try:
            payload = _run_json_js(
                settings, browser, _observe_js(scope, attempt),
                window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
            )
            payload["requested_max_elements"] = requested
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
                    "checked", "input_type", "viewport_rect",
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
            "element_count": payload.get("element_count"),
            "elements": compact_elements,
            "viewport": payload.get("viewport"),
            "scroll": payload.get("scroll"),
            "duration_ms": payload.get("duration_ms"),
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
    return json.dumps(payload, ensure_ascii=False, indent=2)


def browser_observe(
    settings: Settings,
    browser: str,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    scope: str = "interactive",
    max_elements: int = _DEFAULT_OBSERVE_ELEMENTS,
    visual: str = "none",
    element_id: Optional[str] = None,
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
                return json.dumps(payload, ensure_ascii=False, indent=2)
        if isinstance(observed, list) and observed and isinstance(observed[0], str):
            try:
                payload = json.loads(observed[0])
            except json.JSONDecodeError:
                return observed
            if isinstance(payload, dict):
                payload.update(lease_meta)
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
) -> Any:
    _norm_browser(browser)
    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    scope = str(scope or "interactive").lower().strip()
    if scope not in {"interactive", "visible", "content", "leaf"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be interactive, visible, content, or leaf.")
    visual = str(visual or "none").lower().strip()
    if visual not in _VISUAL_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visual must be none, viewport, element, or full_page.")
    max_elements = max(1, min(int(max_elements), _MAX_OBSERVE_ELEMENTS))
    started = time.perf_counter()
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


def _find_candidates_js(query: str, role: Optional[str], text: Optional[str], max_candidates: int = 80, actionable_only: bool = False) -> str:
    q_raw = _normalize_text(query)
    q_tokens = [token for token in q_raw.split() if token not in _GENERIC_QUERY_WORDS]
    q = " ".join(q_tokens) if q_tokens else q_raw
    q_js = json.dumps(q)
    role_js = json.dumps(_normalize_text(role) if role else "")
    text_js = json.dumps(_normalize_text(text) if text else "")
    actionable_js = "true" if actionable_only else "false"
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
function norm(v){{return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9çğıöşü]+/g,' ').replace(/\\s+/g,' ').trim();}}
function rendered(el){{
  if(!el||el.nodeType!==1) return false;
  var st=getComputedStyle(el); if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0) return false;
  var r=el.getBoundingClientRect(); return r.width>0&&r.height>0;
}}
function level(actual,wanted){{
  var a=norm(actual),w=norm(wanted); if(!a||!w)return 0;
  if(a===w)return 5;
  if(a.indexOf(w+' ')===0||a.indexOf(w+'-')===0)return 4;
  var at=a.split(' '),wt=w.split(' '); if(wt.length&&wt.every(function(t){{return at.indexOf(t)>=0;}}))return 3;
  if(w.length>=4&&a.indexOf(w)>=0)return 2;
  return 0;
}}
var s=__mcpState(), q={q_js}, wantedRole={role_js}, wantedText={text_js}, actionableOnly={actionable_js};
__mcpVisual('Finding',null,'',1600);
var out=[];
var all=__mcpQueryAll('*');
for(var i=0;i<all.length&&out.length<{max_candidates};i++){{
  var el=all[i]; if(!rendered(el))continue;
  var d=__mcpDescribe(el,s); d.actionable=__mcpActionable(el);
  if(actionableOnly && !d.actionable)continue;
  if(wantedRole&&norm(d.role)!==wantedRole)continue;
  var fields=[d.text||'',d.aria_label||'',d.placeholder||'',d.name||'',d.title||'',d.value||'',d.context||''];
  fields.push(fields.filter(Boolean).join(' '));
  if(wantedText){{var tl=0;fields.forEach(function(v){{tl=Math.max(tl,level(v,wantedText));}});if(!tl)continue;}}
  if(q){{
    var ql=0;fields.forEach(function(v){{ql=Math.max(ql,level(v,q));}});
    if(d.options){{d.options.forEach(function(o){{ql=Math.max(ql,level(o.text||'',q),level(o.value||'',q));}});}}
    if(!ql)continue;
  }}
  out.push(d);
}}
var obs='bobs_'+s.pageToken+'_'+Date.now().toString(36);s.observations[obs]=s.mutationRevision;
return __mcpB64({{ok:true,observation_id:obs,dom_revision:s.mutationRevision,url:location.href,title:document.title,elements:out}});
}})()'''


def browser_find(
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
) -> Dict[str, Any]:
    """Find a rendered DOM target with exact-first ranking and hard role/text constraints."""
    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    _ensure_visual_companion(settings, browser, window_index, tab_index, tab_handle)
    if not str(query or "").strip() and not text and not role:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query, text, or role is required.")
    started = time.perf_counter()
    max_results = max(1, min(int(max_results), 10))
    candidate_limit = 60
    payload_limited = False
    while True:
        try:
            payload = _run_json_js(
                settings, browser, _find_candidates_js(
                    str(query or ""), role, text, candidate_limit, actionable_only=actionable_only
                ),
                window_index=window_index, tab_index=tab_index, tab_handle=tab_handle,
            )
            break
        except HTTPException as exc:
            if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE or candidate_limit <= 10:
                raise
            candidate_limit = max(10, candidate_limit // 2)
            payload_limited = True
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for element in payload.get("elements", []):
        score = _score_candidate(element, str(query or ""), role, text)
        if score >= 0.30:
            scored.append((score, element))
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
            "href": element.get("href"), "viewport_rect": element.get("viewport_rect"),
            "screen_rect": element.get("screen_rect"), "actionable": element.get("actionable"),
            "ready": element.get("ready"), "readiness_reason": element.get("readiness_reason"),
        })
    return {
        "ok": True,
        "observation_id": payload.get("observation_id"),
        "dom_revision": payload.get("dom_revision"),
        "url": payload.get("url"),
        "title": payload.get("title"),
        "query": query,
        "search_scope": "targeted_scan",
        "actionable_only": actionable_only,
        "candidate_limit": candidate_limit,
        "payload_limited": payload_limited,
        "best_match": matches[0] if matches else None,
        "matches": matches,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _batch_js(actions: List[Dict[str, Any]], observation_id: Optional[str]) -> str:
    actions_json = json.dumps(actions, ensure_ascii=False)
    obs_json = json.dumps(observation_id)
    template = r'''(function(){
__BOOTSTRAP__
function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}
var s=__mcpState();
__mcpFlushMutations(s);
__mcpStartMutationWatch(s,3000);
var expected=__OBS__;
if(expected && !(expected in s.observations)) return __mcpB64({ok:false,error:'stale_observation',observe_again:true});
var changed=expected ? (s.observations[expected]!==s.mutationRevision) : false;
var actions=__ACTIONS__;
var results=[];
function target(a){return __mcpRecoverAction(a,s);}
function emit(el,type){try{el.dispatchEvent(new (__mcpOwnerWindow(el).Event)(type,{bubbles:true,composed:true}));}catch(e){}}
function pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,el){
  __mcpFlushMutations(s);
  var afterState=__mcpEffectState(el);
  return s.mutationRevision!==beforeRevision || location.href!==beforeUrl || document.title!==beforeTitle || __mcpEffectChanged(beforeState,afterState);
}
for(var i=0;i<actions.length;i++){
  var a=actions[i]||{}, type=String(a.type||'').toLowerCase().replace(/-/g,'_');
  var el=a.element_id?target(a):null;
  if(a.element_id && !el){results.push({index:i,type:type,element_id:a.element_id,ok:false,error:'stale_element',observe_again:true});break;}
  var visualLabel=type==='click'||type==='double_click'?'Clicking':(type==='type'||type==='type_text'||type==='paste'?'Typing':(type==='scroll'?'Scrolling':(type==='focus'?'Focusing':(type==='select'?'Selecting':'Working'))));
  var visualDetail=type==='scroll'?(el?'Into view':(Number(a.dy||300)<0?'Up':'Down')):'';
  __mcpVisual(visualLabel,el,(type==='click'||type==='double_click')?'click':'',2200,visualDetail);
  var beforeRevision=s.mutationRevision,beforeUrl=location.href,beforeTitle=document.title,beforeState=el?__mcpEffectState(el):null;
  try{
    if(type==='click'||type==='double_click'){
      if(!el) throw new Error('element_id is required');
      __mcpScrollIntoView(el);
      var tag=(el.tagName||'').toLowerCase(),href=String(el.getAttribute('href')||''),inputType=String(el.getAttribute('type')||'').toLowerCase();
      var mayNavigate=(tag==='a'&&href&&href!=='#'&&!href.endsWith('#'))||((tag==='button'||tag==='input')&&inputType==='submit');
      var shouldDefer=(type==='click'&&i===actions.length-1&&mayNavigate),activated=el,effectObserved=false,verification='no_immediate_effect';
      if(type==='double_click') activated=__mcpDoubleActivate(el);
      else if(shouldDefer) setTimeout(function(node){return function(){try{__mcpActivate(node);}catch(e){}};}(el),0);
      else activated=__mcpActivate(el);
      if(shouldDefer){verification='deferred_pending';}
      else{
        effectObserved=pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,activated||el);
        if(effectObserved) verification='state_changed';
      }
      var clickResult={index:i,type:type,element_id:a.element_id,ok:true,deferred:shouldDefer,activation_target:activated?__mcpId(activated,s):a.element_id,effect_observed:effectObserved,verification:verification,_verify_revision:beforeRevision,_verify_url:beforeUrl,_verify_title:beforeTitle,_verify_state:beforeState};
      if(!effectObserved)clickResult.observe_again=true;
      results.push(clickResult);
    } else if(type==='type'||type==='type_text'||type==='paste'){
      if(!el) throw new Error('element_id is required');
      var value=String(a.text==null?'':a.text);
      __mcpSetText(el,value,a.clear!==false);__mcpFlushMutations(s);
      var actual='';try{actual=('value' in el)?String(el.value||''):String(el.textContent||'');}catch(e){}
      var applied=actual===value;
      var typed={index:i,type:type,element_id:a.element_id,ok:applied,value:actual.slice(0,200),effect_observed:applied,verification:applied?'value_applied':'input_not_applied',observe_again:!applied};
      if(!applied)typed.error='input_not_applied';results.push(typed);if(!applied)break;
    } else if(type==='select'){
      if(!el) throw new Error('element_id is required');
      var wanted=String(a.option==null?'':a.option).trim().toLowerCase(),chosen=null;
      if((el.tagName||'').toLowerCase()==='select'){
        var opts=Array.from(el.options||[]);
        chosen=opts.find(function(o){return String(o.value).toLowerCase()===wanted||String(o.text).trim().toLowerCase()===wanted;})||opts.find(function(o){return String(o.text).trim().toLowerCase().indexOf(wanted)>=0;});
        if(!chosen)throw new Error('option_not_found');
        __mcpNativeValueSetter(el,chosen.value);emit(el,'input');emit(el,'change');
      }else{
        __mcpActivate(el);
        var candidates=__mcpQueryAll('[role="option"],option,[role="menuitem"],li,button,a').filter(__mcpVisible);
        chosen=candidates.find(function(o){return __mcpText(o).toLowerCase()===wanted;})||candidates.find(function(o){return __mcpText(o).toLowerCase().indexOf(wanted)>=0;});
        if(!chosen)throw new Error('option_not_found');__mcpActivate(chosen);
      }
      __mcpFlushMutations(s);results.push({index:i,type:type,element_id:a.element_id,ok:true,selected:chosen?__mcpText(chosen):wanted,effect_observed:pageEffect(beforeRevision,beforeUrl,beforeTitle,beforeState,el)});
    } else if(type==='scroll'){
      if(el)__mcpScrollIntoView(el);else window.scrollBy(Number(a.dx||0),Number(a.dy||300));
      results.push({index:i,type:type,element_id:a.element_id||null,ok:true,effect_observed:true,verification:'scroll_applied'});
    } else if(type==='focus'){
      if(!el)throw new Error('element_id is required');el.focus();results.push({index:i,type:type,element_id:a.element_id,ok:true,effect_observed:true,verification:'focus_applied'});
    } else throw new Error('unsupported_batch_action:'+type);
  }catch(e){results.push({index:i,type:type,element_id:a.element_id||null,ok:false,error:String(e&&e.message||e)});break;}
}
__mcpFlushMutations(s);
var active=document.activeElement;
var compact={ok:true,url:location.href,title:document.title,scroll:{x:scrollX,y:scrollY},dom_revision:s.mutationRevision,active_element:active&&active.nodeType===1?__mcpDescribe(active,s):null};
return __mcpB64({ok:results.every(function(r){return r.ok;}),actions:results,dom_changed_since_observe:changed,dom_revision:s.mutationRevision,url:location.href,title:document.title,scroll:{x:scrollX,y:scrollY},state:compact});
})()'''
    return template.replace('__BOOTSTRAP__', _browser_state_bootstrap()).replace('__OBS__', obs_json).replace('__ACTIONS__', actions_json)


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
var el=s.elements[eid];
if(!el||!el.isConnected) return __mcpB64({{ok:false,error:'stale_element',observe_again:true,element_id:eid}});
__mcpVisual('Selecting',el,'',2200);
if((el.tagName||'').toLowerCase()==='select'){{
  var opts=Array.from(el.options||[]);
  var chosen=opts.find(function(o){{return norm(o.value)===wanted||norm(o.text)===wanted;}}) ||
             opts.find(function(o){{var t=norm(o.text);return t.indexOf(wanted+' ')===0;}}) ||
             opts.find(function(o){{return norm(o.text).split(' ').indexOf(wanted)>=0;}});
  if(!chosen) return __mcpB64({{ok:false,error:'option_not_found',native:true,element_id:eid}});
  el.value=chosen.value;
  try{{el.dispatchEvent(new Event('input',{{bubbles:true}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));}}catch(e){{}}
  return __mcpB64({{ok:true,native:true,selected:String(chosen.text||chosen.value),element_id:eid}});
}}
__mcpScrollIntoView(el);
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
var all=__mcpQueryAll(selectors).filter(rendered);
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
var exact=all.filter(function(el){{return label(el)===wanted;}});
var prefix=all.filter(function(el){{var t=label(el);return t.indexOf(wanted+' ')===0||t.indexOf(wanted+' (')===0;}});
var chosen=(best(exact)||best(prefix)||null);
if(!chosen) return __mcpB64({{ok:true,found:false,candidate_count:all.length}});
var txt=__mcpText(chosen);
__mcpScrollIntoView(chosen);
__mcpActivate(chosen);
return __mcpB64({{ok:true,found:true,selected:txt,tag:(chosen.tagName||'').toLowerCase(),role:chosen.getAttribute('role')||''}});
}})()'''


def _select_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    element_id = str(action.get("element_id") or "")
    if not element_id:
        return {"ok": False, "type": "select", "error": "element_id is required", "_js_calls": 0}
    option = action.get("option")
    timeout_s = max(0.2, min(float(action.get("timeout_s", 2.0)), 5.0))
    poll_s = max(0.05, min(float(action.get("poll_ms", 100)) / 1000.0, 0.5))
    started = time.perf_counter()
    js_calls = 0
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
    prep = _run_json_js(
        settings, browser, _select_prepare_js(element_id, observation_id, option),
        window_index, tab_index, tab_handle,
    )
    js_calls += 1
    if not prep.get("ok"):
        prep.update({"type": "select", "_js_calls": js_calls, "duration_ms": int((time.perf_counter()-started)*1000)})
        return prep
    if prep.get("native"):
        return {
            "ok": True, "type": "select", "element_id": element_id,
            "selected": prep.get("selected"), "native": True,
            "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
        }
    while time.perf_counter() - started < timeout_s:
        found = _run_json_js(
            settings, browser, _select_option_js(element_id, option),
            window_index, tab_index, tab_handle,
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
                time.sleep(0.06)
            return {
                "ok": True, "type": "select", "element_id": element_id,
                "selected": found.get("selected"), "native": False,
                "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
            }
        time.sleep(poll_s)
    return {
        "ok": False, "type": "select", "element_id": element_id,
        "error": "option_not_found", "timed_out": True, "native": False,
        "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls,
    }


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
        if last.get("ready"):
            last.update({"duration_ms": int((now - started) * 1000), "_js_calls": js_calls})
            return last
        if now >= deadline:
            last.update({"ready": False, "timed_out": True, "duration_ms": int((now - started) * 1000), "_js_calls": js_calls})
            if not last.get("reason_code"):
                last["reason_code"] = "ELEMENT_NOT_READY"
            return last
        time.sleep(poll_s)


def _element_effect_state_js(element_id: str) -> str:
    eid = json.dumps(str(element_id or ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState(),el=__mcpRecoverElement({eid},s);
if(!el) return __mcpB64({{ok:true,connected:false,url:location.href,title:document.title,dom_revision:s.mutationRevision}});
var tag=String(el.tagName||'').toLowerCase(),role=__mcpRole(el),value='',text='';
try{{value=('value' in el)?String(el.value==null?'':el.value):'';}}catch(e){{}}
try{{if(!value&&(el.isContentEditable||role==='textbox'||role==='searchbox'))text=String(el.textContent||'');}}catch(e){{}}
var focused=false;try{{focused=!!(el.ownerDocument&&el.ownerDocument.activeElement===el);}}catch(e){{}}
return __mcpB64({{ok:true,connected:!!el.isConnected,url:location.href,title:document.title,dom_revision:s.mutationRevision,tag:tag,role:role,value:value,text:text,
checked:typeof el.checked==='boolean'?!!el.checked:null,aria_expanded:el.getAttribute('aria-expanded'),aria_selected:el.getAttribute('aria-selected'),aria_pressed:el.getAttribute('aria-pressed'),aria_checked:el.getAttribute('aria-checked'),class_name:String(el.className||''),focused:focused}});
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


def _effect_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    if not before or not after:
        return False
    if before.get("url") != after.get("url") or before.get("title") != after.get("title"):
        return True
    if not after.get("connected", True):
        return True
    if before.get("dom_revision") != after.get("dom_revision"):
        return True
    for key in ("value", "text", "checked", "aria_expanded", "aria_selected", "aria_pressed", "aria_checked", "class_name"):
        if before.get(key) != after.get(key):
            return True
    return False


def _verified_dom_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str],
) -> Dict[str, Any]:
    typ = str(action.get("type") or "").lower().replace("-", "_")
    element_id = str(action.get("element_id") or "")
    js_calls = 0

    readiness = _wait_for_element_readiness(
        settings, browser, action, window_index, tab_index, tab_handle,
    )
    js_calls += int(readiness.pop("_js_calls", 0))
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

    out = _run_json_js(
        settings, browser, _batch_js([action], observation_id),
        window_index, tab_index, tab_handle,
    )
    js_calls += 1
    result = dict((out.get("actions") or [out])[0])
    if "type" not in result:
        result["type"] = typ
    if element_id and "element_id" not in result:
        result["element_id"] = element_id
    result["readiness"] = {
        key: readiness.get(key)
        for key in ("ready", "reason_code", "stable_for_ms", "dom_revision", "rect", "pointer_events", "hit_tag", "duration_ms")
        if readiness.get(key) is not None
    }

    before_revision = result.pop("_verify_revision", None)
    before_url = result.pop("_verify_url", None)
    before_title = result.pop("_verify_title", None)
    before_state = result.pop("_verify_state", None)
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
        })
    compact_state = out.get("state") if isinstance(out.get("state"), dict) else None

    # A real click is emitted only once. Verification is read-only and bounded so a
    # delayed SPA commit can be observed without risking a duplicate destructive action.
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
            time.sleep(poll_s)
            try:
                post = _run_json_js(
                    settings, browser, _element_effect_state_js(element_id),
                    window_index, tab_index, tab_handle,
                )
                js_calls += 1
                progressed = (
                    _effect_changed(normalized_before_state, post)
                    or (before_revision is not None and post.get("dom_revision") != before_revision)
                    or (before_url is not None and post.get("url") != before_url)
                    or (before_title is not None and post.get("title") != before_title)
                )
                if progressed:
                    result["effect_observed"] = True
                    result["verification"] = "async_state_changed"
                    result.pop("observe_again", None)
                    compact_state = post
                    break
            except HTTPException:
                # Navigation can invalidate the previous document while the deferred click
                # is taking effect. Losing that document is itself evidence of progress.
                result["effect_observed"] = True
                result["verification"] = "async_navigation"
                result.pop("observe_again", None)
                break

        if not result.get("effect_observed"):
            result.update({
                "ok": False,
                "error": "action_no_effect",
                "reason_code": "ACTION_NO_EFFECT",
                "verification": "no_effect_after_bounded_wait",
                "observe_again": True,
                "automatic_retry": False,
            })

    result["_js_calls"] = js_calls
    if isinstance(compact_state, dict):
        result["_compact_state"] = compact_state
    return result

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


def _extract_action_js(fields: List[Dict[str, Any]], max_chars: int) -> str:
    specs = json.dumps(fields, ensure_ascii=False)
    aliases = json.dumps(_SEMANTIC_EXTRACT_ALIASES, ensure_ascii=False)
    budget = max(256, min(int(max_chars), 20_000))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
__mcpVisual('Reading',null,'',1800);
var specs={specs}, aliases={aliases}, budget={budget}, data={{}}, counts={{}}, truncated=false, used=0;
function readValue(el, attr){{
  attr=String(attr||'text');
  if(attr==='text') return String(el.innerText||el.textContent||'').trim();
  if(attr==='html') return String(el.innerHTML||'');
  if(attr==='value') return String(el.value==null?'':el.value);
  if(attr==='href') return String(el.href||el.getAttribute('href')||'');
  if(attr==='aria_label') return String(el.getAttribute('aria-label')||'');
  return String(el.getAttribute(attr)||'');
}}
function clean(v){{return String(v==null?'':v).replace(/\\s+/g,' ').trim();}}
function norm(v){{
  return clean(v).toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/[ıİ]/g,'i').replace(/[^a-z0-9₺€$%.,:/+\\- ]+/g,' ').replace(/\\s+/g,' ').trim();
}}
function visible(el){{
  try{{var st=getComputedStyle(el),r=el.getBoundingClientRect();return st.display!=='none'&&st.visibility!=='hidden'&&Number(st.opacity||1)!==0&&r.width>0&&r.height>0;}}catch(e){{return false;}}
}}
function termHit(text, term){{
  var t=norm(term); if(!t) return 0;
  if(text===t) return 180;
  if(text.indexOf(t)>=0) return 105+Math.min(35,t.length);
  var words=t.split(' ').filter(Boolean), hits=0;
  for(var i=0;i<words.length;i++) if(words[i].length>1&&text.indexOf(words[i])>=0) hits++;
  return hits&&hits===words.length?70+hits*5:0;
}}
function semanticTerms(sp,name){{
  var target=String(sp.semantic||name||''), key=norm(target), out=[target];
  Object.keys(aliases).forEach(function(aliasKey){{
    var nk=norm(aliasKey);
    if(key===nk||key.indexOf(nk)>=0||nk.indexOf(key)>=0) out=out.concat(aliases[aliasKey]||[]);
  }});
  if(Array.isArray(sp.terms)) out=out.concat(sp.terms);
  var seen={{}}, unique=[];
  out.forEach(function(v){{var n=norm(v);if(n&&!seen[n]){{seen[n]=true;unique.push(v);}}}});
  return unique;
}}
var semanticCache=null;
function semanticCandidates(){{
  if(semanticCache!==null) return semanticCache;
  var nodes=[]; semanticCache=[];
  try{{nodes=__mcpQueryAll('h1,h2,h3,h4,h5,h6,p,li,dt,dd,label,button,a,span,strong,b,small,div');}}catch(e){{return semanticCache;}}
  if(nodes.length>6000) nodes=nodes.slice(0,6000);
  for(var i=0;i<nodes.length;i++){{
    var el=nodes[i]; if(!visible(el)) continue;
    var aria=clean(el.getAttribute&&el.getAttribute('aria-label')||''), title=clean(el.getAttribute&&el.getAttribute('title')||'');
    var raw=clean(el.innerText||el.textContent||aria||title||''); if(!raw||raw.length>520) continue;
    var childText=0, href='', itemId='';
    try{{for(var c=0;c<el.children.length;c++) if(clean(el.children[c].innerText||el.children[c].textContent||'')) childText++;}}catch(e){{}}
    try{{href=String(el.href||el.getAttribute('href')||'');itemId=String(el.getAttribute('data-item-id')||'');}}catch(e){{}}
    semanticCache.push({{el:el,raw:raw,text:norm(raw),childText:childText,tag:String(el.tagName||'').toLowerCase(),href:href,aria:aria,title:title,itemId:itemId}});
  }}
  return semanticCache;
}}
function semanticValues(sp,name,maxItems){{
  var semantic=norm(sp.semantic||name), terms=semanticTerms(sp,name), candidates=semanticCandidates();
  var ranked=[], seen={{}};
  for(var i=0;i<candidates.length;i++){{
    var candidate=candidates[i], el=candidate.el, raw=candidate.raw, text=candidate.text, score=0;
    for(var j=0;j<terms.length;j++) score=Math.max(score,termHit(text,terms[j]));
    if(semantic.indexOf('price')>=0||semantic.indexOf('fiyat')>=0){{
      if(/[₺€$]|\\b(?:tl|try|eur|usd)\\b/i.test(raw)&&/\\d/.test(raw)) score=Math.max(score,150);
    }}
    if(semantic.indexOf('rating')>=0||semantic.indexOf('score')>=0||semantic.indexOf('puan')>=0){{
      if(/^\\s*(?:[0-9](?:[.,][0-9])?|10(?:[.,]0)?)\\s*(?:\\/\\s*(?:5|10))?\\s*$/.test(raw)) score=Math.max(score,135);
    }}
    if(semantic.indexOf('hours')>=0||semantic.indexOf('opening')>=0||semantic.indexOf('calisma saat')>=0||semantic.indexOf('çalışma saat')>=0){{
      if(/(?:open|closed|closes|opens|açık|acik|kapalı|kapali|kapanış saati|kapanis saati|çalışma saatleri|calisma saatleri)/i.test(raw)) score=Math.max(score,175);
      if(/\\b(?:[01]?\\d|2[0-3])[:.]?[0-5]\\d\\b/.test(raw)&&/(?:open|closed|açık|acik|kapalı|kapali|kapan|saat)/i.test(raw)) score+=45;
    }}
    if(semantic.indexOf('address')>=0||semantic.indexOf('location')>=0||semantic.indexOf('adres')>=0||semantic.indexOf('konum')>=0){{
      var addressMeta=[candidate.itemId,candidate.aria,candidate.title].join(' ');
      var structuralAddress=/(?:^|[^a-z])address(?:$|[^a-z])|(?:^|[^a-z])adres(?:$|[^a-z])/i.test(addressMeta);
      if(structuralAddress) score=Math.max(score,260);
      if(/^(?:adres|address)\\s*:/i.test(candidate.aria||'')) score=Math.max(score,280);
      if(/(?:\\b(?:cad(?:desi)?|cd\\.?|sok(?:ak)?|sk\\.?|bulv(?:arı|ari)?|blv\\.?|mah(?:allesi)?|apt\\.?|street|st\\.?|road|rd\\.?|avenue|ave\\.?|boulevard|blvd\\.?)\\b|\\bno[:.]?\\s*\\d)/i.test(raw)) score=Math.max(score,170);
      if(/street view|sokak görünümü|sokak gorunumu/i.test(raw)) score-=260;
      if(raw.length>180&&!structuralAddress) score-=140;
    }}
    if(semantic.indexOf('website')>=0||semantic.indexOf('web site')>=0||semantic.indexOf('homepage')>=0){{
      var websiteHint=/website|web sitesi|web site|official website|official site|resmi site|homepage|authority/i.test([raw,candidate.aria,candidate.title,candidate.itemId].join(' '));
      if(websiteHint&&/^https?:\\/\\//i.test(candidate.href||'')) score=Math.max(score,220);
      else if(websiteHint) score=Math.max(score,165);
    }}
    if(semantic.indexOf('price')>=0||semantic.indexOf('fiyat')>=0){{
      if(/maxipuan|puan kazan|kampanya|\\bindirim\\b/i.test(raw)) score-=90;
      if(/^\\s*[0-9][0-9., ]*\\s*(?:tl|try|₺|eur|€|usd|\\$)\\s*$/i.test(raw)) score+=85;
    }}
    if(semantic.indexOf('cancellation')>=0||semantic.indexOf('iptal')>=0){{
      if(/ücretsiz iptal|ucretsiz iptal|free cancellation|iptal edilemez|non[- ]?refundable|iade edilemez/i.test(raw)) score+=110;
      if(/paketi|garantisi|fiyat farkı|fiyat farki/i.test(raw)) score-=80;
    }}
    if(semantic.indexOf('payment')>=0||semantic.indexOf('odeme')>=0){{
      if(/otele ödeme|otele odeme|otelde ödeme|otelde odeme|tesiste ödeme|tesiste odeme|pay at property|pay later|prepayment|ön ödeme|on odeme/i.test(raw)) score+=120;
    }}
    if(semantic.indexOf('parking')>=0||semantic.indexOf('otopark')>=0){{
      if(/otoparka sahip değildir|otoparka sahip degildir|otopark yok|otopark var|ücretsiz otopark|ucretsiz otopark|free parking|parking available|no parking/i.test(raw)) score+=100;
      if(/\\byorum\\b|\\bkahvalt/i.test(raw)&&raw.length>180) score-=50;
    }}
    if(semantic.indexOf('breakfast')>=0||semantic.indexOf('kahvalti')>=0){{
      if(/kahvaltı dahil|kahvalti dahil|breakfast included/i.test(raw)) score+=120;
      if(raw.length>160) score-=70;
    }}
    if(semantic.indexOf('rating')>=0||semantic.indexOf('score')>=0||semantic.indexOf('puan')>=0){{
      if(/^\\s*[0-9]+\\s*$/.test(raw)) score-=120;
      if(/^\\s*(?:[0-9][.,][0-9]|10[.,]0)\\s*$/.test(raw)) score+=120;
    }}
    if(score<=0) continue;
    if(candidate.childText===0) score+=18;
    if(raw.length<=80) score+=20; else if(raw.length<=180) score+=10;
    if(/^(p|li|dt|dd|label|span|strong|b|small|h[1-6])$/.test(candidate.tag)) score+=8;
    var snippet=raw;
    if((semantic.indexOf('address')>=0||semantic.indexOf('location')>=0||semantic.indexOf('adres')>=0||semantic.indexOf('konum')>=0)&&/^(?:adres|address)\\s*:/i.test(candidate.aria||'')){{
      snippet=String(candidate.aria||'').replace(/^(?:adres|address)\\s*:\\s*/i,'').trim();
    }}
    if((semantic.indexOf('website')>=0||semantic.indexOf('web site')>=0||semantic.indexOf('homepage')>=0)&&candidate.href){{
      try{{
        var websiteUrl=new URL(candidate.href,location.href);
        if(/(^|\\.)google\\./i.test(websiteUrl.hostname)&&websiteUrl.pathname==='/url'){{
          snippet=websiteUrl.searchParams.get('q')||websiteUrl.searchParams.get('url')||websiteUrl.href;
        }}else snippet=websiteUrl.href;
      }}catch(e){{snippet=candidate.href;}}
    }}
    if(raw.length<=80&&terms.some(function(term){{return norm(raw)===norm(term);}})){{
      var parent=el.parentElement, parentText=parent?clean(parent.innerText||parent.textContent||''):'';
      if(parentText&&parentText!==raw&&parentText.length<=240) snippet=parentText;
    }}
    var sig=norm(snippet); if(!sig||seen[sig]) continue; seen[sig]=true;
    ranked.push({{score:score,text:snippet}});
  }}
  ranked.sort(function(a,b){{return b.score-a.score||a.text.length-b.text.length;}});
  var values=[], valueSeen={{}};
  for(var k=0;k<ranked.length&&values.length<maxItems;k++){{
    var sig=norm(ranked[k].text); if(valueSeen[sig]) continue; valueSeen[sig]=true; values.push(ranked[k].text);
  }}
  return values;
}}
function bounded(v){{
  v=String(v==null?'':v);
  var remaining=Math.max(0,budget-used);
  if(v.length>remaining){{v=v.slice(0,remaining);truncated=true;}}
  used+=v.length; return v;
}}
for(var i=0;i<specs.length;i++){{
  var sp=specs[i]||{{}}, name=String(sp.name||('field_'+i));
  var maxItems=Math.max(1,Math.min(Number(sp.max_items||10),100)), vals=[];
  if(sp.semantic){{
    vals=semanticValues(sp,name,maxItems); counts[name]=vals.length; vals=vals.map(bounded);
  }}else{{
    var sel=String(sp.selector||'body'), els=[];
    try{{els=__mcpQueryAll(sel);}}catch(e){{data[name]=null;counts[name]=0;continue;}}
    counts[name]=els.length;
    vals=els.slice(0,maxItems).map(function(el){{return bounded(readValue(el,sp.attr));}});
  }}
  if(sp.regex){{
    try{{
      var re=new RegExp(String(sp.regex),String(sp.flags||''));
      vals=vals.map(function(v){{var m=v.match(re); return m?(m[1]!==undefined?m[1]:m[0]):null;}}).filter(function(v){{return v!==null;}});
    }}catch(e){{}}
  }}
  data[name]=sp.all?vals:(vals.length?vals[0]:null);
}}
return __mcpB64({{ok:true,type:'extract',url:location.href,title:document.title,data:data,matched_counts:counts,truncated:truncated,chars:used}});
}})()'''

def _extract_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    tab_handle: Optional[str] = None,
) -> Dict[str, Any]:
    fields = action.get("fields") or []
    if not isinstance(fields, list) or not fields:
        return {"ok": False, "type": "extract", "error": "fields must be a non-empty list", "_js_calls": 0}
    if len(fields) > 20:
        return {"ok": False, "type": "extract", "error": "fields may contain at most 20 items", "_js_calls": 0}
    out = _run_json_js(
        settings, browser, _extract_action_js(fields, int(action.get("max_chars", 4000))),
        window_index, tab_index, tab_handle,
    )
    out["_js_calls"] = 1
    return out


def _wait_action(
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
                time.sleep(poll_s)
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
            time.sleep(poll_s)
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
            time.sleep(poll_s)
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
                time.sleep(poll_s)
                continue
            raise
        if state.get("matched"):
            return {"ok": True, "type": "wait", "for": kind, "matched": True, "duration_ms": int((time.perf_counter()-started)*1000), "url": state.get("url"), "_compact_state": state, "_js_calls": js_calls}
        time.sleep(poll_s)
    return {"ok": True, "type": "wait", "for": kind, "matched": False, "timed_out": True, "duration_ms": int((time.perf_counter()-started)*1000), "_js_calls": js_calls}


def browser_act(
    settings: Settings,
    browser: str,
    actions: List[Dict[str, Any]],
    observation_id: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    return_state: str = "compact",
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    """Perform one serialized action transaction against a single logical tab."""
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must be a non-empty list.")
    if len(actions) > _MAX_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"actions may contain at most {_MAX_ACTIONS} items.")
    normalized_return_state = str(return_state or "compact").lower().strip()
    if normalized_return_state not in _RETURN_STATE_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full.")
    b = _norm_browser(browser)
    _require_stable_handle_for_mutation(b, tab_handle, window_index, "browser_act")
    _ensure_visual_companion(settings, b, window_index, tab_index, tab_handle)
    with _tab_lease(b, tab_handle, window_index, tab_index) as target:
        return _browser_act_locked(
            settings=settings,
            browser=target.browser,
            actions=actions,
            observation_id=observation_id,
            window_index=target.window_index,
            tab_index=target.tab_index,
            tab_handle=target.tab_handle,
            return_state=normalized_return_state,
            allow_foreground=allow_foreground,
        )


def _browser_act_locked(
    settings: Settings,
    browser: str,
    actions: List[Dict[str, Any]],
    observation_id: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    tab_handle: Optional[str] = None,
    return_state: str = "compact",
    allow_foreground: bool = False,
) -> Dict[str, Any]:
    """Perform batched browser actions while the caller holds the tab lease."""
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must be a non-empty list.")
    if len(actions) > _MAX_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"actions may contain at most {_MAX_ACTIONS} items.")
    return_state = str(return_state or "compact").lower().strip()
    if return_state not in _RETURN_STATE_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full.")

    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)

    started = time.perf_counter()
    results: List[Dict[str, Any]] = []
    internal_js_calls = 0
    current_observation_id = observation_id
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
    compact_state_candidate: Optional[Dict[str, Any]] = None

    def resolve_target(action: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        nonlocal internal_js_calls
        if action.get("element_id"):
            return dict(action), None
        query = str(action.get("query") or action.get("target") or "").strip()
        role = action.get("role")
        match_text = action.get("text_match") or action.get("target_text")
        if not query and not role and not match_text:
            return dict(action), None
        found = browser_find(
            settings, browser, query=query, role=role, text=match_text,
            window_index=window_index, tab_index=tab_index, tab_handle=tab_handle, max_results=1,
        )
        internal_js_calls += 1
        best = found.get("best_match")
        if not best:
            return dict(action), {
                "ok": False, "error": "target_not_found", "query": query,
                "role": role, "text": match_text,
            }
        resolved = dict(action)
        resolved["element_id"] = best.get("element_id")
        return resolved, best

    def flush_pending() -> bool:
        nonlocal pending, internal_js_calls, current_observation_id
        if not pending:
            return True
        out = _run_json_js(
            settings,
            browser,
            _batch_js(pending, current_observation_id),
            window_index,
            tab_index,
            tab_handle,
        )
        internal_js_calls += 1
        if not out.get("ok") and out.get("error") == "stale_observation":
            results.append({"ok": False, "error": "stale_observation", "observe_again": True})
            pending = []
            return False
        results.extend(out.get("actions") or [])
        pending = []
        if out.get("ok"):
            current_observation_id = None
        return bool(out.get("ok"))

    for action in actions:
        if not isinstance(action, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each action must be an object.")
        typ = str(action.get("type") or "").lower().replace("-", "_")
        resolved_target: Optional[Dict[str, Any]] = None
        work_action = dict(action)
        if typ not in {"wait", "key", "keyboard", "shortcut", "extract"}:
            work_action, resolved_target = resolve_target(action)
            if isinstance(resolved_target, dict) and resolved_target.get("ok") is False:
                results.append({"type": typ, **resolved_target})
                break

        if typ in {"wait", "key", "keyboard", "shortcut", "select", "extract", "click", "double_click", "type", "type_text", "paste"}:
            if not flush_pending():
                break
            if typ in {"click", "double_click", "type", "type_text", "paste"}:
                action_result = _verified_dom_action(
                    settings, browser, work_action, current_observation_id,
                    window_index, tab_index, tab_handle,
                )
                internal_js_calls += int(action_result.pop("_js_calls", 0))
                compact_state_candidate = action_result.pop("_compact_state", None)
                if resolved_target:
                    action_result["resolved_target"] = {
                        k: resolved_target.get(k)
                        for k in ("element_id", "text", "role", "tag", "confidence")
                    }
                results.append(action_result)
                if not action_result.get("ok"):
                    break
                current_observation_id = None
            elif typ == "extract":
                extract_result = _extract_action(
                    settings, browser, action, window_index, tab_index, tab_handle,
                )
                internal_js_calls += int(extract_result.pop("_js_calls", 0))
                results.append(extract_result)
                if not extract_result.get("ok"):
                    break
            elif typ == "select":
                select_result = _select_action(
                    settings,
                    browser,
                    work_action,
                    current_observation_id,
                    window_index,
                    tab_index,
                    tab_handle,
                )
                internal_js_calls += int(select_result.pop("_js_calls", 0))
                if resolved_target:
                    select_result["resolved_target"] = {
                        k: resolved_target.get(k)
                        for k in ("element_id", "text", "role", "tag", "confidence")
                    }
                results.append(select_result)
                if not select_result.get("ok"):
                    break
                current_observation_id = None
            elif typ == "wait":
                wait_result = _wait_action(
                    settings,
                    browser,
                    action,
                    window_index,
                    tab_index,
                    initial_url,
                    tab_handle,
                )
                compact_state_candidate = wait_result.pop("_compact_state", None)
                internal_js_calls += int(wait_result.pop("_js_calls", 0))
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
                if eid:
                    focus_result = _run_json_js(
                        settings, browser, _batch_js([{"type": "focus", "element_id": eid}], current_observation_id),
                        window_index, tab_index, tab_handle,
                    )
                    internal_js_calls += 1
                    if not focus_result.get("ok"):
                        results.extend(focus_result.get("actions") or [{"ok": False, "error": "could_not_focus"}])
                        break
                    current_observation_id = None
                key_result = browser_press_key(
                    settings, browser=browser, key=str(action.get("key") or ""),
                    modifiers=action.get("modifiers") or [], window_index=window_index,
                    allow_foreground=allow_foreground,
                )
                results.append({
                    "type": "key",
                    "ok": bool(key_result.get("ok")),
                    "key": action.get("key"),
                    "foreground_required": bool(key_result.get("foreground_required")),
                    "reason": key_result.get("reason"),
                })
                if not key_result.get("ok"):
                    break
        else:
            pending.append(work_action)
    else:
        flush_pending()
    if pending:
        flush_pending()

    ok = all(bool(r.get("ok")) for r in results) if results else True
    response: Dict[str, Any] = {
        "ok": ok,
        "actions": results,
        "action_count": len(actions),
        "internal_js_calls": internal_js_calls,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    if return_state == "compact":
        if compact_state_candidate is not None:
            response["state"] = compact_state_candidate
        else:
            response["state"] = _run_json_js(
                settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
            )
            response["internal_js_calls"] += 1
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
        progress = _run_json_js(
            settings, browser, _light_state_js(), window_index, tab_index, tab_handle,
        )
        response["progress"] = {
            key: progress.get(key) for key in ("url", "title", "dom_revision")
            if progress.get(key) is not None
        }
        response["internal_js_calls"] += 1
    return response
