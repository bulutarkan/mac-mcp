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
