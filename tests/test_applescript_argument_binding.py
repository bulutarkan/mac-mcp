from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server.tools_macos import (
    _run_apple,
    run_applescript,
    send_notification,
    set_reminder,
)


INJECTION = 'x" & do shell script "echo SHOULD_NOT_RUN" & "'
WEIRD_TEXT = 'quote " slash \\ newline\nsecond line — üĞ🚀'


class AppleScriptArgumentBindingTests(unittest.TestCase):
    def test_run_apple_preserves_option_like_and_multiline_argv(self) -> None:
        script = r'''
on run argv
    return (item 1 of argv) & "|" & (item 2 of argv) & "|" & (item 3 of argv)
end run
'''
        result = _run_apple(script, args=["--help", "-e", WEIRD_TEXT])
        self.assertTrue(result["ok"], result)
        self.assertEqual(f"--help|-e|{WEIRD_TEXT}", result["stdout"])

    def test_notification_user_data_never_enters_script_source(self) -> None:
        with patch("mcp_server.tools_macos._run_apple", return_value={"ok": True}) as run:
            result = send_notification(
                None,
                title=INJECTION,
                message=WEIRD_TEXT,
                sound="--help",
            )
        self.assertTrue(result["ok"])
        script = run.call_args.args[0]
        args = run.call_args.kwargs["args"]
        self.assertNotIn(INJECTION, script)
        self.assertNotIn(WEIRD_TEXT, script)
        self.assertNotIn("--help", script)
        self.assertIn("on run argv", script)
        self.assertIn("display notification notificationMessage", script)
        self.assertEqual([INJECTION, WEIRD_TEXT, "--help"], args)

    def test_notification_empty_strings_are_bound_as_data(self) -> None:
        with patch("mcp_server.tools_macos._run_apple", return_value={"ok": True}) as run:
            send_notification(None, title="", message="", sound="")
        self.assertEqual(["", "", ""], run.call_args.kwargs["args"])

    def test_reminder_user_data_never_enters_script_source_without_due_date(self) -> None:
        with patch("mcp_server.tools_macos._run_apple", return_value={"ok": True}) as run:
            result = set_reminder(None, title=INJECTION, notes=WEIRD_TEXT)
        self.assertTrue(result["ok"])
        script = run.call_args.args[0]
        args = run.call_args.kwargs["args"]
        self.assertNotIn(INJECTION, script)
        self.assertNotIn(WEIRD_TEXT, script)
        self.assertIn("set name of r to reminderTitle", script)
        self.assertIn("set body of r to reminderNotes", script)
        self.assertEqual([INJECTION, WEIRD_TEXT], args)

    def test_reminder_user_data_never_enters_due_date_script_source(self) -> None:
        with patch("mcp_server.tools_macos._run_apple", return_value={"ok": True}) as run:
            result = set_reminder(
                None,
                title=INJECTION,
                notes=WEIRD_TEXT,
                due_date="2026-10-02 14:35",
            )
        self.assertTrue(result["ok"])
        script = run.call_args.args[0]
        args = run.call_args.kwargs["args"]
        self.assertNotIn(INJECTION, script)
        self.assertNotIn(WEIRD_TEXT, script)
        self.assertIn("set year of d to 2026", script)
        self.assertIn("set month of d to 10", script)
        self.assertIn("set day of d to 2", script)
        self.assertIn("set hours of d to 14", script)
        self.assertIn("set minutes of d to 35", script)
        self.assertEqual([INJECTION, WEIRD_TEXT], args)

    def test_invalid_due_date_never_invokes_osascript(self) -> None:
        with patch("mcp_server.tools_macos._run_apple") as run:
            result = set_reminder(None, title="safe", notes="safe", due_date=INJECTION)
        self.assertFalse(result["ok"])
        self.assertIn("Could not parse due_date", result["error"])
        run.assert_not_called()

    def test_raw_run_applescript_remains_raw_execution(self) -> None:
        raw = 'display dialog "raw"'
        with patch("mcp_server.tools_macos._run_apple", return_value={"ok": True}) as run:
            result = run_applescript(None, raw, timeout_s=17)
        self.assertTrue(result["ok"])
        run.assert_called_once_with(raw, 17)


if __name__ == "__main__":
    unittest.main()
