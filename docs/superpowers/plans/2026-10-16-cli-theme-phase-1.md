# CLI Theme, Phase 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a terminal, `mac-mcp` (no command), `--help`, `status`, `start`, `stop`, `restart` and `doctor` share one calm, modern look (header, aligned rows, live steps, one summary line). Without a terminal, they print exactly what they print today.

**Architecture:**
- `mcp_server/cli_style.py` (new, standard library only) decides whether the theme is on and holds the primitives: colours, symbols, the header, aligned rows, the summary line and the live `Steps`.
- `mcp_server/cli_views.py` (new) turns the data the commands already compute (the `status --json` report, the `doctor` report, the parser) into themed screens.
- `mcp_server/cli.py` gets one branch per command: `style = Style.for_stream(...)`; themed when `style` is not `None`, today's code otherwise.

**Tech Stack:** Python 3.10+ standard library (`argparse`, `threading`, `shutil`, `contextlib`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-15-cli-theme-design.md`

## Global Constraints

- The theme is on only when stdout is a TTY, and off when `NO_COLOR` is set (any value), `TERM=dumb`, `MAC_MCP_PLAIN` is `1|true|yes|on`, or the command has `--json`.
- With the theme off, output is byte-for-byte today's: the menu app parses `doctor` (`DoctorReport.parse`), start/restart output and `update`; the installer runs `start` and `--version`.
- `mac-mcp stdio` loads only the small `cli_style` (through the parser), never `cli_views` or `cli_steps`, and never draws anything.
- No new dependency.
- Exit codes are unchanged. The only behaviour change: in a terminal, `restart` waits for the outcome (as `--wait`), and bare `mac-mcp` shows the home screen with exit 0 instead of argparse's "required: command" error (exit 2).
- Colours: brand `#ffb45e`, ok `#5ed3a0`, warn `#f5a524`, error `#ff6b6b`, info `#8b7cf6`, secondary = dim attribute. Depth: truecolor if `COLORTERM` is `truecolor`/`24bit`, else 256 if `TERM` contains `256color`, else 16.
- Symbols: `◆` header, `●` dot, `✓` `⚠` `✗` `ℹ`, spinner `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, `·` separator.
- Layout: header `  ◆ Mac MCP · <command>` then an empty line; body indented two spaces; one summary line at the end; width = terminal columns, at most 100.
- Text is English and human ("Running · pid 28323"), never log language.
- Tests: `MCP_API_KEY=test-key-0123456789abcdef0123456789 MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true MAC_MCP_SKIP_MENU_APP_INSTALL=1 ~/mac-mcp/.venv/bin/python -m unittest <module> -v`, run from `/Users/tarkanbulut/Projects/mac-mcp`.

## Review Focus

1. **A narrow terminal (40 columns):** rows wrap the value under its own column, and nothing overflows into a broken layout. Test in Task 2.
2. **Ctrl+C during `start`/`restart`:** the spinner line is replaced by `✗ <step>`, the cursor comes back and the interrupt propagates. Test in Task 3.
3. **A failing step's own messages:** the reason `_start_server` printed ("port 8765 is used by …") must be shown under the `✗`, not swallowed. Test in Task 6.
4. **`status` while the tunnel is down:** the themed screen must end with `⚠` and the remediation, and still exit 2. Test in Task 5.
5. **A doctor check with an unknown category (a new check added later):** it must land in an "Other" group, not vanish. Test in Task 8.

---

### Task 1: Pin today's plain output

Before any change, record what each Phase 1 command prints without a terminal, so the later tasks cannot drift.

**Files:**
- Create: `tests/test_cli_plain_contract.py`
- Create: `tests/golden/cli_help.txt` (generated in Step 2)

**Interfaces:**
- Consumes: today's `mcp_server.cli` (`main`, `status`, `start`, `stop`, `restart`, `doctor`).
- Produces: `tests/golden/cli_help.txt`; the contract tests that every later task must keep green.

- [ ] **Step 1: Write the contract tests**

```python
"""Without a terminal the CLI prints exactly what it printed before the theme (#CLI theme phase 1).

The menu app, the installer and scripts read this text, so these tests pin it.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli
from mcp_server.managed_process import ProcessValidation

GOLDEN = Path(__file__).resolve().parent / "golden"
PLAIN_ENV = {"COLUMNS": "100", "TERM": "xterm-256color", "COLORTERM": "truecolor"}


def _public(mode, url=None):
    return type("Public", (), {"mode": mode, "endpoint_url": url})()


class PlainContract(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, PLAIN_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "MAC_MCP_PLAIN"):
            os.environ.pop(name, None)

    def run_status(self, *, server=(4321, "pid_record"), health="ok", public=None, supervisor=None):
        missing = ProcessValidation("missing", None, "server", None, "record_missing")
        out = StringIO()
        with patch.object(cli, "_resolve_server_identity", return_value=server), \
             patch.object(cli, "_validate_managed_pid", return_value=missing), \
             patch.object(cli, "_launchctl_pid", return_value=None), \
             patch.object(cli, "_server_health", return_value=health), \
             patch.object(cli, "CLOUDFLARE_PID_FILE", Path("/nonexistent/mac-mcp/cloudflared.pid")), \
             patch.object(cli, "resolve_public_endpoint", return_value=public or _public("none")), \
             patch.object(cli, "supervisor_report", return_value=supervisor or {"enabled": True, "loaded": True, "intent": "running"}), \
             patch.object(cli, "LOG_FILE", Path("/state/mac-mcp.log")), \
             redirect_stdout(out):
            code = cli.status(argparse.Namespace(json=False, local_only=False))
        return code, out.getvalue()

    def test_status_healthy_local_only(self) -> None:
        code, text = self.run_status()
        self.assertEqual(0, code)
        self.assertEqual(
            "mac-mcp is running (pid 4321; identity verified via pid_record).\n"
            "mac-mcp log: /state/mac-mcp.log\n"
            "public endpoint mode: local only\n"
            "crash supervisor: watching (intent running)\n",
            text,
        )

    def test_status_stopped(self) -> None:
        code, text = self.run_status(server=(None, "not_running"))
        self.assertEqual(1, code)
        self.assertEqual(
            "mac-mcp is not running.\n"
            "public endpoint mode: local only\n"
            "crash supervisor: watching (intent running)\n",
            text,
        )

    def test_status_tunnel_missing(self) -> None:
        code, text = self.run_status(public=_public("cloudflare", "https://mac.example.com/mcp"))
        self.assertEqual(2, code)
        self.assertIn("Cloudflare Tunnel is configured but no verified mac-mcp cloudflared process is running", text)
        self.assertIn("overall: degraded (local server running; selected public endpoint unavailable)\n", text)

    def test_doctor_prints_format_report(self) -> None:
        from mcp_server import diagnostics
        report = {"ok": True, "health": "healthy", "counts": {"pass": 1, "warn": 0, "fail": 0, "info": 0},
                  "checks": [{"check_id": "runtime.python", "category": "runtime", "status": "pass",
                              "summary": "Python 3.14.7 on arm64"}]}
        out = StringIO()
        with patch.object(diagnostics, "run_doctor", return_value=report), redirect_stdout(out):
            code = cli.doctor(argparse.Namespace(json=False, support_bundle=None, local_only=False, verbose=False))
        self.assertEqual(0, code)
        self.assertEqual(diagnostics.format_report(report) + "\n", out.getvalue())
        self.assertTrue(out.getvalue().startswith("Mac MCP Doctor\n==============\n[PASS] runtime.python: "))

    def test_start_prints_its_steps_messages(self) -> None:
        out = StringIO()

        def fake_server(args):
            print("mac-mcp started (pid 4321).")
            return 0

        with patch.object(cli, "_public_endpoint_config", return_value=_public("none")), \
             patch.object(cli, "validate_bootstrap_security"), \
             patch.object(cli, "load_settings"), \
             patch.object(cli, "_record_intent"), \
             patch.object(cli, "_start_server", side_effect=fake_server), \
             patch.object(cli, "_stop_unselected_public_processes"), \
             redirect_stdout(out):
            code = cli.start(argparse.Namespace(host="127.0.0.1", port=8765))
        self.assertEqual(0, code)
        self.assertEqual("mac-mcp started (pid 4321).\npublic endpoint mode: local only\n", out.getvalue())

    def test_stop_prints_not_running(self) -> None:
        out = StringIO()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(None, "not_running")), \
             patch.object(cli, "PID_FILE", Path("/nonexistent/mac-mcp/server.pid")), \
             patch.object(cli, "_stop_pid", return_value=True), \
             patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        self.assertEqual(0, code)
        self.assertEqual("mac-mcp is not running.\n", out.getvalue())

    def test_restart_hands_off_and_returns(self) -> None:
        out = StringIO()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", side_effect=lambda args: print("handed off") or 0) as spawn, \
             redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False))
        self.assertEqual((0, "handed off\n"), (code, out.getvalue()))
        spawn.assert_called_once()

    def test_bare_command_is_an_argparse_error(self) -> None:
        err = StringIO()
        with patch.object(sys, "argv", ["mac-mcp"]), redirect_stderr(err), redirect_stdout(StringIO()), \
                self.assertRaises(SystemExit) as stop:
            cli.main([])
        self.assertEqual(2, stop.exception.code)
        self.assertIn("the following arguments are required: command", err.getvalue())

    def test_help_text_is_unchanged(self) -> None:
        out = StringIO()
        # argparse names the program after argv[0]; the installed CLI's is mac-mcp.
        with patch.object(sys, "argv", ["mac-mcp", "--help"]), redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertEqual((GOLDEN / "cli_help.txt").read_text(encoding="utf-8"), out.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Generate the help golden from today's code**

Run:
```bash
mkdir -p tests/golden && COLUMNS=100 ~/mac-mcp/.venv/bin/python -c "
import io, contextlib, sys
from mcp_server import cli
sys.argv = ['mac-mcp', '--help']
out = io.StringIO()
with contextlib.redirect_stdout(out):
    try: cli.main(['--help'])
    except SystemExit: pass
open('tests/golden/cli_help.txt', 'w').write(out.getvalue())" && head -3 tests/golden/cli_help.txt
```
Expected: the file starts with `usage: mac-mcp [-h] [--version]`.

- [ ] **Step 3: Run the contract tests on today's code**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_plain_contract -v`
Expected: all PASS. Each one pins today's output. If a test fails, correct its expected text to what today's code prints, because that text is the contract.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cli_plain_contract.py tests/golden/cli_help.txt
git commit -m "Tests: pin the CLI's plain output before the theme"
```

---

### Task 2: `cli_style`, the decision and the primitives

**Files:**
- Create: `mcp_server/cli_style.py`
- Test: `tests/test_cli_style.py`

**Interfaces:**
- Produces:
  - `theme_enabled(stream, env=None, json_output=False) -> bool`
  - `color_depth(env) -> str` ("truecolor" | "256" | "16")
  - `class Style`:
    - `for_stream(stream=None, *, env=None, json_output=False, width=None) -> Style | None`
    - `paint(text, tone=None, *, bold=False, dim=False) -> str`, where tone is "brand" | "ok" | "warn" | "error" | "info" | `None`
    - `write(line="")`
    - `header(command, extra="")`
    - `rows(rows: list[Row])`
    - `note(text, tone=None)`
    - `summary(tone, text, next_command=None)`
    - `steps() -> Steps` (Task 3)
  - `Row = tuple[str | None, str, str, str]` as (tone, label, value, detail); a `None` tone means no dot.
  - `strip_ansi(text) -> str`
  - `home_path(path) -> str`, which shows `~` for the home folder.
  - `class ThemedArgumentParser(argparse.ArgumentParser)`: `print_help(file=None)` is themed in a terminal (through `cli_views.render_command_help`, Task 4), else `super()`.

- [ ] **Step 1: Write the failing tests**

```python
"""The CLI theme's decision and building blocks."""
from __future__ import annotations

import io
import os
import unittest
from pathlib import Path

from mcp_server import cli_style
from mcp_server.cli_style import Style, color_depth, strip_ansi, theme_enabled


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}


