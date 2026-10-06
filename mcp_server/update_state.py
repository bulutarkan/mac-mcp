from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

LEGACY_COMMIT_FILE = ".mac-mcp-deployed-commit"
LEGACY_STATE_FILE = ".mac-mcp-update.json"
INCOMPLETE_UPDATE_STATES = frozenset({
    "prepared",
    "repo_updating",
    "repo_updated",
    "runtime_syncing",
    "runtime_synced",
    "dependency_activating",
    "dependencies_activated",
    "restarting",
    "health_verified",
    "marker_committed",
    "dependency_commit_started",
    "dependency_committed",
    "rolling_back",
})


class UpdateStateError(RuntimeError):
    def __init__(self, code: str, message: str, *, path: Path, error_type: str | None = None):
        super().__init__(message)
        self.code = code
        self.path = path
        self.error_type = error_type


def update_root() -> Path:
    configured = os.getenv("MAC_MCP_UPDATE_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".mac-mcp" / "update"
    root.mkdir(parents=True, exist_ok=True)
    return root


def deployed_commit_path() -> Path:
    return update_root() / "deployed-commit"


def update_state_path() -> Path:
    return update_root() / "state.json"


def backups_root() -> Path:
    path = update_root() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_deployed_commit(runtime: Path) -> str | None:
    current = deployed_commit_path()
    if current.exists():
        value = current.read_text(encoding="utf-8").strip()
        if value:
            return value

    legacy = runtime / LEGACY_COMMIT_FILE
    if legacy.exists():
        value = legacy.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def _atomic_text_write(path: Path, text: str) -> None:
    root = path.parent
    root.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(root), text=True)
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def write_deployed_commit(commit: str) -> None:
    _atomic_text_write(deployed_commit_path(), commit + "\n")


def read_update_state(path: Path | None = None) -> dict[str, Any] | None:
    path = path or update_state_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateStateError(
            "update_state_unreadable",
            "Updater state could not be read reliably.",
            path=path,
            error_type=type(exc).__name__,
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpdateStateError(
            "update_state_corrupt",
            "Updater state is not valid JSON.",
            path=path,
            error_type=type(exc).__name__,
        ) from exc
    if not isinstance(payload, dict):
        raise UpdateStateError(
            "update_state_corrupt",
            "Updater state root must be a JSON object.",
            path=path,
        )

    transaction_version = payload.get("transaction_version")
    if transaction_version is not None:
        try:
            parsed_version = int(transaction_version)
        except (TypeError, ValueError) as exc:
            raise UpdateStateError(
                "update_state_corrupt",
                "Updater transaction version is invalid.",
                path=path,
                error_type=type(exc).__name__,
            ) from exc
        if parsed_version != 1:
            raise UpdateStateError(
                "update_state_corrupt",
                f"Updater transaction version {parsed_version} is unsupported.",
                path=path,
            )
        payload["transaction_version"] = parsed_version

    status = str(payload.get("status") or "")
    if status in INCOMPLETE_UPDATE_STATES and transaction_version is None:
        raise UpdateStateError(
            "update_state_corrupt",
            "Incomplete updater state is missing its transaction version.",
            path=path,
        )
    return payload


def write_update_state(payload: dict[str, Any]) -> None:
    try:
        _atomic_text_write(update_state_path(), json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception:
        pass


def migrate_completed_legacy_update(runtime: Path) -> bool:
    """Move v2.0-and-older updater artifacts out of a repo/runtime after a successful update."""
    legacy_state = runtime / LEGACY_STATE_FILE
    legacy_commit = runtime / LEGACY_COMMIT_FILE
    if not legacy_state.exists():
        return False
    try:
        payload = json.loads(legacy_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("status") != "completed":
        return False

    if legacy_commit.exists():
        commit = legacy_commit.read_text(encoding="utf-8").strip()
        if commit:
            write_deployed_commit(commit)

    legacy_backups = runtime / "backups" / "updates"
    if legacy_backups.exists() and legacy_backups.is_dir():
        destination = backups_root()
        for child in legacy_backups.iterdir():
            target = destination / child.name
            if target.exists():
                suffix = 1
                while (destination / f"{child.name}-{suffix}").exists():
                    suffix += 1
                target = destination / f"{child.name}-{suffix}"
            shutil.move(str(child), str(target))
        try:
            legacy_backups.rmdir()
            legacy_backups.parent.rmdir()
        except OSError:
            pass

    write_update_state(payload)
    legacy_commit.unlink(missing_ok=True)
    legacy_state.unlink(missing_ok=True)
    return True
