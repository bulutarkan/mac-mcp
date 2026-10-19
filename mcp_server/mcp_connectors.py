"""Guided connectors for common services (#260): GitHub, Notion, Slack, Google Drive.

Settings › MCP Servers offers a small catalog of hosted MCP servers, so the owner connects a
service without finding an endpoint or writing a command. A connected service is an ordinary
entry in ~/.mac-mcp/mcp_servers.json ({"url": ..., "catalog": "github", "auth": "token"}), so its
tools go through the same Mac MCP path as every other external tool (external_mcp.py). Only the
credential is different: it lives in the owner's login Keychain, never in that file.

Three ways to sign in:

- ``token``: the owner pastes a token (GitHub). It is tested before it is saved.
- ``oauth``: the service registers Mac MCP itself (Notion supports dynamic client registration),
  so the owner only approves access in the browser.
- ``oauth_app``: the service needs an app of the owner's own (Slack, Google Drive); the owner
  enters its client ID and secret, then approves access in the browser.

Browser sign-in returns to http://localhost:<port>/connectors/oauth/callback on this server.
That route accepts only the random state of a sign-in that is waiting for it. A connection made
in the background never opens a browser: when its sign-in has expired it reports
``sign_in_required`` and the owner signs in again from Settings.

Keychain items are written and read through /usr/bin/security only. Secrets go in on stdin
(``security -i``), never as a process argument; long values are compressed and split over a
few items because one interactive line holds about 4 KB.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import external_mcp

LOG = logging.getLogger(__name__)

SECURITY = "/usr/bin/security"
SERVICE = "com.bulutarkan.mac-mcp.connector"
CALLBACK_PATH = "/connectors/oauth/callback"
CHUNK_CHARS = 3000
MAX_CHUNKS = 8
URL_WAIT_S = 15.0
SIGN_IN_TIMEOUT_S = 600.0
TOKEN_TEST_TIMEOUT_S = 15.0
AUTH_KINDS = ("token", "oauth")


class ConnectorError(ValueError):
    """A connector request that cannot be done as asked."""


SignInRequired = external_mcp.SignInRequired


@dataclass(frozen=True)
class Connector:
    id: str
    title: str
    summary: str
    url: str
    auth: str  # token | oauth | oauth_app
    symbol: str
    manage_url: str
    manage_hint: str
    token_url: Optional[str] = None
    token_hint: str = ""
    setup_url: Optional[str] = None
    setup_steps: Tuple[str, ...] = ()
    scope: Optional[str] = None
    authorize_params: Tuple[Tuple[str, str], ...] = ()
    verified: bool = True

    def public(self, redirect_uri: Optional[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id, "title": self.title, "summary": self.summary, "url": self.url, "auth": self.auth,
            "symbol": self.symbol, "manage_url": self.manage_url, "manage_hint": self.manage_hint,
            "verified": self.verified,
        }
        if self.auth == "token":
            out.update({"token_url": self.token_url, "token_hint": self.token_hint})
        if self.auth == "oauth_app":
            out.update({"setup_url": self.setup_url, "setup_steps": list(self.setup_steps), "redirect_uri": redirect_uri})
        return out


CATALOG: Tuple[Connector, ...] = (
    Connector(
        id="github", title="GitHub", symbol="chevron.left.forwardslash.chevron.right",
        summary="Repositories, issues, pull requests and Actions, through GitHub’s own MCP server.",
        url="https://api.githubcopilot.com/mcp/", auth="token",
        token_url="https://github.com/settings/personal-access-tokens/new",
        token_hint="Create a fine-grained personal access token. Pick the repositories and permissions agents may use; "
                   "read-only permissions keep them from changing anything.",
        manage_url="https://github.com/settings/personal-access-tokens",
        manage_hint="Delete the token on GitHub to cut access everywhere.",
    ),
    Connector(
        id="notion", title="Notion", symbol="doc.text",
        summary="Search, read and edit the pages you share, through Notion’s own MCP server.",
        url="https://mcp.notion.com/mcp", auth="oauth",
        manage_url="https://www.notion.so/profile/integrations",
        manage_hint="Notion lists Mac MCP under Settings › Connections, where you can remove it.",
    ),
    Connector(
        id="slack", title="Slack", symbol="number",
        summary="Search and read messages, and post as you, through Slack’s own MCP server.",
        url="https://mcp.slack.com/mcp", auth="oauth_app", verified=False,
        setup_url="https://api.slack.com/apps",
        setup_steps=(
            "Slack lets only an app of your workspace use its MCP server. At api.slack.com/apps choose "
            "Create New App › From scratch and pick your workspace.",
            "Under OAuth & Permissions add the redirect URL below, and the user token scopes search:read.public, "
            "search:read.private, search:read.im, search:read.mpim, channels:history, groups:history, im:history, "
            "mpim:history, users:read and chat:write.",
            "Copy the Client ID and Client Secret from Basic Information into the fields below.",
        ),
        scope="search:read.public search:read.private search:read.im search:read.mpim channels:history "
              "groups:history im:history mpim:history users:read chat:write",
        manage_url="https://slack.com/apps/manage",
        manage_hint="Remove the app’s access in Slack under Manage Apps.",
    ),
    Connector(
        id="google-drive", title="Google Drive", symbol="externaldrive",
        summary="Search, read and create Drive files, through Google’s own MCP server (developer preview).",
        url="https://drivemcp.googleapis.com/mcp/v1", auth="oauth_app", verified=False,
        setup_url="https://console.cloud.google.com/apis/credentials",
        setup_steps=(
            "Google’s Drive MCP server is in developer preview: your Google Cloud project needs access to it.",
            "In Google Cloud › APIs & Services › Credentials create an OAuth client ID of type Web application "
            "and add the redirect URI below.",
            "Copy its Client ID and Client Secret into the fields below.",
        ),
        scope="https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file",
        authorize_params=(("access_type", "offline"), ("prompt", "consent")),
        manage_url="https://myaccount.google.com/connections",
        manage_hint="Remove Mac MCP’s access in your Google Account under Third-party connections.",
    ),
)


def connector(connector_id: str) -> Connector:
    for item in CATALOG:
        if item.id == connector_id:
            return item
    raise ConnectorError(f"no connector named {connector_id!r}")


def redirect_uri(port: Optional[int]) -> str:
    return f"http://localhost:{int(port or 8765)}{CALLBACK_PATH}"


# ---------------------------------------------------------------------------
# Keychain

def _service() -> str:
    return os.getenv("MAC_MCP_CONNECTOR_KEYCHAIN_SERVICE", SERVICE)


class KeychainStore:
    """Generic-password items under one service, one logical value per account."""

    def _find(self, account: str) -> Optional[str]:
        proc = subprocess.run([SECURITY, "find-generic-password", "-s", _service(), "-a", account, "-w"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
        value = proc.stdout.strip()
        return value if proc.returncode == 0 and value else None

    def read(self, account: str) -> Optional[str]:
        head = self._find(account)
        if not head or not head.startswith("mk1."):
            return None
        try:
            _, count, first = head.split(".", 2)
            parts = [first] + [self._find(f"{account}#{index}") or "" for index in range(1, int(count))]
            return zlib.decompress(base64.b64decode("".join(parts))).decode("utf-8")
        except (ValueError, zlib.error):
            return None

    def write(self, account: str, value: str) -> None:
        blob = base64.b64encode(zlib.compress(value.encode("utf-8"))).decode("ascii")
        chunks = [blob[index:index + CHUNK_CHARS] for index in range(0, len(blob), CHUNK_CHARS)] or [""]
        if len(chunks) > MAX_CHUNKS:
            raise ConnectorError("the sign-in is too large to keep in the Keychain")
        chunks[0] = f"mk1.{len(chunks)}.{chunks[0]}"
        label = f"Mac MCP connector: {account}"
        lines = []
        for index, chunk in enumerate(chunks):
            name = account if index == 0 else f"{account}#{index}"
            # Base64 and the fixed names need no quoting in security's interactive mode.
            lines.append(f'add-generic-password -s {_service()} -a {name} -l "{label}" -U -w {chunk}')
        proc = subprocess.run([SECURITY, "-i"], input="\n".join(lines) + "\n", capture_output=True, text=True,
                              timeout=30, check=False)
        if proc.returncode != 0 or self.read(account) != value:
            raise ConnectorError("the sign-in could not be saved in the Keychain")
        for index in range(len(chunks), MAX_CHUNKS):
            self._delete_one(f"{account}#{index}")

    def _delete_one(self, account: str) -> None:
        subprocess.run([SECURITY, "delete-generic-password", "-s", _service(), "-a", account],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)

    def delete(self, account: str) -> None:
        self._delete_one(account)
        for index in range(1, MAX_CHUNKS):
            self._delete_one(f"{account}#{index}")


_STORE: Any = KeychainStore()


def _token_account(name: str) -> str:
    return f"{name}.token"


def _oauth_account(name: str) -> str:
    return f"{name}.oauth"


def saved_token(name: str) -> Optional[str]:
    return _STORE.read(_token_account(name))


def _pending_account(name: str) -> str:
    return f"{name}.oauth.pending"


def forget(name: str) -> None:
    """Delete every Keychain item of one connected service."""
    _STORE.delete(_token_account(name))
    _STORE.delete(_oauth_account(name))
    _STORE.delete(_pending_account(name))


def _keep_pending(name: str) -> None:
    """A finished sign-in replaces the service's saved one; until then the old one keeps working."""
    value = _STORE.read(_pending_account(name))
    if value:
        _STORE.write(_oauth_account(name), value)
    _STORE.delete(_pending_account(name))


