from __future__ import annotations

import os
import shutil
import stat as stat_module
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, status

from .security import Settings, resolve_path, truncate
from .policy import current_policy_context
from .policy_scope import AccessMode, ResourceScope, ScopeRequest, evaluate_scope
from .scoped_fs import (
    ScopedFilesystemError, scope_needs_path_guard, scoped_copy, scoped_create_directory,
    scoped_delete, scoped_directory_tree, scoped_find, scoped_get_info, scoped_list_directory,
    scoped_move, scoped_read_text, scoped_stat, scoped_write_text_atomic,
)
from .file_transactions import (
    FileTransactionError, TransactionConflict, TransactionExpired, TransactionIrreversible,
    TransactionNotFound, TransactionPrepareFailed, TransactionRestoreFailed,
    commit_transaction, prepare_transaction, rollback_transaction, transaction_paths, undo_transaction,
)

MAX_READ_CHARS = 200_000
_FILE_OPERATION_LOCK = threading.RLock()


def _current_path_scope() -> Optional[ResourceScope]:
    scope = current_policy_context().scope
    return scope if scope_needs_path_guard(scope) else None


def _scoped_http_error(exc: ScopedFilesystemError) -> HTTPException:
    if exc.reason == "path_missing":
        code = status.HTTP_404_NOT_FOUND
    elif exc.reason in {"recursive_required", "not_regular_file", "invalid_search_pattern"}:
        code = status.HTTP_400_BAD_REQUEST
    elif exc.reason in {"destination_exists", "cross_device_move"}:
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_403_FORBIDDEN
    return HTTPException(code, {"error": exc.code, "reason": exc.reason, "message": str(exc)})


def _scoped_call(fn):
    try:
        return fn()
    except ScopedFilesystemError as exc:
        raise _scoped_http_error(exc) from exc


def _scoped_final_destination(scope: ResourceScope, source: Path, destination: Path) -> Path:
    try:
        st = scoped_stat(scope, destination, access_mode=AccessMode.WORKSPACE_WRITE)
    except ScopedFilesystemError as exc:
        if exc.reason == "path_missing":
            return destination
        raise _scoped_http_error(exc) from exc
    if stat_module.S_ISLNK(st.st_mode):
        raise _scoped_http_error(ScopedFilesystemError("symlink_final_target", "Scoped move refuses a symlink destination."))
    return destination / source.name if stat_module.S_ISDIR(st.st_mode) else destination


# ── Transaction helpers ──────────────────────────────────────────────────────

def _write_text_atomic(target: Path, content: str) -> int:
    scope = _current_path_scope()
    if scope is not None:
        return _scoped_call(lambda: scoped_write_text_atomic(scope, target, content))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    previous_mode: Optional[int] = None
    if target.exists() and target.is_file():
        try:
            previous_mode = stat_module.S_IMODE(target.stat().st_mode)
        except OSError:
            previous_mode = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(tmp, previous_mode)
        os.replace(tmp, target)
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)
    return len(encoded)


def _delete_raw(target: Path, *, recursive: bool) -> None:
    scope = _current_path_scope()
    if scope is not None:
        return _scoped_call(lambda: scoped_delete(scope, target, recursive=recursive))
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {target}")
    if target.is_dir():
        if not recursive:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Path is a directory. Set recursive=true to delete it.")
        shutil.rmtree(str(target))
    else:
        target.unlink()


def _missing_parent_root(target: Path) -> Optional[Path]:
    current = target.parent
    missing: list[Path] = []
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    return missing[-1] if missing else None


def _transaction_targets(primary: List[Path]) -> List[Path]:
    targets: list[Path] = []
    seen: set[str] = set()
    for target in primary:
        for candidate in (target, _missing_parent_root(target)):
            if candidate is None:
                continue
            key = (str(Path(os.path.abspath(str(candidate)))) if _current_path_scope() is not None
                   else str(candidate.resolve(strict=False)))
            if key not in seen:
                seen.add(key)
                targets.append(candidate)
    return targets


