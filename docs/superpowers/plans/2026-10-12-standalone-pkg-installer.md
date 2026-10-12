# Standalone `.pkg` Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an unsigned, Apple-Silicon `.pkg` that installs the Mac MCP app, a runtime with its own Python, and starts everything, with no Xcode tools, Git, Homebrew or system Python.

**Architecture:** A home-domain ("Install for me only") package places the prebuilt app in `~/Applications` and a staging folder (Python tree, source tar, wheelhouse, installer core) in `~/.mac-mcp/pkg/staging`. A bash `postinstall` hands off to `pkg_install.py`, a stdlib-only module run on the bundled Python, which lays out `~/mac-mcp` exactly as `install.sh` does (offline pip from hash-locked wheels), preserves an existing pkg install's private files on upgrade, rolls back on failure, and starts the server. Pkg installs are marked with `~/.mac-mcp/install-kind = pkg`, and the updater, doctor and app treat that kind explicitly.

**Tech Stack:** bash (Bash 3.2-compatible), Python 3.14 stdlib, `pkgbuild`/`productbuild`/`pkgutil`, python-build-standalone, pip, SwiftUI (one small app change), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-12-standalone-pkg-installer-design.md`

## Global Constraints

- `install.sh` behavior is not changed by this work (its 2026-10-12 fix 440a7c1 is already in).
- Unsigned package; no Developer ID, notarization or stapling.
- arm64 only; minimum macOS 14.0.
- Bundled Python: `cpython-3.14.7+20260924-aarch64-apple-darwin-install_only_stripped.tar.gz`, SHA-256 `f0fb6b233971672dd7add4efa90f8c209a63253e457ffabd0148b9f734638319`.
- Install domain: current user home only; scripts never run as root and never use `sudo`.
- Paths: app `~/Applications/Mac MCP.app`, runtime `~/mac-mcp`, Python `~/mac-mcp/.python`, venv `~/mac-mcp/.venv`, state `~/.mac-mcp`, CLI `~/.local/bin/mac-mcp`, marker `~/.mac-mcp/install-kind` containing `pkg`.
- The package never overwrites an install that is not marked `pkg`.
- On upgrade, `mcp_server/.env`, everything under `~/.mac-mcp`, and untracked runtime files are preserved byte-identical.
- No secret (API key, tokens) in any log or console output.
- Package identifier `com.bulutarkan.mac-mcp.pkg`; output name `Mac-MCP-<version>-<release_id>-arm64.pkg` plus `.sha256`, under git-ignored `dist/`.
- Manual-update URL: `https://github.com/bulutarkan/mac-mcp/releases`.
- Isolated testing never uses port 8765 or `mac-mcp start`/`stop`/`restart` against a temp HOME (launchd labels are shared with production); use 8878 and start uvicorn directly.
- Tests run with: `MCP_API_KEY=ci-regression-only-0123456789abcdef0123456789abcdef MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true ~/mac-mcp/.venv/bin/python -m unittest <module> -v` from `/Users/tarkanbulut/Projects/mac-mcp`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; commit as Tarkan's Git identity.

## Review Focus

- Installing the pkg on a Mac that already has an `install.sh`/source install must stop before any file is placed and leave that install untouched (preinstall test in Task 5, `refuses_source_install` test in Task 3).
- Re-running the same or a newer pkg over a pkg install must keep the API key, settings, tokens and agent state files, and drop files the new release deleted (upgrade tests in Task 4).
- A failure in the middle of an upgrade (pip, CLI wiring, server never healthy) must bring the previous runtime back and start it (rollback tests in Task 4).
- A missing `~/.zprofile`, or one that already has the PATH line, must end with exactly one PATH line (Task 3 test).
- A second macOS account (different uid, different home) must get files it owns and a working install; the scripts must find that account's home without relying on the build machine (Task 5 home-resolution test, Task 8 real E2E).

---

### Task 1: Install kind for updater, MCP update tool and doctor

**Files:**
- Create: `mcp_server/install_kind.py`
- Modify: `mcp_server/cli.py` (`update()` at ~2109)
- Modify: `mcp_server/tools_update.py` (`mac_mcp_update()` at ~197)
- Modify: `mcp_server/diagnostics.py` (`_check_cli_installation()` details at ~175)
- Test: `tests/test_install_kind.py`

**Interfaces:**
- Produces: `install_kind() -> str` (`"pkg"` or `"source"`), `package_update_payload(check_only: bool = True) -> dict`, constant `RELEASES_URL`. Task 2 decodes the payload keys `install_kind`, `message`, `download_url`. Task 3 writes the marker file this reads.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_install_kind.py
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import install_kind as ik


class InstallKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory(prefix="mac-mcp-install-kind-")
        self.state = Path(self.td.name)
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.state)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.td.cleanup()

    def test_source_when_marker_missing_or_other(self) -> None:
        self.assertEqual("source", ik.install_kind())
        (self.state / "install-kind").write_text("something-else\n")
        self.assertEqual("source", ik.install_kind())

    def test_pkg_when_marker_says_pkg(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        self.assertEqual("pkg", ik.install_kind())

    def test_payload_points_to_releases(self) -> None:
        payload = ik.package_update_payload()
        self.assertEqual("pkg", payload["install_kind"])
        self.assertEqual(ik.RELEASES_URL, payload["download_url"])
        self.assertFalse(payload["update_available"])
        self.assertIn(".pkg", payload["message"])

    def test_cli_update_check_json_for_pkg_install(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import cli
        args = SimpleNamespace(check=True, json=True, repo=None, runtime=None, branch="main", remote="origin",
                               skip_restart=False, skip_deps=False)
        out = io.StringIO()
        with patch("mcp_server.cli.check_update") as check, redirect_stdout(out):
            rc = cli.update(args)
        check.assert_not_called()
        self.assertEqual(0, rc)
        self.assertEqual("pkg", json.loads(out.getvalue())["install_kind"])

    def test_cli_update_install_for_pkg_install_is_refused_with_guidance(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import cli
        args = SimpleNamespace(check=False, json=False, repo=None, runtime=None, branch="main", remote="origin",
                               skip_restart=False, skip_deps=False)
        out = io.StringIO()
        with patch("mcp_server.cli.launch_detached_update") as launch, redirect_stdout(out):
            rc = cli.update(args)
        launch.assert_not_called()
        self.assertEqual(3, rc)
        self.assertIn(ik.RELEASES_URL, out.getvalue())

    def test_update_tool_for_pkg_install(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import tools_update
        with patch("mcp_server.tools_update.check_update") as check:
            payload = tools_update.mac_mcp_update(check_only=False)
        check.assert_not_called()
        self.assertEqual("pkg", payload["install_kind"])
        self.assertFalse(payload["check_only"])

    def test_doctor_reports_install_kind(self) -> None:
        (self.state / "install-kind").write_text("pkg\n")
        from mcp_server import diagnostics
        row = diagnostics._check_cli_installation().to_dict()
        self.assertEqual("pkg", row["details"]["install_kind"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `... -m unittest tests.test_install_kind -v`
Expected: `ModuleNotFoundError: No module named 'mcp_server.install_kind'`

- [ ] **Step 3: Implement `mcp_server/install_kind.py`**

```python
"""How this Mac MCP was installed: from source (install.sh / git updater) or from the .pkg.

Package installs have no Git checkout, so the Git-based updater cannot serve them; they
update by installing the newer package from GitHub Releases.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

RELEASES_URL = "https://github.com/bulutarkan/mac-mcp/releases"
_MESSAGE = (
    "This Mac MCP was installed from a package. To update, download the newer "
    ".pkg from GitHub Releases and open it; your settings and keys are kept."
)


def _marker() -> Path:
    state = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return state / "install-kind"


def install_kind() -> str:
    try:
        value = _marker().read_text(encoding="utf-8").strip()
    except OSError:
        return "source"
    return "pkg" if value == "pkg" else "source"


def package_update_payload(check_only: bool = True) -> Dict[str, Any]:
    return {
        "ok": True,
        "install_kind": "pkg",
        "check_only": check_only,
        "update_available": False,
        "manual_update": True,
        "download_url": RELEASES_URL,
        "message": _MESSAGE,
    }
```

- [ ] **Step 4: Branch the CLI, the tool and doctor**

In `mcp_server/cli.py`, add `from .install_kind import install_kind, package_update_payload` next to the other relative imports, and make the first statements inside `update()`'s `try:` block:

```python
        if install_kind() == "pkg":
            payload = package_update_payload(check_only=bool(args.check))
            if args.check and getattr(args, "json", False):
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(f"[mac-mcp update] {payload['message']} {payload['download_url']}")
            return 0 if args.check else 3
```

In `mcp_server/tools_update.py`, add `from .install_kind import install_kind, package_update_payload` and make the first statement after the branch validation in `mac_mcp_update()`:

```python
    if install_kind() == "pkg":
        return package_update_payload(check_only=bool(check_only))
```

In `mcp_server/diagnostics.py`, add `from .install_kind import install_kind` and add `"install_kind": install_kind(),` to the `details` dict in `_check_cli_installation()`.

- [ ] **Step 5: Run the new tests and the neighbors**

Run: `... -m unittest tests.test_install_kind tests.test_update tests.test_cli_ergonomics -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/install_kind.py mcp_server/cli.py mcp_server/tools_update.py mcp_server/diagnostics.py tests/test_install_kind.py
git commit -m "Updater: package installs point to GitHub Releases instead of Git"
```

---

### Task 2: Menu app shows the package update guidance

**Files:**
- Modify: `menu_app/Sources/AppModels+Updates.swift` (add a model after `UpdateCheckInfo`)
- Modify: `menu_app/Sources/AppState.swift` (`applyUpdateCheckOutput` at ~1336)
- Test: `tests/test_menu_package_update_notice.py`

**Interfaces:**
- Consumes: the JSON from Task 1 (`install_kind`, `message`, `download_url`).

- [ ] **Step 1: Write the failing source test** (the repo's Swift features are pinned by source-inspection tests, see `tests/test_menu_state_performance.py`)

```python
# tests/test_menu_package_update_notice.py
import unittest

from tests._menu_sources import feature_source


class MenuPackageUpdateNoticeTests(unittest.TestCase):
    def test_package_install_update_check_is_decoded_before_git_info(self) -> None:
        models = feature_source("AppModels+Updates")
        self.assertIn("struct PackageInstallUpdateNotice: Decodable", models)
        self.assertIn('case installKind = "install_kind"', models)
        self.assertIn('case downloadURL = "download_url"', models)
        state = feature_source("AppState")
        body = state[state.index("private func applyUpdateCheckOutput"):state.index("func checkForUpdates()")]
        self.assertLess(body.index("PackageInstallUpdateNotice.self"), body.index("UpdateCheckInfo.self"))
        self.assertIn('notice.installKind == "pkg"', body)
        self.assertIn("NSWorkspace.shared.open(url)", body)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `... -m unittest tests.test_menu_package_update_notice -v`
Expected: FAIL on `struct PackageInstallUpdateNotice: Decodable`.

- [ ] **Step 3: Add the model** in `AppModels+Updates.swift`, right after `UpdateCheckInfo`:

```swift
struct PackageInstallUpdateNotice: Decodable, Equatable {
    let installKind: String
    let message: String
    let downloadURL: String

    enum CodingKeys: String, CodingKey {
        case message
        case installKind = "install_kind"
        case downloadURL = "download_url"
    }
}
```

- [ ] **Step 4: Handle it first in `applyUpdateCheckOutput`** (insert at the top of the function body):

```swift
        if let data = output.data(using: .utf8),
           let notice = try? JSONDecoder().decode(PackageInstallUpdateNotice.self, from: data),
           notice.installKind == "pkg" {
            setIfChanged(\.updateCheckInfo, nil)
            guard shouldShowNotice else { return }
            showNotice(ActionNotice(kind: .info, message: notice.message))
            if let url = URL(string: notice.downloadURL) {
                NSWorkspace.shared.open(url)
            }
            return
        }
```

- [ ] **Step 5: Test and build outside the installed app**

Run: `... -m unittest tests.test_menu_package_update_notice tests.test_menu_state_performance -v` → PASS
Run: `menu_app/build_app.sh /tmp/mac-mcp-build-test && codesign --verify --deep --strict "/tmp/mac-mcp-build-test/Mac MCP.app"` → prints the app path, no codesign error.

- [ ] **Step 6: Commit**

```bash
git add menu_app/Sources/AppModels+Updates.swift menu_app/Sources/AppState.swift tests/test_menu_package_update_notice.py
git commit -m "Menu app: package installs open GitHub Releases on update check"
```

Note for the next release: native sources changed, so CFBundleVersion is bumped (to 224) in both plists at release time, not in this task.

---

### Task 3: `pkg_install.py` — fresh install

**Files:**
- Create: `packaging/pkg/pkg_install.py`
- Test: `tests/test_pkg_install.py`

**Interfaces:**
- Consumes: staging layout written by Task 6 (`release.json`, `source.tar`, `python/bin/python3`, `wheelhouse/`).
- `start=True` runs `mac-mcp start`, which also opens the menu app (CLI `_launch_menu_app`), then checks `/health`.
- Produces (Task 4 extends, Task 5 calls): `Layout(home: Path)` with properties `runtime`, `state`, `staging`, `app`, `cli`, `kind_file`, `log_file`, `backups_root`, `files_list`; `InstallError`; `read_release(staging: Path) -> dict`; `is_upgrade(layout: Layout) -> bool`; `install(layout, *, run=_run, health=_health_ok, start=True, clock=time.time) -> dict`; `main(argv: list[str] | None = None) -> int` with `--home PATH` and `--no-start`.
- Runner contract: `run(cmd: list[str], *, env: dict[str, str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess`; raises `InstallError` on non-zero when `check`.
- Health contract: `health(port: int) -> bool`.

- [ ] **Step 1: Write the failing tests (fresh install half)**

```python
# tests/test_pkg_install.py
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pkg_install", ROOT / "packaging/pkg/pkg_install.py")
pkg = importlib.util.module_from_spec(_spec)
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
```

- [ ] **Step 2: Run to verify failure**

Run: `... -m unittest tests.test_pkg_install -v`
Expected: `FileNotFoundError` for `packaging/pkg/pkg_install.py`.

- [ ] **Step 3: Implement `packaging/pkg/pkg_install.py`**

```python
"""Postinstall core of the Mac MCP package.

Runs on the Python bundled in the package (stdlib only), as the installing user. It lays
out ~/mac-mcp the way install.sh does, but offline: the source tree of the release, the
bundled Python, and a virtualenv filled from hash-locked wheels shipped in the package.
An existing package install is upgraded in place with its private files kept; anything
else at ~/mac-mcp is never touched. Any failure restores the previous state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_PORT = 8000
_RELEASE_KEYS = ("release_id", "version", "commit", "source_sha256", "python_version", "arch")
_ENV_DEFAULTS = {
    "MCP_ALLOW_NO_AUTH": "false",
    "MCP_ALLOW_SHELL": "true",
    "MAC_MCP_PERMISSION_PROFILE": "standard",
    "NGROK_DOMAIN": "",
}
# Never carried from an old runtime into a new one: rebuilt by every install.
_NOT_CARRIED_DIRS = (".venv", ".python", "__pycache__")


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    home: Path

    @property
    def runtime(self) -> Path:
        return self.home / "mac-mcp"

    @property
    def state(self) -> Path:
        return self.home / ".mac-mcp"

    @property
    def staging(self) -> Path:
        return self.state / "pkg" / "staging"

    @property
    def app(self) -> Path:
        return self.home / "Applications" / "Mac MCP.app"

    @property
    def cli(self) -> Path:
        return self.home / ".local" / "bin" / "mac-mcp"

    @property
    def kind_file(self) -> Path:
        return self.state / "install-kind"

    @property
    def files_list(self) -> Path:
        return self.state / "pkg" / "installed-files.json"

    @property
    def log_file(self) -> Path:
        return self.state / "logs" / "pkg-install.log"

    @property
    def backups_root(self) -> Path:
        return self.state / "backups"


class _Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def write(self, message: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
        os.chmod(self.path, 0o600)


def _run(cmd: List[str], *, env: Dict[str, str], cwd: Optional[Path] = None, check: bool = True):
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=1800)
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise InstallError(f"{Path(str(cmd[0])).name} {' '.join(map(str, cmd[1:3]))} failed: {' | '.join(tail)}")
    return proc


def _health_ok(port: int) -> bool:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def read_release(staging: Path) -> dict:
    try:
        release = json.loads((staging / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"The package staging folder is unreadable: {exc}") from exc
    missing = [key for key in _RELEASE_KEYS if not release.get(key)]
    if missing:
        raise InstallError(f"release.json is missing {', '.join(missing)}")
    if release["arch"] != "arm64":
        raise InstallError("This package is for Apple Silicon Macs only.")
    for part in ("source.tar", "python/bin/python3"):
        if not (staging / part).is_file():
            raise InstallError(f"The package is incomplete: {part} is missing.")
    if not any((staging / "wheelhouse").glob("*.whl")):
        raise InstallError("The package is incomplete: the wheelhouse is empty.")
    return release


def is_upgrade(layout: Layout) -> bool:
    try:
        marked = layout.kind_file.read_text(encoding="utf-8").strip() == "pkg"
    except OSError:
        marked = False
    return layout.runtime.exists() and marked


def child_env(layout: Layout) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MCP_API_KEY", "MAC_MCP_API_KEY", "PYTHONPATH", "PYTHONHOME"}}
    env.update({"HOME": str(layout.home), "PATH": SAFE_PATH, "MAC_MCP_STATE_DIR": str(layout.state)})
    return env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_members(staging: Path) -> List[str]:
    with tarfile.open(staging / "source.tar") as tar:
        return sorted(m.name for m in tar.getmembers() if m.isfile())


def _lay_out_runtime(layout: Layout, release: dict) -> None:
    layout.runtime.mkdir(parents=True)
    with tarfile.open(layout.staging / "source.tar") as tar:
        tar.extractall(layout.runtime, filter="data")
    shutil.copytree(layout.staging / "python", layout.runtime / ".python", symlinks=True)


def _create_environment(layout: Layout, run, env: Dict[str, str]) -> None:
    rt = layout.runtime
    run([rt / ".python/bin/python3", "-m", "venv", rt / ".venv"], env=env)
    py = rt / ".venv/bin/python"
    pip = [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-index",
           "--find-links", layout.staging / "wheelhouse"]
    run(pip + ["--require-hashes", "-r", rt / "requirements.lock"], env=env)
    run(pip + ["--no-deps", "--no-build-isolation", "-e", rt], env=env)
    run([py, "-m", "pip", "check"], env=env)
    run([py, "-c", "import fastapi, uvicorn, mcp"], env=env, cwd=rt)


def _write_new_env_file(layout: Layout) -> None:
    example = layout.runtime / "mcp_server/.env.example"
    if not example.is_file():
        raise InstallError("mcp_server/.env.example is missing from the release.")
    values = {"MCP_API_KEY": secrets.token_urlsafe(48), **_ENV_DEFAULTS}
    out, seen = [], set()
    for line in example.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in values:
            out.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            out.append(line)
    out += [f"{name}={value}" for name, value in values.items() if name not in seen]
    target = layout.runtime / "mcp_server/.env"
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def _ensure_settings(layout: Layout, log: _Log) -> int:
    path = layout.state / "settings.json"
    layout.state.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            log.write("settings.json is unreadable; left unchanged, using the default port")
            return DEFAULT_PORT
    else:
        data = {}
    before = json.dumps(data, sort_keys=True)
    server = data.setdefault("server", {})
    if not isinstance(server, dict):
        server = data["server"] = {}
    server.setdefault("port", DEFAULT_PORT)
    server.setdefault("cli_path", "")
    server.setdefault("ngrok_on_start", False)
    server.setdefault("public_endpoint_mode", "none")
    server.setdefault("public_url", "")
    server.setdefault("cloudflare_tunnel", "")
    subagents = data.setdefault("subagents", {})
    if not isinstance(subagents, dict):
        subagents = data["subagents"] = {}
    providers = subagents.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = subagents["providers"] = {}
    for name in ("opencode", "codex", "claude"):
        providers.setdefault(name, {}).setdefault("enabled", False)
    if not path.exists() or json.dumps(data, sort_keys=True) != before:
        _write_settings(path, data)
    try:
        return int(server.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _write_settings(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _wire_up(layout: Layout, run, env: Dict[str, str], port: int, release: dict, members: List[str]) -> None:
    rt, py = layout.runtime, layout.runtime / ".venv/bin/python"
    # python -c puts the working directory first on sys.path: always run from the runtime.
    run([py, "-c", "from mcp_server.chrome_background_bridge import ensure_chrome_companion_config; "
                   "p=ensure_chrome_companion_config(); assert p and p.is_file()"],
        env={**env, "MAC_MCP_PORT": str(port)}, cwd=rt)
    for secret in (layout.state / "chrome-companion-token", rt / "menu_app/ChromeVisualCompanion/bridge_config.js"):
        if secret.exists():
            os.chmod(secret, 0o600)
    run([py, "-c", "from pathlib import Path; import os; from mcp_server.cli_bootstrap import ensure_cli_launcher; "
                   "assert ensure_cli_launcher(Path(os.environ['MAC_MCP_RUNTIME_DIR']), "
                   "Path(os.environ['MAC_MCP_CLI_PATH']), strict=True)"],
        env={**env, "MAC_MCP_RUNTIME_DIR": str(rt), "MAC_MCP_CLI_PATH": str(layout.cli)}, cwd=rt)
    profile = layout.home / ".zprofile"
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if PATH_LINE not in existing:
        with profile.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# Mac MCP\n{PATH_LINE}\n")
    (layout.state / "update").mkdir(parents=True, exist_ok=True)
    (layout.state / "update/deployed-commit").write_text(release["commit"] + "\n", encoding="utf-8")
    layout.files_list.parent.mkdir(parents=True, exist_ok=True)
    layout.files_list.write_text(json.dumps(members, indent=0) + "\n", encoding="utf-8")
    layout.kind_file.write_text("pkg\n", encoding="utf-8")


def _start(layout: Layout, run, env: Dict[str, str], port: int, health) -> None:
    run([layout.cli, "start"], env=env)
    if not health(port):
        raise InstallError(f"Mac MCP did not become healthy on port {port}.")


def install(layout: Layout, *, run=_run, health=_health_ok, start: bool = True, clock=time.time) -> dict:
    log = _Log(layout.log_file)
    release = read_release(layout.staging)
    upgrade = is_upgrade(layout)
    if layout.runtime.exists() and not upgrade:
        raise InstallError(
            f"{layout.runtime} was not installed by the Mac MCP package (install.sh or a source install). "
            "It is left untouched; update it with 'mac-mcp update'."
        )
    if _sha256(layout.staging / "source.tar") != release["source_sha256"]:
        raise InstallError("The package source archive does not match its release checksum.")
    members = _source_members(layout.staging)
    env = child_env(layout)
    log.write(f"{'upgrade' if upgrade else 'fresh install'} to {release['release_id']} ({release['commit'][:12]})")
    cli_existed = layout.cli.exists()
    backup: Optional[Path] = None
    runtime_created = False
    try:
        runtime_created = True
        _lay_out_runtime(layout, release)
        _create_environment(layout, run, env)
        _write_new_env_file(layout)
        port = _ensure_settings(layout, log)
        _wire_up(layout, run, env, port, release, members)
        if start:
            _start(layout, run, env, port, health)
    except Exception as exc:
        log.write(f"install failed: {exc}")
        if runtime_created and layout.runtime.exists():
            shutil.rmtree(layout.runtime, ignore_errors=True)
        if not cli_existed and layout.cli.exists():
            layout.cli.unlink()
        layout.kind_file.unlink(missing_ok=True)
        raise
    log.write("install complete")
    return {"mode": "fresh", "release_id": release["release_id"], "backup": None}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mac MCP package postinstall")
    parser.add_argument("--home", required=True)
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args(argv)
    layout = Layout(Path(args.home))
    try:
        result = install(layout, start=not args.no_start)
        print(f"Mac MCP {result['mode']} complete: {result['release_id']}")
        return 0
    except Exception as exc:  # Installer shows stderr; nothing secret is in these messages.
        print(f"Mac MCP installation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(layout.staging, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `... -m unittest tests.test_pkg_install -v`
Expected: all `PkgFreshInstallTests` PASS.

- [ ] **Step 5: Commit**

```bash
git add packaging/pkg/pkg_install.py tests/test_pkg_install.py
git commit -m "Package installer core: offline fresh install of the runtime"
```

---

### Task 4: `pkg_install.py` — upgrade, carry-over and rollback

**Files:**
- Modify: `packaging/pkg/pkg_install.py` (`install()` and new helpers)
- Test: `tests/test_pkg_install.py` (new class `PkgUpgradeTests`)

**Interfaces:**
- Consumes: Task 3's `Layout`, `FakeRun`, `make_staging`.
- Produces: `install()` returns `{"mode": "upgrade", "release_id": ..., "backup": str}` for upgrades; backups at `~/.mac-mcp/backups/pkg-<epoch>/runtime`, only the newest `pkg-*` kept.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pkg_install.py`, before the `__main__` guard)

```python
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

    def test_unhealthy_new_server_rolls_back(self) -> None:
        verdicts = iter([False, True])
        with self.assertRaises(pkg.InstallError):
            pkg.install(self.layout, run=FakeRun(self.layout), health=lambda p: next(verdicts), clock=lambda: 2000)
        self.assertEqual("A\n", (self.layout.runtime / "VERSION_MARK").read_text())
```

and the helper next to `make_staging`:

```python
def make_staging_again(home: Path, files: dict[str, str]) -> Path:
    staging = home / ".mac-mcp" / "pkg" / "staging"
    if staging.exists():
        import shutil
        shutil.rmtree(staging)
    return make_staging(home, files, release_id="stable-test-r2")
```

Note: `pkg.install` does not remove staging (only `main` does), so the setUp's first install leaves staging in place; `make_staging_again` replaces it.

- [ ] **Step 2: Run to verify failure**

Run: `... -m unittest tests.test_pkg_install.PkgUpgradeTests -v`
Expected: ERROR — `FileExistsError` from `_lay_out_runtime`, because Task 3's `install()` has no upgrade path yet.

- [ ] **Step 3: Add upgrade helpers** to `pkg_install.py` (above `install`)

```python
def _move_runtime_to_backup(layout: Layout, clock) -> Path:
    target = layout.backups_root / f"pkg-{int(clock())}" / "runtime"
    target.parent.mkdir(parents=True, exist_ok=False)
    os.rename(layout.runtime, target)
    return target


def _carried_files(backup: Path, previous: List[str], current: List[str]) -> List[str]:
    """Files in the old runtime that no release shipped: the owner's private/local files."""
    shipped = set(previous) | set(current)
    out = []
    for path in backup.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(backup).as_posix()
        parts = rel.split("/")
        if rel in shipped or any(d in parts for d in _NOT_CARRIED_DIRS) or any(x.endswith(".egg-info") for x in parts):
            continue
        out.append(rel)
    return sorted(out)


def _carry_over(backup: Path, runtime: Path, files: List[str]) -> None:
    for rel in files:
        target = runtime / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / rel, target)


def _previous_members(layout: Layout) -> List[str]:
    try:
        data = json.loads(layout.files_list.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _prune_package_backups(layout: Layout, keep: Path) -> None:
    for folder in layout.backups_root.glob("pkg-*"):
        if folder != keep.parent:
            shutil.rmtree(folder, ignore_errors=True)
```

- [ ] **Step 4: Replace `install()`** with the upgrade-aware version

`start=False` (`--no-start`) means "never stop or start processes": the isolated test in Task 7 runs as the same macOS user as production, and `mac-mcp stop/start` act on launchd labels shared with it.

```python
def install(layout: Layout, *, run=_run, health=_health_ok, start: bool = True, clock=time.time) -> dict:
    log = _Log(layout.log_file)
    release = read_release(layout.staging)
    upgrade = is_upgrade(layout)
    if layout.runtime.exists() and not upgrade:
        raise InstallError(
            f"{layout.runtime} was not installed by the Mac MCP package (install.sh or a source install). "
            "It is left untouched; update it with 'mac-mcp update'."
        )
    if _sha256(layout.staging / "source.tar") != release["source_sha256"]:
        raise InstallError("The package source archive does not match its release checksum.")
    members = _source_members(layout.staging)
    previous = _previous_members(layout)
    deployed_file = layout.state / "update/deployed-commit"
    # Restored on a failed upgrade, so the old runtime keeps describing itself.
    saved = {f: f.read_bytes() for f in (layout.files_list, deployed_file) if f.exists()}
    env = child_env(layout)
    log.write(f"{'upgrade' if upgrade else 'fresh install'} to {release['release_id']} ({release['commit'][:12]})")
    cli_existed = layout.cli.exists()
    backup: Optional[Path] = None
    runtime_created = False
    try:
        if upgrade:
            if start and layout.cli.exists():
                run([layout.cli, "stop"], env=env, check=False)
            backup = _move_runtime_to_backup(layout, clock)
        runtime_created = True
        _lay_out_runtime(layout, release)
        if backup is not None:
            _carry_over(backup, layout.runtime, _carried_files(backup, previous, members))
        _create_environment(layout, run, env)
        if not (layout.runtime / "mcp_server/.env").is_file():
            _write_new_env_file(layout)
        port = _ensure_settings(layout, log)
        _wire_up(layout, run, env, port, release, members)
        if start:
            _start(layout, run, env, port, health)
    except Exception as exc:
        log.write(f"install failed: {exc}")
        if runtime_created and layout.runtime.exists():
            shutil.rmtree(layout.runtime, ignore_errors=True)
        if backup is not None:
            os.rename(backup, layout.runtime)
            shutil.rmtree(backup.parent, ignore_errors=True)
            for path, data in saved.items():
                path.write_bytes(data)
            layout.kind_file.write_text("pkg\n", encoding="utf-8")
            if start and layout.cli.exists():
                run([layout.cli, "start"], env=env, check=False)
                log.write("previous runtime restored and started")
        else:
            if not cli_existed and layout.cli.exists():
                layout.cli.unlink()
            layout.kind_file.unlink(missing_ok=True)
        raise
    if backup is not None:
        _prune_package_backups(layout, backup)
    log.write("install complete")
    return {"mode": "upgrade" if upgrade else "fresh", "release_id": release["release_id"],
            "backup": str(backup) if backup else None}
```

Add to `test_failed_upgrade_restores_and_restarts_previous_runtime`:

```python
        self.assertIn("OLD_ONLY", json.loads(self.layout.files_list.read_text()))
```

and add this test to `PkgUpgradeTests`:

```python
    def test_no_start_upgrade_never_touches_processes(self) -> None:
        run = FakeRun(self.layout)
        pkg.install(self.layout, run=run, health=lambda p: True, start=False, clock=lambda: 2000)
        self.assertFalse(run.ran(" stop"))
        self.assertFalse(run.ran(" start"))
```

- [ ] **Step 5: Run the whole module**

Run: `... -m unittest tests.test_pkg_install -v`
Expected: all fresh and upgrade tests PASS.

- [ ] **Step 6: Commit**

```bash
git add packaging/pkg/pkg_install.py tests/test_pkg_install.py
git commit -m "Package installer: upgrades keep private files and roll back on failure"
```

---

### Task 5: Installer scripts and distribution

**Files:**
- Create: `packaging/pkg/scripts/preinstall`
- Create: `packaging/pkg/scripts/postinstall`
- Create: `packaging/pkg/distribution.xml`
- Test: `tests/test_pkg_scripts.py`

**Interfaces:**
- Consumes: `pkg_install.main` via `python3 -I pkg_install.py --home <home>` (Task 3).
- Produces: `distribution.xml` with placeholders `@VERSION@` and `@COMPONENT_PKG@` that Task 6 fills.
- Both scripts resolve the home folder from `MAC_MCP_PKG_HOME` when set (tests only), else from Directory Services for the user the script runs as.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pkg_scripts.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `... -m unittest tests.test_pkg_scripts -v`
Expected: FAIL (scripts and distribution missing).

- [ ] **Step 3: Write `packaging/pkg/scripts/preinstall`** (mode 755)

```bash
#!/bin/bash
# Mac MCP package preinstall: runs as the installing user before any file is placed.
set -u
if [[ -n "${MAC_MCP_PKG_HOME:-}" ]]; then
  HOME_DIR="$MAC_MCP_PKG_HOME"
else
  HOME_DIR="$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
fi
[[ -n "$HOME_DIR" && -d "$HOME_DIR" ]] || { echo "Mac MCP: could not find your home folder." >&2; exit 1; }

kind="$(/bin/cat "$HOME_DIR/.mac-mcp/install-kind" 2>/dev/null || true)"
if [[ -e "$HOME_DIR/mac-mcp" && "$kind" != "pkg" ]]; then
  echo "Mac MCP is already installed in $HOME_DIR/mac-mcp with install.sh or from source." >&2
  echo "The package will not replace it. Update that install with: mac-mcp update" >&2
  exit 1
fi
[[ "$(/usr/bin/uname -m)" == "arm64" ]] || { echo "Mac MCP: this package is for Apple Silicon Macs." >&2; exit 1; }
major="$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d. -f1)"
(( major >= 14 )) || { echo "Mac MCP: macOS 14 or newer is required." >&2; exit 1; }

# A package upgrade replaces the app bundle: stop the running menu app first.
/usr/bin/pkill -f "^$HOME_DIR/Applications/Mac MCP.app/Contents/MacOS/MacMCPMenu" 2>/dev/null || true
exit 0
```

- [ ] **Step 4: Write `packaging/pkg/scripts/postinstall`** (mode 755)

```bash
#!/bin/bash
# Mac MCP package postinstall: hands off to the installer core on the bundled Python.
set -u
if [[ -n "${MAC_MCP_PKG_HOME:-}" ]]; then
  HOME_DIR="$MAC_MCP_PKG_HOME"
else
  HOME_DIR="$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | /usr/bin/awk '{print $2}')"
fi
STAGING="$HOME_DIR/.mac-mcp/pkg/staging"
exec "$STAGING/python/bin/python3" -I "$STAGING/pkg_install.py" --home "$HOME_DIR"
```

- [ ] **Step 5: Write `packaging/pkg/distribution.xml`**

```xml
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Mac MCP</title>
    <options customize="never" require-scripts="false" hostArchitectures="arm64" rootVolumeOnly="false"/>
    <domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
    <allowed-os-versions>
        <os-version min="14.0"/>
    </allowed-os-versions>
    <choices-outline>
        <line choice="default">
            <line choice="com.bulutarkan.mac-mcp.pkg"/>
        </line>
    </choices-outline>
    <choice id="default"/>
    <choice id="com.bulutarkan.mac-mcp.pkg" visible="false">
        <pkg-ref id="com.bulutarkan.mac-mcp.pkg"/>
    </choice>
    <pkg-ref id="com.bulutarkan.mac-mcp.pkg" version="@VERSION@" onConclusion="none">@COMPONENT_PKG@</pkg-ref>
</installer-gui-script>
```

- [ ] **Step 6: Run the tests, make scripts executable, commit**

Run: `chmod 755 packaging/pkg/scripts/preinstall packaging/pkg/scripts/postinstall && /bin/bash -n packaging/pkg/scripts/preinstall && /bin/bash -n packaging/pkg/scripts/postinstall`
Run: `... -m unittest tests.test_pkg_scripts -v` → PASS

```bash
git add packaging/pkg/scripts packaging/pkg/distribution.xml tests/test_pkg_scripts.py
git commit -m "Package installer: preinstall guard, postinstall hand-off, home-only distribution"
```

---

### Task 6: `scripts/build_pkg.sh`

**Files:**
- Create: `scripts/build_pkg.sh`
- Modify: `.gitignore` (add `dist/`)
- Modify: `release/README.md` (one "Package installer" paragraph)
- Modify: `README.md` (short "Install with the package (Apple Silicon)" section, including the Open Anyway step)
- Test: run the script (no unit test; it is glue around Apple tools, verified by its output)

**Interfaces:**
- Consumes: Task 3–5 files from the built commit (`packaging/pkg/*`), `menu_app/build_app.sh`, `scripts/verify_release_manifest.py`.
- Produces: `dist/Mac-MCP-<version>-<release_id>-arm64.pkg`, `.sha256`; with `--stage-only DIR`, a home tree `DIR/Applications/Mac MCP.app` + `DIR/.mac-mcp/pkg/staging/` (used by Task 7).
- CLI: `scripts/build_pkg.sh [--commit SHA] [--allow-unverified] [--stage-only DIR]`

- [ ] **Step 1: Write the script** (mode 755)

```bash
#!/bin/bash
# Build the unsigned Apple Silicon Mac MCP package from one commit (default HEAD).
# Normal use: on a signed stable checkpoint. --allow-unverified is for local tests only.
set -Eeuo pipefail

PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.14.7%2B20260924-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256="f0fb6b233971672dd7add4efa90f8c209a63253e457ffabd0148b9f734638319"
PY_VERSION="3.14.7"
PKG_ID="com.bulutarkan.mac-mcp.pkg"

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
COMMIT="HEAD"; ALLOW_UNVERIFIED=0; STAGE_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit) COMMIT="$2"; shift 2 ;;
    --allow-unverified) ALLOW_UNVERIFIED=1; shift ;;
    --stage-only) STAGE_ONLY="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$(uname -m)" == "arm64" ]] || { echo "build on an Apple Silicon Mac" >&2; exit 1; }
COMMIT="$(git -C "$REPO" rev-parse --verify "$COMMIT^{commit}")"
SHORT="${COMMIT:0:8}"
VERSION="$(git -C "$REPO" show "$COMMIT:pyproject.toml" | /usr/bin/awk -F'"' '/^version *=/{print $2; exit}')"

if [[ "$ALLOW_UNVERIFIED" -eq 1 ]]; then
  RELEASE_ID="unverified-$SHORT"; PAYLOAD_SHA="unverified"
else
  python3 "$REPO/scripts/verify_release_manifest.py" --commit "$COMMIT" --branch main >/dev/null \
    || { echo "$SHORT is not a verified stable checkpoint (use --allow-unverified for local tests)" >&2; exit 1; }
  MANIFEST="$(git -C "$REPO" show "$COMMIT:release/stable-manifest.json")"
  RELEASE_ID="$(printf '%s' "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["release_id"])')"
  PAYLOAD_SHA="$(printf '%s' "$MANIFEST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["payload_sha256"])')"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/mac-mcp-pkg.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"; STAGING="$ROOT/.mac-mcp/pkg/staging"
mkdir -p "$WORK/src" "$STAGING" "$ROOT/Applications"

echo "==> Source $SHORT ($RELEASE_ID)"
git -C "$REPO" archive --format=tar "$COMMIT" > "$STAGING/source.tar"
tar -xf "$STAGING/source.tar" -C "$WORK/src"
[[ -f "$WORK/src/packaging/pkg/pkg_install.py" ]] || { echo "$SHORT has no packaging/pkg; build a newer checkpoint" >&2; exit 1; }
cp "$WORK/src/packaging/pkg/pkg_install.py" "$STAGING/pkg_install.py"

echo "==> App"
"$WORK/src/menu_app/build_app.sh" "$WORK/app" >/dev/null
codesign --verify --deep --strict "$WORK/app/Mac MCP.app"
ditto "$WORK/app/Mac MCP.app" "$ROOT/Applications/Mac MCP.app"

echo "==> Python $PY_VERSION"
CACHE="$REPO/dist/cache"; mkdir -p "$CACHE"
PY_TGZ="$CACHE/cpython-$PY_VERSION-20260924-aarch64-apple-darwin-install_only_stripped.tar.gz"
if [[ ! -f "$PY_TGZ" ]]; then
  curl -fsSL "$PY_URL" -o "$PY_TGZ.part"
  mv -f "$PY_TGZ.part" "$PY_TGZ"
fi
echo "$PY_SHA256  $PY_TGZ" | shasum -a 256 -c - >/dev/null || { echo "Python archive checksum mismatch" >&2; exit 1; }
tar -xzf "$PY_TGZ" -C "$STAGING"   # creates $STAGING/python
"$STAGING/python/bin/python3" -c 'import sys; assert sys.version.startswith("'"$PY_VERSION"'")'

echo "==> Wheels"
plats=(); for v in 11_0 12_0 13_0 14_0; do plats+=(--platform "macosx_${v}_arm64" --platform "macosx_${v}_universal2"); done
"$STAGING/python/bin/python3" -m pip download --quiet --disable-pip-version-check --only-binary=:all: \
  "${plats[@]}" --platform any --python-version 3.14 --implementation cp --abi cp314 --abi abi3 --abi none \
  --require-hashes -r "$WORK/src/requirements.lock" -d "$STAGING/wheelhouse"
find "$STAGING/python" -name __pycache__ -type d -prune -exec rm -rf {} +

python3 - "$STAGING" "$RELEASE_ID" "$VERSION" "$COMMIT" "$PAYLOAD_SHA" "$PY_VERSION" <<'PY'
import hashlib, json, sys, time
from pathlib import Path
staging, release_id, version, commit, payload, py = sys.argv[1:]
src = Path(staging) / "source.tar"
(Path(staging) / "release.json").write_text(json.dumps({
    "release_id": release_id, "version": version, "commit": commit, "payload_sha256": payload,
    "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "python_version": py, "arch": "arm64",
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2, sort_keys=True) + "\n")
PY

if [[ -n "$STAGE_ONLY" ]]; then
  mkdir -p "$STAGE_ONLY"
  ditto "$ROOT" "$STAGE_ONLY"
  echo "Staged home tree: $STAGE_ONLY"
  exit 0
fi

echo "==> Package"
pkgbuild --analyze --root "$ROOT" "$WORK/component.plist" >/dev/null
# Never let Installer "relocate" the payload onto another copy of the app found elsewhere.
python3 - "$WORK/component.plist" <<'PLIST'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as handle:
    bundles = plistlib.load(handle)
for bundle in bundles:
    bundle["BundleIsRelocatable"] = False
with open(path, "wb") as handle:
    plistlib.dump(bundles, handle)
PLIST
chmod 755 "$WORK/src/packaging/pkg/scripts/preinstall" "$WORK/src/packaging/pkg/scripts/postinstall"
pkgbuild --quiet --root "$ROOT" --component-plist "$WORK/component.plist" \
  --scripts "$WORK/src/packaging/pkg/scripts" --identifier "$PKG_ID" --version "$VERSION" \
  --install-location / "$WORK/mac-mcp-component.pkg"
sed -e "s/@VERSION@/$VERSION/" -e "s/@COMPONENT_PKG@/mac-mcp-component.pkg/" \
  "$WORK/src/packaging/pkg/distribution.xml" > "$WORK/distribution.xml"
mkdir -p "$REPO/dist"
OUT="$REPO/dist/Mac-MCP-$VERSION-$RELEASE_ID-arm64.pkg"
productbuild --quiet --distribution "$WORK/distribution.xml" --package-path "$WORK" "$OUT"
(cd "$REPO/dist" && shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256")
du -h "$OUT"
echo "$OUT"
```

- [ ] **Step 2: Ignore build output and document it**

Append `dist/` to `.gitignore`. Add to `release/README.md`:

```markdown
## Package installer

After a stable checkpoint is signed and verified, build the Apple Silicon package from it:
`scripts/build_pkg.sh` (defaults to HEAD; refuses a commit that is not a verified checkpoint).
It writes `dist/Mac-MCP-<version>-<release_id>-arm64.pkg` and a `.sha256` file to attach to the
GitHub Release. The package is unsigned. Package installs update by installing the newer package.
```

Add to `README.md` under the install section:

```markdown
### Install with the package (Apple Silicon, macOS 14+)

Download `Mac-MCP-…-arm64.pkg` from GitHub Releases and open it. The package is not signed
with an Apple Developer ID yet, so macOS blocks the first open: go to System Settings →
Privacy & Security and choose **Open Anyway**. The package installs the menu bar app and
the server for your account and starts them; no Terminal, Xcode tools, Git or Python needed.
To update, install the newer package the same way. `install.sh` remains available for
developers.
```

- [ ] **Step 3: Build a stage-only tree and a package from HEAD**

Run: `scripts/build_pkg.sh --allow-unverified --stage-only "$CLAUDE_JOB_DIR/tmp/pkg-stage/home"`
Expected: ends with `Staged home tree: …`; `…/home/.mac-mcp/pkg/staging/wheelhouse` holds 72 wheels; `release.json` shows `unverified-<sha>`.

Run: `scripts/build_pkg.sh --allow-unverified`
Expected: prints a size around 90–120M and the `dist/…pkg` path.

- [ ] **Step 4: Inspect the package**

```bash
P="$(ls -t dist/*.pkg | head -1)"
pkgutil --check-signature "$P"            # expect: Status: no signature
pkgutil --payload-files "$P" | head        # expect ./Applications/Mac MCP.app/... and ./.mac-mcp/pkg/staging/...
X="$CLAUDE_JOB_DIR/tmp/pkg-expand"; rm -rf "$X"; pkgutil --expand-full "$P" "$X"
grep -c "<relocate>" "$X"/mac-mcp-component.pkg/PackageInfo || true   # expect a relocate block with no bundle entries
grep -o 'enable_currentUserHome="true"' "$X/Distribution"
codesign --verify --deep --strict "$X/mac-mcp-component.pkg/Payload/Applications/Mac MCP.app"
```

Expected: no signature; both payload trees present; `enable_currentUserHome="true"`; app verifies. If `PackageInfo` lists `<bundle ... id="com.bulutarkan.mac-mcp.menu" ...>` under `<relocate>`, the component plist edit failed: fix before continuing.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_pkg.sh .gitignore release/README.md README.md
git commit -m "Build script for the unsigned Apple Silicon package"
```

---

### Task 7: Real offline install from the staged tree (temp HOME, port 8878)

**Files:**
- Create: `tests/test_pkg_staged_install.py` (opt-in, `MAC_MCP_PKG_STAGE=<staged home tree>`)

**Interfaces:**
- Consumes: Task 6 `--stage-only` output, Task 3–4 `pkg_install.main` (via the bundled Python with `--no-start`).

- [ ] **Step 1: Write the opt-in integration test**

```python
# tests/test_pkg_staged_install.py
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
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "MAC_MCP_STATE_DIR": str(home / ".mac-mcp"),
               "MAC_MCP_PORT": str(PORT), "MAC_MCP_HOST": "127.0.0.1"}
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
```

- [ ] **Step 2: Record the production baseline**

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN -t; curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/health
shasum -a 256 ~/.mac-mcp/settings.json ~/mac-mcp/mcp_server/.env | awk '{print $1}' > "$CLAUDE_JOB_DIR/tmp/pkg-baseline.txt"
```

- [ ] **Step 3: Run it**

Run: `MAC_MCP_PKG_STAGE="$CLAUDE_JOB_DIR/tmp/pkg-stage/home" ... -m unittest tests.test_pkg_staged_install -v`
Expected: PASS (first install takes ~1 minute: venv + 72 wheels offline).

- [ ] **Step 4: Confirm production untouched**

Same PID and health 200 as Step 2; `shasum` lines identical; `lsof -nP -iTCP:8878 -sTCP:LISTEN` empty.

- [ ] **Step 5: Run the full suite, then commit**

Run the full suite (Global Constraints command with `discover -s tests`). Expected: only the known environmental native failures (`test_native_*`, `test_context_handoff`, `test_security_routing_bundle`).

```bash
git diff --check
git add tests/test_pkg_staged_install.py
git commit -m "Opt-in test: real offline install and upgrade from a staged package"
git push origin main
```

---

### Task 8: Real end-to-end on a second macOS account (manual gate with Tarkan)

No code. Needs Tarkan to create a standard user account (System Settings → Users & Groups), because installing on his own account would replace the production runtime.

- [ ] **Step 1:** Build `scripts/build_pkg.sh --allow-unverified`, upload the `.pkg` somewhere the test account can download it through Safari (so it carries the quarantine flag), for example a draft GitHub Release asset.
- [ ] **Step 2 (Tarkan, test account):** open the pkg → expect the "not verified" block → System Settings → Privacy & Security → Open Anyway → Installer offers "Install for me only" → install completes without an admin password.
- [ ] **Step 3 (Tarkan, test account):** menu bar app appears; `~/.local/bin/mac-mcp status` and `curl -s http://127.0.0.1:8000/health` show healthy; `ls -ln ~/mac-mcp | head` shows the test account's uid, not 501. Note: the production server on Tarkan's account listens on 8765, the test account's on 8000, so they do not collide.
- [ ] **Step 4 (Tarkan, test account):** open the same pkg again (upgrade) → completes; `~/mac-mcp/mcp_server/.env` unchanged (compare `shasum` before/after).
- [ ] **Step 5:** from Tarkan's own account, confirm production unchanged (PID, 8765 health, public health).
- [ ] **Step 6:** if home-domain install misbehaves (no "for me only" option, files owned by root, scripts run as root), stop and record it: the fallback in spec §7 (system-domain pkg running per-user steps as the console user) becomes a new task.
- [ ] **Step 7:** update roadmap rows #58 (row 58), #59 (row 59), #132 (row 130) and #173 (row 171) with a cross-reference note; mark nothing done that the E2E did not prove.
