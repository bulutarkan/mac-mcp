"""How this Mac MCP was installed: from source (install.sh / git updater) or from the .pkg.

Package installs have no Git checkout, so the Git-based updater cannot serve them; they
update by installing the newer package from GitHub Releases. Their update check compares the
installed signed checkpoint with the newest GitHub Release that carries a .pkg.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths

RELEASES_URL = "https://github.com/bulutarkan/mac-mcp/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/bulutarkan/mac-mcp/releases/latest"
RELEASES_API = "https://api.github.com/repos/bulutarkan/mac-mcp/releases?per_page=30"
_MESSAGE = (
    "This Mac MCP was installed from a package. To update, download the newer "
    ".pkg from GitHub Releases and open it; your settings and keys are kept."
)
_READY_MESSAGE = (
    "A new update is ready. Download the newer .pkg from GitHub Releases and open it; "
    "your settings and keys are kept."
)
_CURRENT_MESSAGE = "Mac MCP is up to date. New versions arrive as a .pkg on GitHub Releases."
_FAILED_MESSAGE = "Couldn't reach GitHub Releases to look for a newer .pkg. Try again later."
# stable-2.2.0-r2 (release id) or v2.2.0-r2 / v2.2.0 (release tag; a bare version is its r1).
_CHECKPOINT = re.compile(r"^(?:stable-|v)(\d+)\.(\d+)\.(\d+)(?:-r(\d+))?$")


def _marker() -> Path:
    state = paths.state_dir()
    return state / "install-kind"


def install_kind() -> str:
    try:
        value = _marker().read_text(encoding="utf-8").strip()
    except OSError:
        return "source"
    return "pkg" if value == "pkg" else "source"


def _checkpoint(name: str) -> Optional[Tuple[int, int, int, int]]:
    match = _CHECKPOINT.match(name.strip())
    if not match:
        return None
    major, minor, patch, revision = match.groups()
    return int(major), int(minor), int(patch), int(revision or 1)


def installed_release_id() -> str:
    """The signed checkpoint this runtime was installed from (the package unpacks it as is)."""
    manifest = Path(__file__).resolve().parents[1] / "release" / "stable-manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["release_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _fetch_latest_release() -> Dict[str, Any]:
    request = urllib.request.Request(LATEST_RELEASE_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "mac-mcp-update-check"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read(2_000_000))
    if not isinstance(data, dict):
        raise ValueError("unexpected GitHub Releases answer")
    return data


def _fetch_releases() -> list:
    request = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "mac-mcp-update-check"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read(4_000_000))
    if not isinstance(data, list):
        raise ValueError("unexpected GitHub Releases answer")
    return [item for item in data if isinstance(item, dict)]


def _has_package(release: Dict[str, Any]) -> bool:
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    return any(isinstance(asset, dict) and str(asset.get("name") or "").endswith(".pkg") for asset in assets)


def package_update_payload(check_only: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "install_kind": "pkg",
        "check_only": check_only,
        "update_available": False,
        "manual_update": True,
        "download_url": RELEASES_URL,
        "message": _MESSAGE,
    }
    installed = installed_release_id()
    payload["installed_release"] = installed or None
    if not _checkpoint(installed):
        return payload
    try:
        release = _fetch_latest_release()
    except (OSError, ValueError) as exc:
        # Not "ok": a check that could not run must not read as "no update" (roadmap #63).
        payload.update(ok=False, check_failed=True, message=_FAILED_MESSAGE, error=type(exc).__name__)
        return payload
    tag = str(release.get("tag_name") or "")
    latest = _checkpoint(tag)
    if not (latest and _has_package(release)):
        # The newest release has no .pkg (yet): an older one that does can still be newer than this
        # install (#103). Drafts and pre-releases never count.
        try:
            candidates = [item for item in _fetch_releases() if not item.get("draft") and not item.get("prerelease")
                          and _checkpoint(str(item.get("tag_name") or "")) and _has_package(item)]
        except (OSError, ValueError) as exc:
            payload.update(ok=False, check_failed=True, message=_FAILED_MESSAGE, error=type(exc).__name__)
            return payload
        if candidates:
            release = max(candidates, key=lambda item: _checkpoint(str(item["tag_name"])))
            tag = str(release.get("tag_name") or "")
            latest = _checkpoint(tag)
    if latest and _has_package(release) and latest > _checkpoint(installed):
        page = str(release.get("html_url") or "")
        payload.update(
            update_available=True,
            latest_release=tag,
            # Only ever a page of this repository's releases.
            download_url=page if page.startswith(RELEASES_URL + "/") else RELEASES_URL,
            message=_READY_MESSAGE,
        )
    else:
        payload["message"] = _CURRENT_MESSAGE
    return payload
