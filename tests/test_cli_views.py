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
        self.assertIn("    status          How Mac MCP is doing", text)
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

    def test_home_lines_command_names_up_in_one_column(self) -> None:
        with patch.object(cli, "_home_running", return_value=False):
            _, text = self.run_main([])
        lines = [line for line in text.split("\n") if line.startswith("    ") and line.strip()]
        columns = {len(line) - len(line.lstrip(" ").split(" ", 1)[1].lstrip(" ")) for line in lines}
        self.assertEqual(1, len(columns), lines)  # connect-config does not push its text out
        self.assertIn("● Stopped", text.split("\n")[0])

    def test_command_help_says_what_the_command_does(self) -> None:
        _, text = self.run_main(["status", "--help"])
        self.assertIn("  Show server and public endpoint status.", text)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["status", "--help"])  # not a terminal: argparse's own help, no description added
        self.assertNotIn("Show server and public endpoint status", out.getvalue())

    def test_nested_command_help_is_themed_too(self) -> None:
        code, text = self.run_main(["recipe", "--help"])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("  ◆ Mac MCP · recipe\n"), text[:80])


if __name__ == "__main__":
    unittest.main()
