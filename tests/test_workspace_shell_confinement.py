from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.policy_scope import ResourceScope
from mcp_server.security import load_settings
from mcp_server.tools_terminal import run_command


class WorkspaceShellConfinementTests(unittest.TestCase):
    def _context(self, root: Path):
        ws = root / "workspace"
        ws.mkdir()
        os.environ["MAC_MCP_SCOPED_SHELL_STATE_DIR"] = str(root / "state")
        settings = replace(load_settings(), workdir=ws)
        context = PolicyContext(
            profile="developer",
            actor="agent:test",
            agent_id="agt_test",
            scope=ResourceScope(
                path_roots=(str(ws),),
                tool_families=("terminal", "jobs", "files", "http"),
                access_mode="workspace_write",
            ),
        )
        return ws, settings, context

    def test_scoped_shell_allows_workspace_and_denies_outside_read_write_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            ws, settings, context = self._context(root)
            outside = root / "outside.txt"
            outside.write_text("secret")
            (ws / "inside.txt").write_text("inside")
            (ws / "escape-link").symlink_to(outside)
            token = set_policy_context(context)
            try:
                inside = run_command(settings, "cat inside.txt")
                outside_read = run_command(settings, f"cat {outside}")
                symlink_read = run_command(settings, "cat escape-link")
                inside_write = run_command(settings, "echo ok > made.txt && cat made.txt")
                outside_target = root / "outside-write.txt"
                outside_write = run_command(settings, f"echo bad > {outside_target}")
            finally:
                reset_policy_context(token)
            self.assertTrue(inside["ok"])
            self.assertEqual("inside", inside["stdout"])
            self.assertTrue(inside["sandboxed"])
            self.assertFalse(outside_read["ok"])
            self.assertFalse(symlink_read["ok"])
            self.assertTrue(inside_write["ok"])
            self.assertFalse(outside_write["ok"])
            self.assertFalse(outside_target.exists())

    def test_scoped_shell_does_not_inherit_parent_secret_env_and_keeps_dev_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            _ws, settings, context = self._context(root)
            os.environ["MAC_MCP_FAKE_PARENT_SECRET"] = "should-not-leak"
            token = set_policy_context(context)
            try:
                env = run_command(settings, "printenv MAC_MCP_FAKE_PARENT_SECRET || true")
                python = run_command(settings, 'python3 -c "print(123)"')
                node = run_command(settings, 'node -e "console.log(456)"')
                git = run_command(settings, "git --version")
            finally:
                reset_policy_context(token)
            self.assertEqual("", env["stdout"].strip())
            self.assertTrue(python["ok"], python)
            self.assertEqual("123", python["stdout"].strip())
            self.assertTrue(node["ok"], node)
            self.assertEqual("456", node["stdout"].strip())
            self.assertTrue(git["ok"], git)

    def test_unrestricted_shell_keeps_historical_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            settings = replace(load_settings(), workdir=root)
            result = run_command(settings, "pwd")
            self.assertTrue(result["ok"])
            self.assertFalse(result["sandboxed"])


if __name__ == "__main__":
    unittest.main()
