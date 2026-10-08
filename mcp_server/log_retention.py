"""Size bounds and safe reading for Mac MCP's persistent log files.

Server and tunnel logs are written through inherited file descriptors (uvicorn
stdout, launchd StandardOutPath), so they are rotated with copy-and-truncate:
the writer keeps its O_APPEND descriptor and simply continues at offset 0.
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .data_guard import redact_sensitive_text

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 3
DEFAULT_UPDATE_LOGS_KEPT = 20
_TAIL_READ_LIMIT = 512 * 1024


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(os.getenv(name, "") or default)))
    except ValueError:
        return default


def log_limits() -> Dict[str, int]:
    return {
        "max_bytes": _int_env("MAC_MCP_LOG_MAX_BYTES", DEFAULT_MAX_BYTES, 256 * 1024, 1024 * 1024 * 1024),
        "backups": _int_env("MAC_MCP_LOG_BACKUPS", DEFAULT_BACKUPS, 1, 20),
        "update_logs_kept": _int_env("MAC_MCP_UPDATE_LOGS_KEPT", DEFAULT_UPDATE_LOGS_KEPT, 1, 500),
    }


def state_dir() -> Path:
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()


def managed_logs(base_dir: Optional[Path] = None) -> Dict[str, Path]:
    root = state_dir()
    logs = {
        "server": root / "mac-mcp.log",
        "cloudflared": root / "cloudflared.log",
        "ngrok": root / "ngrok.log",
    }
    if base_dir is not None:
        logs["audit"] = Path(base_dir) / "audit.log"
    return logs


def _backup(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def rotate_copy_truncate(path: Path, *, max_bytes: Optional[int] = None, backups: Optional[int] = None) -> bool:
    """Rotate a log that another process may hold open; True when it rotated."""
    limits = log_limits()
    max_bytes = limits["max_bytes"] if max_bytes is None else max_bytes
    backups = limits["backups"] if backups is None else backups
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    oldest = _backup(path, backups)
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(backups - 1, 0, -1):
        source = _backup(path, index)
        if source.exists():
            source.replace(_backup(path, index + 1))
    first = _backup(path, 1)
    fd = os.open(first, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as target, path.open("rb") as source:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    os.truncate(path, 0)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def prune_update_logs(logs_dir: Path, keep: Optional[int] = None) -> int:
    """Keep the newest updater logs; returns how many were removed."""
    keep = log_limits()["update_logs_kept"] if keep is None else keep
    try:
        logs = sorted(
            (item for item in logs_dir.glob("upd_*.log") if item.is_file()),
            key=lambda item: item.stat().st_mtime, reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for item in logs[keep:]:
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def rotate_managed_logs(base_dir: Optional[Path] = None) -> List[str]:
    rotated = []
    for name, path in managed_logs(base_dir).items():
        if name == "audit":
            continue  # the audit logger rotates itself
        if rotate_copy_truncate(path):
            rotated.append(name)
    update_logs = update_logs_dir()
    if update_logs.is_dir():
        prune_update_logs(update_logs)
    return rotated


def update_logs_dir() -> Path:
    from .update_state import update_root

    return update_root() / "logs"


_rotation_started = False
_rotation_lock = threading.Lock()


def start_log_rotation(interval_s: float = 600.0, base_dir: Optional[Path] = None) -> None:
    """Rotate oversized logs now and then every interval while the server runs."""
    global _rotation_started
    with _rotation_lock:
        if _rotation_started:
            return
        _rotation_started = True
    stop = threading.Event()

    def loop() -> None:
        while True:
            try:
                rotate_managed_logs(base_dir)
            except Exception:
                pass
            if stop.wait(interval_s):
                return

    threading.Thread(target=loop, name="mac-mcp-log-rotation", daemon=True).start()


def tail_log(path: Path, lines: int = 80) -> str:
    """Last lines of a log, read from the end only and with secrets redacted."""
    lines = max(1, min(int(lines), 2000))
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    start = max(0, size - _TAIL_READ_LIMIT)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if start:
        text = text.split("\n", 1)[-1]
    return redact_sensitive_text("\n".join(text.splitlines()[-lines:]))
