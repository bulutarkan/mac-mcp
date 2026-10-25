"""Mac MCP Chat: the owner's own conversations with a coding agent in a project folder.

Each chat runs one of the owner's own agent CLIs (Claude, Codex, OpenCode, GitHub Copilot) as an Agent Client
Protocol subprocess, signed in as the owner, with Mac MCP added as an MCP server on a scoped credential. The
agent says which models, thinking levels and modes it offers (ACP configOptions); the owner picks them in
the chat. The agent's permission prompts come to the owner in the chat window. Chats live in
<state>/chats/<chat_id>/ (chat.json + transcript.json); the agent keeps its own session, so a chat reopened
later continues where it stopped (session/load).

The transcript is a list of items (user, assistant, thought, tool, permission, plan, notice). Every change
gives the item a new `rev`; the app asks for items with rev > its cursor, so a streaming answer is read
by polling without missing a word.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import extra_harness_providers as harness
from . import paths
from .acp_client import PROTOCOL_VERSION, AcpClosed, AcpConnection, AcpError
from .agent_timeline import _TOOL_WORDS, _cut, _humanize, _inputs, _readable_output
from .policy_scope import ResourceScope
from .runtime_settings import provider_enabled, provider_setting
from .scoped_auth import get_scoped_credential_store
from .version import __version__

CHAT_ID = re.compile(r"^chat_[0-9a-f]{12}$")
IDLE_STOP_S = 15 * 60  # an idle chat's agent process stops; the next message reopens the session
PERMISSION_WAIT_S = 30 * 60
CREDENTIAL_TTL_S = 24 * 3600
MAX_TEXT = 200_000
MAX_TITLE = 120
STREAM_KEEPALIVE_S = 15.0
STREAM_COALESCE_S = 0.06  # at most ~15 updates a second reach the app
EMPTY_CHAT_KEEP_S = 6 * 3600  # a chat nobody wrote in is gone at the next start after this

# Claude Code and Codex speak ACP through their official adapters; the others have it built in.
AGENTS: Dict[str, Dict[str, Any]] = {
    "claude": {
        "label": "Claude", "cli": "claude", "login": "claude",
        "command": ["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.94.0"],
    },
    "codex": {
        "label": "Codex", "cli": "codex", "login": "codex login",
        "command": ["npx", "-y", "@agentclientprotocol/codex-acp@2.2.2"],
    },
    "opencode": {
        "label": "OpenCode", "cli": "opencode", "login": "opencode auth login",
        "command": ["{cli}", "acp"],
    },
    "copilot": {
        "label": "GitHub Copilot", "cli": "copilot", "login": "copilot login",
        # Copilot's own subagents and workflows are left out, as for delegated agents; Mac MCP is the only MCP.
        # Copilot sends its own status lines ("Info: Disabled tools: …") as whole message chunks.
        "status_line": re.compile(r"^(Info|Warning): "),
        "command": ["{cli}", "--acp", "--no-auto-update", "--disable-builtin-mcps", "--disable-client-telemetry",
                    "--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN",
                    "--excluded-tools", *harness.COPILOT_EXCLUDED_TOOLS],
    },
}
# The pickers the chat shows, in this order; other options go under "More".
SHOWN_CATEGORIES = ("model", "thought_level", "mode")

_TOOL_STATUS = {"pending": "running", "in_progress": "running", "completed": "done", "failed": "failed"}


class ChatError(Exception):
    def __init__(self, message: str, code: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def chats_root() -> Path:
    return paths.state_dir() / "chats"


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
    if command[0] == "{cli}":
        command[0] = cli
    else:
        tool = _which(command[0])
        if not tool:
            return None
        command[0] = tool
    return command


def _copilot_byok(model: str) -> bool:
    vendor, _, name = str(model or "").partition("/")
    return bool(os.getenv("COPILOT_PROVIDER_BASE_URL")) or (vendor.lower() in harness.COPILOT_BYOK and bool(name))


def _keychain_lookup(name: str) -> Optional[str]:
    from .tools_agents import _harness_keychain_value  # the server's one place for the OpenRouter key
    return _harness_keychain_value(name)


def _mcp_endpoint() -> str:
    from .tools_agents import _agent_mcp_endpoint
    return _agent_mcp_endpoint()


def _agent_env(agent: str, model: str) -> Dict[str, str]:
    """What the agent process sees: the owner's home (their sign-in and settings), no server secrets."""
    home = Path.home()
    user = os.getenv("USER") or home.name
    env = {
        "HOME": str(home), "USER": user, "LOGNAME": user, "LANG": "en_US.UTF-8",
        "PATH": _search_path(), "TMPDIR": os.getenv("TMPDIR", "/tmp"), "NO_COLOR": "1", "TERM": "dumb",
    }
    if agent == "opencode":
        # The owner's shell gives OpenCode their OpenRouter key; the server keeps it in the Keychain.
        key = os.getenv("OPENROUTER_API_KEY") or _keychain_lookup("OPENROUTER_API_KEY")
        if key:
            env["OPENROUTER_API_KEY"] = key
    if agent == "copilot" and _copilot_byok(model):
        # Copilot on the owner's own provider key (Settings › Agents model openrouter/…), as for delegated runs.
        private_home = chats_root() / "copilot-home"
        private_home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            private_home.chmod(0o700)
        env.update(harness.copilot_env(private_home, _keychain_lookup, model))
    return env


