from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from fastapi import HTTPException, status

from .file_transactions import (
    FileTransactionError,
    TransactionIrreversible,
    abort_capture_transaction,
    begin_capture_transaction,
    capture_transaction_snapshot,
    compose_transactions,
    finalize_capture_transaction,
    get_transaction,
    journal_root,
    path_revision,
)
from .policy import current_policy_context
from .policy_scope import AccessMode, ResourceScope, ScopeRequest, evaluate_scope, path_is_within

_DEFAULT_MAX_FILES = 5000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
_POLICY_EXCLUDED_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", "dist", "build", ".next", ".turbo", "coverage",
}
_CONTROL_EXCLUDED_DIRS = {".git", ".hg", ".svn"}


class ShellCaptureError(RuntimeError):
    code = "shell_capture_error"


class ShellCaptureLimit(ShellCaptureError):
    code = "shell_capture_limit"


class ShellCaptureScopeError(ShellCaptureError):
    code = "shell_capture_scope_denied"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def _max_files() -> int:
    return _env_int("MAC_MCP_SHELL_CAPTURE_MAX_FILES", _DEFAULT_MAX_FILES, 1)


def _max_bytes() -> int:
    return _env_int("MAC_MCP_SHELL_CAPTURE_MAX_BYTES", _DEFAULT_MAX_BYTES, 1024)


