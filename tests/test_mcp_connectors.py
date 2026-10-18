"""Guided connectors (#260): catalog, Keychain-held credentials, browser sign-in, disconnect.

A real OAuth authorization server and a real MCP server that demands its tokens run on a local
port, so discovery, dynamic client registration, PKCE, the callback route, refresh after a
restart, the sign-in-required state and revocation all go over HTTP the way a hosted service does.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import base64
import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from mcp_server import external_mcp, external_mcp_routes, mcp_connectors

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


class MemoryStore:
    """Stands in for the Keychain."""

    def __init__(self) -> None:
        self.items: Dict[str, str] = {}

    def read(self, account: str) -> Optional[str]:
        return self.items.get(account)

    def write(self, account: str, value: str) -> None:
        self.items[account] = value

    def delete(self, account: str) -> None:
        self.items.pop(account, None)


class FakeService:
    """An OAuth authorization server plus an MCP server that accepts only its tokens."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.codes: Dict[str, Dict[str, str]] = {}
        self.valid: set[str] = {"static-good-token"}
        self.refresh: Dict[str, str] = {}
        self.registered: list[dict] = []
        self.grants: list[str] = []
        self.revoked: list[str] = []
        self.authorize_params: list[Dict[str, str]] = []
        self._counter = 0

        mcp = FastMCP("fake-service")

        @mcp.tool()
        def whoami() -> str:
            """Who the token belongs to."""
            return "owner"

        mcp_app = mcp.streamable_http_app()
        service = self

        class RequireToken:
            def __init__(self, app) -> None:
                self.app = app

            async def __call__(self, scope, receive, send) -> None:
                if scope["type"] == "http" and scope["path"].startswith("/mcp"):
                    headers = dict(scope.get("headers") or [])
                    token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
                    if token not in service.valid:
                        response = JSONResponse({"error": "invalid_token"}, status_code=401, headers={
                            "WWW-Authenticate": f'Bearer resource_metadata="{service.base}/.well-known/oauth-protected-resource"'})
                        await response(scope, receive, send)
                        return
                await self.app(scope, receive, send)

        async def resource_metadata(_request: Request):
            return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base]})

        async def server_metadata(_request: Request):
            return JSONResponse({
                "issuer": self.base, "authorization_endpoint": f"{self.base}/oauth/authorize",
                "token_endpoint": f"{self.base}/oauth/token", "registration_endpoint": f"{self.base}/oauth/register",
                "revocation_endpoint": f"{self.base}/oauth/revoke", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            })

        async def register(request: Request):
            body = await request.json()
            self.registered.append(body)
            return JSONResponse({**body, "client_id": f"client-{len(self.registered)}"}, status_code=201)

        async def authorize(request: Request):
            params = dict(request.query_params)
            self.authorize_params.append(params)
            code = f"code-{len(self.authorize_params)}"
            self.codes[code] = params
            return RedirectResponse(f"{params['redirect_uri']}?code={code}&state={params['state']}", status_code=302)

        async def token(request: Request):
            form = dict(await request.form())
            self.grants.append(form["grant_type"])
            if form["grant_type"] == "authorization_code":
                params = self.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if params is None or params["code_challenge"] != challenge:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
            elif form.get("refresh_token") not in self.refresh:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            self._counter += 1
            access, refresh = f"access-{self._counter}", f"refresh-{self._counter}"
            self.valid.add(access)
            self.refresh[refresh] = access
            return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": 3600,
                                 "refresh_token": refresh})

        async def revoke(request: Request):
            form = dict(await request.form())
            self.revoked.append(form.get("token", ""))
            return JSONResponse({})

        app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource", resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
            Route("/.well-known/oauth-authorization-server", server_metadata),
            Route("/oauth/register", register, methods=["POST"]),
            Route("/oauth/authorize", authorize),
            Route("/oauth/token", token, methods=["POST"]),
            Route("/oauth/revoke", revoke, methods=["POST"]),
            Mount("/", app=RequireToken(mcp_app)),
        ], lifespan=lambda _app: mcp.session_manager.run())
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)

    def forget_everything(self) -> None:
        self.valid = {"static-good-token"}
        self.refresh.clear()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConnectorFixture(unittest.TestCase):
    service: FakeService

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = FakeService()
        cls.service.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.stop()

    def setUp(self) -> None:
        self.service.forget_everything()
        self.dir = Path(tempfile.mkdtemp())
        self.store = MemoryStore()
        base = self.service.base
        catalog = (
            mcp_connectors.Connector(id="tokensvc", title="Token Service", summary="s", url=f"{base}/mcp",
                                     auth="token", symbol="key", manage_url="https://example.invalid/tokens",
                                     manage_hint="h", token_url="https://example.invalid/new"),
            mcp_connectors.Connector(id="oauthsvc", title="OAuth Service", summary="s", url=f"{base}/mcp",
                                     auth="oauth", symbol="key", manage_url="https://example.invalid/apps",
                                     manage_hint="h"),
            mcp_connectors.Connector(id="appsvc", title="App Service", summary="s", url=f"{base}/mcp",
                                     auth="oauth_app", symbol="key", manage_url="https://example.invalid/apps",
                                     manage_hint="h", scope="read write", authorize_params=(("access_type", "offline"),)),
        )
        for patcher in (patch.object(mcp_connectors, "_STORE", self.store),
                        patch.object(mcp_connectors, "CATALOG", catalog),
                        patch.object(external_mcp, "config_path", return_value=self.dir / "mcp_servers.json"),
                        patch.object(external_mcp, "manager", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        mcp_connectors._SIGN_INS.clear()
        app = Starlette(routes=external_mcp_routes.create_routes(lambda _request: None))
        self.client = TestClient(app, base_url="http://127.0.0.1:8899")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def act(self, **body) -> dict:
        return self.client.post("/dashboard/api/mcp-servers/action", json=body).json()

    def status(self) -> dict:
        return self.client.get("/dashboard/api/mcp-servers").json()

    def browser_approves(self, authorize_url: str) -> str:
        """Follow the service's sign-in page to its redirect, as the owner's browser would."""
        import httpx

        response = httpx.get(authorize_url, follow_redirects=False)
        location = urlsplit(response.headers["location"])
        return f"{location.path}?{location.query}"

    def wait_for(self, name: str, states=("connected", "failed")) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            flow = self.status()["sign_ins"].get(name) or {}
            if flow.get("state") in states:
                return flow
            time.sleep(0.05)
        self.fail(f"sign-in of {name} did not finish: {self.status()['sign_ins']}")

    def probe_saved(self, name: str) -> dict:
        servers, _ = external_mcp.load_servers()
        return asyncio.run(external_mcp.probe(servers[name], timeout=10))


class TokenConnectorTests(ConnectorFixture):
    def test_a_bad_token_is_not_saved(self) -> None:
        response = self.act(action="connect", connector="tokensvc", token="wrong-token")
        self.assertFalse(response["ok"])
        self.assertIn("did not accept the token", response["message"])
        self.assertNotIn("wrong-token", json.dumps(response))
        self.assertEqual({}, self.store.items)
        self.assertFalse((self.dir / "mcp_servers.json").exists())

    def test_a_good_token_goes_to_the_keychain_not_the_config(self) -> None:
        response = self.act(action="connect", connector="tokensvc", token="static-good-token")
        self.assertTrue(response["ok"], response)
        self.assertEqual(1, response["result"]["tools"])
        config = (self.dir / "mcp_servers.json").read_text()
        self.assertNotIn("static-good-token", config)
        self.assertEqual({"url": f"{self.service.base}/mcp", "catalog": "tokensvc", "auth": "token", "listed": False},
                         json.loads(config)["mcpServers"]["tokensvc"])
        self.assertEqual("static-good-token", self.store.items["tokensvc.token"])
        self.assertTrue(self.probe_saved("tokensvc")["ok"])
        self.assertNotIn("static-good-token", json.dumps(self.status()))

    def test_a_missing_token_asks_for_a_new_connection(self) -> None:
        self.act(action="connect", connector="tokensvc", token="static-good-token")
        self.store.items.clear()
        result = self.probe_saved("tokensvc")
        self.assertFalse(result["ok"])
        self.assertIn("Connect tokensvc again in Settings", result["error"])

    def test_a_hand_made_server_of_the_same_name_is_kept(self) -> None:
        external_mcp.upsert_server("tokensvc", {"url": "https://mine.example/mcp"})
        response = self.client.post("/dashboard/api/mcp-servers/action",
                                    json={"action": "connect", "connector": "tokensvc", "token": "static-good-token"})
        self.assertEqual(400, response.status_code)
        self.assertIn("already exists", response.json()["message"])
        self.assertEqual("https://mine.example/mcp", external_mcp.read_raw_config()["mcpServers"]["tokensvc"]["url"])

    def test_disconnect_deletes_the_keychain_item_and_the_entry(self) -> None:
        self.act(action="connect", connector="tokensvc", token="static-good-token")
        response = self.act(action="disconnect", name="tokensvc")
        self.assertTrue(response["ok"])
        self.assertEqual("https://example.invalid/tokens", response["result"]["manage_url"])
        self.assertEqual({}, self.store.items)
        self.assertNotIn("tokensvc", external_mcp.read_raw_config()["mcpServers"])

    def test_plain_remove_of_a_connected_service_deletes_its_keychain_item(self) -> None:
        self.act(action="connect", connector="tokensvc", token="static-good-token")
        self.assertTrue(self.act(action="remove", name="tokensvc")["ok"])
        self.assertEqual({}, self.store.items)


class BrowserSignInTests(ConnectorFixture):
    def sign_in(self, connector: str = "oauthsvc", **extra) -> dict:
        started = self.act(action="connect", connector=connector, **extra)
        self.assertTrue(started["ok"], started)
        flow = started["result"]
        self.assertEqual("waiting", flow["state"])
        callback = self.browser_approves(flow["authorize_url"])
        page = self.client.get(callback)
        self.assertEqual(200, page.status_code)
        self.assertIn("close this tab", page.text)
        return self.wait_for(connector)

    def test_dynamic_registration_sign_in_saves_tokens_in_the_keychain(self) -> None:
        flow = self.sign_in()
        self.assertEqual("connected", flow["state"])
        self.assertEqual(1, flow["tools"])
        registration = self.service.registered[-1]
        self.assertEqual(["http://localhost:8899/connectors/oauth/callback"], registration["redirect_uris"])
        self.assertEqual("none", registration["token_endpoint_auth_method"])
        self.assertEqual("S256", self.service.authorize_params[-1]["code_challenge_method"])
        saved = json.loads(self.store.items["oauthsvc.oauth"])
        self.assertTrue(saved["tokens"]["access_token"].startswith("access-"))
        self.assertEqual(f"{self.service.base}/oauth/token", saved["oauth_metadata"]["token_endpoint"])
        config = external_mcp.read_raw_config()["mcpServers"]["oauthsvc"]
        self.assertEqual({"url": f"{self.service.base}/mcp", "catalog": "oauthsvc", "auth": "oauth", "listed": False},
                         config)
        self.assertEqual({"oauthsvc.oauth"}, set(self.store.items))
        self.assertNotIn("access-", json.dumps(self.status()))

    def test_signing_in_again_keeps_the_tool_list_choice(self) -> None:
        self.sign_in()
        external_mcp.set_server_fields("oauthsvc", listed=True)
        self.sign_in()
        self.assertTrue(external_mcp.read_raw_config()["mcpServers"]["oauthsvc"]["listed"])

    def test_a_cancelled_sign_in_again_keeps_the_working_one(self) -> None:
        self.sign_in()
        working = self.store.items["oauthsvc.oauth"]
        self.act(action="connect", connector="oauthsvc")
        self.assertIn("oauthsvc.oauth.pending", self.store.items)
        self.act(action="cancel_sign_in", name="oauthsvc")
        self.assertEqual({"oauthsvc.oauth": working}, self.store.items)
        self.assertTrue(self.probe_saved("oauthsvc")["ok"])

    def test_a_restart_with_unknown_expiry_refreshes_at_the_saved_token_endpoint(self) -> None:
        self.sign_in()
        saved = json.loads(self.store.items["oauthsvc.oauth"])
        saved["expires_at"] = None
        self.store.items["oauthsvc.oauth"] = json.dumps(saved)
        grants = len(self.service.grants)
        self.assertTrue(self.probe_saved("oauthsvc")["ok"])
        self.assertEqual(["refresh_token"], self.service.grants[grants:])

    def test_a_revoked_sign_in_reports_sign_in_required_without_a_browser(self) -> None:
        self.sign_in()
        self.service.forget_everything()
        pages = len(self.service.authorize_params)
        result = self.probe_saved("oauthsvc")
        self.assertFalse(result["ok"])
        self.assertIn("Sign in to OAuth Service again in Settings", result["error"])
        self.assertEqual(pages, len(self.service.authorize_params))

    def test_own_app_sign_in_uses_the_owner_client_and_pinned_scope(self) -> None:
        registered = len(self.service.registered)
        flow = self.sign_in("appsvc", client_id="owner-client", client_secret="owner-secret")
        self.assertEqual("connected", flow["state"])
        self.assertEqual(registered, len(self.service.registered))  # no dynamic registration
        params = self.service.authorize_params[-1]
        self.assertEqual("owner-client", params["client_id"])
        self.assertEqual("read write", params["scope"])
        self.assertEqual("offline", params["access_type"])
        self.assertNotIn("owner-secret", json.dumps(self.status()))

    def test_own_app_needs_a_client_id(self) -> None:
        response = self.client.post("/dashboard/api/mcp-servers/action",
                                    json={"action": "connect", "connector": "appsvc"})
        self.assertEqual(400, response.status_code)

    def test_a_callback_with_an_unknown_state_is_refused(self) -> None:
        started = self.act(action="connect", connector="oauthsvc")["result"]
        page = self.client.get("/connectors/oauth/callback?code=x&state=not-the-state")
        self.assertEqual(400, page.status_code)
        self.assertEqual("waiting", self.status()["sign_ins"]["oauthsvc"]["state"])
        self.assertTrue(started["authorize_url"])
        self.assertTrue(self.act(action="cancel_sign_in", name="oauthsvc")["result"]["cancelled"])
        self.assertNotIn("oauthsvc", self.status()["sign_ins"])

    def test_a_denied_sign_in_fails_and_saves_nothing(self) -> None:
        started = self.act(action="connect", connector="oauthsvc")["result"]
        state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
        page = self.client.get(f"/connectors/oauth/callback?error=access_denied&state={state}")
        self.assertIn("was not connected", page.text)
        flow = self.wait_for("oauthsvc")
        self.assertEqual("failed", flow["state"])
        self.assertIn("access_denied", flow["error"])
        self.assertNotIn("oauthsvc", external_mcp.read_raw_config()["mcpServers"])
        self.assertEqual({}, self.store.items)

    def test_disconnect_revokes_at_the_service_and_forgets_tokens(self) -> None:
        self.sign_in()
        refresh = json.loads(self.store.items["oauthsvc.oauth"])["tokens"]["refresh_token"]
        response = self.act(action="disconnect", name="oauthsvc")
        self.assertTrue(response["result"]["revoke_requested"])
        self.assertIn(refresh, self.service.revoked)
        self.assertEqual({}, self.store.items)


class CatalogTests(unittest.TestCase):
    def test_catalog_lists_the_four_services_with_their_own_servers(self) -> None:
        catalog = {item["id"]: item for item in mcp_connectors.catalog_public(8765)}
        self.assertEqual(["github", "notion", "slack", "google-drive"], list(catalog))
        self.assertEqual("https://api.githubcopilot.com/mcp/", catalog["github"]["url"])
        self.assertEqual("token", catalog["github"]["auth"])
        self.assertEqual("oauth", catalog["notion"]["auth"])
        self.assertEqual("http://localhost:8765/connectors/oauth/callback", catalog["slack"]["redirect_uri"])
        for item in catalog.values():
            self.assertTrue(item["url"].startswith("https://"))
            self.assertTrue(item["manage_url"].startswith("https://"))
            external_mcp.parse_server(item["id"], {"url": item["url"], "catalog": item["id"], "auth": "oauth"})

    def test_auth_is_only_token_or_oauth(self) -> None:
        with self.assertRaises(external_mcp.ConfigError):
            external_mcp.parse_server("x", {"url": "https://a", "auth": "password"})
        public = external_mcp.parse_server("x", {"url": "https://a", "catalog": "github", "auth": "token"}).public()
        self.assertEqual(("github", "token"), (public["catalog"], public["auth"]))

    def test_keychain_writes_never_put_a_secret_in_process_arguments(self) -> None:
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs.get("input")))

            class Done:
                returncode, stdout = 0, ""
            return Done()

        store = mcp_connectors.KeychainStore()
        with patch.object(mcp_connectors.subprocess, "run", side_effect=fake_run), \
                patch.object(store, "read", return_value="ghp_secretvalue"):
            store.write("github.token", "ghp_secretvalue")
        add = [args for args, _ in calls if "-i" in args]
        self.assertEqual([[mcp_connectors.SECURITY, "-i"]], add)
        for args, _ in calls:
            self.assertNotIn("ghp_secretvalue", " ".join(args))


class ConnectorPaneSourceTests(unittest.TestCase):
    def test_pane_shows_the_catalog_and_never_reads_secrets_back(self) -> None:
        pane = (SOURCES / "McpServersPane.swift").read_text(encoding="utf-8") \
            + (SOURCES / "McpConnectorsSection.swift").read_text(encoding="utf-8")
        self.assertIn('"connectors"', pane)
        self.assertIn('"action": "connect"', pane)
        self.assertIn('"disconnect"', pane)
        self.assertIn("SecureField", pane)
        self.assertIn('"sign_in_required"', pane)
        self.assertNotIn('object["token"]', pane)
        self.assertNotRegex(pane, r"(?<![A-Za-z0-9_])@State\b")
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Sources/McpConnectorsSection.swift"', script)
        # Each catalog service ships its own logo in the app; none is fetched from the web.
        self.assertIn('"${SCRIPT_DIR}/Assets/Connectors/"*.png', script)
        for item in mcp_connectors.CATALOG:
            self.assertTrue((ROOT / "menu_app" / "Assets" / "Connectors" / f"{item.id}.png").is_file(), item.id)
        self.assertIn('subdirectory: "Assets/Connectors"', pane)


if __name__ == "__main__":
    unittest.main()
