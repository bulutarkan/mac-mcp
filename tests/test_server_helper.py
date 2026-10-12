from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mcp_server import server_helper


class ServerJobTests(unittest.TestCase):
    def test_job_runs_the_server_under_the_helper(self) -> None:
        helper = Path("/x/.helper/Mac MCP Server.app/Contents/MacOS/mac-mcp-server")
        payload = server_helper.server_job_payload(
            helper=helper, command=["/py", "-m", "uvicorn", "mcp_server.main:app", "--port", "8765"],
            env={"PATH": "/usr/bin", "MAC_MCP_PORT": "8765"}, cwd=Path("/x"), log=Path("/s/mac-mcp.log"),
        )
        self.assertEqual("com.macmcp.server", payload["Label"])
        self.assertEqual([str(helper), "/py", "-m", "uvicorn", "mcp_server.main:app", "--port", "8765"],
                         payload["ProgramArguments"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertFalse(payload["KeepAlive"])
        self.assertEqual("/s/mac-mcp.log", payload["StandardOutPath"])
        self.assertEqual("/x", payload["WorkingDirectory"])

    def test_secrets_never_reach_the_job_file(self) -> None:
        env = server_helper.job_environment({
            "PATH": "/usr/bin", "HOME": "/Users/o", "LANG": "tr_TR.UTF-8",
            "MAC_MCP_PORT": "8765", "MAC_MCP_HOST": "0.0.0.0", "MCP_ALLOW_SHELL": "true",
            "MCP_API_KEY": "secret", "MAC_MCP_API_KEY": "secret", "MAC_MCP_CHROME_BRIDGE_TOKEN": "t",
            "GROQ_API_KEY": "g", "OPENAI_API_KEY": "o", "AWS_SECRET_ACCESS_KEY": "a", "RANDOM": "x",
        })
        self.assertEqual({"PATH": "/usr/bin", "HOME": "/Users/o", "LANG": "tr_TR.UTF-8",
                          "MAC_MCP_PORT": "8765", "MAC_MCP_HOST": "0.0.0.0", "MCP_ALLOW_SHELL": "true"}, env)

    def test_the_server_pid_is_the_helpers_child_that_listens(self) -> None:
        parents = {101: 1, 202: 50, 303: 50}
        pid = server_helper.server_child_pid(
            50, 8765, listeners=lambda port: [101, 202], parent_of=parents.get,
        )
        self.assertEqual(202, pid)
        self.assertIsNone(server_helper.server_child_pid(
            50, 8765, listeners=lambda port: [101], parent_of=parents.get,
        ))


class EnsureHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-ensure-helper-"))
        self.addCleanup(shutil.rmtree, self.runtime, True)

    def test_missing_helper_and_no_build_script_means_none(self) -> None:
        self.assertIsNone(server_helper.helper_executable(self.runtime))
        self.assertIsNone(server_helper.ensure_helper(self.runtime))

    def test_ensure_builds_from_the_runtime_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        (self.runtime / "menu_app").mkdir()
        shutil.copytree(root / "menu_app" / "ServerHelper", self.runtime / "menu_app" / "ServerHelper")
        shutil.copytree(root / "menu_app" / "Assets", self.runtime / "menu_app" / "Assets")
        exe = server_helper.ensure_helper(self.runtime)
        self.assertEqual(self.runtime / ".helper" / "Mac MCP Server.app" / "Contents" / "MacOS" / "mac-mcp-server", exe)
        self.assertEqual(7, subprocess.run([str(exe), "/bin/sh", "-c", "exit 7"]).returncode)

    def test_an_unsigned_or_broken_bundle_is_not_used(self) -> None:
        exe = self.runtime / ".helper" / "Mac MCP Server.app" / "Contents" / "MacOS" / "mac-mcp-server"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        self.assertIsNone(server_helper.helper_executable(self.runtime))


if __name__ == "__main__":
    unittest.main()
