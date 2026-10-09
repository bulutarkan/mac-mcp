"""Compile Mac MCP's small Swift helpers on first use and cache them by source hash."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path


def cache_dir(env_name: str, default: str) -> Path:
    path = Path(os.getenv(env_name, default)).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def compile_cached(source: Path, directory: Path, stem: str, *, timeout_s: int = 180) -> Path:
    """Return a binary for this exact source, compiling it once into ``directory``."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    executable = directory / f"{stem}-{digest[:16]}"
    if executable.exists():
        return executable
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not Path(swiftc).exists():
        raise RuntimeError("swiftc is unavailable")
    temporary = directory / f".build-{stem}-{os.getpid()}"
    subprocess.run(
        [swiftc, "-O", str(source), "-o", str(temporary)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout_s, check=True,
    )
    temporary.chmod(0o700)
    os.replace(temporary, executable)
    return executable
