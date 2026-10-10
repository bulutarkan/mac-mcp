"""Ask the person before Mac MCP sends an email or a message.

Sending reaches other people and cannot be taken back, so mac_app's send
actions ask first unless the person turned that off in Settings > Apps
(settings.json: apps.<app>.confirm_send, default true). The question is a
native floating panel (send_approval.swift) that shows who it goes to and the
full text; if the panel cannot be shown, a plain macOS confirmation dialog is
used instead, and if neither can be shown nothing is sent. Only an explicit
click on Send approves; Cancel, closing the panel or the timeout never do.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import swift_build
from .runtime_settings import load_runtime_settings
from .security import Settings

APPROVAL_TIMEOUT_S = 120
_SOURCE = Path(__file__).resolve().with_name("send_approval.swift")
_LOCK = threading.Lock()
_FOOTNOTE = "Nothing is sent unless you click Send. You can turn this confirmation off in Mac MCP Settings › Apps."


def confirm_send_enabled(app: str) -> bool:
    """apps.<app>.confirm_send from settings.json; anything but an explicit false keeps asking."""
    apps = load_runtime_settings().get("apps")
    entry = apps.get(app) if isinstance(apps, dict) else None
    return not (isinstance(entry, dict) and entry.get("confirm_send") is False)


def _helper() -> Path:
    directory = swift_build.cache_dir("MAC_MCP_SEND_APPROVAL_CACHE", "~/.mac-mcp/cache/send-approval")
    return swift_build.compile_cached(_SOURCE, directory, "send-approval")


def _panel(request: Dict[str, Any], timeout_s: int) -> Optional[str]:
    try:
        helper = _helper()
        proc = subprocess.run(
            [str(helper)], input=json.dumps(request), capture_output=True, text=True,
            timeout=timeout_s + 15, check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception:
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            decision = json.loads(line).get("decision")
        except (ValueError, AttributeError):
            continue
        if decision in {"send", "cancel", "timeout"}:
            return decision
    return None


def _dialog(settings: Optional[Settings], request: Dict[str, Any], timeout_s: int) -> Optional[str]:
    from .tools_interactive import ask_confirmation

    lines = [request["title"], ""] + [f"{field['label']}: {field['value']}" for field in request["fields"]]
    text = request["body"]
    lines += ["", text if len(text) <= 300 else text[:297] + "..."]
    try:
        answer = ask_confirmation(
            settings, "\n".join(lines)[:790], sender="Mac MCP", timeout_s=timeout_s,
            confirm_label="Send", deny_label="Don't Send",
        )
    except Exception:
        return None
    if not answer.get("ok", True) and "error" in answer:
        return None
    if answer.get("timed_out"):
        return "timeout"
    return "send" if answer.get("confirmed") is True else "cancel"


def request_send_approval(
    settings: Optional[Settings], *, app: str, app_bundle_id: str, title: str,
    fields: List[Dict[str, str]], body: str, body_label: str = "Message",
    requester: str = "an AI agent through Mac MCP", timeout_s: int = APPROVAL_TIMEOUT_S,
) -> Dict[str, Any]:
    """Return {"approved": bool, "decision": ..., "ui": ...}; only a click on Send approves."""
    if not confirm_send_enabled(app):
        return {"approved": True, "decision": "not_asked", "ui": None,
                "reason": f"Settings > Apps: confirmation before sending {app} is off"}
    request = {
        "title": title, "subtitle": f"Requested by {requester}", "app_bundle_id": app_bundle_id,
        "fields": [field for field in fields if field.get("value")], "body": body, "body_label": body_label,
        "footnote": _FOOTNOTE, "timeout_seconds": int(timeout_s),
    }
    with _LOCK:  # one question on screen at a time
        decision, ui = _panel(request, timeout_s), "panel"
        if decision is None:
            decision, ui = _dialog(settings, request, timeout_s), "dialog"
    if decision is None:
        return {"approved": False, "decision": "unavailable", "ui": None,
                "reason": "No confirmation could be shown, so nothing was sent."}
    return {"approved": decision == "send", "decision": decision, "ui": ui}
