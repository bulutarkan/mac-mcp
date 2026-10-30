"""Mac MCP Chat: the owner's own conversations with a coding agent in a project folder.

Each chat runs one of the owner's own agent CLIs (Claude, Codex, OpenCode, GitHub Copilot) as an Agent Client
Protocol subprocess, signed in as the owner, with Mac MCP added as an MCP server on a scoped credential. The
agent says which models, thinking levels and modes it offers (ACP configOptions); the owner picks them in
the chat. The agent's permission prompts come to the owner in the chat window. Chats live in
<state>/chats/<chat_id>/ (chat.json + transcript.json); the agent keeps its own session, so a chat reopened
later continues where it stopped (session/load). A chat unused for Settings › Privacy's chat retention
(privacy.chat_retention_days, 90 days by default) is removed; one started in private mode is never written.

The transcript is a list of items (user, assistant, thought, tool, permission, plan, notice). Every change
gives the item a new `rev`; the app asks for items with rev > its cursor, so a streaming answer is read
by polling without missing a word.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import extra_harness_providers as harness
from . import paths, privacy_policy
from .acp_client import PROTOCOL_VERSION, AcpClosed, AcpConnection, AcpError
from .agent_timeline import _TOOL_WORDS, _cut, _humanize, _inputs, _readable_output
from .policy_scope import ResourceScope
from .runtime_settings import chat_retention_days, provider_enabled, provider_setting
from .scoped_auth import get_scoped_credential_store
from .version import __version__

CHAT_ID = re.compile(r"^chat_[0-9a-f]{12}$")
IDLE_STOP_S = 3 * 60  # an idle chat's agent process stops; the next message reopens the session
PERMISSION_WAIT_S = 30 * 60
MAX_PENDING_PERMISSIONS = 8  # questions open at once in one chat; more are declined without asking (#119)
CREDENTIAL_TTL_S = 24 * 3600
MAX_TEXT = 200_000
MAX_TITLE = 120
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # sent inline (base64) to agents that take images
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
STREAM_KEEPALIVE_S = 15.0
STREAM_COALESCE_S = 0.06
LIST_COALESCE_S = 0.5  # at most ~15 updates a second reach the app
MAX_TERMINAL = 64_000  # the end of a command's live output kept on its step
MAX_QUEUE = 20
EMPTY_CHAT_KEEP_S = 6 * 3600  # a chat nobody wrote in is gone at the next start after this
RETENTION_SWEEP_S = 3600.0  # unused chats past the retention go at start, then hourly (#114)

# Claude Code and Codex speak ACP through their official adapters; the others have it built in.
AGENTS: Dict[str, Dict[str, Any]] = {
    "claude": {
        "label": "Claude", "cli": "claude", "login": "claude",
        "command": ["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.94.0"],
    },
    "codex": {
        "label": "Codex", "cli": "codex", "login": "codex login",
        "command": ["npx", "-y", "@agentclientprotocol/codex-acp@2.2.2"],
        # The owner's ~/.codex config has its own `mac-mcp` (the adapter would keep that one, unscoped), and
        # Codex's ChatGPT apps include the owner's Mac MCP connector. As for delegated Codex agents, the chat's
        # Mac MCP goes in under a name of its own on the app-server's command line, with both of those off
        # (see _codex_wrapper): MCP servers given over ACP become a session table that overrides them.
        "mcp_name": "mac-mcp-chat",
        "mcp_on_command_line": True,
    },
    "opencode": {
        "label": "OpenCode", "cli": "opencode", "login": "opencode auth login",
        "command": ["{cli}", "acp"],
    },
    "kilo": {
        "label": "Kilo Code", "cli": "kilo", "login": "kilo auth login",
        "command": ["{cli}", "acp"],
    },
    "kimi": {
        "label": "Kimi Code", "cli": "kimi", "login": "kimi login",
        "command": ["{cli}", "acp"],
    },
    "goose": {
        "label": "Goose", "cli": "goose", "login": "goose configure",
        "command": ["{cli}", "acp"],
    },
    "cline": {
        "label": "Cline", "cli": "cline", "login": "cline auth",
        "command": ["{cli}", "--acp"],
    },
    "antigravity": {
        # The agy CLI speaks no ACP: Google ships Antigravity's ACP server apart (ACP registry `antigravity-acp`),
        # fetched on first use like the npx adapters, pinned and checked. It uses the owner's Antigravity sign-in.
        "label": "Antigravity", "cli": "agy", "login": "agy",
        "command": ["{download}"],
        "download": {
            "version": "1.4.3",
            "url": "https://dl.google.com/agy-extensions/releases/macos/agy-acp-server-1.4.3-darwin-arm64.zip",
            "sha256": "c57db0a9b8b712daa9c3c17cdb990a61842ea569f279e8bd79d7aa85a303b20b",
            "cmd": "agy_acp_server.par",
        },
        # Until a sign-in method is chosen it opens no session; the owner's Google sign-in is the one agy uses.
        "auth_method": "oauth-personal",
    },
    "copilot": {
        "label": "GitHub Copilot", "cli": "copilot", "login": "copilot login",
        # Copilot offers no model over ACP: the chat lists the models its own CLI documents and restarts it
        # with --model / --reasoning-effort when the owner picks one (the conversation reopens).
        "restart_options": True,
        # Copilot's own subagents and workflows are left out, as for delegated agents; Mac MCP is the only MCP.
        # Copilot sends its own status lines ("Info: Disabled tools: …") as whole message chunks.
        "status_line": re.compile(r"^(Info|Warning): "),
        "command": ["{cli}", "--acp", "--no-auto-update", "--disable-builtin-mcps", "--disable-client-telemetry",
                    "--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN",
                    "--excluded-tools", *harness.COPILOT_EXCLUDED_TOOLS],
    },
}
# Goose with no provider of its own starts on the owner's OpenRouter key (as delegated Goose does) and needs a
# model to start with: a free one, so the first message costs nothing. Its model picker then lists Goose's own.
GOOSE_START_MODEL = "poolside/laguna-s-2.1:free"
# The pickers the chat shows, in this order; other options go under "More".
SHOWN_CATEGORIES = ("model", "thought_level", "mode")

_TOOL_STATUS = {"pending": "running", "in_progress": "running", "completed": "done", "failed": "failed"}


def _last_used(meta: Dict[str, Any]) -> float:
    try:
        return float(meta.get("updated_at") or meta.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0.0


class ChatError(Exception):
    def __init__(self, message: str, code: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def chats_root() -> Path:
    return paths.state_dir() / "chats"


def _agent_options_path() -> Path:
    # The models, modes and switches each agent last offered: a new chat shows them before its agent starts.
    return chats_root() / "agent_options.json"


def _now() -> float:
    return round(time.time(), 3)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _search_path() -> str:
    home = Path.home()
    parts = [os.getenv("PATH", ""), str(home / ".npm-global" / "bin"), str(home / ".local" / "bin"),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(part for part in parts if part)


def _which(name: str) -> Optional[str]:
    override = os.getenv(f"MAC_MCP_{name.upper()}_BINARY", "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    return shutil.which(name, path=_search_path())


def _binary(agent: str) -> Optional[str]:
    """The agent's own CLI, which holds the owner's sign-in."""
    return _which(AGENTS[agent]["cli"]) if agent in AGENTS else None


def _command(agent: str) -> Optional[List[str]]:
    cli = _binary(agent)
    if not cli:
        return None
    command = list(AGENTS[agent]["command"])
    if command[0] == "{download}":
        command[0] = str(_download_dir(agent) / AGENTS[agent]["download"]["cmd"])  # fetched at the first start
    elif command[0] == "{cli}":
        command[0] = cli
    else:
        tool = _which(command[0])
        if not tool:
            return None
        command[0] = tool
    return command


def _download_dir(agent: str) -> Path:
    spec = AGENTS[agent]["download"]
    return chats_root() / "agents" / f"{agent}-{spec['version']}"


def _ensure_download(agent: str) -> None:
    """An agent's ACP server that ships as an archive: downloaded once, its SHA-256 checked, unpacked in place."""
    import hashlib
    import urllib.request
    import zipfile
    spec = AGENTS[agent]["download"]
    folder = _download_dir(agent)
    if (folder / spec["cmd"]).is_file():
        return
    folder.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{agent}-", dir=str(folder.parent)))
    try:
        archive = staging / "download.zip"
        digest = hashlib.sha256()
        with urllib.request.urlopen(spec["url"], timeout=120) as response, archive.open("wb") as out:  # noqa: S310 - pinned https URL
            for chunk in iter(lambda: response.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != spec["sha256"]:
            raise ChatError(f"{AGENTS[agent]['label']}'s download did not match its checksum; nothing was installed.",
                            "agent_missing")
        unpacked = staging / "files"
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                target = (unpacked / entry.filename).resolve()
                if not str(target).startswith(str(unpacked.resolve()) + os.sep):
                    raise ChatError(f"{AGENTS[agent]['label']}'s download has an unexpected file layout.", "agent_missing")
                bundle.extract(entry, unpacked)
                if not entry.is_dir() and (entry.external_attr >> 16) & 0o111:
                    target.chmod(0o755)
        if not (unpacked / spec["cmd"]).is_file():
            raise ChatError(f"{AGENTS[agent]['label']}'s download has no {spec['cmd']}.", "agent_missing")
        os.replace(unpacked, folder)
    except OSError as exc:
        raise ChatError(f"Could not download {AGENTS[agent]['label']}'s ACP server: {exc}", "agent_missing") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _codex_wrapper() -> Optional[Path]:
    """The Codex the adapter starts (CODEX_PATH): the owner's codex, with their own `mac-mcp` entry and ChatGPT
    apps (which hold their Mac MCP connector) turned off. Session config cannot do it: the adapter's own
    mcp_servers table for the chat is merged over it, so these go on the app-server's command line."""
    codex = _binary("codex")
    if not codex:
        return None
    from .tools_agents import _owner_codex_has_mac_mcp
    name = AGENTS["codex"]["mcp_name"]
    overrides = ["features.apps=false",
                 f"mcp_servers.{name}.url={json.dumps(_mcp_endpoint())}",
                 f'mcp_servers.{name}.bearer_token_env_var="MAC_MCP_CHAT_TOKEN"']
    if _owner_codex_has_mac_mcp():
        overrides.append("mcp_servers.mac-mcp.enabled=false")
    flags = " ".join(f"-c {shlex.quote(override)}" for override in overrides)
    script = f'#!/bin/sh\n# Mac MCP Chat: the owner\'s codex for the chat adapter (see chat_sessions._codex_wrapper).\n' \
             f'if [ "$1" = "app-server" ]; then shift; exec {shlex.quote(codex)} app-server {flags} "$@"; fi\n' \
             f'exec {shlex.quote(codex)} "$@"\n'
    path = chats_root() / "codex-for-chat"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != script:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    except OSError:
        return None
    return path


def _copilot_byok(model: str) -> bool:
    vendor, _, name = str(model or "").partition("/")
    return bool(os.getenv("COPILOT_PROVIDER_BASE_URL")) or (vendor.lower() in harness.COPILOT_BYOK and bool(name))


def _keychain_lookup(name: str) -> Optional[str]:
    from .tools_agents import _harness_keychain_value  # the server's one place for the OpenRouter key
    return _harness_keychain_value(name)


def _mcp_endpoint() -> str:
    from .tools_agents import _agent_mcp_endpoint
    return _agent_mcp_endpoint()


def _agent_env(agent: str, model: str, token: str = "") -> Dict[str, str]:
    """What the agent process sees: the owner's home (their sign-in and settings), no server secrets."""
    home = Path.home()
    user = os.getenv("USER") or home.name
    env = {
        "HOME": str(home), "USER": user, "LOGNAME": user, "LANG": "en_US.UTF-8",
        "PATH": _search_path(), "TMPDIR": os.getenv("TMPDIR", "/tmp"), "NO_COLOR": "1", "TERM": "dumb",
    }
    if agent == "codex":
        wrapper = _codex_wrapper()
        if wrapper:
            env["CODEX_PATH"] = str(wrapper)
            # The adapter writes its own `features` table into each session, merged with this one: without it
            # the session table turns ChatGPT apps back on over the command line's features.apps=false.
            env["CODEX_CONFIG"] = json.dumps({"features": {"apps": False}})
            if token:
                env["MAC_MCP_CHAT_TOKEN"] = token  # read by Codex for the chat's Mac MCP (bearer_token_env_var)
    if agent == "opencode":
        # The owner's shell gives OpenCode their OpenRouter key; the server keeps it in the Keychain.
        key = os.getenv("OPENROUTER_API_KEY") or _keychain_lookup("OPENROUTER_API_KEY")
        if key:
            env["OPENROUTER_API_KEY"] = key
    if agent == "goose" and not os.getenv("GOOSE_PROVIDER") and not (harness.owner_home("goose") / "config.yaml").is_file():
        key = os.getenv("OPENROUTER_API_KEY") or _keychain_lookup("OPENROUTER_API_KEY")
        if key:
            env.update({"OPENROUTER_API_KEY": key, "GOOSE_PROVIDER": "openrouter",
                        "GOOSE_MODEL": os.getenv("GOOSE_MODEL") or GOOSE_START_MODEL})
    if agent == "copilot" and _copilot_byok(model):
        # Copilot on the owner's own provider key (Settings › Agents model openrouter/…), as for delegated runs.
        private_home = chats_root() / "copilot-home"
        private_home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            private_home.chmod(0o700)
        env.update(harness.copilot_env(private_home, _keychain_lookup, model))
    return env


def _not_ready(agent: str) -> str:
    """Why a chat cannot start with this (enabled) agent, or "" when it can."""
    if _command(agent):
        return ""
    label = AGENTS[agent]["label"]
    return f"{label} is not installed." if not _binary(agent) else "Node.js (npx) is needed to connect to it."


def available_agents() -> List[Dict[str, Any]]:
    """Agents the owner can chat with: enabled in Settings and installed. Disabled ones are not offered."""
    out = []
    for agent, spec in AGENTS.items():
        if not provider_enabled(agent):
            continue
        reason = _not_ready(agent)
        out.append({"id": agent, "label": spec["label"], "ready": not reason, "reason": reason})
    return out


def _config_options(raw: Any) -> List[Dict[str, Any]]:
    """ACP configOptions as the app shows them; grouped choices are flattened with their group's name."""
    out = []
    for option in raw if isinstance(raw, list) else []:
        if not isinstance(option, dict) or option.get("type") != "select" or not option.get("id"):
            continue
        choices = []
        for entry in option.get("options") or []:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("options") if isinstance(entry.get("options"), list) else None
            for choice in nested if nested is not None else [entry]:
                if isinstance(choice, dict) and choice.get("value") is not None:
                    choices.append({"value": str(choice["value"]), "name": _cut(str(choice.get("name") or choice["value"]), 120),
                                    "description": _cut(str(choice.get("description") or ""), 240),
                                    "group": str(entry.get("name") or "") if nested is not None else ""})
        out.append({"id": str(option["id"]), "name": str(option.get("name") or option["id"]),
                    "category": str(option.get("category") or ""), "current": str(option.get("currentValue") or ""),
                    "options": choices})
    return out


def _attachments(paths: List[Any]) -> List[Dict[str, Any]]:
    """Files the owner added to a message: real files, readable, within the size limits."""
    if len(paths) > MAX_ATTACHMENTS:
        raise ChatError(f"Add at most {MAX_ATTACHMENTS} files to one message.", "too_many_files")
    out = []
    for raw in paths:
        path = Path(str(raw or "")).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ChatError(f"{path.name or raw} is not a file that can be added.", "bad_attachment")
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ChatError(f"{path.name} is larger than 25 MB.", "attachment_too_large")
        image = path.suffix.lower() in IMAGE_TYPES
        out.append({"name": path.name, "path": str(path.resolve()), "size": size,
                    "kind": "image" if image else "file",
                    "mime": IMAGE_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or ""})
    return out


def _prompt_blocks(text: str, files: List[Dict[str, Any]], caps: Dict[str, Any]) -> List[Dict[str, Any]]:
    """ACP prompt content: the words, images inline where the agent takes them, other files as links it can open."""
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for file in files:
        uri = Path(file["path"]).as_uri()
        if file["kind"] == "image" and caps.get("image") and file["size"] <= MAX_IMAGE_BYTES:
            try:
                data = base64.b64encode(Path(file["path"]).read_bytes()).decode("ascii")
            except OSError as exc:
                raise ChatError(f"Could not read {file['name']}: {exc.strerror or exc}", "bad_attachment") from exc
            blocks.append({"type": "image", "mimeType": file["mime"], "data": data, "uri": uri})
        else:
            link = {"type": "resource_link", "uri": uri, "name": file["name"], "size": file["size"]}
            if file["mime"]:
                link["mimeType"] = file["mime"]
            blocks.append(link)
    if not text:
        blocks.insert(0, {"type": "text", "text": "Here are the attached files."})
    return blocks


_CLI_HELP: Dict[str, Any] = {}


def _cli_help(args: List[str]) -> str:
    """A CLI's own help text (what it documents it can do), read once per binary version."""
    binary = _binary("copilot")
    if not binary:
        return ""
    try:
        stamp = os.stat(binary).st_mtime
    except OSError:
        return ""
    key = (binary, stamp, tuple(args))
    if key not in _CLI_HELP:
        import subprocess
        try:
            done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                  env={"HOME": str(Path.home()), "PATH": _search_path(), "NO_COLOR": "1"})
            _CLI_HELP[key] = done.stdout or ""
        except (OSError, subprocess.SubprocessError):
            _CLI_HELP[key] = ""
    return _CLI_HELP[key]


