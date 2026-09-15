from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from fastapi import HTTPException, status

from .policy import current_policy_context
from .policy_scope import AccessMode, path_is_within

_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_DEFAULT_STATE_ROOT = Path.home() / ".mac-mcp" / "state" / "shell-sandboxes"

# These are executable/runtime roots, not user-data roots. They are read-only
# inside the scoped shell sandbox so normal developer tooling can start without
# exposing the rest of the user's home directory.
_SYSTEM_READ_ROOTS = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library/Developer",
    "/Library/Apple",
    "/opt/homebrew",
    "/usr/local",
    "/private/etc",
    "/dev",
)

_PROTECTED_ENV_KEYS = {
    "HOME",
    "PATH",
    "SHELL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "GIT_CONFIG_GLOBAL",
}


@dataclass(frozen=True)
class ShellExecutionPlan:
    cwd: Path
    env: dict[str, str]
    sandboxed: bool
    workspace_roots: tuple[str, ...]
    profile: Optional[str] = None
    sandbox_exec: Optional[str] = None

    def argv(self, command: str) -> list[str]:
        shell = ["/bin/zsh", "-c" if self.sandboxed else "-lc", command]
        if not self.sandboxed:
            return shell
        if not self.profile or not self.sandbox_exec:
            raise RuntimeError("Scoped shell sandbox plan is incomplete.")
        return [self.sandbox_exec, "-p", self.profile, *shell]


def _canonical(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _state_root() -> Path:
    raw = os.getenv("MAC_MCP_SCOPED_SHELL_STATE_DIR", "").strip()
    return _canonical(raw) if raw else _DEFAULT_STATE_ROOT.resolve(strict=False)


def _safe_identity() -> str:
    context = current_policy_context()
    raw = context.agent_id or context.actor or "scoped"
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:20]
    return f"ctx-{digest}"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _sandbox_scratch() -> Path:
    root = _state_root()
    _ensure_private_dir(root)
    scratch = root / _safe_identity()
    _ensure_private_dir(scratch)
    for name in ("home", "tmp", "cache", "config", "data", "state"):
        _ensure_private_dir(scratch / name)
    return scratch.resolve(strict=False)


def _sb_string(value: str | os.PathLike[str]) -> str:
    text = str(value)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Scoped shell path contains unsupported control characters.")
    return json.dumps(text, ensure_ascii=False)


def _profile_for(roots: tuple[str, ...], scratch: Path) -> str:
    read_roots = [*_SYSTEM_READ_ROOTS, *roots, str(scratch)]
    read_filters = " ".join(f"(subpath {_sb_string(root)})" for root in read_roots)
    write_filters = " ".join(
        f"(subpath {_sb_string(root)})" for root in (*roots, str(scratch))
    )
    ancestor_filters = " ".join(
        f"(path-ancestors {_sb_string(root)})" for root in read_roots
    )
    # P0 here is filesystem confinement. Network policy remains a separate scope
    # dimension, so preserve existing developer-network behavior for now.
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(import \"system.sb\")\n"
        "(allow process*)\n"
        "(allow signal (target self))\n"
        "(allow network*)\n"
        f"(allow file-read-metadata file-test-existence {ancestor_filters})\n"
        f"(allow file-read* file-test-existence process-exec {read_filters})\n"
        f"(allow file-write* file-read* file-test-existence {write_filters})\n"
    )


def _filtered_path(roots: tuple[str, ...], cwd: Path) -> str:
    candidates: list[str] = [
        str(cwd / "node_modules" / ".bin"),
        str(cwd / ".venv" / "bin"),
        str(cwd / "bin"),
    ]
    candidates.extend(os.environ.get("PATH", "").split(os.pathsep))
    allowed_runtime_roots = tuple(_canonical(path) for path in _SYSTEM_READ_ROOTS)
    allowed_workspace_roots = tuple(_canonical(path) for path in roots)
    kept: list[str] = []
    for raw in candidates:
        raw = str(raw or "").strip()
        if not raw:
            continue
        candidate = _canonical(raw)
        if any(path_is_within(candidate, root) for root in (*allowed_runtime_roots, *allowed_workspace_roots)):
            text = str(candidate)
            if text not in kept:
                kept.append(text)
    # Keep a deterministic minimal fallback even if the parent PATH is unusual.
    for fallback in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if fallback not in kept:
            kept.append(fallback)
    return os.pathsep.join(kept)


