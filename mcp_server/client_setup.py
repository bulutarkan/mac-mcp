"""One-click setup: add Mac MCP to an AI app on this Mac.

Each app gets `mac-mcp stdio` as a command-based MCP server, so its config holds
no address and no key; the bridge adds the key from Mac MCP's own .env. Apps
with a CLI for this (Claude Code, Codex) are set up through it; for the others
Mac MCP edits the JSON config itself, keeping everything else in the file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

SERVER_NAME = "mac-mcp"
# The menu app starts the CLI with a minimal PATH; the AI apps' CLIs usually live here.
EXTRA_PATHS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin",
               "~/.opencode/bin")


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class App:
    id: str
    name: str
    next_step: str


APPS = (
    App("claude-code", "Claude Code", "Start a new Claude Code session, then ask it something about your Mac."),
    App("codex", "Codex", "Start a new Codex session, then ask it something about your Mac."),
    App("claude-desktop", "Claude Desktop", "Quit Claude and open it again, then ask it something about your Mac."),
    App("opencode", "OpenCode", "Start OpenCode again, then ask it something about your Mac."),
)
APP_IDS = tuple(app.id for app in APPS)

Run = Callable[..., subprocess.CompletedProcess]


def search_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    parts += [os.path.expanduser(item) for item in EXTRA_PATHS]
    return os.pathsep.join(dict.fromkeys(part for part in parts if part))


def bridge_command() -> list[str]:
    """What an AI app runs to reach Mac MCP: the installed CLI's `stdio` command."""
    configured = os.environ.get("MAC_MCP_CLI", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [Path.home() / ".local" / "bin" / "mac-mcp", Path(sys.executable).parent / "mac-mcp"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate), "stdio"]
    raise SetupError("the mac-mcp command is not installed; run the Mac MCP installer again")


def _home() -> Path:
    return Path.home()


def _which(name: str) -> str | None:
    return shutil.which(name, path=search_path())


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex") / "config.toml"


def _desktop_config() -> Path:
    return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _desktop_app() -> Path | None:
    for base in (Path("/Applications"), _home() / "Applications"):
        if (base / "Claude.app").exists():
            return base / "Claude.app"
    return None


def _opencode_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "opencode"


def _opencode_config() -> Path:
    folder = _opencode_dir()
    for name in ("opencode.jsonc", "opencode.json", "config.json"):
        if (folder / name).exists():
            return folder / name
    return folder / "opencode.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _added(app: str) -> bool:
    if app == "claude-code":
        servers = (_read_json(_home() / ".claude.json") or {}).get("mcpServers")
    elif app == "codex":
        try:
            text = _codex_config().read_text(encoding="utf-8")
        except OSError:
            return False
        if tomllib is None:
            return re.search(rf'^\[mcp_servers\.(?:{SERVER_NAME}|"{SERVER_NAME}")\]', text, re.M) is not None
        try:
            servers = tomllib.loads(text).get("mcp_servers")
        except tomllib.TOMLDecodeError:
            servers = None
    elif app == "claude-desktop":
        servers = (_read_json(_desktop_config()) or {}).get("mcpServers")
    else:
        servers = (_read_json(_opencode_config()) or {}).get("mcp")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _installed(app: str) -> bool:
    if app == "claude-code":
        return _which("claude") is not None
    if app == "codex":
        return _which("codex") is not None
    if app == "claude-desktop":
        return _desktop_app() is not None
    return _which("opencode") is not None or _opencode_dir().exists()


def apps_status() -> list[dict[str, Any]]:
    return [{"id": app.id, "name": app.name, "installed": _installed(app.id), "added": _added(app.id)}
            for app in APPS]


def _run_cli(run: Run, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=search_path())
    return run(args, capture_output=True, text=True, timeout=60, env=env, cwd=str(_home()), check=False)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write that keeps the file's permissions and a copy of what was there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        shutil.copy2(path, path.with_name(path.name + ".before-mac-mcp"))
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _edit_json(path: Path, label: str, update: Callable[[dict[str, Any]], None]) -> None:
    data: dict[str, Any] = {}
    if path.exists():
        loaded = _read_json(path)
        if loaded is None:
            # Comments or broken JSON: rewriting it would lose them, so leave it to the owner.
            raise SetupError(f"{label}'s settings file ({path}) is not plain JSON, so Mac MCP leaves it as it is. "
                             f"Add Mac MCP by hand with the manual setup.")
        data = loaded
    update(data)
    _write_json(path, data)


def connect(app: str, *, run: Run = subprocess.run) -> dict[str, Any]:
    """Add Mac MCP to the app, replacing an earlier Mac MCP entry. Returns what to do next."""
    meta = next((item for item in APPS if item.id == app), None)
    if meta is None:
        raise SetupError(f"unknown app: {app}")
    command = bridge_command()
    if app in ("claude-code", "codex"):
        binary = _which("claude" if app == "claude-code" else "codex")
        if binary is None:
            raise SetupError(f"{meta.name} is not installed on this Mac.")
        if app == "claude-code":
            remove = [binary, "mcp", "remove", SERVER_NAME, "--scope", "user"]
            add = [binary, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *command]
        else:
            remove = [binary, "mcp", "remove", SERVER_NAME]
            add = [binary, "mcp", "add", SERVER_NAME, "--", *command]
        if _added(app):
            _run_cli(run, remove)
        result = _run_cli(run, add)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise SetupError(f"{meta.name} did not accept Mac MCP: {detail[-1] if detail else 'no details'}")
    elif app == "claude-desktop":
        if _desktop_app() is None:
            raise SetupError("Claude Desktop is not installed on this Mac.")

        def add_desktop(data: dict[str, Any]) -> None:
            servers = data.get("mcpServers")
            data["mcpServers"] = servers if isinstance(servers, dict) else {}
            data["mcpServers"][SERVER_NAME] = {"command": command[0], "args": command[1:]}

        _edit_json(_desktop_config(), meta.name, add_desktop)
    else:
        def add_opencode(data: dict[str, Any]) -> None:
            data.setdefault("$schema", "https://opencode.ai/config.json")
            servers = data.get("mcp")
            data["mcp"] = servers if isinstance(servers, dict) else {}
            data["mcp"][SERVER_NAME] = {"type": "local", "command": command, "enabled": True}

        _edit_json(_opencode_config(), meta.name, add_opencode)
    if not _added(app):
        raise SetupError(f"{meta.name} still does not list Mac MCP; use the manual setup.")
    return {"ok": True, "client": app, "name": meta.name, "next": meta.next_step}
