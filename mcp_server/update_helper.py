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

if __package__:
    from . import release_trust
    from .managed_process import (
        matches_role,
        migrate_legacy_record,
        process_snapshot,
        validate_process_record,
        write_process_record,
    )
    from .update_state import backups_root, read_deployed_commit, update_root, write_deployed_commit, write_update_state
else:
    # The detached updater is launched as a staged standalone script. Keep the
    # staged siblings ahead of the repo and site-packages on sys.path so the
    # helper cannot accidentally load unrelated modules.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_trust
    from managed_process import (
        matches_role,
        migrate_legacy_record,
        process_snapshot,
        validate_process_record,
        write_process_record,
    )
    from update_state import backups_root, read_deployed_commit, update_root, write_deployed_commit, write_update_state

DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
DEFAULT_LAUNCHD_LABEL = "mac-mcp-uvicorn"
DEFAULT_PORT = 8000
_STAGING_DIR_RE = re.compile(r"^mac-mcp-update-upd_[0-9a-f]{10}-[a-z0-9_]+$")


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
    release_verified: bool = False
    release_id: str | None = None
    release_version: str | None = None
    release_payload_sha256: str | None = None
    release_signer_fingerprint: str | None = None
    release_file_count: int = 0
    release_artifact_count: int = 0
    branch_tip_commit: str | None = None
    unverified_ahead: int = 0


@dataclass
class DependencyEnvironmentTransaction:
    runtime: Path
    staging_root: Path
    staged_env: Path
    previous_backup: Path
    marker_token: str


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

def _release_marker_state(repo: Path, commit: str) -> tuple[bool, bool]:
    changed = _git(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        commit,
        "--",
        release_trust.MANIFEST_RELPATH,
        release_trust.SIGNATURE_RELPATH,
    )
    paths = {line.strip() for line in changed.splitlines() if line.strip()}
    return (
        release_trust.MANIFEST_RELPATH in paths,
        release_trust.SIGNATURE_RELPATH in paths,
    )


