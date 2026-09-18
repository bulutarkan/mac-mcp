from __future__ import annotations

import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


class ToolCancelledError(RuntimeError):
    """Raised cooperatively inside synchronous tool bodies after client cancellation."""

    def __init__(self, reason: str = "client_cancelled") -> None:
        self.reason = str(reason or "client_cancelled")
        super().__init__(self.reason)


@dataclass
class ToolCancellationContext:
    cleanup_wait_s: float = 1.5
    _event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _callbacks: Dict[str, Callable[[], None]] = field(default_factory=dict, init=False, repr=False)
    _cleanup_threads: list[threading.Thread] = field(default_factory=list, init=False, repr=False)
    reason: Optional[str] = None
    requested_at: Optional[float] = None
    worker_finished: bool = False
    cleanup_confirmed: bool = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise ToolCancelledError(self.reason or "client_cancelled")

    def register_cleanup(self, callback: Callable[[], None]) -> str:
        token = "cleanup_" + uuid.uuid4().hex[:12]
        run_now = False
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                self._callbacks[token] = callback
        if run_now:
            self._start_cleanup(callback, token)
        return token

    def unregister_cleanup(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._callbacks.pop(str(token), None)

    def _start_cleanup(self, callback: Callable[[], None], label: str) -> None:
        def runner() -> None:
            try:
                callback()
            except BaseException:
                # Cancellation cleanup is best-effort; the caller separately marks
                # uncertain side effects when completion cannot be proven.
                pass
        thread = threading.Thread(target=runner, name=f"mac-mcp-cancel-{label}", daemon=True)
        with self._lock:
            self._cleanup_threads.append(thread)
        thread.start()

    def cancel(self, reason: str = "client_cancelled") -> None:
        callbacks: list[tuple[str, Callable[[], None]]] = []
        with self._lock:
            if self._event.is_set():
                return
            self.reason = str(reason or "client_cancelled")
            self.requested_at = time.time()
            self._event.set()
            callbacks = list(self._callbacks.items())
            self._callbacks.clear()
        for token, callback in callbacks:
            self._start_cleanup(callback, token)

    def mark_worker_finished(self) -> None:
        self.worker_finished = True
        self.cleanup_confirmed = True

    def wait_cleanup_threads(self, timeout_s: Optional[float] = None) -> bool:
        deadline = time.monotonic() + max(0.0, float(self.cleanup_wait_s if timeout_s is None else timeout_s))
        while True:
            with self._lock:
                alive = [thread for thread in self._cleanup_threads if thread.is_alive()]
                self._cleanup_threads = alive
            if not alive:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            alive[0].join(timeout=min(0.05, remaining))


_CURRENT_TOOL_CANCELLATION: contextvars.ContextVar[Optional[ToolCancellationContext]] = contextvars.ContextVar(
    "mac_mcp_tool_cancellation", default=None
)


def current_tool_cancellation() -> Optional[ToolCancellationContext]:
    return _CURRENT_TOOL_CANCELLATION.get()


def set_tool_cancellation(context: ToolCancellationContext):
    return _CURRENT_TOOL_CANCELLATION.set(context)


def reset_tool_cancellation(token) -> None:
    _CURRENT_TOOL_CANCELLATION.reset(token)


def cancellation_checkpoint() -> None:
    context = current_tool_cancellation()
    if context is not None:
        context.checkpoint()


def cancellable_sleep(seconds: float) -> None:
    duration = max(0.0, float(seconds))
    context = current_tool_cancellation()
    if context is None:
        time.sleep(duration)
        return
    if context._event.wait(timeout=duration):
        context.checkpoint()


def register_cancellation_cleanup(callback: Callable[[], None]) -> Optional[str]:
    context = current_tool_cancellation()
    if context is None:
        return None
    return context.register_cleanup(callback)


def unregister_cancellation_cleanup(token: Optional[str]) -> None:
    context = current_tool_cancellation()
    if context is not None:
        context.unregister_cleanup(token)


@contextmanager
def cancellation_cleanup_scope():
    """Temporarily suppress cooperative cancellation for bounded cleanup only."""
    token = _CURRENT_TOOL_CANCELLATION.set(None)
    try:
        yield
    finally:
        _CURRENT_TOOL_CANCELLATION.reset(token)