# ---------------------------------------------------------------------------
# OAuth

class KeychainTokenStorage:
    """The MCP SDK's TokenStorage, kept as one Keychain value per server.

    Besides tokens and client registration it keeps when the access token expires and the
    authorization server's metadata, so a restarted Mac MCP refreshes at the right endpoint
    instead of asking for a new sign-in.
    """

    def __init__(self, name: str, *, pending: bool = False) -> None:
        self.account = _pending_account(name) if pending else _oauth_account(name)
        self.provider: Any = None
        self._cache: Optional[Dict[str, Any]] = None

    def load(self) -> Dict[str, Any]:
        if self._cache is None:
            raw = _STORE.read(self.account)
            try:
                self._cache = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._cache = {}
        return self._cache

    async def _save(self, **fields: Any) -> None:
        data = dict(self.load())
        data.update(fields)
        metadata = getattr(getattr(self.provider, "context", None), "oauth_metadata", None)
        if metadata is not None:
            data["oauth_metadata"] = metadata.model_dump(mode="json", exclude_none=True)
        self._cache = data
        await asyncio.to_thread(_STORE.write, self.account, json.dumps(data))

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        tokens = self.load().get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens) -> None:
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        await self._save(tokens=tokens.model_dump(mode="json", exclude_none=True), expires_at=expires_at)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        info = self.load().get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info) -> None:
        await self._save(client_info=client_info.model_dump(mode="json", exclude_none=True))


