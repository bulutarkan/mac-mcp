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
from typing import Mapping, Optional, Sequence, TextIO, Tuple

# Tones of the app and the tour; they read on light and dark terminal backgrounds.
PALETTE = {"brand": (0xFF, 0xB4, 0x5E), "ok": (0x5E, 0xD3, 0xA0), "warn": (0xF5, 0xA5, 0x24),
           "error": (0xFF, 0x6B, 0x6B), "info": (0x8B, 0x7C, 0xF6)}
_PALETTE_256 = {"brand": 215, "ok": 79, "warn": 214, "error": 203, "info": 141}
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


def color_depth(env: Mapping[str, str]) -> str:
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    if "256color" in env.get("TERM", ""):
        return "256"
    return "16"


class Style:
    """The theme for one output stream. Built only when the theme is on (see for_stream)."""

    def __init__(self, stream: TextIO, depth: str, width: int) -> None:
        self.stream = stream
        self.depth = depth
        self.width = max(30, min(MAX_WIDTH, width))

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
        return cls(stream, color_depth(env), width)

    # ── Colour ────────────────────────────────────────────────────────────
    def paint(self, text: str, tone: Optional[str] = None, *, bold: bool = False, dim: bool = False) -> str:
        codes = []
        if bold:
            codes.append("1")
        if dim:
            codes.append("2")
        if tone:
            if self.depth == "truecolor":
                r, g, b = PALETTE[tone]
                codes.append(f"38;2;{r};{g};{b}")
            elif self.depth == "256":
                codes.append(f"38;5;{_PALETTE_256[tone]}")
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

    def summary(self, tone: str, text: str, next_command: Optional[str] = None) -> None:
        """The closing sentence; long ones wrap under their own first word."""
        plain = text + (f" — try: {next_command}" if next_command else "")
        lines = textwrap.wrap(plain, self.width - len(INDENT) - 2, break_on_hyphens=False) or [""]
        self.write()
        for index, line in enumerate(lines):
            painted = self.paint(line, bold=True)
            if next_command and next_command in line:
                head, _, rest = line.partition(next_command)
                painted = self.paint(head, bold=True) + self.paint(next_command, "brand") + self.paint(rest, bold=True)
            lead = f"{self.paint(SYMBOLS[tone], tone, bold=True)} " if index == 0 else "  "
            self.write(f"{INDENT}{lead}{painted}")

    def steps(self) -> "Steps":
        from .cli_steps import Steps
        return Steps(self)


class ThemedArgumentParser(argparse.ArgumentParser):
    """argparse with a themed --help in a terminal; plain argparse help otherwise. Lives here,
    not in cli_views, so building the parser (as `mac-mcp stdio` does) loads no screens."""

    def print_help(self, file=None) -> None:
        style = Style.for_stream(file if file is not None else sys.stdout)
        if style is None:
            return super().print_help(file)
        from .cli_views import render_command_help
        render_command_help(style, self)
