from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .update_state import migrate_completed_legacy_update


def _runtime_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _menu_source_candidates(runtime: Path) -> list[Path]:
    candidates = [runtime / "menu_app"]
    configured_repo = os.getenv("MAC_MCP_REPO", "").strip()
    if configured_repo:
        candidates.append(Path(configured_repo).expanduser() / "menu_app")
    candidates.append(Path.home() / "Projects" / "mac-mcp" / "menu_app")
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _installed_app() -> Path | None:
    for candidate in (Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")):
        if candidate.exists():
            return candidate
    return None


def ensure_menu_app_installed(runtime: Path | None = None) -> bool:
    """One-time v2 migration: install the native controller when upgrading from a pre-v2 checkout."""
    if os.getenv("MAC_MCP_SKIP_MENU_APP_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if _installed_app() is not None:
        return False
    runtime = runtime or _runtime_root()
    source = next((path for path in _menu_source_candidates(runtime) if (path / "install_app.sh").exists()), None)
    if source is None:
        return False
    try:
        completed = subprocess.run(
            [str(source / "install_app.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or _installed_app() is None:
            return False
        subprocess.run(
            ["/usr/bin/open", "-g", str(_installed_app())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _legacy_migration_worker(runtime: Path) -> None:
    # A pre-v2 updater keeps running old Python code while it merges the new checkout.
    # Wait until that old updater writes its final "completed" state, then move its
    # repo-local artifacts into ~/.mac-mcp/update so the checkout becomes clean again.
    for _ in range(90):
        try:
            if migrate_completed_legacy_update(runtime):
                return
        except Exception:
            pass
        time.sleep(1)


def bootstrap_menu_app_and_legacy_state(runtime: Path | None = None) -> None:
    runtime = runtime or _runtime_root()
    ensure_menu_app_installed(runtime)
    thread = threading.Thread(target=_legacy_migration_worker, args=(runtime,), daemon=True, name="mac-mcp-update-migration")
    thread.start()