def _client_metadata(redirect: str, scope: Optional[str], auth_method: str):
    from mcp.shared.auth import OAuthClientMetadata

    class PinnedScope(OAuthClientMetadata):
        # Discovery would replace the scope with everything the server lists; a catalog
        # scope is what the owner was told to grant, so it stays.
        def __setattr__(self, name: str, value: Any) -> None:
            if name == "scope" and scope:
                return
            super().__setattr__(name, value)

    return PinnedScope(
        client_name="Mac MCP", redirect_uris=[redirect], grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], token_endpoint_auth_method=auth_method, scope=scope,
    )


def _auth_provider(server_url: str, storage: KeychainTokenStorage, *, redirect: str, scope: Optional[str],
                   auth_method: str, redirect_handler, callback_handler):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthMetadata

    class ConnectorAuth(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            data = await asyncio.to_thread(storage.load)
            if data.get("oauth_metadata"):
                with contextlib.suppress(Exception):
                    self.context.oauth_metadata = OAuthMetadata.model_validate(data["oauth_metadata"])
            tokens = self.context.current_tokens
            if tokens is not None:
                # Unknown expiry with a refresh token: refresh first rather than trust it.
                expires_at = data.get("expires_at")
                self.context.token_expiry_time = expires_at or (1.0 if tokens.refresh_token else None)

    provider = ConnectorAuth(
        server_url, _client_metadata(redirect, scope, auth_method), storage,
        redirect_handler=redirect_handler, callback_handler=callback_handler, timeout=SIGN_IN_TIMEOUT_S,
    )
    storage.provider = provider
    return provider


def credentials(server: Any) -> Tuple[Dict[str, str], Any]:
    """Headers and httpx auth of a connected service's live connection (registered in external_mcp)."""
    headers = dict(server.headers)
    if server.auth == "token":
        token = saved_token(server.name)
        if not token:
            raise SignInRequired(f"Connect {server.name} again in Settings › MCP Servers: its token is missing.")
        headers["Authorization"] = f"Bearer {token}"
        return headers, None
    return headers, background_auth(server)


def background_auth(server: Any):
    """The httpx auth of a connected service's live connection: refreshes, never opens a browser."""
    item = _catalog_or_none(server.catalog)
    title = item.title if item else server.name

    async def no_browser(_url: str) -> None:
        raise SignInRequired(f"Sign in to {title} again in Settings › MCP Servers.")

    async def no_callback() -> Tuple[str, Optional[str]]:
        raise SignInRequired(f"Sign in to {title} again in Settings › MCP Servers.")

    storage = KeychainTokenStorage(server.name)
    info = storage.load().get("client_info") or {}
    redirect = (info.get("redirect_uris") or [redirect_uri(None)])[0]
    return _auth_provider(server.url, storage, redirect=redirect, scope=item.scope if item else None,
                          auth_method=info.get("token_endpoint_auth_method") or "none",
                          redirect_handler=no_browser, callback_handler=no_callback)


def _catalog_or_none(connector_id: Optional[str]) -> Optional[Connector]:
    try:
        return connector(str(connector_id or ""))
    except ConnectorError:
        return None


# ---------------------------------------------------------------------------
# Browser sign-in

@dataclass
class SignIn:
    name: str
    connector_id: str
    state: str = "starting"  # starting | waiting | connected | failed | cancelled
    authorize_url: Optional[str] = None
    oauth_state: Optional[str] = None
    error: Optional[str] = None
    tools: int = 0
    started_at: float = field(default_factory=time.time)
    url_ready: asyncio.Event = field(default_factory=asyncio.Event)
    code: Optional["asyncio.Future[Tuple[str, Optional[str]]]"] = None
    task: Optional["asyncio.Task[None]"] = None

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "connector": self.connector_id, "state": self.state,
                "authorize_url": self.authorize_url if self.state == "waiting" else None,
                "error": self.error, "tools": self.tools, "started_at": self.started_at}


