"""Live steps for long CLI operations: a spinner while a step runs, then ✓ and its duration,
or ✗ with the reason. The spinner draws on its own thread; a step's own prints are kept and
shown only when the step fails."""
from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
from typing import Iterator, Optional

from .cli_style import INDENT, SPINNER, SYMBOLS, Style

_HIDE, _SHOW, _CLEAR = "\x1b[?25l", "\x1b[?25h", "\r\x1b[2K"
_DURATION_COLUMN = 40


class StepState:
    def __init__(self) -> None:
        self.ok = True
        self.captured = ""


class Steps:
    def __init__(self, style: Style) -> None:
        self.style = style
        self._label: Optional[str] = None
        self._started = 0.0
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    # ── Spinner ───────────────────────────────────────────────────────────
    def _draw(self, frame: str) -> None:
        stream = self.style.stream
        stream.write(f"{_CLEAR}{INDENT}{self.style.paint(frame, 'brand')} {self._label}…")
        stream.flush()

    def _spin(self, stop: threading.Event) -> None:
        index = 0
        while not stop.wait(0.08):
            index = (index + 1) % len(SPINNER)
            self._draw(SPINNER[index])

    def begin(self, label: str) -> None:
        self._label = label
        self._started = time.monotonic()
        self.style.stream.write(_HIDE)
        self._draw(SPINNER[0])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, args=(self._stop,), daemon=True)
        self._thread.start()

    def end(self, ok: bool, reason: str = "", failed_label: Optional[str] = None) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._stop = self._thread = None
        label = (self._label or "") if ok or not failed_label else failed_label
        stream = self.style.stream
        if ok:
            took = f"{time.monotonic() - self._started:.1f}s"
            pad = " " * max(2, _DURATION_COLUMN - len(INDENT) - 2 - len(label))
            stream.write(f"{_CLEAR}{INDENT}{self.style.paint(SYMBOLS['ok'], 'ok')} {label}{pad}{self.style.paint(took, dim=True)}\n")
        else:
            stream.write(f"{_CLEAR}{INDENT}{self.style.paint(SYMBOLS['error'], 'error')} {label}\n")
            for line in reason.strip().splitlines():
                if line.strip():
                    stream.write(f"{INDENT}    {self.style.paint(line.strip(), dim=True)}\n")
        stream.write(_SHOW)
        stream.flush()
        self._label = None

    @contextlib.contextmanager
    def step(self, label: str, failed_label: Optional[str] = None) -> Iterator[StepState]:
        state = StepState()
        buffer = io.StringIO()
        self.begin(label)
        try:
            with contextlib.redirect_stdout(buffer):
                yield state
        except BaseException:
            state.captured = buffer.getvalue()
            self.end(False, state.captured, failed_label)
            raise
        state.captured = buffer.getvalue()
        self.end(state.ok, "" if state.ok else state.captured, failed_label)
