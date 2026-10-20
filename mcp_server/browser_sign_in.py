"""Sign in on the current page with a saved login, without the caller seeing the password (#163).

The page is asked where it is and what login fields it shows; the saved login is
chosen from that page host (never from anything the caller names); the username
and password are written straight into the fields by a page script that first
checks the page is still on the same host. Only booleans come back: the password
is never part of a tool argument, result, error, log or telemetry row.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from . import login_vault
from .security import Settings

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# Shared by both page scripts: the visible password field, and the username field
# that belongs to it (or, on a username-first step, a clearly named username field).
_FIELDS_JS = r"""
function vis(el){if(!el||el.disabled||el.readOnly)return false;var r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none';}
/* Login fields often sit inside web components (Reddit faceplate-text-input): look into open shadow roots too. */
function deep(root,sel,out){out=out||[];Array.prototype.forEach.call(root.querySelectorAll(sel),function(e){out.push(e);});
  Array.prototype.forEach.call(root.querySelectorAll('*'),function(e){if(e.shadowRoot)deep(e.shadowRoot,sel,out);});return out;}
/* The form a field belongs to, also when the field lives inside a component in that form. */
function formOf(el){var node=el;while(node){if(node.form)return node.form;var f=node.closest&&node.closest('form');if(f)return f;var r=node.getRootNode&&node.getRootNode();node=r&&r.host?r.host:null;}return null;}
function fields(){
  var pws=deep(document,'input[type=password]').filter(vis);
  var pw=pws.find(function(e){return /current-password/.test(e.autocomplete||'');})||pws.find(function(e){return !/new-password/.test(e.autocomplete||'');})||null;
  /* Only new-password fields: a sign-up or password-change form, never a place for a saved login. */
  var newOnly=!pw&&pws.length>0;
  var form=pw&&formOf(pw);
  var texts=deep(document,'input').filter(function(e){return vis(e)&&/^(text|email|tel)$/.test(String(e.getAttribute('type')||'text').toLowerCase())&&(!form||formOf(e)===form);});
  var score=function(e){var hint=((e.autocomplete||'')+' '+(e.name||'')+' '+(e.id||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.placeholder||'')).toLowerCase(),n=0;
    if(/username|email/.test(e.autocomplete||''))n+=10;if(String(e.type).toLowerCase()==='email')n+=6;if(/user|email|e-mail|login|account|mail|kullan|posta/.test(hint))n+=4;
    if(pw&&(e.compareDocumentPosition(pw)&Node.DOCUMENT_POSITION_FOLLOWING))n+=2;return n;};
  var ranked=texts.map(function(e){return [score(e),e];}).sort(function(a,b){return b[0]-a[0];});
  var user=!newOnly&&ranked.length&&(pw?ranked[0][0]>=2:ranked[0][0]>=4)?ranked[0][1]:null;
  return {pw:pw,user:user,newOnly:newOnly};
}
function b64(o){return btoa(unescape(encodeURIComponent(JSON.stringify(o))));}
"""


def _probe_js() -> str:
    return "(function(){" + _FIELDS_JS + """
var f=fields();
return b64({ok:true,host:location.hostname,protocol:location.protocol,
  password_field:!!f.pw,username_field:!!f.user,username_filled:!!(f.user&&f.user.value),new_password_only:f.newOnly});
})()"""


def _fill_js(host: str, username: Optional[str], password: Optional[str], submit: bool) -> str:
    # json.dumps makes each value a safe JavaScript string literal.
    return "(function(){" + _FIELDS_JS + f"""
var expected={json.dumps(host)},user={json.dumps(username)},pass={json.dumps(password)},submit={json.dumps(bool(submit))};
if(location.hostname!==expected)return b64({{ok:false,error:'page_changed'}});
var f=fields(),out={{ok:true,username:false,password:false,submitted:false}};
function put(el,v){{var proto=Object.getPrototypeOf(el),d=Object.getOwnPropertyDescriptor(proto,'value')||Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
  try{{el.focus({{preventScroll:true}});}}catch(e){{}}d.set.call(el,'');d.set.call(el,v);
  el.dispatchEvent(new InputEvent('input',{{bubbles:true,composed:true,inputType:'insertText',data:v}}));el.dispatchEvent(new Event('change',{{bubbles:true}}));
  return el.value===v;}}
if(user!==null&&f.user)out.username=put(f.user,user);
if(pass!==null&&f.pw)out.password=put(f.pw,pass);
user=null;pass=null;
if(submit&&out.password){{var form=formOf(f.pw);
  /* Press the page's own sign-in button first: many sites (Reddit) sign in from its click handler,
     and a type=button control ignores a programmatic form submit. */
  var isSignIn=function(b){{return String(b.type).toLowerCase()==='submit'||/^\\s*(log ?in|sign ?in|continue|giriş( yap)?|oturum aç)\\s*$/i.test(b.textContent||b.value||'');}};
  var pick=function(root){{return deep(root,'button,[role=button],input[type=submit]').filter(vis).find(isSignIn);}};
  var btn=(form&&pick(form))||pick(document);
  try{{if(btn&&!btn.disabled){{btn.click();out.submitted=true;}}
    else if(form){{if(form.requestSubmit)form.requestSubmit();else form.submit();out.submitted=true;}}}}catch(e){{}}}}