_SIGN_INS: Dict[str, SignIn] = {}


def sign_ins() -> Dict[str, Dict[str, Any]]:
    return {name: flow.public() for name, flow in _SIGN_INS.items()}


def _with_params(url: str, extra: Tuple[Tuple[str, str], ...]) -> str:
    if not extra:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(dict(extra))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _check_name_free(item: Connector) -> None:
    raw = external_mcp.read_raw_config()["mcpServers"].get(item.id)
    if isinstance(raw, dict) and raw.get("catalog") != item.id:
        raise ConnectorError(f"an MCP server named {item.id!r} already exists; remove it first to connect {item.title}")


def _save_server(item: Connector, auth: str) -> None:
    # A service brings dozens of tools: agents find them with tool_discover unless the owner
    # shows them in the tool list. Signing in again keeps that choice.
    previous = external_mcp.read_raw_config()["mcpServers"].get(item.id) or {}
    external_mcp.upsert_server(item.id, {"url": item.url, "catalog": item.id, "auth": auth,
                                         "listed": previous.get("listed", False) is True})


async def _reload() -> None:
    manager = external_mcp.manager()
    if manager is not None and manager._loop is not None:
        await asyncio.to_thread(manager.reload)


async def connect_token(connector_id: str, token: str) -> Dict[str, Any]:
    """Test a pasted token against the service and keep it only when it works."""
    item = connector(connector_id)
    if item.auth != "token":
        raise ConnectorError(f"{item.title} signs in through the browser, not with a token")
    token = str(token or "").strip()
    if not token or any(ch.isspace() for ch in token):
        raise ConnectorError("paste the token on its own, without spaces")
    _check_name_free(item)
    trial = external_mcp.ServerConfig(name=item.id, transport="http", url=item.url,
                                      headers=(("Authorization", f"Bearer {token}"),))
    result = await external_mcp.probe(trial, timeout=TOKEN_TEST_TIMEOUT_S)
    if not result["ok"]:
        return {"ok": False, "error": _plain_error(result.get("error"), token)}
    await asyncio.to_thread(_STORE.write, _token_account(item.id), token)
    await asyncio.to_thread(_save_server, item, "token")
    await _reload()
    return {"ok": True, "name": item.id, "tools": len(result.get("tools") or [])}


