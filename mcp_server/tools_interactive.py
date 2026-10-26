from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from mcp.server.fastmcp.utilities.types import Image

from . import owner_prompts
from .policy import current_policy_context
from .process_supervision import run_osascript
from .tool_cancellation import ToolCancelledError
from .security import Settings


_MAX_TIMEOUT_S = 300
_MAX_QUESTION_LENGTH = 800
_MAX_SENDER_LENGTH = 50
_MAX_BUTTON_LENGTH = 80

_TIMEOUT_SENTINEL = "__MAC_MCP_TIMEOUT__"
_CANCELLED_SENTINEL = "__MAC_MCP_CANCELLED__"
_SKIP_SENTINEL = "__MAC_MCP_SKIP__"

# Native dialogs are process-global. Never let multiple agents stack dialogs
# and leave each other waiting for an answer that is not visible.
_DIALOG_LOCK = threading.Lock()


def _normalize_timeout(timeout_s: int) -> int | None:
    try:
        return max(1, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return None


def _escape_applescript_text(value: str) -> str:
    """Escape text for an AppleScript string literal."""
    escaped: List[str] = []
    for char in value:
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char in {"\r", "\n"}:
            escaped.append("\\r")
        elif ord(char) < 32:
            escaped.append(" ")
        else:
            escaped.append(char)
    return "".join(escaped)


def _quoted_button_list(labels: List[str]) -> str:
    return "{" + ", ".join(
        f'"{_escape_applescript_text(label)}"' for label in labels
    ) + "}"


def _validate_question_and_sender(question: str, sender: str) -> tuple[str, str] | Dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        return {"ok": False, "error": "question must be a non-empty string"}
    if not isinstance(sender, str) or not sender.strip():
        return {"ok": False, "error": "sender must be a non-empty string"}

    question_display = question[:_MAX_QUESTION_LENGTH]
    if len(question) > _MAX_QUESTION_LENGTH:
        question_display += "…"
    return question_display, sender[:_MAX_SENDER_LENGTH]


def _validate_button_label(label: str, field_name: str) -> str | Dict[str, Any]:
    if not isinstance(label, str) or not label.strip():
        return {"ok": False, "error": f"{field_name} must be a non-empty string"}
    normalized = label.strip()
    if len(normalized) > _MAX_BUTTON_LENGTH:
        return {"ok": False, "error": f"{field_name} is too long (max {_MAX_BUTTON_LENGTH} characters)"}
    if any(ord(char) < 32 for char in normalized):
        return {"ok": False, "error": f"{field_name} contains unsupported control characters"}
    return normalized


def _run_native_script(script: str, total_timeout: int) -> Dict[str, Any]:
    """Run one native dialog while enforcing serialization and hard cleanup."""
    if not _DIALOG_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "prompt_busy",
            "message": "Another interactive dialog is already open; answer or close it before asking again.",
        }

    try:
        try:
            # A dialog gets a longer grace period, and is reaped after SIGKILL. The script, with
            # the question in it, goes on stdin rather than into the process list (#117).
            result = run_osascript(script, total_timeout + 5, grace_s=1.0, reap_s=1.0)
        except FileNotFoundError:
            return {"ok": False, "error": "osascript is not available on this Mac"}
        except ToolCancelledError:
            raise
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if result.timed_out:
            return {"ok": True, "output": _TIMEOUT_SENTINEL, "timed_out": True}

        output = result.stdout.strip()
        if result.returncode not in (0, None):
            error = result.stderr.strip() or f"osascript exited with code {result.returncode}"
            return {"ok": False, "error": error}
        return {"ok": True, "output": output, "timed_out": False}
    finally:
        _DIALOG_LOCK.release()


