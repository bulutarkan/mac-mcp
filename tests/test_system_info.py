from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import tools_terminal


class SystemInfoTests(unittest.TestCase):
    def test_reports_the_macos_version(self) -> None:
        # The onboarding's first request asks the AI app which macOS version this Mac runs.
        with patch.object(tools_terminal, "run_command", side_effect=lambda settings, script: script):
            script = tools_terminal.get_system_info(object())
        self.assertIn("sw_vers", script)


if __name__ == "__main__":
    unittest.main()
