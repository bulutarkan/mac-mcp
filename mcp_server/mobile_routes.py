from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .mobile_auth import DEFAULT_SESSION_TTL_S, PAIR_PREFIX, SESSION_PREFIX, MobileAuthStore
from .observability import TelemetryManager
from .public_endpoint import PublicEndpointError, resolve_public_endpoint
from .request_client import client_address as _client_address, is_direct_local_request
from .security import Settings, dashboard_authorized
from .steering import STEERING_SCHEMA_VERSION, SteeringManager
from .tools_agents import list_agents
from .version import __version__

MOBILE_DIR = Path(__file__).resolve().parent / "mobile"
MOBILE_COOKIE = "mac_mcp_mobile"


def _management_guard(request: Request, dashboard_token: str) -> Optional[Response]:
    if not is_direct_local_request(request):
        return JSONResponse(
            {"detail": "Mobile pairing management is available on localhost only."},
            status_code=403,
        )
    if dashboard_authorized(dashboard_token, request.headers.get("authorization")):
        return None
    return JSONResponse(
        {"detail": "Dashboard authentication required."},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _public_mobile_url() -> Optional[str]:
    try:
        config = resolve_public_endpoint()
    except PublicEndpointError:
        return None
    if not config.endpoint_url:
        return None
    parsed = urlsplit(config.endpoint_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/mobile", "", ""))


def _safe_agent(item: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "agent_id", "team_task_id", "status", "phase", "title", "role",
        "provider", "model", "reasoning", "started_at", "ended_at", "duration_ms",
        "idle_seconds", "last_tool", "tool_call_count", "resource_activity",
    )
    return {key: item.get(key) for key in keys}


def _mobile_agent_rows(items: list[Dict[str, Any]], limit: int = 6) -> list[Dict[str, Any]]:
    active = [item for item in items if item.get("status") in {"starting", "running"}]
    now = time.time()
    recent: list[Dict[str, Any]] = []
    for item in items:
        if item.get("status") in {"starting", "running"}:
            continue
        raw_ts = item.get("ended_at") or item.get("started_at")
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if 0 <= now - ts <= 24 * 60 * 60:
            recent.append(item)

    # Active work is the primary mobile surface. Only same-day history fills
    # any remaining slots so old test/stale agents never dominate the screen.
    cap = max(limit, min(len(active), 10))
    visible = active[:cap]
    if len(visible) < cap:
        visible.extend(recent[: cap - len(visible)])
    return [_safe_agent(item) for item in visible]


def _safe_session(item: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "schema_version", "session_id", "flow_number", "label", "detail", "tool",
        "state", "queued", "activity_state", "lifecycle_state", "last_transition_at",
        "created_at", "last_activity_at", "activity_ms", "active_calls",
        "pending_instruction_count", "awaiting_acknowledgement_count",
    )
    lifecycle = str(item.get("lifecycle_state") or "").strip().lower()
    last_error = str(item.get("last_error") or "").strip()
    safe = {key: item.get(key) for key in keys}
    safe["needs_attention"] = bool(last_error) or lifecycle in {
        "failed", "disconnected", "expired", "unknown",
    }
    return safe


def _safe_recent_session(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if item.get("kind") != "session":
        return None
    lifecycle = str(item.get("lifecycle_state") or "").strip().lower()
    return {
        "session_id": item.get("session_id"),
        "status": item.get("status"),
        "lifecycle_state": item.get("lifecycle_state"),
        "created_at": item.get("created_at"),
        "transitioned_at": item.get("transitioned_at"),
        "tool": item.get("tool"),
        "needs_attention": lifecycle in {"failed", "disconnected", "expired", "unknown"},
    }


def _safe_event(item: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "event_id", "timestamp", "source", "tool", "status", "started_at", "finished_at",
        "duration_ms", "browser_context",
    )
    return {key: item.get(key) for key in keys if key in item}


def create_mobile_routes(
    telemetry: TelemetryManager,
    settings: Settings,
    dashboard_token: str,
    steering: Optional[SteeringManager] = None,
    auth_store: Optional[MobileAuthStore] = None,
) -> list[Route]:
    store = auth_store or MobileAuthStore()
    manual_attempts_by_client: dict[str, list[float]] = {}

    def manual_pairing_rate_limited(request: Request) -> bool:
        now = time.time()
        address = _client_address(request)
        recent = [
            ts for ts in manual_attempts_by_client.get(address, [])
            if now - ts < 60
        ]
        if len(recent) >= 8:
            manual_attempts_by_client[address] = recent
            return True
        recent.append(now)
        manual_attempts_by_client[address] = recent
        return False

    def mobile_session(request: Request) -> Optional[Dict[str, Any]]:
        cookie_token = request.cookies.get(MOBILE_COOKIE)
        session = store.resolve_session(cookie_token)
        if session is not None:
            return session

        authorization = (request.headers.get("authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
            if bearer.startswith(SESSION_PREFIX):
                return store.resolve_session(bearer)
        return None

    def require_mobile(request: Request) -> tuple[Optional[Dict[str, Any]], Optional[Response]]:
        session = mobile_session(request)
        if session is None:
            return None, JSONResponse(
                {"ok": False, "error": "mobile_auth_required"},
                status_code=401,
            )
        return session, None

    async def index(request: Request) -> Response:
        return FileResponse(
            MOBILE_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    async def asset(request: Request) -> Response:
        name = str(request.path_params.get("name") or "")
        allowed = {
            "mobile.css": "text/css; charset=utf-8",
            "mobile.js": "text/javascript; charset=utf-8",
            "manifest.json": "application/manifest+json",
        }
        media_type = allowed.get(name)
        if media_type is None:
            return Response(status_code=404)
        return FileResponse(
            MOBILE_DIR / name,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def pair(request: Request) -> Response:
        content_type = (request.headers.get("content-type") or "").lower()
        form_navigation = "application/x-www-form-urlencoded" in content_type
        if form_navigation:
            raw = (await request.body()).decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=False)
            payload = {
                "code": (parsed.get("code") or [""])[0],
                "device_name": (parsed.get("device_name") or [""])[0],
            }
        else:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

        submitted_code = str(payload.get("code") or "")
        is_manual_code = bool(submitted_code) and not submitted_code.startswith(PAIR_PREFIX)
        if is_manual_code and manual_pairing_rate_limited(request):
            return JSONResponse(
                {"ok": False, "error": "pairing_rate_limited"},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        result = store.consume_pairing(
            submitted_code,
            device_name=payload.get("device_name"),
            session_ttl_s=DEFAULT_SESSION_TTL_S,
        )
        if result is None:
            if form_navigation:
                return RedirectResponse("/mobile?pair_error=expired", status_code=303)
            return JSONResponse(
                {"ok": False, "error": "invalid_or_expired_pairing"},
                status_code=401,
            )

        if form_navigation:
            response = RedirectResponse(
                "/mobile",
                status_code=303,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                },
            )
        else:
            response = JSONResponse(
                {
                    "ok": True,
                    "device": {
                        "device_id": result["device_id"],
                        "device_name": result["device_name"],
                        "created_at": result["created_at"],
                        "expires_at": result["expires_at"],
                    },
                },
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                },
            )

        response.set_cookie(
            MOBILE_COOKIE,
            result["token"],
            max_age=DEFAULT_SESSION_TTL_S,
            expires=datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_SESSION_TTL_S),
            path="/mobile",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    async def status_view(request: Request) -> Response:
        _session, denied = require_mobile(request)
        if denied is not None:
            return denied
        try:
            agent_data = await asyncio.to_thread(list_agents, settings, limit=100)
            agents = list(agent_data.get("agents", []))
        except Exception:
            agents = []
        try:
            summary = telemetry.summary(1)
        except Exception:
            summary = {"total_calls": 0, "success_rate": 100.0}
        try:
            endpoint = resolve_public_endpoint()
            connector = endpoint.mode
        except Exception:
            connector = "none"
        return JSONResponse({
            "ok": True,
            "server": "Mac MCP",
            "version": __version__,
            "connector": connector,
            "calls_1h": int(summary.get("total_calls") or 0),
            "success_rate": float(summary.get("success_rate") or 0.0),
            "active_agents": sum(
                1 for row in agents if row.get("status") in {"starting", "running"}
            ),
        })

    async def agents_view(request: Request) -> Response:
        _session, denied = require_mobile(request)
        if denied is not None:
            return denied
        try:
            data = await asyncio.to_thread(list_agents, settings, limit=100)
            all_rows = list(data.get("agents", []))
            rows = _mobile_agent_rows(all_rows, limit=6)
            active_count = sum(
                1 for row in all_rows if row.get("status") in {"starting", "running"}
            )
        except Exception:
            rows, active_count = [], 0
        return JSONResponse({
            "ok": True,
            "count": len(rows),
            "active_count": active_count,
            "agents": rows,
        })

    async def sessions_view(request: Request) -> Response:
        _session, denied = require_mobile(request)
        if denied is not None:
            return denied
        if steering is None:
            return JSONResponse({
                "ok": True,
                "schema_version": STEERING_SCHEMA_VERSION,
                "count": 0,
                "sessions": [],
                "recent": [],
                "session_ttl_minutes": 10,
            })
        rows = [_safe_session(row) for row in steering.sessions()[:12]]
        recent = [
            safe for safe in (_safe_recent_session(row) for row in steering.recent(30))
            if safe is not None
        ][:12]
        return JSONResponse({
            "ok": True,
            "schema_version": STEERING_SCHEMA_VERSION,
            "count": len(rows),
            "sessions": rows,
            "recent": recent,
            "session_ttl_minutes": steering.session_ttl_minutes,
        })

    async def activity_view(request: Request) -> Response:
        _session, denied = require_mobile(request)
        if denied is not None:
            return denied
        try:
            events = telemetry.query_events(hours=1, limit=12)
            active = telemetry.active_calls()
        except Exception:
            events, active = [], []
        return JSONResponse({
            "ok": True,
            "events": [_safe_event(row) for row in events[:8]],
            "active": [_safe_event(row) for row in active[:6]],
        })

    async def usage_view(request: Request) -> Response:
        _session, denied = require_mobile(request)
        if denied is not None:
            return denied
        try:
            days = int(request.query_params.get("days") or 365)
        except ValueError:
            days = 365
        days = max(1, min(days, 400))
        actor = str(request.query_params.get("actor") or "all").strip().lower()
        actor_class = actor if actor in {"primary", "scoped_subagent"} else None
        try:
            payload = await asyncio.to_thread(
                telemetry.usage_summary,
                days=days,
                actor_class=actor_class,
            )
        except Exception:
            return JSONResponse(
                {
                    "ok": False,
                    "days": days,
                    "actor_class": actor_class or "all",
                    "daily": [],
                    "top_tools": [],
                    "error": "usage_unavailable",
                },
                status_code=503,
            )
        return JSONResponse(payload)

    async def create_pairing(request: Request) -> Response:
        denied = _management_guard(request, dashboard_token)
        if denied is not None:
            return denied
        base = _public_mobile_url()
        if not base:
            return JSONResponse(
                {"ok": False, "error": "public_https_endpoint_required"},
                status_code=409,
            )
        issued = store.issue_pairing()
        pair_url = base + "?pair=1#pair=" + issued["code"]
        return JSONResponse({
            "ok": True,
            "pair_url": pair_url,
            "manual_code": issued["manual_code"],
            "mobile_url": base,
            "expires_at": issued["expires_at"],
        })

    async def devices(request: Request) -> Response:
        denied = _management_guard(request, dashboard_token)
        if denied is not None:
            return denied
        return JSONResponse({"ok": True, "devices": store.list_devices()})

    async def revoke(request: Request) -> Response:
        denied = _management_guard(request, dashboard_token)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            return JSONResponse(
                {"ok": False, "error": "device_id_required"},
                status_code=400,
            )
        return JSONResponse({"ok": True, "revoked": store.revoke(device_id)})

    return [
        Route("/mobile", index, methods=["GET"]),
        Route("/mobile/", index, methods=["GET"]),
        Route("/mobile/assets/{name}", asset, methods=["GET"]),
        Route("/mobile/pair", pair, methods=["POST"]),
        Route("/mobile/api/status", status_view, methods=["GET"]),
        Route("/mobile/api/agents", agents_view, methods=["GET"]),
        Route("/mobile/api/sessions", sessions_view, methods=["GET"]),
        Route("/mobile/api/activity", activity_view, methods=["GET"]),
        Route("/mobile/api/usage", usage_view, methods=["GET"]),
        Route("/dashboard/api/mobile/pairings", create_pairing, methods=["POST"]),
        Route("/dashboard/api/mobile/devices", devices, methods=["GET"]),
        Route("/dashboard/api/mobile/revoke", revoke, methods=["POST"]),
    ]
