from __future__ import annotations

import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mcp_server import host_guard
from mcp_server.host_guard import HostGuard, chrome_unpacked_extension_id, companion_extension_origins


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
        self.assertEqual(200, self.get(app, "127.0.0.1:8765").status_code, "non-browser clients send no Origin")

    def test_without_auth_only_the_mac_mcp_chrome_companion_extension_passes(self) -> None:
        app = guarded(no_auth=True)
        own = next(iter(companion_extension_origins()))
        self.assertEqual(200, self.get(app, "127.0.0.1:8765", origin=own).status_code)
        self.assertEqual(200, self.get(app, "127.0.0.1:8765", origin=own.upper() + "/").status_code)
        for foreign in (
            "chrome-extension://abc",
            "chrome-extension://" + "a" * 32,
            "safari-web-extension://3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "moz-extension://3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            own.replace("chrome-extension://", "moz-extension://"),
            own + ".attacker.example",
        ):
            self.assertEqual(403, self.get(app, "127.0.0.1:8765", origin=foreign).status_code, foreign)
            self.assertEqual({"detail": "untrusted_origin"}, self.get(app, "127.0.0.1:8765", origin=foreign).json())
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with TestClient(app).websocket_connect(
                "/ws", headers={"host": "127.0.0.1:8765", "origin": "chrome-extension://" + "b" * 32},
            ) as socket:
                socket.receive_text()
        self.assertEqual(4403, ctx.exception.code)
        with TestClient(app).websocket_connect("/ws", headers={"host": "127.0.0.1:8765", "origin": own}) as socket:
            self.assertEqual("hi", socket.receive_text())

    def test_companion_id_follows_chromes_unpacked_extension_rule(self) -> None:
        # Chrome's own ID for the companion loaded unpacked from ~/mac-mcp.
        self.assertEqual(
            "accccofaidjhjjblachghodaalolcnmb",
            chrome_unpacked_extension_id(Path("/Users/tarkanbulut/mac-mcp/menu_app/ChromeVisualCompanion")),
        )
        companion = Path(host_guard.__file__).resolve().parent.parent / "menu_app" / "ChromeVisualCompanion"
        self.assertEqual(
            frozenset({"chrome-extension://" + chrome_unpacked_extension_id(companion)}),
            companion_extension_origins(),
        )

    def test_authenticated_mode_leaves_origin_to_authentication(self) -> None:
        app = guarded()
        for origin in (
            "https://chatgpt.com",
            "chrome-extension://" + "a" * 32,
            "safari-web-extension://3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "moz-extension://3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        ):
            self.assertEqual(200, self.get(app, "127.0.0.1:8765", origin=origin).status_code, origin)

    def test_an_all_interfaces_bind_skips_the_host_allowlist(self) -> None:
        self.assertEqual(200, self.get(guarded(enforce_hosts=False), "192.168.1.20:8765").status_code)


if __name__ == "__main__":
    unittest.main()
