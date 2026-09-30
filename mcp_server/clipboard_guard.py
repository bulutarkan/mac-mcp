from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from .tool_cancellation import cancellation_checkpoint


DEFAULT_CLIPBOARD_LOCK_TIMEOUT_S = 2.0
_CLIPBOARD_LOCK = threading.Lock()


class ClipboardBusyError(RuntimeError):
    pass


@contextmanager
def clipboard_guard(
    *,
    deadline: Optional[float] = None,
    timeout_s: float = DEFAULT_CLIPBOARD_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Serialize Mac MCP clipboard access with bounded, cancellation-aware waiting."""
    now = time.monotonic()
    local_deadline = now + max(0.05, float(timeout_s))
    if deadline is not None:
        local_deadline = min(local_deadline, float(deadline))

    acquired = False
    while not acquired:
        cancellation_checkpoint()
        remaining = local_deadline - time.monotonic()
        if remaining <= 0:
            raise ClipboardBusyError("system clipboard is busy")
        acquired = _CLIPBOARD_LOCK.acquire(timeout=min(0.05, remaining))

    try:
        cancellation_checkpoint()
        yield
    finally:
        _CLIPBOARD_LOCK.release()
