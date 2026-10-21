"""#84: the updater restarts and health-checks the port this installation serves on.

It read MAC_MCP_PORT from the runtime .env, else the --port of whatever uvicorn ps listed first,
else 8000. With the port kept in settings.json (the usual case), a stopped server sent the health
check to 8000, and a test server on 8877 could be taken for this one.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import update_helper


class UpdatePortDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-port-")
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "mcp_server").mkdir(parents=True)
        self.settings = self.root / "settings.json"
        env = {key: value for key, value in os.environ.items() if key != "MAC_MCP_PORT"}
        env["MAC_MCP_SETTINGS_PATH"] = str(self.settings)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.ps_lines = ""
        self.ps = patch.object(update_helper, "_run", side_effect=lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout=self.ps_lines, stderr=""))
        self.ps.start()

    def tearDown(self) -> None:
        self.ps.stop()
        self.env.stop()
        self.temp.cleanup()

    def settings_port(self, port) -> None:
        self.settings.write_text(json.dumps({"server": {"port": port}, "other": True}), encoding="utf-8")

    def test_the_settings_port_wins_over_another_runtimes_server(self) -> None:
        self.settings_port(8765)
        self.ps_lines = "python -m uvicorn mcp_server.main:app --host 127.0.0.1 --port 8877\n"
        self.assertEqual(8765, update_helper._detect_port(self.runtime))

    def test_a_stopped_server_still_comes_back_on_its_port(self) -> None:
        self.settings_port(9123)
        self.assertEqual(9123, update_helper._detect_port(self.runtime))

    def test_mac_mcp_port_still_comes_first(self) -> None:
        self.settings_port(8765)
        (self.runtime / "mcp_server" / ".env").write_text("MAC_MCP_PORT=8899\n", encoding="utf-8")
        self.assertEqual(8899, update_helper._detect_port(self.runtime))
        with patch.dict(os.environ, {"MAC_MCP_PORT": "8900"}):
            self.assertEqual(8900, update_helper._detect_port(self.runtime))

    def test_without_settings_the_old_fallbacks_remain(self) -> None:
        for bad in ("eighty", 0, 70000, None):
            with self.subTest(bad=bad):
                self.settings_port(bad)
                self.ps_lines = "uvicorn mcp_server.main:app --port 8765\n"
                self.assertEqual(8765, update_helper._detect_port(self.runtime))
        self.settings.unlink()
        self.ps_lines = ""
        self.assertEqual(update_helper.DEFAULT_PORT, update_helper._detect_port(self.runtime))


if __name__ == "__main__":
    unittest.main()
