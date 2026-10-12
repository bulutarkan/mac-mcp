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
from unittest.mock import patch

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


def make_staging_again(home: Path, files: dict[str, str]) -> Path:
    staging = home / ".mac-mcp" / "pkg" / "staging"
    if staging.exists():
        import shutil
        shutil.rmtree(staging)
    return make_staging(home, files, release_id="stable-test-r2")


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

        with patch.dict(os.environ, {"MCP_API_KEY": "leak-check-value"}):
            pkg.install(self.layout, run=Recording(self.layout), health=self.health)
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

    # Final review fixes.
    def test_refuses_when_cli_from_another_install_exists(self) -> None:
        make_staging(self.home)
        self.layout.cli.parent.mkdir(parents=True)
        self.layout.cli.write_text("#!/bin/sh\n# install.sh launcher\n")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=self.health)
        self.assertIn("install.sh launcher", self.layout.cli.read_text())
        self.assertFalse(self.layout.runtime.exists())

    def test_failed_fresh_install_removes_the_payload_app(self) -> None:
        make_staging(self.home)
        self.layout.app.mkdir(parents=True)
        (self.layout.app / "NEW_APP").write_text("new")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout, fail_on="pip check"), health=self.health)
        self.assertFalse(self.layout.app.exists())

    def test_main_removes_staging_and_returns_1_on_failure(self) -> None:
        make_staging(self.home)
        (self.layout.staging / "source.tar").unlink()
        rc = pkg.main(["--home", str(self.home), "--no-start"])
        self.assertEqual(1, rc)
        self.assertFalse(self.layout.staging.exists())


class PkgUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-pkg-upgrade-")
        self.home = Path(self.td.name)
        self.layout = pkg.Layout(self.home)
        old_files = dict(BASE_FILES, OLD_ONLY="tracked in r1\n")
        make_staging(self.home, old_files)
        pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True)
        rt = self.layout.runtime
        (rt / "mcp_server/agents").mkdir(parents=True)
        (rt / "mcp_server/agents/team.json").write_text('{"local": true}\n')
        self.env_before = (rt / "mcp_server/.env").read_bytes()
        settings = self.layout.state / "settings.json"
        data = json.loads(settings.read_text())
        data["server"]["port"] = 8765
        settings.write_text(json.dumps(data))
        self.settings_before = settings.read_bytes()
        (self.layout.state / "dashboard-token").write_text("secret-token\n")
        (self.layout.backups_root / "promote-20261001").mkdir(parents=True)
        new_files = dict(BASE_FILES, VERSION_MARK="B\n")
        make_staging_again(self.home, new_files)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_upgrade_keeps_private_files_and_drops_removed_ones(self) -> None:
        run = FakeRun(self.layout)
        ports = []
        result = pkg.install(self.layout, run=run, health=lambda p: ports.append(p) or True, clock=lambda: 2000)
        rt = self.layout.runtime
        self.assertEqual("upgrade", result["mode"])
        self.assertEqual("B\n", (rt / "VERSION_MARK").read_text())
        self.assertFalse((rt / "OLD_ONLY").exists())
        self.assertEqual('{"local": true}\n', (rt / "mcp_server/agents/team.json").read_text())
        self.assertEqual(self.env_before, (rt / "mcp_server/.env").read_bytes())
        self.assertEqual(self.settings_before, (self.layout.state / "settings.json").read_bytes())
        self.assertEqual("secret-token\n", (self.layout.state / "dashboard-token").read_text())
        self.assertEqual([str(self.layout.cli), "stop"], run.calls[0])
        self.assertEqual([8765], ports)
        self.assertTrue((self.layout.backups_root / "pkg-2000" / "runtime" / "OLD_ONLY").exists())

    def test_only_newest_package_backup_is_kept(self) -> None:
        pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        make_staging_again(self.home, dict(BASE_FILES, VERSION_MARK="C\n"))
        pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 3000)
        names = sorted(p.name for p in self.layout.backups_root.iterdir())
        self.assertEqual(["pkg-3000", "promote-20261001"], names)

    def test_failed_upgrade_restores_and_restarts_previous_runtime(self) -> None:
        run = FakeRun(self.layout, fail_on="pip check")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=run, health=lambda p: True, clock=lambda: 2000)
        rt = self.layout.runtime
        self.assertEqual("A\n", (rt / "VERSION_MARK").read_text())
        self.assertTrue((rt / "OLD_ONLY").exists())
        self.assertEqual(self.env_before, (rt / "mcp_server/.env").read_bytes())
        self.assertEqual("pkg\n", self.layout.kind_file.read_text())
        self.assertEqual([str(self.layout.cli), "start"], run.calls[-1])
        self.assertFalse((self.layout.backups_root / "pkg-2000").exists())
        self.assertIn("OLD_ONLY", json.loads(self.layout.files_list.read_text()))

    def test_unhealthy_new_server_rolls_back(self) -> None:
        verdicts = iter([False, True])
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: next(verdicts), clock=lambda: 2000)
        self.assertEqual("A\n", (self.layout.runtime / "VERSION_MARK").read_text())

    # Final review fixes.
    def test_backup_move_failure_keeps_package_install_and_restarts_it(self) -> None:
        (self.layout.backups_root / "pkg-2000").mkdir(parents=True)
        run = FakeRun(self.layout)
        with self.assertRaises(Exception):
            pkg.install(self.layout, run=run, health=lambda p: True, clock=lambda: 2000)
        self.assertEqual("A\n", (self.layout.runtime / "VERSION_MARK").read_text())
        self.assertEqual("pkg\n", self.layout.kind_file.read_text())
        self.assertEqual([str(self.layout.cli), "start"], run.calls[-1])

    def test_start_exit_code_alone_does_not_fail_a_healthy_install(self) -> None:
        run = FakeRun(self.layout, fail_on="mac-mcp start")
        result = pkg.install(self.layout, run=run, health=lambda p: True, clock=lambda: 2000)
        self.assertEqual("upgrade", result["mode"])
        self.assertEqual("B\n", (self.layout.runtime / "VERSION_MARK").read_text())

    def test_unhealthy_upgrade_stops_new_server_before_restoring(self) -> None:
        run = FakeRun(self.layout)
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=run, health=lambda p: False, clock=lambda: 2000)
        cli = str(self.layout.cli)
        tail = [c for c in run.calls if c[0] == cli][-3:]
        self.assertEqual([[cli, "start"], [cli, "stop"], [cli, "start"]], tail)

    def test_failed_upgrade_restores_previous_app_bundle(self) -> None:
        self.layout.previous_app.mkdir(parents=True)
        (self.layout.previous_app / "OLD_APP").write_text("old")
        self.layout.app.mkdir(parents=True, exist_ok=True)
        (self.layout.app / "NEW_APP").write_text("new")
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout, fail_on="pip check"), health=lambda p: True, clock=lambda: 2000)
        self.assertTrue((self.layout.app / "OLD_APP").exists())
        self.assertFalse((self.layout.app / "NEW_APP").exists())
        self.assertFalse(self.layout.previous_app.exists())

    def test_successful_upgrade_discards_previous_app_copy(self) -> None:
        self.layout.previous_app.mkdir(parents=True)
        (self.layout.previous_app / "OLD_APP").write_text("old")
        self.layout.app.mkdir(parents=True, exist_ok=True)
        (self.layout.app / "NEW_APP").write_text("new")
        pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: True, clock=lambda: 2000)
        self.assertTrue((self.layout.app / "NEW_APP").exists())
        self.assertFalse(self.layout.previous_app.exists())

    def test_no_start_upgrade_never_touches_processes(self) -> None:
        run = FakeRun(self.layout)
        pkg.install(self.layout, run=run, health=lambda p: True, start=False, clock=lambda: 2000)
        self.assertFalse(run.ran(" stop"))
        self.assertFalse(run.ran(" start"))


if __name__ == "__main__":
    unittest.main()
