"""How this Mac MCP was installed: from source (install.sh / git updater) or from the .pkg.

Package installs have no Git checkout, so the Git-based updater cannot serve them; they
update by installing the newer package from GitHub Releases.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from . import paths

RELEASES_URL = "https://github.com/bulutarkan/mac-mcp/releases"
_MESSAGE = (
    "This Mac MCP was installed from a package. To update, download the newer "
    ".pkg from GitHub Releases and open it; your settings and keys are kept."
)


def _marker() -> Path:
    state = paths.state_dir()
    return state / "install-kind"


def install_kind() -> str:
    try:
        value = _marker().read_text(encoding="utf-8").strip()
    except OSError:
        return "source"
    return "pkg" if value == "pkg" else "source"


def package_update_payload(check_only: bool = True) -> Dict[str, Any]:
    return {
        "ok": True,
        "install_kind": "pkg",
        "check_only": check_only,
        "update_available": False,
        "manual_update": True,
        "download_url": RELEASES_URL,
        "message": _MESSAGE,
    }
