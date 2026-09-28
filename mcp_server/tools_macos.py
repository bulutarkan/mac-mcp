from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

from .security import Settings, truncate
from .tool_cancellation import (
    ToolCancelledError, cancellation_checkpoint, register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)


def _terminate_process_group(proc: subprocess.Popen[str], grace_s: float = 0.5) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_apple(
    script: str,
    timeout: int = 30,
    args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    timeout = max(1, min(int(timeout), 120))
    cancellation_checkpoint()
    command = ["osascript", "-e", script]
    if args is not None:
        # Keep semantic data out of AppleScript source and out of osascript option parsing.
        command.extend(["--", *[str(value) for value in args]])
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    cleanup_token = register_cancellation_cleanup(lambda: _terminate_process_group(proc))
    cancellation_checkpoint()
    try:
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        cancellation_checkpoint()
        stdout, _ = truncate((stdout_raw or "").strip(), 10_000)
        stderr, _ = truncate((stderr_raw or "").strip(), 10_000)
        return {"ok": proc.returncode == 0, "stdout": stdout, "stderr": stderr, "exit_code": proc.returncode}
    except ToolCancelledError:
        _terminate_process_group(proc)
        proc.wait()
        raise
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        proc.wait()
        return {"ok": False, "error": f"AppleScript timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        unregister_cancellation_cleanup(cleanup_token)


def run_applescript(settings: Settings, script: str, timeout_s: int = 30) -> Dict[str, Any]:
    """Run any AppleScript / osascript on macOS."""
    return _run_apple(script, timeout_s)


_NOTIFICATION_SCRIPT = r'''
on run argv
    set notificationTitle to item 1 of argv
    set notificationMessage to item 2 of argv
    set notificationSound to item 3 of argv
    display notification notificationMessage with title notificationTitle sound name notificationSound
end run
'''


def send_notification(settings: Settings, title: str, message: str, sound: str = "Pop") -> Dict[str, Any]:
    """Send a macOS notification using argv-bound data, never source interpolation."""
    return _run_apple(
        _NOTIFICATION_SCRIPT,
        args=[
            "" if title is None else str(title),
            "" if message is None else str(message),
            "" if sound is None else str(sound),
        ],
    )


def clipboard_get(settings: Settings) -> Dict[str, Any]:
    """Read the current clipboard contents."""
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        content, _ = truncate(proc.stdout, 50_000)
        return {"ok": True, "content": content, "length": len(proc.stdout)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clipboard_set(settings: Settings, content: str) -> Dict[str, Any]:
    """Write text to the clipboard."""
    try:
        proc = subprocess.run(["pbcopy"], input=content, capture_output=True, text=True, timeout=5)
        return {"ok": proc.returncode == 0, "chars_copied": len(content)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_app(settings: Settings, app_name: str) -> Dict[str, Any]:
    """Open a macOS application by name (e.g. 'Safari', 'Finder', 'Terminal')."""
    try:
        proc = subprocess.run(["open", "-a", app_name], capture_output=True, text=True, timeout=10)
        return {"ok": proc.returncode == 0, "app": app_name, "stderr": proc.stderr.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def open_url(settings: Settings, url: str) -> Dict[str, Any]:
    """Open a URL in the default browser."""
    try:
        proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=10)
        return {"ok": proc.returncode == 0, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_volume(settings: Settings, level: int) -> Dict[str, Any]:
    """Set system volume (0-100)."""
    level = max(0, min(100, level))
    script = f"set volume output volume {level}"
    return _run_apple(script)


def get_volume(settings: Settings) -> Dict[str, Any]:
    """Get current system volume."""
    script = "output volume of (get volume settings)"
    return _run_apple(script)


def set_brightness(settings: Settings, level: int) -> Dict[str, Any]:
    """Set screen brightness (0-100) via shell. Requires brightness tool."""
    try:
        val = max(0.0, min(1.0, level / 100.0))
        proc = subprocess.run(["brightness", str(val)], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            # Fallback via AppleScript
            return _run_apple(f'tell application "System Events" to set brightness of (item 1 of displays) to {val}')
        return {"ok": True, "brightness": level}
    except FileNotFoundError:
        return {"ok": False, "error": "brightness CLI not installed. Install with: brew install brightness"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def screenshot(settings: Settings, path: str = str(Path.home() / "Desktop" / "screenshot.png"),
               window: bool = False) -> Dict[str, Any]:
    """Take a screenshot and save to path."""
    args = ["screencapture", "-x"]  # -x: no sound
    if window:
        args.append("-w")  # interactive window select
    args.append(path)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return {"ok": proc.returncode == 0, "path": path, "stderr": proc.stderr.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_REMINDER_SCRIPT = r'''
on run argv
    set reminderTitle to item 1 of argv
    set reminderNotes to item 2 of argv
    tell application "Reminders"
        set r to make new reminder at end of default list
        set name of r to reminderTitle
        set body of r to reminderNotes
    end tell
end run
'''


def set_reminder(settings: Settings, title: str, notes: str = "",
                 due_date: Optional[str] = None) -> Dict[str, Any]:
    """Add a reminder to macOS Reminders app.
    due_date formats accepted: 'MM/DD/YYYY HH:MM AM/PM' or 'YYYY-MM-DD HH:MM'
    """
    reminder_args = [
        "" if title is None else str(title),
        "" if notes is None else str(notes),
    ]
    if due_date:
        # Parse in Python and embed only trusted integer components. User text still
        # travels exclusively through AppleScript argv.
        from datetime import datetime
        parsed = None
        for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"):
            try:
                parsed = datetime.strptime(due_date.strip(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return {"ok": False, "error": f"Could not parse due_date: '{due_date}'. Use MM/DD/YYYY HH:MM AM/PM"}

        script = f'''
on run argv
    set reminderTitle to item 1 of argv
    set reminderNotes to item 2 of argv
    tell application "Reminders"
        set r to make new reminder at end of default list
        set name of r to reminderTitle
        set body of r to reminderNotes
        set d to current date
        set year of d to {parsed.year}
        set month of d to {parsed.month}
        set day of d to {parsed.day}
        set hours of d to {parsed.hour}
        set minutes of d to {parsed.minute}
        set seconds of d to {parsed.second}
        set remind me date of r to d
    end tell
end run
'''
        return _run_apple(script, args=reminder_args)

    return _run_apple(_REMINDER_SCRIPT, args=reminder_args)


def get_running_apps(settings: Settings) -> Dict[str, Any]:
    """Get list of currently running macOS applications."""
    script = """
set appList to {}
tell application "System Events"
    set procs to every application process whose background only is false
    repeat with p in procs
        set end of appList to name of p
    end repeat
end tell
return appList
"""
    return _run_apple(script)