def _latest_verified_release(
    repo: Path,
    *,
    deployed: str,
    branch_tip: str,
    branch: str,
    max_commits: int = 512,
) -> tuple[str, release_trust.VerifiedRelease | None]:
    if deployed == branch_tip:
        return deployed, None
    candidates_text = _git(
        repo,
        "rev-list",
        "--first-parent",
        f"--max-count={max_commits}",
        f"{deployed}..{branch_tip}",
    )
    for candidate in candidates_text.splitlines():
        candidate = candidate.strip()
        if not candidate:
            continue
        has_manifest, has_signature = _release_marker_state(repo, candidate)
        if not has_manifest and not has_signature:
            continue
        if has_manifest != has_signature:
            raise UpdateError(
                f"Verified release channel is blocked at {_short(candidate)}: "
                "release manifest/signature pair is incomplete."
            )
        try:
            verified = release_trust.verify_release_commit(
                repo,
                candidate,
                expected_branch=branch,
            )
        except release_trust.ReleaseVerificationError as exc:
            raise UpdateError(
                f"Verified release channel is blocked at {_short(candidate)}: {exc}. "
                "The updater will not modify the repository or runtime."
            ) from exc
        return candidate, verified
    total_ahead = int(_git(repo, "rev-list", "--count", f"{deployed}..{branch_tip}") or "0")
    if total_ahead > max_commits:
        raise UpdateError(
            f"No verified stable release was found within the newest {max_commits} commits "
            f"ahead of deployed {_short(deployed)}; refusing to scan an unbounded history."
        )
    return deployed, None


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
    branch_tip = _git(repo_path, "rev-parse", f"{remote}/{branch}")
    deployed = _read_state_commit(runtime_path) or repo_commit

    if _git(repo_path, "cat-file", "-t", deployed, check=False) != "commit":
        raise UpdateError(f"Deployed commit is not available in the repository: {deployed}")
    ancestry = _run(
        ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", deployed, branch_tip],
        check=False,
    )
    if ancestry.returncode != 0:
        raise UpdateError(
            f"Cannot fast-forward deployed commit {_short(deployed)} to branch tip {_short(branch_tip)}. "
            "The update history diverged; update manually."
        )

    target, verified_release = _latest_verified_release(
        repo_path,
        deployed=deployed,
        branch_tip=branch_tip,
        branch=branch,
    )
    behind_text = _git(repo_path, "rev-list", "--count", f"{deployed}..{target}") or "0"
    unverified_ahead_text = _git(repo_path, "rev-list", "--count", f"{target}..{branch_tip}") or "0"
    release_fields: dict[str, object] = {}
    if verified_release is not None:
        release_fields = {
            "release_verified": True,
            "release_id": verified_release.release_id,
            "release_version": verified_release.version,
            "release_payload_sha256": verified_release.payload_sha256,
            "release_signer_fingerprint": verified_release.signer_fingerprint,
            "release_file_count": verified_release.file_count,
            "release_artifact_count": verified_release.artifact_count,
        }
    return UpdateInfo(
        repo=str(repo_path), runtime=str(runtime_path), branch=branch, remote=remote,
        deployed_commit=deployed, repo_commit=repo_commit, target_commit=target,
        behind_by=int(behind_text), update_available=(deployed != target), dirty=dirty,
        branch_tip_commit=branch_tip,
        unverified_ahead=int(unverified_ahead_text),
        **release_fields,
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
        lines.append(f"Status: Verified update available ({info.behind_by} {plural} behind).")
        if info.release_id:
            lines.append(f"Verified release: {info.release_id} (v{info.release_version or 'unknown'})")
        if info.release_signer_fingerprint:
            lines.append(f"Release signer: {info.release_signer_fingerprint}")
        lines.append("Run: mac-mcp update")
    elif info.unverified_ahead:
        plural = "commit" if info.unverified_ahead == 1 else "commits"
        lines.append(
            f"Status: No newer verified stable release "
            f"({info.unverified_ahead} development/unverified {plural} ahead)."
        )
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


def _settings_payload() -> tuple[Path, dict]:
    configured = os.getenv("MAC_MCP_SETTINGS_PATH", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".mac-mcp" / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return path, payload if isinstance(payload, dict) else {}


def _effective_env_value(env: dict[str, str], name: str) -> str | None:
    # The running server loads runtime .env into its process environment, so an
    # explicit inherited value is the best representation of what a managed
    # restart will see. Fall back to the runtime file for standalone updater use.
    inherited = os.getenv(name)
    if inherited is not None:
        return inherited.strip()
    value = env.get(name)
    return value.strip() if value is not None else None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_public_exposure(runtime: Path, env: dict[str, str]) -> tuple[str | None, str]:
    mode = (_effective_env_value(env, "MAC_MCP_PUBLIC_ENDPOINT_MODE") or "").strip().lower()
    aliases = {"off": "none", "local": "none", "local_only": "none", "tunnel": "cloudflare"}
    mode = aliases.get(mode, mode)
    if mode in {"ngrok", "cloudflare", "custom"}:
        return mode, "environment"

    settings_path, payload = _settings_payload()
    server = payload.get("server", {}) if isinstance(payload, dict) else {}
    if isinstance(server, dict):
        configured = str(server.get("public_endpoint_mode") or "").strip().lower()
        configured = aliases.get(configured, configured)
        if configured in {"ngrok", "cloudflare", "custom"}:
            return configured, f"settings:{settings_path}"
        if not configured and bool(server.get("ngrok_on_start")):
            return "ngrok", f"settings_legacy:{settings_path}"

    domain = (_effective_env_value(env, "NGROK_DOMAIN") or "").strip()
    if domain:
        return "ngrok", "legacy_ngrok_domain"

    host = (_effective_env_value(env, "MAC_MCP_HOST") or "127.0.0.1").strip().lower().strip("[]")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        return "non_loopback", "host"
    return None, "local"


def secure_bootstrap_update_blocker(runtime: Path) -> dict[str, object] | None:
    """Return a secret-safe blocker when the target secure bootstrap would not start.

    This intentionally never generates or prints a connector credential. An
    unauthenticated public connector cannot learn a new key automatically, so
    silently enabling auth during update would produce a healthy server while
    breaking the existing connector. Block before the repo/runtime swap instead.
    """
    env = _env_values(runtime)
    allow_raw = _effective_env_value(env, "MCP_ALLOW_NO_AUTH")
    api_key = _effective_env_value(env, "MCP_API_KEY") or ""
    allow_no_auth = _truthy(allow_raw)
    exposure, exposure_source = _legacy_public_exposure(runtime, env)

    code: str | None = None
    if allow_no_auth and exposure is not None:
        code = "LEGACY_NO_AUTH_PUBLIC_ENDPOINT"
    elif not allow_no_auth and not api_key:
        code = "MISSING_AUTH_CREDENTIAL"

    if code is None:
        return None

    env_path = runtime / "mcp_server" / ".env"
    if code == "LEGACY_NO_AUTH_PUBLIC_ENDPOINT":
        summary = (
            "Update blocked before runtime swap: this installation still allows unauthenticated "
            f"access while using {exposure or 'a public/non-loopback endpoint'}."
        )
    else:
        summary = (
            "Update blocked before runtime swap: the target secure bootstrap requires an MCP API key "
            "when unauthenticated access is not explicitly enabled for local-only use."
        )
    remediation = (
        f"Before updating, edit {env_path}: set a strong MCP_API_KEY and MCP_ALLOW_NO_AUTH=false. "
        "Update the MCP client/connector to send Authorization: Bearer <MCP_API_KEY>, or use "
        "?ApiKey=<MCP_API_KEY> only for a client that cannot send headers. Verify the current "
        "connector with authentication, then rerun mac-mcp update. The updater will never print "
        "or copy the credential into logs or update state."
    )
    return {
        "reason": "secure_bootstrap_migration_required",
        "code": code,
        "summary": summary,
        "remediation": remediation,
        "public_exposure": exposure,
        "exposure_source": exposure_source,
        "env_file": str(env_path),
        "api_key_configured": bool(api_key),
        "allow_no_auth": allow_no_auth,
    }


def _format_secure_bootstrap_blocker(blocker: dict[str, object]) -> str:
    return f"{blocker.get('summary')} {blocker.get('remediation')}"


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

    validation = validate_process_record(
        pid_file,
        "server",
        port=int(port),
        project_root=runtime,
    )
    if validation.legacy_match:
        validation = migrate_legacy_record(
            pid_file,
            "server",
            port=int(port),
            project_root=runtime,
            metadata={"port": int(port), "migrated_from": "legacy_pid", "ownership_source": "updater"},
        )

    old_pid = validation.pid
    if validation.status in {"dead", "missing", "invalid_record"}:
        pid_file.unlink(missing_ok=True)
        old_pid = None
    elif not validation.valid or old_pid is None:
        raise UpdateError(
            "Refusing to restart Mac MCP because the recorded PID identity could not be verified "
            f"({validation.reason})."
        )

    if old_pid is not None:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            old_pid = None
        except PermissionError as exc:
            raise UpdateError("Permission denied while stopping the verified Mac MCP process.") from exc

    deadline = time.time() + 8
    while old_pid is not None and time.time() < deadline:
        current = validate_process_record(
            pid_file,
            "server",
            port=int(port),
            project_root=runtime,
        )
        if current.status in {"dead", "missing"}:
            pid_file.unlink(missing_ok=True)
            old_pid = None
            break
        if current.status in {"identity_mismatch", "role_mismatch"}:
            # Original process is gone and PID was reused. Never signal the replacement.
            pid_file.unlink(missing_ok=True)
            old_pid = None
            break
        if current.status == "unverifiable":
            raise UpdateError("Mac MCP process identity became unverifiable while stopping it.")
        time.sleep(0.2)

    if old_pid is not None:
        current = validate_process_record(
            pid_file,
            "server",
            port=int(port),
            project_root=runtime,
        )
        if current.valid:
            raise UpdateError("Verified Mac MCP process did not stop within 8 seconds; refusing unsafe restart.")
        pid_file.unlink(missing_ok=True)

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

    snapshot = None
    deadline = time.time() + 2
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        candidate = process_snapshot(proc.pid)
        if candidate and matches_role(
            candidate,
            "server",
            port=int(port),
            project_root=runtime,
        ):
            snapshot = candidate
            break
        time.sleep(0.1)

    if snapshot is None:
        try:
            proc.terminate()
        except OSError:
            pass
        raise UpdateError("Restarted Mac MCP process identity could not be verified.")

    try:
        write_process_record(
            pid_file,
            "server",
            proc.pid,
            metadata={"port": int(port), "ownership_source": "updater"},
            snapshot=snapshot,
        )
    except (OSError, RuntimeError) as exc:
        try:
            proc.terminate()
        except OSError:
            pass
        raise UpdateError("Could not persist verified Mac MCP process identity after restart.") from exc


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


def _health_gate_report_path() -> Path:
    return update_root() / "health-gate.json"


def _read_health_gate_report() -> dict | None:
    try:
        payload = json.loads(_health_gate_report_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _target_supports_health_gate(repo: Path, target_commit: str) -> bool:
    proc = _run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{target_commit}:mcp_server/post_update_health.py"],
        check=False,
        timeout=15,
    )
    return proc.returncode == 0


def _gate_failure_summary(report: dict | None) -> str | None:
    if not isinstance(report, dict):
        return None
    failures = report.get("critical_failures")
    if not isinstance(failures, list) or not failures:
        return None
    safe = [str(item)[:120] for item in failures[:8]]
    return ", ".join(safe)


def _menu_app_candidates() -> list[Path]:
    explicit = os.getenv("MAC_MCP_APP_PATH", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([
        Path.home() / "Applications" / "Mac MCP.app",
        Path("/Applications/Mac MCP.app"),
    ])
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _menu_app_target() -> Path:
    # Public installs are per-user and install_app.sh defaults here. Keep the
    # updater on the same canonical target unless an explicit override is set.
    return _menu_app_candidates()[0]


def _menu_app_process_pids(app_path: Path) -> list[int]:
    executable = str((app_path / "Contents" / "MacOS" / "MacMCPMenu").resolve())
    proc = _run(["/bin/ps", "-axo", "pid=,command="], check=False, timeout=10)
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fields = stripped.split(None, 1)
        if len(fields) != 2 or fields[1] != executable:
            continue
        try:
            pids.append(int(fields[0]))
        except ValueError:
            continue
    return pids


def _stop_menu_app(app_path: Path) -> None:
    pids = _menu_app_process_pids(app_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        remaining = set(_menu_app_process_pids(app_path))
        if not any(pid in remaining for pid in pids):
            return
        time.sleep(0.1)
    remaining = set(_menu_app_process_pids(app_path))
    for pid in pids:
        if pid not in remaining:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _start_menu_app(app_path: Path) -> None:
    _run(["/usr/bin/open", "-g", "-n", str(app_path)], timeout=20)
    for _ in range(50):
        if _menu_app_process_pids(app_path):
            return
        time.sleep(0.2)
    raise UpdateError(f"Mac MCP menu bar app did not start: {app_path}")


def _refresh_installed_menu_app(runtime: Path) -> bool:
    if os.getenv("MAC_MCP_SKIP_MENU_APP_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    installer = runtime / "menu_app" / "install_app.sh"
    if not installer.exists():
        return False
    target = _menu_app_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    _stop_menu_app(target)
    _run(["/usr/bin/env", "MAC_MCP_MENU_APP_LIFECYCLE_EXTERNAL=1", str(installer), str(target)], timeout=180)
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)], timeout=30)
    _start_menu_app(target)
    return True


def _deps_changed(repo: Path, deployed: str, target: str) -> bool:
    changed = _git(repo, "diff", "--name-only", deployed, target, "--", "mcp_server/requirements.txt")
    return bool(changed.strip())


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _clone_dependency_environment(source: Path, destination: Path) -> None:
    if not source.exists():
        raise UpdateError(f"Runtime virtual environment was not found: {source}")
    source = source.resolve()
    if not source.is_dir():
        raise UpdateError(f"Runtime virtual environment is not a directory: {source}")
    clone = _run(["/bin/cp", "-cR", str(source), str(destination)], check=False, timeout=180)
    if clone.returncode == 0:
        return
    _remove_path(destination)
    try:
        shutil.copytree(source, destination, symlinks=True)
    except Exception as exc:
        detail = (clone.stderr or clone.stdout or "APFS clone failed").strip()
        raise UpdateError(f"Could not stage the dependency environment ({detail}): {exc}") from exc


def _prepare_dependency_environment(runtime: Path, requirements: Path, target_commit: str) -> Path:
    canonical = runtime / ".venv"
    if not (canonical / "bin" / "python").exists():
        raise UpdateError(f"Runtime Python was not found: {canonical / 'bin' / 'python'}")
    if not requirements.is_file():
        raise UpdateError(f"Dependency requirements were not found: {requirements}")

    staging_root = Path(tempfile.mkdtemp(prefix=f".{runtime.name}.venv-update-", dir=str(runtime.parent)))
    staged = staging_root / "candidate"
    try:
        _clone_dependency_environment(canonical, staged)
        staged_python = staged / "bin" / "python"
        if not staged_python.exists():
            raise UpdateError("Staged virtual environment is incomplete.")
        _run([str(staged_python), "-m", "pip", "install", "-r", str(requirements)], timeout=300)
        return staged
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def _activate_dependency_environment(runtime: Path, staged: Path) -> DependencyEnvironmentTransaction:
    canonical = runtime / ".venv"
    if not staged.is_dir() or not (staged / "bin" / "python").exists():
        raise UpdateError("Staged dependency environment is incomplete.")
    staging_root = staged.parent
    previous_backup = staging_root / "previous"
    marker_token = f"{os.getpid()}-{time.time_ns()}"
    marker = staged / ".mac-mcp-update-env"
    marker.write_text(marker_token + "\n", encoding="utf-8")

    previous_moved = False
    try:
        os.replace(canonical, previous_backup)
        previous_moved = True
        os.replace(staged, canonical)
    except Exception:
        if previous_moved:
            if canonical.exists() or canonical.is_symlink():
                _remove_path(canonical)
            if previous_backup.exists() or previous_backup.is_symlink():
                os.replace(previous_backup, canonical)
        shutil.rmtree(staging_root, ignore_errors=True)
        raise

    active_marker = canonical / ".mac-mcp-update-env"
    if not active_marker.is_file() or active_marker.read_text(encoding="utf-8").strip() != marker_token:
        if canonical.exists() or canonical.is_symlink():
            _remove_path(canonical)
        if previous_backup.exists() or previous_backup.is_symlink():
            os.replace(previous_backup, canonical)
        shutil.rmtree(staging_root, ignore_errors=True)
        raise UpdateError("Dependency environment activation verification failed.")

    return DependencyEnvironmentTransaction(
        runtime=runtime,
        staging_root=staging_root,
        staged_env=canonical,
        previous_backup=previous_backup,
        marker_token=marker_token,
    )


def _dependency_env_matches(transaction: DependencyEnvironmentTransaction) -> bool:
    marker = transaction.runtime / ".venv" / ".mac-mcp-update-env"
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == transaction.marker_token
    except OSError:
        return False


def _rollback_dependency_environment(transaction: DependencyEnvironmentTransaction) -> dict[str, str]:
    canonical = transaction.runtime / ".venv"
    if not _dependency_env_matches(transaction):
        return {
            "status": "failed",
            "reason": "Active virtual environment changed after updater activation; refusing to overwrite it.",
        }
    try:
        failed_env = transaction.staging_root / "failed-candidate"
        os.replace(canonical, failed_env)
        os.replace(transaction.previous_backup, canonical)
        _remove_path(failed_env)
        shutil.rmtree(transaction.staging_root, ignore_errors=True)
        if not (canonical / "bin" / "python").exists():
            raise UpdateError("Restored runtime virtual environment is incomplete.")
        return {"status": "restored", "reason": "Previous runtime virtual environment was restored."}
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}


def _commit_dependency_environment(transaction: DependencyEnvironmentTransaction) -> dict[str, str]:
    canonical = transaction.runtime / ".venv"
    if not _dependency_env_matches(transaction):
        raise UpdateError("Active dependency environment changed before update commit.")
    (canonical / ".mac-mcp-update-env").unlink(missing_ok=True)
    cleanup_warning = None
    try:
        _remove_path(transaction.previous_backup)
        shutil.rmtree(transaction.staging_root, ignore_errors=True)
    except Exception as exc:
        cleanup_warning = str(exc)
    if cleanup_warning:
        return {
            "status": "activated_cleanup_warning",
            "reason": f"New dependency environment is active, but previous environment cleanup failed: {cleanup_warning}",
        }
    return {"status": "activated", "reason": "Staged dependency environment passed validation and is now active."}


def _cleanup_staging_dir(requested_dir: str | None) -> None:
    """Remove only the explicitly authorized, detached updater staging directory."""
    if not requested_dir:
        return

    try:
        helper_file = Path(__file__).resolve(strict=True)
        helper_dir = helper_file.parent
        requested_path = Path(requested_dir).expanduser().resolve(strict=True)
        temp_dir = Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError):
        return

    if (
        helper_file.name != "update_helper.py"
        or requested_path != helper_dir
        or helper_dir.parent != temp_dir
        or not _STAGING_DIR_RE.fullmatch(helper_dir.name)
        or not helper_dir.is_dir()
        or not (helper_dir / "update_state.py").is_file()
        or not (helper_dir / "managed_process.py").is_file()
    ):
        return

    try:
        shutil.rmtree(helper_dir)
    except OSError as exc:
        print(f"[mac-mcp update] WARNING: staging cleanup failed: {exc}", file=sys.stderr, flush=True)


