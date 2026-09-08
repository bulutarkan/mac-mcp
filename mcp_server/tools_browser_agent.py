from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from mcp.server.fastmcp.utilities.types import Image

from .security import Settings
from .tools_browser import (
    _norm_browser,
    _resolve_tab_target,
    browser_execute_js,
    browser_press_key,
)

_MAX_OBSERVE_ELEMENTS = 240
_DEFAULT_OBSERVE_ELEMENTS = 120
_MAX_ACTIONS = 20
_VISUAL_MODES = {"none", "viewport", "element"}
_RETURN_STATE_MODES = {"none", "compact", "full"}
_GENERIC_QUERY_WORDS = {
    "button", "link", "input", "field", "select", "dropdown", "combobox", "option",
    "filter", "control", "element", "box", "menu", "tab", "checkbox", "radio",
}


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
) -> Dict[str, Any]:
    raw = browser_execute_js(
        settings, browser=browser, js=js, window_index=window_index, tab_index=tab_index,
    )
    return _decode_js_payload(raw)


def _b64_return(expression: str) -> str:
    return (
        "(function(){"
        "function __mcpB64(obj){return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}"
        f"return __mcpB64({expression});"
        "})()"
    )


def _browser_state_bootstrap() -> str:
    return r'''
function __mcpState(){
  var s=window.__macMcpBrowserAgent;
  if(!s){
    s=window.__macMcpBrowserAgent={
      counter:0,
      ids:new WeakMap(),
      elements:Object.create(null),
      pageToken:Math.random().toString(36).slice(2,10),
      mutationRevision:0,
      observations:Object.create(null),
      observer:null
    };
    try{
      s.observer=new MutationObserver(function(){s.mutationRevision+=1;});
      s.observer.observe(document.documentElement||document,{subtree:true,childList:true,attributes:true,characterData:true});
    }catch(e){}
  }
  return s;
}
function __mcpId(el,s){
  var id=s.ids.get(el);
  if(!id){id='e'+(++s.counter);s.ids.set(el,id);}
  s.elements[id]=el;
  return id;
}
function __mcpVisible(el){
  if(!el || el.nodeType!==1) return false;
  var st=getComputedStyle(el);
  if(st.display==='none'||st.visibility==='hidden'||parseFloat(st.opacity||'1')===0) return false;
  var r=el.getBoundingClientRect();
  if(r.width<1||r.height<1) return false;
  return r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
}
function __mcpActionable(el){
  var tag=(el.tagName||'').toLowerCase();
  var role=(el.getAttribute('role')||'').toLowerCase();
  if(['a','button','input','textarea','select','summary','details'].indexOf(tag)>=0) return true;
  if(['button','link','checkbox','radio','tab','menuitem','option','combobox','textbox','searchbox','switch','slider'].indexOf(role)>=0) return true;
  if(el.isContentEditable || el.hasAttribute('onclick')) return true;
  var cls=String(el.className||'');
  if(/collapseTitle|collapse-title|dropdown-toggle|select-trigger|clickable|toggle/i.test(cls)) return true;
  try{if(getComputedStyle(el).cursor==='pointer') return true;}catch(e){}
  var ti=el.getAttribute('tabindex');
  return ti!==null && Number(ti)>=0;
}
function __mcpText(el){
  var aria=el.getAttribute('aria-label')||'';
  var ph=el.getAttribute('placeholder')||'';
  var title=el.getAttribute('title')||'';
  var txt='';
  try{txt=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();}catch(e){}
  return (aria||ph||title||txt).slice(0,240);
}
function __mcpRole(el){
  var role=el.getAttribute('role');
  if(role) return role;
  var tag=(el.tagName||'').toLowerCase();
  if(tag==='a') return 'link';
  if(tag==='button') return 'button';
  if(tag==='select') return 'combobox';
  if(tag==='textarea') return 'textbox';
  if(tag==='input'){
    var t=(el.type||'text').toLowerCase();
    if(t==='checkbox') return 'checkbox';
    if(t==='radio') return 'radio';
    if(['button','submit','reset'].indexOf(t)>=0) return 'button';
    return 'textbox';
  }
  return '';
}
function __mcpRect(el){
  var r=el.getBoundingClientRect();
  var ox=(window.outerWidth-window.innerWidth);
  var oy=(window.outerHeight-window.innerHeight);
  var viewportX=window.screenX + Math.max(0, Math.round(ox/2));
  var viewportY=window.screenY + Math.max(0, Math.round(oy));
  return {
    viewport:{x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)},
    document:{x:Math.round(r.left+scrollX),y:Math.round(r.top+scrollY),w:Math.round(r.width),h:Math.round(r.height)},
    screen:{x:Math.round(viewportX+r.left),y:Math.round(viewportY+r.top),w:Math.round(r.width),h:Math.round(r.height),estimated:true}
  };
}
function __mcpDescribe(el,s){
  var tag=(el.tagName||'').toLowerCase();
  var rect=__mcpRect(el);
  var out={
    element_id:__mcpId(el,s),tag:tag,role:__mcpRole(el),text:__mcpText(el),
    viewport_rect:rect.viewport,screen_rect:rect.screen,actionable:__mcpActionable(el)
  };
  var aria=el.getAttribute('aria-label')||'', ph=el.getAttribute('placeholder')||'', name=el.getAttribute('name')||'', title=el.getAttribute('title')||'';
  if(aria) out.aria_label=aria.slice(0,120);
  if(ph) out.placeholder=ph.slice(0,100);
  if(name) out.name=name.slice(0,100);
  if(title) out.title=title.slice(0,100);
  if(tag==='a'&&el.href) out.href=String(el.href).slice(0,220);
  if(el.disabled===true) out.enabled=false;
  if(document.activeElement===el) out.focused=true;
  if(['input','textarea','select'].indexOf(tag)>=0) out.value=String(el.value||'').slice(0,160);
  if(tag==='input'&&el.type) out.input_type=String(el.type);
  if(typeof el.checked==='boolean'&&el.checked) out.checked=true;
  if(tag==='select'){
    out.options=Array.from(el.options||[]).slice(0,24).map(function(o){return {text:String(o.text||'').slice(0,90),value:String(o.value||'').slice(0,90),selected:!!o.selected};});
  }
  return out;
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
Object.keys(s.elements).forEach(function(k){{var e=s.elements[k];if(!e||!e.isConnected)delete s.elements[k];}});
var scope={scope_js};
var all=Array.from(document.querySelectorAll('*'));
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
    window_index: int, tab_index: Optional[int],
) -> Dict[str, Any]:
    requested = max_elements
    attempt = max_elements
    while True:
        try:
            payload = _run_json_js(
                settings, browser, _observe_js(scope, attempt),
                window_index=window_index, tab_index=tab_index,
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
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if image_data:
        return [text, Image(data=image_data, format="jpeg")]
    return text


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
    """Compact DOM observation with stable element IDs and optional viewport/element image."""
    _norm_browser(browser)
    window_index, tab_index = _resolve_tab_target(browser, tab_handle, window_index, tab_index)
    scope = str(scope or "interactive").lower().strip()
    if scope not in {"interactive", "visible", "content", "leaf"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be interactive, visible, content, or leaf.")
    visual = str(visual or "none").lower().strip()
    if visual not in _VISUAL_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visual must be none, viewport, or element.")
    max_elements = max(1, min(int(max_elements), _MAX_OBSERVE_ELEMENTS))
    started = time.perf_counter()
    payload = _observe_payload(
        settings, browser, scope, max_elements, window_index=window_index, tab_index=tab_index,
    )
    payload["duration_ms"] = int((time.perf_counter() - started) * 1000)

    image_data: Optional[bytes] = None
    if visual != "none":
        metrics = payload.get("window_metrics") or {}
        ox = max(0, int(metrics.get("outerWidth") or 0) - int(metrics.get("innerWidth") or 0))
        oy = max(0, int(metrics.get("outerHeight") or 0) - int(metrics.get("innerHeight") or 0))
        viewport_rect = {
            "x": int(metrics.get("screenX") or 0) + int(round(ox / 2)),
            "y": int(metrics.get("screenY") or 0) + oy,
            "w": int(metrics.get("innerWidth") or 1),
            "h": int(metrics.get("innerHeight") or 1),
        }
        target_rect = viewport_rect
        if visual == "element":
            if not element_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "element_id is required when visual='element'.")
            match = next((e for e in payload.get("elements", []) if e.get("element_id") == element_id), None)
            if not match:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"element_id not found in this observation: {element_id}")
            r = match.get("screen_rect") or {}
            target_rect = {
                "x": max(viewport_rect["x"], int(r.get("x") or viewport_rect["x"])),
                "y": max(viewport_rect["y"], int(r.get("y") or viewport_rect["y"])),
                "w": min(int(r.get("w") or 1), viewport_rect["w"]),
                "h": min(int(r.get("h") or 1), viewport_rect["h"]),
            }
        image_data, image_error = _capture_region(target_rect)
        payload["visual"] = {
            "mode": visual,
            "ok": image_data is not None,
            "rect": target_rect,
            "mime_type": "image/jpeg" if image_data else None,
        }
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
        element.get("name"), element.get("title"),
    ]
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
    score = max(level_score.get(best_query, 0.0), level_score.get(best_text, 0.0))
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
var out=[];
var all=Array.from(document.querySelectorAll('*'));
for(var i=0;i<all.length&&out.length<{max_candidates};i++){{
  var el=all[i]; if(!rendered(el))continue;
  var d=__mcpDescribe(el,s); d.actionable=__mcpActionable(el);
  if(actionableOnly && !d.actionable)continue;
  if(wantedRole&&norm(d.role)!==wantedRole)continue;
  var fields=[d.text||'',d.aria_label||'',d.placeholder||'',d.name||'',d.title||''];
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
                window_index=window_index, tab_index=tab_index,
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
            "placeholder": element.get("placeholder"), "value": element.get("value"),
            "href": element.get("href"), "viewport_rect": element.get("viewport_rect"),
            "screen_rect": element.get("screen_rect"), "actionable": element.get("actionable"),
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
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
var s=__mcpState();
var expected={obs_json};
if(expected && !(expected in s.observations)) return __mcpB64({{ok:false,error:'stale_observation',observe_again:true}});
var changed=expected ? (s.observations[expected]!==s.mutationRevision) : false;
var actions={actions_json};
var results=[];
function target(id){{var el=s.elements[id];return (el&&el.isConnected)?el:null;}}
function emit(el,type){{try{{el.dispatchEvent(new Event(type,{{bubbles:true}}));}}catch(e){{}}}}
for(var i=0;i<actions.length;i++){{
  var a=actions[i]||{{}}, type=String(a.type||'').toLowerCase().replace(/-/g,'_');
  var el=a.element_id?target(a.element_id):null;
  if(a.element_id && !el){{results.push({{index:i,type:type,element_id:a.element_id,ok:false,error:'stale_element',observe_again:true}});break;}}
  try{{
    if(type==='click'||type==='double_click'){{
      if(!el) throw new Error('element_id is required');
      el.scrollIntoView({{block:'center',inline:'nearest'}});
      var tag=(el.tagName||'').toLowerCase(), href=String(el.getAttribute('href')||'');
      var inputType=String(el.getAttribute('type')||'').toLowerCase();
      var mayNavigate=(tag==='a' && href && href!=='#' && !href.endsWith('#')) || ((tag==='button'||tag==='input') && inputType==='submit');
      var shouldDefer=(type==='click'&&i===actions.length-1&&mayNavigate);
      if(type==='double_click') {{
        el.dispatchEvent(new MouseEvent('dblclick',{{bubbles:true,cancelable:true,view:window}}));
      }} else if(shouldDefer) {{
        setTimeout(function(){{try{{el.click();}}catch(e){{}}}},0);
      }} else {{
        el.click();
      }}
      results.push({{index:i,type:type,element_id:a.element_id,ok:true,deferred:shouldDefer}});
    }} else if(type==='type'||type==='type_text'||type==='paste'){{
      if(!el) throw new Error('element_id is required');
      el.focus();
      var value=String(a.text==null?'':a.text);
      if(a.clear!==false){{try{{el.value='';}}catch(e){{}}}}
      try{{el.value=value;}}catch(e){{el.textContent=value;}}
      emit(el,'input');emit(el,'change');
      results.push({{index:i,type:type,element_id:a.element_id,ok:true,value:String(el.value||'').slice(0,200)}});
    }} else if(type==='select'){{
      if(!el) throw new Error('element_id is required');
      var wanted=String(a.option==null?'':a.option).trim().toLowerCase();
      var chosen=null;
      if((el.tagName||'').toLowerCase()==='select'){{
        var opts=Array.from(el.options||[]);
        chosen=opts.find(function(o){{return String(o.value).toLowerCase()===wanted||String(o.text).trim().toLowerCase()===wanted;}}) ||
               opts.find(function(o){{return String(o.text).trim().toLowerCase().indexOf(wanted)>=0;}});
        if(!chosen) throw new Error('option_not_found');
        el.value=chosen.value;emit(el,'input');emit(el,'change');
      }} else {{
        el.click();
        var candidates=Array.from(document.querySelectorAll('[role="option"],option,[role="menuitem"],li,button,a')).filter(__mcpVisible);
        chosen=candidates.find(function(o){{return __mcpText(o).toLowerCase()===wanted;}}) || candidates.find(function(o){{return __mcpText(o).toLowerCase().indexOf(wanted)>=0;}});
        if(!chosen) throw new Error('option_not_found');
        chosen.click();
      }}
      results.push({{index:i,type:type,element_id:a.element_id,ok:true,selected:chosen?__mcpText(chosen):wanted}});
    }} else if(type==='scroll'){{
      if(el) el.scrollIntoView({{block:String(a.block||'center'),inline:'nearest'}});
      else window.scrollBy(Number(a.dx||0),Number(a.dy||300));
      results.push({{index:i,type:type,element_id:a.element_id||null,ok:true}});
    }} else if(type==='focus'){{
      if(!el) throw new Error('element_id is required');el.focus();results.push({{index:i,type:type,element_id:a.element_id,ok:true}});
    }} else throw new Error('unsupported_batch_action:'+type);
  }}catch(e){{results.push({{index:i,type:type,element_id:a.element_id||null,ok:false,error:String(e&&e.message||e)}});break;}}
}}
return __mcpB64({{ok:results.every(function(r){{return r.ok;}}),actions:results,dom_changed_since_observe:changed,dom_revision:s.mutationRevision,url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}}}});
}})()'''


