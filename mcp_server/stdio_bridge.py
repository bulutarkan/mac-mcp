"""`mac-mcp stdio`: Mac MCP as a stdio MCP server, for AI apps set up with one click.

The AI app starts this command and speaks MCP over stdin/stdout; every message is
forwarded to the running server's Streamable HTTP endpoint with the API key added
here. The key stays in Mac MCP's own .env and never lands in the app's config.

The bridge also hides what an HTTP client would otherwise see: when the server
restarts (after a permission grant, an update) the session is replayed on the new
server, and while it is down a request waits briefly, then fails with a clear error.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

PROTOCOL_HEADER = "mcp-protocol-version"
SESSION_HEADER = "mcp-session-id"
# How long a request waits for a server that is starting or restarting.
DEFAULT_WAIT_S = 15.0
NOT_RUNNING = "Mac MCP is not running. Start it from the Mac MCP menu at the top of your screen."
KEY_REJECTED = "Mac MCP rejected this connection. Connect this app again from Mac MCP's setup."
INTERRUPTED = "Mac MCP stopped while working on this request. Check the result before trying again."


class ServerUnavailable(Exception):
    pass


class Bridge:
    def __init__(
        self,
        url: str,
        api_key: str | None,
        write: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        client: httpx.AsyncClient | None = None,
        wait_s: float = DEFAULT_WAIT_S,
        retry_delay_s: float = 0.5,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.write = write
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        self.wait_s = wait_s
        self.retry_delay_s = retry_delay_s
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.initialize: dict[str, Any] | None = None
        self.initialized = False
        self._session_lock = asyncio.Lock()
        self._listener: asyncio.Task[None] | None = None

    def headers(self) -> dict[str, str]:
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if self.protocol_version:
            headers[PROTOCOL_HEADER] = self.protocol_version
        return headers

    async def handle(self, message: dict[str, Any]) -> None:
        """Forward one message from the AI app; a failed request gets a JSON-RPC error back."""
        try:
            await self._forward(message)
        except ServerUnavailable as exc:
            if "id" in message and "method" in message:
                await self.write({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32000, "message": str(exc)}})
            else:
                print(f"mac-mcp stdio: {exc}", file=sys.stderr)

    async def _forward(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "initialize":
            self.initialize = message
            self.session_id = None
            status = await self._post(message, relay=True, capture=True)
        elif method == "notifications/initialized":
            self.initialized = True
            status = await self._post(message, relay=True)
            self._listen()
        else:
            sent_with = self.session_id
            status = await self._post(message, relay=True)
            if status == 404 and sent_with and self.initialize:
                # The server restarted and forgot the session, so it never saw this
                # message: replay the session, then send it again.
                await self._renew(sent_with)
                status = await self._post(message, relay=True)
        if status >= 400:
            raise ServerUnavailable(f"Mac MCP could not handle this message (HTTP {status}).")

    async def _renew(self, stale: str | None) -> None:
        async with self._session_lock:
            if self.session_id != stale:
                return  # another request already renewed it
            self.session_id = None
            await self._post(self.initialize, relay=False, capture=True)
            if self.initialized:
                await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, relay=False)
                self._listen()

    async def _post(self, message: dict[str, Any] | None, *, relay: bool, capture: bool = False) -> int:
        deadline = asyncio.get_running_loop().time() + self.wait_s
        while True:
            try:
                async with self.client.stream("POST", self.url, headers=self.headers(),
                                              content=json.dumps(message)) as response:
                    if response.status_code == 401:
                        raise ServerUnavailable(KEY_REJECTED)
                    if capture and response.headers.get(SESSION_HEADER):
                        self.session_id = response.headers[SESSION_HEADER]
                    if response.status_code >= 400:
                        await response.aread()
                        return response.status_code
                    try:
                        async for reply in _messages(response):
                            if capture and isinstance(reply.get("result"), dict):
                                self.protocol_version = reply["result"].get("protocolVersion") or self.protocol_version
                            if relay:
                                await self.write(reply)
                    except httpx.TransportError:
                        # The server got the message and may have acted on it: never resend.
                        raise ServerUnavailable(INTERRUPTED) from None
                    return response.status_code
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Nothing reached the server, so waiting for it and sending again is safe.
                if asyncio.get_running_loop().time() >= deadline:
                    raise ServerUnavailable(NOT_RUNNING) from None
                await asyncio.sleep(self.retry_delay_s)
            except httpx.TransportError:
                raise ServerUnavailable(INTERRUPTED) from None

    def _listen(self) -> None:
        if self._listener is None or self._listener.done():
            self._listener = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        """Messages the server sends on its own (list changes, logs) arrive on a GET stream."""
        while True:
            try:
                headers = {k: v for k, v in self.headers().items() if k != "content-type"}
                headers["accept"] = "text/event-stream"
                async with self.client.stream("GET", self.url, headers=headers) as response:
                    if response.status_code in (400, 404, 405):
                        # No standalone stream, or the session is gone (a restart): the next
                        # request renews the session and starts listening again.
                        return
                    if response.status_code == 200:
                        async for reply in _messages(response):
                            await self.write(reply)
            except httpx.TransportError:
                pass
            await asyncio.sleep(2.0)

    async def close(self) -> None:
        if self._listener:
            self._listener.cancel()
        if self.session_id:
            try:
                await self.client.delete(self.url, headers=self.headers(), timeout=2.0)
            except httpx.HTTPError:
                pass
        await self.client.aclose()


async def _messages(response: httpx.Response):
    """The JSON-RPC messages in a response: a JSON body (one or a batch) or an SSE stream."""
    kind = response.headers.get("content-type", "")
    if kind.startswith("text/event-stream"):
        data: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                yield json.loads("\n".join(data))
                data = []
        if data:
            yield json.loads("\n".join(data))
        return
    body = (await response.aread()).strip()
    if not body or not kind.startswith("application/json"):
        return
    parsed = json.loads(body)
    for item in parsed if isinstance(parsed, list) else [parsed]:
        yield item


async def serve(url: str, api_key: str | None) -> int:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout.buffer
    out_lock = asyncio.Lock()

    async def write(message: dict[str, Any]) -> None:
        async with out_lock:
            out.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            out.flush()

    bridge = Bridge(url, api_key, write)
    pending: set[asyncio.Task[None]] = set()
    try:
        while line := await reader.readline():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print("mac-mcp stdio: ignored a line that is not JSON", file=sys.stderr)
                continue
            if message.get("method") in ("initialize", "notifications/initialized"):
                await bridge.handle(message)  # in order: everything else needs the session
                continue
            task = asyncio.create_task(bridge.handle(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.wait(pending, timeout=5.0)
    finally:
        await bridge.close()
    return 0


def run(port: int, api_key: str | None) -> int:
    try:
        return asyncio.run(serve(f"http://127.0.0.1:{port}/mcp", api_key))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover - `mac-mcp stdio` is the entry point
    sys.exit(run(int(os.getenv("MAC_MCP_PORT", "8765")), os.getenv("MCP_API_KEY")))
