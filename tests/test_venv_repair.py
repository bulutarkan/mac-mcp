from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import cli_bootstrap, venv_repair


def make_venv(venv: Path, *, python_target: str | None = sys.executable, home: str = "/opt/homebrew/opt/python@3.14/bin") -> None:
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.14.7\n", encoding="utf-8")
    if python_target is not None:
        os.symlink(python_target, venv / "bin" / "python")
    script = venv / "bin" / "mac-mcp"
    script.write_text(f"#!{venv}/bin/python\nprint('mac-mcp')\n", encoding="utf-8")
    script.chmod(0o755)


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-venv-"))
        self.addCleanup(shutil.rmtree, self.runtime, True)

    def test_states(self) -> None:
        self.assertEqual("missing", venv_repair.inspect_venv(self.runtime)["status"])
        make_venv(self.runtime / ".venv", python_target=str(self.runtime / "gone" / "python3.14"))
        self.assertEqual("base_missing", venv_repair.inspect_venv(self.runtime)["status"])

    def test_a_working_interpreter_is_ok_and_a_cellar_home_is_flagged(self) -> None:
        make_venv(self.runtime / ".venv", home="/opt/homebrew/Cellar/python@3.14/3.14.3_1/bin")
        report = venv_repair.inspect_venv(self.runtime)
        self.assertEqual("ok", report["status"])
        self.assertTrue(report["pinned_to_versioned_path"])
        self.assertEqual(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", report["version"])

    def test_an_interpreter_that_cannot_run_is_broken(self) -> None:
        venv = self.runtime / ".venv"
        make_venv(venv, python_target=None)
        fake = venv / "bin" / "python"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        self.assertEqual("broken", venv_repair.inspect_venv(self.runtime)["status"])


class RepairTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-venv-"))
        self.addCleanup(shutil.rmtree, self.runtime, True)
        make_venv(self.runtime / ".venv", python_target=str(self.runtime / "gone" / "python3.14"))
        (self.runtime / ".venv" / "OLD").write_text("old", encoding="utf-8")

    def fake_run(self, fail_on: str | None = None):
        def run(cmd, **_kwargs):
            joined = " ".join(cmd)
            if fail_on and fail_on in joined:
                raise RuntimeError(f"failed: {fail_on}")
            if cmd[1:3] == ["-m", "venv"]:
                make_venv(Path(cmd[3]))
        return run

    def repair(self, *, run, verify=None):
        with patch.object(venv_repair, "choose_interpreter", return_value=(Path(sys.executable), {"version": [3, 14, 7]})), \
             patch.object(venv_repair, "_run", side_effect=run), \
             patch.object(venv_repair, "_verify", side_effect=verify or (lambda venv, runtime: None)):
            return venv_repair.repair(self.runtime)

    def test_successful_repair_swaps_in_the_new_venv_and_keeps_the_old_one(self) -> None:
        self.assertEqual(0, self.repair(run=self.fake_run()))
        venv = self.runtime / ".venv"
        self.assertFalse((venv / "OLD").exists())
        previous = list(self.runtime.glob(".venv.previous-*"))
        self.assertEqual(1, len(previous))
        self.assertTrue((previous[0] / "OLD").exists())
        self.assertEqual(f"#!{venv}/bin/python", (venv / "bin" / "mac-mcp").read_text().splitlines()[0],
                         "scripts built in the staging folder point at the final venv")
        self.assertFalse(list(self.runtime.glob(".venv.repair-*")))

    def test_a_staging_failure_never_touches_the_active_venv(self) -> None:
        self.assertEqual(1, self.repair(run=self.fake_run(fail_on="pip install")))
        self.assertTrue((self.runtime / ".venv" / "OLD").exists())
        self.assertFalse(list(self.runtime.glob(".venv.repair-*")))
        self.assertFalse(list(self.runtime.glob(".venv.previous-*")))

    def test_a_failed_check_after_the_swap_restores_the_previous_venv(self) -> None:
        calls = []

        def verify(venv, runtime):
            calls.append(venv)
            if venv.name == ".venv":
                raise RuntimeError("post-swap import failed")

        self.assertEqual(1, self.repair(run=self.fake_run(), verify=verify))
        self.assertTrue((self.runtime / ".venv" / "OLD").exists(), "the previous venv is back")
        self.assertEqual(1, len(list(self.runtime.glob(".venv.failed-*"))))
        self.assertEqual(2, len(calls))

    def test_a_healthy_venv_is_left_alone_unless_forced(self) -> None:
        shutil.rmtree(self.runtime / ".venv")
        make_venv(self.runtime / ".venv")
        with patch.object(venv_repair, "_run") as run:
            self.assertEqual(0, venv_repair.repair(self.runtime))
        run.assert_not_called()


class LauncherTests(unittest.TestCase):
    def test_launcher_explains_a_missing_runtime_python(self) -> None:
        runtime = Path(tempfile.mkdtemp(prefix="mac-mcp-launcher-"))
        self.addCleanup(shutil.rmtree, runtime, True)
        make_venv(runtime / ".venv", python_target=str(runtime / "gone" / "python3.14"))
        launcher = runtime / "mac-mcp-shim"
        launcher.write_text(cli_bootstrap.launcher_text(runtime), encoding="utf-8")
        launcher.chmod(0o755)
        proc = subprocess.run([str(launcher), "status"], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "MAC_MCP_RUNTIME_DIR": str(runtime)})
        self.assertEqual(70, proc.returncode)
        self.assertIn("venv_repair.py repair", proc.stderr)


class DoctorIdTests(unittest.TestCase):
    def test_every_doctor_check_has_a_unique_id(self) -> None:
        from mcp_server import diagnostics

        with patch.object(diagnostics.subprocess, "run", side_effect=OSError("no subprocesses in this test")):
            ids = [check.check_id for check in diagnostics.doctor_checks()]
        duplicates = sorted({check_id for check_id in ids if ids.count(check_id) > 1})
        self.assertEqual([], duplicates)
        self.assertIn("runtime.venv", ids)


if __name__ == "__main__":
    unittest.main()
