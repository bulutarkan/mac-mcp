"""Run the server under "Mac MCP Server" so macOS attributes its permissions to Mac MCP.

macOS records Accessibility, Screen Recording and Automation consent for the responsible
process of whatever asks. Started by launchd, the small "Mac MCP Server" app is its own
responsible process and spawns the server as its child, so prompts and System Settings say
"Mac MCP" instead of "Python" (or Terminal, when the server was started from a shell).
See docs/superpowers/specs/2026-10-12-server-permission-identity-design.md.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from .managed_process import listener_pids

SERVER_LABEL = "com.macmcp.server"
HELPER_BUNDLE = "Mac MCP Server.app"

# The job file sits in the state folder; only what the server needs to find its runtime goes
# in it. Secrets stay in mcp_server/.env, which the server reads itself.
_PASSED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH"}
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def helper_bundle(runtime: Path) -> Path:
    return runtime / ".helper" / HELPER_BUNDLE


def helper_executable(runtime: Path) -> Optional[Path]:
    bundle = helper_bundle(runtime)
    exe = bundle / "Contents" / "MacOS" / "mac-mcp-server"
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return None
    try:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(bundle)], capture_output=True, timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if verified else None


def ensure_helper(runtime: Path) -> Optional[Path]:
    """Build the helper from the runtime's sources when they changed; the current helper otherwise."""
    script = runtime / "menu_app" / "ServerHelper" / "build_helper.sh"
    if script.is_file():
        try:
            subprocess.run(["/bin/zsh", str(script), str(runtime / ".helper")],
                           capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            pass
    return helper_executable(runtime)


def job_environment(env: Mapping[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if any(marker in upper for marker in _SECRET_MARKERS):
            continue
        if name in _PASSED or upper.startswith("MAC_MCP_") or upper.startswith("MCP_"):
            out[name] = str(value)
    return out


def server_job_payload(*, helper: Path, command: List[str], env: Mapping[str, str], cwd: Path, log: Path) -> dict:
    return {
        "Label": SERVER_LABEL,
        "ProgramArguments": [str(helper), *[str(part) for part in command]],
        "EnvironmentVariables": dict(env),
        "WorkingDirectory": str(cwd),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        # Started on demand by `mac-mcp start`; restarts and login belong to the supervisor.
        "RunAtLoad": True,
        "KeepAlive": False,
        "Umask": 0o077,
    }


def _parent_pid(pid: int) -> Optional[int]:
    try:
        raw = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return int(raw) if raw else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def server_child_pid(
    helper_pid: int,
    port: int,
    *,
    listeners: Callable[[int], Iterable[int]] = listener_pids,
    parent_of: Callable[[int], Optional[int]] = _parent_pid,
) -> Optional[int]:
    """The server process: the helper's child listening on the port."""
    for pid in listeners(port):
        if parent_of(pid) == int(helper_pid):
            return pid
    return None
