"""#117: shell commands and jobs never inherit the server's credentials; osascript gets its script on stdin.

The credentials below are synthetic. Output is checked with assertTrue/assertFalse so a failure never
prints the child's environment.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
from mcp_server import process_supervision, tools_jobs, tools_macos
from mcp_server.security import SERVER_ONLY_ENV_KEYS, child_process_env, load_settings
from mcp_server.tools_terminal import run_command

SYNTHETIC = {
    "MCP_API_KEY": "synthetic-mcp-key-117-Zq9vX2kL7mN4pR8sT1uW",
    "MAC_MCP_CHROME_BRIDGE_TOKEN": "synthetic-bridge-117-Hx3Lq8Zv2Nw7Rt5K",
    "MAC_MCP_DECISIONS_OPENAI_API_KEY": "synthetic-decisions-117-p9Ym4Bc6Df1G",
}
USER_KEY = ("MY_OWN_SERVICE_API_KEY", "user-exported-117-value")


class ChildEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-117-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {**SYNTHETIC, USER_KEY[0]: USER_KEY[1]})
        self.env.start()
        self.jobs = patch.object(tools_jobs, "JOBS_DIR", self.root / "jobs")
        self.jobs.start()
        self.settings = replace(load_settings(), workdir=self.root / "work", allow_shell=True)

    def tearDown(self) -> None:
        self.jobs.stop()
        self.env.stop()
        self.temp.cleanup()

    def assert_clean(self, output: str) -> None:
        for name, value in SYNTHETIC.items():
            self.assertFalse(value in output, f"{name} reached the child")
        self.assertTrue(f"{USER_KEY[0]}={USER_KEY[1]}" in output, "the user's own variables must stay")
        self.assertTrue("HOME=" in output and "PATH=" in output)

    def assert_clean_names(self, output: str) -> None:
        """Job output is stored with secret values masked (#114), so jobs print only the variable names."""
        names = set(output.split())
        for name in SYNTHETIC:
            self.assertNotIn(name, names, f"{name} reached the child")
        self.assertIn(USER_KEY[0], names, "the user's own variables must stay")
        self.assertTrue({"HOME", "PATH"} <= names)

    def test_strip_list_is_narrow(self) -> None:
        self.assertIn("MCP_API_KEY", SERVER_ONLY_ENV_KEYS)
        self.assertTrue(all(name == "MCP_API_KEY" or name.startswith("MAC_MCP_") for name in SERVER_ONLY_ENV_KEYS))
        env = child_process_env()
        self.assertFalse(any(name in env for name in SYNTHETIC))
        self.assertEqual(USER_KEY[1], env.get(USER_KEY[0]))
        self.assertEqual(SYNTHETIC["MCP_API_KEY"], os.environ.get("MCP_API_KEY"))  # the server keeps its own

    def test_run_command_env_has_no_server_key(self) -> None:
        result = run_command(self.settings, "env")
        self.assertEqual(0, result.get("exit_code"))
        self.assert_clean(result["stdout"])

    def test_background_job_env_has_no_server_key(self) -> None:
        job = tools_jobs.start_background_job(self.settings, "env | cut -d= -f1", cwd=str(self.root / "work"))
        tools_jobs.wait_jobs(self.settings, [job["job_id"]], timeout_s=30)
        output = tools_jobs.get_job_output(self.settings, job["job_id"])
        self.assert_clean_names(str(output.get("stdout") or ""))

    def test_parallel_commands_env_has_no_server_key(self) -> None:
        result = tools_jobs.run_commands_parallel(self.settings, ["env | cut -d= -f1"], cwd=str(self.root / "work"),
                                                  timeout_s=30)
        job_id = result["started"][0]["job_id"]
        self.assert_clean_names(str(tools_jobs.get_job_output(self.settings, job_id).get("stdout") or ""))

    def test_developer_tools_still_run(self) -> None:
        commands = ["git --version", "python3 -c 'print(117)'"]
        if shutil.which("brew") or Path("/opt/homebrew/bin/brew").exists():
            commands.append("brew --version")
        for command in commands:
            with self.subTest(command=command.split()[0]):
                result = run_command(self.settings, command, timeout_s=60)
                self.assertEqual(0, result.get("exit_code"), result.get("stderr"))
                self.assertTrue(result["stdout"].strip())


class _FakeOsascript:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = 0
        self.stdin_text = None

    def communicate(self, input=None, timeout=None):
        self.stdin_text = input
        return "done\n", ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class OsascriptArgvTests(unittest.TestCase):
    SCRIPT_MARKER = "secret-script-body-117"

    def _run(self, call):
        process = _FakeOsascript()
        with patch.object(process_supervision.subprocess, "Popen", return_value=process) as popen:
            result = call()
        return popen.call_args.args[0], process.stdin_text, result

    def test_run_applescript_script_is_not_in_argv(self) -> None:
        script = f'do shell script "echo {self.SCRIPT_MARKER}"'
        argv, stdin, result = self._run(lambda: tools_macos.run_applescript(
            replace(load_settings(), allow_shell=True), script))
        self.assertTrue(result["ok"])
        self.assertEqual(["osascript", "-"], argv)
        self.assertFalse(any(self.SCRIPT_MARKER in part for part in argv))
        self.assertEqual(script, stdin)

    def test_argument_bound_helpers_keep_the_script_out_of_argv(self) -> None:
        argv, stdin, _ = self._run(lambda: tools_macos.send_notification(None, "Title", "Body", "Pop"))
        self.assertEqual(["osascript", "-", "Title", "Body", "Pop"], argv)
        self.assertIn("display notification", stdin)
        self.assertFalse(any("display notification" in part for part in argv))
        argv, stdin, _ = self._run(lambda: tools_macos.set_reminder(None, "Call", "notes", "2026-10-26 09:30"))
        self.assertEqual(["osascript", "-", "Call", "notes"], argv)
        self.assertIn("set year of d to 2026", stdin)

    def test_real_osascript_reads_arguments_after_stdin_script(self) -> None:
        tricky = ["--", "-e", 'x" & (do shell script "echo pwned") & "', ""]
        result = process_supervision.run_osascript(
            'on run argv\nset out to (count of argv) as text\nrepeat with a in argv\n'
            'set out to out & "|" & a\nend repeat\nreturn out\nend run', 10, args=tricky)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual("4|" + "|".join(tricky), result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
