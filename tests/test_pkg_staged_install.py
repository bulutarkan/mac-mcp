"""Opt-in: install from a staged package tree into a temp HOME and start it on port 8878.

MAC_MCP_PKG_STAGE=/path/to/staged/home ~/mac-mcp/.venv/bin/python -m unittest tests.test_pkg_staged_install -v
The staged tree comes from: scripts/build_pkg.sh --allow-unverified --stage-only DIR
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

STAGE = os.getenv("MAC_MCP_PKG_STAGE", "")
PORT = 8878


@unittest.skipUnless(STAGE, "set MAC_MCP_PKG_STAGE to a staged package home tree")
class PkgStagedInstallTests(unittest.TestCase):
    def install_into(self, home: Path) -> subprocess.CompletedProcess[str]:
        staging = home / ".mac-mcp/pkg/staging"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(Path(STAGE) / ".mac-mcp/pkg/staging", staging, symlinks=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        return subprocess.run([str(staging / "python/bin/python3"), "-I", str(staging / "pkg_install.py"),
                               "--home", str(home), "--no-start"], env=env, text=True, capture_output=True,
                              timeout=1800, check=False)

    def serve(self, home: Path) -> subprocess.Popen:
        rt = home / "mac-mcp"
        # The server installs and opens the menu app when none is installed; the staged
        # tree carries no app, so keep it from building one into the temp home.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "MAC_MCP_STATE_DIR": str(home / ".mac-mcp"),
               "MAC_MCP_PORT": str(PORT), "MAC_MCP_HOST": "127.0.0.1",
               "MAC_MCP_SKIP_MENU_APP_INSTALL": "1", "MAC_MCP_SKIP_MENU_APP": "1"}
        proc = subprocess.Popen([str(rt / ".venv/bin/python"), "-m", "uvicorn", "mcp_server.main:app",
                                 "--host", "127.0.0.1", "--port", str(PORT)], cwd=str(rt), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(10)))
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        return proc
            except OSError:
                time.sleep(0.5)
        self.fail("server never became healthy")

    def test_fresh_install_then_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-staged-") as td:
            home = Path(td)
            first = self.install_into(home)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertFalse((home / ".mac-mcp/pkg/staging").exists())
            env_before = (home / "mac-mcp/mcp_server/.env").read_bytes()
            check = subprocess.run([str(home / "mac-mcp/.venv/bin/python"), "-m", "pip", "check"],
                                   text=True, capture_output=True)
            self.assertEqual(0, check.returncode, check.stdout)
            proc = self.serve(home)
            proc.terminate(); proc.wait(10)
            second = self.install_into(home)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(env_before, (home / "mac-mcp/mcp_server/.env").read_bytes())
            self.assertTrue(any((home / ".mac-mcp/backups").glob("pkg-*/runtime")))
            self.serve(home)
            log = (home / ".mac-mcp/logs/pkg-install.log").read_text()
            key = [l.split("=", 1)[1] for l in env_before.decode().splitlines() if l.startswith("MCP_API_KEY=")][0]
            self.assertNotIn(key, log)
            self.assertEqual("pkg", (home / ".mac-mcp/install-kind").read_text().strip())
            self.assertFalse((home / "Applications").exists(), "the test server installed a menu app")
