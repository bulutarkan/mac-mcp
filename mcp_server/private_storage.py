"""Owner-only directories for what Mac MCP stores about your work.

Agent prompts and output, job logs, telemetry, memory and lessons describe what
agents did on this Mac. Each store's directory is kept at 0700 so other accounts
on the Mac cannot list or read it, whatever the umask or the modes of the files
inside. The server's umask is deliberately left alone: delegated agents inherit
it and the files they create in your projects must keep their usual modes.
At-rest encryption is not part of this (FileVault covers the disk).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# Directories under the state root (~/.mac-mcp) that hold content.
STATE_CONTENT_DIRS = ("dashboard", "memory", "role-learning", "recipes", "state", "cache", "runtime-backups", "backups")


def make_private(path: Path, *, directory: bool = True) -> bool:
    """chmod one existing directory to 0700 (or file to 0600); never raises."""
    try:
        if path.is_symlink() or not path.exists():
            return False
        mode = PRIVATE_DIR_MODE if directory else PRIVATE_FILE_MODE
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
            return True
    except OSError:
        pass
    return False


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    make_private(path)
    return path


def secure_content_stores(extra_dirs: Iterable[Path] = (), *, state_dir: Optional[Path] = None) -> Dict[str, List[str]]:
    """Tighten the store directories that exist; returns what changed."""
    state = state_dir or Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    targets: List[Path] = [state] + [state / name for name in STATE_CONTENT_DIRS] + [Path(p) for p in extra_dirs]
    changed = [str(path) for path in targets if make_private(path)]
    return {"changed": changed}
