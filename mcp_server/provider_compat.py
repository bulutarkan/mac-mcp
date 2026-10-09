"""Check that a provider CLI still accepts the options Mac MCP passes, before any work is admitted.

Version strings are not a reliable capability signal, so this probes each
subcommand's --help for the options and subcommands _build_provider_command
uses. Results are cached per resolved binary (path, size, mtime), so a CLI
upgrade is re-checked on the next spawn and an unchanged one costs nothing.

"incompatible" (an option is missing) blocks spawning with a clear remediation.
"unverified" (help could not be read) does not block: the run itself will
report what is wrong, and a slow or unusual CLI must not lock the owner out.
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# (help command after the binary, tokens that must appear in its output)
REQUIRED_OPTIONS: Dict[str, Sequence[Tuple[Sequence[str], Sequence[str]]]] = {
    "codex": (
        (("exec", "--help"), (
            "--json", "--color", "--skip-git-repo-check", "--output-last-message", "--config", "--model",
            "--sandbox", "--dangerously-bypass-approvals-and-sandbox",
        )),
        (("exec", "resume", "--help"), (
            "--json", "--skip-git-repo-check", "--output-last-message", "--config", "--model",
            "--dangerously-bypass-approvals-and-sandbox",
        )),
    ),
    "opencode": (
        (("run", "--help"), ("--format", "--auto", "--dir", "--pure", "--model", "--variant", "--session")),
    ),
    "chatgpt": (
        (("--help",), ("new", "resume", "--json-stream", "--timeout", "--model", "--effort")),
    ),
}

_lock = threading.Lock()
_cache: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}


def _identity(binary: str) -> Optional[Tuple[str, int, int]]:
    try:
        real = os.path.realpath(binary)
        info = os.stat(real)
    except OSError:
        return None
    return real, info.st_size, info.st_mtime_ns


def _help_text(binary: str, args: Sequence[str], env: Optional[Dict[str, str]]) -> Optional[str]:
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=20, env=env, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return text if text.strip() else None


def check(provider: str, binary: str, *, env: Optional[Dict[str, str]] = None,
          help_text: Callable[[str, Sequence[str], Optional[Dict[str, str]]], Optional[str]] = _help_text) -> Dict[str, Any]:
    """{"status": "compatible" | "incompatible" | "unverified", "missing": [...], ...} for one provider CLI."""
    provider = str(provider or "").lower()
    contract = REQUIRED_OPTIONS.get(provider)
    if not contract:
        return {"status": "unverified", "provider": provider, "missing": [], "reason": "no_contract"}
    identity = _identity(binary)
    if identity is None:
        return {"status": "unverified", "provider": provider, "missing": [], "reason": "binary_unreadable"}
    key = (provider, *identity)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return dict(cached)
    missing: List[str] = []
    unreadable: List[str] = []
    for args, tokens in contract:
        text = help_text(binary, args, env)
        command = " ".join(args[:-1] if args and args[-1] == "--help" else args) or "(top level)"
        if text is None:
            unreadable.append(command)
            continue
        missing += [f"{command}: {token}" for token in tokens if token not in text]
    if missing:
        result = {
            "status": "incompatible", "provider": provider, "missing": missing,
            "remediation": (
                f"This {provider} CLI does not accept options Mac MCP uses ({', '.join(missing[:6])}). "
                f"Update {provider} (or point Settings > Agents at a supported build), then try again."
            ),
        }
    elif unreadable:
        result = {"status": "unverified", "provider": provider, "missing": [], "reason": "help_unavailable",
                  "unreadable": unreadable}
    else:
        result = {"status": "compatible", "provider": provider, "missing": []}
    with _lock:
        _cache[key] = result
    return dict(result)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
