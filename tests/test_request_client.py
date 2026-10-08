from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_server.dashboard_routes import _client_address, _local_only, create_dashboard_routes
from mcp_server.mobile_auth import MobileAuthStore
from mcp_server.mobile_routes import _management_guard, create_mobile_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings


TOKEN = "request-client-fixture-token"


def request(headers=(), *, peer="127.0.0.1", path="/dashboard/api/summary"):
    return Request({
        "type": "http", "method": "GET", "scheme": "http", "path": path,
        "query_string": b"", "server": ("127.0.0.1", 8877),
        "client": (peer, 12345) if peer is not None else None,
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
    })


class RequestClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-mcp-request-client-")
        self.env = patch.dict(os.environ, {
            "MAC_MCP_SETTINGS_PATH": str(Path(self.tmp.name) / "settings.json"),
            "MAC_MCP_STATE_DIR": self.tmp.name,
            "MAC_MCP_PUBLIC_ENDPOINT_MODE": "ngrok", "NGROK_DOMAIN": "fixture.ngrok.app",
            "MAC_MCP_PUBLIC_URL": "https://fixture.example/mcp", "CLOUDFLARE_TUNNEL": "fixture",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_ngrok_uses_appended_hop_even_with_forged_cloudflare_header(self):
        req = request([
            ("x-forwarded-for", "127.0.0.1, 203.0.113.9"),
            ("cf-connecting-ip", "127.0.0.1"),
        ])
        self.assertEqual("203.0.113.9", _client_address(req))
        self.assertEqual(403, _local_only(req).status_code)

    def test_remote_peer_cannot_claim_loopback_using_headers(self):
        req = request([("x-forwarded-for", "127.0.0.1")], peer="203.0.113.9")
        self.assertEqual("203.0.113.9", _client_address(req))
        self.assertEqual(403, _local_only(req).status_code)

    def test_forwarded_loopback_never_grants_dashboard_or_mobile_management(self):
        for mode in ("none", "ngrok", "cloudflare", "custom"):
            for header in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip", "forwarded",
                           "x-forwarded-proto", "x-forwarded-host"):
                with self.subTest(mode=mode, header=header), patch.dict(os.environ, {"MAC_MCP_PUBLIC_ENDPOINT_MODE": mode}):
                    req = request([(header, "127.0.0.1"), ("authorization", "Bearer " + TOKEN)])
                    dashboard_denial = _local_only(req)
                    mobile_denial = _management_guard(req, TOKEN)
                    self.assertIsNotNone(dashboard_denial)
                    self.assertIsNotNone(mobile_denial)
                    self.assertEqual(403, dashboard_denial.status_code)
                    self.assertEqual(403, mobile_denial.status_code)

    def test_direct_local_access_still_requires_token(self):
        for mode in ("none", "ngrok", "cloudflare", "custom"):
            for peer in ("127.0.0.1", "::1"):
                with self.subTest(mode=mode, peer=peer), patch.dict(os.environ, {"MAC_MCP_PUBLIC_ENDPOINT_MODE": mode}):
                    self.assertIsNone(_local_only(request(peer=peer)))
                    self.assertEqual(401, _management_guard(request(peer=peer), TOKEN).status_code)
                    self.assertIsNone(_management_guard(request([("authorization", "Bearer " + TOKEN)], peer=peer), TOKEN))
        self.assertEqual(403, _local_only(request(peer=None)).status_code)

    def test_cloudflare_uses_single_connecting_ip(self):
        with patch.dict(os.environ, {"MAC_MCP_PUBLIC_ENDPOINT_MODE": "cloudflare"}):
            req = request([("cf-connecting-ip", "2001:db8::9"), ("x-forwarded-for", "127.0.0.1")])
            self.assertEqual("2001:db8::9", _client_address(req))
            self.assertEqual(403, _local_only(req).status_code)

    def test_unconfigured_proxy_headers_do_not_change_client_identity(self):
        for mode in ("none", "custom"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"MAC_MCP_PUBLIC_ENDPOINT_MODE": mode}):
                req = request([("cf-connecting-ip", "203.0.113.9"), ("x-forwarded-for", "203.0.113.10")])
                self.assertEqual("127.0.0.1", _client_address(req))
                self.assertEqual(403, _local_only(req).status_code)

    def test_ngrok_duplicate_headers_use_last_appended_hop(self):
        req = request([("x-forwarded-for", "127.0.0.1"), ("x-forwarded-for", "203.0.113.10")])
        self.assertEqual("203.0.113.10", _client_address(req))

    def test_malformed_or_loopback_forwarding_has_no_usable_remote_identity(self):
        for mode, headers in (
            ("ngrok", [("x-forwarded-for", "203.0.113.9, invalid")]),
            ("ngrok", [("x-forwarded-for", "203.0.113.9,")]),
            ("ngrok", [("x-forwarded-for", "127.0.0.1")]),
            ("ngrok", [("cf-connecting-ip", "203.0.113.9")]),
            ("cloudflare", [("cf-connecting-ip", "127.0.0.1")]),
            ("cloudflare", [("cf-connecting-ip", "203.0.113.9"), ("cf-connecting-ip", "203.0.113.10")]),
            ("invalid", [("x-forwarded-for", "203.0.113.9")]),
        ):
            with self.subTest(mode=mode, headers=headers), patch.dict(os.environ, {"MAC_MCP_PUBLIC_ENDPOINT_MODE": mode}):
                req = request(headers)
                self.assertEqual("unknown", _client_address(req))
                self.assertEqual(403, _local_only(req).status_code)

    def test_real_routes_deny_forged_local_request_after_uvicorn_proxy_rewrite(self):
        root = Path(self.tmp.name)
        telemetry = TelemetryManager(db_path=root / "telemetry.sqlite3")
        store = MobileAuthStore(root / "mobile.sqlite3")
        settings = load_settings()
        app = Starlette(routes=[
            *create_dashboard_routes(telemetry, settings, TOKEN),
            *create_mobile_routes(telemetry, settings, TOKEN, auth_store=store),
        ])
        client = TestClient(ProxyHeadersMiddleware(app), client=("127.0.0.1", 12345))
        auth = {"authorization": "Bearer " + TOKEN}
        self.assertEqual(200, client.get("/dashboard/api/summary", headers=auth).status_code)
        forged = {**auth, "x-forwarded-for": "127.0.0.1, 203.0.113.9", "cf-connecting-ip": "127.0.0.1"}
        for path in ("/dashboard", "/dashboard/api/summary", "/dashboard/api/mobile/devices"):
            with self.subTest(path=path):
                self.assertEqual(403, client.get(path, headers=forged).status_code)
        self.assertEqual(403, client.post("/dashboard/api/mobile/pairings", headers=forged).status_code)
        self.assertEqual(403, client.post("/dashboard/api/mobile/revoke", headers=forged, json={"device_id": "fixture"}).status_code)
        self.assertEqual(200, client.get("/mobile", headers=forged).status_code)
        self.assertEqual(401, client.get("/mobile/api/status", headers=forged).status_code)


if __name__ == "__main__":
    unittest.main()
