"""#86: run_applescript honours MCP_ALLOW_SHELL like every other way to run commands.

AppleScript runs commands itself (do shell script), so with the shell switch off the raw tool
ran them anyway. Sending a notification, which binds its data as arguments, stays available.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import tools_macos
from mcp_server.security import load_settings


class AppleScriptShellSwitchTests(unittest.TestCase):
    def test_raw_applescript_is_refused_while_the_shell_is_off(self) -> None:
        off = replace(load_settings(), allow_shell=False)
        with patch.object(tools_macos, "_run_apple", return_value={"ok": True}) as run:
            with self.assertRaises(HTTPException) as refused:
                tools_macos.run_applescript(off, 'do shell script "touch /tmp/x"')
        self.assertEqual(403, refused.exception.status_code)
        run.assert_not_called()

    def test_it_runs_while_the_shell_is_on(self) -> None:
        on = replace(load_settings(), allow_shell=True)
        with patch.object(tools_macos, "_run_apple", return_value={"ok": True, "stdout": "hi"}) as run:
            self.assertEqual("hi", tools_macos.run_applescript(on, 'return "hi"', timeout_s=5)["stdout"])
        run.assert_called_once_with('return "hi"', 5)

    def test_notifications_do_not_need_the_shell(self) -> None:
        off = replace(load_settings(), allow_shell=False)
        with patch.object(tools_macos, "_run_apple", return_value={"ok": True}) as run:
            self.assertTrue(tools_macos.send_notification(off, "Title", "Body")["ok"])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
