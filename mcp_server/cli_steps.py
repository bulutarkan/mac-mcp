"""Live steps for long CLI operations: a spinner while a step runs, then ✓ and its duration,
or ✗ with the reason. The spinner draws on its own thread; a step's own prints are kept and
shown when the step fails (and its warnings when it succeeds)."""
from __future__ import annotations

import contextlib
import io
import textwrap
import threading
import time
from typing import Iterator, Optional

from .cli_style import INDENT, SPINNER, SYMBOLS, Style

_HIDE, _SHOW, _CLEAR = "\x1b[?25l", "\x1b[?25h", "\r\x1b[2K"
_DURATION_COLUMN = 40
_REASON_INDENT = INDENT + "    "


class StepState:
    def __init__(self, buffer: Optional[io.StringIO] = None) -> None:
        self.ok = True
        self.captured = ""
        # Set by the block to say what happened when it differs from the label
        # ("The server was already running" for "Started the server").
        self.label: Optional[str] = None
        self._buffer = buffer

    @property
    def printed(self) -> str:
        """What the block has printed so far."""
        return self._buffer.getvalue() if self._buffer is not None else self.captured


class Steps:
    def __init__(self, style: Style) -> None:
        self.style = style
        self._label: Optional[str] = None
        self._running: Optional[str] = None
        self._started = 0.0
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        return self._label is not None

    @property
    def running_label(self) -> Optional[str]:
        """What the current step is doing ("Stop the server"), for a line that ends it early."""
        return self._running

    # ── Spinner ───────────────────────────────────────────────────────────
    def _draw(self, frame: str) -> None:
        stream = self.style.stream
        stream.write(f"{_CLEAR}{INDENT}{self.style.paint(frame, 'brand')} {self._running}…")
        stream.flush()

    def _spin(self, stop: threading.Event) -> None:
        index = 0
        while not stop.wait(0.08):
            index = (index + 1) % len(SPINNER)
            self._draw(SPINNER[index])

    def begin(self, label: str, running: Optional[str] = None) -> None:
        """Start a step. `running` is what the spinner says meanwhile ("Start the server");
        `label` is the line it becomes when done ("Started the server")."""
        self._label = label
        self._running = running or label
        self._started = time.monotonic()
        self.style.stream.write(_HIDE)
        self._draw(SPINNER[0])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, args=(self._stop,), daemon=True)
        self._thread.start()

    def _wrapped(self, text: str, tone: Optional[str] = None) -> None:
        room = max(20, self.style.width - len(_REASON_INDENT) - (2 if tone else 0))
        for line in text.strip("\n").splitlines():
            if not line.strip():
                continue
            # A line keeps its own indentation (a traceback in a log), also where it wraps.
            own = line[: len(line) - len(line.lstrip())]
            parts = textwrap.wrap(line.strip(), max(10, room - len(own)), break_on_hyphens=False) or [""]
            for index, part in enumerate(parts):
                lead = (f"{self.style.paint(SYMBOLS[tone], tone)} " if index == 0 else "  ") if tone else ""
                self.style.stream.write(f"{_REASON_INDENT}{lead}{own}{self.style.paint(part, dim=True)}\n")

    def end(self, ok: bool, reason: str = "", failed_label: Optional[str] = None,
            done_label: Optional[str] = None, warnings: str = "") -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._stop = self._thread = None
        label = (done_label or self._label or "") if ok else (failed_label or self._label or "")
        stream = self.style.stream
        if ok:
            took = f"{time.monotonic() - self._started:.1f}s"
            # The time sits in one column, pulled in on a narrow terminal.
            column = min(_DURATION_COLUMN, self.style.width - len(took))
            pad = " " * max(2, column - len(INDENT) - 2 - len(label))
            stream.write(f"{_CLEAR}{INDENT}{self.style.paint(SYMBOLS['ok'], 'ok')} {label}{pad}{self.style.paint(took, dim=True)}\n")
            if warnings:
                self._wrapped(warnings, "warn")
        else:
            stream.write(f"{_CLEAR}{INDENT}{self.style.paint(SYMBOLS['error'], 'error')} {label}\n")
            self._wrapped(reason)
        stream.write(_SHOW)
        stream.flush()
        self._label = self._running = None

    @contextlib.contextmanager
    def step(self, label: str, failed_label: Optional[str] = None) -> Iterator[StepState]:
        buffer = io.StringIO()
        state = StepState(buffer)
        self.begin(label, running=failed_label)
        try:
            with contextlib.redirect_stdout(buffer):
                yield state
        except BaseException:
            state.captured = buffer.getvalue()
            self.end(False, state.captured, failed_label)
            raise
        state.captured = buffer.getvalue()
        warnings = "\n".join(line for line in state.captured.splitlines() if line.lstrip().lower().startswith("warning"))
        self.end(state.ok, "" if state.ok else state.captured, failed_label, done_label=state.label, warnings=warnings)
