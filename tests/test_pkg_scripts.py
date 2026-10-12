from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "packaging/pkg/scripts"


def run_script(name: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "MAC_MCP_PKG_HOME": str(home)}
    return subprocess.run(["/bin/bash", str(SCRIPTS / name), "/tmp/x.pkg", str(home), "/"],
                          env=env, text=True, capture_output=True, check=False)


class PkgScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-scripts-")
        self.home = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_preinstall_allows_a_fresh_home(self) -> None:
        proc = run_script("preinstall", self.home)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_preinstall_refuses_a_source_install(self) -> None:
        (self.home / "mac-mcp").mkdir()
        proc = run_script("preinstall", self.home)
        self.assertEqual(1, proc.returncode)
        self.assertIn("mac-mcp update", proc.stderr)
        self.assertTrue((self.home / "mac-mcp").is_dir())

    def test_preinstall_allows_a_package_upgrade(self) -> None:
        (self.home / "mac-mcp").mkdir()
        (self.home / ".mac-mcp").mkdir()
        (self.home / ".mac-mcp/install-kind").write_text("pkg\n")
        self.assertEqual(0, run_script("preinstall", self.home).returncode)

    def test_postinstall_runs_the_bundled_python_isolated(self) -> None:
        staging = self.home / ".mac-mcp/pkg/staging"
        (staging / "python/bin").mkdir(parents=True)
        (staging / "python/bin/python3").symlink_to(sys.executable)
        record = self.home / "argv.txt"
        (staging / "pkg_install.py").write_text(
            "import sys\n"
            f"open({str(record)!r}, 'w').write(repr((sys.flags.isolated, sys.argv[1:])))\n"
        )
        proc = run_script("postinstall", self.home)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(repr((1, ["--home", str(self.home)])), record.read_text())

    def test_home_resolution_without_override_uses_directory_services(self) -> None:
        text = (SCRIPTS / "preinstall").read_text()
        self.assertIn("dscl . -read", text)
        self.assertIn("NFSHomeDirectory", text)
        self.assertNotIn("sudo", text + (SCRIPTS / "postinstall").read_text())

    def test_distribution_is_home_only_arm64_macos14(self) -> None:
        root = ET.fromstring((ROOT / "packaging/pkg/distribution.xml").read_text())
        domains = root.find("domains")
        self.assertEqual("true", domains.get("enable_currentUserHome"))
        self.assertEqual("false", domains.get("enable_localSystem"))
        self.assertEqual("false", domains.get("enable_anywhere"))
        self.assertEqual("arm64", root.find("options").get("hostArchitectures"))
        self.assertEqual("14.0", root.find("allowed-os-versions/os-version").get("min"))


if __name__ == "__main__":
    unittest.main()