def _rollback_repo(
    repo: Path,
    expected_branch: str,
    pre_update_head: str | None,
    post_merge_head: str | None,
    head_moved: bool,
) -> dict[str, str]:
    """Safely move the source checkout back to its pre-update HEAD."""
    if not head_moved:
        return {"status": "skipped", "reason": "Updater did not move repository HEAD."}
    if not pre_update_head or not post_merge_head:
        return {"status": "skipped", "reason": "Repository rollback markers were not recorded."}

    try:
        current_branch = _git(repo, "branch", "--show-current")
    except Exception as exc:
        return {"status": "skipped", "reason": f"Could not verify the current branch: {exc}"}
    if current_branch != expected_branch:
        return {
            "status": "skipped",
            "reason": f"Current branch changed from '{expected_branch}' to '{current_branch or 'detached HEAD'}'.",
        }

    try:
        current_head = _git(repo, "rev-parse", "HEAD")
    except Exception as exc:
        return {"status": "skipped", "reason": f"Could not verify the current HEAD: {exc}"}
    if current_head != post_merge_head:
        return {
            "status": "skipped",
            "reason": (
                f"Current HEAD {_short(current_head)} no longer matches the updater's "
                f"post-merge HEAD {_short(post_merge_head)}."
            ),
        }

    try:
        status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    except Exception as exc:
        return {"status": "skipped", "reason": f"Could not verify repository cleanliness: {exc}"}
    if status:
        return {"status": "skipped", "reason": "Repository worktree/index is not clean at rollback time."}

    reset = _run(
        ["git", "-C", str(repo), "reset", "--keep", pre_update_head],
        check=False,
        timeout=120,
    )
    if reset.returncode != 0:
        detail = (reset.stderr or reset.stdout or "git reset --keep failed").strip()
        return {"status": "failed", "reason": f"git reset --keep failed: {detail}"}

    try:
        restored_head = _git(repo, "rev-parse", "HEAD")
        restored_status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    except Exception as exc:
        return {"status": "failed", "reason": f"Could not verify repository rollback: {exc}"}
    if restored_head != pre_update_head:
        return {
            "status": "failed",
            "reason": f"Repository rollback ended at {_short(restored_head)}, expected {_short(pre_update_head)}.",
        }
    if restored_status:
        return {"status": "failed", "reason": "Repository is not clean after git reset --keep."}
    return {"status": "restored", "reason": f"Repository HEAD restored to {_short(pre_update_head)}."}