def _scoped_env(roots: tuple[str, ...], cwd: Path, scratch: Path, extra_env: Optional[Mapping[str, str]]) -> dict[str, str]:
    if extra_env:
        protected = sorted(
            key for key in extra_env
            if key in _PROTECTED_ENV_KEYS or key.startswith("DYLD_") or key.startswith("LD_")
        )
        if protected:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Scoped shell cannot override protected environment variable(s): " + ", ".join(protected),
            )
    home = scratch / "home"
    temp = scratch / "tmp"
    env = {
        "HOME": str(home),
        "USER": os.getenv("USER", Path.home().name),
        "LOGNAME": os.getenv("LOGNAME", os.getenv("USER", Path.home().name)),
        "SHELL": "/bin/zsh",
        "PATH": _filtered_path(roots, cwd),
        "LANG": os.getenv("LANG", "en_US.UTF-8") or "en_US.UTF-8",
        "LC_ALL": os.getenv("LC_ALL", "en_US.UTF-8") or "en_US.UTF-8",
        "TMPDIR": str(temp) + os.sep,
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_STATE_HOME": str(scratch / "state"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "CI": "1",
        "NO_COLOR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
    }
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def _normal_env(extra_env: Optional[Mapping[str, str]]) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(Path.home()),
        "USER": os.getenv("USER", Path.home().name),
        "LOGNAME": os.getenv("LOGNAME", os.getenv("USER", Path.home().name)),
        "PATH": f"{os.environ.get('PATH', '')}:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    })
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    return env


def shell_execution_plan(
    default_cwd: str | os.PathLike[str],
    *,
    requested_cwd: Optional[str] = None,
    extra_env: Optional[Mapping[str, str]] = None,
) -> ShellExecutionPlan:
    """Build an execution plan that fail-closes scoped raw shell on macOS.

    Global/unrestricted calls retain the historical behavior. Any non-full
    ResourceScope with path roots gets an OS Seatbelt sandbox: user-data reads
    and writes are limited to those roots plus an owner-only Mac MCP scratch
    directory. The command still has normal developer network access; network
    host scoping is handled separately.
    """

    context = current_policy_context()
    scope = context.scope
    roots = tuple(scope.path_roots or ()) if scope is not None else ()
    restricted = bool(scope is not None and roots and scope.access_mode != AccessMode.FULL)

    if not restricted:
        cwd = _canonical(requested_cwd) if requested_cwd else _canonical(default_cwd)
        if not cwd.exists() or not cwd.is_dir():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {cwd}")
        return ShellExecutionPlan(
            cwd=cwd,
            env=_normal_env(extra_env),
            sandboxed=False,
            workspace_roots=roots,
        )

    if platform.system() != "Darwin" or not _SANDBOX_EXEC.is_file():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Scoped raw shell is denied because the macOS sandbox runtime is unavailable.",
        )

    canonical_roots = tuple(str(_canonical(root)) for root in roots)
    if requested_cwd:
        cwd = _canonical(requested_cwd)
        if not any(path_is_within(cwd, root) for root in canonical_roots):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Scoped shell cwd is outside the allowed workspace roots.")
    else:
        configured = _canonical(default_cwd)
        cwd = configured if any(path_is_within(configured, root) for root in canonical_roots) else _canonical(canonical_roots[0])
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {cwd}")

    scratch = _sandbox_scratch()
    profile = _profile_for(canonical_roots, scratch)
    return ShellExecutionPlan(
        cwd=cwd,
        env=_scoped_env(canonical_roots, cwd, scratch, extra_env),
        sandboxed=True,
        workspace_roots=canonical_roots,
        profile=profile,
        sandbox_exec=str(_SANDBOX_EXEC),
    )
