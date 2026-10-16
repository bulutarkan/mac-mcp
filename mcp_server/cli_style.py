"""How the CLI looks in a terminal: one header, aligned rows, live steps, one summary line.

The theme is on only when the output is a terminal. Otherwise (a pipe, the menu app, the
installer, NO_COLOR, TERM=dumb, MAC_MCP_PLAIN=1, --json) a command prints exactly what it
printed before this module existed; callers keep their plain code for that case.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO, Tuple

# Tones of the app and the tour, for dark backgrounds (5:1 or more on black and #1e1e1e).
PALETTE = {"brand": (0xFF, 0xB4, 0x5E), "ok": (0x5E, 0xD3, 0xA0), "warn": (0xF5, 0xA5, 0x24),
           "error": (0xFF, 0x6B, 0x6B), "info": (0x8B, 0x7C, 0xF6)}
# The same tones, deeper, for light backgrounds (5:1 or more on white, 3.7:1 on beige).
PALETTE_LIGHT = {"brand": (0xA8, 0x4D, 0x0B), "ok": (0x16, 0x79, 0x4A), "warn": (0x9A, 0x62, 0x00),
                 "error": (0xC2, 0x2D, 0x2D), "info": (0x5B, 0x45, 0xD6)}
_PALETTE_256 = {"brand": 215, "ok": 79, "warn": 214, "error": 203, "info": 141}
_PALETTE_256_LIGHT = {"brand": 130, "ok": 29, "warn": 136, "error": 160, "info": 61}
_PALETTE_16 = {"brand": 33, "ok": 32, "warn": 33, "error": 31, "info": 35}
SYMBOLS = {"brand": "◆", "dot": "●", "ok": "✓", "warn": "⚠", "error": "✗", "info": "ℹ"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MAX_WIDTH = 100
INDENT = "  "

Row = Tuple[Optional[str], str, str, str]  # tone (None: no dot), label, value, detail

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ON = {"1", "true", "yes", "on"}


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def home_path(path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def theme_enabled(stream, env: Optional[Mapping[str, str]] = None, json_output: bool = False) -> bool:
    env = os.environ if env is None else env
    if json_output or "NO_COLOR" in env or env.get("TERM", "") == "dumb":
        return False
    if env.get("MAC_MCP_PLAIN", "").strip().lower() in _ON:
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


_OSC11 = re.compile(rb"\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")
_background_cache: dict = {}


def background_is_light(reply: bytes) -> Optional[bool]:
    """The terminal's answer to OSC 11 (its background colour): light, dark, or unknown."""
    match = _OSC11.search(reply or b"")
    if not match:
        return None
    channels = [int(part, 16) / (16 ** len(part) - 1) for part in match.groups()]
    luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    return luminance > 0.5


def _ask_background(stream) -> Optional[bool]:
    """Ask the terminal behind `stream` for its background colour, waiting at most 0.15 s in
    all. Only a real terminal whose foreground job is this one is asked: a background job
    would be stopped by the terminal (SIGTTOU), and anything else (a test stream, a pipe)
    gets None. Keys typed meanwhile stay in the input (TCSANOW, never a flush)."""
    try:
        if not os.isatty(stream.fileno()):
            return None
    except (AttributeError, OSError, ValueError):
        return None
    if "answer" in _background_cache:
        return _background_cache["answer"]
    try:
        import select
        import termios
        import time
        import tty
    except ImportError:
        return None
    answer = None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            if os.tcgetpgrp(fd) == os.getpgrp():
                saved = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd, termios.TCSANOW)
                    os.write(fd, b"\x1b]11;?\x07")
                    reply = b""
                    deadline = time.monotonic() + 0.15
                    while not (reply.endswith(b"\x07") or reply.endswith(b"\x1b\\")):
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        ready, _, _ = select.select([fd], [], [], left)
                        if not ready:
                            break
                        reply += os.read(fd, 64)
                    answer = background_is_light(reply)
                finally:
                    termios.tcsetattr(fd, termios.TCSANOW, saved)
        finally:
            os.close(fd)
    except (OSError, ValueError, termios.error):  # termios.error is not an OSError
        answer = None
    _background_cache["answer"] = answer
    return answer


def color_depth(env: Mapping[str, str]) -> str:
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    if "256color" in env.get("TERM", ""):
        return "256"
    return "16"


