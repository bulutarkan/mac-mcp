"""The owner's own MCP servers, offered to agents as Mac MCP tools.

Mac MCP is an MCP server, and here it is also an MCP client: the servers listed in
~/.mac-mcp/mcp_servers.json (the same "mcpServers" shape Claude Desktop, Cursor and
Claude Code use) are connected once, and each of their tools is registered as a
Mac MCP tool named ``<server>__<tool>``. An agent connected to Mac MCP (ChatGPT, a
delegated subagent, a model driven through the API) finds and calls them like any
other tool, so the profile, scope, telemetry, idempotency and Tool Activity rules of
Mac MCP apply to every call before it is forwarded.

Connections live on one background event loop owned by this module, so a call can
come from the server loop, a worker thread or the CLI. The config file is re-read
when it changes; adding or removing a server in Settings or with ``mac-mcp mcp``
takes effect without a restart. Header and env values (API keys) stay in that
owner-only file and are never returned by status reads.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths
from .private_storage import PRIVATE_FILE_MODE, ensure_private_dir

LOG = logging.getLogger(__name__)

CONFIG_NAME = "mcp_servers.json"
FAMILY = "external_mcp"
SEPARATOR = "__"
MAX_TOOL_NAME = 64
SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
TRANSPORTS = ("stdio", "http", "sse")
CONNECT_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 2.0
# Binaries an MCP server is usually started with live here even when launchd gives a short PATH.
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"),
               str(Path.home() / ".npm-global" / "bin"), str(Path.home() / ".bun" / "bin"))


class ConfigError(ValueError):
    """A server entry or the config file is not usable."""


class SignInRequired(RuntimeError):
    """A catalog service's saved sign-in is missing or expired; the owner signs in again in Settings."""


# How a catalog service's credential is read (headers, httpx auth). mcp_connectors registers it
# when it loads, so this lower layer never imports it.
CredentialProvider = Callable[["ServerConfig"], Tuple[Dict[str, str], Any]]
_CREDENTIALS: Optional[CredentialProvider] = None


def register_credentials(provider: Optional[CredentialProvider]) -> None:
    global _CREDENTIALS
    _CREDENTIALS = provider


def config_path() -> Path:
    return paths.state_dir() / CONFIG_NAME


def logs_dir() -> Path:
    return paths.state_dir() / "external-mcp"


# ---------------------------------------------------------------------------
# Config

@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: str
    command: Optional[str] = None
    args: Tuple[str, ...] = ()
    env: Tuple[Tuple[str, str], ...] = ()
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Tuple[Tuple[str, str], ...] = ()
    enabled: bool = True
    listed: bool = True
    timeout_s: float = CALL_TIMEOUT_S
    # A service connected from the catalog (mcp_connectors.py): its credential is in the Keychain.
    catalog: Optional[str] = None
    auth: Optional[str] = None  # token | oauth

    def public(self) -> Dict[str, Any]:
        """What Settings and status reads may show: no header or env values."""
        return {
            "name": self.name, "transport": self.transport, "enabled": self.enabled, "listed": self.listed,
            "command": self.command, "args": list(self.args), "cwd": self.cwd, "url": _redact_url(self.url),
            "env_keys": [key for key, _ in self.env], "header_keys": [key for key, _ in self.headers],
            "timeout_s": self.timeout_s, "catalog": self.catalog, "auth": self.auth,
        }


def _redact_url(url: Optional[str]) -> Optional[str]:
    """Hide query values (hosted servers often take the API key there)."""
    if not url or "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts = [f"{item.split('=', 1)[0]}=***" if "=" in item else item for item in query.split("&")]
    return base + "?" + "&".join(parts)


