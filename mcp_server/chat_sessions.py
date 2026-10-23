"""Mac MCP Chat: the owner's own conversations with a coding agent in a project folder.

Each chat runs its agent (GitHub Copilot today) as an Agent Client Protocol subprocess with Mac MCP as its
only MCP server, reached with a scoped credential. The agent's own permission prompts come to the owner in
the chat window. Chats live in <state>/chats/<chat_id>/ (chat.json + transcript.json); the agent keeps its
own session in a private home, so a chat reopened later continues where it stopped (session/load).

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
from .agent_timeline import _cut, _inputs, _readable_output
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

AGENTS: Dict[str, Dict[str, Any]] = {
    "copilot": {
        "label": "GitHub Copilot",
        # Copilot's own subagents and workflows are left out, as for delegated agents; Mac MCP is the only MCP.
        # Copilot sends its own status lines ("Info: Disabled tools: …") as whole message chunks.
        "status_line": re.compile(r"^(Info|Warning): "),
        "args": ["--acp", "--no-auto-update", "--disable-builtin-mcps", "--disable-client-telemetry",
                 "--secret-env-vars=COPILOT_PROVIDER_API_KEY,COPILOT_GITHUB_TOKEN",
                 "--excluded-tools", *harness.COPILOT_EXCLUDED_TOOLS],
    },
}

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


def _binary(agent: str) -> Optional[str]:
    for candidate in harness.binary_candidates(agent):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _keychain_lookup(name: str) -> Optional[str]:
    from .tools_agents import _harness_keychain_value  # the server's one place for the OpenRouter key
    return _harness_keychain_value(name)


def _mcp_endpoint() -> str:
    from .tools_agents import _agent_mcp_endpoint
    return _agent_mcp_endpoint()


def _agent_env(agent: str, model: str) -> Dict[str, str]:
    """What the agent process sees: no server secrets, only its private home and its model credentials."""
    home = Path.home()
    user = os.getenv("USER") or home.name
    path_parts = [os.getenv("PATH", ""), str(home / ".npm-global" / "bin"), str(home / ".local" / "bin"),
                  "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        "HOME": str(home), "USER": user, "LOGNAME": user, "LANG": "en_US.UTF-8",
        "PATH": ":".join(part for part in path_parts if part), "TMPDIR": os.getenv("TMPDIR", "/tmp"),
        "NO_COLOR": "1", "TERM": "dumb",
    }
    if agent == "copilot":
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
        binary = _binary(agent)
        signed_in, reason = harness.sign_in_state(agent, _keychain_lookup) if binary else (False, "")
        out.append({
            "id": agent, "label": spec["label"], "ready": bool(binary and signed_in),
            "reason": "" if binary and signed_in else (reason or f"{spec['label']} is not installed."),
            "default_model": str(provider_setting(agent, "model") or "").strip(),
        })
    return out


def _last_log_line(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 4096))
            lines = [line.strip() for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _friendly_title(item: Dict[str, Any], fallback: str) -> str:
    """File steps by the file's name ("Read notes.md"); other steps keep the agent's own sentence."""
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
        meta = {key: value for key, value in self.meta.items() if key not in ("acp_session_id",)}
        meta["running"] = self.turn is not None and not self.turn.done()
        meta["agent_alive"] = self.conn is not None and self.conn.running
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

    async def delete(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if chat.turn is not None and not chat.turn.done():
            await self.cancel(chat_id)
        await self._stop_agent(chat)
        self._chats.pop(chat.meta["id"], None)
        shutil.rmtree(chat.dir, ignore_errors=True)
        return {"ok": True}

    async def shutdown(self) -> None:
        for chat in list(self._chats.values()):
            if chat.turn is not None and not chat.turn.done():
                chat.turn.cancel()
            await self._stop_agent(chat)
            if chat.save_pending:
                chat.save()

    # ── one turn ────────────────────────────────────────────────────────────

    async def _run_turn(self, chat: Chat, text: str) -> None:
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
            chat.meta["status"] = "idle"
            chat.bump()
            chat.save()
            if chat.conn is not None and chat.conn.running:
                loop = asyncio.get_running_loop()
                chat.idle = loop.call_later(IDLE_STOP_S, lambda: asyncio.ensure_future(self._stop_agent(chat)))

    async def _ensure_session(self, chat: Chat) -> None:
        async with chat.start_lock:
            if chat.conn is not None and chat.conn.running and chat.session_id:
                return
            await self._stop_agent(chat)
            agent = chat.meta["agent"]
            binary = _binary(agent)
            if not binary:
                raise ChatError(f"{AGENTS[agent]['label']} is not installed.", "agent_missing")
            project = chat.meta["project"]
            if not Path(project).is_dir():
                raise ChatError("The project folder is gone.", "bad_project")
            token, credential_id = get_scoped_credential_store().issue(
                agent_id=f"chat:{chat.meta['id']}", team_id=None, profile="trusted",
                scope=ResourceScope(path_roots=(project,)), ttl_s=CREDENTIAL_TTL_S,
            )
            chat.meta["credential_id"] = credential_id
            conn = AcpConnection(lambda method, params: self._on_update(chat, method, params),
                                 lambda method, params: self._on_request(chat, method, params))
            await conn.start([binary, *AGENTS[agent]["args"]], cwd=project, env=_agent_env(agent, chat.meta["model"]),
                             stderr_path=chat.dir / "agent.log")
            chat.conn = conn
            init = await conn.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "mac-mcp-chat", "title": "Mac MCP Chat", "version": __version__},
            }, timeout=60)
            servers = [{"type": "http", "name": "mac-mcp", "url": _mcp_endpoint(),
                        "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
            previous = chat.meta.get("acp_session_id")
            can_load = bool((init.get("agentCapabilities") or {}).get("loadSession"))
            if previous and can_load:
                chat.replaying = True  # the agent replays the conversation; the transcript already has it
                try:
                    await conn.request("session/load", {"sessionId": previous, "cwd": project, "mcpServers": servers},
                                       timeout=120)
                    chat.session_id = previous
                except AcpError:
                    chat.add("notice", text="The earlier conversation could not be reopened; the agent starts fresh.",
                             tone="warning")
                finally:
                    chat.replaying = False
            if not chat.session_id:
                created = await conn.request("session/new", {"cwd": project, "mcpServers": servers}, timeout=120)
                chat.session_id = str(created.get("sessionId") or "")
                if not chat.session_id:
                    raise ChatError("The agent did not open a session.", "agent_failed")
                chat.meta["acp_session_id"] = chat.session_id

    async def _stop_agent(self, chat: Chat) -> None:
        if chat.idle is not None:
            chat.idle.cancel()
            chat.idle = None
        conn, chat.conn, chat.session_id = chat.conn, None, None
        if conn is not None:
            await conn.close()
        get_scoped_credential_store().revoke_agent(f"chat:{chat.meta['id']}")

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
        elif kind == "usage_update":
            chat.meta["context"] = {"used": int(update.get("used") or 0), "size": int(update.get("size") or 0)}
            chat.bump()
        else:
            return
        self._schedule_save(chat)

    def _tool(self, chat: Chat, update: Dict[str, Any], new: bool) -> None:
        call_id = str(update.get("toolCallId") or "")
        item = chat.tools.get(call_id)
        if item is None:
            item = chat.add("tool", call_id=call_id, title="Working", kind="other", status="running",
                            input={}, files=[], output="")
            chat.tools[call_id] = item
        if update.get("title"):
            item["agent_title"] = _cut(str(update["title"]), 300)
        if update.get("kind"):
            item["kind"] = str(update["kind"])
        if update.get("status"):
            item["status"] = _TOOL_STATUS.get(str(update["status"]), item["status"])
        if isinstance(update.get("rawInput"), dict):
            item["input"] = _inputs(update["rawInput"])
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

    async def _on_request(self, chat: Chat, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method != "session/request_permission":
            raise AcpError("Mac MCP Chat does not offer that.", -32601)
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = [{"id": str(option.get("optionId") or ""), "label": str(option.get("name") or ""),
                    "kind": str(option.get("kind") or "")}
                   for option in params.get("options") or [] if isinstance(option, dict) and option.get("optionId")]
        tool = chat.tools.get(str(call.get("toolCallId") or ""))
        title = str(call.get("title") or (tool or {}).get("title") or "The agent wants to continue")
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
