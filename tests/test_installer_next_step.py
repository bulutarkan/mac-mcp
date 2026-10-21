"""#66: the installer's last line names the one next step that fits how the start went.

The completion screen always said "Next command: mac-mcp start", also when the server had just started
(nothing left to start) or had failed to start (where `mac-mcp doctor` says why).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerNextStepTests(unittest.TestCase):
    def run_start(self, answer: str, cli_exit: int) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "mac-mcp"
            cli.write_text(f"#!/bin/sh\nexit {cli_exit}\n")
            cli.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MAC_MCP_", "MCP_"))}
            env.update({"MAC_MCP_INSTALLER_LIBRARY_ONLY": "1", "NO_COLOR": "1", "MAC_MCP_START_NOW": answer})
            return subprocess.run(
                ["/bin/bash", "-c", f'source "{INSTALLER}"; CLI_PATH="{cli}"; optionally_start_server; next_step_line'],
                env=env, text=True, capture_output=True, check=False,
            )

    def test_a_started_server_points_to_connecting_an_app(self) -> None:
        proc = self.run_start("yes", 0)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Next step: add Mac MCP to your AI app: mac-mcp clients", proc.stdout)
        self.assertNotIn("Next command: mac-mcp start", proc.stdout)

    def test_a_failed_start_points_to_doctor(self) -> None:
        proc = self.run_start("yes", 1)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Next command: mac-mcp doctor", proc.stdout + proc.stderr)
        self.assertNotIn("Next command: mac-mcp start", proc.stdout)

    def test_a_skipped_start_points_to_start(self) -> None:
        proc = self.run_start("no", 0)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("Next command: mac-mcp start", proc.stdout)

    def test_the_summary_prints_the_step(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        summary = text[text.index("completion_runtime_summary() {"):]
        summary = summary[:summary.index("\nmain() {")]
        self.assertIn("next_step_line", summary)
        self.assertNotIn("Next command:%s mac-mcp start", summary)


if __name__ == "__main__":
    unittest.main()
