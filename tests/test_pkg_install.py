from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pkg_install", ROOT / "packaging/pkg/pkg_install.py")
pkg = importlib.util.module_from_spec(_spec)
sys.modules["pkg_install"] = pkg  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(pkg)


def make_source_tar(path: Path, files: dict[str, str]) -> str:
    with tarfile.open(path, "w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


BASE_FILES = {
    "mcp_server/.env.example": "MCP_API_KEY=\nMCP_ALLOW_NO_AUTH=true\n# comment\nOTHER=1\n",
    "requirements.lock": "fastapi==1 \\\n    --hash=sha256:00\n",
    "menu_app/ChromeVisualCompanion/manifest.json": "{}\n",
    "VERSION_MARK": "A\n",
}


def make_staging(home: Path, files: dict[str, str] | None = None, *, release_id: str = "stable-test-r1") -> Path:
    staging = home / ".mac-mcp" / "pkg" / "staging"
    (staging / "python" / "bin").mkdir(parents=True)
    (staging / "python" / "bin" / "python3").write_text("#!/bin/sh\n")
    (staging / "python" / "lib").mkdir()
    (staging / "wheelhouse").mkdir()
    (staging / "wheelhouse" / "fastapi-1-py3-none-any.whl").write_text("w")
    sha = make_source_tar(staging / "source.tar", files or BASE_FILES)
    (staging / "release.json").write_text(json.dumps({
        "release_id": release_id, "version": "2.1.9", "commit": "c" * 40, "source_sha256": sha,
        "payload_sha256": "d" * 64, "python_version": "3.14.7", "arch": "arm64",
    }))
    return staging


class FakeRun:
    """Records commands and imitates the side effects install() relies on."""

    def __init__(self, layout, fail_on: str | None = None):
        self.layout = layout
        self.fail_on = fail_on
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, env, cwd=None, check=True):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if self.fail_on and self.fail_on in joined:
            if check:
                raise pkg.InstallError(f"failed: {self.fail_on}")
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if "-m venv" in joined:
            (self.layout.runtime / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            (self.layout.runtime / ".venv" / "bin" / "python").write_text("")
        if "ensure_chrome_companion_config" in joined:
            (self.layout.state / "chrome-companion-token").write_text("tok\n")
            (self.layout.runtime / "menu_app/ChromeVisualCompanion/bridge_config.js").write_text("cfg\n")
        if "ensure_cli_launcher" in joined:
            self.layout.cli.parent.mkdir(parents=True, exist_ok=True)
            self.layout.cli.write_text("#!/bin/sh\n")
            self.layout.cli.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


class PkgFreshInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-install-")
        self.home = Path(self.td.name)
        self.layout = pkg.Layout(self.home)
        self.health_ports: list[int] = []

    def tearDown(self) -> None:
        self.td.cleanup()

    def health(self, port: int) -> bool:
        self.health_ports.append(port)
        return True

    def test_fresh_install_lays_out_runtime_like_install_sh(self) -> None:
        make_staging(self.home)
        run = FakeRun(self.layout)
        result = pkg.install(self.layout, run=run, health=self.health)
        rt = self.layout.runtime
        self.assertEqual("fresh", result["mode"])
        self.assertEqual("A\n", (rt / "VERSION_MARK").read_text())
        self.assertTrue((rt / ".python" / "bin" / "python3").is_file())
        env_text = (rt / "mcp_server/.env").read_text()
        key = [l.split("=", 1)[1] for l in env_text.splitlines() if l.startswith("MCP_API_KEY=")][0]
        self.assertEqual(64, len(key))
        self.assertIn("MCP_ALLOW_NO_AUTH=false", env_text)
        self.assertIn("MCP_ALLOW_SHELL=true", env_text)
        self.assertIn("MAC_MCP_PERMISSION_PROFILE=standard", env_text)
        self.assertIn("OTHER=1", env_text)
        self.assertEqual(0o600, stat.S_IMODE((rt / "mcp_server/.env").stat().st_mode))
        settings = json.loads((self.layout.state / "settings.json").read_text())
        self.assertEqual(8000, settings["server"]["port"])
        for name in ("opencode", "codex", "claude"):
            self.assertFalse(settings["subagents"]["providers"][name]["enabled"])
        self.assertEqual("pkg\n", self.layout.kind_file.read_text())
        self.assertEqual("c" * 40 + "\n", (self.layout.state / "update/deployed-commit").read_text())
        self.assertIn("VERSION_MARK", json.loads(self.layout.files_list.read_text()))
        self.assertTrue(run.ran("-m venv"))
        self.assertTrue(run.ran("--no-index --find-links"))
        self.assertTrue(run.ran("--require-hashes"))
        self.assertTrue(run.ran("--no-build-isolation -e"))
        self.assertTrue(run.ran("pip check"))
        self.assertEqual([str(self.layout.cli), "start"], run.calls[-1])
        self.assertEqual([8000], self.health_ports)
        self.assertNotIn(key, self.layout.log_file.read_text())

    def test_python_steps_run_from_the_runtime_directory(self) -> None:
        make_staging(self.home)
        seen = {}

        class Recording(FakeRun):
            def __call__(inner, cmd, *, env, cwd=None, check=True):
                if "ensure_" in " ".join(map(str, cmd)):
                    seen[" ".join(map(str, cmd))[-40:]] = cwd
                return super().__call__(cmd, env=env, cwd=cwd, check=check)

        pkg.install(self.layout, run=Recording(self.layout), health=self.health)
        self.assertTrue(seen)
        self.assertTrue(all(cwd == self.layout.runtime for cwd in seen.values()))

    def test_child_environment_is_scoped_to_the_install_home(self) -> None:
        make_staging(self.home)
        envs = []

        class Recording(FakeRun):
            def __call__(inner, cmd, *, env, cwd=None, check=True):
                envs.append(env)
                return super().__call__(cmd, env=env, cwd=cwd, check=check)

        os.environ["MCP_API_KEY"] = "leak-check-value"
        try:
            pkg.install(self.layout, run=Recording(self.layout), health=self.health)
        finally:
            del os.environ["MCP_API_KEY"]
        for env in envs:
            self.assertEqual(str(self.home), env["HOME"])
            self.assertEqual(str(self.layout.state), env["MAC_MCP_STATE_DIR"])
            self.assertNotIn("MCP_API_KEY", env)

    def test_zprofile_gets_exactly_one_path_line(self) -> None:
        make_staging(self.home)
        pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        text = (self.home / ".zprofile").read_text()
        self.assertEqual(1, text.count(pkg.PATH_LINE))

    def test_existing_zprofile_line_is_not_duplicated(self) -> None:
        make_staging(self.home)
        (self.home / ".zprofile").write_text("# mine\n" + pkg.PATH_LINE + "\n")
        pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        text = (self.home / ".zprofile").read_text()
        self.assertEqual(1, text.count(pkg.PATH_LINE))
        self.assertTrue(text.startswith("# mine\n"))

    def test_refuses_source_install(self) -> None:
        make_staging(self.home)
        self.layout.runtime.mkdir()
        (self.layout.runtime / "SOURCE_INSTALL").write_text("keep\n")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        self.assertEqual("keep\n", (self.layout.runtime / "SOURCE_INSTALL").read_text())
        self.assertFalse(self.layout.kind_file.exists())

    def test_source_checksum_mismatch_stops_before_runtime_exists(self) -> None:
        staging = make_staging(self.home)
        release = json.loads((staging / "release.json").read_text())
        release["source_sha256"] = "0" * 64
        (staging / "release.json").write_text(json.dumps(release))
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        self.assertFalse(self.layout.runtime.exists())

    def test_incomplete_staging_is_rejected(self) -> None:
        staging = make_staging(self.home)
        (staging / "source.tar").unlink()
        with self.assertRaises(pkg.InstallError):
            pkg.read_release(staging)

    def test_failed_fresh_install_leaves_no_runtime_cli_or_marker(self) -> None:
        make_staging(self.home)
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout, fail_on="pip check"), health=self.health)
        self.assertFalse(self.layout.runtime.exists())
        self.assertFalse(self.layout.cli.exists())
        self.assertFalse(self.layout.kind_file.exists())

    def test_unhealthy_server_fails_the_install(self) -> None:
        make_staging(self.home)
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda port: False)
        self.assertFalse(self.layout.runtime.exists())

    def test_no_start_skips_start_and_health(self) -> None:
        make_staging(self.home)
        run = FakeRun(self.layout)
        pkg.install(self.layout, run=run, health=self.health, start=False)
        self.assertFalse(run.ran(" start"))
        self.assertEqual([], self.health_ports)

    def test_broken_settings_are_not_overwritten(self) -> None:
        make_staging(self.home)
        self.layout.state.mkdir(parents=True, exist_ok=True)
        (self.layout.state / "settings.json").write_text("{not json")
        pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        self.assertEqual("{not json", (self.layout.state / "settings.json").read_text())
        self.assertEqual([8000], self.health_ports)

    def test_main_removes_staging_and_returns_1_on_failure(self) -> None:
        make_staging(self.home)
        (self.layout.staging / "source.tar").unlink()
        rc = pkg.main(["--home", str(self.home), "--no-start"])
        self.assertEqual(1, rc)
        self.assertFalse(self.layout.staging.exists())


if __name__ == "__main__":
    unittest.main()
