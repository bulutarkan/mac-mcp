from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class BinaryResolution:
    path: str | None
    source: str

    @property
    def available(self) -> bool:
        return bool(self.path)


def _executable(candidate: str | None) -> str | None:
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    except OSError:
        return None
    return None


def resolve_binary(
    name: str,
    *,
    configured: str | None = None,
    env_var: str | None = None,
    fallbacks: Iterable[str] = (),
) -> BinaryResolution:
    configured_path = _executable(configured)
    if configured_path:
        return BinaryResolution(configured_path, "argument")

    if env_var:
        env_path = _executable(os.getenv(env_var))
        if env_path:
            return BinaryResolution(env_path, f"env:{env_var}")

    path_candidate = _executable(shutil.which(name))
    if path_candidate:
        return BinaryResolution(path_candidate, "path")

    for fallback in fallbacks:
        fallback_path = _executable(fallback)
        if fallback_path:
            return BinaryResolution(fallback_path, "fallback")

    return BinaryResolution(None, "missing")


def resolve_ngrok_binary(configured: str | None = None) -> BinaryResolution:
    return resolve_binary(
        "ngrok",
        configured=configured,
        env_var="NGROK_BIN",
        fallbacks=("/opt/homebrew/bin/ngrok", "/usr/local/bin/ngrok"),
    )


def resolve_cloudflared_binary(configured: str | None = None) -> BinaryResolution:
    return resolve_binary(
        "cloudflared",
        configured=configured,
        env_var="CLOUDFLARED_BIN",
        fallbacks=("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"),
    )


@lru_cache(maxsize=16)
def ngrok_http_endpoint_flag(binary: str) -> str:
    """Return the supported ngrok HTTP endpoint flag without hard-coding a version."""
    try:
        proc = subprocess.run(
            [binary, "http", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
        )
        help_text = proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        help_text = ""

    if "--url" in help_text:
        return "--url"
    if "--domain" in help_text:
        return "--domain"

    # Older v3 builds used --domain. Keeping this fallback preserves existing
    # supported installs if help probing is unavailable.
    return "--domain"
