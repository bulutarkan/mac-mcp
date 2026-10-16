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


class Background(unittest.TestCase):
    """A light terminal gets darker tones: the bright ones read only on dark backgrounds."""

    def test_the_terminals_answer_decides(self) -> None:
        self.assertTrue(cli_style.background_is_light(b"\x1b]11;rgb:dfff/dba4/c3ff\x07"))   # Terminal.app Novel
        self.assertTrue(cli_style.background_is_light(b"]11;rgb:ffff/ffff/ffff\x1b\\"))
        self.assertFalse(cli_style.background_is_light(b"\x1b]11;rgb:0000/0000/0000\x07"))  # Pro
        self.assertFalse(cli_style.background_is_light(b"\x1b]11;rgb:1e/1e/1e\x07"))
        self.assertIsNone(cli_style.background_is_light(b""))
        self.assertIsNone(cli_style.background_is_light(b"garbage"))

    def test_light_palette_and_override(self) -> None:
        light = Style.for_stream(FakeTTY(), env={**ENV, "MAC_MCP_THEME": "light"}, width=80)
        self.assertEqual("\x1b[38;2;168;77;11mx\x1b[0m", light.paint("x", "brand"))
        dark = Style.for_stream(FakeTTY(), env={**ENV, "MAC_MCP_THEME": "dark"}, width=80)
        self.assertEqual("\x1b[38;2;255;180;94mx\x1b[0m", dark.paint("x", "brand"))
        light256 = Style.for_stream(FakeTTY(), env={"TERM": "xterm-256color", "MAC_MCP_THEME": "light"}, width=80)
        self.assertEqual("\x1b[38;5;130mx\x1b[0m", light256.paint("x", "brand"))

    def test_no_query_without_a_real_terminal(self) -> None:
        # A stream without a file descriptor (tests, pipes) is never asked; the dark tones stay.
        style = Style.for_stream(FakeTTY(), env=ENV, width=80)
        self.assertFalse(style.light)


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

    def test_a_long_summary_wraps_under_its_text(self) -> None:
        style, stream = self.style(width=50)
        style.summary("error", "Port 8765 is used by Python (pid 50849). Mac MCP never stops programs it did not start.")
        lines = visible(stream.getvalue()).strip("\n").split("\n")
        self.assertTrue(all(len(line) <= 50 for line in lines), lines)
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[0].startswith("  ✗ Port 8765"), lines)
        self.assertTrue(all(line.startswith("    ") for line in lines[1:]), lines)

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


class StdioStaysLean(unittest.TestCase):
    def test_building_the_parser_does_not_load_the_screens(self) -> None:
        import subprocess, sys
        code = ("import sys; from mcp_server import cli; cli.build_parser(); "
                "print('mcp_server.cli_views' in sys.modules, 'mcp_server.cli_steps' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual("False False", out.stdout.strip(), out.stderr)


class BackgroundQuerySafety(unittest.TestCase):
    """The OSC 11 probe never runs where it could stop or break the command."""

    class Stream:
        def fileno(self):
            return 1

    def setUp(self) -> None:
        cli_style._background_cache.clear()
        self.addCleanup(cli_style._background_cache.clear)

    def test_a_background_job_is_not_asked(self) -> None:
        import termios
        from unittest.mock import patch
        with patch.object(cli_style.os, "isatty", return_value=True), \
             patch.object(cli_style.os, "open", return_value=99), patch.object(cli_style.os, "close"), \
             patch.object(cli_style.os, "tcgetpgrp", return_value=111), patch.object(cli_style.os, "getpgrp", return_value=222), \
             patch.object(termios, "tcgetattr", side_effect=AssertionError("must not touch the terminal")):
            self.assertIsNone(cli_style._ask_background(self.Stream()))

    def test_a_terminal_error_is_not_a_crash(self) -> None:
        import termios
        from unittest.mock import patch
        with patch.object(cli_style.os, "isatty", return_value=True), \
             patch.object(cli_style.os, "open", return_value=99), patch.object(cli_style.os, "close"), \
             patch.object(cli_style.os, "tcgetpgrp", return_value=5), patch.object(cli_style.os, "getpgrp", return_value=5), \
             patch.object(termios, "tcgetattr", side_effect=termios.error(5, "Input/output error")):
            self.assertIsNone(cli_style._ask_background(self.Stream()))

    def test_typeahead_is_kept(self) -> None:
        import termios
        import tty
        from unittest.mock import patch
        calls = []
        with patch.object(cli_style.os, "isatty", return_value=True), \
             patch.object(cli_style.os, "open", return_value=99), patch.object(cli_style.os, "close"), \
             patch.object(cli_style.os, "tcgetpgrp", return_value=5), patch.object(cli_style.os, "getpgrp", return_value=5), \
             patch.object(termios, "tcgetattr", return_value=[]), patch.object(termios, "tcsetattr"), \
             patch.object(tty, "setraw", side_effect=lambda fd, when=termios.TCSAFLUSH: calls.append(when)), \
             patch.object(cli_style.os, "write"), \
             patch("select.select", return_value=([], [], [])):
            cli_style._ask_background(self.Stream())
        self.assertEqual([termios.TCSANOW], calls)


if __name__ == "__main__":
    unittest.main()