def _plain_error(error: Any, *secrets_: str) -> str:
    text = str(error or "unknown error")
    for value in secrets_:
        if value:
            text = text.replace(value, "***")
    if "401" in text or "Unauthorized" in text:
        return "The service did not accept the token. Check that it is complete and not expired."
    return text[:400]


async def start_sign_in(connector_id: str, port: Optional[int], *, client_id: str = "",
                        client_secret: str = "") -> Dict[str, Any]:
    """Begin a browser sign-in; returns the page to open once the service has given one."""
    item = connector(connector_id)
    if item.auth == "token":
        raise ConnectorError(f"{item.title} connects with a token, not a browser sign-in")
    client_id, client_secret = str(client_id or "").strip(), str(client_secret or "").strip()
    if item.auth == "oauth_app" and not client_id:
        raise ConnectorError(f"enter the Client ID of your {item.title} app")
    _check_name_free(item)
    await cancel_sign_in(item.id)

    redirect = redirect_uri(port)
    storage = KeychainTokenStorage(item.id, pending=True)
    await asyncio.to_thread(_STORE.delete, _pending_account(item.id))  # a fresh sign-in starts clean
    storage._cache = {}
    auth_method = "none"
    if item.auth == "oauth_app":
        from mcp.shared.auth import OAuthClientInformationFull

        auth_method = "client_secret_post" if client_secret else "none"
        await storage.set_client_info(OAuthClientInformationFull(
            client_id=client_id, client_secret=client_secret or None, redirect_uris=[redirect],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method=auth_method, scope=item.scope,
        ))

    flow = SignIn(name=item.id, connector_id=item.id)
    flow.code = asyncio.get_running_loop().create_future()
    _SIGN_INS[item.id] = flow

    async def open_page(url: str) -> None:
        url = _with_params(url, item.authorize_params)
        flow.oauth_state = dict(parse_qsl(urlsplit(url).query)).get("state")
        flow.authorize_url, flow.state = url, "waiting"
        flow.url_ready.set()

    async def wait_for_code() -> Tuple[str, Optional[str]]:
        assert flow.code is not None
        return await asyncio.wait_for(flow.code, SIGN_IN_TIMEOUT_S)

    provider = _auth_provider(item.url, storage, redirect=redirect, scope=item.scope, auth_method=auth_method,
                              redirect_handler=open_page, callback_handler=wait_for_code)
    flow.task = asyncio.create_task(_finish_sign_in(flow, item, provider), name=f"connector-sign-in-{item.id}")
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.wait({flow.task, asyncio.ensure_future(flow.url_ready.wait())},
                                            return_when=asyncio.FIRST_COMPLETED), URL_WAIT_S)
    return flow.public()


