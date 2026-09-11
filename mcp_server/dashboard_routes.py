from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .observability import TelemetryManager, sanitize_value
from .policy import RISK_REGISTRY
from .security import Settings
from .steering import SteeringManager
from .tools_agents import list_agents
from .version import __version__

DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
REST_TOOL_ALIASES = {
    "/run": "run_command",
    "/system_info": "get_system_info",
    "/process_list": "process_list",
    "/kill_process": "kill_process",
    "/jobs/start": "start_background_job",
    "/jobs/status": "get_job_status",
    "/jobs/output": "get_job_output",
    "/jobs/stop": "stop_job",
    "/jobs/list": "list_jobs",
    "/jobs/wait": "wait_jobs",
    "/run_parallel": "run_commands_parallel",
    "/http": "http_request",
    "/interactive": "ask_user",
    "/interactive/choice": "ask_choice",
    "/interactive/confirmation": "ask_confirmation",
}


def _client_address(request: Request) -> str:
    # A tunnel/proxy must not be able to make a remote client look local. If a
    # forwarding header exists, the original first-hop address is authoritative.
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        raw = request.headers.get(header)
        if raw:
            return raw.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _is_loopback(address: str) -> bool:
    if address in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _local_only(request: Request) -> Optional[Response]:
    if _is_loopback(_client_address(request)):
        return None
    if request.url.path.startswith("/dashboard/api/") or request.url.path == "/dashboard/events":
        return JSONResponse({"detail": "The Mac MCP dashboard is available on localhost only."}, status_code=403)
    return HTMLResponse("Dashboard is available on localhost only.", status_code=403)


def _float_query(request: Request, key: str, default: float) -> float:
    try:
        return float(request.query_params.get(key, str(default)))
    except ValueError:
        return default


def _int_query(request: Request, key: str, default: int) -> int:
    try:
        return int(request.query_params.get(key, str(default)))
    except ValueError:
        return default


