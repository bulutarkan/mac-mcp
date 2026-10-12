"""The frozen "Mac MCP Server" launcher: permissions are granted to it, not to Python."""
from __future__ import annotations

import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER_SRC = ROOT / "menu_app" / "ServerHelper"
BUILD = HELPER_SRC / "build_helper.sh"


def cdhash(bundle: Path) -> str:
    out = subprocess.run(["codesign", "-dvvv", str(bundle)], capture_output=True, text=True).stderr
    return next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("CDHash="))


class ServerHelperBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-helper-build-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.out = self.root / "helper"
        self.bundle = self.out / "Mac MCP Server.app"

    def build(self, *, src: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/usr/bin:/bin"}
        if src is not None:
            env["MAC_MCP_HELPER_SRC"] = str(src)
        return subprocess.run(["/bin/zsh", str(BUILD), str(self.out)], capture_output=True, text=True,
                              env=env, timeout=300)

    def test_builds_a_signed_bundle_named_mac_mcp(self) -> None:
        proc = self.build()
        self.assertEqual(0, proc.returncode, proc.stderr)
        subprocess.run(["codesign", "--verify", "--strict", str(self.bundle)], check=True)
        info = plistlib.loads((self.bundle / "Contents" / "Info.plist").read_bytes())
        self.assertEqual("Mac MCP", info["CFBundleName"])
        self.assertEqual("Mac MCP", info["CFBundleDisplayName"])
        self.assertEqual("com.bulutarkan.mac-mcp.server", info["CFBundleIdentifier"])
        self.assertTrue(info["LSUIElement"])
        self.assertTrue((self.bundle / "Contents" / "Resources" / "AppIcon.icns").is_file())

    def test_same_sources_do_not_rebuild_so_grants_survive(self) -> None:
        self.assertEqual(0, self.build().returncode)
        first = cdhash(self.bundle)
        stamp = (self.bundle / "Contents" / "MacOS" / "mac-mcp-server").stat().st_mtime_ns
        second = self.build()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("up to date", second.stdout)
        self.assertEqual(stamp, (self.bundle / "Contents" / "MacOS" / "mac-mcp-server").stat().st_mtime_ns)
        self.assertEqual(first, cdhash(self.bundle))

    def test_changed_sources_rebuild(self) -> None:
        src = self.root / "src"
        shutil.copytree(HELPER_SRC, src)
        self.assertEqual(0, self.build(src=src).returncode)
        first = cdhash(self.bundle)
        # A comment alone leaves the binary (and the grant) unchanged; change real code.
        (src / "launcher.c").write_text((src / "launcher.c").read_text().replace("usage: %s", "Usage: %s"))
        proc = self.build(src=src)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("up to date", proc.stdout)
        self.assertNotEqual(first, cdhash(self.bundle))

    def test_launcher_returns_the_child_status_and_forwards_sigterm(self) -> None:
        self.assertEqual(0, self.build().returncode)
        exe = self.bundle / "Contents" / "MacOS" / "mac-mcp-server"
        self.assertEqual(7, subprocess.run([str(exe), "/bin/sh", "-c", "exit 7"]).returncode)
        proc = subprocess.Popen([str(exe), "/bin/sleep", "30"])
        time.sleep(0.5)
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(128 + signal.SIGTERM, proc.wait(timeout=10))
        self.assertLess(time.time() - started, 5)
        self.assertEqual(127, subprocess.run([str(exe), "/nonexistent/binary"], capture_output=True).returncode)


if __name__ == "__main__":
    unittest.main()
