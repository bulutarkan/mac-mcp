from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .update_state import backups_root, read_deployed_commit, write_deployed_commit, write_update_state

DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
DEFAULT_LAUNCHD_LABEL = "mac-mcp-uvicorn"
DEFAULT_PORT = 8000


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    repo: str
    runtime: str
    branch: str
    remote: str
    deployed_commit: str
    repo_commit: str
    target_commit: str
    behind_by: int
    update_available: bool
    dirty: bool


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise UpdateError(f"Command failed ({' '.join(cmd)}): {detail}")
    return proc


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    return _run(["git", "-C", str(repo), *args], check=check, timeout=timeout).stdout.strip()


def _short(commit: str) -> str:
    return commit[:8] if commit else "unknown"


def _default_repo() -> Path:
    env = os.getenv("MAC_MCP_REPO", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    preferred = Path.home() / "Projects" / "mac-mcp"
    if (preferred / ".git").exists():
        return preferred.resolve()
    package_root = Path(__file__).resolve().parent.parent
    if (package_root / ".git").exists():
        return package_root.resolve()
    return preferred.resolve()


def _default_runtime(repo: Path) -> Path:
    env = os.getenv("MAC_MCP_RUNTIME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    preferred = Path.home() / "mac-mcp"
    if (preferred / "mcp_server").exists():
        return preferred.resolve()
    return repo.resolve()


def resolve_paths(repo: str | None = None, runtime: str | None = None) -> tuple[Path, Path]:
    repo_path = Path(repo).expanduser().resolve() if repo else _default_repo()
    runtime_path = Path(runtime).expanduser().resolve() if runtime else _default_runtime(repo_path)
    return repo_path, runtime_path


def _read_state_commit(runtime: Path) -> str | None:
    return read_deployed_commit(runtime)


def _write_state_commit(runtime: Path, commit: str) -> None:
    del runtime
    write_deployed_commit(commit)


def _write_update_state(runtime: Path, payload: dict) -> None:
    del runtime
    write_update_state(payload)

def check_update(
    repo: str | Path | None = None,
    runtime: str | Path | None = None,
    branch: str = DEFAULT_BRANCH,
    remote: str = DEFAULT_REMOTE,
    fetch: bool = True,
) -> UpdateInfo:
    repo_path, runtime_path = resolve_paths(str(repo) if repo else None, str(runtime) if runtime else None)
    if not (repo_path / ".git").exists():
        raise UpdateError(f"Repository not found or is not a Git checkout: {repo_path}")
    if not (runtime_path / "mcp_server").exists():
        raise UpdateError(f"Runtime directory not found: {runtime_path}")

    dirty = bool(_git(repo_path, "status", "--porcelain"))
    if fetch:
        _git(repo_path, "fetch", "--quiet", remote, branch, timeout=120)
    repo_commit = _git(repo_path, "rev-parse", "HEAD")
    target = _git(repo_path, "rev-parse", f"{remote}/{branch}")
    deployed = _read_state_commit(runtime_path) or repo_commit

    if _git(repo_path, "cat-file", "-t", deployed, check=False) != "commit":
        raise UpdateError(f"Deployed commit is not available in the repository: {deployed}")
    ancestry = _run(["git", "-C", str(repo_path), "merge-base", "--is-ancestor", deployed, target], check=False)
    if ancestry.returncode != 0:
        raise UpdateError(
            f"Cannot fast-forward deployed commit {_short(deployed)} to {_short(target)}. "
            "The update history diverged; update manually."
        )
    behind_text = _git(repo_path, "rev-list", "--count", f"{deployed}..{target}") or "0"
    return UpdateInfo(
        repo=str(repo_path), runtime=str(runtime_path), branch=branch, remote=remote,
        deployed_commit=deployed, repo_commit=repo_commit, target_commit=target,
        behind_by=int(behind_text), update_available=(deployed != target), dirty=dirty,
    )


def format_check(info: UpdateInfo) -> str:
    lines = [
        "Mac MCP Update Check",
        f"Repository: {info.repo}",
        f"Runtime: {info.runtime}",
        f"Branch: {info.remote}/{info.branch}",
        f"Installed commit: {_short(info.deployed_commit)}",
        f"Latest commit: {_short(info.target_commit)}",
    ]
    if info.dirty:
        lines.append("Status: Update blocked because the repository has local changes.")
    elif info.update_available:
        plural = "commit" if info.behind_by == 1 else "commits"
        lines.append(f"Status: Update available ({info.behind_by} {plural} behind).")
        lines.append("Run: mac-mcp update")
    else:
        lines.append("Status: Mac MCP is up to date.")
    return "\n".join(lines)


def _tracked_files(repo: Path, commit: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", "mcp_server", "menu_app")
    return [line for line in out.splitlines() if line and not line.endswith("/")]


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _prepare_runtime_merge(repo: Path, runtime: Path, deployed: str, target: str, temp_root: Path) -> tuple[Path, int]:
    stage = temp_root / "runtime-merge"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(stage), deployed)
    old_files = _tracked_files(repo, deployed)
    overlay_count = 0
    for rel in old_files:
        src = runtime / rel
        dst = stage / rel
        if src.exists() and src.is_file():
            before = dst.read_bytes() if dst.exists() else None
            data = src.read_bytes()
            if before != data:
                _copy_file(src, dst)
                overlay_count += 1
    if overlay_count:
        _git(stage, "config", "user.email", "mac-mcp-updater@localhost")
        _git(stage, "config", "user.name", "Mac MCP Updater")
        add_paths = ["mcp_server"]
        if (stage / "menu_app").exists():
            add_paths.append("menu_app")
        _git(stage, "add", "--", *add_paths)
        _git(stage, "commit", "--quiet", "-m", "runtime overlay")
    merge = _run(["git", "-C", str(stage), "merge", "--no-edit", "--no-ff", target], check=False, timeout=120)
    if merge.returncode != 0:
        conflicts = _git(stage, "diff", "--name-only", "--diff-filter=U", check=False)
        detail = conflicts.replace("\n", ", ") if conflicts else (merge.stderr or merge.stdout).strip()
        _git(repo, "worktree", "remove", "--force", str(stage), check=False)
        raise UpdateError(f"Runtime customizations conflict with the update: {detail}")
    return stage, overlay_count


def _backup_runtime(repo: Path, runtime: Path, deployed: str, target: str) -> tuple[Path, list[str], list[str]]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backups_root() / f"{stamp}-{_short(deployed)}"
    backup.mkdir(parents=True, exist_ok=True)
    old_files = _tracked_files(repo, deployed)
    new_files = _tracked_files(repo, target)
    existing: list[str] = []
    for rel in sorted(set(old_files) | set(new_files)):
        src = runtime / rel
        if src.exists() and src.is_file():
            _copy_file(src, backup / rel)
            existing.append(rel)
    manifest = {
        "deployed_commit": deployed,
        "target_commit": target,
        "existing_files": existing,
        "old_files": old_files,
        "new_files": new_files,
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return backup, old_files, new_files


def _sync_runtime(stage: Path, runtime: Path, old_files: Iterable[str], new_files: Iterable[str]) -> int:
    old_set, new_set = set(old_files), set(new_files)
    count = 0
    for rel in sorted(new_set):
        src = stage / rel
        if not src.exists() or not src.is_file():
            continue
        _copy_file(src, runtime / rel)
        count += 1
    for rel in sorted(old_set - new_set):
        path = runtime / rel
        if path.exists() and path.is_file():
            path.unlink()
    return count


def _restore_runtime(runtime: Path, backup: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    existing = set(manifest.get("existing_files", []))
    new_files = set(manifest.get("new_files", []))
    old_files = set(manifest.get("old_files", []))
    for rel in new_files - old_files:
        path = runtime / rel
        if path.exists() and rel not in existing:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
    for rel in existing:
        src = backup / rel
        if src.exists():
            _copy_file(src, runtime / rel)


def _env_values(runtime: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = runtime / "mcp_server" / ".env"
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _detect_port(runtime: Path) -> int:
    env = _env_values(runtime)
    try:
        if env.get("MAC_MCP_PORT"):
            return int(env["MAC_MCP_PORT"])
    except ValueError:
        pass
    ps = _run(["ps", "-axo", "command="], check=False).stdout
    for line in ps.splitlines():
        if "uvicorn mcp_server.main:app" not in line:
            continue
        match = re.search(r"--port(?:=|\s+)[\"']?(\d+)", line)
        if match:
            return int(match.group(1))
    return DEFAULT_PORT


def _detect_host(runtime: Path) -> str:
    env = _env_values(runtime)
    host = env.get("MAC_MCP_HOST", "").strip()
    if host and host not in {"0.0.0.0", "::"}:
        return host
    return "127.0.0.1"


def _launchd_loaded(label: str) -> bool:
    uid = os.getuid()
    return _run(["launchctl", "print", f"gui/{uid}/{label}"], check=False).returncode == 0


def _restart_launchd(label: str) -> None:
    uid = os.getuid()
    proc = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False, timeout=30)
    if proc.returncode != 0:
        raise UpdateError((proc.stderr or proc.stdout or f"Could not restart launchd service {label}").strip())


def _restart_cli(runtime: Path, host: str, port: int) -> None:
    state_dir = Path.home() / ".mac-mcp"
    pid_file = state_dir / "mac-mcp.pid"
    log_file = state_dir / "mac-mcp.log"
    if not pid_file.exists():
        raise UpdateError("Could not determine how Mac MCP is managed. Restart the service manually.")
    try:
        old_pid = int(pid_file.read_text().strip())
        os.kill(old_pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError):
        pass
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            os.kill(old_pid, 0)
        except (ProcessLookupError, UnboundLocalError):
            break
        time.sleep(0.2)
    python = runtime / ".venv" / "bin" / "python"
    if not python.exists():
        raise UpdateError(f"Runtime Python was not found: {python}")
    state_dir.mkdir(parents=True, exist_ok=True)
    log = log_file.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "mcp_server.main:app", "--host", host, "--port", str(port)],
        cwd=str(runtime), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid) + "\n")


def _restart_service(runtime: Path, label: str) -> str:
    port = _detect_port(runtime)
    host = _detect_host(runtime)
    if _launchd_loaded(label):
        _restart_launchd(label)
        return f"http://{host}:{port}/health"
    _restart_cli(runtime, host, port)
    return f"http://{host}:{port}/health"


def _health_ok(url: str, attempts: int = 30, delay: float = 0.4) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8", errors="replace"))
                    if data.get("ok") is True:
                        return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def _refresh_installed_menu_app(runtime: Path) -> bool:
    installer = runtime / "menu_app" / "install_app.sh"
    installed = [Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")]
    if not installer.exists() or not any(path.exists() for path in installed):
        return False
    _run([str(installer)], timeout=180)
    return True


def _deps_changed(repo: Path, deployed: str, target: str) -> bool:
    changed = _git(repo, "diff", "--name-only", deployed, target, "--", "mcp_server/requirements.txt")
    return bool(changed.strip())


def apply_update(
    repo: str | Path | None = None,
    runtime: str | Path | None = None,
    branch: str = DEFAULT_BRANCH,
    remote: str = DEFAULT_REMOTE,
    launchd_label: str = DEFAULT_LAUNCHD_LABEL,
    skip_restart: bool = False,
    skip_deps: bool = False,
    deferred_seconds: float = 0.0,
) -> dict:
    if deferred_seconds > 0:
        time.sleep(deferred_seconds)
    repo_path, runtime_path = resolve_paths(str(repo) if repo else None, str(runtime) if runtime else None)
    print("[mac-mcp update] Checking repository...", flush=True)
    info = check_update(repo_path, runtime_path, branch=branch, remote=remote, fetch=True)
    print(f"[mac-mcp update] Current deployed commit: {_short(info.deployed_commit)}", flush=True)
    print(f"[mac-mcp update] Latest {remote}/{branch}: {_short(info.target_commit)}", flush=True)
    if info.dirty:
        raise UpdateError("Repository has local changes. Commit or stash them before updating.")
    if not info.update_available:
        print("[mac-mcp update] Mac MCP is already up to date.", flush=True)
        return {"ok": True, "updated": False, **asdict(info)}

    temp_root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-"))
    stage: Optional[Path] = None
    backup: Optional[Path] = None
    health_url: Optional[str] = None
    deps_changed = _deps_changed(repo_path, info.deployed_commit, info.target_commit)
    try:
        print("[mac-mcp update] Preparing runtime merge...", flush=True)
        stage, overlay_count = _prepare_runtime_merge(
            repo_path, runtime_path, info.deployed_commit, info.target_commit, temp_root
        )
        print(f"[mac-mcp update] Runtime customizations detected: {overlay_count} file(s).", flush=True)
        print("[mac-mcp update] Runtime merge check passed.", flush=True)

        backup, old_files, new_files = _backup_runtime(
            repo_path, runtime_path, info.deployed_commit, info.target_commit
        )
        print(f"[mac-mcp update] Runtime backup: {backup}", flush=True)

        current_branch = _git(repo_path, "branch", "--show-current")
        if current_branch != branch:
            raise UpdateError(f"Repository must be on branch '{branch}', currently on '{current_branch or 'detached HEAD'}'.")
        print("[mac-mcp update] Updating repository (fast-forward)...", flush=True)
        _git(repo_path, "merge", "--ff-only", info.target_commit)

        synced = _sync_runtime(stage, runtime_path, old_files, new_files)
        print(f"[mac-mcp update] Synced {synced} managed runtime file(s).", flush=True)

        if _refresh_installed_menu_app(runtime_path):
            print("[mac-mcp update] Refreshed installed Mac MCP menu bar app.", flush=True)

        if deps_changed and not skip_deps:
            python = runtime_path / ".venv" / "bin" / "python"
            requirements = runtime_path / "mcp_server" / "requirements.txt"
            if not python.exists():
                raise UpdateError(f"Runtime Python was not found: {python}")
            print("[mac-mcp update] Installing updated dependencies...", flush=True)
            _run([str(python), "-m", "pip", "install", "-r", str(requirements)], timeout=300)
        elif deps_changed:
            print("[mac-mcp update] Dependency installation skipped (test mode).", flush=True)
        else:
            print("[mac-mcp update] Dependencies unchanged.", flush=True)

        if skip_restart:
            print("[mac-mcp update] Service restart skipped (test mode).", flush=True)
        else:
            print("[mac-mcp update] Restarting Mac MCP...", flush=True)
            health_url = _restart_service(runtime_path, launchd_label)
            if not _health_ok(health_url):
                raise UpdateError(f"Health check failed after restart: {health_url}")
            print(f"[mac-mcp update] Health check passed: {health_url}", flush=True)

        _write_state_commit(runtime_path, info.target_commit)
        result = {
            "ok": True, "updated": True,
            "from_commit": info.deployed_commit, "to_commit": info.target_commit,
            "from_short": _short(info.deployed_commit), "to_short": _short(info.target_commit),
            "backup": str(backup), "synced_files": synced, "health_url": health_url,
        }
        _write_update_state(runtime_path, {"status": "completed", **result})
        print(f"[mac-mcp update] Update complete: {_short(info.deployed_commit)} -> {_short(info.target_commit)}", flush=True)
        return result
    except Exception as exc:
        message = str(exc)
        print(f"[mac-mcp update] ERROR: {message}", flush=True)
        if backup is not None:
            try:
                print("[mac-mcp update] Restoring the previous runtime...", flush=True)
                _restore_runtime(runtime_path, backup)
                _write_state_commit(runtime_path, info.deployed_commit)
                try:
                    _refresh_installed_menu_app(runtime_path)
                except Exception as menu_exc:
                    print(f"[mac-mcp update] WARNING: menu app rollback refresh failed: {menu_exc}", flush=True)
                if not skip_restart:
                    try:
                        rollback_health = _restart_service(runtime_path, launchd_label)
                        if _health_ok(rollback_health):
                            print("[mac-mcp update] Rollback health check passed.", flush=True)
                        else:
                            print("[mac-mcp update] WARNING: rollback health check failed.", flush=True)
                    except Exception as restart_exc:
                        print(f"[mac-mcp update] WARNING: rollback restart failed: {restart_exc}", flush=True)
                print("[mac-mcp update] Runtime rollback completed.", flush=True)
            except Exception as rollback_exc:
                print(f"[mac-mcp update] WARNING: runtime rollback failed: {rollback_exc}", flush=True)
        _write_update_state(runtime_path, {"status": "failed", "error": message})
        raise
    finally:
        if stage is not None:
            _git(repo_path, "worktree", "remove", "--force", str(stage), check=False)
        shutil.rmtree(temp_root, ignore_errors=True)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Update Mac MCP from the latest commit on a Git branch.")
    p.add_argument("--repo", default=None, help="Git repository path. Defaults to ~/Projects/mac-mcp.")
    p.add_argument("--runtime", default=None, help="Runtime path. Defaults to ~/mac-mcp.")
    p.add_argument("--branch", default=DEFAULT_BRANCH)
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument("--check", action="store_true", help="Check for updates without changing files.")
    p.add_argument("--skip-restart", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-deps", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--deferred-seconds", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--launchd-label", default=os.getenv("MAC_MCP_LAUNCHD_LABEL", DEFAULT_LAUNCHD_LABEL), help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.check:
            info = check_update(args.repo, args.runtime, args.branch, args.remote, fetch=True)
            print(format_check(info))
            return 2 if info.dirty else 0
        apply_update(
            repo=args.repo, runtime=args.runtime, branch=args.branch, remote=args.remote,
            launchd_label=args.launchd_label, skip_restart=args.skip_restart,
            skip_deps=args.skip_deps, deferred_seconds=args.deferred_seconds,
        )
        return 0
    except UpdateError as exc:
        print(f"mac-mcp update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
