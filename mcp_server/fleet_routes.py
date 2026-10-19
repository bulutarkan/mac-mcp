"""HTTP side of the fleet (#147).

/fleet/... is what another Mac calls on this one, over this Mac's public address: it carries no Mac MCP
API key, so each route authenticates on its own (an invitation code, or the credential this Mac issued)
and failed attempts are limited per source. /dashboard/api/fleet/... is the owner's: local and
dashboard-token only, like every dashboard route.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import fleet, fleet_client
from .observability import publish_live_event
from .request_client import client_address
from .security import AuthFailureLimiter

RecipeRunner = Callable[..., Awaitable[Dict[str, Any]]]
_RECIPE_ID = re.compile(r"rcp_[0-9a-f]{12}")


_BUSY = {"ok": False, "error": "fleet_busy", "message": "This Mac is set to Busy; it takes nothing from other Macs now."}


def _error(exc: fleet.FleetError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.code.lower(), "message": str(exc)}, status_code=exc.status)


async def _json_body(request: Request) -> Optional[Dict[str, Any]]:
    if "application/json" not in (request.headers.get("content-type") or ""):
        return None
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def create_fleet_routes(dashboard_guard: Callable[[Request], Optional[Response]],
                        recipe_runner: Optional[RecipeRunner] = None) -> List[Route]:
    pair_attempts: Dict[str, List[float]] = {}
    auth_failures = AuthFailureLimiter(limit_per_minute=20)

    def pairing_limited(request: Request) -> bool:
        now = time.time()
        address = client_address(request)
        recent = [stamp for stamp in pair_attempts.get(address, []) if now - stamp < 60]
        limited = len(recent) >= 8
        if not limited:
            recent.append(now)
        pair_attempts[address] = recent
        if len(pair_attempts) > 4096:  # bounded: forget the quietest sources
            for key in sorted(pair_attempts, key=lambda k: max(pair_attempts[k] or [0]))[:1024]:
                pair_attempts.pop(key, None)
        return limited

    def caller(request: Request) -> tuple[Optional[Dict[str, Any]], Optional[Response]]:
        address = client_address(request)
        if auth_failures.blocked(address):
            return None, JSONResponse({"ok": False, "error": "fleet_rate_limited"}, status_code=429,
                                      headers={"Retry-After": "60"})
        header = (request.headers.get("authorization") or "").strip()
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        client = fleet.store().client_for(token)
        if client is None:
            auth_failures.record_failure(address)
            return None, JSONResponse({"ok": False, "error": "fleet_auth_required"}, status_code=401)
        return client, None

    # ── What another Mac calls on this one ────────────────────────────────────
    async def pair(request: Request) -> Response:
        if pairing_limited(request):
            return JSONResponse({"ok": False, "error": "fleet_rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=415)
        me = fleet.identity()
        if not me["available"]:
            return JSONResponse(_BUSY, status_code=423)
        try:
            made = fleet.store().request_access(
                str(body.get("code") or ""), client_device_id=str(body.get("device_id") or ""),
                client_name=str(body.get("name") or ""), server_device_id=me["device_id"],
                source=client_address(request))
        except fleet.FleetError as exc:
            return _error(exc)
        publish_live_event("fleet_request")  # the menu app shows a notification for it
        return JSONResponse({"ok": True, **made, "server": {key: me[key] for key in ("device_id", "name", "model")}})

    async def pair_status(request: Request) -> Response:
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=415)
        address = client_address(request)
        if auth_failures.blocked(address):
            return JSONResponse({"ok": False, "error": "fleet_rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        try:
            result = fleet.store().poll_request(str(body.get("request_id") or ""), str(body.get("poll_secret") or ""))
        except fleet.FleetError as exc:
            auth_failures.record_failure(address)
            return _error(exc)
        return JSONResponse({"ok": True, **result})

    async def status(request: Request) -> Response:
        client, denied = caller(request)
        if denied:
            return denied
        return JSONResponse({"ok": True, **fleet.own_status(client["permission"])})

    async def recipes_list(request: Request) -> Response:
        client, denied = caller(request)
        if denied:
            return denied
        if client["permission"] not in ("read_only", "full"):
            return JSONResponse({"ok": False, "error": "fleet_permission_denied",
                                 "message": "This Mac lets yours see its status only."}, status_code=403)
        if not fleet.identity()["available"]:
            return JSONResponse(_BUSY, status_code=423)
        from .recipes import list_recipes

        keep = ("recipe_id", "name", "description", "status", "parameters", "consequential", "run_count", "last_run")
        rows = [{key: item.get(key) for key in keep} for item in list_recipes(include_drafts=False).get("recipes", [])
                if item.get("status") == "active"]
        return JSONResponse({"ok": True, "count": len(rows), "recipes": rows})

    async def recipes_run(request: Request) -> Response:
        client, denied = caller(request)
        if denied:
            return denied
        if client["permission"] != "full":
            return JSONResponse({"ok": False, "error": "fleet_permission_denied",
                                 "message": "Running recipes here needs full access."}, status_code=403)
        if not fleet.identity()["available"]:
            return JSONResponse(_BUSY, status_code=423)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=415)
        recipe_id = str(body.get("recipe_id") or "")
        values = body.get("values") or {}
        if not _RECIPE_ID.fullmatch(recipe_id) or not isinstance(values, dict) or len(values) > 20 \
                or any(not isinstance(v, (str, int, float, bool)) for v in values.values()):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        if recipe_runner is None:
            return JSONResponse({"ok": False, "error": "fleet_recipes_unavailable"}, status_code=503)
        # This Mac's own permission profile and approvals decide, as for any recipe run here.
        result = await recipe_runner(recipe_id, values, actor=f"fleet:{client['name']}",
                                     description=f"Run a saved recipe for {client['name']}")
        return JSONResponse({"ok": bool(result.get("ok")), "result": result})

    async def forget(request: Request) -> Response:
        client, denied = caller(request)
        if denied:
            return denied
        fleet.store().revoke_client(client["client_id"])
        return JSONResponse({"ok": True})

    # ── The owner's ──────────────────────────────────────────────────────────
    async def overview(request: Request) -> Response:
        denied = dashboard_guard(request)
        if denied:
            return denied
        await fleet_client.refresh_all(force=request.query_params.get("refresh") == "1")
        store = fleet.store()
        try:
            address = fleet.own_url()
        except fleet.FleetError:
            address = ""
        from .policy import permission_profile_name

        return JSONResponse({
            "ok": True, "this_mac": {**fleet.identity(), "url": address, "profile": permission_profile_name()},
            "invitation": store.active_offer(), "requests": store.pending_requests(),
            "clients": [{**row, "access": fleet.effective_access(row["permission"])} for row in store.clients()],
            "peers": [fleet.public_peer(row) for row in store.peers()],
        })

    async def pending(request: Request) -> Response:
        """Just the Macs waiting to be allowed, without asking the paired Macs how they are."""
        denied = dashboard_guard(request)
        if denied:
            return denied
        return JSONResponse({"ok": True, "requests": fleet.store().pending_requests()})

    async def owner_body(request: Request) -> tuple[Optional[Dict[str, Any]], Optional[Response]]:
        denied = dashboard_guard(request)
        if denied:
            return None, denied
        body = await _json_body(request)
        if body is None:
            return None, JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        return body, None

    async def invite(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        try:
            address = fleet.own_url()
        except fleet.FleetError as exc:
            return _error(exc)
        if not address:
            return JSONResponse({"ok": False, "error": "public_https_endpoint_required",
                                 "message": "Other Macs reach this one at its public address: turn on Cloudflare, "
                                            "ngrok or a custom HTTPS URL in Settings › Connections first."},
                                status_code=409)
        try:
            offer = fleet.store().issue_offer(str(body.get("permission") or "status"))
        except fleet.FleetError as exc:
            return _error(exc)
        return JSONResponse({"ok": True, "link": f"{address}/fleet#pair={offer['token']}", "address": address,
                             "code": offer["code"], "permission": offer["permission"],
                             "expires_at": offer["expires_at"]})

    async def cancel_invite(request: Request) -> Response:
        _, denied = await owner_body(request)
        if denied:
            return denied
        fleet.store().cancel_offer()
        return JSONResponse({"ok": True})

    async def decide(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        try:
            result = fleet.store().decide(str(body.get("request_id") or ""), allow=body.get("allow") is True)
        except fleet.FleetError as exc:
            return _error(exc)
        publish_live_event("fleet_request_closed")
        return JSONResponse({"ok": True, **result})

    async def revoke(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        if not fleet.store().revoke_client(str(body.get("client_id") or "")):
            return JSONResponse({"ok": False, "error": "fleet_client_unknown"}, status_code=404)
        return JSONResponse({"ok": True})

    async def add(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        try:
            peer = await fleet_client.add(str(body.get("invitation") or ""))
        except fleet.FleetError as exc:
            return _error(exc)
        return JSONResponse({"ok": True, "peer": peer})

    async def remove(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        try:
            await fleet_client.remove(str(body.get("peer_id") or ""))
        except fleet.FleetError as exc:
            return _error(exc)
        return JSONResponse({"ok": True})

    async def availability(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        return JSONResponse({"ok": True, "this_mac": fleet.set_available(body.get("available") is not False)})

    async def rename(request: Request) -> Response:
        body, denied = await owner_body(request)
        if denied:
            return denied
        return JSONResponse({"ok": True, "this_mac": fleet.rename(str(body.get("name") or ""))})

    return [
        Route("/fleet/pair", pair, methods=["POST"]),
        Route("/fleet/pair/status", pair_status, methods=["POST"]),
        Route("/fleet/status", status, methods=["GET"]),
        Route("/fleet/recipes", recipes_list, methods=["GET"]),
        Route("/fleet/recipes/run", recipes_run, methods=["POST"]),
        Route("/fleet/forget", forget, methods=["POST"]),
        Route("/dashboard/api/fleet", overview, methods=["GET"]),
        Route("/dashboard/api/fleet/requests", pending, methods=["GET"]),
        Route("/dashboard/api/fleet/invite", invite, methods=["POST"]),
        Route("/dashboard/api/fleet/invite/cancel", cancel_invite, methods=["POST"]),
        Route("/dashboard/api/fleet/requests/decide", decide, methods=["POST"]),
        Route("/dashboard/api/fleet/clients/revoke", revoke, methods=["POST"]),
        Route("/dashboard/api/fleet/peers/add", add, methods=["POST"]),
        Route("/dashboard/api/fleet/peers/remove", remove, methods=["POST"]),
        Route("/dashboard/api/fleet/name", rename, methods=["POST"]),
        Route("/dashboard/api/fleet/availability", availability, methods=["POST"]),
    ]