return b64(out);
}})()"""


def _choose(logins: list, account: Optional[str]) -> Dict[str, Any]:
    if account:
        wanted = str(account).strip().lower()
        for row in logins:
            if wanted in {str(row.get("label") or "").lower(), str(row.get("username") or "").lower(), str(row.get("id"))}:
                return row
        raise HTTPException(status.HTTP_404_NOT_FOUND, {
            "error": "account_not_found", "message": "No saved login with that account for this site.",
            "accounts": [row.get("label") or row.get("username") for row in logins],
        })
    if len(logins) > 1:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "choose_account", "message": "Several saved logins match this site; pass account=<one of accounts>.",
            "accounts": [row.get("label") or row.get("username") for row in logins],
        })
    return logins[0]


def browser_sign_in(
    settings: Settings, *, browser: str, tab_handle: Optional[str] = None, account: Optional[str] = None,
    submit: bool = False, window_index: int = 1, tab_index: Optional[int] = None, frame: Optional[str] = None,
) -> Dict[str, Any]:
    from .tools_browser import frame_scope

    # frame: a login form inside a child frame (part of its URL; Chrome with the companion). Every page
    # script then runs in that frame, so the site the login is chosen for, and checked against right
    # before filling, is the frame's own, never the page around it (#36).
    with frame_scope(frame):
        result = _sign_in(settings, browser=browser, tab_handle=tab_handle, account=account, submit=submit,
                          window_index=window_index, tab_index=tab_index)
    if str(frame or "").strip():
        result["frame"] = str(frame).strip()
    return result


def _sign_in(
    settings: Settings, *, browser: str, tab_handle: Optional[str], account: Optional[str],
    submit: bool, window_index: int, tab_index: Optional[int],
) -> Dict[str, Any]:
    from . import browser_tabs
    from .tools_browser_agent import (
        _norm_browser, _require_stable_handle_for_mutation, _run_json_js, _tab_lease, delegated_agent_identity,
    )

    b = _norm_browser(browser)
    _require_stable_handle_for_mutation(b, tab_handle, window_index, "browser_sign_in")
    # Filling is a change to the page: hold the tab like browser_act does.
    with _tab_lease(b, tab_handle, window_index, tab_index, mutation=True) as target:
        where = (target.browser, target.window_index, target.tab_index, target.tab_handle)
        probe = _run_json_js(settings, where[0], _probe_js(), where[1], where[2], where[3])
        host = str(probe.get("host") or "").lower()
        if probe.get("protocol") != "https:" and host not in _LOOPBACK:
            return {"ok": False, "error": "insecure_page", "site": host,
                    "message": "Saved logins are only filled on HTTPS pages."}
        if probe.get("new_password_only"):
            return {"ok": False, "error": "not_a_sign_in_form", "site": host,
                    "message": "This page asks for a new password (sign-up or password change); saved logins are "
                               "only filled into sign-in forms. Nothing was filled."}
        if not probe.get("password_field") and not probe.get("username_field"):
            return {"ok": False, "error": "no_login_form", "site": host,
                    "message": "No visible username or password field on this page."}
        logins = login_vault.logins_for_host(host)
        if not logins:
            saved = login_vault.site_allowing_subdomains_would_match(host)
            if saved:
                return {"ok": False, "error": "login_for_other_site", "site": host,
                        "message": f"The saved login is only for {saved}, not {host}. The owner can allow its "
                                   "subdomains in Mac MCP Settings > Saved Logins."}
            return {"ok": False, "error": "no_saved_login", "site": host,
                    "message": "No saved login for this site. The owner can add one in Mac MCP Settings > Saved Logins."}
        login = _choose(logins, account)
        fresh = None
        if delegated_agent_identity() is not None:
            # A delegated agent must still own this tab right before the fill.
            fresh, blocked = browser_tabs.revalidate_mutation_lease(
                where[0], str(where[3] or ""), int(getattr(target, "lease_generation", 0) or 0),
            )
            if blocked is not None:
                return {**dict(blocked), "ok": False, "automatic_retry": False}
        password = login_vault.secret_for(str(login["id"])) if probe.get("password_field") else None
        script = _fill_js(host, str(login["username"]), password, submit)
        try:
            filled = _run_json_js(settings, where[0], script, where[1], where[2], where[3], prevalidated_target=fresh)
        except HTTPException as exc:
            # Nothing about the page script, which holds the password, may reach the caller.
            raise HTTPException(exc.status_code, {"error": "sign_in_fill_failed",
                                                  "message": "The login could not be filled on this page."}) from None
        finally:
            password = None
            script = ""
    if not filled.get("ok"):
        return {"ok": False, "error": str(filled.get("error") or "fill_failed"), "site": host,
                "message": "The page moved to another site before the login was filled; nothing was filled."}
    result = {
        "ok": bool(filled.get("username") or filled.get("password")),
        "site": host,
        "account": login.get("label") or login.get("username"),
        "filled_username": bool(filled.get("username")),
        "filled_password": bool(filled.get("password")),
        "submitted": bool(filled.get("submitted")),
    }
    if not probe.get("password_field"):
        result["next_step"] = "Username step: continue to the password page, then call browser_sign_in again."
    elif not submit:
        result["next_step"] = "Filled. Click the page's sign-in button (or call again with submit=true)."
    return result
