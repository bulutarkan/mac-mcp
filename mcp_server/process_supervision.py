"""Child processes for tools: stopping a process group, and running osascript.

Tools start helpers (osascript, screencapture, shells, dialogs) in their own session
(start_new_session=True) so a timeout or a cancellation can stop the helper together
with anything it spawned.
"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

from .tool_cancellation import (
    cancellation_checkpoint,
    register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)


def terminate_process_group(proc: subprocess.Popen, grace_s: float = 0.5, *, reap_s: float = 0.0) -> None:
    """SIGTERM the process group, give it `grace_s` to exit, then SIGKILL it.

    Does nothing if the process already exited. With `reap_s`, also waits that long
    for the killed process to be reaped. A group that is already gone, or cannot be
    signalled, is not an error: this runs in cleanup paths.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        return
    if reap_s > 0:
        try:
            proc.wait(timeout=reap_s)
        except subprocess.TimeoutExpired:
            pass


@dataclass(frozen=True)
class OsascriptResult:
    """What one osascript run did, before any tool maps it to its own response shape."""

    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def run_osascript(
    script: str,
    timeout_s: float,
    *,
    args: Optional[Sequence[str]] = None,
    script_on_stdin: bool = True,
    grace_s: float = 0.5,
    reap_s: float = 0.0,
) -> OsascriptResult:
    """Run one AppleScript in its own process group.

    The script goes on stdin by default (process arguments are visible to other
    processes); `args` reach the script's run handler as argv, so data stays out of the
    script source. A timeout stops the group and returns timed_out=True. A cancelled tool
    call, or any other failure while waiting, stops the group and re-raises. Errors
    starting osascript itself propagate unchanged.
    """
    command = ["osascript", "-" if script_on_stdin else "-e"]
    if not script_on_stdin:
        command.append(script)
    if args is not None:
        # After "-" osascript parses no more options and would hand a "--" to the script
        # as its first argument; after -e <script>, "--" ends option parsing.
        command.extend([*([] if script_on_stdin else ["--"]), *[str(value) for value in args]])
    cancellation_checkpoint()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if script_on_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def stop() -> None:
        terminate_process_group(proc, grace_s, reap_s=reap_s)

    cleanup_token = register_cancellation_cleanup(stop)
    try:
        cancellation_checkpoint()
        if script_on_stdin:
            stdout, stderr = proc.communicate(input=script, timeout=timeout_s)
        else:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        cancellation_checkpoint()
    except subprocess.TimeoutExpired:
        stop()
        proc.wait()
        return OsascriptResult(None, "", "", timed_out=True)
    except BaseException:
        stop()
        proc.wait()
        raise
    finally:
        unregister_cancellation_cleanup(cleanup_token)
    return OsascriptResult(proc.returncode, stdout or "", stderr or "")