def _same_checkout_restore_guard(
    repo: Path,
    expected_branch: str,
    expected_head: str | None,
) -> tuple[bool, str]:
    """Check that a same-checkout runtime restore will not overwrite user work."""
    if not expected_head:
        return False, "The pre-update repository HEAD was not recorded."
    try:
        current_branch = _git(repo, "branch", "--show-current")
        current_head = _git(repo, "rev-parse", "HEAD")
        status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    except Exception as exc:
        return False, f"Could not verify the checkout before runtime restore: {exc}"
    if current_branch != expected_branch:
        return False, "The repository branch changed before the runtime restore."
    if current_head != expected_head:
        return False, "The repository HEAD changed before the runtime restore."
    if status:
        return False, "The repository worktree/index is not clean before the runtime restore."
    return True, "The checkout is still at the verified clean pre-update revision."


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

    try:
        verified_release = release_trust.verify_release_commit(
            repo_path,
            info.target_commit,
            expected_branch=branch,
        )
    except release_trust.ReleaseVerificationError as exc:
        raise UpdateError(
            f"Verified release re-check failed before swap: {exc}. "
            "Repository and runtime were left unchanged."
        ) from exc
    if (
        not info.release_verified
        or verified_release.release_id != info.release_id
        or verified_release.payload_sha256 != info.release_payload_sha256
    ):
        raise UpdateError(
            "Verified release changed between update check and apply. "
            "Repository and runtime were left unchanged."
        )
    print(
        f"[mac-mcp update] Verified release: {verified_release.release_id} "
        f"(v{verified_release.version}, signer {verified_release.signer_fingerprint or 'unknown'}).",
        flush=True,
    )

    bootstrap_blocker = secure_bootstrap_update_blocker(runtime_path)
    if bootstrap_blocker is not None:
        _write_update_state(runtime_path, {
            "status": "blocked",
            "reason": "secure_bootstrap_migration_required",
            "migration": bootstrap_blocker,
            "from_commit": info.deployed_commit,
            "to_commit": info.target_commit,
        })
        raise UpdateError(_format_secure_bootstrap_blocker(bootstrap_blocker))

    temp_root = Path(tempfile.mkdtemp(prefix="mac-mcp-update-"))
    stage: Optional[Path] = None
    backup: Optional[Path] = None
    health_url: Optional[str] = None
    expected_branch = branch
    pre_update_head: Optional[str] = info.repo_commit
    post_merge_head: Optional[str] = None
    repo_head_moved = False
    merge_completed = False
    runtime_sync_attempted = False
    dependency_install_attempted = False
    dependencies_updated = False
    dependency_env_transaction: DependencyEnvironmentTransaction | None = None
    dependency_commit: dict[str, str] | None = None
    dependency_rollback: dict[str, str] = {
        "status": "skipped",
        "reason": "Dependency environment was not activated.",
    }
    repo_rollback: dict[str, str] = {
        "status": "skipped",
        "reason": "Repository fast-forward was not attempted.",
    }
    runtime_rollback: dict[str, str] = {
        "status": "skipped",
        "reason": "Runtime synchronization was not attempted.",
    }
    rollback_health: dict[str, str] = {
        "status": "skipped",
        "reason": "Rollback runtime was not restored or restarted.",
    }
    deps_changed = _deps_changed(repo_path, info.deployed_commit, info.target_commit)
    try:
        current_branch = _git(repo_path, "branch", "--show-current")
        if current_branch != expected_branch:
            raise UpdateError(
                f"Repository must be on branch '{expected_branch}', currently on "
                f"'{current_branch or 'detached HEAD'}'."
            )
        verified_head = _git(repo_path, "rev-parse", "HEAD")
        if verified_head != pre_update_head:
            raise UpdateError(
                f"Repository changed while preparing the update: expected {_short(pre_update_head)}, "
                f"found {_short(verified_head)}."
            )

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
        if current_branch != expected_branch:
            raise UpdateError(
                f"Repository branch changed while preparing the update: expected '{expected_branch}', "
                f"found '{current_branch or 'detached HEAD'}'."
            )
        current_head = _git(repo_path, "rev-parse", "HEAD")
        if current_head != pre_update_head:
            raise UpdateError(
                f"Repository changed while preparing the update: expected {_short(pre_update_head)}, "
                f"found {_short(current_head)}."
            )
        print("[mac-mcp update] Updating repository (fast-forward)...", flush=True)
        _git(repo_path, "merge", "--ff-only", info.target_commit)
        merge_completed = True
        observed_head = _git(repo_path, "rev-parse", "HEAD")
        post_merge_head = info.target_commit
        repo_head_moved = info.target_commit != pre_update_head
        if observed_head != info.target_commit:
            raise UpdateError(
                f"Repository fast-forward ended at {_short(observed_head)}, "
                f"expected {_short(info.target_commit)}."
            )

        runtime_sync_attempted = True
        synced = _sync_runtime(stage, runtime_path, old_files, new_files)
        print(f"[mac-mcp update] Synced {synced} managed runtime file(s).", flush=True)

        if _refresh_installed_menu_app(runtime_path):
            print("[mac-mcp update] Refreshed installed Mac MCP menu bar app.", flush=True)

        if deps_changed and not skip_deps:
            requirements = runtime_path / "mcp_server" / "requirements.txt"
            print("[mac-mcp update] Preparing transactional dependency environment...", flush=True)
            dependency_install_attempted = True
            staged_env = _prepare_dependency_environment(runtime_path, requirements, info.target_commit)
            dependency_env_transaction = _activate_dependency_environment(runtime_path, staged_env)
            dependencies_updated = True
            print("[mac-mcp update] Transactional dependency environment activated.", flush=True)
        elif deps_changed:
            print("[mac-mcp update] Dependency installation skipped (test mode).", flush=True)
        else:
            print("[mac-mcp update] Dependencies unchanged.", flush=True)

        health_gate_report: dict | None = None
        target_has_health_gate = _target_supports_health_gate(repo_path, info.target_commit)
        if skip_restart:
            print("[mac-mcp update] Service restart skipped (test mode).", flush=True)
        else:
            if target_has_health_gate:
                _write_update_state(runtime_path, {
                    "status": "health_gate",
                    "from_commit": info.deployed_commit,
                    "to_commit": info.target_commit,
                    "repo": str(repo_path),
                    "runtime": str(runtime_path),
                    "release_id": verified_release.release_id,
                    "release_version": verified_release.version,
                })
            print("[mac-mcp update] Restarting Mac MCP...", flush=True)
            health_url = _restart_service(runtime_path, launchd_label)
            if not _health_ok(health_url):
                health_gate_report = _read_health_gate_report()
                gate_summary = None
                if (
                    isinstance(health_gate_report, dict)
                    and str(health_gate_report.get("target_commit") or "").lower() == info.target_commit.lower()
                ):
                    gate_summary = _gate_failure_summary(health_gate_report)
                if gate_summary:
                    raise UpdateError(f"Post-update health gate failed: {gate_summary}")
                raise UpdateError(f"Health check failed after restart: {health_url}")
            print(f"[mac-mcp update] Health check passed: {health_url}", flush=True)

            if target_has_health_gate:
                health_gate_report = _read_health_gate_report()
                if not isinstance(health_gate_report, dict):
                    raise UpdateError("Post-update health gate report was not produced by the target runtime.")
                report_target = str(health_gate_report.get("target_commit") or "").lower()
                if report_target != info.target_commit.lower():
                    raise UpdateError(
                        "Post-update health gate report does not match the target release "
                        f"({_short(report_target)} != {_short(info.target_commit)})."
                    )
                if health_gate_report.get("ok") is not True:
                    gate_summary = _gate_failure_summary(health_gate_report) or "unknown critical check"
                    raise UpdateError(f"Post-update health gate failed: {gate_summary}")
                warnings = health_gate_report.get("warnings")
                warning_count = len(warnings) if isinstance(warnings, list) else 0
                print(
                    f"[mac-mcp update] Post-update health gate passed "
                    f"({warning_count} warning(s)).",
                    flush=True,
                )

        _write_state_commit(runtime_path, info.target_commit)

        if dependency_env_transaction is not None:
            dependency_commit = _commit_dependency_environment(dependency_env_transaction)
            print(
                f"[mac-mcp update] Dependency environment {dependency_commit['status']}: "
                f"{dependency_commit['reason']}",
                flush=True,
            )

        result = {
            "ok": True, "updated": True,
            "from_commit": info.deployed_commit, "to_commit": info.target_commit,
            "from_short": _short(info.deployed_commit), "to_short": _short(info.target_commit),
            "backup": str(backup), "synced_files": synced, "health_url": health_url,
            "release_verified": True,
            "release_id": verified_release.release_id,
            "release_version": verified_release.version,
            "release_payload_sha256": verified_release.payload_sha256,
            "release_signer_fingerprint": verified_release.signer_fingerprint,
            "dependency_environment": dependency_commit,
            "health_gate": (
                {
                    "status": health_gate_report.get("status"),
                    "duration_ms": health_gate_report.get("duration_ms"),
                    "warnings": health_gate_report.get("warnings", []),
                }
                if isinstance(health_gate_report, dict)
                else None
            ),
        }
        _write_update_state(runtime_path, {"status": "completed", **result})
        print(f"[mac-mcp update] Update complete: {_short(info.deployed_commit)} -> {_short(info.target_commit)}", flush=True)
        return result
    except Exception as exc:
        message = str(exc)
        print(f"[mac-mcp update] ERROR: {message}", flush=True)
        _write_update_state(runtime_path, {
            "status": "rolling_back",
            "error": message,
            "from_commit": info.deployed_commit,
            "to_commit": info.target_commit,
        })
        if dependency_env_transaction is not None:
            dependency_rollback = _rollback_dependency_environment(dependency_env_transaction)
            print(
                f"[mac-mcp update] Dependency rollback {dependency_rollback['status']}: "
                f"{dependency_rollback['reason']}",
                flush=True,
            )
        if merge_completed and post_merge_head is None:
            post_merge_head = info.target_commit
            repo_head_moved = info.target_commit != pre_update_head
        try:
            repo_rollback = _rollback_repo(
                repo_path,
                expected_branch,
                pre_update_head,
                post_merge_head,
                repo_head_moved,
            )
        except Exception as repo_exc:
            repo_rollback = {"status": "failed", "reason": f"Unexpected repository rollback error: {repo_exc}"}
        print(
            f"[mac-mcp update] Repository rollback {repo_rollback['status']}: "
            f"{repo_rollback['reason']}",
            flush=True,
        )
        if backup is not None and runtime_sync_attempted:
            same_checkout = repo_path == runtime_path
            restore_runtime = True
            if same_checkout:
                expected_restore_head = pre_update_head
                restore_runtime, guard_reason = _same_checkout_restore_guard(
                    repo_path, expected_branch, expected_restore_head
                )
                if not restore_runtime:
                    runtime_rollback = {"status": "skipped", "reason": guard_reason}
                elif repo_head_moved and repo_rollback.get("status") != "restored":
                    restore_runtime = False
                    runtime_rollback = {
                        "status": "skipped",
                        "reason": (
                            "Repository rollback was not restored safely; preserving the same-checkout "
                            "worktree and user changes."
                        ),
                    }
            if restore_runtime:
                try:
                    print("[mac-mcp update] Restoring the previous runtime...", flush=True)
                    _restore_runtime(runtime_path, backup)
                    _write_state_commit(runtime_path, info.deployed_commit)
                    try:
                        _refresh_installed_menu_app(runtime_path)
                    except Exception as menu_exc:
                        print(f"[mac-mcp update] WARNING: menu app rollback refresh failed: {menu_exc}", flush=True)
                    dependency_restore_safe = (
                        dependency_env_transaction is None
                        or dependency_rollback.get("status") == "restored"
                    )
                    if skip_restart:
                        rollback_health = {
                            "status": "skipped",
                            "reason": "Rollback restart was explicitly skipped; runtime health is unverified.",
                        }
                    elif dependency_restore_safe:
                        try:
                            rollback_health_url = _restart_service(runtime_path, launchd_label)
                            if _health_ok(rollback_health_url):
                                rollback_health = {
                                    "status": "passed",
                                    "reason": "Rollback service restart and health check passed.",
                                }
                                print("[mac-mcp update] Rollback health check passed.", flush=True)
                            else:
                                rollback_health = {
                                    "status": "failed",
                                    "reason": "Rollback service restarted but failed its health check.",
                                }
                                print("[mac-mcp update] WARNING: rollback health check failed.", flush=True)
                        except Exception as restart_exc:
                            rollback_health = {
                                "status": "failed",
                                "reason": f"Rollback restart failed: {restart_exc}",
                            }
                            print(f"[mac-mcp update] WARNING: rollback restart failed: {restart_exc}", flush=True)
                    else:
                        rollback_health = {
                            "status": "skipped",
                            "reason": (
                                "Rollback restart was skipped because the previous dependency environment "
                                "was not restored safely."
                            ),
                        }
                        print(
                            "[mac-mcp update] WARNING: rollback restart skipped because the previous "
                            "dependency environment was not restored safely.",
                            flush=True,
                        )
                    if rollback_health.get("status") == "passed":
                        runtime_rollback = {
                            "status": "restored",
                            "reason": "Previous runtime files, deployed marker, and healthy service were restored.",
                        }
                        print("[mac-mcp update] Runtime rollback completed and verified healthy.", flush=True)
                    else:
                        runtime_rollback = {
                            "status": "restore_unverified",
                            "reason": (
                                "Previous runtime files and deployed marker were restored, but service health "
                                "was not verified."
                            ),
                        }
                        print("[mac-mcp update] WARNING: runtime files restored but rollback health is unverified.", flush=True)
                except Exception as rollback_exc:
                    runtime_rollback = {"status": "failed", "reason": str(rollback_exc)}
                    print(f"[mac-mcp update] WARNING: runtime rollback failed: {rollback_exc}", flush=True)
        elif backup is not None:
            runtime_rollback = {"status": "skipped", "reason": "Runtime synchronization was not attempted."}
        failed_state = {
            "status": "failed",
            "error": message,
            "repo_rollback": repo_rollback,
            "runtime_rollback": runtime_rollback,
            "rollback_health": rollback_health,
            "dependency_rollback": dependency_rollback,
            "repo_head_moved": repo_head_moved,
        }
        if dependency_install_attempted:
            failed_state["dependency_install_attempted"] = True
        if dependencies_updated:
            failed_state["dependencies_updated"] = True
        if pre_update_head is not None:
            failed_state["repo_pre_update_commit"] = pre_update_head
        if post_merge_head is not None:
            failed_state["repo_post_merge_commit"] = post_merge_head
        _write_update_state(runtime_path, failed_state)
        raise
    finally:
        if stage is not None:
            try:
                _git(repo_path, "worktree", "remove", "--force", str(stage), check=False)
            except Exception as cleanup_exc:
                print(f"[mac-mcp update] WARNING: temporary worktree cleanup failed: {cleanup_exc}", flush=True)
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
    p.add_argument("--cleanup-staging-dir", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.check:
            info = check_update(args.repo, args.runtime, args.branch, args.remote, fetch=True)
            print(format_check(info))
            if info.update_available:
                runtime_path = Path(info.runtime)
                blocker = secure_bootstrap_update_blocker(runtime_path)
                if blocker is not None:
                    print(f"Migration required: {_format_secure_bootstrap_blocker(blocker)}")
                    return 2
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
    finally:
        _cleanup_staging_dir(args.cleanup_staging_dir)


if __name__ == "__main__":
    raise SystemExit(main())