class Style:
    """The theme for one output stream. Built only when the theme is on (see for_stream)."""

    def __init__(self, stream: TextIO, depth: str, width: int, light: bool = False) -> None:
        self.stream = stream
        self.depth = depth
        self.width = max(30, min(MAX_WIDTH, width))
        self.light = light

    @classmethod
    def for_stream(cls, stream: Optional[TextIO] = None, *, env: Optional[Mapping[str, str]] = None,
                   json_output: bool = False, width: Optional[int] = None) -> Optional["Style"]:
        stream = sys.stdout if stream is None else stream
        env = os.environ if env is None else env
        if not theme_enabled(stream, env, json_output):
            return None
        if width is None:
            try:
                width = int(env.get("COLUMNS", "")) or 0
            except ValueError:
                width = 0
            width = width or shutil.get_terminal_size((80, 24)).columns
        theme = env.get("MAC_MCP_THEME", "").strip().lower()
        light = theme == "light" if theme in {"light", "dark"} else bool(_ask_background(stream))
        return cls(stream, color_depth(env), width, light)

    # ── Colour ────────────────────────────────────────────────────────────
    def paint(self, text: str, tone: Optional[str] = None, *, bold: bool = False, dim: bool = False) -> str:
        codes = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if tone:
            if self.depth == "truecolor":
                r, g, b = (PALETTE_LIGHT if self.light else PALETTE)[tone]
                codes.append(f"38;2;{r};{g};{b}")
            elif self.depth == "256":
                codes.append(f"38;5;{(_PALETTE_256_LIGHT if self.light else _PALETTE_256)[tone]}")
            else:
                codes.append(str(_PALETTE_16[tone]))
        if not codes or not text:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"

    # ── Lines ─────────────────────────────────────────────────────────────
    def write(self, line: str = "") -> None:
        self.stream.write(line + "\n")
        self.stream.flush()

    def header(self, command: str, extra: str = "") -> None:
        title = self.paint(f"{SYMBOLS['brand']} Mac MCP", "brand", bold=True)
        tail = f" {self.paint('·', dim=True)} {command}" if command else ""
        self.write(f"{INDENT}{title}{tail}{('  ' + extra) if extra else ''}")
        self.write()

    def rows(self, rows: Sequence[Row]) -> None:
        """Dot, label, value, dim detail; values start in one column for the whole block."""
        if not rows:
            return
        label_width = max(len(label) for _, label, _, _ in rows)
        lead = len(INDENT) + 2  # the dot and its space
        column = lead + label_width + 2
        room = max(10, self.width - column)
        for tone, label, value, detail in rows:
            dot = self.paint(SYMBOLS["dot"], tone) if tone else " "
            plain = value + (f" · {detail}" if detail else "")
            wrapped = textwrap.wrap(plain, room, break_long_words=True, break_on_hyphens=False) or [""]
            first = wrapped[0]
            painted = self._value(first, value, detail)
            self.write(f"{INDENT}{dot} {self.paint(label.ljust(label_width), bold=True)}  {painted}")
            for rest in wrapped[1:]:
                self.write(" " * column + self.paint(rest, dim=True))

    def _value(self, line: str, value: str, detail: str) -> str:
        if detail and line.startswith(value + " · "):
            return value + self.paint(line[len(value):], dim=True)
        return line

    def note(self, text: str, tone: Optional[str] = None) -> None:
        symbol = SYMBOLS.get(tone or "", "")
        prefix = f"{self.paint(symbol, tone)} " if symbol else ""
        self.write(f"{INDENT}{prefix}{text}")

    def summary(self, tone: str, text: str, next_command: Optional[str] = None, note: str = "") -> None:
        """The closing sentence; long ones wrap under their own first word. `note` is a dim
        aside after it, kept whole."""
        # The command never breaks across lines: its spaces are held by a non-space while wrapping.
        glue = "\u2063"
        plain = text + (f" — try:{glue}{next_command.replace(' ', glue)}" if next_command else "")
        room = self.width - len(INDENT) - 2
        lines = [line.replace(glue, " ") for line in textwrap.wrap(plain, room, break_on_hyphens=False)] or [""]
        # The note joins the last line when it fits, else takes a line of its own.
        tail = ""
        if note and len(lines[-1]) + 3 + len(note) <= room:
            tail = "   " + self.paint(note, dim=True)
        self.write()
        for index, line in enumerate(lines):
            painted = self.paint(line, bold=True)
            if next_command and next_command in line:
                head, _, rest = line.partition(next_command)
                painted = self.paint(head, bold=True) + self.paint(next_command, "brand") + self.paint(rest, bold=True)
            lead = f"{self.paint(SYMBOLS[tone], tone, bold=True)} " if index == 0 else "  "
            self.write(f"{INDENT}{lead}{painted}{tail if index == len(lines) - 1 else ''}")
        if note and not tail:
            self.write(f"{INDENT}  {self.paint(note, dim=True)}")



class ThemedArgumentParser(argparse.ArgumentParser):
    """argparse with a themed --help in a terminal; plain argparse help otherwise. Lives here,
    not in cli_views, so building the parser (as `mac-mcp stdio` does) loads no screens.

    The CLI sets `themed_help(style, parser)`; this module imports nothing above it."""

    themed_help: Optional[Callable[[Style, argparse.ArgumentParser], None]] = None

    def print_help(self, file=None) -> None:
        style = Style.for_stream(file if file is not None else sys.stdout)
        if style is None or ThemedArgumentParser.themed_help is None:
            return super().print_help(file)
        ThemedArgumentParser.themed_help(style, self)