def visible(text: str) -> str:
    """What a person sees: no colour codes, and each line as left by its last carriage return."""
    return "\n".join(line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n"))


class Decision(unittest.TestCase):
    def test_on_only_for_a_terminal_without_opt_outs(self) -> None:
        self.assertTrue(theme_enabled(FakeTTY(), ENV))
        self.assertFalse(theme_enabled(io.StringIO(), ENV))
        for extra in ({"NO_COLOR": ""}, {"NO_COLOR": "1"}, {"TERM": "dumb"}, {"MAC_MCP_PLAIN": "1"}, {"MAC_MCP_PLAIN": "yes"}):
            with self.subTest(extra=extra):
                self.assertFalse(theme_enabled(FakeTTY(), {**ENV, **extra}))
        self.assertTrue(theme_enabled(FakeTTY(), {**ENV, "MAC_MCP_PLAIN": "0"}))
        self.assertFalse(theme_enabled(FakeTTY(), ENV, json_output=True))

    def test_a_stream_without_isatty_or_closed_is_plain(self) -> None:
        class Closed(io.StringIO):
            def isatty(self):
                raise ValueError("closed")
        self.assertFalse(theme_enabled(object(), ENV))
        self.assertFalse(theme_enabled(Closed(), ENV))

    def test_colour_depth(self) -> None:
        self.assertEqual("truecolor", color_depth({"COLORTERM": "24bit"}))
        self.assertEqual("256", color_depth({"TERM": "xterm-256color"}))
        self.assertEqual("16", color_depth({"TERM": "xterm"}))

    def test_for_stream_returns_none_when_off(self) -> None:
        self.assertIsNone(Style.for_stream(io.StringIO(), env=ENV))
        self.assertIsInstance(Style.for_stream(FakeTTY(), env=ENV), Style)


class Primitives(unittest.TestCase):
    def style(self, width=80, depth_env=ENV) -> tuple[Style, FakeTTY]:
        stream = FakeTTY()
        return Style.for_stream(stream, env=depth_env, width=width), stream

    def test_paint_uses_the_palette_per_depth(self) -> None:
        style, _ = self.style()
        self.assertEqual("\x1b[38;2;94;211;160mok\x1b[0m", style.paint("ok", "ok"))
        style256, _ = self.style(depth_env={"TERM": "xterm-256color"})
        self.assertEqual("\x1b[38;5;79mok\x1b[0m", style256.paint("ok", "ok"))
        style16, _ = self.style(depth_env={"TERM": "xterm"})
        self.assertEqual("\x1b[32mok\x1b[0m", style16.paint("ok", "ok"))
        self.assertEqual("\x1b[2mpid\x1b[0m", style.paint("pid", dim=True))
        self.assertEqual("plain", style.paint("plain"))

    def test_header_rows_and_summary(self) -> None:
        style, stream = self.style()
        style.header("status")
        style.rows([("ok", "Running", "pid 4321", "port 8765"), ("warn", "Public address", "mac.example.com", "tunnel down"),
                    (None, "Logs", "~/.mac-mcp/mac-mcp.log", "")])
        style.summary("ok", "All good — AI apps can reach this Mac.")
        self.assertEqual(
            "  ◆ Mac MCP · status\n"
            "\n"
            "  ● Running         pid 4321 · port 8765\n"
            "  ● Public address  mac.example.com · tunnel down\n"
            "    Logs            ~/.mac-mcp/mac-mcp.log\n"
            "\n"
            "  ✓ All good — AI apps can reach this Mac.\n",
            visible(stream.getvalue()),
        )

    def test_summary_names_the_next_command(self) -> None:
        style, stream = self.style()
        style.summary("error", "Mac MCP is not running", next_command="mac-mcp start")
        self.assertEqual("\n  ✗ Mac MCP is not running — try: mac-mcp start\n", visible(stream.getvalue()))

    def test_a_narrow_terminal_wraps_values_under_their_column(self) -> None:
        style, stream = self.style(width=40)
        style.rows([("ok", "Public address", "a-very-long-host-name.example.com", "Cloudflare tunnel")])
        lines = visible(stream.getvalue()).rstrip("\n").split("\n")
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[1].startswith(" " * 18), lines)

    def test_home_path(self) -> None:
        self.assertEqual("~/.mac-mcp/mac-mcp.log", cli_style.home_path(Path.home() / ".mac-mcp" / "mac-mcp.log"))
        self.assertEqual("/etc/hosts", cli_style.home_path("/etc/hosts"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_style -v`
Expected: `ModuleNotFoundError: No module named 'mcp_server.cli_style'`.

- [ ] **Step 3: Write `mcp_server/cli_style.py`**

```python
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
        tail = f" — try: {self.paint(next_command, 'brand')}" if next_command else ""
        self.write()
        self.write(f"{INDENT}{self.paint(SYMBOLS[tone], tone, bold=True)} {self.paint(text, bold=True)}{tail}")

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
```

Note: `Steps` lives in `mcp_server/cli_steps.py` (Task 3) so this file stays about looks.

- [ ] **Step 4: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_style -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/cli_style.py tests/test_cli_style.py
git commit -m "CLI: a style module for the terminal theme"
```

---

### Task 3: Live steps

**Files:**
- Create: `mcp_server/cli_steps.py`
- Test: `tests/test_cli_steps.py`

**Interfaces:**
- Consumes: `Style` (Task 2): `paint`, `stream`, `SYMBOLS`, `SPINNER`, `INDENT`.
- Produces:
  - `class Steps`:
    - `begin(label: str) -> None`
    - `end(ok: bool, reason: str = "", failed_label: str | None = None) -> None`
    - `step(label, failed_label=None) -> ContextManager[StepState]`
  - `failed_label` is the line a failed step reads as ("Start the server" for "Started the server").
  - `class StepState`: `.ok: bool` (default `True`), `.captured: str` (what the block printed).
  - Inside `step()`, `sys.stdout` is redirected to a buffer. A step that ends with `ok=False` shows the buffer's lines dimmed under the `✗`. An exception (KeyboardInterrupt included) ends the step as `✗`, shows the buffer and re-raises.

- [ ] **Step 1: Write the failing tests**

```python
"""Live steps: a spinner per step, fixed with ✓ and its duration, or ✗ and the reason."""
from __future__ import annotations

import io
import re
import sys
import unittest

from mcp_server.cli_style import Style, strip_ansi


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def visible(text: str) -> str:
    seen = "\n".join(line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n"))
    return re.sub(r"\d+\.\d+s", "0.0s", seen)


class StepsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = FakeTTY()
        self.style = Style.for_stream(self.stream, env={"TERM": "xterm-256color"}, width=60)

    def test_a_step_is_fixed_with_a_tick_and_its_duration(self) -> None:
        steps = self.style.steps()
        with steps.step("Starting the server") as state:
            print("mac-mcp started (pid 4321).")
        self.assertTrue(state.ok)
        self.assertEqual("mac-mcp started (pid 4321).\n", state.captured)
        self.assertEqual("  ✓ Starting the server" + " " * 17 + "0.0s\n", visible(self.stream.getvalue()))

    def test_a_failed_step_shows_what_it_printed(self) -> None:
        steps = self.style.steps()
        with steps.step("Starting the server") as state:
            print("configured port 8765 is used by Python (pid 99).")
            state.ok = False
        self.assertEqual(
            "  ✗ Starting the server\n"
            "      configured port 8765 is used by Python (pid 99).\n",
            visible(self.stream.getvalue()),
        )

    def test_ctrl_c_fixes_the_line_restores_the_cursor_and_propagates(self) -> None:
        steps = self.style.steps()
        with self.assertRaises(KeyboardInterrupt):
            with steps.step("Restarting"):
                raise KeyboardInterrupt
        text = self.stream.getvalue()
        self.assertIn("✗ Restarting", visible(text))
        self.assertIn("\x1b[?25h", text.rsplit("✗", 1)[1])  # the cursor is shown again

    def test_stdout_is_restored_after_a_step(self) -> None:
        before = sys.stdout
        with self.style.steps().step("x"):
            pass
        self.assertIs(before, sys.stdout)

    def test_a_failed_step_can_read_as_an_instruction(self) -> None:
        with self.style.steps().step("Started the server", failed_label="Start the server") as state:
            state.ok = False
        self.assertIn("✗ Start the server", visible(self.stream.getvalue()))

    def test_begin_end_for_steps_driven_from_outside(self) -> None:
        steps = self.style.steps()
        steps.begin("Stopping the server")
        steps.end(True)
        steps.begin("Starting the server")
        steps.end(False, "health check timed out")
        self.assertEqual(
            "  ✓ Stopping the server" + " " * 17 + "0.0s\n"
            "  ✗ Starting the server\n"
            "      health check timed out\n",
            visible(self.stream.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_steps -v`
Expected: `ModuleNotFoundError: No module named 'mcp_server.cli_steps'`.

- [ ] **Step 3: Write `mcp_server/cli_steps.py`**

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_steps tests.test_cli_style -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/cli_steps.py tests/test_cli_steps.py
git commit -m "CLI: live steps with a spinner for long operations"
```

---

### Task 4: Home screen and themed help

**Files:**
- Create: `mcp_server/cli_views.py` (home, help; later tasks add to it)
- Modify: `mcp_server/cli.py`, `main()` (around line 2824: parser creation and the final `parse_args`)
- Test: `tests/test_cli_views.py`

**Interfaces:**
- Consumes: `Style` (Task 2).
- Produces:
  - `GROUPS: list[tuple[str, list[str]]]`
  - `ADVANCED: list[str]`
  - `SHORT_HELP: dict[str, str]`
  - `EXAMPLES: dict[str, str]`
  - `render_home(style, version: str, running: Optional[bool]) -> None`, where `running=None` means "no status line" (that is `--help`).
  - `render_command_help(style, parser) -> None`, used by `cli_style.ThemedArgumentParser`.

- [ ] **Step 1: Write the failing tests**

```python
"""The CLI's themed screens."""
from __future__ import annotations

import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mcp_server import cli, cli_views
from mcp_server.cli_style import Style, strip_ansi
from mcp_server.managed_process import ProcessValidation


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


TTY_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "COLUMNS": "80"}


def visible(text: str) -> str:
    return "\n".join(line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n"))


class ThemedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, TTY_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("NO_COLOR", "MAC_MCP_PLAIN"):
            os.environ.pop(name, None)

    def run_main(self, argv):
        out = FakeTTY()
        code = None
        with redirect_stdout(out):
            try:
                code = cli.main(argv)
            except SystemExit as stop:
                code = stop.code
        return code, visible(out.getvalue())


class HomeAndHelp(ThemedTestCase):
    def test_groups_list_every_command_but_stdio_once(self) -> None:
        listed = [name for _, names in cli_views.GROUPS for name in names] + cli_views.ADVANCED
        parser_commands = set(cli.command_names())
        self.assertEqual(sorted(parser_commands - {"stdio"}), sorted(listed))
        self.assertEqual(len(listed), len(set(listed)))
        self.assertTrue(set(cli_views.SHORT_HELP) >= set(listed))

    def test_bare_command_shows_home_with_status(self) -> None:
        with patch.object(cli, "_home_running", return_value=True):
            code, text = self.run_main([])
        self.assertEqual(0, code)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("  ◆ Mac MCP · ") and lines[0].endswith("● Running"), lines[0])
        self.assertIn("  Everyday", text)
        self.assertIn("    status      How Mac MCP is doing", text)
        self.assertIn("  Advanced  ", text)
        self.assertIn("mac-mcp <command> --help for more.", text)
        self.assertNotIn("stdio", text)

    def test_help_has_no_status_line_and_no_server_call(self) -> None:
        with patch.object(cli, "_home_running", side_effect=AssertionError("no server call")):
            code, text = self.run_main(["--help"])
        self.assertEqual(0, code)
        self.assertNotIn("Running", text.split("\n")[0])
        self.assertIn("  Connect", text)

    def test_command_help_is_themed(self) -> None:
        code, text = self.run_main(["status", "--help"])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · status\n"), text[:80])
        self.assertIn("--local-only", text)
        self.assertIn("--json", text)
        self.assertIn("Example", text)
        self.assertIn("mac-mcp status --local-only", text)

    def test_nested_command_help_is_themed_too(self) -> None:
        code, text = self.run_main(["recipe", "--help"])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · recipe\n"), text[:80])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views -v`
Expected: FAIL: `ImportError: cannot import name 'cli_views'`.

- [ ] **Step 3: Write `mcp_server/cli_views.py` (home and help)**

```python
"""Themed screens of the CLI, built from the data the commands already compute.

Only used when the theme is on (see cli_style); every command keeps its plain output otherwise.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from .cli_style import INDENT, SYMBOLS, Style

GROUPS = [
    ("Everyday", ["status", "start", "stop", "restart", "update"]),
    ("Connect", ["clients", "connect", "connect-config"]),
    ("Troubleshoot", ["doctor", "logs", "repair"]),
]
ADVANCED = ["dashboard", "privacy", "recipe", "logins", "catalog", "credential", "autostart", "conformance", "uninstall"]

SHORT_HELP = {
    "status": "How Mac MCP is doing", "start": "Start the server", "stop": "Stop the server",
    "restart": "Restart the server", "update": "Update to the latest stable release",
    "clients": "AI apps on this Mac", "connect": "Add Mac MCP to an AI app",
    "connect-config": "A connection snippet for an app", "doctor": "Check everything",
    "logs": "Recent log lines", "repair": "Fix the parts Mac MCP manages",
    "dashboard": "Open the dashboard", "privacy": "Stored data", "recipe": "Saved recipes",
    "logins": "Saved logins", "catalog": "Skill packs", "credential": "Tunnel credentials",
    "autostart": "Start at login", "conformance": "Computer Use checks", "uninstall": "Remove Mac MCP",
}

EXAMPLES = {
    "status": "mac-mcp status --local-only", "start": "mac-mcp start", "stop": "mac-mcp stop",
    "restart": "mac-mcp restart", "update": "mac-mcp update --check", "clients": "mac-mcp clients",
    "connect": "mac-mcp connect --client claude-code", "connect-config": "mac-mcp connect-config --client codex",
    "doctor": "mac-mcp doctor --verbose", "logs": "mac-mcp logs server -n 40", "repair": "mac-mcp repair",
    "dashboard": "mac-mcp dashboard", "privacy": "mac-mcp privacy", "recipe": "mac-mcp recipe list",
    "logins": "mac-mcp logins list", "catalog": "mac-mcp catalog list", "credential": "mac-mcp credential cloudflare status",
    "autostart": "mac-mcp autostart on", "conformance": "mac-mcp conformance", "uninstall": "mac-mcp uninstall",
}


def render_home(style: Style, version: str, running: Optional[bool]) -> None:
    status = ""
    if running is not None:
        status = (style.paint(SYMBOLS["dot"], "ok") + " Running") if running else (style.paint(SYMBOLS["dot"], "error") + " Stopped")
    style.header(version, extra=status)
    for title, names in GROUPS:
        style.write(f"{INDENT}{style.paint(title, bold=True)}")
        for name in names:
            style.write(f"{INDENT}  {style.paint(name.ljust(10), 'brand')}  {SHORT_HELP[name]}")
        style.write()
    advanced = f" {style.paint('·', dim=True)} ".join(ADVANCED)
    style.write(f"{INDENT}{style.paint('Advanced', bold=True)}  {advanced}")
    style.write()
    style.write(f"{INDENT}{style.paint('mac-mcp <command> --help for more.', dim=True)}")


def render_command_help(style: Style, parser: argparse.ArgumentParser) -> None:
    """A command's --help in the theme: a description, its options, one example."""
    command = parser.prog.split(" ", 1)[1] if " " in parser.prog else ""
    top = command.split(" ")[0]
    style.header(command or "help")
    if parser.description:
        style.write(f"{INDENT}{parser.description}")
        style.write()
    options = [action for action in parser._actions if action.help != argparse.SUPPRESS]
    if options:
        style.write(f"{INDENT}{style.paint('Options', bold=True)}")
        for action in options:
            names = ", ".join(action.option_strings) or action.dest
            if action.choices and not action.option_strings:
                names = "{" + ",".join(map(str, action.choices)) + "}"
            style.write(f"{INDENT}  {style.paint(names, 'brand')}")
            if action.help:
                text = action.help % vars(action) if "%(" in action.help else action.help
                style.write(f"{INDENT}      {style.paint(text, dim=True)}")
    example = EXAMPLES.get(top)
    if example:
        style.write()
        style.write(f"{INDENT}{style.paint('Example', bold=True)}  {example}")
```

- [ ] **Step 4: Wire it into `main()` in `mcp_server/cli.py`**

At the top of `cli.py`, next to the other `from .` imports, add:
```python
from .cli_style import Style
```
(`cli_views` is imported lazily inside `main` and the commands, so `stdio` never loads it.)

Move the parser construction out of `main()` into `build_parser()`: everything from `parser = argparse.ArgumentParser(...)` to the last `set_defaults(...)` before `args = parser.parse_args(argv)`, with these changes at its start:

```python
def build_parser() -> argparse.ArgumentParser:
    # prog is fixed so the help reads the same whatever argv[0] is (the installed CLI's is mac-mcp).
    parser = ThemedArgumentParser(prog="mac-mcp", description="Manage the Mac MCP local server.")
    parser.add_argument("--version", action="version", version=f"mac-mcp {__version__}")
    # Subcommands (and theirs) are themed parsers too: argparse builds them with parser_class.
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ThemedArgumentParser)
    # … every add_parser / add_argument / set_defaults line from main(), unchanged …
    return parser
```

`ThemedArgumentParser` comes from `cli_style`; building the parser loads no screens.

Then `main()` becomes:
```python
def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = build_parser()
    words = list(sys.argv[1:] if argv is None else argv)
    style = Style.for_stream()
    if style is not None and (not words or words in (["-h"], ["--help"])):
        from .cli_views import render_home
        render_home(style, __version__, None if words else _home_running())
        return 0
    args = parser.parse_args(argv)
    return int(args.func(args))


def _home_running() -> bool:
    """For the home screen's one status line: a verified server owns the port."""
    return _resolve_server_identity(_default_port())[0] is not None


def command_names() -> list[str]:
    """Every top-level command, for the grouped help's test."""
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
    return []
```

At the top of `cli.py`, next to the other `from .` imports, add `from .cli_style import Style, ThemedArgumentParser`.

- [ ] **Step 5: Run the view tests and the plain contract**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views tests.test_cli_plain_contract -v`
Expected: all PASS. `test_help_text_is_unchanged` still matches the golden, because the plain `print_help` is argparse's own.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/cli_views.py mcp_server/cli.py tests/test_cli_views.py
git commit -m "CLI: a grouped home screen and themed help in a terminal"
```

---

### Task 5: Themed `status`

**Files:**
- Modify: `mcp_server/cli.py`, `status()` (lines ~1670–1907: build the report dict for both paths, then branch)
- Modify: `mcp_server/cli_views.py` (add `render_status`)
- Test: `tests/test_cli_views.py` (add `StatusScreen`)

**Interfaces:**
- Consumes: the `status --json` report dict, with the keys `ok`, `state`, `exit_code`, `server{running,pid,port,health,log,port_owners}`, `public_endpoint{mode,url,tunnel_running,route,error}`, `stray_processes`, `privacy`, `supervisor`, `remediation`.
- Produces: `render_status(style, report: dict) -> None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_views.py`)

