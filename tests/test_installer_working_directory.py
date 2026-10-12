from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"

_COMPANION_STUB = """
import os
from pathlib import Path

def ensure_chrome_companion_config():
    root = Path(__file__).resolve().parent.parent
    token = Path(os.environ["MAC_MCP_STATE_DIR"]) / "chrome-companion-token"
    token.write_text("t\\n")
    path = root / "menu_app" / "ChromeVisualCompanion" / "bridge_config.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// from " + str(root) + "\\n")
    return path
"""

_CLI_STUB = """
from pathlib import Path

def ensure_cli_launcher(runtime, cli_path, strict=False):
    marker = Path(__file__).resolve().parent.parent / "cli-written-by-this-copy"
    marker.write_text("x")
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text("#!/bin/sh\\nexit 0\\n")
    cli_path.chmod(0o755)
    return cli_path
"""


class InstallerWorkingDirectoryTests(unittest.TestCase):
    """install.sh run from inside another Mac MCP checkout must use the runtime's code only."""

    def make_copy(self, root: Path) -> Path:
        (root / "mcp_server").mkdir(parents=True)
        (root / "mcp_server" / "__init__.py").write_text("")
        (root / "mcp_server" / "chrome_background_bridge.py").write_text(_COMPANION_STUB)
        (root / "mcp_server" / "cli_bootstrap.py").write_text(_CLI_STUB)
        return root

    def test_python_steps_import_the_runtime_not_the_callers_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-installer-cwd-") as td:
            base = Path(td)
            decoy = self.make_copy(base / "checkout")
            runtime = self.make_copy(base / "runtime")
            (runtime / ".venv" / "bin").mkdir(parents=True)
            (runtime / ".venv" / "bin" / "python").symlink_to(sys.executable)
            state = base / "state"
            (state / "update").mkdir(parents=True)
            home = base / "home"
            home.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MAC_MCP_", "MCP_"))}
            env.update({"MAC_MCP_INSTALLER_LIBRARY_ONLY": "1", "HOME": str(home), "NO_COLOR": "1"})
            body = (
                f'source "{INSTALLER}"; RUNTIME_DIR="{runtime}"; STATE_DIR="{state}"; '
                f'BIN_DIR="{base}/bin"; CLI_PATH="{base}/bin/mac-mcp"; INSTALLED_COMMIT=abc; '
                "prepare_chrome_companion && install_update_state_and_cli"
            )
            proc = subprocess.run(
                ["/bin/bash", "-c", body], cwd=str(decoy), env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((runtime / "menu_app/ChromeVisualCompanion/bridge_config.js").is_file())
            self.assertTrue((runtime / "cli-written-by-this-copy").is_file())
            self.assertFalse((decoy / "menu_app").exists(), "installer wrote into the caller's checkout")
            self.assertFalse((decoy / "cli-written-by-this-copy").exists(), "installer imported the caller's checkout")


if __name__ == "__main__":
    unittest.main()