def _prepare_file_transaction(operation: str, paths: List[Path], *, require_undoable: bool = False) -> Dict[str, Any]:
    context = current_policy_context()
    try:
        return prepare_transaction(
            operation, _transaction_targets(paths), require_undoable=require_undoable,
            actor=context.actor, agent_id=context.agent_id, scope=_current_path_scope(),
        )
    except FileTransactionError as exc:
        code = status.HTTP_409_CONFLICT if isinstance(exc, TransactionIrreversible) else status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(code, {"error": exc.code, "message": str(exc), "action_executed": False}) from exc


def _finish_file_transaction(transaction_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        receipt = commit_transaction(transaction_id, scope=_current_path_scope())
    except FileTransactionError as exc:
        try:
            rollback_transaction(transaction_id, scope=_current_path_scope())
        except FileTransactionError:
            pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"error": exc.code, "message": "Filesystem action could not be durably committed and was rolled back when possible.",
             "transaction_id": transaction_id},
        ) from exc
    return {**result, **receipt}


def _rollback_after_error(transaction_id: str, exc: BaseException) -> None:
    try:
        receipt = rollback_transaction(transaction_id, scope=_current_path_scope())
    except FileTransactionError as rollback_exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"error": rollback_exc.code, "message": "Filesystem action failed and rollback could not be verified.",
             "transaction_id": transaction_id, "outcome": "unknown"},
        ) from exc
    if isinstance(exc, HTTPException):
        raise HTTPException(
            exc.status_code,
            {"error": "filesystem_action_failed_rolled_back", "message": str(exc.detail),
             "transaction_id": transaction_id, "rolled_back": True, "transaction": receipt},
        ) from exc
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        {"error": "filesystem_action_failed_rolled_back", "message": exc.__class__.__name__,
         "transaction_id": transaction_id, "rolled_back": True, "transaction": receipt},
    ) from exc


def _journaled(operation: str, paths: List[Path], mutate: Callable[[], Dict[str, Any]], *, require_undoable: bool = False) -> Dict[str, Any]:
    with _FILE_OPERATION_LOCK:
        prepared = _prepare_file_transaction(operation, paths, require_undoable=require_undoable)
        transaction_id = str(prepared["transaction_id"])
        try:
            result = mutate()
        except BaseException as exc:
            _rollback_after_error(transaction_id, exc)
            raise AssertionError("unreachable")
        return _finish_file_transaction(transaction_id, result)


# ── Write ────────────────────────────────────────────────────────────────────

def write_file(settings: Settings, path: str, content: str) -> Dict[str, Any]:
    target = resolve_path(path)
    def mutate() -> Dict[str, Any]:
        size = _write_text_atomic(target, content)
        return {"ok": True, "path": str(target), "bytes": size}
    return _journaled("write_file", [target], mutate)


def write_files_batch(settings: Settings, files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
    with _FILE_OPERATION_LOCK:
        return _write_files_batch_locked(settings, files, atomic=atomic)


def _write_files_batch_locked(settings: Settings, files: List[Dict[str, str]], atomic: bool = True) -> Dict[str, Any]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "files list is empty.")
    prepared_items: list[tuple[Path, str]] = []
    for item in files:
        p, c = item.get("path", ""), item.get("content", "")
        if not p:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each file needs a 'path'.")
        prepared_items.append((resolve_path(p), c))
    prepared = _prepare_file_transaction(
        "write_files_batch", [target for target, _ in prepared_items], require_undoable=bool(atomic),
    )
    transaction_id = str(prepared["transaction_id"])
    written: list[str] = []
    try:
        for target, content in prepared_items:
            _write_text_atomic(target, content)
            written.append(str(target))
    except BaseException as exc:
        if atomic:
            _rollback_after_error(transaction_id, exc)
            raise AssertionError("unreachable")
        try:
            receipt = commit_transaction(transaction_id, scope=_current_path_scope())
        except FileTransactionError as commit_exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                {"error": commit_exc.code, "message": "Partial batch write could not be journaled safely.",
                 "transaction_id": transaction_id, "outcome": "unknown"},
            ) from exc
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {"error": "partial_batch_write", "message": exc.__class__.__name__, "transaction_id": transaction_id,
             "written_count": len(written), "transaction": receipt},
        ) from exc
    return _finish_file_transaction(
        transaction_id, {"ok": True, "written_count": len(written), "written": written, "atomic": bool(atomic)},
    )


