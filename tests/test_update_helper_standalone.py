"""The updater runs as a staged standalone script (tools_update copies it with a few siblings)."""
from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "mcp_server" / "update_helper.py"
STAGED_SIBLINGS = ("update_state.py", "release_trust.py", "managed_process.py")


class StandaloneUpdaterTests(unittest.TestCase):
    def test_relative_imports_only_inside_the_package_branch(self) -> None:
        # Anything else fails when the staged script runs (r7's first update did).
        tree = ast.parse(HELPER.read_text(encoding="utf-8"))
        package_branch = next(stmt for stmt in tree.body if isinstance(stmt, ast.If)
                              and isinstance(stmt.test, ast.Name) and stmt.test.id == "__package__")
        allowed = {id(node) for stmt in package_branch.body for node in ast.walk(stmt)}
        stray = [node.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.level > 0 and id(node) not in allowed]
        self.assertEqual([], stray)

    def test_server_helper_step_runs_from_the_staged_copy(self) -> None:
        stage = Path(tempfile.mkdtemp(prefix="mac-mcp-staged-updater-"))
        self.addCleanup(shutil.rmtree, stage, True)
        shutil.copy2(HELPER, stage / "update_helper.py")
        for name in STAGED_SIBLINGS:
            shutil.copy2(HELPER.with_name(name), stage / name)
        spec = importlib.util.spec_from_file_location("staged_update_helper", stage / "update_helper.py")
        module = importlib.util.module_from_spec(spec)
        saved_path, saved_modules = list(sys.path), dict(sys.modules)
        sys.modules[spec.name] = module  # dataclasses look the module up by name
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = saved_path
            for name in set(sys.modules) - set(saved_modules):
                del sys.modules[name]

        runtime = stage / "runtime"
        script = runtime / "menu_app" / "ServerHelper" / "build_helper.sh"
        script.parent.mkdir(parents=True)
        marker = stage / "built"
        script.write_text(f'echo "$1" > {marker}\n', encoding="utf-8")
        out = StringIO()
        with redirect_stdout(out):
            module._ensure_server_helper(runtime)
        self.assertEqual(str(runtime / ".helper"), marker.read_text(encoding="utf-8").strip())
        self.assertIn("could not be built", out.getvalue())  # the fake build makes no app


if __name__ == "__main__":
    unittest.main()


class HelperRestartTests(unittest.TestCase):
    """After an update the server starts again under Mac MCP Server, keeping its permissions."""

    def setUp(self) -> None:
        from mcp_server import update_helper

        self.module = update_helper
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-helper-restart-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.runtime = self.root / "runtime"
        self.state = self.root / "state"
        self.state.mkdir()
        self.helper = self.runtime / ".helper" / "Mac MCP Server.app" / "Contents" / "MacOS" / "mac-mcp-server"
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text("#!/bin/sh\n")
        self.helper.chmod(0o755)
        self.calls: list[tuple] = []

    def write_job(self, program: Path) -> None:
        import plistlib

        with (self.state / "server.plist").open("wb") as handle:
            plistlib.dump({"Label": "com.macmcp.server", "ProgramArguments": [str(program), "python"]}, handle)

    def start(self, parent: str):
        from unittest.mock import patch

        def launchctl(*args):
            self.calls.append(args)
            import subprocess
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(self.module, "_launchctl_quiet", side_effect=launchctl), \
                patch.object(self.module, "listener_pids", return_value=[4242]), \
                patch.object(self.module, "process_snapshot", return_value=object()), \
                patch.object(self.module, "matches_role", return_value=True), \
                patch.object(self.module, "_parent_executable", return_value=parent):
            return self.module._start_under_server_helper(self.runtime, self.state, 8765)

    def test_no_job_file_means_the_old_direct_start(self) -> None:
        self.assertIsNone(self.start(str(self.helper)))
        self.assertEqual([], self.calls)

    def test_a_job_for_another_program_is_left_alone(self) -> None:
        self.write_job(self.root / "elsewhere")
        self.assertIsNone(self.start(str(self.helper)))

    def test_the_job_is_loaded_again_and_its_child_recorded(self) -> None:
        self.write_job(self.helper)
        pid, _ = self.start(str(self.helper))
        self.assertEqual(4242, pid)
        self.assertEqual("bootout", self.calls[0][0])
        self.assertEqual(("bootstrap", f"gui/{__import__('os').getuid()}", str(self.state / "server.plist")), self.calls[1])
