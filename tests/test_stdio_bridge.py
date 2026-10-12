from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from mcp_server.stdio_bridge import INTERRUPTED, KEY_REJECTED, NOT_RUNNING, Bridge

URL = "http://127.0.0.1:8765/mcp"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class FakeServer:
    """A Streamable HTTP server that can forget its sessions (a restart) or be down."""

    def __init__(self) -> None:
        self.sessions = 0
        self.live: set[str] = set()
        self.seen: list[tuple[str, dict]] = []
        self.down = 0
        self.key = "k"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            self.down -= 1
            raise httpx.ConnectError("refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401)
        if request.method in ("GET", "DELETE"):
            return httpx.Response(405)
        message = json.loads(request.content)
        session = request.headers.get("mcp-session-id")
        self.seen.append((session or "", message))
        if message.get("method") == "initialize":
            self.sessions += 1
            sid = f"s{self.sessions}"
            self.live.add(sid)
            body = {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-06-18"}}
            return httpx.Response(200, json=body, headers={"mcp-session-id": sid})
        if session not in self.live:
            return httpx.Response(404)
        if "id" not in message:
            return httpx.Response(202)
        sse = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {'ok': session}})}\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeServer()
        self.out: list[dict] = []

    def bridge(self, key: str = "k") -> Bridge:
        async def write(message: dict) -> None:
            self.out.append(message)

        client = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        return Bridge(URL, key, write, client=client, wait_s=0.2, retry_delay_s=0.01)

    def run_messages(self, bridge: Bridge, *messages: dict, between=None) -> None:
        async def go() -> None:
            for message in messages:
                await bridge.handle(message)
                if between:
                    between()
            await bridge.close()

        asyncio.run(go())

    def test_forwards_requests_and_relays_sse_replies(self) -> None:
        self.run_messages(self.bridge(), INIT, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([1, 2], [m["id"] for m in self.out])
        self.assertEqual({"ok": "s1"}, self.out[1]["result"])

    def test_a_restarted_server_gets_the_session_replayed(self) -> None:
        bridge = self.bridge()
        self.run_messages(bridge, INIT, INITIALIZED)
        self.server.live.clear()  # restart: every session is gone
        self.run_messages(self.bridge_with_state(bridge), {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual({"ok": "s2"}, self.out[-1]["result"])
        replayed = [m.get("method") for _, m in self.server.seen[2:]]
        self.assertEqual(["tools/list", "initialize", "notifications/initialized", "tools/list"], replayed)
        self.assertEqual(2, len([m for m in self.out if "result" in m]))  # the replayed init is not relayed

    def bridge_with_state(self, old: Bridge) -> Bridge:
        new = self.bridge()
        new.session_id, new.protocol_version = old.session_id, old.protocol_version
        new.initialize, new.initialized = old.initialize, old.initialized
        return new

    def test_waits_for_a_server_that_is_starting(self) -> None:
        self.server.down = 3
        self.run_messages(self.bridge(), INIT)
        self.assertEqual("2025-06-18", self.out[0]["result"]["protocolVersion"])

    def test_a_server_that_stays_down_answers_with_an_error(self) -> None:
        self.server.down = 10_000
        self.run_messages(self.bridge(), INIT)
        self.assertEqual(NOT_RUNNING, self.out[0]["error"]["message"])
        self.assertEqual(1, self.out[0]["id"])

    def test_a_wrong_key_is_explained(self) -> None:
        self.run_messages(self.bridge(key="wrong"), INIT)
        self.assertEqual(KEY_REJECTED, self.out[0]["error"]["message"])

    def test_a_request_cut_off_mid_reply_is_never_resent(self) -> None:
        bridge = self.bridge()
        self.run_messages(bridge, INIT, INITIALIZED)
        calls = []

        def broken(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadError("connection reset", request=request)

        async def write(message: dict) -> None:
            self.out.append(message)

        cut = Bridge(URL, "k", write, client=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
                     wait_s=0.2, retry_delay_s=0.01)
        cut.session_id = bridge.session_id
        asyncio.run(cut.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call"}))
        self.assertEqual(1, len(calls))
        self.assertEqual(INTERRUPTED, self.out[-1]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