def copilot_models() -> List[str]:
    """The models the installed Copilot CLI documents for its `model` setting (`copilot help config`)."""
    text = _cli_help(["help", "config"])
    match = re.search(r"`model`:[^\n]*\n((?:\s+- \"[^\"]+\"\n?)+)", text)
    return re.findall(r'- "([^"]+)"', match.group(1)) if match else []


def copilot_efforts() -> List[str]:
    text = _cli_help(["--help"])
    match = re.search(r"--reasoning-effort.*?possible values:\s*([^\]]+)\]", text, re.S)
    return [value.strip() for value in match.group(1).split(",") if value.strip()] if match else []


def _pretty_model(name: str) -> str:
    words = name.replace("-", " ").split()
    return " ".join(word.upper() if word in ("gpt", "mai") else word.capitalize() for word in words)


def _local_options(chat: "Chat", offered: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copilot's model and thinking, which it takes only when it starts (--model, --reasoning-effort)."""
    choices = chat.meta.get("choices") or {}
    categories = {option["category"] for option in offered}
    out = []
    models = copilot_models()
    if "model" not in categories and models and not _copilot_byok(chat.meta.get("model") or ""):
        out.append({"id": "model", "name": "Model", "category": "model", "local": True,
                    "current": choices.get("model", "auto"),
                    "options": [{"value": "auto", "name": "Auto", "description": "Copilot picks", "group": ""}]
                    + [{"value": name, "name": _pretty_model(name), "description": "", "group": ""} for name in models]})
    efforts = copilot_efforts()
    if "thought_level" not in categories and efforts:
        out.append({"id": "reasoning_effort", "name": "Thinking", "category": "thought_level", "local": True,
                    "current": choices.get("reasoning_effort", "default"),
                    "options": [{"value": "default", "name": "Default", "description": "", "group": ""}]
                    + [{"value": level, "name": level.capitalize(), "description": "", "group": ""} for level in efforts]})
    return out


def _restart_args(chat: "Chat") -> List[str]:
    choices = chat.meta.get("choices") or {}
    args: List[str] = []
    if choices.get("model") not in (None, "", "auto") and choices["model"] in copilot_models():
        args += ["--model", choices["model"]]
    if choices.get("reasoning_effort") not in (None, "", "default") and choices["reasoning_effort"] in copilot_efforts():
        args += ["--reasoning-effort", choices["reasoning_effort"]]
    return args


def _needs_sign_in(exc: AcpError) -> bool:
    """Not signed in, or no model provider set up yet (Goose says so as an internal error)."""
    text = f"{exc} {exc.data or ''}".lower()
    return exc.code == -32000 or any(word in text for word in ("auth", "login", "goose_provider", "configure"))


MAX_DIFF_LINES = 400


def _diffs(content: Any) -> List[Dict[str, Any]]:
    """ACP diff blocks (an edit's old and new text) as unified diffs with their added and removed line counts."""
    import difflib
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "diff":
            continue
        path = str(block.get("path") or "")
        old = str(block.get("oldText") or "").splitlines()
        new = str(block.get("newText") or "").splitlines()
        lines = list(difflib.unified_diff(old, new, lineterm="", n=2))[2:]  # without the ---/+++ header
        added = sum(1 for line in lines if line.startswith("+"))
        removed = sum(1 for line in lines if line.startswith("-"))
        out.append({"path": path, "added": added, "removed": removed, "created": block.get("oldText") in (None, ""),
                    # Not _cut: a diff line's leading space (an unchanged line) matters.
                    "lines": [line if len(line) <= 400 else line[:399] + "…" for line in lines[:MAX_DIFF_LINES]],
                    "truncated": len(lines) > MAX_DIFF_LINES})
    return out[:20]


def _last_log_line(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 4096))
            lines = [line.strip() for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


# How agents name an MCP tool: mcp__mac-mcp__read_file (Claude), mcp.mac-mcp.read_file (Codex),
# mac-mcp_read_file (OpenCode), mac-mcp-read_file (Copilot).
_MCP_TOOL = re.compile(r"^(?:mcp(?:__|\.)(?P<server>[\w.-]+?)(?:__|\.)|(?P<plain>mac[-_]mcp)[-_])(?P<tool>[a-z][a-z0-9_]*)$")
_MCP_STARTUP = re.compile(r"^mcp(?:__|\.)[\w.-]+(?:__|\.)startup$")
_AGENT_TOOLS = {"ToolSearch": "Looked for the right tool", "TodoWrite": "Updated its plan", "WebSearch": "Searched the web",
                "WebFetch": "Read a web page", "Task": "Started a helper"}


def _tool_source(agent_title: str) -> tuple[str, str]:
    """("mac-mcp", "read_file") for a Mac MCP tool, (server, tool) for another MCP server, else ("", "")."""
    match = _MCP_TOOL.match(agent_title.strip())
    if not match:
        return "", ""
    server = match.group("plain") or match.group("server") or ""
    mine = server.replace("_", "-") in ("mac-mcp", "mac-mcp-chat")  # Codex's chat connection has its own name
    return ("mac-mcp" if mine else server), match.group("tool")


def _friendly_title(item: Dict[str, Any], fallback: str) -> str:
    """File steps by the file's name ("Read notes.md"); a Mac MCP step by what it was for; else the agent's words."""
    agent_title = str(item.get("agent_title") or fallback or "")
    files = [name for name in item.get("files") or [] if name]
    name = Path(files[0]).name if files else ""
    kind = item.get("kind")
    if name and kind == "read":
        return f"Read {name}"
    if name and kind == "edit":
        return f"Created {name}" if agent_title.lower().startswith(("creat", "writ")) else f"Edited {name}"
    if name and kind == "delete":
        return f"Deleted {name}"
    if name and kind == "move":
        return f"Moved {name}"
    if item.get("intent"):
        return _cut(str(item["intent"]), 200)  # Mac MCP tools say what they are for (their description)
    match = _MCP_TOOL.match(agent_title.strip())
    if match:
        tool = match.group("tool")
        return _TOOL_WORDS.get(tool) or _humanize(tool)
    if agent_title in _AGENT_TOOLS:
        return _AGENT_TOOLS[agent_title]
    return _cut(agent_title, 200) or str(item.get("title") or "Working")


def _text_of(content: Any) -> str:
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        if content.get("type") == "content":
            return _text_of(content.get("content"))
        if content.get("type") == "diff":
            return ""  # shown as the step's diff instead
        return ""
    if isinstance(content, list):
        return "\n".join(part for part in (_text_of(item) for item in content) if part)
    return ""


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\\\)|\x1b[()][0-9A-Za-z]")