```python
class StatusScreen(ThemedTestCase):
    def report(self, **changes):
        report = {
            "ok": True, "state": "healthy", "exit_code": 0,
            "server": {"running": True, "pid": 4321, "identity": "pid_record", "port": 8765, "health": "ok",
                       "log": "/Users/x/.mac-mcp/mac-mcp.log", "port_owners": []},
            "public_endpoint": {"mode": "cloudflare", "url": "https://mac.example.com/mcp", "tunnel_running": True,
                                "tunnel_pid": 77, "route": "ok", "error": None},
            "stray_processes": [], "privacy": {"paused": False, "private_mode": False},
            "supervisor": {"enabled": True, "loaded": True, "intent": "running"}, "remediation": [],
        }
        for key, value in changes.items():
            report[key] = {**report[key], **value} if isinstance(value, dict) and isinstance(report.get(key), dict) else value
        return report

    def render(self, report) -> str:
        out = FakeTTY()
        style = Style.for_stream(out, env=TTY_ENV)
        cli_views.render_status(style, report)
        return visible(out.getvalue())

    def test_healthy(self) -> None:
        text = self.render(self.report())
        self.assertTrue(text.startswith("  ◆ Mac MCP · status\n\n"))
        self.assertIn("  ● Running         pid 4321 · port 8765", text)
        self.assertIn("  ● Public address  mac.example.com · Cloudflare", text)
        self.assertIn("  ● Crash guard     watching", text)
        self.assertIn("Logs", text)
        self.assertTrue(text.rstrip("\n").endswith("✓ All good — AI apps can reach this Mac."), text)

    def test_stopped_names_the_next_command(self) -> None:
        text = self.render(self.report(ok=False, state="stopped", exit_code=1,
                                       server={"running": False, "pid": None, "health": None},
                                       remediation=["Start it with: mac-mcp start"]))
        self.assertIn("● Stopped", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Mac MCP is not running — try: mac-mcp start"), text)

    def test_tunnel_down_ends_with_the_remediation(self) -> None:
        text = self.render(self.report(ok=False, state="degraded", exit_code=2,
                                       public_endpoint={"tunnel_running": False, "route": None},
                                       remediation=["Run mac-mcp restart to start the cloudflare tunnel, or use --local-only to ignore it."]))
        self.assertIn("● Public address  mac.example.com · Cloudflare · tunnel not running", text)
        self.assertIn("⚠ Run mac-mcp restart to start the cloudflare tunnel", text)

    def test_local_only_and_privacy_paused(self) -> None:
        text = self.render(self.report(public_endpoint={"mode": "none", "url": None, "tunnel_running": None, "route": None},
                                       privacy={"paused": True, "private_mode": False}))
        self.assertIn("Public address  local only", text)
        self.assertIn("● Privacy", text)
        self.assertIn("observation paused", text)
        self.assertIn("AI apps on this Mac can reach it.", text)

    def test_status_command_uses_the_screen_and_keeps_its_exit_code(self) -> None:
        missing = ProcessValidation("missing", None, "server", None, "record_missing")
        public = type("Public", (), {"mode": "cloudflare", "endpoint_url": "https://mac.example.com/mcp"})()
        out = FakeTTY()
        with patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_validate_managed_pid", return_value=missing), \
             patch.object(cli, "_launchctl_pid", return_value=None), \
             patch.object(cli, "_server_health", return_value="ok"), \
             patch.object(cli, "CLOUDFLARE_PID_FILE", cli.Path("/nonexistent/cloudflared.pid")), \
             patch.object(cli, "resolve_public_endpoint", return_value=public), \
             redirect_stdout(out):
            code = cli.status(argparse.Namespace(json=False, local_only=False))
        self.assertEqual(2, code)
        self.assertIn("tunnel not running", visible(out.getvalue()))
        self.assertNotIn("overall: degraded", out.getvalue())
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views.StatusScreen -v`
Expected: FAIL: `AttributeError: module 'mcp_server.cli_views' has no attribute 'render_status'`.