def _max_file_bytes() -> int:
    return _env_int("MAC_MCP_SHELL_CAPTURE_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES, 1)


def _capture_dir(transaction_id: str) -> Path:
    return journal_root() / transaction_id / "capture"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShellCaptureError("shell capture metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise ShellCaptureError("shell capture metadata is invalid")
    return value


def _run_git(repo_root: Path, args: Sequence[str], *, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        raise ShellCaptureError("git metadata command failed")
    return proc.stdout


def _nul_paths(data: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in data.split(b"\0")
        if item
    ]


def _git_repo_root(root: Path) -> Optional[Path]:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return Path(value).resolve(strict=False) if value else None


def _repo_relative(repo_root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(repo_root.resolve(strict=False)).as_posix()


def _git_pathspec(repo_root: Path, root: Path) -> str:
    rel = _repo_relative(repo_root, root)
    return "." if rel == "." else rel


def _git_tracked(repo_root: Path, pathspec: str) -> set[str]:
    return set(_nul_paths(_run_git(repo_root, ["ls-files", "-z", "--cached", "--", pathspec])))


def _git_untracked(repo_root: Path, pathspec: str) -> set[str]:
    return set(_nul_paths(_run_git(repo_root, ["ls-files", "-z", "--others", "--exclude-standard", "--", pathspec])))


def _git_ignored(repo_root: Path, pathspec: str) -> set[str]:
    return set(_nul_paths(_run_git(
        repo_root, ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", pathspec],
        check=False,
    )))


def _git_dirty(repo_root: Path, pathspec: str) -> set[str]:
    return set(_nul_paths(_run_git(repo_root, ["diff", "--name-only", "-z", "HEAD", "--", pathspec])))


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_index_path(repo_root: Path) -> Optional[Path]:
    output = _run_git(repo_root, ["rev-parse", "--git-path", "index"], check=False).decode(
        "utf-8", errors="replace"
    ).strip()
    if not output:
        return None
    path = Path(output)
    return path if path.is_absolute() else repo_root / path


def _git_tree_mode(repo_root: Path, commit: str, rel: str) -> Optional[int]:
    output = _run_git(repo_root, ["ls-tree", commit, "--", rel], check=False).decode(
        "utf-8", errors="replace"
    ).strip()
    if not output:
        return None
    token = output.split(None, 1)[0]
    try:
        mode = int(token, 8)
    except ValueError:
        return None
    if mode == 0o120000:
        return stat.S_IFLNK | 0o777
    if mode == 0o100755:
        return stat.S_IFREG | 0o755
    if mode == 0o100644:
        return stat.S_IFREG | 0o644
    return None


def _git_changed_from(repo_root: Path, commit: str, pathspec: str) -> set[str]:
    return set(_nul_paths(_run_git(
        repo_root, ["diff", "--name-only", "-z", commit, "--", pathspec],
    )))


def _git_mode(repo_root: Path, rel: str) -> Optional[int]:
    output = _run_git(repo_root, ["ls-files", "-s", "--", rel], check=False).decode("utf-8", errors="replace").strip()
    if not output:
        return None
    token = output.split(None, 1)[0]
    try:
        mode = int(token, 8)
    except ValueError:
        return None
    if mode == 0o120000:
        return stat.S_IFLNK | 0o777
    if mode == 0o100755:
        return stat.S_IFREG | 0o755
    if mode == 0o100644:
        return stat.S_IFREG | 0o644
    return None


def _path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    if path.exists():
        return "other"
    return "absent"


def _file_mode(path: Path) -> Optional[int]:
    try:
        return stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        return None


def _copy_preimage(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    kind = _path_kind(source)
    if kind == "file":
        shutil.copy2(source, destination, follow_symlinks=False)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
    elif kind == "symlink":
        destination.symlink_to(os.readlink(source))
    else:
        raise ShellCaptureError(f"unsupported shell preimage kind: {kind}")


def _materialize_git_preimage(repo_root: Path, base_commit: str, rel: str, destination: Path) -> tuple[str, Optional[int]]:
    mode = _git_tree_mode(repo_root, base_commit, rel)
    data = _run_git(repo_root, ["show", f"{base_commit}:{rel}"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None and stat.S_ISLNK(mode):
        destination.symlink_to(data.decode("utf-8", errors="surrogateescape"))
        return "symlink", 0o777
    destination.write_bytes(data)
    file_mode = stat.S_IMODE(mode) if mode is not None else 0o644
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return "file", file_mode


def _is_policy_excluded(rel: Path) -> bool:
    parts = set(rel.parts)
    return bool(parts.intersection(_POLICY_EXCLUDED_DIRS))


def _is_control_excluded(rel: Path) -> bool:
    return bool(set(rel.parts).intersection(_CONTROL_EXCLUDED_DIRS))


def _scan_dirs(root: Path) -> tuple[set[str], set[str], list[str]]:
    dirs: set[str] = set()
    empty_dirs: set[str] = set()
    unsupported: list[str] = []
    for current, dirnames, _filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = current_path.relative_to(root)
        kept: list[str] = []
        for name in sorted(dirnames):
            full = current_path / name
            rel = rel_current / name
            if _is_control_excluded(rel):
                continue
            if _is_policy_excluded(rel):
                unsupported.append(rel.as_posix())
                continue
            if full.is_symlink():
                if not path_is_within(full, root):
                    unsupported.append(rel.as_posix())
                continue
            kept.append(name)
            dirs.add(rel.as_posix())
        dirnames[:] = kept
    for rel in sorted(dirs):
        path = root / rel
        try:
            if not any(path.iterdir()):
                empty_dirs.add(rel)
        except OSError:
            unsupported.append(rel)
    return dirs, empty_dirs, sorted(set(unsupported))


def _topmost_dirs(paths: Iterable[str]) -> list[str]:
    values = sorted(set(paths), key=lambda value: (len(Path(value).parts), value))
    selected: list[str] = []
    selected_paths: list[Path] = []
    for value in values:
        candidate = Path(value)
        if any(parent == candidate or parent in candidate.parents for parent in selected_paths):
            continue
        selected.append(value)
        selected_paths.append(candidate)
    return selected


def _capture_new_directories(
    transaction_id: str,
    root: Path,
    baseline_dirs: Iterable[str],
    current_dirs: Iterable[str],
    scope: Optional[ResourceScope],
) -> list[str]:
    new_dirs = set(current_dirs).difference(set(baseline_dirs))
    captured: list[str] = []
    for rel in _topmost_dirs(new_dirs):
        target = root / rel
        capture_transaction_snapshot(
            transaction_id,
            target,
            before_kind="absent",
            scope=scope if scope and scope.path_roots is not None else None,
        )
        captured.append(str(target))
    return captured


def _capture_deleted_empty_directories(
    transaction_id: str,
    root: Path,
    baseline_empty_dirs: Iterable[str],
    current_dirs: Iterable[str],
    scope: Optional[ResourceScope],
) -> list[str]:
    deleted = set(baseline_empty_dirs).difference(set(current_dirs))
    captured: list[str] = []
    base = _capture_dir(transaction_id) / "empty-dir-preimages"
    # Deepest-first append means reverse rollback restores parents before children.
    for rel in sorted(deleted, key=lambda value: (-len(Path(value).parts), value)):
        target = root / rel
        source = base / rel
        source.mkdir(parents=True, exist_ok=True)
        capture_transaction_snapshot(
            transaction_id,
            target,
            before_kind="directory",
            before_source=source,
            before_fingerprint=path_revision(source),
            before_mode=0o755,
            scope=scope if scope and scope.path_roots is not None else None,
        )
        captured.append(str(target))
    return captured


def _scan_non_git(root: Path, *, allow_partial: bool = False) -> tuple[Dict[str, Dict[str, Any]], list[str], list[str]]:
    rows: Dict[str, Dict[str, Any]] = {}
    dirs: list[str] = []
    unsupported: list[str] = []
    total_bytes = 0
    count = 0
    capture_root = root

    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = current_path.relative_to(root)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            rel = rel_current / name
            if _is_control_excluded(rel):
                continue
            if _is_policy_excluded(rel):
                unsupported.append(rel.as_posix())
                continue
            full = current_path / name
            if full.is_symlink():
                # Directory symlinks are never traversed; capture the link object below.
                filenames.append(name)
                continue
            kept_dirs.append(name)
            dirs.append(rel.as_posix())
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            path = current_path / name
            rel = path.relative_to(root)
            if _is_control_excluded(rel) or _is_policy_excluded(rel):
                continue
            kind = _path_kind(path)
            if kind == "symlink" and not path_is_within(path, capture_root):
                unsupported.append(rel.as_posix())
                continue
            if kind not in {"file", "symlink"}:
                unsupported.append(rel.as_posix())
                continue
            count += 1
            if count > _max_files():
                if allow_partial:
                    unsupported.append("__capture_limit__:file_count")
                    return rows, dirs, sorted(set(unsupported))
                raise ShellCaptureLimit("filesystem baseline exceeds file-count limit")
            size = len(os.readlink(path).encode("utf-8", errors="replace")) if kind == "symlink" else path.stat().st_size
            if size > _max_file_bytes():
                if allow_partial:
                    unsupported.append(rel.as_posix())
                    continue
                raise ShellCaptureLimit(f"filesystem baseline contains an oversized file: {rel.as_posix()}")
            if total_bytes + max(0, size) > _max_bytes():
                if allow_partial:
                    unsupported.append("__capture_limit__:bytes")
                    return rows, dirs, sorted(set(unsupported))
                raise ShellCaptureLimit("filesystem baseline exceeds byte limit")
            total_bytes += max(0, size)
            rows[rel.as_posix()] = {
                "kind": kind,
                "fingerprint": path_revision(path),
                "mode": _file_mode(path),
                "size": size,
            }
    return rows, dirs, sorted(set(unsupported))


def _copy_non_git_baseline(root: Path, transaction_id: str, rows: Dict[str, Dict[str, Any]]) -> None:
    base = _capture_dir(transaction_id) / "preimages"
    for rel in sorted(rows):
        _copy_preimage(root / rel, base / rel)


def _scan_current_non_git(root: Path, *, allow_partial: bool = True) -> tuple[set[str], list[str]]:
    paths: set[str] = set()
    unsupported: list[str] = []
    count = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = current_path.relative_to(root)
        kept: list[str] = []
        for name in sorted(dirnames):
            rel = rel_current / name
            if _is_control_excluded(rel):
                continue
            if _is_policy_excluded(rel):
                unsupported.append(rel.as_posix())
                continue
            full = current_path / name
            if full.is_symlink():
                filenames.append(name)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            full = current_path / name
            rel = full.relative_to(root)
            if _is_control_excluded(rel) or _is_policy_excluded(rel):
                continue
            if full.is_symlink() and not path_is_within(full, root):
                unsupported.append(rel.as_posix())
                continue
            count += 1
            if count > _max_files():
                if allow_partial:
                    unsupported.append("__post_capture_limit__:file_count")
                    return paths, sorted(set(unsupported))
                raise ShellCaptureLimit("post-run filesystem scan exceeds file-count limit")
            paths.add(rel.as_posix())
    return paths, sorted(set(unsupported))


def _scope_for_root(root: Path) -> Optional[ResourceScope]:
    context = current_policy_context()
    scope = context.scope
    if scope is None:
        return None
    decision = evaluate_scope(
        scope,
        ScopeRequest(path=str(root), tool_family="terminal", access_mode=AccessMode.WORKSPACE_WRITE),
    )
    if not decision.allowed:
        raise ShellCaptureScopeError("reversible_root is outside the current policy scope")
    return scope


def begin_shell_capture(root: Path, *, require_full: bool = False) -> Dict[str, Any]:
    root = Path(root).expanduser().resolve(strict=False)
    if root.is_symlink() or not root.is_dir():
        raise ShellCaptureError("reversible_root must be an existing non-symlink directory")
    scope = _scope_for_root(root)
    context = current_policy_context()
    tx = begin_capture_transaction(
        "shell_reversible_run",
        actor=context.actor,
        agent_id=context.agent_id,
        metadata={"root": str(root), "require_full": bool(require_full)},
    )
    txid = str(tx["transaction_id"])
    capture = _capture_dir(txid)
    capture.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(capture, 0o700)
    except OSError:
        pass

    try:
        repo_root = _git_repo_root(root)
        if repo_root is not None and path_is_within(root, repo_root):
            pathspec = _git_pathspec(repo_root, root)
            base_commit = _run_git(repo_root, ["rev-parse", "HEAD"]).decode().strip()
            index_path = _git_index_path(repo_root)
            baseline_index_sha256 = _sha256_file(index_path) if index_path is not None else None
            tracked = _git_tracked(repo_root, pathspec)
            dirty = _git_dirty(repo_root, pathspec)
            untracked = _git_untracked(repo_root, pathspec)
            ignored = _git_ignored(repo_root, pathspec)
            baseline_paths = sorted(dirty.union(untracked))
            unsupported = list(sorted(ignored))
            uncovered: list[str] = []
            if len(baseline_paths) > _max_files():
                if require_full:
                    raise ShellCaptureLimit("git dirty/untracked baseline exceeds file-count limit")
                uncovered.extend(baseline_paths[_max_files():])
                baseline_paths = baseline_paths[:_max_files()]
                unsupported.append("__capture_limit__:file_count")
            total = 0
            rows: Dict[str, Dict[str, Any]] = {}
            base = capture / "preimages"
            for rel in baseline_paths:
                path = repo_root / rel
                kind = _path_kind(path)
                if kind == "absent":
                    continue
                if kind == "symlink" and not path_is_within(path, root):
                    unsupported.append(rel)
                    continue
                if kind not in {"file", "symlink"}:
                    raise ShellCaptureLimit(f"unsupported git baseline object: {rel}")
                size = len(os.readlink(path).encode()) if kind == "symlink" else path.stat().st_size
                if size > _max_file_bytes():
                    if require_full:
                        raise ShellCaptureLimit(f"git baseline contains an oversized file: {rel}")
                    uncovered.append(rel)
                    unsupported.append(rel)
                    continue
                if total + max(0, size) > _max_bytes():
                    if require_full:
                        raise ShellCaptureLimit("git dirty/untracked baseline exceeds byte limit")
                    uncovered.append(rel)
                    unsupported.append(rel)
                    continue
                total += max(0, size)
                _copy_preimage(path, base / rel)
                rows[rel] = {
                    "kind": kind,
                    "fingerprint": path_revision(path, scope if scope and scope.path_roots is not None else None),
                    "mode": _file_mode(path),
                    "tracked": rel in tracked,
                }
            unsupported = sorted(set(unsupported))
            if require_full and unsupported:
                raise ShellCaptureLimit("git ignored paths are outside reversible coverage")
            baseline_dirs, baseline_empty_dirs, dir_unsupported = _scan_dirs(root)
            unsupported = sorted(set(unsupported).union(dir_unsupported))
            if require_full and unsupported:
                raise ShellCaptureLimit("git ignored/generated/symlink paths are outside reversible coverage")
            metadata = {
                "mode": "git",
                "root": str(root),
                "repo_root": str(repo_root),
                "pathspec": pathspec,
                "base_commit": base_commit,
                "baseline_index_sha256": baseline_index_sha256,
                "tracked": sorted(tracked),
                "baseline_dirty": sorted(dirty),
                "baseline_untracked": sorted(untracked),
                "baseline_rows": rows,
                "baseline_uncovered": sorted(set(uncovered)),
                "baseline_dirs": sorted(baseline_dirs),
                "baseline_empty_dirs": sorted(baseline_empty_dirs),
                "baseline_ignored": unsupported,
                "scope": scope.to_dict() if scope is not None else None,
            }
        else:
            rows, dirs, unsupported = _scan_non_git(root, allow_partial=not require_full)
            baseline_uncovered = sorted({
                rel for rel in unsupported
                if not str(rel).startswith("__")
                and ((root / rel).is_file() or (root / rel).is_symlink())
            })
            dir_set, empty_dirs, dir_unsupported = _scan_dirs(root)
            unsupported = sorted(set(unsupported).union(dir_unsupported))
            if require_full and unsupported:
                raise ShellCaptureLimit("generated/excluded paths are outside reversible coverage")
            _copy_non_git_baseline(root, txid, rows)
            metadata = {
                "mode": "snapshot",
                "root": str(root),
                "baseline_rows": rows,
                "baseline_uncovered": baseline_uncovered,
                "baseline_dirs": sorted(dir_set),
                "baseline_empty_dirs": sorted(empty_dirs),
                "baseline_unsupported": unsupported,
                "scope": scope.to_dict() if scope is not None else None,
            }
        _atomic_json(capture / "baseline.json", metadata)
        return {
            "transaction_id": txid,
            "root": str(root),
            "mode": metadata["mode"],
            "baseline_unsupported": (
                metadata.get("baseline_ignored") or metadata.get("baseline_unsupported") or []
            ),
            "require_full": bool(require_full),
        }
    except BaseException:
        try:
            abort_capture_transaction(txid, reason="capture_prepare_failed", outcome_unknown=False)
        except Exception:
            pass
        shutil.rmtree(capture, ignore_errors=True)
        raise


def _baseline(transaction_id: str) -> Dict[str, Any]:
    return _read_json(_capture_dir(transaction_id) / "baseline.json")


def _capture_changed_git(transaction_id: str, baseline: Dict[str, Any], scope: Optional[ResourceScope]) -> tuple[list[str], list[Dict[str, Any]]]:
    repo_root = Path(str(baseline["repo_root"]))
    root = Path(str(baseline["root"]))
    pathspec = str(baseline["pathspec"])
    base_commit = str(baseline["base_commit"])
    tracked = set(baseline.get("tracked") or [])
    baseline_dirty = set(baseline.get("baseline_dirty") or [])
    baseline_untracked = set(baseline.get("baseline_untracked") or [])
    rows = dict(baseline.get("baseline_rows") or {})
    uncovered = set(baseline.get("baseline_uncovered") or [])

    post_dirty = _git_changed_from(repo_root, base_commit, pathspec)
    post_untracked = _git_untracked(repo_root, pathspec)
    post_ignored = _git_ignored(repo_root, pathspec)
    current_dirs, _current_empty_dirs, dir_unsupported = _scan_dirs(root)
    candidates = set(post_dirty).union(baseline_dirty).union(post_untracked).union(baseline_untracked)
    changed: list[str] = _capture_new_directories(
        transaction_id, root, baseline.get("baseline_dirs") or [], current_dirs, scope,
    )
    unsupported: list[Dict[str, Any]] = []
    preimages = _capture_dir(transaction_id) / "preimages"
    materialized = _capture_dir(transaction_id) / "git-preimages"

    for rel in sorted(candidates):
        target = repo_root / rel
        if rel in uncovered:
            unsupported.append({"path": str(target), "reason": "baseline_capture_limit"})
            continue
        baseline_row = rows.get(rel)
        if baseline_row is not None:
            before_fp = str(baseline_row.get("fingerprint") or "")
            try:
                current_fp = path_revision(target, scope if scope and scope.path_roots is not None else None)
            except FileTransactionError:
                current_fp = ""
            if before_fp and before_fp == current_fp:
                continue
            capture_transaction_snapshot(
                transaction_id,
                target,
                before_kind=str(baseline_row.get("kind") or "file"),
                before_source=preimages / rel,
                before_fingerprint=before_fp or None,
                before_mode=baseline_row.get("mode"),
                scope=scope if scope and scope.path_roots is not None else None,
            )
            changed.append(str(target))
            continue

        if rel in tracked:
            source = materialized / rel
            try:
                kind, mode = _materialize_git_preimage(repo_root, base_commit, rel, source)
            except ShellCaptureError:
                unsupported.append({"path": str(target), "reason": "git_preimage_unavailable"})
                continue
            before_fp = path_revision(source)
            capture_transaction_snapshot(
                transaction_id,
                target,
                before_kind=kind,
                before_source=source,
                before_fingerprint=before_fp,
                before_mode=mode,
                scope=scope if scope and scope.path_roots is not None else None,
            )
            changed.append(str(target))
        else:
            capture_transaction_snapshot(
                transaction_id,
                target,
                before_kind="absent",
                scope=scope if scope and scope.path_roots is not None else None,
            )
            changed.append(str(target))

    changed.extend(_capture_deleted_empty_directories(
        transaction_id, root, baseline.get("baseline_empty_dirs") or [], current_dirs, scope,
    ))
    baseline_ignored = set(baseline.get("baseline_ignored") or [])
    for rel in sorted(post_ignored.symmetric_difference(baseline_ignored)):
        unsupported.append({"path": str(repo_root / rel), "reason": "git_ignored_path_changed"})
    post_head = _run_git(repo_root, ["rev-parse", "HEAD"], check=False).decode().strip()
    if post_head and post_head != base_commit:
        unsupported.append({"path": str(repo_root), "reason": "git_head_changed"})
    index_path = _git_index_path(repo_root)
    post_index_sha256 = _sha256_file(index_path) if index_path is not None else None
    if post_index_sha256 != baseline.get("baseline_index_sha256"):
        unsupported.append({"path": str(repo_root), "reason": "git_index_changed"})
    for rel in dir_unsupported:
        unsupported.append({"path": str(root / rel), "reason": "policy_excluded_path_unobserved"})
    return changed, unsupported


def _capture_changed_non_git(transaction_id: str, baseline: Dict[str, Any], scope: Optional[ResourceScope]) -> tuple[list[str], list[Dict[str, Any]]]:
    root = Path(str(baseline["root"]))
    rows = dict(baseline.get("baseline_rows") or {})
    uncovered = set(baseline.get("baseline_uncovered") or [])
    current_paths, post_unsupported = _scan_current_non_git(root, allow_partial=True)
    current_dirs, _current_empty_dirs, dir_unsupported = _scan_dirs(root)
    candidates = set(rows).union(current_paths)
    preimages = _capture_dir(transaction_id) / "preimages"
    changed: list[str] = _capture_new_directories(
        transaction_id, root, baseline.get("baseline_dirs") or [], current_dirs, scope,
    )

    for rel in sorted(candidates):
        target = root / rel
        if rel in uncovered:
            continue
        row = rows.get(rel)
        if row is None:
            capture_transaction_snapshot(
                transaction_id, target, before_kind="absent",
                scope=scope if scope and scope.path_roots is not None else None,
            )
            changed.append(str(target))
            continue
        before_fp = str(row.get("fingerprint") or "")
        current_fp = path_revision(target, scope if scope and scope.path_roots is not None else None)
        if before_fp == current_fp:
            continue
        capture_transaction_snapshot(
            transaction_id,
            target,
            before_kind=str(row.get("kind") or "file"),
            before_source=preimages / rel,
            before_fingerprint=before_fp,
            before_mode=row.get("mode"),
            scope=scope if scope and scope.path_roots is not None else None,
        )
        changed.append(str(target))

    changed.extend(_capture_deleted_empty_directories(
        transaction_id, root, baseline.get("baseline_empty_dirs") or [], current_dirs, scope,
    ))
    unsupported_names = set(baseline.get("baseline_unsupported") or []).union(post_unsupported).union(dir_unsupported)
    unsupported = []
    for rel in sorted(unsupported_names):
        if str(rel).startswith("__"):
            unsupported.append({"path": str(root), "reason": str(rel).strip("_").replace(":", "_")})
        else:
            unsupported.append({"path": str(root / rel), "reason": "policy_excluded_path_unobserved"})
    return changed, unsupported


def finalize_shell_capture(
    transaction_id: str,
    *,
    join_transaction_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    baseline = _baseline(transaction_id)
    scope_dict = baseline.get("scope")
    scope = ResourceScope.from_dict(scope_dict) if isinstance(scope_dict, dict) else None
    if baseline.get("mode") == "git":
        changed, unsupported = _capture_changed_git(transaction_id, baseline, scope)
    else:
        changed, unsupported = _capture_changed_non_git(transaction_id, baseline, scope)

    require_full = bool((get_transaction(transaction_id).get("metadata") or {}).get("require_full"))
    reversibility = "partial" if unsupported else "full"
    receipt = finalize_capture_transaction(
        transaction_id,
        scope=scope if scope and scope.path_roots is not None else None,
        reversibility=reversibility,
        unsupported=unsupported,
        metadata={
            "changed_paths": changed,
            "changed_count": len(changed),
            "coverage": "scoped_workspace_policy",
            "full_reversibility_requested": require_full,
        },
    )
    try:
        shutil.rmtree(_capture_dir(transaction_id), ignore_errors=True)
    except OSError:
        pass

    join_ids = [str(value) for value in (join_transaction_ids or []) if str(value or "").strip()]
    if join_ids:
        capture_manifest = get_transaction(transaction_id)
        creator = dict(capture_manifest.get("creator") or {})
        receipt = compose_transactions(
            "compound_reversible_run",
            [*join_ids, transaction_id],
            actor=creator.get("actor"),
            agent_id=creator.get("agent_id"),
        )
    return {
        **receipt,
        "changed_paths": changed,
        "changed_count": len(changed),
        "capture_mode": baseline.get("mode"),
        "coverage": "scoped_workspace_policy",
        "full_reversibility_requested": require_full,
        "full_reversibility_met": reversibility == "full",
    }


def shell_capture_http_error(exc: BaseException) -> HTTPException:
    if isinstance(exc, ShellCaptureScopeError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, (ShellCaptureLimit, TransactionIrreversible)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error = getattr(exc, "code", None) or (
        exc.code if isinstance(exc, FileTransactionError) else "shell_capture_error"
    )
    return HTTPException(
        code,
        {"error": error, "message": str(exc), "action_executed": False},
    )
