import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerServerHelperTests(unittest.TestCase):
    def run_step(self, runtime: Path) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MAC_MCP_", "MCP_"))}
        env.update({"MAC_MCP_INSTALLER_LIBRARY_ONLY": "1", "NO_COLOR": "1"})
        return subprocess.run(["/bin/bash", "-c", f'source "{INSTALLER}"; RUNTIME_DIR="{runtime}"; install_server_helper'],
                              env=env, text=True, capture_output=True, check=False)

    def test_builds_the_helper_into_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            script = runtime / "menu_app" / "ServerHelper" / "build_helper.sh"
            script.parent.mkdir(parents=True)
            script.write_text('mkdir -p "$1" && print built > "$1/marker"\n')
            proc = self.run_step(runtime)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("built\n", (runtime / ".helper" / "marker").read_text())
            self.assertIn("Mac MCP", proc.stdout)

    def test_older_releases_without_a_helper_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = self.run_step(Path(td))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("", proc.stdout)

    def test_a_failed_build_only_warns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            script = runtime / "menu_app" / "ServerHelper" / "build_helper.sh"
            script.parent.mkdir(parents=True)
            script.write_text("exit 3\n")
            proc = self.run_step(runtime)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("Python", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