- [ ] **Step 3: Add `render_status` to `cli_views.py`**

```python
from urllib.parse import urlparse

from .cli_style import home_path

_MODE_NAMES = {"cloudflare": "Cloudflare", "ngrok": "ngrok", "custom": "custom HTTPS"}


def _host(url: Optional[str]) -> str:
    return urlparse(url).netloc if url else ""


def render_status(style: Style, report: dict) -> None:
    server, public = report["server"], report["public_endpoint"]
    state = report["state"]
    rows = []
    if server["running"] and server.get("health") != "unreachable":
        rows.append(("ok", "Running", f"pid {server['pid']}", f"port {server['port']}"))
    elif server["running"]:
        rows.append(("warn", "Not answering", f"pid {server['pid']}", "/health does not answer"))
    elif state == "port_conflict":
        owners = ", ".join(f"{o['name'] or 'pid'} ({o['pid']})" for o in server.get("port_owners") or []) or "another program"
        rows.append(("error", "Port in use", f"port {server['port']}", f"used by {owners}"))
    elif state == "ownership_unverified":
        rows.append(("warn", "Not verified", "a server is running that Mac MCP cannot verify", ""))
    else:
        rows.append(("error", "Stopped", f"not answering on port {server['port']}", ""))

    mode = public.get("mode")
    if public.get("error"):
        rows.append(("error", "Public address", "configuration error", str(public["error"])))
    elif mode in (None, "none"):
        rows.append((None, "Public address", "local only", "AI apps on this Mac"))
    else:
        detail = _MODE_NAMES.get(mode, mode)
        tone = "ok"
        if public.get("tunnel_running") is False:
            detail, tone = f"{detail} · tunnel not running", "warn"
        elif public.get("route") == "unreachable":
            detail, tone = f"{detail} · does not answer", "warn"
        rows.append((tone, "Public address", _host(public.get("url")), detail))

    supervisor = report.get("supervisor") or {}
    if not supervisor.get("enabled"):
        rows.append((None, "Crash guard", "off", ""))
    elif not supervisor.get("loaded"):
        rows.append(("warn", "Crash guard", "not loaded", "mac-mcp start loads it"))
    else:
        recovery = supervisor.get("last_recovery") or {}
        detail = ""
        if recovery:
            import time as _time
            detail = "last recovery " + _time.strftime("%b %-d", _time.localtime(float(recovery.get("at") or 0)))
        if supervisor.get("backoff_until"):
            detail = "backing off after repeated failures"
        rows.append(("ok", "Crash guard", "watching", detail))

    privacy = report.get("privacy") or {}
    if privacy.get("paused"):
        rows.append(("warn", "Privacy", "observation paused", "Settings › Privacy"))
    elif privacy.get("private_mode"):
        rows.append(("info", "Privacy", "private mode on", "nothing about this work is kept"))
    for stray in report.get("stray_processes") or []:
        rows.append(("warn", "Stray process", f"{stray['name']} pid {stray['pid']}", "not the selected mode"))

    style.header("status")
    style.rows(rows)
    style.write()
    style.rows([(None, "Logs", home_path(server["log"]), "")])

    remediation = (report.get("remediation") or [""])[0]
    if state == "healthy":
        local = mode in (None, "none")
        style.summary("ok", "All good — AI apps on this Mac can reach it." if local else "All good — AI apps can reach this Mac.")
    elif state == "stopped":
        style.summary("error", "Mac MCP is not running", next_command="mac-mcp start")
    elif state in ("unresponsive", "ownership_unverified"):
        style.summary("error", "Mac MCP is not answering", next_command="mac-mcp restart")
    elif state == "port_conflict":
        style.summary("error", remediation or f"Port {server['port']} is taken")
    else:
        style.summary("warn", remediation or "The public address is not available")
```

