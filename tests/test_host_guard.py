from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mcp_server.host_guard import HostGuard


async def ok(request):
    return PlainTextResponse("ok")


async def ws(websocket):
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


def guarded(*, enforce_hosts=True, no_auth=False, public=("mac.example.com",), extra=()):
    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"]), WebSocketRoute("/ws", ws)])
    app.add_middleware(HostGuard, enforce_hosts=enforce_hosts, no_auth=no_auth,
                       public_hosts=lambda: list(public), extra_hosts=list(extra))
    return app


class HostGuardTests(unittest.TestCase):
    def get(self, app, host, **headers):
        return TestClient(app, base_url="http://127.0.0.1").get("/mcp", headers={"host": host, **headers})

    def test_loopback_public_and_listed_hosts_pass(self) -> None:
        app = guarded(extra=["mymac.local"])
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "mac.example.com", "mymac.local:8765"):
            self.assertEqual(200, self.get(app, host).status_code, host)

    def test_a_rebinding_host_is_refused_before_any_route(self) -> None:
        response = self.get(guarded(), "attacker.example:8765")
        self.assertEqual(421, response.status_code)
        self.assertEqual({"detail": "untrusted_host"}, response.json())

    def test_websockets_are_guarded_too(self) -> None:
        with TestClient(guarded()).websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}) as socket:
            self.assertEqual("hi", socket.receive_text())
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with TestClient(guarded()).websocket_connect("/ws", headers={"host": "attacker.example"}) as socket:
                socket.receive_text()
        self.assertEqual(4403, ctx.exception.code)

    def test_without_auth_a_foreign_browser_origin_is_refused(self) -> None:
        app = guarded(no_auth=True)
        self.assertEqual(403, self.get(app, "127.0.0.1:8765", origin="https://attacker.example").status_code)
        self.assertEqual(403, self.get(app, "127.0.0.1:8765", origin="null").status_code)
        self.assertEqual(200, self.get(app, "127.0.0.1:8765", origin="http://127.0.0.1:8765").status_code)
        self.assertEqual(200, self.get(app, "127.0.0.1:8765", origin="chrome-extension://abc").status_code)
        self.assertEqual(200, self.get(app, "127.0.0.1:8765").status_code, "non-browser clients send no Origin")

    def test_authenticated_mode_leaves_origin_to_authentication(self) -> None:
        self.assertEqual(200, self.get(guarded(), "127.0.0.1:8765", origin="https://chatgpt.com").status_code)

    def test_an_all_interfaces_bind_skips_the_host_allowlist(self) -> None:
        self.assertEqual(200, self.get(guarded(enforce_hosts=False), "192.168.1.20:8765").status_code)


if __name__ == "__main__":
    unittest.main()
