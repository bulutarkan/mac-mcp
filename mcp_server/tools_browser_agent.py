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
    viewport_rect:rect.viewport,screen_rect:rect.screen
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
  elements.push(__mcpDescribe(el,s));
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
    scope: str = "interactive",
    max_elements: int = _DEFAULT_OBSERVE_ELEMENTS,
    visual: str = "none",
    element_id: Optional[str] = None,
) -> Any:
    """Compact DOM observation with stable element IDs and optional viewport/element image."""
    _norm_browser(browser)
    scope = str(scope or "interactive").lower().strip()
    if scope not in {"interactive", "visible"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be interactive or visible.")
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


def _score_candidate(element: Dict[str, Any], query: str, role: Optional[str], text: Optional[str]) -> float:
    option_text = " ".join(
        f"{item.get('text', '')} {item.get('value', '')}"
        for item in (element.get("options") or []) if isinstance(item, dict)
    )
    fields = [
        element.get("text"), element.get("aria_label"), element.get("placeholder"),
        element.get("name"), element.get("title"), element.get("role"), element.get("tag"),
        option_text,
    ]
    hay = _normalize_text(" ".join(str(v or "") for v in fields))
    q = _normalize_text(query)
    raw_tokens = [t for t in q.split() if t not in _GENERIC_QUERY_WORDS]
    q_tokens = [t for t in raw_tokens if len(t) >= 2 or not t.isdigit()]
    score = 0.0
    if q and q in hay:
        score += 0.58
    if q_tokens:
        matched = sum(1 for t in q_tokens if t in hay)
        ratio = matched / len(q_tokens)
        score += 0.36 * ratio
        if ratio == 1.0:
            score += 0.20
    if text:
        wanted = _normalize_text(text)
        actual = _normalize_text(element.get("text") or element.get("aria_label") or element.get("placeholder"))
        if wanted == actual:
            score += 0.35
        elif wanted and wanted in actual:
            score += 0.22
    if role:
        if _normalize_text(element.get("role")) == _normalize_text(role):
            score += 0.22
        else:
            score -= 0.15
    if element.get("actionable"):
        score += 0.08
    else:
        score -= 0.12
        if str(element.get("tag") or "").lower() in {"html", "body", "main", "section", "article", "div"}:
            score -= 0.16
        if len(str(element.get("text") or "")) > 160:
            score -= 0.12
    return max(0.0, min(1.0, score))


def browser_find(
    settings: Settings,
    browser: str,
    query: str,
    role: Optional[str] = None,
    text: Optional[str] = None,
    window_index: int = 1,
    tab_index: Optional[int] = None,
    max_results: int = 5,
) -> Dict[str, Any]:
    """Find a visible DOM target by fuzzy text/role semantics and return stable element IDs."""
    if not str(query or "").strip() and not text and not role:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "query, text, or role is required.")
    started = time.perf_counter()
    max_results = max(1, min(int(max_results), 10))

    def rank(payload: Dict[str, Any]) -> List[Tuple[float, Dict[str, Any]]]:
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for element in payload.get("elements", []):
            score = _score_candidate(element, str(query or ""), role, text)
            if score >= 0.30:
                scored.append((score, element))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    payload = _observe_payload(
        settings, browser, "interactive", 180, window_index=window_index, tab_index=tab_index,
    )
    scored = rank(payload)
    search_scope = "interactive"
    if not scored:
        payload = _observe_payload(
            settings, browser, "visible", 180, window_index=window_index, tab_index=tab_index,
        )
        scored = rank(payload)
        search_scope = "visible_fallback"

    matches = []
    for score, element in scored[:max_results]:
        matches.append({
            "element_id": element.get("element_id"),
            "confidence": round(score, 3),
            "tag": element.get("tag"), "role": element.get("role"),
            "text": element.get("text"), "aria_label": element.get("aria_label"),
            "placeholder": element.get("placeholder"), "value": element.get("value"),
            "href": element.get("href"), "viewport_rect": element.get("viewport_rect"),
            "screen_rect": element.get("screen_rect"),
        })
    return {
        "ok": True,
        "observation_id": payload.get("observation_id"),
        "dom_revision": payload.get("dom_revision"),
        "url": payload.get("url"),
        "title": payload.get("title"),
        "query": query,
        "search_scope": search_scope,
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
      if(type==='double_click') {{
        el.dispatchEvent(new MouseEvent('dblclick',{{bubbles:true,cancelable:true,view:window}}));
      }} else if(i===actions.length-1) {{
        setTimeout(function(){{try{{el.click();}}catch(e){{}}}},0);
      }} else {{
        el.click();
      }}
      results.push({{index:i,type:type,element_id:a.element_id,ok:true,deferred:(type==='click'&&i===actions.length-1)}});
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
    return_state: str = "compact",
) -> Dict[str, Any]:
    """Perform a batch of stable-ID DOM actions with internal waits and compact post-state."""
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions must be a non-empty list.")
    if len(actions) > _MAX_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"actions may contain at most {_MAX_ACTIONS} items.")
    return_state = str(return_state or "compact").lower().strip()
    if return_state not in _RETURN_STATE_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "return_state must be none, compact, or full.")

    started = time.perf_counter()
    results: List[Dict[str, Any]] = []
    internal_js_calls = 0
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

    def flush_pending() -> bool:
        nonlocal pending, internal_js_calls
        if not pending:
            return True
        out = _run_json_js(settings, browser, _batch_js(pending, observation_id), window_index, tab_index)
        internal_js_calls += 1
        if not out.get("ok") and out.get("error") == "stale_observation":
            results.append({"ok": False, "error": "stale_observation", "observe_again": True})
            pending = []
            return False
        results.extend(out.get("actions") or [])
        pending = []
        return bool(out.get("ok"))

    for action in actions:
        if not isinstance(action, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each action must be an object.")
        typ = str(action.get("type") or "").lower().replace("-", "_")
        if typ in {"wait", "key", "keyboard", "shortcut"}:
            if not flush_pending():
                break
            if typ == "wait":
                wait_result = _wait_action(settings, browser, action, window_index, tab_index, initial_url)
                compact_state_candidate = wait_result.pop("_compact_state", None)
                internal_js_calls += int(wait_result.pop("_js_calls", 0))
                results.append(wait_result)
                if not wait_result.get("matched") and action.get("required", True):
                    break
            else:
                eid = action.get("element_id")
                if eid:
                    focus_result = _run_json_js(
                        settings, browser, _batch_js([{"type": "focus", "element_id": eid}], observation_id),
                        window_index, tab_index,
                    )
                    internal_js_calls += 1
                    if not focus_result.get("ok"):
                        results.extend(focus_result.get("actions") or [{"ok": False, "error": "could_not_focus"}])
                        break
                key_result = browser_press_key(
                    settings, browser=browser, key=str(action.get("key") or ""),
                    modifiers=action.get("modifiers") or [], window_index=window_index,
                )
                results.append({"type": "key", "ok": bool(key_result.get("ok")), "key": action.get("key")})
        else:
            pending.append(action)
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
        full = _run_json_js(settings, browser, _observe_js("interactive", 80), window_index, tab_index)
        response["state"] = full
        response["internal_js_calls"] += 1
    return response
