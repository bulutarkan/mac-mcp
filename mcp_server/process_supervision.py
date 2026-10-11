"""Stopping a child process group: one contract for every tool that starts one.

Tools start helpers (osascript, screencapture, shells, dialogs) in their own session
(start_new_session=True) so a timeout or a cancellation can stop the helper together
with anything it spawned.
"""
from __future__ import annotations

import os
import signal
import subprocess


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