def file_transaction_batch(settings: Settings, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    with _FILE_OPERATION_LOCK:
        return _file_transaction_batch_locked(settings, actions)


def _file_transaction_batch_locked(settings: Settings, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute write/move/delete actions as one reversible all-or-nothing filesystem transaction."""
    if not actions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions list is empty.")
    if len(actions) > 50:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "actions list is limited to 50 items.")

    normalized: list[Dict[str, Any]] = []
    targets: list[Path] = []
    for index, item in enumerate(actions):
        kind = str(item.get("type") or "").strip().lower()
        if kind == "write":
            raw_path = str(item.get("path") or "").strip()
            if not raw_path or not isinstance(item.get("content", ""), str):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Action {index} write requires path and string content.")
            target = resolve_path(raw_path)
            normalized.append({"type": kind, "path": target, "content": item.get("content", "")})
            targets.append(target)
        elif kind == "move":
            raw_source = str(item.get("source") or "").strip()
            raw_destination = str(item.get("destination") or "").strip()
            if not raw_source or not raw_destination:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Action {index} move requires source and destination.")
            src = resolve_path(raw_source)
            dst = resolve_path(raw_destination)
            scope = _current_path_scope()
            final_dst = _scoped_final_destination(scope, src, dst) if scope is not None else ((dst / src.name) if dst.exists() and dst.is_dir() else dst)
            normalized.append({"type": kind, "source": src, "destination": dst, "final_destination": final_dst})
            targets.extend((src, final_dst))
        elif kind == "delete":
            raw_path = str(item.get("path") or "").strip()
            if not raw_path:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Action {index} delete requires path.")
            target = resolve_path(raw_path)
            normalized.append({"type": kind, "path": target, "recursive": bool(item.get("recursive", False))})
            targets.append(target)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Action {index} type must be write, move, or delete.")

    prepared = _prepare_file_transaction("file_transaction_batch", targets, require_undoable=True)
    transaction_id = str(prepared["transaction_id"])
    results: list[Dict[str, Any]] = []
    try:
        for index, action in enumerate(normalized):
            kind = action["type"]
            if kind == "write":
                target = action["path"]
                size = _write_text_atomic(target, action["content"])
                results.append({"index": index, "type": kind, "ok": True, "path": str(target), "bytes": size})
            elif kind == "move":
                src, dst = action["source"], action["destination"]
                scope = _current_path_scope()
                if scope is not None:
                    actual = _scoped_call(lambda: scoped_move(scope, src, dst))
                else:
                    if not src.exists():
                        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Batch move source not found at action {index}.")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    actual = Path(shutil.move(str(src), str(dst))).resolve(strict=False)
                results.append({"index": index, "type": kind, "ok": True, "source": str(src), "destination": str(actual)})
            else:
                target = action["path"]
                try:
                    _delete_raw(target, recursive=bool(action["recursive"]))
                except HTTPException as exc:
                    raise HTTPException(exc.status_code, f"Batch delete failed at action {index}: {exc.detail}") from exc
                results.append({"index": index, "type": kind, "ok": True, "deleted": str(target)})
    except BaseException as exc:
        _rollback_after_error(transaction_id, exc)
        raise AssertionError("unreachable")
    return _finish_file_transaction(
        transaction_id, {"ok": True, "atomic": True, "action_count": len(results), "actions": results},
    )


# ── Read ─────────────────────────────────────────────────────────────────────

def read_file(settings: Settings, path: str, offset: int = 0, length: Optional[int] = None) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        content = _scoped_call(lambda: scoped_read_text(scope, target, errors="replace"))
    else:
        if not target.exists() or not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"File not found: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
    if offset or length is not None:
        lines = content.splitlines(keepends=True)
        sliced = lines[offset: offset + length if length else None]
        content = "".join(sliced)
    bounded, truncated = truncate(content, MAX_READ_CHARS)
    return {"ok": True, "path": str(target), "content": bounded, "truncated": truncated}


def read_multiple_files(settings: Settings, paths: List[str]) -> Dict[str, Any]:
    results = []
    for path in paths:
        try:
            target = resolve_path(path)
            scope = _current_path_scope()
            if scope is not None:
                try:
                    content = scoped_read_text(scope, target, errors="replace")
                except ScopedFilesystemError as exc:
                    if exc.reason not in {"path_missing"}:
                        raise _scoped_http_error(exc) from exc
                    results.append({"path": path, "error": "Not found", "status": "error"})
                    continue
                bounded, _ = truncate(content, 50_000)
                results.append({"path": path, "content": bounded, "status": "ok"})
            elif target.exists() and target.is_file():
                content = target.read_text(encoding="utf-8", errors="replace")
                bounded, _ = truncate(content, 50_000)
                results.append({"path": path, "content": bounded, "status": "ok"})
            else:
                results.append({"path": path, "error": "Not found", "status": "error"})
        except HTTPException:
            raise
        except Exception as e:
            results.append({"path": path, "error": str(e), "status": "error"})
    return {"ok": True, "files": results}


# ── Edit (find & replace) ────────────────────────────────────────────────────

def edit_file(settings: Settings, path: str, old_string: str, new_string: str,
              expected_replacements: int = 1) -> Dict[str, Any]:
    """Find-and-replace in a file. Fails if count doesn't match expected_replacements."""
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        content = _scoped_call(lambda: scoped_read_text(scope, target, errors="strict"))
    else:
        if not target.exists() or not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"File not found: {path}")
        content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "old_string not found in file.")
    if count != expected_replacements:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Found {count} occurrences but expected {expected_replacements}. "
                            "Be more specific or set expected_replacements correctly.")
    new_content = content.replace(old_string, new_string)
    def mutate() -> Dict[str, Any]:
        _write_text_atomic(target, new_content)
        return {"ok": True, "path": str(target), "replacements": count}
    return _journaled("edit_file", [target], mutate)


