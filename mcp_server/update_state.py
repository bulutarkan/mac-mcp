from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

LEGACY_COMMIT_FILE = ".mac-mcp-deployed-commit"
LEGACY_STATE_FILE = ".mac-mcp-update.json"


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


def write_deployed_commit(commit: str) -> None:
    deployed_commit_path().write_text(commit + "\n", encoding="utf-8")


def read_update_state() -> dict[str, Any] | None:
    path = update_state_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_update_state(payload: dict[str, Any]) -> None:
    path = update_state_path()
    root = path.parent
    root.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=str(root), text=True)
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
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
    except Exception:
        pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            tmp.unlink(missing_ok=True)


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
