"""MCP sessions are forgotten after MAC_MCP_SESSION_IDLE_TIMEOUT_S without a request (default 30 min).

`mac-mcp stdio` sets a forgotten session up again by itself (verified end to end against a server
with a 60 s timeout); the setting is for clients that do not, such as Claude Desktop through mcp-remote.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import main


class SessionIdleTimeoutTests(unittest.TestCase):
    def timeout(self, raw: str | None) -> float:
        env = {} if raw is None else {"MAC_MCP_SESSION_IDLE_TIMEOUT_S": raw}
        with patch.dict(os.environ, env, clear=False):
            if raw is None:
                os.environ.pop("MAC_MCP_SESSION_IDLE_TIMEOUT_S", None)
            return main.session_idle_timeout_s()

    def test_default_bounds_and_bad_values(self) -> None:
        self.assertEqual(1800.0, self.timeout(None))
        self.assertEqual(7200.0, self.timeout("7200"))
        self.assertEqual(60.0, self.timeout("5"))
        self.assertEqual(86400.0, self.timeout("999999"))
        self.assertEqual(1800.0, self.timeout("soon"))

    def test_the_app_uses_it(self) -> None:
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn("mcp.session_manager.session_idle_timeout = session_idle_timeout_s()", source)
        self.assertNotIn("session_idle_timeout = 1800.0", source)


if __name__ == "__main__":
    unittest.main()