async def _finish_sign_in(flow: SignIn, item: Connector, provider: Any) -> None:
    server = external_mcp.ServerConfig(name=item.id, transport="http", url=item.url)
    try:
        async with contextlib.AsyncExitStack() as stack:
            _, tools, _ = await external_mcp.in_time(
                external_mcp._open_session(stack, server, auth=provider), SIGN_IN_TIMEOUT_S + 60,
            )
        await asyncio.to_thread(_keep_pending, item.id)
        await asyncio.to_thread(_save_server, item, "oauth")
        flow.tools, flow.state = len(tools), "connected"
        await _reload()
    except asyncio.CancelledError:
        flow.state = "cancelled"
        await asyncio.to_thread(_STORE.delete, _pending_account(item.id))
        raise
    except BaseException as exc:  # anyio task groups raise ExceptionGroup
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        await asyncio.to_thread(_STORE.delete, _pending_account(item.id))
        if flow.state != "cancelled":
            timed_out = external_mcp.find_exception(exc, asyncio.TimeoutError) is not None
            flow.error = ("The sign-in was not finished in time. Start it again." if timed_out
                          else external_mcp._short_error(exc))
            flow.state = "failed"
            LOG.warning("connector %s sign-in failed: %s", item.id, flow.error)
    finally:
        flow.url_ready.set()


def complete_callback(state: str, code: str, error: str = "") -> Optional[SignIn]:
    """The browser came back: hand the code to the sign-in waiting for this state."""
    for flow in _SIGN_INS.values():
        if flow.state == "waiting" and flow.oauth_state and state and secrets.compare_digest(flow.oauth_state, state):
            if flow.code is not None and not flow.code.done():
                if error or not code:
                    flow.error = f"The service did not allow access ({error or 'no code'})."
                    flow.code.set_exception(ConnectorError(flow.error))
                else:
                    flow.code.set_result((code, state))
            return flow
    return None


async def cancel_sign_in(name: str) -> bool:
    flow = _SIGN_INS.pop(name, None)
    if flow is None:
        return False
    if flow.task is not None and not flow.task.done():
        flow.state = "cancelled"
        flow.task.cancel()
        with contextlib.suppress(BaseException):
            await flow.task
    return True


# ---------------------------------------------------------------------------
# Disconnect

async def _revoke(name: str) -> None:
    """Best effort: tell the service to forget the tokens (RFC 7009) when it says how."""
    storage = KeychainTokenStorage(name)
    data = await asyncio.to_thread(storage.load)
    endpoint = (data.get("oauth_metadata") or {}).get("revocation_endpoint")
    tokens = data.get("tokens") or {}
    info = data.get("client_info") or {}
    token = tokens.get("refresh_token") or tokens.get("access_token")
    if not endpoint or not token or not info.get("client_id"):
        return
    import httpx

    form = {"token": token, "client_id": info["client_id"]}
    if info.get("client_secret"):
        form["client_secret"] = info["client_secret"]
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(endpoint, data=form)


async def disconnect(name: str) -> Dict[str, Any]:
    """Revoke where the service allows it, delete the Keychain items and the server entry."""
    raw = external_mcp.read_raw_config()["mcpServers"].get(name)
    if not isinstance(raw, dict) or not raw.get("catalog"):
        raise ConnectorError(f"{name!r} is not a connected service")
    await cancel_sign_in(name)
    revoked = raw.get("auth") == "oauth"
    if revoked:
        await _revoke(name)
    await asyncio.to_thread(forget, name)
    await asyncio.to_thread(external_mcp.remove_server, name)
    await _reload()
    item = _catalog_or_none(raw.get("catalog"))
    return {"name": name, "revoke_requested": revoked, "manage_url": item.manage_url if item else None}


def catalog_public(port: Optional[int]) -> List[Dict[str, Any]]:
    redirect = redirect_uri(port)
    return [item.public(redirect) for item in CATALOG]


external_mcp.register_credentials(credentials)
