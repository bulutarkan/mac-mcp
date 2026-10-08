from __future__ import annotations

import ipaddress

from starlette.requests import Request

from .public_endpoint import PublicEndpointError, resolve_public_endpoint


def is_loopback(address: str) -> bool:
    if address in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        name in {"forwarded", "cf-connecting-ip", "cf-connecting-ipv6", "cf-ray", "x-real-ip"}
        or name.startswith("x-forwarded-")
        for name in request.headers
    )


def is_direct_local_request(request: Request) -> bool:
    """Forwarded addresses must never grant access to local management routes.

    Uvicorn may already have replaced scope.client using X-Forwarded-For. Keep
    the header check even when that rewritten address happens to be loopback.
    A configured tunnel and a loopback peer do not prove which process sent a
    request, so no forwarded request is accepted as direct local traffic.
    """
    return bool(request.client and is_loopback(request.client.host) and not _has_forwarding_headers(request))


def client_address(request: Request) -> str:
    """Resolve an address for rate limiting, never for local authorization."""
    peer = request.client.host if request.client else "unknown"
    if not is_loopback(peer) or not _has_forwarding_headers(request):
        return peer

    try:
        mode = resolve_public_endpoint().mode
    except PublicEndpointError:
        return "unknown"

    if mode == "ngrok":
        # ngrok appends its client hop; prefixes (and other providers' headers)
        # may have been supplied by the caller. Include duplicate field lines.
        values = request.headers.getlist("x-forwarded-for")
        address = ",".join(values).rsplit(",", 1)[-1].strip()
    elif mode == "cloudflare":
        values = request.headers.getlist("cf-connecting-ip")
        address = values[0].strip() if len(values) == 1 else ""
    else:
        # No provider-specific forwarding contract is configured.
        return peer

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if parsed.is_loopback or parsed.is_unspecified or (parsed.version == 6 and parsed.ipv4_mapped and parsed.ipv4_mapped.is_loopback):
        return "unknown"
    return str(parsed)
