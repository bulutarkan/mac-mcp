"""Read the Accessibility tree with the native Swift observer (ax_observe.swift).

The AppleScript observer sends one System Events Apple Event per attribute per
node; a 130-node Finder window took ~33 s. The native observer batches each
node's attributes into one Accessibility call and returns the same record
format in tens to hundreds of milliseconds.

It is compiled on first use in the background and enabled only in the
CLI-managed server. Until it is ready, or whenever it cannot answer (not built,
app not found by name, Accessibility refused), callers fall back to the
AppleScript observer, so observation never depends on it. MAC_MCP_AX_NATIVE=0
turns it off.

Observations go to one long-lived helper (``--serve``) that starts on first use and
stops after a minute without requests; a crashed helper is replaced, and a call that
finds it busy or broken runs its own one-shot helper as before. MAC_MCP_AX_WORKER=0
keeps the one-shot helper for every call.
"""
from __future__ import annotations

import atexit
import json
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import swift_build
from .tool_cancellation import (
    ToolCancelledError,
    cancellation_checkpoint,
    register_cancellation_cleanup,
    unregister_cancellation_cleanup,
)

_SOURCE = Path(__file__).resolve().with_name("ax_observe.swift")
_lock = threading.Lock()
_state = {"allowed": False, "executable": None, "building": False, "disabled": False}


def enable() -> None:
    """Allow the native observer and get its helper ready now, so the first observation
    after a start or an update does not take the AppleScript path while it builds."""
    with _lock:
        _state["allowed"] = True
    _executable()


def _build() -> None:
    try:
        directory = swift_build.cache_dir("MAC_MCP_AX_NATIVE_CACHE", "~/.mac-mcp/cache/ax-observe")
        executable = swift_build.compile_cached(_SOURCE, directory, "ax-observe")
        with _lock:
            _state["executable"] = str(executable)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        with _lock:
            _state["disabled"] = True
    finally:
        with _lock:
            _state["building"] = False


