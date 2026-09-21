from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

GIT_ISOLATION_MODES = frozenset({"auto", "off", "required"})
_WORKTREE_DIRNAME = ".mac-mcp-worktrees"
_EXCLUDE_LINE = f"{_WORKTREE_DIRNAME}/"


class AgentWorktreeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def _run(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
    input_bytes: bytes | None = None, check: bool = True, timeout: int = 120,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=env, input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentWorktreeError("git_command_failed", f"Could not run Git safely: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"git command failed").decode("utf-8", errors="replace").strip()
        raise AgentWorktreeError("git_command_failed", detail[:2000])
    return proc


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 120, env: dict[str, str] | None = None,
         input_bytes: bytes | None = None) -> str:
    proc = _run(["git", "-C", str(repo), *args], check=check, timeout=timeout, env=env, input_bytes=input_bytes)
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _git_bytes(repo: Path, *args: str, check: bool = True, timeout: int = 120,
               env: dict[str, str] | None = None, input_bytes: bytes | None = None) -> bytes:
    return _run(["git", "-C", str(repo), *args], check=check, timeout=timeout, env=env, input_bytes=input_bytes).stdout


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _repo_root(cwd: Path) -> Path | None:
    proc = _run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], check=False, timeout=15)
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    root = Path(raw).expanduser().resolve(strict=False)
    return root if root.exists() and root.is_dir() else None


def resolve_git_base(cwd: Path, *, mode: str = "auto", access_mode: str = "workspace_write") -> str | None:
    mode = str(mode or "auto").strip().lower()
    if mode not in GIT_ISOLATION_MODES:
        raise AgentWorktreeError("invalid_git_isolation", f"git_isolation must be one of: {', '.join(sorted(GIT_ISOLATION_MODES))}")
    if mode == "off" or access_mode == "read_only":
        return None
    if access_mode != "workspace_write":
        if mode == "required":
            raise AgentWorktreeError("git_isolation_unenforceable", "Git isolation requires access_mode=workspace_write.")
        return None
    repo = _repo_root(cwd.expanduser().resolve(strict=False))
    if repo is None:
        if mode == "required":
            raise AgentWorktreeError("git_repository_required", "git_isolation=required but cwd is not inside a Git worktree.")
        return None
    return _git(repo, "rev-parse", "HEAD")


def _common_git_dir(repo: Path) -> Path:
    raw = _git(repo, "rev-parse", "--git-common-dir")
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve(strict=False)


def _ensure_local_exclude(repo: Path) -> None:
    info = _common_git_dir(repo) / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if _EXCLUDE_LINE in lines:
        return
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("# Mac MCP delegated-agent ephemeral worktrees\n")
        handle.write(_EXCLUDE_LINE + "\n")


def _disabled(mode: str, reason: str, *, source_cwd: Path) -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": mode,
        "status": "disabled",
        "reason": reason,
        "source_cwd": str(source_cwd),
    }