def available_agents() -> List[Dict[str, Any]]:
    """Agents the owner can chat with: enabled in Settings and installed. Disabled ones are not offered."""
    out = []
    for agent, spec in AGENTS.items():
        if not provider_enabled(agent):
            continue
        command = _command(agent)
        reason = "" if command else (f"{spec['label']} is not installed." if not _binary(agent)
                                     else "Node.js (npx) is needed to connect to it.")
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


def _needs_sign_in(exc: AcpError) -> bool:
    return exc.code == -32000 or "auth" in str(exc).lower()


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
    return ("mac-mcp" if server.replace("_", "-") == "mac-mcp" else server), match.group("tool")


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
            return f"Changed {content.get('path') or 'a file'}"
        return ""
    if isinstance(content, list):
        return "\n".join(part for part in (_text_of(item) for item in content) if part)
    return ""


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

    @property
    def dir(self) -> Path:
        return chats_root() / self.meta["id"]

    def bump(self, item: Optional[Dict[str, Any]] = None) -> None:
        self.seq += 1
        if item is not None:
            item["rev"] = self.seq
        else:
            self.meta["rev"] = self.seq
        self.meta["updated_at"] = _now()
        # Wake whoever streams this chat (the app's event stream).
        waiters, self.waiters = self.waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

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

    def save(self) -> None:
        self.save_pending = False
        _write_json(self.dir / "transcript.json", self.items)
        _write_json(self.dir / "chat.json", self.meta)

    def public(self) -> Dict[str, Any]:
        meta = {key: value for key, value in self.meta.items() if key not in ("acp_session_id", "credential_id")}
        meta["running"] = self.turn is not None and not self.turn.done()
        meta["agent_alive"] = self.conn is not None and self.conn.running
        meta["connecting"] = self.warming is not None and not self.warming.done()
        meta.setdefault("config", [])
        return meta