def _executable() -> Optional[str]:
    if os.getenv("MAC_MCP_AX_NATIVE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _lock:
        if not _state["allowed"] or _state["disabled"]:
            return None
        if _state["executable"]:
            return str(_state["executable"])
        if not _state["building"]:
            _state["building"] = True
            threading.Thread(target=_build, name="ax-observe-build", daemon=True).start()
    return None


class _WorkerTimeout(Exception):
    pass


class _Worker:
    """One long-lived ``ax-observe --serve`` process answering one request at a time (#185).

    Launching the helper cost ~40 ms per observation, more than walking a typical window;
    the worker pays it once. It stops after IDLE_S without requests, and a crashed or hung
    worker is killed and replaced on the next request.
    """

    IDLE_S = 60.0

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.busy = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.sequence = 0
        self._buffer = b""
        self._idle: Optional[threading.Timer] = None

    def _ensure(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [self.executable, "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
            )
            self._buffer = b""
        return self.proc

    @staticmethod
    def _close(proc: subprocess.Popen, *, gently: bool) -> None:
        try:
            if gently:
                proc.stdin.close()  # end of input: the helper exits on its own
                proc.wait(timeout=2)
            else:
                proc.kill()
                proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass

    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            self._close(proc, gently=False)

    def _stop_if_idle(self) -> None:
        if not self.busy.acquire(blocking=False):
            return
        try:
            proc, self.proc = self.proc, None
        finally:
            # Close outside the lock: a request arriving meanwhile starts a fresh helper
            # instead of finding this one busy and paying for a one-shot launch.
            self.busy.release()
        if proc is not None:
            self._close(proc, gently=True)

    def _arm_idle_timer(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self._idle = threading.Timer(self.IDLE_S, self._stop_if_idle)
        self._idle.daemon = True
        self._idle.start()

    def request(self, args: List[str], timeout_s: float) -> Tuple[int, str, str]:
        """(exit code, records, error) for one observation. Caller holds ``busy``.

        Raises _WorkerTimeout when no answer came in time, OSError/ValueError when the
        worker could not be started, died or answered something unreadable."""
        proc = self._ensure()
        self.sequence += 1
        line = json.dumps({"id": self.sequence, "args": args}) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        deadline = time.monotonic() + timeout_s
        fd = proc.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerTimeout()
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise OSError("the Accessibility helper exited")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\n", 1)
        answer = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(answer, dict) or answer.get("id") != self.sequence:
            raise ValueError("the Accessibility helper answered out of order")
        self._arm_idle_timer()
        return int(answer.get("code", 2)), str(answer.get("out") or ""), str(answer.get("err") or "")


_worker: Optional[_Worker] = None


def _worker_enabled() -> bool:
    return os.getenv("MAC_MCP_AX_WORKER", "1").strip().lower() not in {"0", "false", "no", "off"}


def _shared_worker(executable: str) -> _Worker:
    global _worker
    with _lock:
        if _worker is None or _worker.executable != executable:
            if _worker is not None:
                _worker._stop_if_idle()
            _worker = _Worker(executable)
        return _worker


def _stop_worker() -> None:
    worker = _worker
    if worker is not None:
        worker.kill()


atexit.register(_stop_worker)


def _result(code: int, out: str) -> Optional[Tuple[bool, str, str]]:
    # ax_observe.swift exits 3 (not trusted), 4 (app not found or ambiguous) or fails
    # otherwise: each means "use the AppleScript observer".
    if code != 0:
        return None
    return True, out, ""


def _timed_out(timeout_s: float) -> Tuple[bool, str, str]:
    return False, "", f"Accessibility observation timed out after {timeout_s}s"


def observe(
    *,
    app: Optional[str],
    app_pid: Optional[int],
    window_index: int,
    max_depth: int,
    max_children: int,
    max_nodes: int,
    timeout_s: float,
) -> Optional[Tuple[bool, str, str]]:
    """Return (ok, raw_records, error), or None when the AppleScript observer should run."""
    executable = _executable()
    if executable is None:
        return None
    if app_pid is not None:
        target = ["--pid", str(int(app_pid))]
    elif app:
        target = ["--name", str(app)]
    else:
        target = ["--frontmost"]
    args = [
        *target, "--window", str(int(window_index)), "--max-depth", str(int(max_depth)),
        "--max-children", str(int(max_children)), "--max-nodes", str(int(max_nodes)),
    ]
    return _call(executable, args, timeout_s)


def act(
    *,
    app_pid: int,
    element_id: str,
    op: str,
    name: Optional[str] = None,
    text: Optional[str] = None,
    replace: bool = True,
    expect: Optional[Dict[str, str]] = None,
    timeout_s: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Run one element-bound Accessibility action natively (press, a named action, or a text
    write), for windows System Events cannot reach: those on a desktop no display shows.

    `expect` holds the observed role/title/description; the helper refuses an element that
    no longer matches. Returns the helper's JSON answer, or None when the helper is unavailable."""
    executable = _executable()
    if executable is None:
        return None
    args = ["--mode", "act", "--pid", str(int(app_pid)), "--element", str(element_id), "--op", str(op)]
    if name:
        args += ["--name", str(name)]
    if text is not None:
        args += ["--text", str(text), "--replace", "true" if replace else "false"]
    for key, value in (expect or {}).items():
        if value is not None:
            args += [f"--expect-{key}", str(value)]
    answer = _call(executable, args, timeout_s)
    if answer is None:
        return None
    ok, out, error = answer
    if not ok:
        return {"ok": False, "reason_code": "NATIVE_ACTION_TIMEOUT", "error": error}
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def observe_frame(*, bundle_id: str, page_url: str, frame: str, max_nodes: int = 600,
                  timeout_s: float = 8.0) -> Optional[Dict[str, Any]]:
    """A browser page's child frame (the one whose URL contains `frame`) as Accessibility nodes,
    when the page is the tab its window shows (#384). None when the helper is unavailable."""
    executable = _executable()
    if executable is None:
        return None
    answer = _call(executable, ["--mode", "frame", "--bundle", str(bundle_id), "--page-url", str(page_url),
                                "--frame", str(frame), "--max-nodes", str(int(max_nodes))], timeout_s)
    if answer is None:
        return None
    ok, out, error = answer
    if not ok:
        return {"ok": False, "reason_code": "FRAME_OBSERVE_TIMEOUT", "error": error}
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def wake_web_accessibility(app_pid: int, timeout_s: float = 3.0) -> bool:
    """Ask a Chromium-based app for its full Accessibility tree, so its web controls offer AXPress (#82).

    It is what VoiceOver asks for; the app keeps it on while it runs. Returns whether the helper was asked."""
    executable = _executable()
    if executable is None:
        return False
    answer = _call(executable, ["--mode", "act", "--pid", str(int(app_pid)), "--element", "w1", "--op", "wake_web"],
                   timeout_s)
    return bool(answer and answer[0])


def _call(executable: str, args: List[str], timeout_s: float) -> Optional[Tuple[bool, str, str]]:
    cancellation_checkpoint()
    limit = max(0.5, float(timeout_s))
    if _worker_enabled():
        worker = _shared_worker(executable)
        # A second observation while the worker is busy runs as its own process instead of waiting.
        if worker.busy.acquire(blocking=False):
            cleanup = register_cancellation_cleanup(worker.kill)
            try:
                code, out, _ = worker.request(args, limit)
                return _result(code, out)
            except _WorkerTimeout:
                worker.kill()
                return _timed_out(timeout_s)
            except (OSError, ValueError):
                # Crashed, could not start, or garbled: replace it next time; this call
                # runs the one-shot helper below unless it was cancelled.
                worker.kill()
                cancellation_checkpoint()
            finally:
                unregister_cancellation_cleanup(cleanup)
                worker.busy.release()
    return _observe_once([executable, *args], timeout_s)


def _observe_once(command: List[str], timeout_s: float) -> Optional[Tuple[bool, str, str]]:
    """One helper process for one observation (the path before #185, and the fallback)."""
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return None
    cleanup = register_cancellation_cleanup(proc.kill)
    try:
        stdout, _ = proc.communicate(timeout=max(0.5, float(timeout_s)))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return _timed_out(timeout_s)
    except ToolCancelledError:
        proc.kill()
        proc.wait()
        raise
    finally:
        unregister_cancellation_cleanup(cleanup)
    return _result(proc.returncode, stdout.decode("utf-8", "replace"))
