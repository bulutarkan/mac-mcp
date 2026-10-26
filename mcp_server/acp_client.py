"""A small Agent Client Protocol (ACP) client: JSON-RPC 2.0 over an agent's stdio, one JSON object per line.

The agent (Copilot `--acp`, claude-agent-acp, codex-acp, …) is the server; Mac MCP is the client. The agent
sends notifications (session/update) and requests of its own (session/request_permission), which go to the
handlers given here. See https://agentclientprotocol.com/protocol/overview.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

PROTOCOL_VERSION = 1
log = logging.getLogger(__name__)
# Per connection (#119): one message may be this long (a tool result can be one long line); a longer one
# is skipped and the stream goes on. Requests from the agent beyond the open limit are refused at once,
# without a handler, so a flood never reaches the owner as prompts.
_LINE_LIMIT = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_MAX_OPEN_REQUESTS = 32

NotificationHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class AcpError(Exception):
    def __init__(self, message: str, code: int = -32603, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class AcpClosed(AcpError):
    pass


class AcpConnection:
    def __init__(self, on_notification: NotificationHandler, on_request: RequestHandler) -> None:
        self._on_notification = on_notification
        self._on_request = on_request
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._write_lock = asyncio.Lock()
        self.closed = asyncio.Event()

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self.closed.is_set()

    async def start(self, cmd: List[str], *, cwd: str, env: Dict[str, str], stderr_path: Optional[Path]) -> None:
        """stderr_path None: the agent's own messages are not kept (a private-mode chat)."""
        if stderr_path is None:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_READ_CHUNK, start_new_session=True,
            )
        else:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            with stderr_path.open("ab") as stderr:
                os.chmod(stderr_path, 0o600)
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=stderr, limit=_READ_CHUNK, start_new_session=True,
                )
        self._reader = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self.running:
            raise AcpClosed("The agent is not running.")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout) if timeout else await future
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        if self.running:
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self, grace_s: float = 3.0) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), grace_s)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), grace_s)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._reader, grace_s)
        self._fail_pending(AcpClosed("The agent stopped."))
        self.closed.set()

    async def _send(self, message: Dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, AttributeError) as exc:
                raise AcpClosed("The agent stopped.") from exc

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def _read_loop(self) -> None:
        line = bytearray()
        skipping = False  # inside a line over _LINE_LIMIT: dropped up to its newline
        try:
            while True:
                chunk = await self._proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                start = 0
                while True:
                    newline = chunk.find(b"\n", start)
                    if newline < 0:
                        if not skipping:
                            line += chunk[start:]
                            if len(line) > _LINE_LIMIT:
                                line.clear()
                                skipping = True
                        break
                    if not skipping:
                        line += chunk[start:newline]
                        if len(line) <= _LINE_LIMIT:
                            await self._handle_line(bytes(line))
                    line.clear()
                    skipping = False
                    start = newline + 1
            if line and not skipping:
                await self._handle_line(bytes(line))
        finally:
            self._fail_pending(AcpClosed("The agent stopped."))
            self.closed.set()

    async def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return  # agents may log a stray non-JSON line; never fatal
        if isinstance(message, dict):
            await self._dispatch(message)

    async def _dispatch(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
            if future is None or future.done():
                return
            if "error" in message:
                error = message.get("error") or {}
                future.set_exception(AcpError(str(error.get("message") or "Agent error"),
                                              int(error.get("code") or -32603), error.get("data")))
            else:
                result = message.get("result")
                future.set_result(result if isinstance(result, dict) else {})
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" in message:
            if len(self._tasks) >= _MAX_OPEN_REQUESTS:
                with contextlib.suppress(AcpClosed):
                    await self._send({"jsonrpc": "2.0", "id": message["id"],
                                      "error": {"code": -32000, "message": "Too many open requests."}})
                return
            # A request (a permission prompt) may wait for the person, so it runs on its own.
            self._spawn(self._answer(message["id"], str(method), params))
        else:
            # Updates are handled in order: message chunks must not overtake each other.
            try:
                await self._on_notification(str(method), params)
            except Exception:  # a handler bug must not stop the stream
                log.exception("ACP notification %s failed", method)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, rid: Any, method: str, params: Dict[str, Any]) -> None:
        try:
            result = await self._on_request(method, params)
            reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except AcpError as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:  # a handler bug must not leave the agent waiting forever
            log.exception("ACP request %s failed", method)
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": type(exc).__name__}}
        with contextlib.suppress(AcpClosed):
            await self._send(reply)