# ── Directory ops ─────────────────────────────────────────────────────────────

def list_directory(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        rows = _scoped_call(lambda: scoped_list_directory(scope, target))
        entries = [{**row, "modified": time.ctime(float(row["modified"]))} for row in rows]
    else:
        if not target.exists() or not target.is_dir():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Directory not found: {path}")
        entries = []
        for entry in sorted(target.iterdir()):
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size": stat.st_size if entry.is_file() else None,
                "modified": time.ctime(stat.st_mtime),
            })
    return {"ok": True, "path": str(target), "count": len(entries), "entries": entries}


def directory_tree(settings: Settings, path: str, depth: int = 3) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        tree = _scoped_call(lambda: scoped_directory_tree(scope, target, depth))
        return {"ok": True, "path": str(target), "tree": tree}
    if not target.exists() or not target.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Directory not found: {path}")

    def build(p: Path, d: int) -> Dict:
        node: Dict[str, Any] = {"name": p.name, "type": "directory"}
        if d <= 0:
            node["children"] = ["..."]
            return node
        children = []
        try:
            for entry in sorted(p.iterdir()):
                if entry.is_dir():
                    children.append(build(entry, d - 1))
                else:
                    children.append({"name": entry.name, "type": "file", "size": entry.stat().st_size})
        except PermissionError:
            pass
        node["children"] = children
        return node

    return {"ok": True, "path": str(target), "tree": build(target, depth)}


def create_directory(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        _scoped_call(lambda: scoped_create_directory(scope, target))
    else:
        target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target)}


# ── Move / Copy / Delete ──────────────────────────────────────────────────────

def move_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    src = resolve_path(source)
    dst = resolve_path(destination)
    scope = _current_path_scope()
    if scope is None and not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Source not found: {source}")
    final_dst = _scoped_final_destination(scope, src, dst) if scope is not None else ((dst / src.name) if dst.exists() and dst.is_dir() else dst)
    def mutate() -> Dict[str, Any]:
        if scope is not None:
            actual = _scoped_call(lambda: scoped_move(scope, src, dst))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            actual = Path(shutil.move(str(src), str(dst))).resolve(strict=False)
        return {"ok": True, "source": str(src), "destination": str(actual)}
    return _journaled("move_file", [src, final_dst], mutate)


