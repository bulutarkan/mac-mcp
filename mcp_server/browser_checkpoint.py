"""Resumable human checkpoints for sign-in, 2FA and similar steps.

An agent that reaches a password, one-time-code, passkey or push-approval
step should not type those secrets. It creates a checkpoint instead: the
result says awaiting_human with a checkpoint_id, the user gets a notification
to finish the step in the browser, and the agent waits (bounded) or checks
back later. The checkpoint resolves when that same tab no longer shows the
challenge; the agent then re-observes before acting again.

Only the browser, tab handle, origin, page title, a non-sensitive challenge
category and the caller's completed-action count are kept, in memory, for
CHECKPOINT_TTL_S. Detection looks at which kinds of fields exist, never at
their values. Nothing here blocks other tools or waits for an approval.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, status

CHECKPOINT_TTL_S = 30 * 60
MAX_CHECKPOINTS = 100
MAX_WAIT_S = 300
POLL_S = 2.0
CATEGORIES = ("password", "otp", "passkey", "push", "captcha", "other")

_LOCK = threading.Lock()
_CHECKPOINTS: Dict[str, Dict[str, Any]] = {}

# Field kinds only: the script never reads a value, label text is matched by keyword.
CHALLENGE_JS = r"""(function(){
function vis(el){try{var r=el.getBoundingClientRect(),st=getComputedStyle(el);return r.width>0&&r.height>0&&st.visibility!=='hidden'&&st.display!=='none';}catch(e){return false;}}
function any(sel){return Array.from(document.querySelectorAll(sel)).some(vis);}
var text=String((document.body&&document.body.innerText)||'').slice(0,20000).toLowerCase();
var category=null;
if(any('input[autocomplete="one-time-code"],input[name*="otp" i],input[id*="otp" i],input[name*="totp" i],input[inputmode="numeric"][maxlength="6"]')||/verification code|one-time code|two-factor|2-step|authenticator app|doğrulama kodu/.test(text))category='otp';
else if(any('input[type="password"]'))category='password';
else if(/passkey|security key|use your fingerprint|touch id|windows hello/.test(text))category='passkey';
else if(/approve (the )?sign.?in|check your phone|tap yes|push notification/.test(text))category='push';
else if(any('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"]'))category='captcha';
var out=JSON.stringify({ok:true,url:location.href,title:document.title.slice(0,120),challenge:category});
return btoa(unescape(encodeURIComponent(out)));
})()"""


def _origin(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _prune(now: float) -> None:
    for key in [k for k, v in _CHECKPOINTS.items() if now - v["created_at"] > CHECKPOINT_TTL_S]:
        _CHECKPOINTS.pop(key, None)
    while len(_CHECKPOINTS) >= MAX_CHECKPOINTS:
        _CHECKPOINTS.pop(min(_CHECKPOINTS, key=lambda k: _CHECKPOINTS[k]["created_at"]))


def _public(record: Dict[str, Any]) -> Dict[str, Any]:
    return {key: record.get(key) for key in (
        "checkpoint_id", "browser", "tab_handle", "origin", "title", "category", "completed_actions",
    )}


def create(
    browser: str,
    tab_handle: str,
    probe: Callable[[], Dict[str, Any]],
    notify: Optional[Callable[[str, str], Any]] = None,
    *,
    category: Optional[str] = None,
    message: Optional[str] = None,
    completed_actions: Optional[int] = None,
) -> Dict[str, Any]:
    if not tab_handle:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": "tab_target_required",
                                                          "message": "browser_checkpoint needs the tab_handle of the sign-in tab."})
    state = probe()
    detected = state.get("challenge")
    chosen = str(category or detected or "other").lower()
    if chosen not in CATEGORIES:
        chosen = "other"
    record = {
        "checkpoint_id": "bck_" + secrets.token_hex(8),
        "browser": browser,
        "tab_handle": tab_handle,
        "origin": _origin(state.get("url", "")),
        "title": str(state.get("title") or "")[:120],
        "category": chosen,
        "completed_actions": max(0, int(completed_actions or 0)),
        "created_at": time.time(),
    }
    with _LOCK:
        _prune(record["created_at"])
        _CHECKPOINTS[record["checkpoint_id"]] = record
    instruction = (str(message or "").strip()[:160]
                   or f"Finish the {chosen if chosen != 'other' else 'sign-in'} step in {browser} ({record['title'] or record['origin']}).")
    notified = False
    if notify is not None:
        try:
            notify("Mac MCP: your turn in the browser", instruction)
            notified = True
        except Exception:
            notified = False
    return {
        "ok": True, "status": "awaiting_human", **_public(record), "detected_challenge": detected,
        "instruction": instruction, "notified": notified,
        "next": "Do not type passwords or codes. Call browser_checkpoint action='wait' (wait_s up to 300) "
                "or 'status' later; after 'resolved', browser_observe before acting.",
    }


def check(
    checkpoint_id: str,
    probe: Callable[[], Dict[str, Any]],
    tab_exists: Callable[[], bool],
    *,
    wait_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    with _LOCK:
        _prune(time.time())
        record = dict(_CHECKPOINTS.get(str(checkpoint_id or "")) or {})
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"error": "unknown_checkpoint",
                                                        "message": "No such checkpoint (expired after 30 minutes, cancelled, or the server restarted)."})
    deadline = clock() + max(0.0, min(float(wait_s or 0), MAX_WAIT_S))
    while True:
        if not tab_exists():
            return {"ok": True, "status": "tab_closed", **_public(record),
                    "next": "The sign-in tab is gone; the checkpoint no longer matches. Start again from browser_list_tabs."}
        state = probe()
        challenge = state.get("challenge")
        if not challenge:
            with _LOCK:
                _CHECKPOINTS.pop(record["checkpoint_id"], None)
            origin = _origin(state.get("url", ""))
            return {"ok": True, "status": "resolved", **_public(record), "current_origin": origin,
                    "origin_changed": origin != record["origin"], "title_now": str(state.get("title") or "")[:120],
                    "observe_again": True,
                    "next": "The challenge is gone. browser_observe this tab before the next action."}
        if clock() >= deadline:
            return {"ok": True, "status": "awaiting_human", **_public(record), "challenge_now": challenge,
                    "next": "Still waiting for the person; call wait again or check status later."}
        sleep(POLL_S)


def handle_for(checkpoint_id: str) -> str:
    with _LOCK:
        record = _CHECKPOINTS.get(str(checkpoint_id or "")) or {}
    return str(record.get("tab_handle") or "")


def cancel(checkpoint_id: str) -> Dict[str, Any]:
    with _LOCK:
        removed = _CHECKPOINTS.pop(str(checkpoint_id or ""), None)
    return {"ok": True, "status": "cancelled" if removed else "unknown_checkpoint", "checkpoint_id": checkpoint_id}
