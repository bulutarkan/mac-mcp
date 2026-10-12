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
