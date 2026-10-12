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
