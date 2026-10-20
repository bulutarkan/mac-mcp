"""The API key in Mac MCP's .env wins over an older MCP_API_KEY left in the environment.

Clients get the .env key (`mac-mcp stdio`, connect, the dashboard). A stale key exported in a shell
used to win when the server was restarted from that shell, and every client was then rejected.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import security, update_helper


class ApiKeySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_file = Path(self.temp.name) / ".env"
        environ = patch.dict(os.environ, {}, clear=False)
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("MCP_API_KEY", "MAC_MCP_TEST_SETTING"):
            os.environ.pop(name, None)
        warned = patch.object(security, "_KEY_SOURCE_WARNED", False)
        warned.start()
        self.addCleanup(warned.stop)

    def test_env_file_key_replaces_a_stale_exported_key_and_says_so(self) -> None:
        self.env_file.write_text('MCP_API_KEY="fresh-key"\nMAC_MCP_TEST_SETTING=from-file\n', encoding="utf-8")
        os.environ["MCP_API_KEY"] = "stale-key"
        os.environ["MAC_MCP_TEST_SETTING"] = "from-shell"
        with self.assertLogs("mac_mcp.security", level="WARNING") as logs:
            security.load_env_file(self.env_file)
        self.assertEqual("fresh-key", os.environ["MCP_API_KEY"])
        # Every other setting keeps the usual rule: the environment wins.
        self.assertEqual("from-shell", os.environ["MAC_MCP_TEST_SETTING"])
        self.assertIn("using the .env key", logs.output[0])
        self.assertNotIn("stale-key", logs.output[0])
        self.assertNotIn("fresh-key", logs.output[0])

    def test_matching_or_missing_keys_change_nothing_and_stay_quiet(self) -> None:
        self.env_file.write_text("MCP_API_KEY=same\n", encoding="utf-8")
        os.environ["MCP_API_KEY"] = "same"
        with self.assertNoLogs("mac_mcp.security", level="WARNING"):
            security.load_env_file(self.env_file)
        self.assertEqual("same", os.environ["MCP_API_KEY"])
        # No key in .env: an exported key is still used.
        self.env_file.write_text("MAC_MCP_TEST_SETTING=x\n", encoding="utf-8")
        os.environ["MCP_API_KEY"] = "exported"
        security.load_env_file(self.env_file)
        self.assertEqual("exported", os.environ["MCP_API_KEY"])
        # No .env at all.
        security.load_env_file(Path(self.temp.name) / "missing.env")
        self.assertEqual("exported", os.environ["MCP_API_KEY"])

    def test_server_and_cli_load_through_it(self) -> None:
        root = Path(security.__file__).resolve().parent
        self.assertIn('load_env_file(BASE_DIR / ".env")', (root / "security.py").read_text(encoding="utf-8"))
        cli = (root / "cli.py").read_text(encoding="utf-8")
        load = cli[cli.index("def _load_env() -> None:"):]
        self.assertIn("load_env_file(ENV_FILE)", load[:200])

    def test_update_check_reads_the_key_the_server_will_use(self) -> None:
        os.environ["MCP_API_KEY"] = "stale-key"
        os.environ["MAC_MCP_TEST_SETTING"] = "from-shell"
        env = {"MCP_API_KEY": "fresh-key", "MAC_MCP_TEST_SETTING": "from-file"}
        self.assertEqual("fresh-key", update_helper._effective_env_value(env, "MCP_API_KEY"))
        self.assertEqual("from-shell", update_helper._effective_env_value(env, "MAC_MCP_TEST_SETTING"))
        self.assertEqual("stale-key", update_helper._effective_env_value({}, "MCP_API_KEY"))


if __name__ == "__main__":
    unittest.main()