def copy_file(settings: Settings, source: str, destination: str) -> Dict[str, Any]:
    src = resolve_path(source)
    dst = resolve_path(destination)
    scope = _current_path_scope()
    if scope is not None:
        src_stat = _scoped_call(lambda: scoped_stat(scope, src, access_mode=AccessMode.READ_ONLY))
        if stat_module.S_ISLNK(src_stat.st_mode):
            raise _scoped_http_error(ScopedFilesystemError("symlink_source", "Scoped copy refuses a symlink source."))
        final_dst = _scoped_final_destination(scope, src, dst) if stat_module.S_ISREG(src_stat.st_mode) else dst
        actual = _scoped_call(lambda: scoped_copy(scope, src, final_dst))
        return {"ok": True, "source": str(src), "destination": str(actual)}
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Source not found: {source}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return {"ok": True, "source": str(src), "destination": str(dst)}


def delete_path(settings: Settings, path: str, recursive: bool = False) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is None:
        if not target.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
        if target.is_dir() and not recursive:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Path is a directory. Set recursive=true to delete it.")
    def mutate() -> Dict[str, Any]:
        _delete_raw(target, recursive=recursive)
        return {"ok": True, "deleted": str(target)}
    return _journaled("delete_path", [target], mutate)


def undo_file_transaction(settings: Settings, transaction_id: str, force: bool = False) -> Dict[str, Any]:
    with _FILE_OPERATION_LOCK:
        return _undo_file_transaction_locked(settings, transaction_id, force=force)


def _undo_file_transaction_locked(settings: Settings, transaction_id: str, force: bool = False) -> Dict[str, Any]:
    context = current_policy_context()
    try:
        paths = transaction_paths(transaction_id)
    except FileTransactionError as exc:
        code = status.HTTP_404_NOT_FOUND if isinstance(exc, TransactionNotFound) else status.HTTP_409_CONFLICT
        raise HTTPException(code, {"error": exc.code, "message": str(exc)}) from exc
    if context.scope is not None:
        for path in paths:
            decision = evaluate_scope(
                context.scope,
                ScopeRequest(path=path, tool_family="files", access_mode=AccessMode.WORKSPACE_WRITE),
            )
            if not decision.allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    {"error": "scope_denied", "message": "Transaction contains a path outside the current scope.",
                     "reasons": list(decision.reasons)},
                )
    try:
        receipt = undo_transaction(transaction_id, force=force, scope=_current_path_scope())
    except FileTransactionError as exc:
        if isinstance(exc, TransactionNotFound):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, TransactionExpired):
            code = status.HTTP_410_GONE
        else:
            code = status.HTTP_409_CONFLICT
        raise HTTPException(code, {"error": exc.code, "message": str(exc)}) from exc
    return {"ok": True, **receipt}


# ── File info ─────────────────────────────────────────────────────────────────

def get_file_info(settings: Settings, path: str) -> Dict[str, Any]:
    target = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        info = _scoped_call(lambda: scoped_get_info(scope, target))
        return {
            "ok": True, "path": str(target), "type": info["type"], "size": info["size"],
            "created": time.ctime(float(info["created"])), "modified": time.ctime(float(info["modified"])),
            "mode": oct(int(info["mode"])), "suffix": target.suffix,
        }
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
    stat = target.stat()
    return {
        "ok": True,
        "path": str(target),
        "type": "directory" if target.is_dir() else "file",
        "size": stat.st_size,
        "created": time.ctime(stat.st_ctime),
        "modified": time.ctime(stat.st_mtime),
        "mode": oct(stat.st_mode),
        "suffix": target.suffix,
    }



# ── Find files by name ────────────────────────────────────────────────────────

def find_files(settings: Settings, pattern: str, path: str = str(Path.home()),
               file_type: str = "any") -> Dict[str, Any]:
    """Find files/directories by name pattern (glob). file_type: file | dir | any."""
    root = resolve_path(path)
    scope = _current_path_scope()
    if scope is not None:
        results = _scoped_call(lambda: scoped_find(scope, root, pattern, file_type, 500))
        return {"ok": True, "count": len(results), "results": results}
    if not root.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Path not found: {path}")
    results = []
    try:
        for match in sorted(root.rglob(pattern)):
            if file_type == "file" and not match.is_file():
                continue
            if file_type == "dir" and not match.is_dir():
                continue
            results.append({
                "path": str(match),
                "type": "directory" if match.is_dir() else "file",
                "size": match.stat().st_size if match.is_file() else None,
            })
            if len(results) >= 500:
                break
    except PermissionError:
        pass
    return {"ok": True, "count": len(results), "results": results}