def prepare_worktree(
    *, agent_id: str, cwd: Path, path_roots: Iterable[str] | None,
    mode: str = "auto", access_mode: str = "workspace_write",
    base_commit: str | None = None,
) -> dict[str, Any]:
    mode = str(mode or "auto").strip().lower()
    if mode not in GIT_ISOLATION_MODES:
        raise AgentWorktreeError("invalid_git_isolation", f"git_isolation must be one of: {', '.join(sorted(GIT_ISOLATION_MODES))}")
    source_cwd = cwd.expanduser().resolve(strict=False)
    if mode == "off":
        return _disabled(mode, "disabled_by_request", source_cwd=source_cwd)
    if access_mode == "read_only":
        return _disabled(mode, "read_only_agent", source_cwd=source_cwd)
    if access_mode != "workspace_write":
        if mode == "required":
            raise AgentWorktreeError(
                "git_isolation_unenforceable",
                "Git isolation requires access_mode=workspace_write; full access could still reach the original checkout.",
            )
        return _disabled(mode, "full_access_not_confined", source_cwd=source_cwd)

    repo = _repo_root(source_cwd)
    if repo is None:
        if mode == "required":
            raise AgentWorktreeError("git_repository_required", "git_isolation=required but cwd is not inside a Git worktree.")
        return _disabled(mode, "non_git_workspace", source_cwd=source_cwd)

    roots = [Path(raw).expanduser().resolve(strict=False) for raw in (path_roots or ())]
    if not roots:
        roots = [source_cwd]
    if any(not _within(root, repo) for root in roots):
        if mode == "required":
            raise AgentWorktreeError(
                "git_isolation_scope_external",
                "Git isolation cannot safely remap workspace_write roots outside the repository.",
                details={"repo_root": str(repo), "path_roots": [str(root) for root in roots]},
            )
        return _disabled(mode, "scope_includes_external_path", source_cwd=source_cwd)

    anchors = [root for root in roots if _within(source_cwd, root)]
    if not anchors:
        if mode == "required":
            raise AgentWorktreeError(
                "git_isolation_scope_anchor_missing",
                "No workspace_write path root contains cwd, so an isolated child scope cannot be attenuated safely.",
            )
        return _disabled(mode, "scope_anchor_missing", source_cwd=source_cwd)
    # Use the narrowest authorized root that still contains cwd.
    anchor = max(anchors, key=lambda item: len(item.parts))
    if not _within(anchor, repo):
        if mode == "required":
            raise AgentWorktreeError("git_isolation_scope_anchor_external", "The isolation anchor is outside the Git repository.")
        return _disabled(mode, "scope_anchor_external", source_cwd=source_cwd)

    resolved_base = str(base_commit or "").strip() or _git(repo, "rev-parse", "HEAD")
    # Verify the supplied base belongs to this repository before creating anything.
    _git(repo, "cat-file", "-e", f"{resolved_base}^{{commit}}")
    branch = f"mac-mcp/agent/{agent_id}"
    root_parent = anchor / _WORKTREE_DIRNAME
    worktree = (root_parent / agent_id).resolve(strict=False)
    if worktree.exists():
        raise AgentWorktreeError("git_worktree_exists", f"Agent worktree path already exists: {worktree}")
    _ensure_local_exclude(repo)
    root_parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(repo, "worktree", "add", "--quiet", "-b", branch, str(worktree), resolved_base, timeout=120)
    except Exception:
        try:
            if worktree.exists():
                shutil.rmtree(worktree, ignore_errors=True)
            _git(repo, "branch", "-D", branch, check=False)
            _git(repo, "worktree", "prune", check=False)
        except Exception:
            pass
        raise

    relative_cwd = source_cwd.relative_to(repo)
    isolated_cwd = (worktree / relative_cwd).resolve(strict=False)
    mapped_roots: list[dict[str, str]] = []
    for root in roots:
        relative = root.relative_to(repo)
        mapped = (worktree / relative).resolve(strict=False)
        # The derived child root must still be inside at least one original authorized root.
        if not any(_within(mapped, original) for original in roots):
            cleanup_worktree({
                "enabled": True, "repo_root": str(repo), "path": str(worktree), "branch": branch,
            }, force=True)
            raise AgentWorktreeError(
                "git_isolation_scope_widened",
                "Derived worktree scope would escape the parent workspace roots.",
                details={"mapped_root": str(mapped)},
            )
        mapped_roots.append({"source": str(root), "isolated": str(mapped)})

    return {
        "enabled": True,
        "mode": mode,
        "status": "active",
        "reason": None,
        "repo_root": str(repo),
        "source_cwd": str(source_cwd),
        "cwd": str(isolated_cwd),
        "path": str(worktree),
        "branch": branch,
        "base_commit": resolved_base,
        "source_head_at_spawn": _git(repo, "rev-parse", "HEAD"),
        "anchor_root": str(anchor),
        "path_map": mapped_roots,
        "created_at": time.time(),
        "apply_status": "pending",
        "applied_at": None,
        "applied_to_head": None,
        "applied_snapshot_commit": None,
        "changed_files": [],
        "change_count": 0,
        "has_changes": False,
        "snapshot_commit": resolved_base,
        "diff_stat": "",
    }


def reuse_worktree(existing: dict[str, Any]) -> dict[str, Any]:
    if not existing or not existing.get("enabled"):
        return dict(existing or {})
    path = Path(str(existing.get("path") or "")).expanduser().resolve(strict=False)
    repo = Path(str(existing.get("repo_root") or "")).expanduser().resolve(strict=False)
    if not path.exists() or not repo.exists():
        raise AgentWorktreeError("git_worktree_missing", "The isolated Git worktree is no longer available for resume/follow-up.")
    current = dict(existing)
    current["status"] = "active"
    current["reused_at"] = time.time()
    return current


