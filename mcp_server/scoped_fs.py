from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .policy_scope import AccessMode, ResourceScope, ScopeRequest, evaluate_scope


class ScopedFilesystemError(RuntimeError):
    """Fail-closed error for a scoped path that cannot be safely resolved at operation time."""

    code = "scoped_path_unsafe"

    def __init__(self, reason: str, message: str = "Scoped filesystem path is unsafe or changed during the operation.") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class GuardedParent:
    parent_fd: int
    name: str
    absolute: Path
    root: Path

    def close(self) -> None:
        try:
            os.close(self.parent_fd)
        except OSError:
            pass


def scope_needs_path_guard(scope: Optional[ResourceScope]) -> bool:
    return bool(scope is not None and scope.path_roots is not None)


def _absolute_lexical(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _matching_root(scope: ResourceScope, path: Path, access_mode: AccessMode | str) -> Path:
    decision = evaluate_scope(
        scope,
        ScopeRequest(path=str(path), access_mode=access_mode),
    )
    if not decision.allowed:
        raise ScopedFilesystemError("scope_denied", "Path is outside the current delegated resource scope.")
    roots = scope.path_roots
    if roots is None:
        raise ScopedFilesystemError("scope_unbounded", "Scoped path guard requires explicit path roots.")
    candidate = str(_absolute_lexical(path))
    matches: list[str] = []
    for raw_root in roots:
        root = str(_absolute_lexical(raw_root))
        try:
            if os.path.commonpath((candidate, root)) == root:
                matches.append(root)
        except ValueError:
            continue
    if not matches:
        raise ScopedFilesystemError("path_not_allowed", "Path is outside the current delegated resource scope.")
    return Path(max(matches, key=len))


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _translate_open_error(exc: OSError) -> ScopedFilesystemError:
    if exc.errno == errno.ELOOP:
        return ScopedFilesystemError("symlink_or_path_swap", "Scoped path contains a symlink or changed during the operation.")
    if exc.errno == errno.ENOTDIR:
        return ScopedFilesystemError("not_directory", "Scoped path component is no longer a directory.")
    if exc.errno == errno.ENOENT:
        return ScopedFilesystemError("path_missing", "Scoped path no longer exists.")
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return ScopedFilesystemError("permission_denied", "Scoped path cannot be accessed safely.")
    return ScopedFilesystemError("path_open_failed", "Scoped path could not be opened safely.")


def _open_absolute_dir_nofollow(path: Path) -> int:
    absolute = _absolute_lexical(path)
    fd = os.open("/", _dir_flags())
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _dir_flags(), dir_fd=fd)
            except OSError as exc:
                raise _translate_open_error(exc) from exc
            os.close(fd)
            fd = child
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _open_child_dir(parent_fd: int, name: str, *, create: bool = False) -> int:
    try:
        return os.open(name, _dir_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise ScopedFilesystemError("path_missing", "Scoped parent path no longer exists.")
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _translate_open_error(exc) from exc
        try:
            return os.open(name, _dir_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from exc
    except OSError as exc:
        raise _translate_open_error(exc) from exc


@contextmanager
def guarded_parent(
    scope: ResourceScope,
    path: str | os.PathLike[str],
    *,
    access_mode: AccessMode | str,
    create_parents: bool = False,
) -> Iterator[GuardedParent]:
    absolute = _absolute_lexical(path)
    root = _matching_root(scope, absolute, access_mode)
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ScopedFilesystemError("path_not_allowed") from exc
    parts = relative.parts
    if not parts:
        raise ScopedFilesystemError("scope_root_target", "Operation cannot use the scope root as a file target.")
    fd = _open_absolute_dir_nofollow(root)
    try:
        for part in parts[:-1]:
            child = _open_child_dir(fd, part, create=create_parents)
            os.close(fd)
            fd = child
        guarded = GuardedParent(parent_fd=fd, name=parts[-1], absolute=absolute, root=root)
        try:
            yield guarded
        finally:
            guarded.close()
            fd = -1
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def guarded_directory(
    scope: ResourceScope,
    path: str | os.PathLike[str],
    *,
    access_mode: AccessMode | str = AccessMode.READ_ONLY,
    create: bool = False,
) -> Iterator[tuple[int, Path]]:
    absolute = _absolute_lexical(path)
    root = _matching_root(scope, absolute, access_mode)
    if absolute == root:
        fd = _open_absolute_dir_nofollow(root)
    else:
        with guarded_parent(scope, absolute, access_mode=access_mode, create_parents=create) as guarded:
            try:
                fd = _open_child_dir(guarded.parent_fd, guarded.name, create=create)
            except ScopedFilesystemError:
                raise
    try:
        yield fd, absolute
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _lstat_guarded(guarded: GuardedParent) -> os.stat_result:
    try:
        return os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ScopedFilesystemError("path_missing", "Scoped path no longer exists.") from exc
    except OSError as exc:
        raise _translate_open_error(exc) from exc


def scoped_exists(scope: ResourceScope, path: Path, *, access_mode: AccessMode | str = AccessMode.READ_ONLY) -> bool:
    try:
        with guarded_parent(scope, path, access_mode=access_mode) as guarded:
            os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
            return True
    except (FileNotFoundError, ScopedFilesystemError) as exc:
        if isinstance(exc, ScopedFilesystemError) and exc.reason not in {"path_missing"}:
            raise
        return False


def scoped_stat(scope: ResourceScope, path: Path, *, access_mode: AccessMode | str = AccessMode.READ_ONLY) -> os.stat_result:
    with guarded_parent(scope, path, access_mode=access_mode) as guarded:
        return _lstat_guarded(guarded)


def scoped_kind(scope: ResourceScope, path: Path, *, access_mode: AccessMode | str = AccessMode.READ_ONLY) -> str:
    with guarded_parent(scope, path, access_mode=access_mode) as guarded:
        try:
            st = os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        return _entry_kind(st)


def scoped_read_bytes(scope: ResourceScope, path: Path) -> bytes:
    with guarded_parent(scope, path, access_mode=AccessMode.READ_ONLY) as guarded:
        try:
            fd = os.open(guarded.name, _file_read_flags(), dir_fd=guarded.parent_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ScopedFilesystemError("not_regular_file", "Scoped read target is not a regular file.")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


def scoped_read_text(scope: ResourceScope, path: Path, *, errors: str = "replace") -> str:
    return scoped_read_bytes(scope, path).decode("utf-8", errors=errors)


def scoped_write_text_atomic(scope: ResourceScope, path: Path, content: str) -> int:
    encoded = content.encode("utf-8")
    with guarded_parent(scope, path, access_mode=AccessMode.WORKSPACE_WRITE, create_parents=True) as guarded:
        previous_mode: Optional[int] = None
        try:
            st = os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ScopedFilesystemError("symlink_final_target", "Scoped write refuses a symlink target.")
            if not stat.S_ISREG(st.st_mode):
                raise ScopedFilesystemError("not_regular_file", "Scoped write target is not a regular file.")
            previous_mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            pass
        tmp_name = f".{guarded.name}.mac-mcp-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=guarded.parent_fd)
        except OSError as exc:
            raise _translate_open_error(exc) from exc
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            if previous_mode is not None:
                os.fchmod(fd, previous_mode)
        finally:
            os.close(fd)
        try:
            os.replace(tmp_name, guarded.name, src_dir_fd=guarded.parent_fd, dst_dir_fd=guarded.parent_fd)
            os.fsync(guarded.parent_fd)
        except OSError as exc:
            try:
                os.unlink(tmp_name, dir_fd=guarded.parent_fd)
            except OSError:
                pass
            raise _translate_open_error(exc) from exc
        return len(encoded)


def _remove_entry(parent_fd: int, name: str, *, recursive: bool) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ScopedFilesystemError("path_missing", "Scoped path no longer exists.") from exc
    if stat.S_ISDIR(st.st_mode):
        if not recursive:
            raise IsADirectoryError(name)
        child_fd = _open_child_dir(parent_fd, name)
        try:
            for child in os.listdir(child_fd):
                child_st = os.stat(child, dir_fd=child_fd, follow_symlinks=False)
                if stat.S_ISDIR(child_st.st_mode):
                    _remove_entry(child_fd, child, recursive=True)
                else:
                    os.unlink(child, dir_fd=child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        # Files and symlinks are removed as directory entries; symlink targets are never followed.
        os.unlink(name, dir_fd=parent_fd)


def scoped_delete(scope: ResourceScope, path: Path, *, recursive: bool) -> None:
    with guarded_parent(scope, path, access_mode=AccessMode.WORKSPACE_WRITE) as guarded:
        try:
            _remove_entry(guarded.parent_fd, guarded.name, recursive=recursive)
        except IsADirectoryError as exc:
            raise ScopedFilesystemError("recursive_required", "Scoped directory delete requires recursive=true.") from exc
        except OSError as exc:
            if isinstance(exc, ScopedFilesystemError):
                raise
            raise _translate_open_error(exc) from exc
        try:
            os.fsync(guarded.parent_fd)
        except OSError:
            pass


def scoped_create_directory(scope: ResourceScope, path: Path) -> None:
    absolute = _absolute_lexical(path)
    root = _matching_root(scope, absolute, AccessMode.WORKSPACE_WRITE)
    if absolute == root:
        with guarded_directory(scope, absolute, access_mode=AccessMode.WORKSPACE_WRITE):
            return
    with guarded_parent(scope, absolute, access_mode=AccessMode.WORKSPACE_WRITE, create_parents=True) as guarded:
        try:
            os.mkdir(guarded.name, 0o755, dir_fd=guarded.parent_fd)
        except FileExistsError:
            st = _lstat_guarded(guarded)
            if not stat.S_ISDIR(st.st_mode):
                raise ScopedFilesystemError("path_type_changed", "Scoped directory target is not a directory.")
            fd = _open_child_dir(guarded.parent_fd, guarded.name)
            os.close(fd)
        except OSError as exc:
            raise _translate_open_error(exc) from exc


def _copy_file_fd(src_fd: int, dst_parent_fd: int, dst_name: str, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    dst_fd = os.open(dst_name, flags, stat.S_IMODE(mode) or 0o600, dir_fd=dst_parent_fd)
    try:
        while True:
            chunk = os.read(src_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                n = os.write(dst_fd, view)
                view = view[n:]
        os.fsync(dst_fd)
        os.fchmod(dst_fd, stat.S_IMODE(mode))
    finally:
        os.close(dst_fd)


def _copy_tree_from_fd(src_fd: int, dst_fd: int) -> None:
    for name in sorted(os.listdir(src_fd)):
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            os.mkdir(name, stat.S_IMODE(st.st_mode) or 0o755, dir_fd=dst_fd)
            sfd = _open_child_dir(src_fd, name)
            dfd = _open_child_dir(dst_fd, name)
            try:
                _copy_tree_from_fd(sfd, dfd)
            finally:
                os.close(sfd); os.close(dfd)
        elif stat.S_ISREG(st.st_mode):
            sfd = os.open(name, _file_read_flags(), dir_fd=src_fd)
            try:
                _copy_file_fd(sfd, dst_fd, name, st.st_mode)
            finally:
                os.close(sfd)
        elif stat.S_ISLNK(st.st_mode):
            target = os.readlink(name, dir_fd=src_fd)
            os.symlink(target, name, dir_fd=dst_fd)
        else:
            raise ScopedFilesystemError("unsupported_file_type", "Scoped copy refuses special filesystem objects.")


def scoped_copy(scope: ResourceScope, source: Path, destination: Path) -> Path:
    with guarded_parent(scope, source, access_mode=AccessMode.READ_ONLY) as src_guard, \
         guarded_parent(scope, destination, access_mode=AccessMode.WORKSPACE_WRITE, create_parents=True) as dst_guard:
        src_st = _lstat_guarded(src_guard)
        if stat.S_ISLNK(src_st.st_mode):
            raise ScopedFilesystemError("symlink_source", "Scoped copy refuses a symlink source.")
        dst_st: Optional[os.stat_result] = None
        try:
            dst_st = os.stat(dst_guard.name, dir_fd=dst_guard.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if dst_st is not None and stat.S_ISLNK(dst_st.st_mode):
            raise ScopedFilesystemError("symlink_final_target", "Scoped copy refuses a symlink destination.")
        if stat.S_ISREG(src_st.st_mode):
            if dst_st is not None and not stat.S_ISREG(dst_st.st_mode):
                raise ScopedFilesystemError("destination_exists", "Scoped copy destination has an incompatible type.")
            sfd = os.open(src_guard.name, _file_read_flags(), dir_fd=src_guard.parent_fd)
            tmp_name = f".{dst_guard.name}.mac-mcp-copy-{uuid.uuid4().hex}.tmp"
            try:
                _copy_file_fd(sfd, dst_guard.parent_fd, tmp_name, src_st.st_mode)
                os.replace(tmp_name, dst_guard.name, src_dir_fd=dst_guard.parent_fd, dst_dir_fd=dst_guard.parent_fd)
                try: os.fsync(dst_guard.parent_fd)
                except OSError: pass
            except Exception:
                try: os.unlink(tmp_name, dir_fd=dst_guard.parent_fd)
                except OSError: pass
                raise
            finally:
                os.close(sfd)
        elif stat.S_ISDIR(src_st.st_mode):
            if dst_st is not None:
                raise ScopedFilesystemError("destination_exists", "Scoped directory copy destination already exists.")
            os.mkdir(dst_guard.name, stat.S_IMODE(src_st.st_mode) or 0o755, dir_fd=dst_guard.parent_fd)
            sfd = _open_child_dir(src_guard.parent_fd, src_guard.name)
            dfd = _open_child_dir(dst_guard.parent_fd, dst_guard.name)
            try:
                _copy_tree_from_fd(sfd, dfd)
            finally:
                os.close(sfd); os.close(dfd)
        else:
            raise ScopedFilesystemError("unsupported_file_type", "Scoped copy refuses special filesystem objects.")
        return destination


def scoped_move(scope: ResourceScope, source: Path, destination: Path) -> Path:
    with guarded_parent(scope, source, access_mode=AccessMode.WORKSPACE_WRITE) as src_guard:
        src_st = _lstat_guarded(src_guard)
        # Destination semantics match shutil.move: an existing directory receives source basename.
        try:
            with guarded_directory(scope, destination, access_mode=AccessMode.WORKSPACE_WRITE) as (dst_dir_fd, dst_abs):
                final_name = src_guard.name
                try:
                    os.rename(src_guard.name, final_name, src_dir_fd=src_guard.parent_fd, dst_dir_fd=dst_dir_fd)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise ScopedFilesystemError("cross_device_move", "Scoped cross-device move is not supported safely.") from exc
                    raise _translate_open_error(exc) from exc
                return dst_abs / final_name
        except ScopedFilesystemError as dir_exc:
            if dir_exc.reason not in {"path_missing", "not_directory", "path_open_failed"}:
                # A symlink or other unsafe destination is never treated as a normal missing path.
                if dir_exc.reason in {"symlink_or_path_swap", "scope_denied", "path_not_allowed"}:
                    raise
        with guarded_parent(scope, destination, access_mode=AccessMode.WORKSPACE_WRITE, create_parents=True) as dst_guard:
            try:
                os.rename(src_guard.name, dst_guard.name, src_dir_fd=src_guard.parent_fd, dst_dir_fd=dst_guard.parent_fd)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ScopedFilesystemError("cross_device_move", "Scoped cross-device move is not supported safely.") from exc
                raise _translate_open_error(exc) from exc
            return destination


def _entry_kind(st: os.stat_result) -> str:
    if stat.S_ISDIR(st.st_mode): return "directory"
    if stat.S_ISREG(st.st_mode): return "file"
    if stat.S_ISLNK(st.st_mode): return "symlink"
    return "other"


def scoped_list_directory(scope: ResourceScope, path: Path) -> list[Dict[str, Any]]:
    with guarded_directory(scope, path) as (fd, _):
        rows: list[Dict[str, Any]] = []
        for name in sorted(os.listdir(fd)):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            kind = _entry_kind(st)
            rows.append({
                "name": name,
                "type": kind,
                "size": st.st_size if kind == "file" else None,
                "modified": st.st_mtime,
            })
        return rows


def _tree_from_fd(fd: int, name: str, depth: int) -> Dict[str, Any]:
    node: Dict[str, Any] = {"name": name, "type": "directory"}
    if depth <= 0:
        node["children"] = ["..."]
        return node
    children: list[Any] = []
    for child in sorted(os.listdir(fd)):
        st = os.stat(child, dir_fd=fd, follow_symlinks=False)
        kind = _entry_kind(st)
        if kind == "directory":
            cfd = _open_child_dir(fd, child)
            try:
                children.append(_tree_from_fd(cfd, child, depth - 1))
            finally:
                os.close(cfd)
        else:
            children.append({"name": child, "type": kind, "size": st.st_size if kind == "file" else None})
    node["children"] = children
    return node


def scoped_directory_tree(scope: ResourceScope, path: Path, depth: int) -> Dict[str, Any]:
    with guarded_directory(scope, path) as (fd, absolute):
        return _tree_from_fd(fd, absolute.name or str(absolute), depth)


def scoped_get_info(scope: ResourceScope, path: Path) -> Dict[str, Any]:
    with guarded_parent(scope, path, access_mode=AccessMode.READ_ONLY) as guarded:
        st = _lstat_guarded(guarded)
        return {
            "type": _entry_kind(st),
            "size": st.st_size,
            "created": st.st_ctime,
            "modified": st.st_mtime,
            "mode": st.st_mode,
        }


def _walk_find(fd: int, base: Path, pattern: str, file_type: str, results: list[Dict[str, Any]], limit: int) -> None:
    import fnmatch
    for name in sorted(os.listdir(fd)):
        if len(results) >= limit:
            return
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        kind = _entry_kind(st)
        current = base / name
        if fnmatch.fnmatch(name, pattern):
            if file_type == "any" or (file_type == "file" and kind == "file") or (file_type == "dir" and kind == "directory"):
                results.append({"path": str(current), "type": kind, "size": st.st_size if kind == "file" else None})
        if kind == "directory":
            cfd = _open_child_dir(fd, name)
            try:
                _walk_find(cfd, current, pattern, file_type, results, limit)
            finally:
                os.close(cfd)


def scoped_find(scope: ResourceScope, path: Path, pattern: str, file_type: str, limit: int = 500) -> list[Dict[str, Any]]:
    with guarded_directory(scope, path) as (fd, absolute):
        results: list[Dict[str, Any]] = []
        _walk_find(fd, absolute, pattern, file_type, results, limit)
        return results


def scoped_fingerprint(scope: ResourceScope, path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with guarded_parent(scope, path, access_mode=AccessMode.READ_ONLY) as guarded:
            try:
                st = os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                digest.update(b"absent\0")
                return digest.hexdigest()
            kind = _entry_kind(st)
            digest.update((kind + "\0").encode())
            if kind == "file":
                fd = os.open(guarded.name, _file_read_flags(), dir_fd=guarded.parent_fd)
                try:
                    digest.update(str(st.st_size).encode()); digest.update(b"\0")
                    while True:
                        chunk = os.read(fd, 1024 * 1024)
                        if not chunk: break
                        digest.update(chunk)
                finally:
                    os.close(fd)
            elif kind == "symlink":
                digest.update(os.readlink(guarded.name, dir_fd=guarded.parent_fd).encode("utf-8", errors="surrogateescape"))
            elif kind == "directory":
                dfd = _open_child_dir(guarded.parent_fd, guarded.name)
                try:
                    _fingerprint_dir_fd(dfd, digest, Path())
                finally:
                    os.close(dfd)
            else:
                digest.update(f"{st.st_mode}:{st.st_size}:{st.st_mtime_ns}".encode())
            return digest.hexdigest()
    except ScopedFilesystemError:
        raise


def _fingerprint_dir_fd(fd: int, digest: Any, prefix: Path) -> None:
    for name in sorted(os.listdir(fd)):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        kind = _entry_kind(st)
        rel = prefix / name
        digest.update(str(rel).encode("utf-8", errors="surrogateescape")); digest.update(b"\0")
        digest.update(kind.encode()); digest.update(b"\0")
        if kind == "file":
            ffd = os.open(name, _file_read_flags(), dir_fd=fd)
            try:
                digest.update(str(st.st_size).encode()); digest.update(b"\0")
                while True:
                    chunk = os.read(ffd, 1024 * 1024)
                    if not chunk: break
                    digest.update(chunk)
            finally:
                os.close(ffd)
        elif kind == "symlink":
            digest.update(os.readlink(name, dir_fd=fd).encode("utf-8", errors="surrogateescape"))
        elif kind == "directory":
            cfd = _open_child_dir(fd, name)
            try:
                _fingerprint_dir_fd(cfd, digest, rel)
            finally:
                os.close(cfd)
        digest.update(b"\n")


def scoped_estimate_bytes(scope: ResourceScope, path: Path) -> int:
    with guarded_parent(scope, path, access_mode=AccessMode.READ_ONLY) as guarded:
        try:
            st = os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        kind = _entry_kind(st)
        if kind == "file": return max(0, st.st_size)
        if kind == "symlink": return len(os.readlink(guarded.name, dir_fd=guarded.parent_fd).encode("utf-8", errors="replace"))
        if kind != "directory": return 0
        dfd = _open_child_dir(guarded.parent_fd, guarded.name)
        try:
            return _estimate_dir_fd(dfd)
        finally:
            os.close(dfd)


def _estimate_dir_fd(fd: int) -> int:
    total = 0
    for name in os.listdir(fd):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        kind = _entry_kind(st)
        if kind == "file": total += max(0, st.st_size)
        elif kind == "symlink": total += len(os.readlink(name, dir_fd=fd).encode("utf-8", errors="replace"))
        elif kind == "directory":
            cfd = _open_child_dir(fd, name)
            try: total += _estimate_dir_fd(cfd)
            finally: os.close(cfd)
    return total


def scoped_snapshot(scope: ResourceScope, path: Path, backup: Path) -> Dict[str, Any]:
    """Snapshot one scoped path without following any path-component symlink."""
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup.parent, 0o700)
    except OSError:
        pass
    with guarded_parent(scope, path, access_mode=AccessMode.READ_ONLY) as guarded:
        try:
            st = os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"kind": "absent", "mode": None}
        kind = _entry_kind(st)
        mode = stat.S_IMODE(st.st_mode)
        if kind == "file":
            src_fd = os.open(guarded.name, _file_read_flags(), dir_fd=guarded.parent_fd)
            try:
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    while True:
                        chunk = os.read(src_fd, 1024 * 1024)
                        if not chunk: break
                        view = memoryview(chunk)
                        while view:
                            n = os.write(fd, view); view = view[n:]
                    os.fsync(fd)
                finally: os.close(fd)
            finally: os.close(src_fd)
        elif kind == "directory":
            backup.mkdir(mode=0o700)
            src_fd = _open_child_dir(guarded.parent_fd, guarded.name)
            dst_fd = os.open(backup, _dir_flags())
            try:
                _copy_tree_from_fd(src_fd, dst_fd)
            finally:
                os.close(src_fd); os.close(dst_fd)
        elif kind == "symlink":
            target = os.readlink(guarded.name, dir_fd=guarded.parent_fd)
            os.symlink(target, backup)
        else:
            raise ScopedFilesystemError("unsupported_file_type", "Scoped snapshot refuses special filesystem objects.")
        return {"kind": kind, "mode": mode}


def _copy_backup_to_dest(source: Path, dest_parent_fd: int, dest_name: str, kind: str, mode: Optional[int]) -> None:
    if kind == "file":
        src_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _copy_file_fd(src_fd, dest_parent_fd, dest_name, mode or 0o600)
        finally: os.close(src_fd)
    elif kind == "directory":
        os.mkdir(dest_name, mode or 0o755, dir_fd=dest_parent_fd)
        src_fd = os.open(source, _dir_flags())
        dst_fd = _open_child_dir(dest_parent_fd, dest_name)
        try:
            _copy_backup_tree(src_fd, dst_fd)
        finally:
            os.close(src_fd); os.close(dst_fd)
    elif kind == "symlink":
        target = os.readlink(source)
        os.symlink(target, dest_name, dir_fd=dest_parent_fd)
    else:
        raise ScopedFilesystemError("unsupported_file_type", "Scoped restore refuses special filesystem objects.")


def _copy_backup_tree(src_fd: int, dst_fd: int) -> None:
    for name in sorted(os.listdir(src_fd)):
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        kind = _entry_kind(st)
        if kind == "file":
            sfd = os.open(name, _file_read_flags(), dir_fd=src_fd)
            try: _copy_file_fd(sfd, dst_fd, name, st.st_mode)
            finally: os.close(sfd)
        elif kind == "directory":
            os.mkdir(name, stat.S_IMODE(st.st_mode) or 0o755, dir_fd=dst_fd)
            sfd = _open_child_dir(src_fd, name); dfd = _open_child_dir(dst_fd, name)
            try: _copy_backup_tree(sfd, dfd)
            finally: os.close(sfd); os.close(dfd)
        elif kind == "symlink":
            os.symlink(os.readlink(name, dir_fd=src_fd), name, dir_fd=dst_fd)
        else:
            raise ScopedFilesystemError("unsupported_file_type", "Scoped restore refuses special filesystem objects.")


def scoped_restore(scope: ResourceScope, path: Path, before_kind: str, backup: Optional[Path], mode: Optional[int]) -> None:
    with guarded_parent(scope, path, access_mode=AccessMode.WORKSPACE_WRITE, create_parents=True) as guarded:
        try:
            os.stat(guarded.name, dir_fd=guarded.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _remove_entry(guarded.parent_fd, guarded.name, recursive=True)
        if before_kind == "absent":
            return
        if backup is None or (not backup.exists() and not backup.is_symlink()):
            raise ScopedFilesystemError("snapshot_missing", "Scoped transaction snapshot is missing.")
        _copy_backup_to_dest(backup, guarded.parent_fd, guarded.name, before_kind, mode)
        try: os.fsync(guarded.parent_fd)
        except OSError: pass


def scoped_search_text(
    scope: ResourceScope,
    root: Path,
    pattern: str,
    *,
    include_extensions: Optional[list[str]] = None,
    case_sensitive: bool = False,
    max_chars: int = 50_000,
) -> tuple[str, int, bool]:
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ScopedFilesystemError("invalid_search_pattern", f"Invalid search pattern: {exc}") from exc
    extensions = None
    if include_extensions:
        extensions = {"." + ext.lstrip(".").lower() for ext in include_extensions}
    with guarded_directory(scope, root) as (fd, absolute):
        lines: list[str] = []
        chars = 0
        matches = 0
        truncated = False

        def walk(dir_fd: int, base: Path) -> None:
            nonlocal chars, matches, truncated
            if truncated: return
            for name in sorted(os.listdir(dir_fd)):
                if truncated: return
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                kind = _entry_kind(st)
                current = base / name
                if kind == "directory":
                    cfd = _open_child_dir(dir_fd, name)
                    try: walk(cfd, current)
                    finally: os.close(cfd)
                elif kind == "file":
                    if extensions is not None and current.suffix.lower() not in extensions:
                        continue
                    ffd = os.open(name, _file_read_flags(), dir_fd=dir_fd)
                    try:
                        data = bytearray()
                        while len(data) <= max_chars * 4:
                            chunk = os.read(ffd, 1024 * 1024)
                            if not chunk: break
                            data.extend(chunk)
                            if len(data) > 8 * 1024 * 1024:
                                break
                    finally: os.close(ffd)
                    text = bytes(data).decode("utf-8", errors="replace")
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            rendered = f"{current}:{lineno}:{line}\n"
                            matches += 1
                            if chars + len(rendered) > max_chars:
                                truncated = True
                                return
                            lines.append(rendered); chars += len(rendered)
                # Symlinks and special entries are intentionally not traversed.
        walk(fd, absolute)
        return "".join(lines), matches, truncated