class ChatHost:
    def __init__(self) -> None:
        self._chats: Dict[str, Chat] = {}
        self._loaded = False

    # ── store ───────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        root = chats_root()
        if not root.is_dir():
            return
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
            if meta.get("status") in ("running", "waiting"):
                meta["status"] = "idle"  # the server stopped mid-turn
            chat = Chat(meta, items if isinstance(items, list) else [])
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

    def list(self) -> Dict[str, Any]:
        self._load_all()
        chats = sorted(self._chats.values(), key=lambda chat: float(chat.meta.get("updated_at") or 0), reverse=True)
        projects: Dict[str, Dict[str, Any]] = {}
        for chat in chats:
            project = chat.meta["project"]
            entry = projects.setdefault(project, {"path": project, "name": Path(project).name or project, "chats": []})
            entry["chats"].append(chat.public())
        return {"ok": True, "projects": list(projects.values()), "agents": available_agents()}

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
        if not _binary(agent):
            raise ChatError(f"{AGENTS[agent]['label']} is not installed.", "agent_missing")
        chat_id = "chat_" + secrets.token_hex(6)
        now = _now()
        meta = {
            "id": chat_id, "title": _cut(str(title or "").strip(), MAX_TITLE) or "New chat", "titled": bool(title),
            "project": str(folder), "agent": agent, "model": str(model or provider_setting(agent, "model") or "").strip(),
            "created_at": now, "updated_at": now, "status": "idle", "rev": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0}, "context": None,
        }
        chat = Chat(meta, [])
        chat.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            chat.dir.chmod(0o700)
        chat.save()
        self._chats[chat_id] = chat
        return {"ok": True, "chat": chat.public()}

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

    async def prompt(self, chat_id: str, text: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        text = str(text or "").strip()
        if not text:
            raise ChatError("Write a message first.", "empty")
        if len(text) > MAX_TEXT:
            raise ChatError("That message is too long.", "too_long")
        if chat.turn is not None and not chat.turn.done():
            raise ChatError("The agent is still answering. Stop it or wait.", "busy", 409)
        if chat.meta["agent"] not in AGENTS or not provider_enabled(chat.meta["agent"]):
            raise ChatError("That agent is turned off in Settings.", "agent_disabled", 409)
        if chat.idle is not None:
            chat.idle.cancel()
            chat.idle = None
        chat.add("user", text=text)
        if not chat.meta.get("titled") and chat.meta.get("title") == "New chat":
            chat.meta["title"] = _cut(text.splitlines()[0], 60)
        chat.meta["status"] = "running"
        chat.meta["turn_started_at"] = _now()
        chat.save()
        chat.turn = asyncio.create_task(self._run_turn(chat, text))
        return {"ok": True, "chat": chat.public(), "cursor": chat.seq}

    async def cancel(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if chat.turn is None or chat.turn.done():
            return {"ok": True, "chat": chat.public()}
        for future in list(chat.permissions.values()):
            if not future.done():
                future.set_result(None)
        if chat.conn is not None and chat.session_id:
            await chat.conn.notify("session/cancel", {"sessionId": chat.session_id})
        try:
            await asyncio.wait_for(asyncio.shield(chat.turn), 10)
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
        return {"ok": True}

    async def shutdown(self) -> None:
        for chat in list(self._chats.values()):
            for task in (chat.turn, chat.warming):
                if task is not None and not task.done():
                    task.cancel()
            await self._stop_agent(chat)
            if chat.save_pending:
                chat.save()

    # ── one turn ────────────────────────────────────────────────────────────

    async def _run_turn(self, chat: Chat, text: str) -> None:
        started = float(chat.meta.get("turn_started_at") or _now())
        stop, usage = "error", {}
        try:
            await self._ensure_session(chat)
            result = await chat.conn.request("session/prompt", {
                "sessionId": chat.session_id, "prompt": [{"type": "text", "text": text}]})
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
            raise
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
            await conn.start(command, cwd=project, env=_agent_env(agent, chat.meta.get("model") or ""),
                             stderr_path=chat.dir / "agent.log")
            chat.conn = conn
            try:
                # The first start of an adapter downloads it (npx), so this may take a while.
                init = await conn.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "mac-mcp-chat", "title": "Mac MCP Chat", "version": __version__},
                }, timeout=240)
                servers = [{"type": "http", "name": "mac-mcp", "url": _mcp_endpoint(),
                            "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
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
                        if any(item.get("type") == "turn" for item in chat.items):
                            # Only worth saying when there was a conversation (an empty session is not kept).
                            chat.add("notice", text="The earlier conversation could not be reopened; the agent starts fresh.",
                                     tone="warning")
                    finally:
                        chat.replaying = False
                if not chat.session_id:
                    opened = await conn.request("session/new", {"cwd": project, "mcpServers": servers}, timeout=120)
                    chat.session_id = str(opened.get("sessionId") or "")
                    if not chat.session_id:
                        raise ChatError("The agent did not open a session.", "agent_failed")
                    chat.meta["acp_session_id"] = chat.session_id
            except AcpError as exc:
                if _needs_sign_in(exc):
                    raise ChatError(f"Sign in to {spec['label']} first: run `{spec['login']}` in Terminal.",
                                    "auth_required", 409) from exc
                raise
            if "configOptions" in opened:
                chat.meta["config"] = _config_options(opened.get("configOptions"))
            await self._apply_choices(chat)
            chat.meta.pop("agent_error", None)
            chat.bump()
            chat.save()

    async def _apply_choices(self, chat: Chat) -> None:
        """The owner's picks (model, thinking, mode) survive a restart of the agent: set them again."""
        for config_id, value in dict(chat.meta.get("choices") or {}).items():
            option = next((entry for entry in chat.meta.get("config") or [] if entry["id"] == config_id), None)
            if option is None or option["current"] == value:
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
                chat.meta["config"] = _config_options(result.get("configOptions"))

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
        chat.bump()
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
        chat.meta.setdefault("choices", {})[config_id] = value
        if chat.conn is not None and chat.conn.running and chat.session_id:
            try:
                result = await chat.conn.request("session/set_config_option", {
                    "sessionId": chat.session_id, "configId": config_id, "value": value}, timeout=60)
            except AcpError as exc:
                raise ChatError(_cut(f"The agent did not take it: {exc}", 300), "agent_failed", 409) from exc
            if "configOptions" in result:
                chat.meta["config"] = _config_options(result.get("configOptions"))
            else:
                option["current"] = value
        else:
            option["current"] = value  # set again when the agent starts
        chat.bump()
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
                chat.bump()
        elif kind == "config_option_update":
            chat.meta["config"] = _config_options(update.get("configOptions"))
            chat.bump()
        elif kind == "current_mode_update":
            for option in chat.meta.get("config") or []:
                if option["category"] == "mode":
                    option["current"] = str(update.get("currentModeId") or option["current"])
            chat.bump()
        elif kind == "usage_update":
            chat.meta["context"] = {"used": int(update.get("used") or 0), "size": int(update.get("size") or 0)}
            chat.bump()
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