def create_dashboard_routes(telemetry: TelemetryManager, settings: Settings, steering: Optional[SteeringManager] = None) -> list[Route]:
    async def index(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        path = DASHBOARD_DIR / "index.html"
        return FileResponse(path, media_type="text/html; charset=utf-8")

    async def asset(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        name = request.path_params.get("name", "")
        allowed = {"dashboard.css": "text/css; charset=utf-8", "dashboard.js": "text/javascript; charset=utf-8"}
        if name not in allowed:
            return Response(status_code=404)
        return FileResponse(DASHBOARD_DIR / name, media_type=allowed[name])

    async def summary(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        hours = _float_query(request, "hours", 24)
        payload = telemetry.summary(hours)
        try:
            agents = list_agents(settings, limit=50).get("agents", [])
        except Exception:
            agents = []
        payload.update({
            "server": "Mac MCP",
            "version": __version__,
            "local_only": True,
            "agent_count": len(agents),
            "active_agents": sum(1 for agent in agents if agent.get("status") in {"starting", "running"}),
        })
        return JSONResponse(payload)

    async def events(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        payload = telemetry.query_events(
            hours=_float_query(request, "hours", 24),
            limit=_int_query(request, "limit", 120),
            source=request.query_params.get("source"),
            status=request.query_params.get("status"),
            tool=request.query_params.get("tool"),
        )
        return JSONResponse({"events": payload, "active": telemetry.active_calls()})

    async def agents(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        try:
            data = list_agents(settings, limit=max(1, min(_int_query(request, "limit", 20), 100)))
        except Exception as exc:
            return JSONResponse({"ok": False, "count": 0, "agents": [], "error": str(sanitize_value(exc))})
        public_agents = []
        for item in data.get("agents", []):
            public_agents.append({
                key: item.get(key) for key in (
                    "agent_id", "team_id", "status", "phase", "title", "provider", "model", "reasoning",
                    "access_mode", "started_at", "ended_at", "duration_ms", "first_event_latency_ms",
                    "idle_seconds", "step_count", "tool_call_count", "last_tool", "last_tool_duration_ms",
                    "retry_count", "output_tokens", "result_preview",
                )
            })
        return JSONResponse({"ok": True, "count": len(public_agents), "agents": public_agents})

    async def steering_state(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        if steering is None:
            return JSONResponse({"ok": True, "sessions": [], "recent": [], "session_ttl_minutes": 10})
        return JSONResponse({
            "ok": True,
            "sessions": steering.sessions(),
            "recent": steering.recent(30),
            "session_ttl_minutes": steering.session_ttl_minutes,
        })

    async def steering_send(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        if steering is None:
            return JSONResponse({"ok": False, "error": "steering_unavailable"}, status_code=503)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        session_id = str(payload.get("session_id") or "").strip()
        text = str(payload.get("text") or "")
        if not session_id:
            return JSONResponse({"ok": False, "error": "session_required"}, status_code=400)
        try:
            message = steering.enqueue(session_id, text)
        except KeyError:
            return JSONResponse({"ok": False, "error": "session_closed"}, status_code=409)
        except OverflowError:
            return JSONResponse({"ok": False, "error": "queue_full"}, status_code=409)
        except ValueError as exc:
            code = str(exc) or "invalid_message"
            return JSONResponse({"ok": False, "error": code}, status_code=400)
        return JSONResponse({
            "ok": True,
            "status": "queued",
            "message": {
                "id": message["id"],
                "session_id": message["session_id"],
                "created_at": message["created_at"],
                "status": message["status"],
                "session_state": message.get("session_state"),
            },
        })


    async def steering_settings(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        if steering is None:
            return JSONResponse({"ok": False, "error": "steering_unavailable"}, status_code=503)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        try:
            minutes = int(payload.get("session_ttl_minutes"))
            minutes = steering.set_session_ttl_minutes(minutes)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "session_ttl_must_be_positive"}, status_code=400)
        return JSONResponse({"ok": True, "session_ttl_minutes": minutes})

    async def stream(request: Request) -> Response:
        denied = _local_only(request)
        if denied:
            return denied
        queue = telemetry.subscribe()

        async def generator():
            try:
                hello = {"kind": "connected", "active": telemetry.active_calls()}
                yield "event: telemetry\ndata: " + json.dumps(hello, ensure_ascii=False) + "\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield "event: telemetry\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                telemetry.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return [
        Route("/dashboard", index, methods=["GET"]),
        Route("/dashboard/assets/{name}", asset, methods=["GET"]),
        Route("/dashboard/api/summary", summary, methods=["GET"]),
        Route("/dashboard/api/events", events, methods=["GET"]),
        Route("/dashboard/api/agents", agents, methods=["GET"]),
        Route("/dashboard/api/steering", steering_state, methods=["GET"]),
        Route("/dashboard/api/steering", steering_send, methods=["POST"]),
        Route("/dashboard/api/steering/settings", steering_settings, methods=["POST"]),
        Route("/dashboard/events", stream, methods=["GET"]),
    ]


async def rest_telemetry_middleware(request: Request, call_next, telemetry: TelemetryManager):
    """Capture legacy REST/OpenAPI usage without changing route handlers."""
    raw_body = await request.body()
    payload: Dict[str, Any] = {}
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            payload = parsed if isinstance(parsed, dict) else {"body": parsed}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"body": raw_body.decode("utf-8", errors="replace")}

    rest_path = request.url.path.removeprefix("/api").rstrip("/") or "/"
    path_tool = rest_path.rsplit("/", 1)[-1] or "rest"
    # One-to-one aliases and operation paths are authoritative. Grouped legacy
    # endpoints are renamed after their validated dispatcher selects a tool.
    tool_name = str(REST_TOOL_ALIASES.get(rest_path) or (
        path_tool if path_tool in RISK_REGISTRY else f"rest{rest_path.replace('/', '.')}"
    ))
    arguments = dict(payload)
    arguments["http_method"] = request.method
    arguments["path"] = request.url.path
    event_id = telemetry.start_call("rest", tool_name, arguments)

    # Starlette's BaseHTTP middleware consumes request.body(); replay it once for FastAPI.
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}
    request._receive = receive  # type: ignore[attr-defined]

    try:
        response = await call_next(request)
    except BaseException as exc:
        telemetry.finish_call(event_id, error=exc)
        raise
    policy_tool = getattr(request.state, "policy_tool", None)
    policy_fields = getattr(request.state, "policy_metadata", None)
    telemetry.update_context(event_id, tool=policy_tool, metadata=policy_fields)
    policy_denied = bool(
        isinstance(policy_fields, dict)
        and policy_fields.get("policy_decision") == "profile_denied"
    )
    result = {
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type"),
    }
    if policy_denied:
        result.update({"ok": False, "denied": True, "error": "profile_denied"})
    telemetry.finish_call(
        event_id,
        result=result,
        error=None if response.status_code < 400 or policy_denied else f"HTTP {response.status_code}",
    )
    return response
