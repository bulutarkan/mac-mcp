from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpcore
import httpx
from fastapi import HTTPException, status
from httpcore import SyncBackend

from .security import (
    Settings, _address_is_blocked, _private_host_allowed, resolve_host_addresses,
    truncate, validate_url,
)

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_MAX_REDIRECTS = 10


class _RevalidatingSyncBackend(SyncBackend):
    """Resolve and pin every TCP connection after enforcing the private-network boundary."""

    def __init__(self, private_allowlist: list[str]) -> None:
        super().__init__()
        self._private_allowlist = list(private_allowlist)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        hostname = host.decode("ascii") if isinstance(host, bytes) else str(host)
        addresses = resolve_host_addresses(hostname)
        private_exception = _private_host_allowed(hostname, self._private_allowlist)
        blocked = [ip for ip in addresses if _address_is_blocked(ip)]
        if blocked and not private_exception:
            # Raising an httpcore ConnectError keeps the network boundary inside the
            # transport and is translated by httpx into RequestError.
            raise httpcore.ConnectError("destination resolved to local/private/non-global address")
        # Connect only to already-validated IP literals. Keep public multi-address
        # fallback without asking DNS again; TLS is upgraded by httpcore afterward
        # with the original origin hostname/SNI.
        last_error: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("destination resolved to no usable validated address")


class _SafeHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport with connect-time DNS revalidation and IP pinning."""

    def __init__(self, settings: Settings) -> None:
        # Environment proxies could otherwise move SSRF enforcement to a proxy that
        # may have access to private networks, so host requests never inherit them.
        super().__init__(trust_env=False, retries=0)
        pool = getattr(self, "_pool", None)
        if pool is None or not hasattr(pool, "_network_backend"):
            raise RuntimeError("Installed httpx/httpcore transport does not expose the required network backend hook")
        pool._network_backend = _RevalidatingSyncBackend(settings.http_private_allowlist)


def _safe_origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _read_response_bytes(resp: httpx.Response, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_bytes():
        remaining = limit - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def http_request(
    settings: Settings,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
) -> Dict[str, Any]:
    """Make an outbound HTTP request with per-hop SSRF and connect-time DNS validation."""
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Method must be one of: {', '.join(sorted(_ALLOWED_METHODS))}")
    validate_url(settings, url)
    headers = headers or {}
    start = time.perf_counter()
    redirect_chain: list[str] = []
    final_resp: Optional[httpx.Response] = None
    content_bytes = b""
    truncated = False

    try:
        transport = _SafeHTTPTransport(settings)
        with httpx.Client(
            timeout=httpx.Timeout(settings.http_timeout_s), follow_redirects=False,
            transport=transport, trust_env=False, max_redirects=_MAX_REDIRECTS,
        ) as client:
            request = client.build_request(
                method, url, headers=headers,
                content=body.encode() if isinstance(body, str) else body,
            )
            for hop in range(_MAX_REDIRECTS + 1):
                current_url = str(request.url)
                validate_url(settings, current_url)
                resp = client.send(request, stream=True, follow_redirects=False)
                final_resp = resp
                if resp.is_redirect and resp.headers.get("location"):
                    next_request = resp.next_request
                    resp.close()
                    if next_request is None:
                        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Redirect response could not be resolved safely.")
                    next_url = str(next_request.url)
                    validate_url(settings, next_url)
                    redirect_chain.append(_safe_origin(next_url))
                    if hop >= _MAX_REDIRECTS:
                        raise HTTPException(status.HTTP_508_LOOP_DETECTED, "Too many HTTP redirects.")
                    request = next_request
                    continue
                try:
                    content_bytes, truncated = _read_response_bytes(resp, settings.http_max_response_bytes)
                finally:
                    resp.close()
                break
            else:  # pragma: no cover - loop has explicit bound
                raise HTTPException(status.HTTP_508_LOOP_DETECTED, "Too many HTTP redirects.")
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"HTTP request failed: {e}") from e

    if final_resp is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "HTTP request produced no response.")
    duration_ms = int((time.perf_counter() - start) * 1000)
    text = content_bytes.decode(final_resp.encoding or "utf-8", errors="replace")
    bounded, _ = truncate(text, settings.max_output_chars)

    return {
        "ok": 200 <= final_resp.status_code < 400,
        "status": final_resp.status_code,
        "headers": dict(final_resp.headers),
        "text": bounded,
        "truncated": truncated,
        "duration_ms": duration_ms,
        "url": str(final_resp.url),
        "method": method,
        "redirect_count": len(redirect_chain),
        "redirect_chain": redirect_chain,
    }
