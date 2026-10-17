"""#210: the @bulutarkan/mac-mcp npm package is a thin, script-free wrapper around this repository's
install.sh and the installed `mac-mcp` CLI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "npm"
BIN = PKG / "bin" / "mac-mcp-setup.js"


class PackageManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((PKG / "package.json").read_text(encoding="utf-8"))

    def test_version_follows_the_product(self) -> None:
        product = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines()
                       if line.startswith("version = "))
        self.assertEqual(product, self.manifest["version"])

    def test_nothing_runs_at_install_and_nothing_extra_ships(self) -> None:
        self.assertNotIn("scripts", self.manifest)  # no preinstall/postinstall code
        self.assertNotIn("dependencies", self.manifest)
        self.assertEqual(["bin/mac-mcp-setup.js", "install.sh", "README.md", "LICENSE"], self.manifest["files"])
        self.assertEqual(["darwin"], self.manifest["os"])

    def test_it_never_shadows_the_installed_mac_mcp_command(self) -> None:
        self.assertEqual({"mac-mcp-setup": "bin/mac-mcp-setup.js"}, self.manifest["bin"])
        self.assertEqual("@bulutarkan/mac-mcp", self.manifest["name"])

    def test_the_installer_copy_is_made_at_pack_time_not_committed(self) -> None:
        ignored = (PKG / ".gitignore").read_text().split()
        self.assertIn("install.sh", ignored)
        script = (ROOT / "scripts" / "build_npm_package.sh").read_text()
        self.assertIn('/bin/cp "${ROOT}/install.sh" "${PKG}/install.sh"', script)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WrapperBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-npm-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pkg = self.dir / "pkg"
        shutil.copytree(PKG, self.pkg, ignore=shutil.ignore_patterns("install.sh", "*.tgz", "LICENSE"))
        (self.pkg / "install.sh").write_text('#!/bin/bash\necho "installer cwd=$(pwd) args=$*"\nexit 7\n')
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.pkg / "bin" / "mac-mcp-setup.js"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home)}
        env.pop("MAC_MCP_BIN_DIR", None)
        return subprocess.run(["node", str(self.bin), *args], capture_output=True, text=True, env=env, timeout=30)

    def fake_cli(self, body: str = 'echo "cli $*"') -> Path:
        cli = self.home / ".local" / "bin" / "mac-mcp"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text(f"#!/bin/sh\n{body}\n")
        cli.chmod(0o755)
        return cli

    def test_help_and_version(self) -> None:
        self.assertEqual("2.2.0", self.run_cli("--version").stdout.strip())
        self.assertIn("npx @bulutarkan/mac-mcp install", self.run_cli().stdout)
        self.assertEqual(2, self.run_cli("frobnicate").returncode)

    def test_install_runs_the_bundled_installer_from_home(self) -> None:
        ran = self.run_cli("install", "--example")
        self.assertEqual(7, ran.returncode)
        self.assertIn(f"installer cwd={str(self.home.resolve()).replace('/private', '')}", ran.stdout.replace("/private", ""))
        self.assertIn("args=--example", ran.stdout)

    def test_an_existing_install_is_left_alone(self) -> None:
        self.fake_cli()
        ran = self.run_cli("install")
        self.assertEqual(0, ran.returncode)
        self.assertIn("already installed", ran.stdout)
        self.assertNotIn("installer cwd", ran.stdout)

    def test_other_commands_go_to_the_installed_cli_by_its_full_path(self) -> None:
        self.assertEqual(1, self.run_cli("status").returncode)  # not installed yet
        self.fake_cli()
        ran = self.run_cli("update", "--check")
        self.assertEqual(("cli update --check", 0), (ran.stdout.strip(), ran.returncode))

    def test_a_cli_that_points_back_to_the_package_is_refused(self) -> None:
        cli = self.home / ".local" / "bin" / "mac-mcp"
        cli.parent.mkdir(parents=True)
        cli.symlink_to(self.bin)
        ran = self.run_cli("status")
        self.assertEqual(1, ran.returncode)
        self.assertIn("refusing to recurse", ran.stderr)


if __name__ == "__main__":
    unittest.main()