def _select_prepare_js(element_id: str, observation_id: Optional[str], option: Any) -> str:
    eid = json.dumps(str(element_id or ""))
    obs = json.dumps(observation_id)
    wanted = json.dumps(str(option if option is not None else ""))
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
function norm(v){{return String(v||'').normalize('NFKD').toLowerCase().replace(/[\u0300-\u036f]/g,'').replace(/\\s+/g,' ').trim();}}
var s=__mcpState(), expected={obs}, eid={eid}, wanted=norm({wanted});
if(expected && !(expected in s.observations)) return __mcpB64({{ok:false,error:'stale_observation',observe_again:true}});
var el=s.elements[eid];
if(!el||!el.isConnected) return __mcpB64({{ok:false,error:'stale_element',observe_again:true,element_id:eid}});
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
el.scrollIntoView({{block:'center',inline:'nearest'}});
el.click();
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
var originRect=origin&&origin.getBoundingClientRect?origin.getBoundingClientRect():{{left:0,top:0,width:0,height:0}};
var selectors='[role="option"],[role="menuitem"],option,li,[class*="option"],[class*="suggest"],[class*="dropdown"] a,[class*="menu"] a,button,a';
var all=Array.from(document.querySelectorAll(selectors)).filter(rendered);
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
chosen.scrollIntoView({{block:'nearest',inline:'nearest'}});
chosen.click();
return __mcpB64({{ok:true,found:true,selected:txt,tag:(chosen.tagName||'').toLowerCase(),role:chosen.getAttribute('role')||''}});
}})()'''


def _select_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    observation_id: Optional[str],
    window_index: int,
    tab_index: Optional[int],
) -> Dict[str, Any]:
    element_id = str(action.get("element_id") or "")
    if not element_id:
        return {"ok": False, "type": "select", "error": "element_id is required", "_js_calls": 0}
    option = action.get("option")
    timeout_s = max(0.2, min(float(action.get("timeout_s", 2.0)), 5.0))
    poll_s = max(0.05, min(float(action.get("poll_ms", 100)) / 1000.0, 0.5))
    started = time.perf_counter()
    js_calls = 0
    prep = _run_json_js(
        settings, browser, _select_prepare_js(element_id, observation_id, option),
        window_index, tab_index,
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
        found = _run_json_js(settings, browser, _select_option_js(element_id, option), window_index, tab_index)
        js_calls += 1
        if found.get("found"):
            stable_ms = max(100, min(int(action.get("stable_ms", 250)), 1000))
            settle_deadline = min(started + timeout_s, time.perf_counter() + 1.0)
            last_revision = None
            stable_since = time.perf_counter()
            while time.perf_counter() < settle_deadline:
                state = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index)
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


def _condition_js(action: Dict[str, Any], initial_url: str) -> str:
    kind = str(action.get("for") or action.get("condition") or "selector").lower().strip()
    if kind == "selector":
        selector = json.dumps(str(action.get("selector") or ""))
        expr = f"!!document.querySelector({selector})"
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
        expr = "document.readyState==='complete'"
    else:
        expr = "false"
    return f'''(function(){{
{_browser_state_bootstrap()}
function __mcpB64(obj){{return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));}}
return __mcpB64({{ok:true,matched:!!({expr}),url:location.href,title:document.title,scroll:{{x:scrollX,y:scrollY}},dom_revision:__mcpState().mutationRevision}});
}})()'''


def _wait_action(
    settings: Settings,
    browser: str,
    action: Dict[str, Any],
    window_index: int,
    tab_index: Optional[int],
    initial_url: str,
) -> Dict[str, Any]:
    kind = str(action.get("for") or action.get("condition") or "selector").lower().strip()
    timeout_s = max(0.1, min(float(action.get("timeout_s", 10)), 60.0))
    poll_s = max(0.05, min(float(action.get("poll_ms", 125)) / 1000.0, 1.0))
    started = time.perf_counter()
    js_calls = 0
    if kind == "dom_stable":
        stable_ms = max(100, min(int(action.get("stable_ms", 500)), 5000))
        last_revision = None
        stable_since = time.perf_counter()
        while time.perf_counter() - started < timeout_s:
            state = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index)
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
            state = _run_json_js(settings, browser, _condition_js(action, initial_url), window_index, tab_index)
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
    """Perform batched browser actions; targets may use element_id or semantic query/text/role."""
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
        initial_state = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index)
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
            window_index=window_index, tab_index=tab_index, max_results=1,
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
        out = _run_json_js(settings, browser, _batch_js(pending, current_observation_id), window_index, tab_index)
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
        if typ not in {"wait", "key", "keyboard", "shortcut"}:
            work_action, resolved_target = resolve_target(action)
            if isinstance(resolved_target, dict) and resolved_target.get("ok") is False:
                results.append({"type": typ, **resolved_target})
                break

        if typ in {"wait", "key", "keyboard", "shortcut", "select"}:
            if not flush_pending():
                break
            if typ == "select":
                select_result = _select_action(
                    settings, browser, work_action, current_observation_id, window_index, tab_index,
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
                wait_result = _wait_action(settings, browser, action, window_index, tab_index, initial_url)
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
                        window_index, tab_index,
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
            response["state"] = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index)
            response["internal_js_calls"] += 1
    elif return_state == "full":
        try:
            full = _observe_payload(
                settings, browser, "content", 120, window_index=window_index, tab_index=tab_index,
            )
            response["state"] = full
            response["internal_js_calls"] += 1
        except HTTPException as exc:
            if exc.status_code != status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
                raise
            response["state"] = _run_json_js(settings, browser, _light_state_js(), window_index, tab_index)
            response["state_fallback"] = "compact"
            response["full_state_error"] = "payload_too_large"
            response["internal_js_calls"] += 1
    return response