def remapped_roots(state: dict[str, Any]) -> list[str]:
    return [str(item["isolated"]) for item in (state.get("path_map") or []) if isinstance(item, dict) and item.get("isolated")]


def _snapshot_commit(state: dict[str, Any]) -> str:
    if not state.get("enabled"):
        return ""
    worktree = Path(str(state["path"]))
    base = str(state["base_commit"])
    if not worktree.exists():
        raise AgentWorktreeError("git_worktree_missing", f"Agent worktree no longer exists: {worktree}")
    fd, index_name = tempfile.mkstemp(prefix="mac-mcp-agent-index-", suffix=".idx")
    os.close(fd)
    index = Path(index_name)
    index.unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    try:
        _git(worktree, "read-tree", base, env=env)
        _git(worktree, "add", "-A", "--", ".", env=env)
        tree = _git(worktree, "write-tree", env=env)
        commit_env = env.copy()
        commit_env.update({
            "GIT_AUTHOR_NAME": "Mac MCP Agent",
            "GIT_AUTHOR_EMAIL": "mac-mcp-agent@localhost",
            "GIT_COMMITTER_NAME": "Mac MCP Agent",
            "GIT_COMMITTER_EMAIL": "mac-mcp-agent@localhost",
        })
        commit = _git(
            worktree, "commit-tree", tree, "-p", base,
            env=commit_env, input_bytes=b"Mac MCP isolated agent snapshot\n",
        )
        return commit
    finally:
        index.unlink(missing_ok=True)


