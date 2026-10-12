"""Uninstall and repair for a Mac MCP install (#132).

Uninstall is preview-first and never deletes: everything it removes is moved into one
dated folder in the Trash, so it can be put back. The owner's data (the state folder with
settings, keys, tokens and memory, and a source checkout) is kept unless asked for
explicitly. Repair checks only the parts Mac MCP manages and says how each is fixed.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .venv_repair import inspect_venv

SUPERVISOR_JOB = "com.macmcp.supervisor.plist"
TUNNEL_JOB = "com.macmcp.cloudflared.plist"
# Names inside the Trash folder; the CLI launcher would otherwise collide with the runtime.
_TRASH_NAMES = {"cli": "mac-mcp-cli"}


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    path: Path
    data_bearing: bool
    remove: bool
    note: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists() or self.path.is_symlink()


def uninstall_plan(
    *,
    runtime: Path,
    state: Path,
    app: Path,
    cli: Path,
    launch_agents_dir: Path,
    source: Optional[Path] = None,
    include_state: bool = False,
    include_source: bool = False,
) -> List[Item]:
    items = [
        Item("app", "Menu bar app", app, False, True, "includes the Safari Visual Companion extension"),
        Item("runtime", "Server runtime", runtime, False, True,
             "includes the MCP API key in mcp_server/.env; AI apps connected with it stop working"),
        Item("cli", "mac-mcp command", cli, False, True),
        Item("supervisor_job", "Start-at-login and crash supervisor job", launch_agents_dir / SUPERVISOR_JOB, False, True),
        Item("tunnel_job", "Cloudflare Tunnel login job", launch_agents_dir / TUNNEL_JOB, False, True),
        Item("state", "Your settings, keys and tokens, memory, logs and run history", state, True, include_state),
    ]
    if source is not None and (source / ".git").is_dir() and source.resolve() != runtime.resolve():
        items.append(Item("source", "Source checkout", source, True, include_source,
                          "a Git checkout; uncommitted work in it goes to the Trash too"))
    return items


def format_plan(items: Iterable[Item], *, trash_folder: Optional[Path] = None) -> str:
    items = list(items)
    moving = [item for item in items if item.remove and item.exists]
    kept = [item for item in items if not item.remove and item.exists]
    lines = [
        "Moved to the Trash:" if trash_folder else "Would move to the Trash:",
    ]
    lines += [f"  - {item.label}: {item.path}" + (f" ({item.note})" if item.note else "") for item in moving] or ["  (nothing)"]
    lines.append("Kept:")
    lines += [f"  - {item.label}: {item.path}" for item in kept] or ["  (nothing)"]
    flags = {
        "state": ("--include-state", "your settings, keys and tokens"),
        "source": ("--include-source", "the source checkout"),
    }
    for item in kept:
        if item.key in flags:
            flag, what = flags[item.key]
            lines.append(f"  Add {flag} to move {what} too.")
    if trash_folder:
        lines.append(f"Everything moved is in: {trash_folder}")
    lines.append("Nothing is deleted: removed parts go to one folder in the Trash, so you can put them back.")
    lines.append("Not changed: AI app configurations that point at Mac MCP, the PATH line in ~/.zprofile, "
                 "saved logins in your Keychain, and the Chrome companion (remove it at chrome://extensions).")
    return "\n".join(lines)


def apply_uninstall(items: Iterable[Item], *, trash_root: Path, stamp: str) -> List[Dict[str, Any]]:
    folder = trash_root / f"Mac MCP uninstall {stamp}"
    results: List[Dict[str, Any]] = []
    for item in items:
        if not item.remove:
            results.append({"key": item.key, "path": str(item.path), "result": "kept"})
            continue
        if not item.exists:
            results.append({"key": item.key, "path": str(item.path), "result": "absent"})
            continue
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / _TRASH_NAMES.get(item.key, item.path.name)
        try:
            shutil.move(str(item.path), str(target))
            results.append({"key": item.key, "path": str(item.path), "result": "moved", "to": str(target)})
        except OSError as exc:
            results.append({"key": item.key, "path": str(item.path), "result": "failed", "error": type(exc).__name__})
    return results


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    fix: str = ""


def repair_checks(*, runtime: Path, cli: Path, app: Path, supervisor_job: Path, start_at_login: bool) -> List[Check]:
    venv = inspect_venv(runtime)
    venv_ok = venv.get("status") == "ok"
    launcher_ok = cli.is_file() and os.access(cli, os.X_OK) and str(runtime) in cli.read_text(encoding="utf-8", errors="replace")
    checks = [
        Check("venv", "Python environment", venv_ok,
              f"Python {venv.get('version')}" if venv_ok else f"status: {venv.get('status')}",
              "" if venv_ok else "rebuild the runtime's virtual environment"),
        Check("cli", "mac-mcp command", launcher_ok, str(cli),
              "" if launcher_ok else "recreate the launcher for this runtime"),
        Check("app", "Menu bar app", app.is_dir(), str(app),
              "" if app.is_dir() else "reinstall the menu bar app"),
    ]
    if start_at_login:
        job_ok = supervisor_job.is_file()
        checks.append(Check("supervisor", "Start-at-login job", job_ok, str(supervisor_job),
                            "" if job_ok else "reinstall the start-at-login job"))
    return checks
