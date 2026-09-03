from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Any, Dict, List, Optional

from .security import Settings


_MAX_TIMEOUT_S = 300
_MAX_QUESTION_LENGTH = 800
_MAX_SENDER_LENGTH = 50
_MAX_BUTTON_LENGTH = 80
_MIN_CHOICES = 2
_MAX_CHOICES = 6

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


def _validate_choices(
    choices: List[str],
    default_choice: Optional[str],
) -> tuple[List[str], Optional[str]] | Dict[str, Any]:
    if not isinstance(choices, list):
        return {"ok": False, "error": "choices must be a list of strings"}
    if not _MIN_CHOICES <= len(choices) <= _MAX_CHOICES:
        return {
            "ok": False,
            "error": f"choices must contain between {_MIN_CHOICES} and {_MAX_CHOICES} options",
        }

    normalized: List[str] = []
    seen = set()
    for index, choice in enumerate(choices):
        label = _validate_button_label(choice, f"choices[{index}]")
        if isinstance(label, dict):
            return label
        if label.casefold() == "cancel":
            return {"ok": False, "error": "choices cannot use the reserved label 'Cancel'"}
        if label.casefold() in seen:
            return {"ok": False, "error": "choices must be unique"}
        seen.add(label.casefold())
        normalized.append(label)

    if default_choice is None:
        return normalized, None
    default_label = _validate_button_label(default_choice, "default_choice")
    if isinstance(default_label, dict):
        return default_label
    if default_label not in normalized:
        return {"ok": False, "error": "default_choice must match one of choices"}
    return normalized, default_label


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass

    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _run_native_script(script: str, total_timeout: int) -> Dict[str, Any]:
    """Run one native dialog while enforcing serialization and hard cleanup."""
    if not _DIALOG_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "error": "prompt_busy",
            "message": "Another interactive dialog is already open; answer or close it before asking again.",
        }

    proc: subprocess.Popen[str] | None = None
    try:
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=total_timeout + 5)
        except FileNotFoundError:
            return {"ok": False, "error": "osascript is not available on this Mac"}
        except subprocess.TimeoutExpired:
            if proc is not None:
                _terminate_process_group(proc)
            return {"ok": True, "output": _TIMEOUT_SENTINEL, "timed_out": True}
        except Exception as exc:
            if proc is not None:
                _terminate_process_group(proc)
            return {"ok": False, "error": str(exc)}

        output = (stdout or "").strip()
        return_code = getattr(proc, "returncode", 0)
        if return_code not in (0, None):
            error = (stderr or "").strip() or f"osascript exited with code {return_code}"
            return {"ok": False, "error": error}
        return {"ok": True, "output": output, "timed_out": False}
    finally:
        _DIALOG_LOCK.release()


def _result_error(result: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": False, "error": result.get("error", "Interactive dialog failed")}


def ask_user(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """Ask the local user a free-form question with a native macOS dialog."""
    total_timeout = _normalize_timeout(timeout_s)
    if total_timeout is None:
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    common = _validate_question_and_sender(question, sender)
    if isinstance(common, dict):
        return common
    question_display, sender_display = common
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


def ask_choice(
    settings: Settings,
    question: str,
    choices: List[str],
    sender: str = "AI",
    timeout_s: int = 60,
    default_choice: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask the local user to select one option from native dialog buttons."""
    total_timeout = _normalize_timeout(timeout_s)
    if total_timeout is None:
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    common = _validate_question_and_sender(question, sender)
    if isinstance(common, dict):
        return common
    validated_choices = _validate_choices(choices, default_choice)
    if isinstance(validated_choices, dict):
        return validated_choices
    normalized_choices, default_label = validated_choices
    question_display, sender_display = common

    buttons = ["Cancel", *normalized_choices]
    default_button = default_label or "Cancel"
    script = f"""
set q_text to "{_escape_applescript_text(question_display)}"
set s_name to "{_escape_applescript_text(sender_display)}"
try
    set answer_dialog to display dialog q_text ¬
        with title "🤖 " & s_name & " — Choose" ¬
        buttons {_quoted_button_list(buttons)} ¬
        default button "{_escape_applescript_text(default_button)}" ¬
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
            "choice": None,
            "choice_index": None,
            "cancelled": False,
            "timed_out": True,
        }
    if output in {_CANCELLED_SENTINEL, "Cancel", ""}:
        return {
            "ok": True,
            "choice": None,
            "choice_index": None,
            "cancelled": True,
            "timed_out": False,
        }
    if output not in normalized_choices:
        return {"ok": False, "error": "Native dialog returned an unknown choice"}
    return {
        "ok": True,
        "choice": output,
        "choice_index": normalized_choices.index(output),
        "cancelled": False,
        "timed_out": False,
    }


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