def _parse_name_status(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    parts = raw.split(b"\x00")
    changed: set[str] = set()
    records: list[dict[str, str]] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        index += 1
        if not token:
            continue
        status_text = token.decode("utf-8", errors="replace")
        if status_text.startswith(("R", "C")):
            if index + 1 >= len(parts):
                break
            old = parts[index].decode("utf-8", errors="surrogateescape")
            new = parts[index + 1].decode("utf-8", errors="surrogateescape")
            index += 2
            changed.update({old, new})
            records.append({"status": status_text, "path": new, "old_path": old})
        else:
            if index >= len(parts):
                break
            path = parts[index].decode("utf-8", errors="surrogateescape")
            index += 1
            changed.add(path)
            records.append({"status": status_text, "path": path})
    return sorted(changed), records


def _patch_for_state(state: dict[str, Any]) -> bytes:
    inspected = inspect_worktree(state)
    if inspected.get("status") == "missing":
        raise AgentWorktreeError("git_worktree_missing", "A dependency worktree is missing before fan-in.")
    if inspected.get("inspection_error"):
        raise AgentWorktreeError("git_worktree_unreadable", str(inspected["inspection_error"]))
    if not inspected.get("has_changes"):
        return b""
    return _git_bytes(
        Path(str(inspected["path"])), "diff", "--binary", "--full-index",
        str(inspected["base_commit"]), str(inspected["snapshot_commit"]), "--",
    )


def seed_worktree(target: dict[str, Any], sources: Iterable[dict[str, Any]]) -> dict[str, Any]:
    current = dict(target or {})
    if not current.get("enabled"):
        return current
    target_path = Path(str(current["path"]))
    base = str(current.get("base_commit") or "")
    seeded: list[dict[str, Any]] = []
    for source in sources:
        if not source or not source.get("enabled"):
            continue
        source_base = str(source.get("base_commit") or "")
        if source_base != base:
            raise AgentWorktreeError(
                "git_dependency_base_mismatch",
                "Dependency worktrees do not share the team's pinned Git base.",
                details={"target_base": base, "source_base": source_base},
            )
        patch = _patch_for_state(source)
        if not patch:
            seeded.append({"path": source.get("path"), "changed": False})
            continue
        check = _run(
            ["git", "-C", str(target_path), "apply", "--check", "--binary", "-"],
            input_bytes=patch, check=False, timeout=120,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout or b"dependency patch conflict").decode("utf-8", errors="replace").strip()
            raise AgentWorktreeError(
                "git_dependency_conflict",
                "Dependency worktree changes conflict while preparing a downstream isolated task.",
                details={"detail": detail[:1200], "source_path": source.get("path")},
            )
        _run(["git", "-C", str(target_path), "apply", "--binary", "-"], input_bytes=patch, timeout=120)
        seeded.append({"path": source.get("path"), "changed": True})
    current["seeded_from"] = seeded
    current["seeded_at"] = time.time() if seeded else None
    return current


def inspect_worktree(state: dict[str, Any]) -> dict[str, Any]:
    current = dict(state or {})
    if not current.get("enabled"):
        return current
    worktree = Path(str(current["path"]))
    if not worktree.exists():
        current.update({"status": "missing", "has_changes": False, "change_count": 0, "changed_files": []})
        return current
    snapshot = _snapshot_commit(current)
    base = str(current["base_commit"])
    raw = _git_bytes(worktree, "diff", "--name-status", "-z", "--find-renames", base, snapshot, "--")
    changed, records = _parse_name_status(raw)
    stat_text = _git(worktree, "diff", "--stat", base, snapshot, "--", check=False)
    applied_snapshot = str(current.get("applied_snapshot_commit") or "").strip() or None
    current.update({
        "status": "active",
        "snapshot_commit": snapshot,
        "changed_files": changed,
        "changes": records,
        "change_count": len(changed),
        "has_changes": bool(changed),
        "pending_changes": bool(changed) and snapshot != applied_snapshot,
        "diff_stat": stat_text[-4000:],
        "worktree_head": _git(worktree, "rev-parse", "HEAD"),
        "inspected_at": time.time(),
    })
    return current


def _safe_relpath(value: str) -> Path:
    rel = Path(value)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise AgentWorktreeError("unsafe_git_path", f"Unsafe Git path in worktree diff: {value}")
    return rel


def _fingerprint(path: Path) -> dict[str, Any]:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    if stat.S_ISLNK(st.st_mode):
        return {"kind": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(st.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return {"kind": "file", "sha256": digest.hexdigest(), "mode": stat.S_IMODE(st.st_mode)}
    return {"kind": "unsupported", "mode": stat.S_IFMT(st.st_mode)}


def _assert_safe_parent(repo: Path, rel: Path) -> Path:
    target = repo / rel
    current = repo
    for part in rel.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise AgentWorktreeError(
                "apply_symlink_parent", f"Refusing to apply through symlink parent: {current}",
                details={"path": str(rel)},
            )
    try:
        target.parent.resolve(strict=False).relative_to(repo.resolve(strict=False))
    except ValueError as exc:
        raise AgentWorktreeError("apply_scope_escape", f"Apply target escapes repository: {rel}") from exc
    return target


@contextmanager
def _apply_lock(repo: Path) -> Iterator[None]:
    lock_path = _common_git_dir(repo) / "mac-mcp-agent-apply.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _dirty_paths(repo: Path, touched: list[str]) -> list[str]:
    if not touched:
        return []
    raw = _git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *touched, check=False)
    parts = raw.split(b"\x00")
    dirty: set[str] = set()
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item:
            continue
        text = item.decode("utf-8", errors="surrogateescape")
        path = text[3:] if len(text) >= 4 else text
        if path:
            dirty.add(path)
        if text[:1] in {"R", "C"} or text[1:2] in {"R", "C"}:
            if index < len(parts) and parts[index]:
                dirty.add(parts[index].decode("utf-8", errors="surrogateescape"))
                index += 1
    return sorted(dirty)


def _backup_preimage(path: Path, backup_root: Path, rel: Path) -> dict[str, Any]:
    observed = _fingerprint(path)
    record: dict[str, Any] = {"fingerprint": observed, "rel": str(rel)}
    if observed["kind"] == "file":
        backup = backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup, follow_symlinks=False)
        record["backup"] = str(backup)
    return record


def _restore_preimage(repo: Path, record: dict[str, Any]) -> None:
    rel = _safe_relpath(str(record["rel"]))
    target = _assert_safe_parent(repo, rel)
    fingerprint = dict(record.get("fingerprint") or {})
    kind = fingerprint.get("kind")
    if kind == "absent":
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-rollback-", dir=target.parent)
        os.close(fd); tmp = Path(tmp_name); tmp.unlink(missing_ok=True)
        os.symlink(str(fingerprint.get("target") or ""), tmp)
        os.replace(tmp, target)
        return
    if kind == "file":
        backup = Path(str(record["backup"]))
        fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-rollback-", dir=target.parent)
        os.close(fd); tmp = Path(tmp_name)
        try:
            shutil.copyfile(backup, tmp, follow_symlinks=False)
            os.chmod(tmp, int(fingerprint.get("mode") or 0o644))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return
    raise AgentWorktreeError("apply_rollback_unsupported", f"Cannot restore unsupported preimage type for {rel}")


def _copy_desired_state(integration: Path, repo: Path, touched: list[str], expected: dict[str, dict[str, Any]]) -> None:
    backup_root = Path(tempfile.mkdtemp(prefix="mac-mcp-agent-apply-preimage-"))
    preimages: dict[str, dict[str, Any]] = {}
    mutated: list[str] = []
    try:
        for raw in touched:
            rel = _safe_relpath(raw)
            target = _assert_safe_parent(repo, rel)
            observed = _fingerprint(target)
            if observed != expected[raw]:
                raise AgentWorktreeError(
                    "apply_cas_conflict",
                    f"Repository path changed during apply preflight: {raw}",
                    details={"conflicts": [{"path": raw, "reason": "changed_during_apply"}]},
                )
            preimages[raw] = _backup_preimage(target, backup_root, rel)

        for raw in touched:
            rel = _safe_relpath(raw)
            source = integration / rel
            target = _assert_safe_parent(repo, rel)
            # Re-check immediately before each mutation to narrow the external-race window.
            if _fingerprint(target) != expected[raw]:
                raise AgentWorktreeError(
                    "apply_cas_conflict",
                    f"Repository path changed immediately before apply: {raw}",
                    details={"conflicts": [{"path": raw, "reason": "changed_during_apply"}]},
                )
            source_fp = _fingerprint(source)
            if source_fp["kind"] == "absent":
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        raise AgentWorktreeError("apply_directory_conflict", f"Refusing to remove directory for Git file deletion: {raw}")
                    target.unlink()
                mutated.append(raw)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_fp["kind"] == "symlink":
                fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-apply-", dir=target.parent)
                os.close(fd); tmp = Path(tmp_name); tmp.unlink(missing_ok=True)
                os.symlink(os.readlink(source), tmp)
                os.replace(tmp, target)
                mutated.append(raw)
                continue
            if source_fp["kind"] != "file":
                raise AgentWorktreeError("apply_unsupported_type", f"Unsupported Git path type during apply: {raw}")
            fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-apply-", dir=target.parent)
            os.close(fd); tmp = Path(tmp_name)
            try:
                shutil.copyfile(source, tmp, follow_symlinks=False)
                os.chmod(tmp, int(source_fp["mode"]))
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            mutated.append(raw)
    except Exception as exc:
        rollback_errors: list[str] = []
        for raw in reversed(mutated):
            try:
                _restore_preimage(repo, preimages[raw])
            except Exception as rollback_exc:
                rollback_errors.append(f"{raw}: {rollback_exc}")
        if rollback_errors:
            raise AgentWorktreeError(
                "apply_rollback_failed",
                "Safe apply failed and one or more source paths could not be restored.",
                details={"rollback_errors": rollback_errors},
            ) from exc
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def apply_worktree(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    current = inspect_worktree(state)
    if not current.get("enabled"):
        raise AgentWorktreeError("git_isolation_disabled", "This agent has no isolated Git worktree to apply.")
    if current.get("status") == "missing":
        raise AgentWorktreeError("git_worktree_missing", "The isolated worktree no longer exists.")
    if not current.get("has_changes"):
        current.update({"apply_status": "nothing_to_apply", "pending_changes": False})
        return current, {"ok": True, "status": "nothing_to_apply", "changed_files": []}
    snapshot = str(current["snapshot_commit"])
    if current.get("apply_status") == "applied":
        if snapshot == str(current.get("applied_snapshot_commit") or ""):
            return current, {
                "ok": True, "status": "already_applied", "changed_files": list(current.get("changed_files") or []),
                "applied_to_head": current.get("applied_to_head"),
            }
        raise AgentWorktreeError(
            "worktree_changed_after_apply",
            "The isolated worktree changed after its previous apply. Start a fresh write agent or review/apply the new delta manually.",
        )

    repo = Path(str(current["repo_root"])).resolve(strict=False)
    base = str(current["base_commit"])
    worktree = Path(str(current["path"])).resolve(strict=False)
    touched = [str(item) for item in current.get("changed_files") or []]
    with _apply_lock(repo):
        current_head = _git(repo, "rev-parse", "HEAD")
        dirty = _dirty_paths(repo, touched)
        conflicts: list[dict[str, str]] = [
            {"path": path, "reason": "source_worktree_dirty"} for path in dirty
        ]
        head_overlap_raw = _git_bytes(repo, "diff", "--name-only", "-z", base, current_head, "--", *touched, check=False)
        head_overlap = [item.decode("utf-8", errors="surrogateescape") for item in head_overlap_raw.split(b"\x00") if item]
        conflicts.extend({"path": path, "reason": "base_advanced_on_touched_path"} for path in head_overlap if path not in dirty)
        if conflicts:
            return current, {
                "ok": False,
                "status": "conflict",
                "error": "git_apply_conflict",
                "conflicts": conflicts,
                "changed_files": touched,
                "base_commit": base,
                "current_head": current_head,
                "next_action": "Resolve/review the listed source-tree changes, then retry apply or discard the isolated worktree.",
            }

        expected = {raw: _fingerprint(_assert_safe_parent(repo, _safe_relpath(raw))) for raw in touched}
        patch = _git_bytes(worktree, "diff", "--binary", "--full-index", base, snapshot, "--")
        integration = Path(tempfile.mkdtemp(prefix="mac-mcp-agent-apply-"))
        try:
            _git(repo, "worktree", "add", "--quiet", "--detach", str(integration), current_head, timeout=120)
            check = _run(
                ["git", "-C", str(integration), "apply", "--check", "--binary", "-"],
                input_bytes=patch, check=False, timeout=120,
            )
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or b"patch conflict").decode("utf-8", errors="replace").strip()
                return current, {
                    "ok": False,
                    "status": "conflict",
                    "error": "git_patch_conflict",
                    "conflicts": [{"path": path, "reason": "patch_conflict"} for path in touched],
                    "detail": detail[:1200],
                    "changed_files": touched,
                    "base_commit": base,
                    "current_head": current_head,
                    "next_action": "Review the isolated worktree diff and resolve the conflict manually; no source-tree files were changed.",
                }
            _run(["git", "-C", str(integration), "apply", "--binary", "-"], input_bytes=patch, timeout=120)
            # One final HEAD/dirty re-check before any source-tree mutation.
            if _git(repo, "rev-parse", "HEAD") != current_head:
                raise AgentWorktreeError("apply_head_raced", "Repository HEAD changed during apply preflight.")
            raced_dirty = _dirty_paths(repo, touched)
            if raced_dirty:
                raise AgentWorktreeError(
                    "apply_cas_conflict", "Repository paths changed during apply preflight.",
                    details={"conflicts": [{"path": path, "reason": "changed_during_apply"} for path in raced_dirty]},
                )
            _copy_desired_state(integration, repo, touched, expected)
        finally:
            try:
                _git(repo, "worktree", "remove", "--force", str(integration), check=False, timeout=120)
                _git(repo, "worktree", "prune", check=False)
            finally:
                shutil.rmtree(integration, ignore_errors=True)

    current.update({
        "apply_status": "applied",
        "applied_at": time.time(),
        "applied_to_head": current_head,
        "applied_snapshot_commit": snapshot,
        "pending_changes": False,
    })
    return current, {
        "ok": True,
        "status": "applied",
        "changed_files": touched,
        "base_commit": base,
        "applied_to_head": current_head,
        "source_repo": str(repo),
    }


def cleanup_worktree(state: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    current = dict(state or {})
    if not current.get("enabled"):
        return current
    repo = Path(str(current.get("repo_root") or "")).resolve(strict=False)
    worktree = Path(str(current.get("path") or "")).resolve(strict=False)
    branch = str(current.get("branch") or "").strip()
    if repo.exists():
        remove_args = ["worktree", "remove"]
        if force:
            remove_args.append("--force")
        remove_args.append(str(worktree))
        _git(repo, *remove_args, check=False, timeout=120)
        if branch:
            _git(repo, "branch", "-D", branch, check=False, timeout=30)
        _git(repo, "worktree", "prune", check=False, timeout=30)
    shutil.rmtree(worktree, ignore_errors=True)
    current.update({
        "status": "discarded" if force else "cleaned",
        "cleaned_at": time.time(),
        "pending_changes": False,
    })
    return current
