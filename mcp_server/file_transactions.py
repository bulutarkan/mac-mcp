from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

SCHEMA_VERSION = 1
DEFAULT_RETENTION_S = 7 * 24 * 60 * 60
DEFAULT_MAX_TRANSACTIONS = 64
DEFAULT_MAX_STORE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
ACTIVE_GRACE_S = 5 * 60

_LOCK = threading.RLock()


class FileTransactionError(RuntimeError):
    code = "file_transaction_error"


class TransactionNotFound(FileTransactionError):
    code = "transaction_not_found"


class TransactionConflict(FileTransactionError):
    code = "transaction_conflict"


class TransactionExpired(FileTransactionError):
    code = "transaction_expired"


class TransactionIrreversible(FileTransactionError):
    code = "transaction_irreversible"


class TransactionRestoreFailed(FileTransactionError):
    code = "transaction_restore_failed"


class TransactionPrepareFailed(FileTransactionError):
    code = "transaction_prepare_failed"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def _now() -> float:
    return time.time()


def journal_root() -> Path:
    configured = os.getenv("MAC_MCP_FILE_JOURNAL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    state = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return state / "transactions"


def _retention_s() -> int:
    return _env_int("MAC_MCP_FILE_JOURNAL_RETENTION_S", DEFAULT_RETENTION_S, 60)


def _max_transactions() -> int:
    return _env_int("MAC_MCP_FILE_JOURNAL_MAX_TRANSACTIONS", DEFAULT_MAX_TRANSACTIONS, 1)


def _max_store_bytes() -> int:
    return _env_int("MAC_MCP_FILE_JOURNAL_MAX_BYTES", DEFAULT_MAX_STORE_BYTES, 1024 * 1024)


def _max_snapshot_bytes() -> int:
    return _env_int("MAC_MCP_FILE_JOURNAL_MAX_SNAPSHOT_BYTES", DEFAULT_MAX_SNAPSHOT_BYTES, 1024)


def _ensure_root() -> Path:
    root = journal_root()
    if root.is_symlink():
        raise TransactionPrepareFailed("transaction root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


@contextmanager
def _journal_lock() -> Iterator[None]:
    root = _ensure_root()
    with _LOCK:
        lock_path = root / ".lock"
        with lock_path.open("a", encoding="utf-8") as handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _validate_transaction_id(transaction_id: str) -> str:
    value = str(transaction_id or "").strip()
    if not value.startswith("ftx_") or len(value) != 36:
        raise TransactionNotFound("invalid transaction id")
    try:
        int(value[4:], 16)
    except ValueError as exc:
        raise TransactionNotFound("invalid transaction id") from exc
    return value


def _txn_dir(transaction_id: str) -> Path:
    return journal_root() / _validate_transaction_id(transaction_id)


def _manifest_path(transaction_id: str) -> Path:
    return _txn_dir(transaction_id) / "manifest.json"


def _read_manifest(transaction_id: str) -> Dict[str, Any]:
    path = _manifest_path(transaction_id)
    if not path.is_file():
        raise TransactionNotFound("transaction was not found or has expired")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionRestoreFailed("transaction manifest is unreadable") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise TransactionRestoreFailed("transaction manifest schema mismatch")
    if str(payload.get("transaction_id") or "") != transaction_id:
        raise TransactionRestoreFailed("transaction manifest identity mismatch")
    return payload


def _write_manifest(payload: Mapping[str, Any]) -> None:
    transaction_id = _validate_transaction_id(str(payload.get("transaction_id") or ""))
    _atomic_json(_manifest_path(transaction_id), payload)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def _estimate_bytes(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_symlink():
        return len(os.readlink(path).encode("utf-8", errors="replace"))
    if path.is_file():
        return max(0, path.stat().st_size)
    if path.is_dir():
        total = 0
        for root, dirs, files in os.walk(path, followlinks=False):
            root_path = Path(root)
            for name in files:
                item = root_path / name
                try:
                    total += max(0, item.lstat().st_size)
                except OSError:
                    continue
            for name in dirs:
                item = root_path / name
                if item.is_symlink():
                    try:
                        total += len(os.readlink(item).encode("utf-8", errors="replace"))
                    except OSError:
                        pass
        return total
    return 0


def _fingerprint(path: Path) -> str:
    kind = _path_kind(path)
    digest = hashlib.sha256()
    digest.update((kind + "\0").encode())
    if kind == "absent":
        return digest.hexdigest()
    if kind == "symlink":
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        return digest.hexdigest()
    if kind == "file":
        digest.update(str(path.stat().st_size).encode())
        digest.update(b"\0")
        digest.update(_hash_file(path).encode())
        return digest.hexdigest()
    if kind == "directory":
        for item in sorted(path.rglob("*"), key=lambda p: str(p.relative_to(path))):
            rel = str(item.relative_to(path))
            child_kind = _path_kind(item)
            digest.update(rel.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(child_kind.encode())
            digest.update(b"\0")
            if child_kind == "file":
                digest.update(str(item.stat().st_size).encode())
                digest.update(b"\0")
                digest.update(_hash_file(item).encode())
            elif child_kind == "symlink":
                digest.update(os.readlink(item).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\n")
        return digest.hexdigest()
    stat = path.stat()
    digest.update(f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def _open_secure_tar(path: Path) -> tuple[tarfile.TarFile, Any]:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    fileobj = os.fdopen(fd, "wb")
    try:
        archive = tarfile.open(fileobj=fileobj, mode="w", dereference=False)
    except Exception:
        fileobj.close()
        path.unlink(missing_ok=True)
        raise
    return archive, fileobj


def _snapshot(path: Path, txn_dir: Path, index: int) -> Dict[str, Any]:
    kind = _path_kind(path)
    item: Dict[str, Any] = {
        "path": str(path),
        "before_kind": kind,
        "before_fingerprint": _fingerprint(path),
        "estimated_bytes": _estimate_bytes(path),
        "backup": None,
        "post_fingerprint": None,
    }
    if kind == "absent":
        return item
    if kind == "other":
        raise TransactionPrepareFailed("unsupported filesystem object cannot be snapshotted")
    snapshots = txn_dir / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(snapshots, 0o700)
    except OSError:
        pass
    backup = snapshots / f"{index:04d}.tar"
    archive, fileobj = _open_secure_tar(backup)
    try:
        archive.add(path, arcname="payload", recursive=True)
        archive.close()
        fileobj.flush()
        os.fsync(fileobj.fileno())
    finally:
        try:
            archive.close()
        except Exception:
            pass
        fileobj.close()
    os.chmod(backup, 0o600)
    item["backup"] = str(backup.relative_to(txn_dir))
    return item


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    members = archive.getmembers()
    for member in members:
        name = member.name
        parts = Path(name).parts
        if not parts or parts[0] != "payload" or name.startswith("/") or ".." in parts:
            raise TransactionRestoreFailed("unsafe backup archive member")
    try:
        archive.extractall(destination, filter="data")
    except TypeError:
        archive.extractall(destination)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink(missing_ok=True)


def _restore_snapshot(txn_dir: Path, snapshot: Mapping[str, Any]) -> None:
    target = Path(str(snapshot.get("path") or ""))
    before_kind = str(snapshot.get("before_kind") or "absent")
    _remove_path(target)
    if before_kind == "absent":
        return
    backup_rel = str(snapshot.get("backup") or "")
    if not backup_rel:
        raise TransactionRestoreFailed("snapshot backup is unavailable")
    backup = txn_dir / backup_rel
    if not backup.is_file():
        raise TransactionRestoreFailed("snapshot backup is missing")
    restore_dir = txn_dir / (".restore_" + uuid.uuid4().hex)
    restore_dir.mkdir(mode=0o700)
    try:
        with tarfile.open(backup, mode="r") as archive:
            _safe_extract(archive, restore_dir)
        payload = restore_dir / "payload"
        if not payload.exists() and not payload.is_symlink():
            raise TransactionRestoreFailed("snapshot payload is missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(payload), str(target))
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _transaction_dirs(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.is_dir() and p.name.startswith("ftx_")]


def _prune_locked(*, exclude: Optional[str] = None) -> None:
    root = _ensure_root()
    now = _now()
    entries: list[tuple[Path, Dict[str, Any], int]] = []
    for directory in _transaction_dirs(root):
        if directory.name == exclude:
            continue
        try:
            manifest = _read_manifest(directory.name)
        except FileTransactionError:
            # Corrupt journals are owner-only but unusable; retain briefly by mtime.
            try:
                if now - directory.stat().st_mtime > _retention_s():
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                pass
            continue
        state = str(manifest.get("state") or "")
        created = float(manifest.get("created_at") or 0.0)
        expires = float(manifest.get("expires_at") or (created + _retention_s()))
        if expires <= now:
            shutil.rmtree(directory, ignore_errors=True)
            continue
        entries.append((directory, manifest, _dir_bytes(directory)))

    def removable(item: tuple[Path, Dict[str, Any], int]) -> bool:
        _, manifest, _ = item
        state = str(manifest.get("state") or "")
        created = float(manifest.get("created_at") or 0.0)
        return state in {"committed", "undone", "rolled_back", "rollback_failed", "aborted"} or now - created > ACTIVE_GRACE_S

    entries.sort(key=lambda item: float(item[1].get("created_at") or 0.0))
    excluded_count = 1 if exclude and (root / exclude).is_dir() else 0
    while len(entries) + excluded_count > _max_transactions():
        index = next((i for i, item in enumerate(entries) if removable(item)), None)
        if index is None:
            break
        directory, _, _ = entries.pop(index)
        shutil.rmtree(directory, ignore_errors=True)

    excluded_bytes = _dir_bytes(root / exclude) if exclude and (root / exclude).is_dir() else 0
    total = sum(size for _, _, size in entries) + excluded_bytes
    max_bytes = _max_store_bytes()
    while total > max_bytes:
        index = next((i for i, item in enumerate(entries) if removable(item)), None)
        if index is None:
            break
        directory, _, size = entries.pop(index)
        shutil.rmtree(directory, ignore_errors=True)
        total -= size


def prune_transactions() -> None:
    root = journal_root()
    if not root.is_dir() or root.is_symlink():
        return
    with _journal_lock():
        _prune_locked()


def prepare_transaction(
    operation: str,
    paths: Sequence[Path],
    *,
    require_undoable: bool = False,
    actor: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    clean_paths: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser().resolve(strict=False)
        key = str(path)
        if key not in seen:
            seen.add(key)
            clean_paths.append(path)
    if not clean_paths:
        raise TransactionPrepareFailed("transaction has no filesystem targets")

    transaction_id = "ftx_" + uuid.uuid4().hex
    root = _ensure_root()
    txn_dir = root / transaction_id
    with _journal_lock():
        _prune_locked()
        txn_dir.mkdir(mode=0o700)
        created = _now()
        manifest: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "operation": str(operation)[:80],
            "state": "preparing",
            "undoable": True,
            "irreversible_reason": None,
            "created_at": created,
            "expires_at": created + _retention_s(),
            "committed_at": None,
            "undone_at": None,
            "rolled_back_at": None,
            "creator": {
                "actor": str(actor or "")[:120] or None,
                "agent_id": str(agent_id or "")[:120] or None,
            },
            "snapshots": [],
        }
        _write_manifest(manifest)
        estimated = sum(_estimate_bytes(path) for path in clean_paths)
        if estimated > _max_snapshot_bytes():
            manifest["undoable"] = False
            manifest["irreversible_reason"] = "snapshot_limit_exceeded"
            if require_undoable:
                manifest["state"] = "aborted"
                _write_manifest(manifest)
                raise TransactionIrreversible("atomic operation exceeds the reversible snapshot limit")
            manifest["snapshots"] = [
                {
                    "path": str(path),
                    "before_kind": _path_kind(path),
                    "before_fingerprint": None,
                    "estimated_bytes": _estimate_bytes(path),
                    "backup": None,
                    "post_fingerprint": None,
                }
                for path in clean_paths
            ]
            manifest["state"] = "prepared"
            _write_manifest(manifest)
            return dict(manifest)
        try:
            for index, path in enumerate(clean_paths):
                snapshot = _snapshot(path, txn_dir, index)
                manifest["snapshots"].append(snapshot)
                _write_manifest(manifest)
        except Exception as exc:
            manifest["state"] = "aborted"
            manifest["undoable"] = False
            manifest["irreversible_reason"] = "snapshot_prepare_failed"
            _write_manifest(manifest)
            raise TransactionPrepareFailed("could not prepare reversible filesystem snapshot") from exc
        manifest["state"] = "prepared"
        _write_manifest(manifest)
        return dict(manifest)


def transaction_paths(transaction_id: str) -> tuple[str, ...]:
    with _journal_lock():
        manifest = _read_manifest(transaction_id)
        return tuple(str(item.get("path") or "") for item in manifest.get("snapshots") or [] if item.get("path"))


def commit_transaction(transaction_id: str) -> Dict[str, Any]:
    with _journal_lock():
        manifest = _read_manifest(transaction_id)
        if manifest.get("state") != "prepared":
            raise TransactionConflict("transaction is not prepared")
        if manifest.get("undoable"):
            for item in manifest.get("snapshots") or []:
                item["post_fingerprint"] = _fingerprint(Path(str(item.get("path") or "")))
        manifest["state"] = "committed"
        manifest["committed_at"] = _now()
        _write_manifest(manifest)
        _prune_locked(exclude=transaction_id)
        return transaction_receipt(manifest)


def _rollback_locked(manifest: Dict[str, Any], *, terminal_state: str) -> Dict[str, Any]:
    transaction_id = str(manifest["transaction_id"])
    if not manifest.get("undoable"):
        manifest["state"] = "rollback_failed"
        manifest["irreversible_reason"] = manifest.get("irreversible_reason") or "snapshot_unavailable"
        _write_manifest(manifest)
        raise TransactionIrreversible("transaction has no reversible snapshot")
    txn_dir = _txn_dir(transaction_id)
    try:
        for snapshot in reversed(list(manifest.get("snapshots") or [])):
            _restore_snapshot(txn_dir, snapshot)
    except Exception as exc:
        manifest["state"] = "rollback_failed"
        manifest["rollback_error"] = exc.__class__.__name__
        _write_manifest(manifest)
        raise TransactionRestoreFailed("filesystem rollback could not be completed") from exc
    manifest["state"] = terminal_state
    if terminal_state == "undone":
        manifest["undone_at"] = _now()
    else:
        manifest["rolled_back_at"] = _now()
    _write_manifest(manifest)
    return transaction_receipt(manifest)


def rollback_transaction(transaction_id: str) -> Dict[str, Any]:
    with _journal_lock():
        manifest = _read_manifest(transaction_id)
        if manifest.get("state") not in {"prepared", "committed"}:
            raise TransactionConflict("transaction cannot be rolled back in its current state")
        return _rollback_locked(manifest, terminal_state="rolled_back")


def undo_transaction(transaction_id: str, *, force: bool = False) -> Dict[str, Any]:
    with _journal_lock():
        manifest = _read_manifest(transaction_id)
        state = str(manifest.get("state") or "")
        if state == "prepared":
            if not force:
                raise TransactionConflict(
                    "transaction stopped between prepare and commit; outcome is unknown, use force=true only to restore the recorded pre-state"
                )
            return _rollback_locked(manifest, terminal_state="undone")
        if state != "committed":
            raise TransactionConflict("only a committed transaction can be undone")
        if float(manifest.get("expires_at") or 0.0) <= _now():
            raise TransactionExpired("transaction undo window has expired")
        if not manifest.get("undoable"):
            raise TransactionIrreversible(str(manifest.get("irreversible_reason") or "transaction is irreversible"))
        if not force:
            conflicts: list[int] = []
            for index, item in enumerate(manifest.get("snapshots") or []):
                expected = str(item.get("post_fingerprint") or "")
                current = _fingerprint(Path(str(item.get("path") or "")))
                if not expected or current != expected:
                    conflicts.append(index)
            if conflicts:
                raise TransactionConflict(
                    "filesystem changed after this transaction; refusing to overwrite newer changes"
                )
        return _rollback_locked(manifest, terminal_state="undone")


def get_transaction(transaction_id: str) -> Dict[str, Any]:
    with _journal_lock():
        return _read_manifest(transaction_id)


def transaction_receipt(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "transaction_id": manifest.get("transaction_id"),
        "transaction_state": manifest.get("state"),
        "operation": manifest.get("operation"),
        "undoable": bool(manifest.get("undoable")),
        "undo_expires_at": manifest.get("expires_at") if manifest.get("undoable") else None,
        "irreversible_reason": manifest.get("irreversible_reason"),
        "path_count": len(manifest.get("snapshots") or []),
    }