def _string_map(value: Any, label: str, name: str) -> Tuple[Tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: {label} must be an object of strings")
    return tuple((str(key), str(item)) for key, item in value.items())


def parse_server(name: str, raw: Any) -> ServerConfig:
    if not SERVER_NAME_RE.match(str(name or "")):
        raise ConfigError(
            f"{name!r}: server names use lowercase letters, digits, '-' or '_' (max 24) so tool names stay short"
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: each server is an object")
    transport = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    if transport in {"streamable-http", "streamable_http", "streamablehttp"}:
        transport = "http"
    if not transport:
        transport = "stdio" if raw.get("command") else "http" if raw.get("url") else ""
    if transport not in TRANSPORTS:
        raise ConfigError(f"{name}: set either command (a local server) or url (a remote server)")
    try:
        timeout_s = float(raw.get("timeout_s") or CALL_TIMEOUT_S)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: timeout_s must be a number") from exc
    common = {
        "name": name, "transport": transport,
        "enabled": raw.get("enabled", True) is not False and raw.get("disabled") is not True,
        "listed": raw.get("listed", True) is not False,
        "timeout_s": max(1.0, min(timeout_s, 900.0)),
    }
    if transport == "stdio":
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ConfigError(f"{name}: a local server needs a command")
        args = raw.get("args") or []
        if not isinstance(args, list):
            raise ConfigError(f"{name}: args must be a list")
        cwd = raw.get("cwd")
        return ServerConfig(command=command, args=tuple(str(item) for item in args),
                            env=_string_map(raw.get("env"), "env", name),
                            cwd=str(Path(str(cwd)).expanduser()) if cwd else None, **common)
    url = str(raw.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ConfigError(f"{name}: url must start with https:// (or http:// for a local server)")
    auth = str(raw.get("auth") or "").strip().lower() or None
    if auth not in (None, "token", "oauth"):
        raise ConfigError(f"{name}: auth is token or oauth (a service connected in Settings)")
    catalog = str(raw.get("catalog") or "").strip() or None
    return ServerConfig(url=url, headers=_string_map(raw.get("headers"), "headers", name), catalog=catalog,
                        auth=auth, **common)


def read_raw_config(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"mcpServers": {}}
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a JSON object")
    servers = data.get("mcpServers")
    if servers is None:
        data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise ConfigError(f"{path.name}: mcpServers must be an object")
    return data


def load_servers(path: Optional[Path] = None) -> Tuple[Dict[str, ServerConfig], Dict[str, str]]:
    """Valid servers by name, and the error for each entry that is not usable."""
    servers: Dict[str, ServerConfig] = {}
    errors: Dict[str, str] = {}
    for name, raw in read_raw_config(path)["mcpServers"].items():
        try:
            servers[name] = parse_server(name, raw)
        except ConfigError as exc:
            errors[str(name)] = str(exc)
    return servers, errors


def write_raw_config(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomic owner-only write; keeps unknown top-level keys."""
    path = path or config_path()
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    os.chmod(path, PRIVATE_FILE_MODE)


def upsert_server(name: str, raw: Dict[str, Any], path: Optional[Path] = None) -> ServerConfig:
    """Add or replace one server (validated first); other servers are untouched."""
    server = parse_server(name, raw)
    data = read_raw_config(path)
    data["mcpServers"][name] = raw
    write_raw_config(data, path)
    return server


def set_server_fields(name: str, path: Optional[Path] = None, **fields: Any) -> ServerConfig:
    data = read_raw_config(path)
    raw = data["mcpServers"].get(name)
    if not isinstance(raw, dict):
        raise ConfigError(f"no MCP server named {name!r}")
    raw = dict(raw)
    if "enabled" in fields:
        raw.pop("disabled", None)
    raw.update(fields)
    server = parse_server(name, raw)
    data["mcpServers"][name] = raw
    write_raw_config(data, path)
    return server


def remove_server(name: str, path: Optional[Path] = None) -> bool:
    data = read_raw_config(path)
    if name not in data["mcpServers"]:
        return False
    del data["mcpServers"][name]
    write_raw_config(data, path)
    return True


def import_servers(blob: Any, path: Optional[Path] = None) -> List[str]:
    """Merge a pasted Claude Desktop / Cursor style {"mcpServers": {...}} object (or a bare map)."""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"not valid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise ConfigError("paste an object like {\"mcpServers\": {\"name\": {...}}}")
    servers = blob.get("mcpServers", blob)
    if not isinstance(servers, dict) or not servers:
        raise ConfigError("no servers found in the pasted JSON")
    for name, raw in servers.items():
        parse_server(name, raw)  # all or nothing
    data = read_raw_config(path)
    data["mcpServers"].update(servers)
    write_raw_config(data, path)
    return list(servers)


# ---------------------------------------------------------------------------
# Tool names and results

def tool_name(server: str, tool: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(tool))
    name = f"{server}{SEPARATOR}{safe}"
    if len(name) > MAX_TOOL_NAME:
        # Stable across processes (the CLI and the server must agree on the name).
        digest = hashlib.sha256(str(tool).encode("utf-8")).hexdigest()[:4]
        name = name[: MAX_TOOL_NAME - 5] + "_" + digest
    return name


def is_external_tool(name: str) -> bool:
    return SEPARATOR in str(name or "")


def result_payload(server: str, tool: str, result: Any) -> Dict[str, Any]:
    """CallToolResult -> the JSON object Mac MCP tools return."""
    texts: List[str] = []
    other: List[Dict[str, Any]] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            texts.append(str(getattr(block, "text", "")))
        elif kind in {"image", "audio"}:
            data = str(getattr(block, "data", "") or "")
            other.append({"type": kind, "mimeType": getattr(block, "mimeType", None), "base64_chars": len(data),
                          "data": data if len(data) <= 200_000 else None})
        elif kind == "resource_link":
            other.append({"type": kind, "uri": str(getattr(block, "uri", "")), "name": getattr(block, "name", None)})
        else:
            dumped = block.model_dump(mode="json") if hasattr(block, "model_dump") else {"type": str(kind)}
            other.append(dumped)
    payload: Dict[str, Any] = {"ok": not bool(getattr(result, "isError", False)), "server": server, "tool": tool}
    if texts:
        payload["text"] = "\n\n".join(texts)
    if other:
        payload["content"] = other
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload["structured"] = structured
    return payload


def risk_for(tool: Any):
    """The central risk entry for one external tool, from its own annotations.

    A tool that says it is read-only reads over the network; anything else may change
    things elsewhere. Only an explicit destructiveHint marks it destructive, so the
    standard profile keeps the owner's own servers usable.
    """
    from .policy import Capability, RiskEntry

    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "readOnlyHint", None) is True
    capabilities = frozenset({Capability.READ, Capability.NETWORK_ACCESS} if read_only
                             else {Capability.NETWORK_ACCESS, Capability.EXTERNAL_SIDE_EFFECT})
    destructive = not read_only and getattr(annotations, "destructiveHint", None) is True
    return RiskEntry(tool="", family=FAMILY, capabilities=capabilities, destructive=destructive, sensitive=True)


# ---------------------------------------------------------------------------
# Connections

def _stdio_environment(server: ServerConfig) -> Dict[str, str]:
    from mcp.client.stdio import get_default_environment

    env = get_default_environment()
    path_parts = [part for part in (os.environ.get("PATH") or env.get("PATH") or "").split(":") if part]
    for extra in _EXTRA_PATH:
        if extra not in path_parts:
            path_parts.append(extra)
    env["PATH"] = ":".join(path_parts)
    env.update(dict(server.env))
    return env


@dataclass
class _Connection:
    config: ServerConfig
    status: str = "connecting"
    error: Optional[str] = None
    tools: List[Any] = field(default_factory=list)
    server_info: Optional[Dict[str, Any]] = None
    session: Any = None
    connected_at: Optional[float] = None
    stop: Optional[asyncio.Event] = None
    ready: Optional[asyncio.Event] = None
    task: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            **self.config.public(),
            "status": self.status if self.config.enabled else "disabled",
            "error": self.error,
            "server_info": self.server_info,
            "connected_at": self.connected_at,
            "tools": [
                {"name": tool_name(self.config.name, tool.name), "remote_name": tool.name,
                 "description": (tool.description or "")[:300],
                 "read_only": getattr(getattr(tool, "annotations", None), "readOnlyHint", None) is True}
                for tool in self.tools
            ],
        }


def _auth_for(server: ServerConfig) -> Tuple[Dict[str, str], Any]:
    """Headers and httpx auth of a remote server; a catalog service's credential comes from the Keychain."""
    if not server.auth:
        return dict(server.headers), None
    if _CREDENTIALS is None:
        raise SignInRequired(f"{server.name} is a connected service, but its sign-in cannot be read here.")
    return _CREDENTIALS(server)


async def _open_session(stack: contextlib.AsyncExitStack, server: ServerConfig,
                        message_handler: Optional[Callable[..., Any]] = None, *, log_name: Optional[str] = None,
                        auth: Any = None):
    from mcp import ClientSession, types

    if server.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = _stdio_environment(server)
        command = shutil.which(server.command, path=env["PATH"]) or server.command
        ensure_private_dir(logs_dir())
        errlog = stack.enter_context(open(logs_dir() / f"{log_name or server.name}.stderr.log", "w", encoding="utf-8"))
        params = StdioServerParameters(command=command, args=list(server.args), env=env, cwd=server.cwd)
        read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
    elif server.transport == "sse":
        from mcp.client.sse import sse_client

        headers, saved_auth = await asyncio.to_thread(_auth_for, server)
        read, write = await stack.enter_async_context(sse_client(server.url, headers=headers or None,
                                                                 auth=auth or saved_auth))
    else:
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        headers, saved_auth = await asyncio.to_thread(_auth_for, server)
        client = await stack.enter_async_context(httpx.AsyncClient(
            headers=headers, follow_redirects=True, auth=auth or saved_auth,
            timeout=httpx.Timeout(30.0, read=max(300.0, server.timeout_s)),
        ))
        read, write, _ = await stack.enter_async_context(streamable_http_client(server.url, http_client=client))
    from .version import __version__ as version

    session = await stack.enter_async_context(ClientSession(
        read, write, message_handler=message_handler,
        client_info=types.Implementation(name="mac-mcp", version=str(version)),
    ))
    init = await session.initialize()
    tools: List[Any] = []
    cursor: Optional[str] = None
    for _ in range(50):
        page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            break
    info = getattr(init, "serverInfo", None)
    server_info = {"name": getattr(info, "name", None), "version": getattr(info, "version", None),
                   "protocol": getattr(init, "protocolVersion", None)}
    return session, tools, server_info


def find_exception(exc: BaseException, kind: type) -> Optional[BaseException]:
    """The first ``kind`` inside exception groups, causes and contexts."""
    seen: set[int] = set()
    stack: List[BaseException] = [exc]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, kind):
            return item
        if isinstance(item, BaseExceptionGroup):
            stack.extend(item.exceptions)
        for linked in (item.__cause__, item.__context__):
            if linked is not None:
                stack.append(linked)
    return None


def _short_error(exc: BaseException) -> str:
    needs_sign_in = find_exception(exc, SignInRequired)
    if needs_sign_in is not None:
        return str(needs_sign_in)
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:400]


def _stderr_tail(server: ServerConfig, lines: int = 5, log_name: Optional[str] = None) -> str:
    try:
        content = (logs_dir() / f"{log_name or server.name}.stderr.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.strip().splitlines()[-lines:])[:600]


ToolsChanged = Callable[[str, ServerConfig, List[Any]], None]


class Manager:
    """Owns every connection on one background event loop."""

    def __init__(self, on_tools_changed: Optional[ToolsChanged] = None, *, path: Optional[Path] = None) -> None:
        self._on_tools_changed = on_tools_changed
        self._path = path
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connections: Dict[str, _Connection] = {}
        self._config_errors: Dict[str, str] = {}
        self._config_mtime: Optional[float] = None
        self._lock = threading.Lock()
        self._reload_lock: Optional[asyncio.Lock] = None
        self._watcher: Optional[asyncio.Task] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            self._watcher = loop.create_task(self._watch_config())
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="mac-mcp-external-mcp", daemon=True)
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._close_all(), loop)
        loop.call_soon_threadsafe(lambda: self._watcher and self._watcher.cancel())
        with contextlib.suppress(Exception):
            future.result(10)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        self._thread = None
        self._loop = None

    def _submit(self, coro) -> Future:
        if self._loop is None:
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _close_all(self) -> None:
        for name in list(self._connections):
            await self._disconnect(name)

    # -- config reconciliation -------------------------------------------------
    async def _watch_config(self) -> None:
        while True:
            try:
                await self._reload_if_changed()
            except Exception:  # keep watching; the error is shown in status
                LOG.exception("external MCP config reload failed")
            await asyncio.sleep(POLL_INTERVAL_S)

    def _path_or_default(self) -> Path:
        return self._path or config_path()

    async def _reload_if_changed(self, force: bool = False) -> None:
        # The file watcher and an explicit reload must not connect the same server twice.
        if self._reload_lock is None:
            self._reload_lock = asyncio.Lock()
        async with self._reload_lock:
            await self._reload_locked(force)

    async def _reload_locked(self, force: bool) -> None:
        path = self._path_or_default()
        try:
            mtime: Optional[float] = path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if not force and mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            servers, errors = load_servers(path)
        except ConfigError as exc:
            with self._lock:
                self._config_errors = {"*": str(exc)}
            return  # a broken file keeps the servers that are running
        with self._lock:
            self._config_errors = errors
        for name in list(self._connections):
            if name not in servers or servers[name] != self._connections[name].config:
                await self._disconnect(name)
        for name, server in servers.items():
            if name in self._connections:
                continue
            if server.enabled:
                await self._connect(server)
            else:
                with self._lock:
                    self._connections[name] = _Connection(config=server, status="disabled")

    async def _connect(self, server: ServerConfig) -> _Connection:
        connection = _Connection(config=server, stop=asyncio.Event(), ready=asyncio.Event())
        with self._lock:
            self._connections[server.name] = connection
        connection.task = asyncio.create_task(self._run(connection), name=f"external-mcp-{server.name}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(connection.ready.wait(), CONNECT_TIMEOUT_S + 5)
        return connection

    async def _run(self, connection: _Connection) -> None:
        server = connection.config
        assert connection.stop is not None and connection.ready is not None

        async def on_message(message: Any) -> None:
            root = getattr(message, "root", None)
            if getattr(root, "method", None) == "notifications/tools/list_changed" and connection.session:
                asyncio.get_running_loop().create_task(self._refresh_tools(connection))

        try:
            async with contextlib.AsyncExitStack() as stack:
                session, tools, server_info = await asyncio.wait_for(
                    _open_session(stack, server, on_message), CONNECT_TIMEOUT_S,
                )
                connection.session, connection.tools, connection.server_info = session, tools, server_info
                connection.status, connection.error, connection.connected_at = "connected", None, time.time()
                self._publish(connection)
                connection.ready.set()
                await connection.stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # anyio task groups raise ExceptionGroup
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not connection.stop.is_set():
                connection.status = "sign_in_required" if find_exception(exc, SignInRequired) else "error"
                tail = _stderr_tail(server) if server.transport == "stdio" else ""
                connection.error = _short_error(exc) + (f"\n{tail}" if tail else "")
                LOG.warning("external MCP %s failed: %s", server.name, connection.error)
        finally:
            connection.session = None
            if connection.status == "connected":
                connection.status = "disconnected"
            connection.ready.set()

    async def _refresh_tools(self, connection: _Connection) -> None:
        session = connection.session
        if session is None:
            return
        try:
            result = await session.list_tools()
        except Exception as exc:
            LOG.warning("external MCP %s tool refresh failed: %s", connection.config.name, exc)
            return
        connection.tools = list(result.tools)
        self._publish(connection)

    async def _disconnect(self, name: str) -> None:
        with self._lock:
            connection = self._connections.pop(name, None)
        if connection is None:
            return
        if connection.stop is not None:
            connection.stop.set()
        if connection.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(connection.task), 10)
            except (asyncio.TimeoutError, Exception):
                connection.task.cancel()
        if self._on_tools_changed is not None:
            self._on_tools_changed(name, connection.config, [])

    def _publish(self, connection: _Connection) -> None:
        if self._on_tools_changed is not None:
            try:
                self._on_tools_changed(connection.config.name, connection.config, list(connection.tools))
            except Exception:
                LOG.exception("registering tools of external MCP %s failed", connection.config.name)

    # -- calls -----------------------------------------------------------------
    async def _call(self, server_name: str, tool: str, arguments: Dict[str, Any]) -> Any:
        connection = self._connections.get(server_name)
        if connection is None or not connection.config.enabled:
            raise ConfigError(f"MCP server {server_name!r} is not connected (removed or turned off in Settings)")
        if connection.session is None:
            # Reconnect a dropped server before sending; a sent call is never retried.
            await self._disconnect(server_name)
            connection = await self._connect(connection.config)
            if connection.session is None:
                raise ConnectionError(f"MCP server {server_name!r} is unavailable: {connection.error or 'not connected'}")
        try:
            return await connection.session.call_tool(
                tool, arguments, read_timeout_seconds=timedelta(seconds=connection.config.timeout_s),
            )
        except Exception as exc:
            if connection.status == "connected" and not _is_tool_level_error(exc):
                connection.status, connection.error = "error", _short_error(exc)
                connection.session = None
                if connection.stop is not None:
                    connection.stop.set()
            raise

    async def call_async(self, server_name: str, tool: str, arguments: Dict[str, Any]) -> Any:
        """Call from any event loop; cancelling the caller cancels the remote call."""
        return await asyncio.wrap_future(self._submit(self._call(server_name, tool, arguments)))

    def call(self, server_name: str, tool: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        return self._submit(self._call(server_name, tool, arguments)).result(timeout)

    # -- reads -----------------------------------------------------------------
    def reload(self, timeout: float = CONNECT_TIMEOUT_S + 10) -> None:
        self._submit(self._reload_if_changed(force=True)).result(timeout)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            servers = [connection.snapshot() for connection in self._connections.values()]
            errors = dict(self._config_errors)
        return {
            "config_path": str(self._path_or_default()),
            "servers": sorted(servers, key=lambda item: item["name"]),
            "config_errors": errors,
        }


def _is_tool_level_error(exc: BaseException) -> bool:
    from mcp.shared.exceptions import McpError

    return isinstance(exc, McpError)


async def probe(server: ServerConfig, timeout: float = CONNECT_TIMEOUT_S) -> Dict[str, Any]:
    """Connect once, list the tools and disconnect (Settings' Test button, `mac-mcp mcp test`)."""
    started = time.monotonic()
    try:
        async with contextlib.AsyncExitStack() as stack:
            # A test connection keeps its own log so it never truncates the live server's.
            _, tools, server_info = await asyncio.wait_for(
                _open_session(stack, server, log_name=f"{server.name}.test"), timeout,
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        tail = _stderr_tail(server, log_name=f"{server.name}.test") if server.transport == "stdio" else ""
        return {"ok": False, "name": server.name, "error": _short_error(exc) + (f"\n{tail}" if tail else ""),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    return {
        "ok": True, "name": server.name, "server_info": server_info,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "tools": [{"name": tool_name(server.name, tool.name), "remote_name": tool.name,
                   "description": (tool.description or "")[:300]} for tool in tools],
    }


# ---------------------------------------------------------------------------
# Registration into the Mac MCP server

_MANAGER: Optional[Manager] = None
_LISTED: set[str] = set()


def manager() -> Optional[Manager]:
    return _MANAGER


def listed_tool_names() -> frozenset[str]:
    """External tools the owner chose to show in the default (core) tool list."""
    return frozenset(_LISTED)


def _proxy_tool(mcp: Any, manager_: Manager, server: ServerConfig, remote: Any) -> Any:
    from mcp.server.fastmcp.tools.base import Tool
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
    from mcp.types import ToolAnnotations
    from pydantic import ConfigDict

    from .policy import annotations_for_tool

    class PassThrough(ArgModelBase):
        # Every argument goes to the remote tool as given; it validates its own schema.
        model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

        def model_dump_one_level(self) -> Dict[str, Any]:
            return {"arguments": dict(self.model_extra or {})}

    remote_name = str(remote.name)

    async def call(arguments: Dict[str, Any]) -> Dict[str, Any]:
        from mcp.server.fastmcp.exceptions import ToolError

        result = await manager_.call_async(server.name, remote_name, arguments)
        payload = result_payload(server.name, remote_name, result)
        if not payload["ok"]:
            raise ToolError(f"{server.name} {remote_name} failed: {payload.get('text') or 'tool error'}"[:4000])
        return payload

    name = tool_name(server.name, remote_name)
    schema = remote.inputSchema if isinstance(getattr(remote, "inputSchema", None), dict) else {}
    schema = {"type": "object", "properties": {}, **schema}
    central = annotations_for_tool(name)
    title = getattr(remote, "title", None) or getattr(getattr(remote, "annotations", None), "title", None)
    description = (remote.description or remote_name).strip()
    return Tool(
        fn=call, name=name, title=title or f"{server.name}: {remote_name}",
        description=f"[{server.name} MCP server] {description}",
        parameters=schema, fn_metadata=FuncMetadata(arg_model=PassThrough), is_async=True,
        annotations=ToolAnnotations(
            readOnlyHint=central.readOnlyHint, destructiveHint=central.destructiveHint,
            idempotentHint=getattr(getattr(remote, "annotations", None), "idempotentHint", None),
            openWorldHint=True,
        ),
        meta={"mac_mcp/external_server": server.name, "mac_mcp/remote_tool": remote_name},
    )


def attach(mcp: Any, *, path: Optional[Path] = None) -> Manager:
    """Create the manager whose tool lists are registered on ``mcp`` (call start() to connect)."""
    global _MANAGER
    from .policy import RISK_REGISTRY

    registered: Dict[str, List[str]] = {}
    lock = threading.Lock()

    def on_tools_changed(server_name: str, server: ServerConfig, tools: List[Any]) -> None:
        tool_manager = mcp._tool_manager
        with lock:
            # Replace the registry dict in one assignment so readers on other threads never
            # see it change size while they iterate.
            current = dict(tool_manager._tools)
            previous = registered.pop(server_name, [])
            names: List[str] = []
            for remote in tools:
                name = tool_name(server_name, remote.name)
                if name in current and name not in previous:
                    LOG.warning("external MCP tool %s skipped: the name is already taken", name)
                    continue
                entry = risk_for(remote)
                RISK_REGISTRY[name] = type(entry)(
                    tool=name, family=entry.family, capabilities=entry.capabilities,
                    destructive=entry.destructive, sensitive=entry.sensitive,
                )
                current[name] = _proxy_tool(mcp, manager_, server, remote)
                names.append(name)
                if server.listed:
                    _LISTED.add(name)
                else:
                    _LISTED.discard(name)
            # New entries are in place before stale ones go, so a refresh never drops a live tool.
            for old in previous:
                if old in names:
                    continue
                if (getattr(current.get(old), "meta", None) or {}).get("mac_mcp/external_server") == server_name:
                    current.pop(old, None)
                RISK_REGISTRY.pop(old, None)
                _LISTED.discard(old)
            registered[server_name] = names
            tool_manager._tools = current

    manager_ = Manager(on_tools_changed, path=path)
    _MANAGER = manager_
    return manager_