def _terminal_text(data: Any) -> str:
    """A command's output as it reads: colours and other escape codes out, a line redrawn with \\r kept as its last."""
    text = _ANSI.sub("", str(data or "")).replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


CARRY_OVER_CHARS = 6000  # the conversation part of a carry-over summary; the newest words are kept
CARRY_OVER_FILES = 25


def _carry_over_summary(chat: "Chat") -> str:
    """A short account of the conversation before the message now going out, for an agent whose earlier session
    could not be reopened: what was said (the newest kept when long) and the files its steps touched, each checked
    on disk now."""
    items = chat.items
    last_user = max((index for index, item in enumerate(items) if item.get("type") == "user"), default=len(items))
    lines: List[str] = []
    files: Dict[str, str] = {}
    for item in items[:last_user]:
        kind = item.get("type")
        if kind == "user":
            attached = [entry for entry in item.get("attachments") or [] if isinstance(entry, dict)]
            text = _cut(" ".join(str(item.get("text") or "").split()), 600)
            named = ", ".join(str(entry.get("name") or "") for entry in attached)
            lines.append("Owner: " + (text or "(files only)") + (f" [attached: {named}]" if named else ""))
            for entry in attached:
                files[str(entry.get("path") or "")] = "attached"
        elif kind == "assistant" and str(item.get("text") or "").strip():
            lines.append("Agent: " + _cut(" ".join(str(item["text"]).split()), 1200))
        elif kind == "tool":
            for diff in item.get("diffs") or []:
                files[str(diff.get("path") or "")] = f"edited, +{diff.get('added', 0)} -{diff.get('removed', 0)}"
            what = {"edit": "edited", "delete": "deleted", "move": "moved", "read": "read"}.get(str(item.get("kind")))
            for name in item.get("files") or []:
                if what and str(name) not in files:
                    files[str(name)] = what
    if not lines:
        return ""
    kept: List[str] = []
    used = 0
    for line in reversed(lines):
        if kept and used + len(line) > CARRY_OVER_CHARS:
            kept.append("… (earlier messages left out)")
            break
        kept.append(line)
        used += len(line)
    out = ["[Mac MCP Chat] This conversation began in an earlier session that could not be reopened, so this is a "
           "new session. Here is a summary of it; carry on from it. The owner's new message follows the summary.",
           "", "Conversation so far (oldest first):", *reversed(kept)]
    project = Path(str(chat.meta.get("project") or "/"))
    checked = []
    for name, what in [entry for entry in reversed(list(files.items())) if entry[0]][:CARRY_OVER_FILES]:
        path = Path(name) if Path(name).is_absolute() else project / name
        checked.append(f"- {path} ({what}; {'exists' if path.exists() else 'no longer there'})")
    if checked:
        out += ["", "Files from the earlier session, newest first (checked just now):", *checked]
    out += ["", "[End of summary]"]
    return "\n".join(out)