- [ ] **Step 4: Make `status()` build the report for both paths and branch**

In `mcp_server/cli.py` `status()`, the `if getattr(args, "json", False):` block builds `report` and writes JSON. Change the end of the function so the report is built first and used three ways:

```python
    tunnel_pid = cloudflare_pid if public is not None and public.mode == "cloudflare" else (
        ngrok_pid if public is not None and public.mode == "ngrok" else None)
    stray = [
        {"name": name, "pid": pid}
        for name, pid, mode in (("ngrok", ngrok_pid, "ngrok"), ("cloudflared", cloudflare_pid, "cloudflare"))
        if pid is not None and (public is None or public.mode != mode)
    ]
    report = {
        # … exactly the dict that was inside the json branch …
    }
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return code
    style = Style.for_stream()
    if style is not None:
        from .cli_views import render_status
        render_status(style, report)
        return code
    for line in lines:
        sys.stdout.write(line + "\n")
    return code
```

Keep every key of the dict as it is; this only moves it out of the `if`. The `say(...)` lines stay; the plain branch prints them.

- [ ] **Step 5: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views tests.test_cli_plain_contract tests.test_public_endpoint -v`
Expected: all PASS (`test_public_endpoint` covers `status --json`).

- [ ] **Step 6: Commit**

```bash
git add mcp_server/cli.py mcp_server/cli_views.py tests/test_cli_views.py
git commit -m "CLI: a themed status screen"
```

---

### Task 6: Themed `start` and `stop`

**Files:**
- Modify: `mcp_server/cli.py`, `start()` (~1490) and `stop()` (~1520)
- Test: `tests/test_cli_views.py` (add `StartStop`)

**Interfaces:**
- Consumes: `Style.steps()` (Task 3): `step(label)`, `StepState.ok`, `.captured`.
- Produces: the themed `start(args)` and `stop(args)`, which return the same exit codes as today.

- [ ] **Step 1: Write the failing tests** (append)

```python
class StartStop(ThemedTestCase):
    def start(self, server_code=0, server_print="mac-mcp started (pid 4321)."):
        out = FakeTTY()

        def fake_server(args):
            print(server_print)
            return server_code

        public = type("Public", (), {"mode": "cloudflare", "endpoint_url": "https://mac.example.com/mcp"})()
        with patch.object(cli, "_public_endpoint_config", return_value=public), \
             patch.object(cli, "validate_bootstrap_security"), patch.object(cli, "load_settings"), \
             patch.object(cli, "_record_intent"), patch.object(cli, "_start_server", side_effect=fake_server), \
             patch.object(cli, "_stop_unselected_public_processes"), \
             patch.object(cli, "_start_cloudflare", side_effect=lambda args, public: print("cloudflared started") or 0), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             redirect_stdout(out):
            code = cli.start(argparse.Namespace(host="127.0.0.1", port=8765))
        return code, re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue()))

    def test_start_shows_steps_and_a_summary(self) -> None:
        code, text = self.start()
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · start\n\n"))
        self.assertIn("✓ Started the server", text)
        self.assertIn("✓ Connected the Cloudflare tunnel", text)
        self.assertNotIn("cloudflared started", text)  # a step's own prints show only when it fails
        self.assertTrue(text.rstrip("\n").endswith("✓ Started — pid 4321 · mac.example.com"), text)

    def test_a_failed_server_step_shows_its_reason(self) -> None:
        code, text = self.start(server_code=1, server_print="configured port 8765 is used by Python (pid 99).")
        self.assertEqual(1, code)
        self.assertIn("✗ Start the server", text)
        self.assertIn("      configured port 8765 is used by Python (pid 99).", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Mac MCP did not start — try: mac-mcp doctor"), text)

    def test_stop(self) -> None:
        out = FakeTTY()
        with patch.object(cli, "_record_intent"), \
             patch.object(cli, "_resolve_server_identity", return_value=(4321, "pid_record")), \
             patch.object(cli, "_stop_pid", return_value=True), patch.object(cli, "_stop_cloudflare", return_value=True), \
             patch.object(cli, "_server_job_plist_path", return_value=cli.Path("/nonexistent/job.plist")), \
             redirect_stdout(out):
            code = cli.stop(argparse.Namespace(timeout=5.0, force=False, keep_intent=False))
        text = visible(out.getvalue())
        self.assertEqual(0, code)
        self.assertIn("✓ Stopped the server", text)
        self.assertIn("✓ Stopped the public tunnel", text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Stopped — AI apps can no longer reach this Mac."), text)
```

Add `import re` at the top of `tests/test_cli_views.py`.

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views.StartStop -v`
Expected: FAIL (plain output, so there is no `◆ Mac MCP · start` header).

- [ ] **Step 3: Split `start()` into a plain body and a themed wrapper**

Rename today's `def start(args)` to `def _start_plain(args)`, unchanged. Then add:

```python
_TUNNEL_STEP = {"cloudflare": ("Connected the Cloudflare tunnel", "Connect the Cloudflare tunnel"),
                "ngrok": ("Started ngrok", "Start ngrok")}


def start(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if style is None:
        return _start_plain(args)
    _load_env()
    style.header("start")
    steps = style.steps()
    with steps.step("Checked the settings", failed_label="Check the settings") as state:
        try:
            public = _public_endpoint_config(args)
            validate_bootstrap_security(load_settings(), host=args.host, public_endpoint_mode=public.mode)
        except (PublicEndpointError, RuntimeError) as exc:
            print(str(exc))
            state.ok = False
    if not state.ok:
        style.summary("error", "Mac MCP's settings need a fix first", next_command="mac-mcp doctor")
        return 2
    _record_intent("running", args)
    with steps.step("Started the server", failed_label="Start the server") as state:
        code = _start_server(args)
        state.ok = code == 0
    if code != 0:
        style.summary("error", "Mac MCP did not start", next_command="mac-mcp doctor")
        return code
    _stop_unselected_public_processes(public.mode)
    if public.mode in _TUNNEL_STEP:
        done, failed = _TUNNEL_STEP[public.mode]
        with steps.step(done, failed_label=failed) as state:
            code = _start_cloudflare(args, public) if public.mode == "cloudflare" else _start_ngrok(args)
            state.ok = code == 0
        if code != 0:
            style.summary("warn", "Mac MCP is running, but its public address is not", next_command="mac-mcp doctor")
            return code
    pid = _resolve_server_identity(int(getattr(args, "port", _default_port())))[0]
    where = urlparse(public.endpoint_url).netloc if getattr(public, "endpoint_url", None) else "this Mac only"
    style.summary("ok", f"Started — pid {pid} · {where}")
    return 0
```

Each step passes the line it reads as when it fails (`failed_label`, Task 3).

Import `urlparse` in `cli.py` if it is not imported yet (`from urllib.parse import urlparse`).

- [ ] **Step 4: Split `stop()` the same way**

Rename today's `def stop(args)` to `def _stop_plain(args)`, unchanged, and add:

```python
def stop(args: argparse.Namespace) -> int:
    style = Style.for_stream()
    if style is None or getattr(args, "keep_intent", False):
        # keep_intent: the supervisor's and restart's own stop, never a person at a terminal.
        return _stop_plain(args)
    _load_env()
    style.header("stop")
    steps = style.steps()
    _record_intent("stopped", None)
    port = _default_port()
    with steps.step("Stopped the server", failed_label="Stop the server") as state:
        server_pid, server_source = _resolve_server_identity(port)
        if server_pid:
            state.ok = _stop_pid(PID_FILE, "mac-mcp", args.timeout, args.force)
        elif server_source in {"pid_unverifiable", "foreign_listener", "ambiguous_listener", "adoption_failed"}:
            print(f"Refusing to stop mac-mcp: server ownership is not verifiable ({server_source}).")
            state.ok = False
        else:
            PID_FILE.unlink(missing_ok=True)
    server_ok = state.ok
    with steps.step("Stopped the public tunnel", failed_label="Stop the public tunnel") as state:
        state.ok = _stop_pid(NGROK_PID_FILE, "ngrok", args.timeout, args.force) and _stop_cloudflare(args.timeout, args.force)
    tunnel_ok = state.ok
    if server_ok and STATE_DIR.resolve() == DEFAULT_STATE_DIR.resolve() and _server_job_plist_path().exists():
        _unload_server_job()
    if server_ok and tunnel_ok:
        style.summary("ok", "Stopped — AI apps can no longer reach this Mac.")
        return 0
    style.summary("error", "Something did not stop", next_command="mac-mcp stop --force")
    return 1
```

`_stop_plain` stays the function `restart` uses: `restart()` calls `stop(stop_args)` with `keep_intent=True`, which takes the plain path above. Keep that call as it is.

- [ ] **Step 5: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views tests.test_cli_plain_contract tests.test_public_endpoint tests.test_cli_stop_exiting_process -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/cli.py tests/test_cli_views.py
git commit -m "CLI: themed start and stop with live steps"
```

---

### Task 7: Themed `restart` that waits and shows its stages

**Files:**
- Modify: `mcp_server/cli.py`, `restart()` (~2276), plus a new `_watch_restart(style, steps, since)`
- Test: `tests/test_cli_views.py` (add `RestartScreen`)

**Interfaces:**
- Consumes: `_spawn_detached_restart(args)` (today), `_read_restart_status()` (today), `RESTART_FINAL_STATES`, `Steps.begin/end`.
- Produces: in a terminal, `restart` waits for the worker's outcome and shows the stages `stopping` → "Stopped the server", `starting` → "Started the server", `verifying` → "Checked that it answers", and `recovering` → "Recovered after a failed start". Its exit codes are those of `--wait`: 0 succeeded, 1 failed/degraded, 3 no outcome in time.

- [ ] **Step 1: Write the failing tests** (append)

```python
class RestartScreen(ThemedTestCase):
    def test_stages_become_steps_and_the_outcome_the_summary(self) -> None:
        payloads = iter([
            {"state": "running", "stage": "stopping", "updated_at": 2e9},
            {"state": "running", "stage": "starting", "updated_at": 2e9},
            {"state": "running", "stage": "verifying", "updated_at": 2e9},
            {"state": "succeeded", "server_pid": 4321, "updated_at": 2e9},
        ])
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", side_effect=lambda args: print("handed off") or 0) as spawn, \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), \
             redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        text = re.sub(r"\d+\.\d+s", "0.0s", visible(out.getvalue()))
        self.assertEqual(0, code)
        self.assertFalse(spawn.call_args[0][0].wait)  # the themed path waits itself
        for label in ("✓ Handed the restart to launchd", "✓ Stopped the server", "✓ Started the server", "✓ Checked that it answers"):
            self.assertIn(label, text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Restarted — pid 4321"), text)

    def test_a_failed_restart_names_the_stage_and_the_repair(self) -> None:
        payloads = iter([
            {"state": "running", "stage": "starting", "updated_at": 2e9},
            {"state": "failed", "stage": "health", "repair_command": "mac-mcp doctor", "updated_at": 2e9},
        ])
        out = FakeTTY()
        with patch.dict(os.environ, {cli.RESTART_HANDOFF_ENV: ""}), \
             patch.object(cli, "_spawn_detached_restart", return_value=0), \
             patch.object(cli, "_read_restart_status", side_effect=lambda: next(payloads)), \
             patch.object(cli.time, "sleep"), redirect_stdout(out):
            code = cli.restart(argparse.Namespace(wait=False, timeout=5.0))
        text = visible(out.getvalue())
        self.assertEqual(1, code)
        self.assertIn("✗ Start the server", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ Restart failed — try: mac-mcp doctor"), text)
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views.RestartScreen -v`
Expected: FAIL (plain path, so there is no header).

- [ ] **Step 3: Implement the themed requester path**

In `restart()`, replace:
```python
    if not handoff_child:
        ...
        return _spawn_detached_restart(args)
```
with:
```python
    if not handoff_child:
        # Always delegate restart to launchd. A restart tears down the server that
        # may currently be carrying this command; launchd ownership prevents MCP
        # cancellation cleanup or shell process-group cleanup from killing the worker.
        style = Style.for_stream()
        if style is None:
            return _spawn_detached_restart(args)
        return _restart_themed(args, style)
```
(keep the original comment lines) and add after `restart()`:

```python
_RESTART_STAGES = {
    "stopping": ("Stopped the server", "Stop the server"),
    "starting": ("Started the server", "Start the server"),
    "verifying": ("Checked that it answers", "Check that it answers"),
    "recovering": ("Recovered after a failed start", "Recover after a failed start"),
}


def _restart_themed(args: argparse.Namespace, style) -> int:
    """In a terminal, restart waits for the launchd worker and shows its stages as steps."""
    style.header("restart")
    steps = style.steps()
    since = time.time()
    with steps.step("Handed the restart to launchd", failed_label="Hand the restart to launchd") as state:
        code = _spawn_detached_restart(argparse.Namespace(**{**vars(args), "wait": False}))
        state.ok = code == 0
    if code != 0:
        style.summary("error", "The restart could not start", next_command="mac-mcp doctor")
        return code
    return _watch_restart(style, steps, args, since)


def _watch_restart(style, steps, args: argparse.Namespace, since: float) -> int:
    deadline = time.time() + 2 * _startup_health_timeout_s() + RESTART_REQUESTER_WAIT_S + 60
    current: str | None = None
    payload: dict = {}
    while time.time() < deadline:
        payload = _read_restart_status()
        state = str(payload.get("state") or "")
        stage = str(payload.get("stage") or "")
        if state in RESTART_FINAL_STATES and float(payload.get("updated_at") or 0) >= since:
            break
        if stage in _RESTART_STAGES and stage != current:
            if current is not None:
                steps.end(True)
            steps.begin(_RESTART_STAGES[stage][0])
            current = stage
        time.sleep(0.25)
    else:
        if current is not None:
            steps.end(False, failed_label=_RESTART_STAGES[current][1])
        style.summary("error", "The restart did not report back in time", next_command="mac-mcp status")
        return 3
    state = str(payload.get("state"))
    if current is not None:
        steps.end(state == "succeeded", str(payload.get("error") or ""), _RESTART_STAGES[current][1])
    if state == "succeeded":
        retried = " · after one recovery attempt" if payload.get("recovered_after_retry") else ""
        style.summary("ok", f"Restarted — pid {payload.get('server_pid')}{retried}")
        return 0
    if state == "degraded":
        style.summary("warn", "Restarted locally, but the public address did not start", next_command="mac-mcp doctor")
        return 1
    if payload.get("server_left_running"):
        style.summary("warn", f"Restart not attempted ({payload.get('stage')}): {payload.get('error')}. The server kept running.")
        return 1
    style.summary("error", "Restart failed", next_command=str(payload.get("repair_command") or "mac-mcp doctor"))
    return 1
```

`Steps.end(ok, reason, failed_label)` is the signature from Task 3.

- [ ] **Step 4: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views tests.test_cli_plain_contract -v`, then `~/mac-mcp/.venv/bin/python -m unittest discover -s tests -p "test_*restart*"`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/cli.py tests/test_cli_views.py
git commit -m "CLI: a themed restart that waits and shows its stages"
```

---

### Task 8: Themed `doctor`

**Files:**
- Modify: `mcp_server/cli.py`, `doctor()` (~2489), and the `doctor` subparser (add `--verbose`)
- Modify: `mcp_server/cli_views.py` (add `render_doctor`, `DOCTOR_GROUPS`)
- Test: `tests/test_cli_views.py` (add `DoctorScreen`)

**Interfaces:**
- Consumes: the `run_doctor()` report: `{ok, health, counts, checks: [{check_id, category, status (pass|warn|fail|info), summary, remediation?}]}`.
- Produces: `render_doctor(style, report: dict, verbose: bool, support_path: Optional[str]) -> None`; a `--verbose` flag on `doctor`.

- [ ] **Step 1: Write the failing tests** (append)

```python
class DoctorScreen(ThemedTestCase):
    def report(self, *checks, health="healthy", ok=True):
        return {"ok": ok, "health": health, "counts": {}, "checks": [
            {"check_id": check_id, "category": check_id.split(".")[0], "status": status, "summary": summary,
             **({"remediation": fix} if fix else {})}
            for check_id, status, summary, fix in checks]}

    def render(self, report, verbose=False) -> str:
        out = FakeTTY()
        cli_views.render_doctor(Style.for_stream(out, env=TTY_ENV), report, verbose, None)
        return visible(out.getvalue())

    def test_passes_fold_per_group_and_notes_are_written_out(self) -> None:
        text = self.render(self.report(
            ("runtime.python", "pass", "Python 3.14.7 on arm64", None),
            ("runtime.venv", "pass", "Runtime Python is present.", None),
            ("permissions.accessibility", "pass", "Accessibility is allowed.", None),
            ("permissions.microphone", "info", "Microphone is asked the first time you use voice.", None),
            ("dependencies.cloudflared", "pass", "cloudflared is available.", None),
        ))
        self.assertTrue(text.startswith("  ◆ Mac MCP · doctor\n\n"))
        self.assertIn("Runtime       ✓ 2 checks passed", text)
        self.assertIn("Permissions   ✓ Accessibility", text)
        self.assertIn("ℹ Microphone is asked the first time you use voice.", text)
        self.assertIn("Dependencies  ✓ cloudflared", text)
        self.assertTrue(text.rstrip("\n").endswith("✓ Everything Mac MCP needs is in place.   (--verbose: every check)"), text)

    def test_warnings_and_failures_show_their_fix(self) -> None:
        text = self.render(self.report(
            ("server.health", "fail", "The server does not answer /health.", "Run mac-mcp restart."),
            ("agents.worktrees", "warn", "2 agent worktrees hold unreviewed changes.", "Review them in Settings › Agents."),
            ok=False))
        self.assertIn("✗ The server does not answer /health.", text)
        self.assertIn("Fix: Run mac-mcp restart.", text)
        self.assertIn("⚠ 2 agent worktrees hold unreviewed changes.", text)
        self.assertTrue(text.rstrip("\n").endswith("✗ 1 problem and 1 warning — fix them, then run: mac-mcp doctor"), text)

    def test_an_unknown_category_lands_in_other(self) -> None:
        text = self.render(self.report(("brandnew.check", "warn", "Something new to look at.", None), ok=True))
        self.assertIn("Other", text)
        self.assertIn("⚠ Something new to look at.", text)

    def test_verbose_lists_every_check(self) -> None:
        text = self.render(self.report(("runtime.python", "pass", "Python 3.14.7 on arm64", None)), verbose=True)
        self.assertIn("✓ Python 3.14.7 on arm64", text)

    def test_doctor_command_json_and_plain_are_untouched(self) -> None:
        from mcp_server import diagnostics
        report = self.report(("runtime.python", "pass", "Python 3.14.7 on arm64", None))
        out = FakeTTY()
        with patch.object(diagnostics, "run_doctor", return_value=report), redirect_stdout(out):
            code = cli.doctor(argparse.Namespace(json=True, support_bundle=None, local_only=False, verbose=False))
        self.assertEqual(0, code)
        self.assertTrue(out.getvalue().lstrip().startswith("{"))
```

- [ ] **Step 2: Run them to see them fail**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views.DoctorScreen -v`
Expected: FAIL: `AttributeError: … has no attribute 'render_doctor'`.

- [ ] **Step 3: Add `render_doctor` to `cli_views.py`**

```python
DOCTOR_GROUPS = {
    "runtime": "Runtime", "cli": "Runtime", "state": "Runtime", "system": "Runtime",
    "server": "Server", "process": "Server", "network": "Server", "auth": "Server", "config": "Server", "security": "Server",
    "permissions": "Permissions", "dependencies": "Dependencies", "companion": "Dependencies",
    "agents": "Agents", "update": "Updates",
}
_GROUP_ORDER = ["Runtime", "Server", "Permissions", "Dependencies", "Agents", "Updates", "Other"]
# Groups whose passes read better as names than as a count.
_NAMED_PASSES = {"Permissions", "Dependencies"}
_TONES = {"pass": "ok", "warn": "warn", "fail": "error", "info": "info"}


_TOOL_NAMES = {"osascript", "cliclick", "cloudflared", "ngrok"}


def _pass_name(check_id: str) -> str:
    """Tools keep their own lower-case name; the rest read as words (Accessibility)."""
    name = check_id.split(".", 1)[-1].replace("_", " ")
    return name if name in _TOOL_NAMES else name[:1].upper() + name[1:]


def render_doctor(style: Style, report: dict, verbose: bool, support_path: Optional[str]) -> None:
    groups: dict = {}
    for check in report.get("checks", []):
        group = DOCTOR_GROUPS.get(str(check.get("category") or ""), "Other")
        groups.setdefault(group, []).append(check)
    width = max((len(name) for name in groups), default=0) + 2
    style.header("doctor")
    for name in [group for group in _GROUP_ORDER if group in groups]:
        checks = groups[name]
        lines = []
        passed = [check for check in checks if check.get("status") == "pass"]
        if passed and not verbose:
            if name in _NAMED_PASSES:
                text = f" {style.paint('·', dim=True)} ".join(_pass_name(str(check["check_id"])) for check in passed)
            else:
                text = f"{len(passed)} check{'s' if len(passed) != 1 else ''} passed"
            lines.append(f"{style.paint('✓', 'ok')} {text}")
        for check in checks:
            status = str(check.get("status") or "info")
            if status == "pass" and not verbose:
                continue
            tone = _TONES.get(status, "info")
            lines.append(f"{style.paint(SYMBOLS[tone], tone)} {check.get('summary')}")
            if check.get("remediation") and status in {"warn", "fail"}:
                lines.append(f"  {style.paint('Fix: ' + str(check['remediation']), dim=True)}")
        for index, line in enumerate(lines):
            label = style.paint(name.ljust(width), bold=True) if index == 0 else " " * width
            style.write(f"{INDENT}{label}{line}")
    if support_path:
        style.write()
        style.write(f"{INDENT}Support bundle  {support_path}")
    fails = sum(1 for check in report.get("checks", []) if check.get("status") == "fail")
    warns = sum(1 for check in report.get("checks", []) if check.get("status") == "warn")
    hint = "   " + style.paint("(--verbose: every check)", dim=True) if not verbose else ""
    if fails:
        parts = [f"{fails} problem{'s' if fails != 1 else ''}"] + ([f"{warns} warning{'s' if warns != 1 else ''}"] if warns else [])
        style.summary("error", " and ".join(parts) + " — fix them, then run: mac-mcp doctor")
    elif report.get("health") == "degraded":
        style.summary("warn", "The local runtime is fine; the public address is not available", next_command="mac-mcp status")
    elif warns:
        style.summary("warn", f"{warns} thing{'s' if warns != 1 else ''} to look at")
    else:
        style.summary("ok", "Everything Mac MCP needs is in place." + hint)
```

- [ ] **Step 4: Branch in `doctor()` and add `--verbose`**

In `build_parser()`, after the other `p_doctor.add_argument(...)` lines, add:
```python
    p_doctor.add_argument("--verbose", action="store_true", help="In a terminal, list every check.")
```

In `doctor()`, replace:
```python
    else:
        print(format_report(report))
        if support_path is not None:
            print(f"Support bundle: {support_path}")
    return code
```
with:
```python
    else:
        style = Style.for_stream()
        if style is not None:
            from .cli_views import render_doctor
            render_doctor(style, report, bool(getattr(args, "verbose", False)), str(support_path) if support_path else None)
            return code
        print(format_report(report))
        if support_path is not None:
            print(f"Support bundle: {support_path}")
    return code
```

- [ ] **Step 5: Run the tests**

Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_views tests.test_cli_plain_contract tests.test_diagnostics -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/cli.py mcp_server/cli_views.py tests/test_cli_views.py
git commit -m "CLI: a grouped, themed doctor screen"
```

---

### Task 9: Full verification, docs, promotion and live check

**Files:**
- Modify: `CHANGELOG.md` (Unreleased entry)
- Modify: `README.md` (where the CLI commands are described: one paragraph on the terminal theme and `MAC_MCP_PLAIN`/`NO_COLOR`)
- Test: the whole suite

- [ ] **Step 1: Prove that `stdio` does not load the theme's screens**

`mac-mcp stdio` goes through `main()`, so it builds the parser and loads the small `cli_style`, and that is all: never `cli_views` or `cli_steps`. Add to `tests/test_cli_style.py`:
```python
class StdioStaysLean(unittest.TestCase):
    def test_building_the_parser_does_not_load_the_screens(self) -> None:
        import subprocess, sys
        code = ("import sys; from mcp_server import cli; cli.build_parser(); "
                "print('mcp_server.cli_views' in sys.modules, 'mcp_server.cli_steps' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual("False False", out.stdout.strip(), out.stderr)
```
Run: `~/mac-mcp/.venv/bin/python -m unittest tests.test_cli_style.StdioStaysLean -v`
Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run:
```bash
MCP_API_KEY=test-key-0123456789abcdef0123456789 MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true MAC_MCP_SKIP_MENU_APP_INSTALL=1 MAC_MCP_TELEMETRY_DIR=/tmp/cli-theme-telemetry ~/mac-mcp/.venv/bin/python -m unittest discover -s tests
```
Expected: `OK`. Also `git diff --check`.

- [ ] **Step 3: Write the docs**

`CHANGELOG.md`, under `## Unreleased`:
```markdown
### The CLI has a look of its own
- In a terminal, `mac-mcp`, `--help`, `status`, `start`, `stop`, `restart` and `doctor` open with the same header, line their facts up in one column and end with one sentence that says what happened, plus the command to run next when something is wrong. `start`, `stop` and `restart` show each step live, with its time; `restart` now waits for the outcome in a terminal. `doctor` groups its checks and folds the ones that passed (`--verbose` lists every check).
- Without a terminal (scripts, the menu app, the installer), with `NO_COLOR`, `MAC_MCP_PLAIN=1` or `--json`, the output is exactly what it was.
```
In `README.md`, next to the CLI command list, add:
```markdown
In a terminal the CLI uses Mac MCP's own look (colours, aligned rows, live steps). Set `NO_COLOR=1` or `MAC_MCP_PLAIN=1` for plain text; pipes and `--json` are always plain.
```

- [ ] **Step 4: Commit, push and promote**

```bash
git add CHANGELOG.md README.md tests/test_cli_style.py
git commit -m "Docs: the CLI's terminal look"
git push origin main
```
Then promote only the changed runtime files (`mcp_server/cli.py`, `cli_style.py`, `cli_steps.py`, `cli_views.py`):
1. Check that each runtime copy equals the previous commit's version.
2. Back up the runtime copies.
3. Copy the new files.
4. Restart with `~/.local/bin/mac-mcp restart --wait`.
5. Verify a single listener on 8765, `/health` 200, a new PID and clean logs.

- [ ] **Step 5: Live check in a real terminal**

In Terminal.app (dark and light profiles), run `~/.local/bin/mac-mcp`, `mac-mcp --help`, `mac-mcp status`, `mac-mcp restart`, `mac-mcp doctor`, `mac-mcp status --help`. Capture the window with `screencapture -l <window id>` and show the owner. Then confirm the plain path:
- `mac-mcp status | cat` prints the old lines;
- `NO_COLOR=1 mac-mcp status` prints the old lines;
- the menu app's Settings › Help & Diagnostics still shows the doctor report;
- the menu's Restart still shows its notice.
