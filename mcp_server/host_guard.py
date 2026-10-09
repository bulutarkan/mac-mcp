"""Reject requests whose Host (or, without authentication, browser Origin) is not ours.

DNS rebinding lets a hostile web page resolve its own name to 127.0.0.1 and
then talk to the loopback server as a "same-origin" page: the request arrives
with Host: attacker.example. A loopback-bound server only ever legitimately
receives loopback names, the configured public endpoint (through the tunnel)
and hosts the owner lists in MAC_MCP_ALLOWED_HOSTS, so anything else is
refused (421) before any route runs, for HTTP and WebSocket alike.

When the server runs without authentication (loopback-only development
mode), a browser Origin must also be loopback, the public endpoint or a
browser extension, because there is no key a hostile page would lack.

A server bound to all interfaces is reached by LAN names the owner chose and
always requires authentication, so it skips the Host allowlist.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Set
from urllib.parse import urlsplit

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
EXTENSION_SCHEMES = ("chrome-extension://", "safari-web-extension://", "moz-extension://")
_PUBLIC_HOST_TTL_S = 10.0


def _hostname(value: str) -> str:
    """Lowercase host without port; handles [ipv6]:port."""
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


class HostGuard:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        enforce_hosts: bool,
        no_auth: bool,
        public_hosts: Callable[[], Iterable[str]],
        extra_hosts: Optional[Iterable[str]] = None,
    ) -> None:
        self.app = app
        self.enforce_hosts = enforce_hosts
        self.no_auth = no_auth
        self._public_hosts = public_hosts
        self._extra = {_hostname(item) for item in (extra_hosts or ()) if _hostname(item)}
        self._cache: Dict[str, Any] = {"at": 0.0, "hosts": set()}

    def allowed_hosts(self) -> Set[str]:
        now = time.monotonic()
        if now - float(self._cache["at"]) > _PUBLIC_HOST_TTL_S:
            try:
                hosts = {_hostname(item) for item in self._public_hosts() if _hostname(item)}
            except Exception:
                hosts = set(self._cache["hosts"])
            self._cache.update(at=now, hosts=hosts)
        return set(LOOPBACK_NAMES) | self._extra | set(self._cache["hosts"])

    def _origin_allowed(self, origin: str, allowed: Set[str]) -> bool:
        if origin.startswith(EXTENSION_SCHEMES):
            return True
        if origin == "null":
            return False
        return _hostname(urlsplit(origin).netloc) in allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        allowed = self.allowed_hosts()
        reason = None
        if self.enforce_hosts and _hostname(headers.get("host", "")) not in allowed:
            reason = "untrusted_host"
        elif self.no_auth and headers.get("origin") and not self._origin_allowed(headers["origin"], allowed):
            reason = "untrusted_origin"
        if reason is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = json.dumps({"detail": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": 421 if reason == "untrusted_host" else 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


def extra_hosts_from_env() -> list[str]:
    return [item.strip() for item in os.getenv("MAC_MCP_ALLOWED_HOSTS", "").split(",") if item.strip()]