class Chat:
    """One conversation: its record, its transcript and, while in use, its agent process."""

    def __init__(self, meta: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
        self.meta = meta
        self.items = items
        self.seq = max([int(item.get("rev") or 0) for item in items] + [int(meta.get("rev") or 0)])
        self.conn: Optional[AcpConnection] = None
        self.session_id: Optional[str] = None
        self.turn: Optional[asyncio.Task] = None
        self.permissions: Dict[str, asyncio.Future] = {}
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.open_text: Optional[Dict[str, Any]] = None  # the assistant/thought item chunks go to
        self.replaying = False
        self.start_lock = asyncio.Lock()
        self.idle: Optional[asyncio.TimerHandle] = None
        self.save_pending = False
        self.warming: Optional[asyncio.Task] = None
        self.startup_calls: set[str] = set()  # the Codex adapter's MCP start reports, not steps
        self.waiters: List[asyncio.Future] = []
        # What the agent takes in a prompt besides text (image, …); None until it said, at its start.
        self.prompt_caps: Optional[Dict[str, Any]] = None
        self.stop_requested = False  # Stop came while the agent was still starting: the turn task is cancelled
        self.on_change: Optional[Callable[[], None]] = None

    @property
    def dir(self) -> Path:
        return chats_root() / self.meta["id"]

    def bump(self, item: Optional[Dict[str, Any]] = None, touch: bool = True) -> None:
        self.seq += 1
        if item is not None:
            item["rev"] = self.seq
        else:
            self.meta["rev"] = self.seq
        # Only a message and its answer use the chat. Pinning, archiving, renaming, opening it, its settings
        # and the agent's own session news keep its place in the list, its date and its retention clock (#387).
        if touch:
            self.meta["updated_at"] = _now()
        # Wake whoever streams this chat (the app's event stream), and the chat list's stream.
        waiters, self.waiters = self.waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
        if self.on_change is not None:
            self.on_change()

    async def changed(self, after: int, timeout: float) -> bool:
        """Wait until the transcript moves past `after`; False when nothing happened in time."""
        if self.seq > after:
            return True
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            if waiter in self.waiters:
                self.waiters.remove(waiter)
        return self.seq > after

    def add(self, item_type: str, **fields: Any) -> Dict[str, Any]:
        item = {"id": f"{item_type[0]}{len(self.items) + 1}_{secrets.token_hex(3)}", "type": item_type, "at": _now(),
                **fields}
        self.items.append(item)
        self.bump(item)
        if item_type not in ("assistant", "thought"):
            self.open_text = None
        return item

    @property
    def private(self) -> bool:
        """Started in private mode: kept in memory only, gone when the server stops (#114)."""
        return bool(self.meta.get("private"))

    def save(self) -> None:
        self.save_pending = False
        if self.private:
            return
        _write_json(self.dir / "transcript.json", self.items)
        _write_json(self.dir / "chat.json", self.meta)

    def public(self) -> Dict[str, Any]:
        meta = {key: value for key, value in self.meta.items() if key not in ("acp_session_id", "credential_id")}
        meta["running"] = self.turn is not None and not self.turn.done()
        meta["agent_alive"] = self.conn is not None and self.conn.running
        meta["connecting"] = self.warming is not None and not self.warming.done()
        meta.setdefault("config", [])
        # Whether a picture goes to the agent as one (else as a file link): the app notes it on the attachment.
        meta["takes_images"] = None if self.prompt_caps is None else bool(self.prompt_caps.get("image"))
        return meta


class ChatHost:
    def __init__(self) -> None:
        self._chats: Dict[str, Chat] = {}
        self._loaded = False
        self._version = 0  # moves with any chat's change; the chat list's stream follows it
        self._list_waiters: List[asyncio.Future] = []

    def _changed(self) -> None:
        self._version += 1
        waiters, self._list_waiters = self._list_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def list_stream(self, disconnected) -> Any:
        """The chat list as it changes (any chat starting, finishing, waiting, renamed): for the sidebar, so a
        chat working in the background shows it. A keep-alive (None) when quiet. It starts with the list as it is, so
        nothing that changed while the app reconnected is missed."""
        self._load_all()
        cursor = self._version
        yield self.list(agents=False)
        while not await disconnected():
            if self._version == cursor:
                waiter: asyncio.Future = asyncio.get_running_loop().create_future()
                self._list_waiters.append(waiter)
                try:
                    await asyncio.wait_for(waiter, STREAM_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield None
                    continue
                finally:
                    if waiter in self._list_waiters:
                        self._list_waiters.remove(waiter)
            await asyncio.sleep(LIST_COALESCE_S)  # a streaming answer changes its chat many times a second
            cursor = self._version
            yield self.list(agents=False)

    # ── store ───────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        root = chats_root()
        if not root.is_dir():
            return
        keep_s = chat_retention_days() * 86400
        for folder in root.iterdir():
            if not CHAT_ID.match(folder.name):
                continue
            meta = _read_json(folder / "chat.json", None)
            if not isinstance(meta, dict) or meta.get("id") != folder.name:
                continue
            items = _read_json(folder / "transcript.json", [])
            if not items and time.time() - float(meta.get("updated_at") or 0) > EMPTY_CHAT_KEEP_S:
                shutil.rmtree(folder, ignore_errors=True)  # opened and left without a word
                continue
            if _last_used(meta) and time.time() - _last_used(meta) > keep_s:
                shutil.rmtree(folder, ignore_errors=True)  # unused past Settings › Privacy's chat retention
                continue
            if meta.get("status") in ("running", "waiting"):
                meta["status"] = "idle"  # the server stopped mid-turn
            chat = Chat(meta, items if isinstance(items, list) else [])
            chat.on_change = self._changed
            for item in chat.items:
                if item.get("type") == "permission" and item.get("status") == "pending":
                    item["status"] = "expired"
                if item.get("type") == "tool" and item.get("status") == "running":
                    item["status"] = "stopped"
            self._chats[folder.name] = chat

    def _chat(self, chat_id: str) -> Chat:
        self._load_all()
        chat = self._chats.get(str(chat_id or ""))
        if chat is None:
            raise ChatError("No such chat.", "not_found", 404)
        return chat

    def _schedule_save(self, chat: Chat) -> None:
        if chat.save_pending:
            return
        chat.save_pending = True
        asyncio.get_running_loop().call_later(0.5, chat.save)

    # ── API ─────────────────────────────────────────────────────────────────

    def list(self, agents: bool = True) -> Dict[str, Any]:
        self._load_all()
        chats = sorted(self._chats.values(), key=lambda chat: float(chat.meta.get("updated_at") or 0), reverse=True)
        projects: Dict[str, Dict[str, Any]] = {}
        for chat in chats:
            project = chat.meta["project"]
            entry = projects.setdefault(project, {"path": project, "name": Path(project).name or project, "chats": []})
            entry["chats"].append(chat.public())
        out: Dict[str, Any] = {"ok": True, "projects": list(projects.values())}
        if agents:
            out["agents"] = available_agents()
        return out

    def create(self, project: str, agent: str, model: str = "", title: str = "") -> Dict[str, Any]:
        self._load_all()
        folder = Path(str(project or "")).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ChatError("Choose an existing project folder.", "bad_project")
        folder = folder.resolve()
        if folder == Path("/") or folder == Path.home().resolve():
            raise ChatError("Choose a project folder, not the whole disk or home folder.", "bad_project")
        if agent not in AGENTS or not provider_enabled(agent):
            raise ChatError("That agent is not turned on in Settings.", "agent_disabled")
        reason = _not_ready(agent)
        if reason:
            raise ChatError(reason, "agent_missing")  # the app shows the same reason and keeps Start Chat off
        chat_id = "chat_" + secrets.token_hex(6)
        now = _now()
        meta = {
            "id": chat_id, "title": _cut(str(title or "").strip(), MAX_TITLE) or "New chat", "titled": bool(title),
            "project": str(folder), "agent": agent, "model": str(model or provider_setting(agent, "model") or "").strip(),
            "created_at": now, "updated_at": now, "status": "idle", "rev": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0}, "context": None,
        }
        if privacy_policy.policy(fresh=True)["private_mode"]:
            meta["private"] = True  # nothing of it reaches the disk, also after private mode is turned off
        known = _read_json(_agent_options_path(), {})
        if isinstance(known, dict) and isinstance(known.get(agent), list):
            # Shown before the agent starts with the first message; the agent's own list replaces it then.
            meta["config"] = copy.deepcopy(known[agent])
        chat = Chat(meta, [])
        chat.on_change = self._changed
        if not chat.private:
            chat.dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                chat.dir.chmod(0o700)
            chat.save()
        self._chats[chat_id] = chat
        return {"ok": True, "chat": chat.public()}

    def rename(self, chat_id: str, title: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        title = " ".join(str(title or "").split())
        if not title:
            raise ChatError("Give the chat a name.", "empty")
        chat.meta["title"] = _cut(title, MAX_TITLE)
        chat.meta["titled"] = True  # the agent's own title no longer replaces it
        chat.bump(touch=False)
        chat.save()
        return {"ok": True, "chat": chat.public()}

    def organize(self, chat_id: str, archived: Optional[bool] = None, pinned: Optional[bool] = None) -> Dict[str, Any]:
        """Pin a chat to the top of the list, or archive it out of the list (and back). Only these two fields
        change; the rest of its record stays as it is."""
        chat = self._chat(chat_id)
        if archived is None and pinned is None:
            raise ChatError("Say what to change: archived or pinned.", "bad_request")
        for key, value in (("archived_at", archived), ("pinned_at", pinned)):
            if value is True:
                chat.meta[key] = chat.meta.get(key) or _now()
            elif value is False:
                chat.meta.pop(key, None)
        chat.bump(touch=False)
        chat.save()
        return {"ok": True, "chat": chat.public()}

    def usage(self, days: int = 30) -> Dict[str, Any]:
        """Mac MCP Chat's own usage, apart from delegated agents: tokens and answers by agent, model and day."""
        self._load_all()
        days = max(1, min(int(days or 30), 365))
        since = time.time() - days * 86400
        totals = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "chats": 0, "seconds": 0.0}
        agents: Dict[str, Dict[str, Any]] = {}
        models: Dict[tuple, Dict[str, Any]] = {}
        daily: Dict[str, Dict[str, Any]] = {}
        for chat in self._chats.values():
            turns = [item for item in chat.items if item.get("type") == "turn" and float(item.get("ended_at") or 0) >= since]
            if not turns:
                continue
            agent = chat.meta["agent"]
            row = agents.setdefault(agent, {"agent": agent, "label": AGENTS.get(agent, {}).get("label", agent),
                                            "turns": 0, "input_tokens": 0, "output_tokens": 0, "chats": 0})
            row["chats"] += 1
            totals["chats"] += 1
            for turn in turns:
                tokens_in, tokens_out = int(turn.get("input_tokens") or 0), int(turn.get("output_tokens") or 0)
                for bucket in (totals, row):
                    bucket["turns"] += 1
                    bucket["input_tokens"] += tokens_in
                    bucket["output_tokens"] += tokens_out
                totals["cached_tokens"] += int(turn.get("cached_tokens") or 0)
                totals["seconds"] += float(turn.get("duration_s") or 0)
                model = models.setdefault((agent, str(turn.get("model") or "")), {
                    "agent": agent, "model": str(turn.get("model") or "") or "default", "turns": 0, "tokens": 0})
                model["turns"] += 1
                model["tokens"] += tokens_in + tokens_out
                day = time.strftime("%Y-%m-%d", time.localtime(float(turn.get("ended_at") or 0)))
                bucket = daily.setdefault(day, {"date": day, "turns": 0, "tokens": 0, "agents": {}})
                bucket["turns"] += 1
                bucket["tokens"] += tokens_in + tokens_out
                bucket["agents"][agent] = bucket["agents"].get(agent, 0) + tokens_in + tokens_out  # the day's bar, by agent
        totals["seconds"] = round(totals["seconds"], 1)
        return {
            "ok": True, "days": days, "totals": totals,
            "agents": sorted(agents.values(), key=lambda row: -(row["input_tokens"] + row["output_tokens"])),
            "models": sorted(models.values(), key=lambda row: -row["tokens"])[:12],
            "daily": [daily[key] for key in sorted(daily)],
        }

    def read(self, chat_id: str, after: int = 0) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        items = [item for item in chat.items if int(item.get("rev") or 0) > after]
        return {"ok": True, "chat": chat.public(), "items": items, "cursor": chat.seq}

    async def stream(self, chat_id: str, after: int, disconnected) -> Any:
        """The chat's changes as they happen: one read() batch per burst, a keep-alive when quiet."""
        chat = self._chat(chat_id)
        cursor = max(0, int(after))
        while not await disconnected():
            if not await chat.changed(cursor, STREAM_KEEPALIVE_S):
                yield None
                continue
            await asyncio.sleep(STREAM_COALESCE_S)  # a burst of words goes out as one update
            batch = self.read(chat_id, cursor)
            cursor = batch["cursor"]
            yield batch

    async def prompt(self, chat_id: str, text: str, attachments: Optional[List[str]] = None) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        text = str(text or "").strip()
        files = _attachments(attachments or [])
        if not text and not files:
            raise ChatError("Write a message first.", "empty")
        if len(text) > MAX_TEXT:
            raise ChatError("That message is too long.", "too_long")
        if chat.meta["agent"] not in AGENTS or not provider_enabled(chat.meta["agent"]):
            raise ChatError("That agent is turned off in Settings.", "agent_disabled", 409)
        if chat.turn is not None and not chat.turn.done():
            # Written while the agent works: it goes out when this answer ends (kept if the server restarts).
            queue = chat.meta.setdefault("queue", [])
            if len(queue) >= MAX_QUEUE:
                raise ChatError(f"At most {MAX_QUEUE} messages can wait.", "queue_full", 409)
            queue.append({"id": f"q_{secrets.token_hex(4)}", "text": text, "attachments": files})
            chat.bump()
            chat.save()
            return {"ok": True, "queued": True, "chat": chat.public(), "cursor": chat.seq}
        self._start_turn(chat, text, files)
        return {"ok": True, "chat": chat.public(), "cursor": chat.seq}

    def _start_turn(self, chat: Chat, text: str, files: List[Dict[str, Any]]) -> None:
        chat.stop_requested = False
        if chat.idle is not None:
            chat.idle.cancel()
            chat.idle = None
        chat.add("user", text=text, **({"attachments": files} if files else {}))
        if not chat.meta.get("titled") and chat.meta.get("title") == "New chat":
            chat.meta["title"] = _cut(text.splitlines()[0] if text else files[0]["name"], 60)
        chat.meta["status"] = "running"
        chat.meta["turn_started_at"] = _now()
        chat.save()
        chat.turn = asyncio.create_task(self._run_turn(chat, text, files))

    async def queued(self, chat_id: str, queued_id: str, action: str) -> Dict[str, Any]:
        """A waiting message: removed, or sent now (the answer in progress is stopped first)."""
        chat = self._chat(chat_id)
        queue = chat.meta.setdefault("queue", [])
        entry = next((entry for entry in queue if entry.get("id") == queued_id), None)
        if entry is None:
            raise ChatError("That message is no longer waiting.", "not_queued", 409)
        if action not in ("remove", "send_now"):
            raise ChatError("Unknown action.", "bad_request")
        queue.remove(entry)
        chat.bump()
        chat.save()
        if action == "send_now":
            if chat.meta["agent"] not in AGENTS or not provider_enabled(chat.meta["agent"]):
                raise ChatError("That agent is turned off in Settings.", "agent_disabled", 409)
            await self.cancel(chat_id)
            self._start_turn(chat, entry["text"], list(entry.get("attachments") or []))
        return {"ok": True, "chat": chat.public(), "cursor": chat.seq}

    def _send_next(self, chat: Chat) -> None:
        """After an answer, the next waiting message goes out; not after Stop or a failure, so nothing runs on
        that the owner did not see coming."""
        queue = chat.meta.get("queue") or []
        if not queue or chat.meta["agent"] not in AGENTS or not provider_enabled(chat.meta["agent"]):
            return
        entry = queue.pop(0)
        self._start_turn(chat, entry["text"], list(entry.get("attachments") or []))

    async def cancel(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if chat.turn is None or chat.turn.done():
            return {"ok": True, "chat": chat.public()}
        for future in list(chat.permissions.values()):
            if not future.done():
                future.set_result(None)
        if chat.conn is not None and chat.session_id:
            await chat.conn.notify("session/cancel", {"sessionId": chat.session_id})
        else:
            chat.stop_requested = True  # nothing was asked yet: the agent is still starting
            chat.turn.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(chat.turn), 10)
        except asyncio.CancelledError:
            if not chat.turn.cancelled():
                raise
            # Cancelled before it ran a line: the turn is closed here instead.
            chat.add("notice", text="Stopped.", tone="neutral")
            self._end_turn(chat, float(chat.meta.get("turn_started_at") or _now()), "cancelled", {})
        except asyncio.TimeoutError:
            await self._stop_agent(chat)  # an agent that ignores cancel is stopped; the session reopens later
        return {"ok": True, "chat": chat.public()}

    def answer(self, chat_id: str, item_id: str, option_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        future = chat.permissions.get(item_id)
        item = next((entry for entry in chat.items if entry.get("id") == item_id), None)
        if future is None or future.done() or item is None:
            raise ChatError("That question was already answered.", "not_pending", 409)
        if option_id not in {option.get("id") for option in item.get("options") or []}:
            raise ChatError("Unknown choice.", "bad_option")
        future.set_result(option_id)
        return {"ok": True}

    async def delete(self, chat_id: str, only_if_empty: bool = False) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if only_if_empty and (chat.items or (chat.turn is not None and not chat.turn.done())):
            return {"ok": True, "kept": True}  # the app thought it was empty; the transcript decides
        if chat.turn is not None and not chat.turn.done():
            await self.cancel(chat_id)
        await self._stop_agent(chat)
        self._chats.pop(chat.meta["id"], None)
        shutil.rmtree(chat.dir, ignore_errors=True)
        self._changed()
        return {"ok": True}

    async def expire(self, now: Optional[float] = None) -> List[str]:
        """Remove chats unused for longer than the chat retention; a working chat is kept (#114)."""
        self._load_all()
        cutoff = (time.time() if now is None else now) - chat_retention_days() * 86400
        removed: List[str] = []
        for chat in list(self._chats.values()):
            busy = ((chat.turn is not None and not chat.turn.done()) or chat.meta.get("status") in ("running", "waiting")
                    or (chat.warming is not None and not chat.warming.done()))
            if busy or not _last_used(chat.meta) or _last_used(chat.meta) >= cutoff:
                continue  # a record without a time is kept: nothing says it is old
            await self._stop_agent(chat)
            self._chats.pop(chat.meta["id"], None)
            shutil.rmtree(chat.dir, ignore_errors=True)
            removed.append(chat.meta["id"])
        if removed:
            self._changed()
        return removed

    async def retention_loop(self) -> None:
        """Expired chats go when the server starts, then once an hour."""
        while True:
            try:
                await self.expire()
            except Exception:  # retention must never stop the server; the next pass tries again
                pass
            await asyncio.sleep(RETENTION_SWEEP_S)

    async def shutdown(self) -> None:
        for chat in list(self._chats.values()):
            for task in (chat.turn, chat.warming):
                if task is not None and not task.done():
                    task.cancel()
            await self._stop_agent(chat)
            if chat.save_pending:
                chat.save()

    # ── one turn ────────────────────────────────────────────────────────────

    async def _run_turn(self, chat: Chat, text: str, files: Optional[List[Dict[str, Any]]] = None) -> None:
        started = float(chat.meta.get("turn_started_at") or _now())
        stop, usage = "error", {}
        try:
            await self._ensure_session(chat)
            blocks = _prompt_blocks(text, files or [], chat.prompt_caps or {})
            # Not before an agent command ("/compact"): the command must stay the message's first words.
            carry = bool(chat.meta.get("carry_over")) and not text.startswith("/")
            if carry:
                summary = _carry_over_summary(chat)
                if summary:
                    blocks.insert(0, {"type": "text", "text": summary})
            result = await chat.conn.request("session/prompt", {"sessionId": chat.session_id, "prompt": blocks})
            if carry:
                chat.meta.pop("carry_over", None)
            stop = str(result.get("stopReason") or "end_turn")
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}

            totals = chat.meta.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
            totals["input_tokens"] = int(totals.get("input_tokens") or 0) + int(usage.get("inputTokens") or 0)
            totals["output_tokens"] = int(totals.get("output_tokens") or 0) + int(usage.get("outputTokens") or 0)
            if stop == "cancelled":
                chat.add("notice", text="Stopped.", tone="neutral")
            elif stop in ("max_tokens", "max_turn_requests"):
                chat.add("notice", text="The agent stopped at its limit for one answer.", tone="warning")
            elif stop == "refusal":
                chat.add("notice", text="The agent declined to continue.", tone="warning")
        except asyncio.CancelledError:
            if not chat.stop_requested:
                raise
            asyncio.current_task().uncancel()
            stop = "cancelled"
            chat.add("notice", text="Stopped.", tone="neutral")
        except AcpClosed:
            chat.add("notice", text=_cut("The agent quit unexpectedly. " + _last_log_line(chat.dir / "agent.log"), 600),
                     tone="error")
            await self._stop_agent(chat)
        except AcpError as exc:
            chat.add("notice", text=_cut(f"The agent stopped: {exc}", 600), tone="error")
            await self._stop_agent(chat)
        except ChatError as exc:
            chat.add("notice", text=str(exc), tone="error")
        except OSError as exc:
            chat.add("notice", text=_cut(f"Could not start the agent: {exc}", 600), tone="error")
            await self._stop_agent(chat)
        finally:
            self._end_turn(chat, started, stop, usage)

    def _end_turn(self, chat: Chat, started: float, stop: str, usage: Dict[str, Any]) -> None:
        for item in chat.tools.values():
            if item.get("status") == "running":
                item["status"] = "stopped"
                chat.bump(item)
        chat.tools.clear()
        chat.open_text = None
        # What the CLI reports for the answer: how long it worked, with which model, and its tokens.
        model = next((option for option in chat.meta.get("config") or [] if option["category"] == "model"), None)
        model_name = next((choice["name"] for choice in (model or {}).get("options") or []
                           if choice["value"] == (model or {}).get("current")), (model or {}).get("current") or "")
        ended = _now()
        chat.add("turn", started_at=started, ended_at=ended, duration_s=round(max(0.0, ended - started), 1),
                 stop=stop, model=model_name or chat.meta.get("model") or "",
                 input_tokens=int(usage.get("inputTokens") or 0), output_tokens=int(usage.get("outputTokens") or 0),
                 cached_tokens=int(usage.get("cachedReadTokens") or 0))
        chat.meta.pop("turn_started_at", None)
        chat.meta["status"] = "idle"
        chat.bump()
        chat.save()
        self._arm_idle(chat)
        if stop not in ("cancelled", "error"):
            self._send_next(chat)

    async def _ensure_session(self, chat: Chat) -> None:
        async with chat.start_lock:
            if chat.conn is not None and chat.conn.running and chat.session_id:
                return
            await self._stop_agent(chat)
            agent = chat.meta["agent"]
            spec = AGENTS[agent]
            command = _command(agent)
            if not command:
                raise ChatError(f"{spec['label']} is not installed." if not _binary(agent)
                                else "Node.js (npx) is needed to connect to it.", "agent_missing")
            project = chat.meta["project"]
            if not Path(project).is_dir():
                raise ChatError("The project folder is gone.", "bad_project")
            token, credential_id = get_scoped_credential_store().issue(
                agent_id=chat.meta["id"], team_id=None, profile="trusted",
                scope=ResourceScope(path_roots=(project,)), ttl_s=CREDENTIAL_TTL_S,
            )
            chat.meta["credential_id"] = credential_id
            conn = AcpConnection(lambda method, params: self._on_update(chat, method, params),
                                 lambda method, params: self._on_request(chat, method, params))
            if spec.get("restart_options"):
                command[1:1] = _restart_args(chat)  # before Copilot's variadic --excluded-tools
            if spec.get("download"):
                await asyncio.to_thread(_ensure_download, agent)
            await conn.start(command, cwd=project, env=_agent_env(agent, chat.meta.get("model") or "", token),
                             stderr_path=None if chat.private else chat.dir / "agent.log")
            chat.conn = conn
            try:
                # The first start of an adapter downloads it (npx), so this may take a while.
                init = await conn.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    # The agent runs its commands itself; with Zed's terminal_output it streams their output to us.
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                                           "_meta": {"terminal_output": True}},
                    "clientInfo": {"name": "mac-mcp-chat", "title": "Mac MCP Chat", "version": __version__},
                }, timeout=240)
                caps = (init.get("agentCapabilities") or {}).get("promptCapabilities")
                chat.prompt_caps = caps if isinstance(caps, dict) else {}
                servers = [] if spec.get("mcp_on_command_line") else [
                    {"type": "http", "name": spec.get("mcp_name", "mac-mcp"), "url": _mcp_endpoint(),
                     "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
                methods = {str(entry.get("id")) for entry in init.get("authMethods") or [] if isinstance(entry, dict)}
                try:
                    opened = await self._open_session(chat, init, servers)
                except AcpError as exc:
                    # An agent with no sign-in method chosen yet (Antigravity) opens a session once told which one.
                    if not (_needs_sign_in(exc) and spec.get("auth_method") in methods):
                        raise
                    await conn.request("authenticate", {"methodId": spec["auth_method"]}, timeout=180)
                    opened = await self._open_session(chat, init, servers)
            except AcpError as exc:
                if _needs_sign_in(exc):
                    raise ChatError(f"Sign in to {spec['label']} first: run `{spec['login']}` in Terminal.",
                                    "auth_required", 409) from exc
                raise
            if "configOptions" in opened:
                self._set_config(chat, opened.get("configOptions"))
            elif spec.get("restart_options"):
                self._set_config(chat, [])
            await self._apply_choices(chat)
            chat.meta.pop("agent_error", None)
            chat.bump(touch=False)
            chat.save()

    async def _open_session(self, chat: Chat, init: Dict[str, Any], servers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The agent's earlier session for this chat (session/load) or a new one."""
        conn = chat.conn
        project = chat.meta["project"]
        previous = chat.meta.get("acp_session_id")
        opened: Dict[str, Any] = {}
        if previous and (init.get("agentCapabilities") or {}).get("loadSession"):
            chat.replaying = True  # the agent replays the conversation; the transcript already has it
            try:
                opened = await conn.request("session/load", {"sessionId": previous, "cwd": project,
                                                             "mcpServers": servers}, timeout=120)
                chat.session_id = previous
            except AcpError as exc:
                if _needs_sign_in(exc):
                    raise
            finally:
                chat.replaying = False
        if not chat.session_id:
            opened = await conn.request("session/new", {"cwd": project, "mcpServers": servers}, timeout=120)
            chat.session_id = str(opened.get("sessionId") or "")
            if not chat.session_id:
                raise ChatError("The agent did not open a session.", "agent_failed")
            chat.meta["acp_session_id"] = chat.session_id
            if previous and any(item.get("type") == "turn" for item in chat.items):
                # The earlier session is gone (it would not load, or the agent loads none): the next message takes
                # a summary of the conversation along. Kept in the record until it went, also over a restart.
                # Only worth it when there was a conversation (an empty session is not kept).
                chat.meta["carry_over"] = True
                chat.add("notice", text="The earlier conversation could not be reopened; the agent continues from "
                                        "a summary of it.", tone="warning")
        return opened

    async def _apply_choices(self, chat: Chat) -> None:
        """The owner's picks (model, thinking, mode) survive a restart of the agent: set them again."""
        for config_id, value in dict(chat.meta.get("choices") or {}).items():
            option = next((entry for entry in chat.meta.get("config") or [] if entry["id"] == config_id), None)
            if option is None or option.get("local") or option["current"] == value:
                continue
            if value not in {choice["value"] for choice in option["options"]}:
                chat.meta["choices"].pop(config_id, None)  # the agent no longer offers it
                continue
            try:
                result = await chat.conn.request("session/set_config_option", {
                    "sessionId": chat.session_id, "configId": config_id, "value": value}, timeout=60)
            except AcpError:
                continue
            if "configOptions" in result:
                self._set_config(chat, result.get("configOptions"))

    def _set_config(self, chat: Chat, raw: Any) -> None:
        options = _config_options(raw)
        if AGENTS.get(chat.meta["agent"], {}).get("restart_options"):
            options += _local_options(chat, options)
        chat.meta["config"] = options
        if options and not chat.private:
            known = _read_json(_agent_options_path(), {})
            known = known if isinstance(known, dict) else {}
            known[chat.meta["agent"]] = [option for option in options if not option.get("local")]
            with contextlib.suppress(OSError):
                _write_json(_agent_options_path(), known)

    def _arm_idle(self, chat: Chat) -> None:
        if chat.idle is not None:
            chat.idle.cancel()
        if chat.conn is not None and chat.conn.running:
            loop = asyncio.get_running_loop()
            chat.idle = loop.call_later(IDLE_STOP_S, lambda: asyncio.ensure_future(self._stop_agent(chat)))

    async def _warm(self, chat: Chat) -> None:
        try:
            await self._ensure_session(chat)
        except ChatError as exc:
            chat.meta["agent_error"] = str(exc)
        except AcpClosed:
            chat.meta["agent_error"] = _cut("The agent quit. " + _last_log_line(chat.dir / "agent.log"), 400)
            await self._stop_agent(chat)
        except (AcpError, OSError) as exc:
            chat.meta["agent_error"] = _cut(f"Could not start the agent: {exc}", 400)
            await self._stop_agent(chat)
        else:
            if chat.turn is None or chat.turn.done():
                self._arm_idle(chat)
        chat.bump(touch=False)
        chat.save()

    def connect(self, chat_id: str) -> Dict[str, Any]:
        """Start the chat's agent ahead of the first message, so its models and modes can be shown."""
        chat = self._chat(chat_id)
        alive = chat.conn is not None and chat.conn.running and chat.session_id
        if not alive and (chat.warming is None or chat.warming.done()) and (chat.turn is None or chat.turn.done()):
            chat.meta.pop("agent_error", None)
            chat.warming = asyncio.create_task(self._warm(chat))
        return {"ok": True, "chat": chat.public()}

    async def configure(self, chat_id: str, config_id: str, value: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        option = next((entry for entry in chat.meta.get("config") or [] if entry["id"] == config_id), None)
        if option is None:
            raise ChatError("The agent does not offer that setting.", "bad_option")
        if value not in {choice["value"] for choice in option["options"]}:
            raise ChatError("Unknown choice.", "bad_option")
        if option.get("local"):
            # Set by starting the agent again with it (Copilot's model and thinking).
            if chat.turn is not None and not chat.turn.done():
                raise ChatError("Change it after this answer.", "busy", 409)
            chat.meta.setdefault("choices", {})[config_id] = value
            option["current"] = value
            chat.bump(touch=False)
            chat.save()
            # A running agent takes it by starting again; that waits for the next message (#387).
            await self._stop_agent(chat)
            return {"ok": True, "chat": chat.public()}
        chat.meta.setdefault("choices", {})[config_id] = value
        if chat.conn is not None and chat.conn.running and chat.session_id:
            try:
                result = await chat.conn.request("session/set_config_option", {
                    "sessionId": chat.session_id, "configId": config_id, "value": value}, timeout=60)
            except AcpError as exc:
                raise ChatError(_cut(f"The agent did not take it: {exc}", 300), "agent_failed", 409) from exc
            if "configOptions" in result:
                self._set_config(chat, result.get("configOptions"))
            else:
                option["current"] = value
        else:
            option["current"] = value  # set again when the agent starts
        # Choosing a model or Fast mode is not using the chat: it keeps its date and place (#387).
        chat.bump(touch=False)
        chat.save()
        return {"ok": True, "chat": chat.public()}

    async def _stop_agent(self, chat: Chat) -> None:
        if chat.idle is not None:
            chat.idle.cancel()
            chat.idle = None
        conn, chat.conn, chat.session_id = chat.conn, None, None
        if conn is not None:
            await conn.close()
        get_scoped_credential_store().revoke_agent(chat.meta["id"])

    # ── what the agent sends ────────────────────────────────────────────────

    async def _on_update(self, chat: Chat, method: str, params: Dict[str, Any]) -> None:
        if method != "session/update" or chat.replaying:
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            text = _text_of(update.get("content"))
            if not text:
                return
            wanted = "assistant" if kind == "agent_message_chunk" else "thought"
            status_line = AGENTS.get(chat.meta["agent"], {}).get("status_line")
            if wanted == "assistant" and status_line is not None and status_line.match(text):
                if text.startswith("Warning: "):
                    chat.add("notice", text=_cut(text[len("Warning: "):], 600), tone="warning")
                    self._schedule_save(chat)
                return
            item = chat.open_text
            if item is None or item.get("type") != wanted:
                item = chat.add(wanted, text="")
                chat.open_text = item
            item["text"] = (item["text"] + text)[-MAX_TEXT:]
            chat.bump(item)
        elif kind == "tool_call":
            self._tool(chat, update, new=True)
        elif kind == "tool_call_update":
            self._tool(chat, update, new=False)
        elif kind == "plan":
            entries = [{"text": _cut(str(entry.get("content") or ""), 300), "status": str(entry.get("status") or "")}
                       for entry in update.get("entries") or [] if isinstance(entry, dict)]
            plan = next((item for item in reversed(chat.items) if item.get("type") == "plan"), None)
            if plan is not None and plan is chat.items[-1]:
                plan["entries"] = entries
                chat.bump(plan)
            else:
                chat.add("plan", entries=entries)
        elif kind == "session_info_update":
            title = str(update.get("title") or "").strip()
            if title and not chat.meta.get("titled"):
                chat.meta["title"] = _cut(title, MAX_TITLE)
                chat.bump(touch=False)
        elif kind == "config_option_update":
            self._set_config(chat, update.get("configOptions"))
            chat.bump(touch=False)
        elif kind == "available_commands_update":
            commands = []
            for command in update.get("availableCommands") or []:
                if isinstance(command, dict) and command.get("name"):
                    hint = (command.get("input") or {}).get("hint") if isinstance(command.get("input"), dict) else ""
                    commands.append({"name": str(command["name"]).lstrip("/")[:60],
                                     "description": _cut(str(command.get("description") or ""), 200),
                                     "hint": _cut(str(hint or ""), 80)})
            chat.meta["commands"] = commands[:500]  # Claude lists its skills as commands too
            chat.bump(touch=False)
        elif kind == "current_mode_update":
            for option in chat.meta.get("config") or []:
                if option["category"] == "mode":
                    option["current"] = str(update.get("currentModeId") or option["current"])
            chat.bump(touch=False)
        elif kind == "usage_update":
            chat.meta["context"] = {"used": int(update.get("used") or 0), "size": int(update.get("size") or 0)}
            chat.bump(touch=False)
        else:
            return
        self._schedule_save(chat)

    def _tool(self, chat: Chat, update: Dict[str, Any], new: bool) -> None:
        call_id = str(update.get("toolCallId") or "")
        if call_id in chat.startup_calls or _MCP_STARTUP.match(str(update.get("title") or "").strip()):
            self._mcp_startup(chat, call_id, update)
            return
        item = chat.tools.get(call_id)
        if item is None:
            item = chat.add("tool", call_id=call_id, title="Working", kind="other", status="running",
                            input={}, files=[], output="")
            chat.tools[call_id] = item
        if update.get("title"):
            item["agent_title"] = _cut(str(update["title"]), 300)
            server, tool = _tool_source(item["agent_title"])
            if tool and not item.get("tool"):
                item["server"], item["tool"] = server, tool
        if update.get("kind"):
            item["kind"] = str(update["kind"])
        if update.get("status"):
            item["status"] = _TOOL_STATUS.get(str(update["status"]), item["status"])
        if isinstance(update.get("rawInput"), dict):
            item["input"] = _inputs(update["rawInput"])
            if isinstance(update["rawInput"].get("description"), str) and update["rawInput"]["description"].strip():
                item["intent"] = update["rawInput"]["description"].strip()
        if update.get("locations"):
            item["files"] = [str(place.get("path") or "") for place in update["locations"] if isinstance(place, dict)][:20]
        diffs = _diffs(update.get("content"))
        if diffs:
            item["diffs"] = diffs
        meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        info = meta.get("terminal_info") if isinstance(meta.get("terminal_info"), dict) else None
        chunk = meta.get("terminal_output_delta") or meta.get("terminal_output")
        ended = meta.get("terminal_exit") if isinstance(meta.get("terminal_exit"), dict) else None
        if info or chunk or ended:
            terminal = item.setdefault("terminal", {"output": "", "exit_code": None})
            if info and info.get("cwd"):
                terminal["cwd"] = str(info["cwd"])
            if isinstance(chunk, dict) and chunk.get("data"):
                terminal["output"] = (terminal["output"] + _terminal_text(chunk["data"]))[-MAX_TERMINAL:]
            if ended is not None:
                terminal["exit_code"] = ended.get("exit_code") if isinstance(ended.get("exit_code"), int) else None
                terminal["ended"] = True
        output = _text_of(update.get("content")) if update.get("content") else ""
        if not output and isinstance(update.get("rawOutput"), dict):
            raw = update["rawOutput"]
            output = str(raw.get("content") or raw.get("message") or "")
        if output:
            item["output"] = _readable_output(output)
        item["title"] = _friendly_title(item, str(update.get("title") or ""))
        chat.bump(item)
        if not new and item["status"] != "running":
            chat.tools.pop(call_id, None)

    def _mcp_startup(self, chat: Chat, call_id: str, update: Dict[str, Any]) -> None:
        """The Codex adapter reports each MCP server's start as a tool call. It is not a step: only a server
        that failed to start is worth a line, and one it skipped because the owner's config has it is not."""
        chat.startup_calls.add(call_id)
        if str(update.get("status") or "") != "failed":
            return
        detail = _text_of(update.get("content")) if update.get("content") else ""
        if not detail and isinstance(update.get("rawOutput"), dict):
            detail = str(update["rawOutput"].get("message") or update["rawOutput"].get("content") or "")
        if "already defines" in detail:
            return
        chat.add("notice", text=_cut(f"An MCP server did not start: {detail or 'no reason given'}", 400), tone="warning")

    async def _on_request(self, chat: Chat, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method != "session/request_permission":
            raise AcpError("Mac MCP Chat does not offer that.", -32601)
        if len(chat.permissions) >= MAX_PENDING_PERMISSIONS:
            return {"outcome": {"outcome": "cancelled"}}
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = [{"id": str(option.get("optionId") or ""), "label": str(option.get("name") or ""),
                    "kind": str(option.get("kind") or "")}
                   for option in params.get("options") or [] if isinstance(option, dict) and option.get("optionId")]
        tool = chat.tools.get(str(call.get("toolCallId") or ""))
        raw_title = str(call.get("title") or "")
        title = (_friendly_title({"agent_title": raw_title}, raw_title) if raw_title
                 else str((tool or {}).get("title") or "The agent wants to continue"))
        item = chat.add("permission", title=_cut(title, 200), call_id=str(call.get("toolCallId") or ""),
                        input=_inputs(call.get("rawInput")) if isinstance(call.get("rawInput"), dict) else {},
                        files=list((tool or {}).get("files") or []), options=options, status="pending",
                        choice=None)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        chat.permissions[item["id"]] = future
        chat.meta["status"] = "waiting"
        chat.bump()
        chat.save()
        try:
            choice = await asyncio.wait_for(future, PERMISSION_WAIT_S)
        except asyncio.TimeoutError:
            choice = None
        finally:
            chat.permissions.pop(item["id"], None)
        item["status"] = "answered" if choice else "cancelled"
        item["choice"] = choice
        chat.bump(item)
        if chat.meta.get("status") == "waiting" and not chat.permissions:
            chat.meta["status"] = "running"
            chat.bump()
        chat.save()
        if not choice:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": choice}}


_HOST: Optional[ChatHost] = None


def get_host() -> ChatHost:
    global _HOST
    if _HOST is None:
        _HOST = ChatHost()
    return _HOST


def reset_host_for_tests() -> None:
    global _HOST
    _HOST = None