def _result_error(result: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": False, "error": result.get("error", "Interactive dialog failed")}


def _calling_agent_id() -> Optional[str]:
    try:
        return current_policy_context().agent_id
    except Exception:
        return None


def _ask_in_menu_app(question: str, sender: str, timeout_s: int,
                     suggestions: Optional[List[str]], allow_attachments: bool) -> Optional[Dict[str, Any]]:
    """The question in the menu app's own window; None when the app cannot show it."""
    payload = {"kind": "text", "question": question, "sender": sender, "agent_id": _calling_agent_id(),
               "suggestions": owner_prompts.clean_suggestions(suggestions or []),
               "allow_attachments": bool(allow_attachments)}
    answer = owner_prompts.ask(payload, timeout_s, allow_attachments=allow_attachments)
    if answer is None:
        return None
    base = {"ok": True, "ui": "menu_app"}
    if answer.get("action") == "timeout":
        return {**base, "response": None, "timed_out": True, "skipped": False, "attachments": []}
    if answer.get("action") == "skip":
        return {**base, "response": None, "timed_out": False, "skipped": True, "attachments": []}
    return {**base, "response": answer.get("text") or "", "timed_out": False, "skipped": False,
            "attachments": answer.get("attachments") or []}


def ask_user(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
    suggestions: Optional[List[str]] = None,
    allow_attachments: bool = True,
) -> Dict[str, Any]:
    """Ask the local user a free-form question: in the menu app when it runs, else a macOS dialog."""
    total_timeout = _normalize_timeout(timeout_s)
    if total_timeout is None:
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    common = _validate_question_and_sender(question, sender)
    if isinstance(common, dict):
        return common
    question_display, sender_display = common

    in_app = _ask_in_menu_app(question_display, sender_display, total_timeout, suggestions, allow_attachments)
    if in_app is not None:
        return in_app

    q_display = _escape_applescript_text(question_display)
    s_display = _escape_applescript_text(sender_display)

    script = f"""
set q_text to "{q_display}"
set s_name to "{s_display}"
set started_at to current date

try
    set step1 to display dialog q_text ¬
        with title "🤖 " & s_name & " — Question" ¬
        buttons {{"Skip", "Answer →"}} ¬
        default button "Answer →" ¬
        giving up after {total_timeout}

    if gave up of step1 then
        return "{_TIMEOUT_SENTINEL}"
    end if
    if button returned of step1 is "Skip" then
        return "{_SKIP_SENTINEL}"
    end if

    set remaining_seconds to {total_timeout} - ((current date) - started_at)
    if remaining_seconds < 1 then
        return "{_TIMEOUT_SENTINEL}"
    end if

    set step2 to display dialog "Write your answer:" ¬
        with title "🤖 " & s_name & " — Answer" ¬
        default answer "" ¬
        buttons {{"Cancel", "Send ➤"}} ¬
        default button "Send ➤" ¬
        giving up after remaining_seconds

    if gave up of step2 then
        return "{_TIMEOUT_SENTINEL}"
    end if
    if button returned of step2 is "Cancel" then
        return "{_SKIP_SENTINEL}"
    end if

    return text returned of step2
on error number -128
    return "{_SKIP_SENTINEL}"
end try
"""

    result = _run_native_script(script, total_timeout)
    if not result.get("ok"):
        return _result_error(result)
    output = result.get("output", "")
    if result.get("timed_out") or output == _TIMEOUT_SENTINEL or not output:
        return {"ok": True, "response": None, "timed_out": True, "skipped": False}
    if output == _SKIP_SENTINEL or output == _CANCELLED_SENTINEL:
        return {"ok": True, "response": None, "timed_out": False, "skipped": True}
    return {"ok": True, "response": output, "timed_out": False, "skipped": False}


def ask_user_mcp_result(result: Dict[str, Any]) -> Any:
    """ask_user for an MCP client: each picture the owner added becomes an image block (its base64
    leaves the JSON text, which keeps the file's name, path and size)."""
    attachments = result.get("attachments") if isinstance(result, dict) else None
    if not attachments:
        return result
    images: List[Image] = []
    listed: List[Dict[str, Any]] = []
    for item in attachments:
        item = dict(item)
        data = item.pop("data_base64", None)
        mime = item.pop("data_mime_type", None)
        item.pop("encoding", None)
        if data:
            images.append(Image(data=base64.b64decode(data), format="png" if mime == "image/png" else "jpeg"))
            item["shown_below"] = True
        listed.append(item)
    if not images:
        return result
    text = json.dumps({**result, "attachments": listed}, ensure_ascii=False, separators=(",", ":"))
    return [text, *images]


# Questions Mac MCP itself has put in front of the owner (an upload in Safari, say), listed while their dialog
# shows so the dashboard and the Live Island can say "Waiting for your answer" next to the security approvals.
_OWNER_QUESTIONS: Dict[str, Dict[str, Any]] = {}
_OWNER_QUESTIONS_LOCK = threading.Lock()


@contextmanager
def owner_question(tool: str, *, origin: Optional[str] = None, summary: Optional[str] = None,
                   timeout_s: int = 60) -> Iterator[str]:
    now = time.time()
    request_id = "q_" + uuid.uuid4().hex[:16]
    row = {"request_id": request_id, "tool": tool, "origin": origin, "tab_title": None,
           "reason_code": "OWNER_QUESTION", "target_summary": summary, "created_at": now,
           "expires_at": now + max(1, int(timeout_s)) + 5}
    with _OWNER_QUESTIONS_LOCK:
        _OWNER_QUESTIONS[request_id] = row
    try:
        yield request_id
    finally:
        with _OWNER_QUESTIONS_LOCK:
            _OWNER_QUESTIONS.pop(request_id, None)


def pending_owner_questions() -> List[Dict[str, Any]]:
    now = time.time()
    with _OWNER_QUESTIONS_LOCK:
        rows = [dict(row) for row in _OWNER_QUESTIONS.values() if row["expires_at"] > now]
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def _confirm_in_menu_app(question: str, sender: str, timeout_s: int, confirm: str, deny: str) -> Optional[Dict[str, Any]]:
    """A Yes/No question in the menu app's window, with only its two buttons; None when the app cannot
    show it. Only the confirm button confirms: deny, skip and timeout are all a no."""
    payload = {"kind": "confirm", "question": question, "sender": sender, "agent_id": _calling_agent_id(),
               "confirm_label": confirm, "deny_label": deny, "suggestions": [], "allow_attachments": False}
    answer = owner_prompts.ask(payload, timeout_s, allow_attachments=False)
    if answer is None:
        return None
    action = answer.get("action")
    decision = {"confirm": "confirmed", "deny": "denied", "timeout": "timed_out"}.get(str(action), "cancelled")
    return {"ok": True, "ui": "menu_app", "confirmed": decision == "confirmed", "decision": decision,
            "cancelled": decision == "cancelled", "timed_out": decision == "timed_out"}


def ask_confirmation(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
    confirm_label: str = "Yes",
    deny_label: str = "No",
) -> Dict[str, Any]:
    """Ask for explicit Yes/No confirmation; timeout and close always deny."""
    total_timeout = _normalize_timeout(timeout_s)
    if total_timeout is None:
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    common = _validate_question_and_sender(question, sender)
    if isinstance(common, dict):
        return common
    confirm = _validate_button_label(confirm_label, "confirm_label")
    if isinstance(confirm, dict):
        return confirm
    deny = _validate_button_label(deny_label, "deny_label")
    if isinstance(deny, dict):
        return deny
    if confirm.casefold() == deny.casefold():
        return {"ok": False, "error": "confirm_label and deny_label must be different"}
    if "cancel" in {confirm.casefold(), deny.casefold()}:
        return {"ok": False, "error": "confirm_label and deny_label cannot use the reserved label 'Cancel'"}

    question_display, sender_display = common
    in_app = _confirm_in_menu_app(question_display, sender_display, total_timeout, confirm, deny)
    if in_app is not None:
        return in_app

    buttons = [deny, confirm]
    script = f"""
set q_text to "{_escape_applescript_text(question_display)}"
set s_name to "{_escape_applescript_text(sender_display)}"
try
    set answer_dialog to display dialog q_text ¬
        with title "🤖 " & s_name & " — Confirmation" ¬
        buttons {_quoted_button_list(buttons)} ¬
        default button "{_escape_applescript_text(deny)}" ¬
        giving up after {total_timeout}
    if gave up of answer_dialog then
        return "{_TIMEOUT_SENTINEL}"
    end if
    return button returned of answer_dialog
on error number -128
    return "{_CANCELLED_SENTINEL}"
end try
"""

    result = _run_native_script(script, total_timeout)
    if not result.get("ok"):
        return _result_error(result)
    output = result.get("output", "")
    if result.get("timed_out") or output == _TIMEOUT_SENTINEL:
        return {
            "ok": True,
            "confirmed": False,
            "decision": "timed_out",
            "cancelled": False,
            "timed_out": True,
        }
    if output == _CANCELLED_SENTINEL or not output:
        return {
            "ok": True,
            "confirmed": False,
            "decision": "cancelled",
            "cancelled": True,
            "timed_out": False,
        }
    if output == confirm:
        return {
            "ok": True,
            "confirmed": True,
            "decision": "confirmed",
            "cancelled": False,
            "timed_out": False,
        }
    if output == deny:
        return {
            "ok": True,
            "confirmed": False,
            "decision": "denied",
            "cancelled": False,
            "timed_out": False,
        }
    return {"ok": False, "error": "Native dialog returned an unknown confirmation"}
