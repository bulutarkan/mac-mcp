from __future__ import annotations

import os
import shlex
import stat
import tempfile
from pathlib import Path


def runtime_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_cli_path() -> Path:
    configured = os.getenv("MAC_MCP_CLI_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "bin" / "mac-mcp"


def runtime_entrypoint(runtime: Path | None = None) -> Path:
    root = (runtime or runtime_root()).expanduser()
    return root / ".venv" / "bin" / "mac-mcp"


def launcher_text(runtime: Path) -> str:
    default_runtime = shlex.quote(str(runtime.expanduser()))
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"default_runtime={default_runtime}\n"
        'runtime="${MAC_MCP_RUNTIME_DIR:-$default_runtime}"\n'
        'entry="$runtime/.venv/bin/mac-mcp"\n'
        'if [ ! -x "$entry" ]; then\n'
        '  printf \'%s\\n\' "mac-mcp: runtime CLI not found or not executable: $entry" >&2\n'
        '  printf \'%s\\n\' "mac-mcp: set MAC_MCP_RUNTIME_DIR or reinstall Mac MCP." >&2\n'
        "  exit 127\n"
        "fi\n"
        'exec "$entry" "$@"\n'
    )


def ensure_cli_launcher(
    runtime: Path | None = None,
    cli_path: Path | None = None,
    *,
    strict: bool = False,
) -> Path | None:
    root = (runtime or runtime_root()).expanduser()
    entry = runtime_entrypoint(root)
    target = (cli_path or default_cli_path()).expanduser()

    if not entry.is_file() or not os.access(entry, os.X_OK):
        if strict:
            raise RuntimeError(f"runtime CLI entrypoint is missing or not executable: {entry}")
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        desired = launcher_text(root)
        current = None
        if target.is_file() and not target.is_symlink():
            try:
                current = target.read_text(encoding="utf-8")
            except OSError:
                current = None
        if current == desired and os.access(target, os.X_OK):
            return target

        fd, temp_name = tempfile.mkstemp(prefix=".mac-mcp-launcher.", dir=str(target.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(desired)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o755)
            os.replace(temp_name, target)
            os.chmod(target, 0o755)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return target
    except OSError:
        if strict:
            raise
        return None


def launcher_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    try:
        mode = path.stat().st_mode
    except OSError:
        return "missing"
    return "file" if stat.S_ISREG(mode) else "other"
