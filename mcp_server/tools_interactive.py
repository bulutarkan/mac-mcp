from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Dict

from .security import Settings


def ask_user(
    settings: Settings,
    question: str,
    sender: str = "AI",
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """Ask the local user a question with a native macOS dialog.

    The total timeout covers both the question prompt and the answer input.
    Skip, cancel, or timeout returns ``response=None``.
    """
    try:
        total_timeout = max(1, min(int(timeout_s), 300))
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout_s must be a positive integer"}

    def _as_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    q_display = _as_escape(question[:800] + ("…" if len(question) > 800 else ""))
    s_display = _as_escape(sender[:50])

    script = f"""
set q_text to "{q_display}"
set s_name to "{s_display}"
set started_at to current date

set step1 to display dialog q_text ¬
    with title "🤖 " & s_name & " — Question" ¬
    buttons {{"Skip", "Answer →"}} ¬
    default button "Answer →" ¬
    giving up after {total_timeout}

if gave up of step1 then
    return "__TIMEOUT__"
end if
if button returned of step1 is "Skip" then
    return "__SKIP__"
end if

set remaining_seconds to {total_timeout} - ((current date) - started_at)
if remaining_seconds < 1 then
    return "__TIMEOUT__"
end if

set step2 to display dialog "Write your answer:" ¬
    with title "🤖 " & s_name & " — Answer" ¬
    default answer "" ¬
    buttons {{"Cancel", "Send ➤"}} ¬
    default button "Send ➤" ¬
    giving up after remaining_seconds

if gave up of step2 then
    return "__TIMEOUT__"
end if
if button returned of step2 is "Cancel" then
    return "__SKIP__"
end if

return text returned of step2
"""

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, _stderr = proc.communicate(timeout=total_timeout + 5)
        output = (stdout or "").strip()

        if not output or output == "__TIMEOUT__":
            return {"ok": True, "response": None, "timed_out": True, "skipped": False}
        if output == "__SKIP__":
            return {"ok": True, "response": None, "timed_out": False, "skipped": True}
        return {"ok": True, "response": output, "timed_out": False, "skipped": False}

    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        return {"ok": True, "response": None, "timed_out": True, "skipped": False}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
