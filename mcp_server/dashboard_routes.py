from __future__ import annotations

import asyncio
import json
import sqlite3
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import decision_engine
from .chrome_background_bridge import chrome_background_bridge
from .foreground_guard import foreground_authorization
from .file_transactions import (
    FileTransactionError,
    TransactionConflict,
    TransactionExpired,
    TransactionIrreversible,
    TransactionNotFound,
    TransactionRestoreFailed,
    get_transaction,
    recent_transactions,
    transaction_history_item,
    undo_transaction,
)
from .observability import TelemetryManager, sanitize_value
from .permission_probe import probe_permissions
from .provider_usage import clear as provider_usage_clear, summary as provider_usage_summary
from .policy import (
    GLOBAL_PROFILE_NAMES,
    RISK_REGISTRY,
    SERVER_APPROVAL_PROFILE_NAMES,
    is_global_permission_profile,
    permission_semantics,
)
from .runtime_settings import MEMORY_RETENTION_CHOICES, USAGE_RETENTION_CHOICES, update_runtime_setting, usage_privacy
from .tools_memory import memory_clear, memory_export_all, memory_overview
from .usage_metering import clear_usage
from . import recipes
from .request_client import client_address as _client_address, is_direct_local_request, is_loopback as _is_loopback
from .security import Settings, dashboard_authorized
from .security_context import SecurityContextManager
from .steering import (
    STEERING_SCHEMA_VERSION,
    SteeringGenerationMismatch,
    SteeringIdempotencyConflict,
    SteeringIdempotencyExpired,
    SteeringManager,
)
from .tools_agents import agent_action, agent_catalog, dashboard_team_summary, list_agents, provider_overview
from .tools_browser import browser_activate_tab
from .version import __version__

DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
PERMISSION_ENV_FILE = Path(__file__).resolve().parent / ".env"

_BROWSER_ACTIONS = {
    "browser_open_url": "Opened tab",
    "browser_list_tabs": "Listed tabs",
    "browser_activate_tab": "Selected tab",
    "browser_close_tab": "Closed tab",
    "browser_observe": "Observing",
    "browser_find": "Finding element",
    "browser_act": "Interacting",
    "browser_do": "Browser task",
    "browser_execute_js": "Running page script",
    "browser_click_selector": "Clicking",
    "browser_type_selector": "Typing",
    "browser_wait_for_selector": "Waiting",
    "browser_get_html": "Reading page",
    "browser_wait_for_download": "Waiting for download",
    "browser_screenshot": "Capturing page",
    "browser_scroll": "Scrolling",
    "browser_press_key": "Pressing key",
    "browser_coordinate_click": "Clicking",
    "browser_get_snapshot": "Inspecting page",
}


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _browser_site(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname.removeprefix("www.")[:120]


def browser_event_context(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return minimal browser metadata suitable for compact local UI surfaces.

    Deliberately excludes URL paths, query strings, page titles, selectors and
    page content. The full sanitized event remains available to the localhost
    dashboard; this projection is for glanceable menu-bar visibility only.
    """
    tool = str(event.get("tool") or "")
    if not tool.startswith("browser_"):
        return None
    arguments = _mapping(event.get("arguments"))
    result = _mapping(event.get("result"))
    opened = _mapping(result.get("opened"))
    state = _mapping(result.get("state"))

    browser = str(arguments.get("browser") or result.get("browser") or "").strip() or None
    handle = str(
        arguments.get("tab_handle")
        or result.get("tab_handle")
        or opened.get("tab_handle")
        or state.get("tab_handle")
        or ""
    ).strip() or None
    site = None
    for candidate in (arguments.get("url"), result.get("url"), opened.get("url"), state.get("url")):
        site = _browser_site(candidate)
        if site:
            break
    return {
        "browser": browser,
        "tab_handle": handle,
        "site": site,
        "action": _BROWSER_ACTIONS.get(tool, tool.removeprefix("browser_").replace("_", " ").title()),
    }


def _with_browser_context(event: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(event)
    context = browser_event_context(item)
    if context is not None:
        item["browser_context"] = context
    return item
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


def _local_only(request: Request) -> Optional[Response]:
    if is_direct_local_request(request):
        return None
    if request.url.path.startswith("/dashboard/api/") or request.url.path == "/dashboard/events":
        return JSONResponse({"detail": "The Mac MCP dashboard is available on localhost only."}, status_code=403)
    return HTMLResponse("Dashboard is available on localhost only.", status_code=403)


def _dashboard_guard(request: Request, dashboard_token: str) -> Optional[Response]:
    denied = _local_only(request)
    if denied is not None:
        return denied
    if dashboard_authorized(dashboard_token, request.headers.get("authorization")):
        return None
    return JSONResponse(
        {"detail": "Dashboard authentication required."},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


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


def _persist_permission_profile(profile: str, env_file: Path = PERMISSION_ENV_FILE) -> None:
    profile = str(profile or "").strip().lower()
    if not is_global_permission_profile(profile):
        raise ValueError(f"permission profile is not a global server preset: {profile or '<empty>'}")
    key = "MAC_MCP_PERMISSION_PROFILE"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    lines = existing.splitlines()
    replacement = f"{key}={profile}"
    replaced = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not replaced and (stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}=")):
            output.append(replacement)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(replacement)
    text = "\n".join(output).rstrip("\n") + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-env-", dir=str(env_file.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_file)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def create_dashboard_routes(
    telemetry: TelemetryManager, settings: Settings, dashboard_token: str,
    steering: Optional[SteeringManager] = None, security_context: Optional[SecurityContextManager] = None,
    recipe_runner: Optional[Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
) -> list[Route]:
    agent_cache: Dict[str, Any] = {"at": 0.0, "limit": 0, "data": None}
    team_summary_cache: Dict[str, Dict[str, Any]] = {}
    team_summary_cache_ttl_s = 4.0

    def cached_team_summary(team_id: str) -> Dict[str, Any]:
        now = time.monotonic()
        cached = team_summary_cache.get(team_id)
        if cached is not None and now - float(cached.get("at") or 0.0) < team_summary_cache_ttl_s:
            return dict(cached.get("data") or {})
        data = dashboard_team_summary(team_id)
        team_summary_cache[team_id] = {"at": now, "data": dict(data)}
        if len(team_summary_cache) > 32:
            stale = sorted(
                team_summary_cache.items(),
                key=lambda item: float(item[1].get("at") or 0.0),
            )[:-24]
            for stale_team_id, _ in stale:
                team_summary_cache.pop(stale_team_id, None)
        return data

    def cached_agents(limit: int) -> Dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        now = time.monotonic()
        cached = agent_cache.get("data")
        if cached is not None and now - float(agent_cache.get("at") or 0) < 1.0 and int(agent_cache.get("limit") or 0) >= bounded:
            items = list(cached.get("agents", []))[:bounded]
            return {
                "ok": True, "count": len(items), "agents": items,
                "global_admission": cached.get("global_admission"),
            }
        data = list_agents(settings, limit=max(50, bounded))
        agent_cache.update({"at": now, "limit": max(50, bounded), "data": data})
        items = list(data.get("agents", []))[:bounded]
        return {
            "ok": True, "count": len(items), "agents": items,
            "global_admission": data.get("global_admission"),
        }

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
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        hours = _float_query(request, "hours", 24)
        payload = telemetry.summary(hours)
        try:
            cached_agent_data = cached_agents(50)
            agents = cached_agent_data.get("agents", [])
        except Exception:
            cached_agent_data = {"global_admission": None}
            agents = []
        payload.update({
            "server": "Mac MCP",
            "version": __version__,
            "local_only": True,
            "agent_count": len(agents),
            "active_agents": sum(1 for agent in agents if agent.get("status") in {"starting", "running"}),
            "global_admission": cached_agent_data.get("global_admission"),
        })
        return JSONResponse(payload)

    async def runtime_diagnostics(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        return JSONResponse({
            "ok": True,
            "version": __version__,
            "chrome_companion_connected": bool(chrome_background_bridge.is_connected()),
        })

    async def permissions_view(request: Request) -> Response:
        # Consent is recorded per process, so only the server can describe its own.
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        return JSONResponse(await asyncio.to_thread(probe_permissions))

    async def decision_acceleration_status(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        if request.query_params.get("reload") == "1":
            await asyncio.to_thread(decision_engine.reload_api_key)
        return JSONResponse({"ok": True, **decision_engine.decision_status()})

    async def decision_acceleration_verify(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        result = await asyncio.to_thread(decision_engine.verify_api_key)
        return JSONResponse({**result, "key_status": decision_engine.decision_status()["key_status"]})

    async def security_semantics(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        server_profile = security_context.server_approval_profile if security_context is not None else "off"
        return JSONResponse(permission_semantics(server_approval_profile=server_profile))

    async def set_security_profile(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        profile = str(body.get("profile") or "").strip().lower()
        if profile not in GLOBAL_PROFILE_NAMES:
            return JSONResponse({
                "ok": False,
                "error": "invalid_permission_profile",
                "allowed_profiles": list(GLOBAL_PROFILE_NAMES),
                "profile_scope": (
                    "delegated_only" if profile in {"developer", "browser_only"} else "unknown"
                ),
            }, status_code=400)
        try:
            _persist_permission_profile(profile)
        except OSError:
            return JSONResponse({"ok": False, "error": "permission_profile_persist_failed"}, status_code=500)
        os.environ["MAC_MCP_PERMISSION_PROFILE"] = profile
        server_profile = security_context.server_approval_profile if security_context is not None else "off"
        payload = permission_semantics(profile, server_approval_profile=server_profile)
        payload.update({
            "ok": True,
            "restart_required": False,
            "existing_scoped_agents_retain_profile": True,
        })
        return JSONResponse(payload)

    async def set_server_approval_profile(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        if security_context is None:
            return JSONResponse({"ok": False, "error": "security_context_unavailable"}, status_code=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        profile = str(body.get("profile") or "").strip().lower()
        if profile not in SERVER_APPROVAL_PROFILE_NAMES:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_server_approval_profile",
                    "allowed_profiles": list(SERVER_APPROVAL_PROFILE_NAMES),
                },
                status_code=400,
            )
        try:
            update_runtime_setting("security", "server_approval_profile", profile)
        except (OSError, RuntimeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "server_approval_profile_persist_failed"},
                status_code=500,
            )
        payload = permission_semantics(server_approval_profile=profile)
        payload.update({"ok": True, "restart_required": False})
        return JSONResponse(payload)

    async def security_events(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        payload = telemetry.query_security_events(
            hours=_float_query(request, "hours", 24),
            limit=_int_query(request, "limit", 100),
            session_id=request.query_params.get("session_id"),
        )
        return JSONResponse({"ok": True, "events": payload})

    async def security_escalate(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        if security_context is None:
            return JSONResponse({"ok": False, "error": "security_context_unavailable"}, status_code=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        session_id = str(body.get("session_id") or "").strip()
        tool = str(body.get("tool") or "").strip()
        request_id = str(body.get("request_id") or "").strip() or None
        if tool not in RISK_REGISTRY:
            return JSONResponse({"ok": False, "error": "unknown_tool"}, status_code=400)
        try:
            grant = security_context.grant_escalation(
                session_id, tool, ttl_s=int(body.get("ttl_seconds") or 120),
                request_id=request_id,
            )
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid_escalation_request"}, status_code=400)
        except KeyError:
            return JSONResponse({"ok": False, "error": "web_scoped_session_not_found"}, status_code=404)
        telemetry.record_security_event(
            session_id=session_id, event_type="WEB_TO_HOST_APPROVAL", tool=tool,
            tool_class=RISK_REGISTRY[tool].family, origin=grant.get("origin"), decision="grant",
            reason_code="local_user_one_shot_grant", profile=None, actor="dashboard-local-user",
            agent_id=None, target_summary=grant.get("target_summary") or f"{RISK_REGISTRY[tool].family}:{tool}",
        )
        return JSONResponse({"ok": True, "grant": grant})

    async def changes(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        hours = _float_query(request, "hours", 24)
        max_items = max(1, min(_int_query(request, "max_items", 30), 100))
        session_id = str(request.query_params.get("session_id") or "").strip() or None
        agent_id = str(request.query_params.get("agent_id") or "").strip() or None
        team_id = str(request.query_params.get("team_id") or "").strip() or None
        if session_id or agent_id or team_id:
            return JSONResponse(telemetry.change_summary(
                hours=hours, session_id=session_id, agent_id=agent_id, team_id=team_id, max_items=max_items
            ))
        sets = telemetry.recent_change_sets(
            hours=hours, limit=max(1, min(_int_query(request, "limit", 5), 20)), max_items=max_items
        )
        return JSONResponse({"ok": True, "change_sets": sets})

    async def transactions(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        limit = max(1, min(_int_query(request, "limit", 10), 50))
        offset = max(0, min(_int_query(request, "offset", 0), 500))
        payload = recent_transactions(limit=limit, offset=offset)
        return JSONResponse({"ok": True, **payload})

    async def undo_transaction_route(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        transaction_id = str(body.get("transaction_id") or "").strip()
        if not transaction_id:
            return JSONResponse(
                {"ok": False, "error": "transaction_id_required"},
                status_code=400,
            )
        if body.get("confirm") is not True:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "undo_confirmation_required",
                    "transaction_id": transaction_id,
                },
                status_code=400,
            )
        event_id = telemetry.start_call(
            "dashboard",
            "file_transaction_undo",
            {"transaction_id": transaction_id, "force": False},
            metadata={"actor": "dashboard-local-user"},
        )
        try:
            undo_transaction(transaction_id, force=False)
            item = transaction_history_item(get_transaction(transaction_id))
        except TransactionNotFound as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_not_found"},
                status_code=404,
            )
        except TransactionExpired as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_expired"},
                status_code=409,
            )
        except TransactionIrreversible as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_irreversible"},
                status_code=409,
            )
        except TransactionConflict as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_conflict"},
                status_code=409,
            )
        except TransactionRestoreFailed as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_restore_failed"},
                status_code=500,
            )
        except FileTransactionError as exc:
            telemetry.finish_call(event_id, error=exc)
            return JSONResponse(
                {"ok": False, "error": "transaction_undo_failed"},
                status_code=409,
            )
        telemetry.finish_call(
            event_id,
            result={
                "ok": True,
                "transaction_id": transaction_id,
                "transaction_state": "undone",
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "transaction_id": transaction_id,
                "transaction": item,
            }
        )

    async def usage(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        days = max(1, min(_int_query(request, "days", 365), 400))
        actor = str(request.query_params.get("actor") or "all").strip().lower()
        actor_class = actor if actor in {"primary", "scoped_subagent"} else None
        try:
            payload = telemetry.usage_summary(days=days, actor_class=actor_class)
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "days": days,
                    "actor_class": actor_class or "all",
                    "daily": [],
                    "top_tools": [],
                    "error": str(sanitize_value(exc)),
                },
                status_code=500,
            )
        return JSONResponse(payload)

    async def usage_settings(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        updates: Dict[str, Any] = {}
        if "metering_enabled" in body:
            if not isinstance(body["metering_enabled"], bool):
                return JSONResponse({"ok": False, "error": "metering_enabled_must_be_boolean"}, status_code=400)
            updates["usage_metering"] = body["metering_enabled"]
        if "retention_days" in body:
            if body["retention_days"] not in USAGE_RETENTION_CHOICES:
                return JSONResponse(
                    {"ok": False, "error": "invalid_retention_days", "allowed": list(USAGE_RETENTION_CHOICES)},
                    status_code=400,
                )
            updates["usage_retention_days"] = int(body["retention_days"])
        try:
            for key, value in updates.items():
                update_runtime_setting("privacy", key, value)
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False, "error": "usage_settings_persist_failed"}, status_code=500)
        return JSONResponse({"ok": True, **usage_privacy(fresh=True)})

    async def _json_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    async def memory_view(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            return JSONResponse(await asyncio.to_thread(memory_overview))
        except Exception:
            return JSONResponse({"ok": False, "error": "memory_unavailable"}, status_code=503)

    async def memory_export(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        payload = await asyncio.to_thread(memory_export_all)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    async def memory_clear_route(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        body = await _json_body(request)
        try:
            result = await asyncio.to_thread(
                memory_clear, confirm=body.get("confirm") is True,
                date_from=body.get("date_from") or None, date_to=body.get("date_to") or None,
            )
        except HTTPException as exc:
            return JSONResponse({"ok": False, "error": "invalid_request", "message": str(exc.detail)}, status_code=400)
        return JSONResponse(result)

    async def memory_settings(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        body = await _json_body(request)
        updates: Dict[str, Any] = {}
        if "retention_days" in body:
            if body["retention_days"] not in MEMORY_RETENTION_CHOICES or isinstance(body["retention_days"], bool):
                return JSONResponse(
                    {"ok": False, "error": "invalid_retention_days", "allowed": list(MEMORY_RETENTION_CHOICES)},
                    status_code=400,
                )
            updates["memory_retention_days"] = int(body["retention_days"])
        if "keep_important" in body:
            if not isinstance(body["keep_important"], bool):
                return JSONResponse({"ok": False, "error": "keep_important_must_be_boolean"}, status_code=400)
            updates["memory_keep_important"] = body["keep_important"]
        try:
            for key, value in updates.items():
                update_runtime_setting("privacy", key, value)
        except (OSError, RuntimeError, ValueError):
            return JSONResponse({"ok": False, "error": "memory_settings_persist_failed"}, status_code=500)
        return JSONResponse(await asyncio.to_thread(memory_overview))

    async def usage_clear(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False, "error": "confirmation_required",
                 "message": "Send {\"confirm\": true} to delete all stored usage aggregates."},
                status_code=400,
            )
        try:
            tool_rows = clear_usage(telemetry.db_path)
            provider_rows = provider_usage_clear()
        except (OSError, sqlite3.Error) as exc:
            return JSONResponse({"ok": False, "error": str(sanitize_value(exc))}, status_code=500)
        return JSONResponse({"ok": True, "tool_usage_rows": tool_rows, "provider_usage_rows": provider_rows})

    async def agent_cancel(request: Request) -> Response:
        """The owner stops a delegated agent from the app or dashboard (recorded as cancelled by the user)."""
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        agent_id = str((body or {}).get("agent_id") or "").strip() if isinstance(body, dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", agent_id):
            return JSONResponse({"ok": False, "error": "invalid_agent_id"}, status_code=400)
        try:
            result = await asyncio.to_thread(
                agent_action, settings, action="cancel", agent_id=agent_id, requested_by="user",
            )
        except HTTPException as exc:
            return JSONResponse({"ok": False, "error": sanitize_value(exc.detail)}, status_code=exc.status_code)
        agent_cache["data"] = None  # the next list shows the new state
        cancellation = result.get("cancellation") or {}
        return JSONResponse({
            "ok": True, "agent_id": agent_id, "status": result.get("status"),
            "cancellation": cancellation, "message": result.get("message") or cancellation.get("message"),
        })

    async def launcher_recipes(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        listing = recipes.list_recipes(include_drafts=False)
        return JSONResponse({"ok": True, "recipes": [
            {key: item.get(key) for key in ("recipe_id", "name", "description", "status", "parameters",
                                            "consequential", "run_count", "last_run")}
            for item in listing["recipes"]
        ]})

    async def launcher_run_recipe(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        if recipe_runner is None:
            return JSONResponse({"ok": False, "error": "recipe_runner_unavailable"}, status_code=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        recipe_id = str(body.get("recipe_id") or "")
        values = body.get("values") or {}
        # Launchers only name an installed recipe and pass plain values; nothing else
        # (tool names, steps, commands) is accepted here.
        if not re.fullmatch(r"rcp_[0-9a-f]{12}", recipe_id):
            return JSONResponse({"ok": False, "error": "invalid_recipe_id"}, status_code=400)
        if not isinstance(values, dict) or len(values) > 20 or not all(
            isinstance(key, str) and (value is None or isinstance(value, (str, int, float, bool)))
            for key, value in values.items()
        ):
            return JSONResponse({"ok": False, "error": "invalid_values",
                                 "message": "values must be an object of up to 20 plain values."}, status_code=400)
        result = await recipe_runner(recipe_id, values)
        status_value = result.get("status") or ("completed" if result.get("ok") else "failed")
        recipe_info = result.get("recipe") if isinstance(result.get("recipe"), dict) else {}
        return JSONResponse({
            "ok": bool(result.get("ok")),
            "status": status_value,
            "recipe_id": recipe_id,
            "name": recipe_info.get("name"),
            "reason_code": result.get("reason_code"),
            "message": result.get("message") or result.get("error"),
            "failed_step": result.get("step_id"),
            "steps_executed": (result.get("plan_stats") or {}).get("steps_executed"),
        })

    async def provider_usage(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        days = max(1, min(_int_query(request, "days", 365), 400))
        try:
            payload = provider_usage_summary(days=days)
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "days": days,
                    "providers": {},
                    "error": str(sanitize_value(exc)),
                },
                status_code=500,
            )
        return JSONResponse(payload)

    async def events(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        payload = telemetry.query_events(
            hours=_float_query(request, "hours", 24),
            limit=_int_query(request, "limit", 120),
            source=request.query_params.get("source"),
            status=request.query_params.get("status"),
            tool=request.query_params.get("tool"),
        )
        return JSONResponse({
            "events": [_with_browser_context(event) for event in payload],
            "active": [_with_browser_context(event) for event in telemetry.active_calls()],
        })

    async def show_browser_tab(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        browser = str(body.get("browser") or "").strip()
        tab_handle = str(body.get("tab_handle") or "").strip()
        if not browser or not tab_handle:
            return JSONResponse({"ok": False, "error": "browser_and_tab_handle_required"}, status_code=400)
        try:
            with foreground_authorization("dashboard_show_tab"):
                result = await asyncio.to_thread(
                    browser_activate_tab,
                    settings,
                    browser=browser,
                    tab_handle=tab_handle,
                    allow_foreground=True,
                )
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 500) or 500)
            detail = getattr(exc, "detail", None) or str(exc)
            return JSONResponse(
                {"ok": False, "error": sanitize_value(detail)},
                status_code=max(400, min(status_code, 599)),
            )
        return JSONResponse({
            "ok": True,
            "browser": result.get("browser"),
            "tab_handle": result.get("tab_handle"),
            "foreground_forced": bool(result.get("foreground_forced")),
        })

    async def providers(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(provider_overview)
        except Exception as exc:
            return JSONResponse({"ok": False, "providers": [], "error": str(sanitize_value(exc))})
        return JSONResponse(data)

    async def agent_catalog_data(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            data = await asyncio.to_thread(agent_catalog, settings)
        except Exception as exc:
            return JSONResponse({"ok": False, "providers": {}, "error": str(sanitize_value(exc))})
        return JSONResponse(data)

    async def agents(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        try:
            data = cached_agents(max(1, min(_int_query(request, "limit", 20), 100)))
        except Exception as exc:
            return JSONResponse({"ok": False, "count": 0, "agents": [], "error": str(sanitize_value(exc))})
        public_agents = []
        for item in data.get("agents", []):
            public_agents.append({
                key: item.get(key) for key in (
                    "agent_id", "team_id", "team_task_id", "status", "phase", "title", "role", "provider", "model", "reasoning",
                    "access_mode", "capability_profile", "provenance_class", "injected_lesson_ids",
                    "lesson_context_chars", "lesson_candidate_ids", "started_at", "ended_at", "duration_ms", "first_event_latency_ms",
                    "idle_seconds", "step_count", "tool_call_count", "last_tool", "last_tool_duration_ms",
                    "resource_activity", "admission_generation",
                    "turn_count", "turn_elapsed_ms", "turn_budget_s", "hard_tool_budget_s",
                    "checkpoint_count", "checkpoint_pending", "last_checkpoint_at",
                    "workflow_id", "resume_generation", "checkpoint_state", "checkpoint_safety",
                    "checkpoint_reason", "side_effect_receipt_count", "pending_side_effect_count", "checkpoint_cursor", "last_durable_checkpoint_at", "resumable",
                    "throttle_count", "last_throttled_at", "last_throttle_reason", "cooldown_until",
                    "retry_count", "output_tokens", "result_preview",
                )
            })
        public_teams = []
        team_ids = []
        for item in public_agents:
            team_id = str(item.get("team_id") or "").strip()
            if team_id and team_id not in team_ids:
                team_ids.append(team_id)
            if len(team_ids) >= 20:
                break
        for team_id in team_ids:
            try:
                team = cached_team_summary(team_id)
            except Exception:
                continue
            public_teams.append({
                "team_id": team.get("team_id"),
                "status": team.get("status"),
                "success": team.get("success"),
                "outcome": team.get("outcome"),
                "partial_failure": team.get("partial_failure"),
                "successful_count": team.get("successful_count"),
                "failure_count": team.get("failure_count"),
                "pending_count": team.get("pending_count"),
                "work_count": team.get("work_count"),
                "title": sanitize_value(team.get("title"), preview_chars=96),
                "provider": team.get("provider"),
                "model": team.get("model"),
                "created_at": team.get("created_at"),
                "updated_at": team.get("updated_at"),
                "count": team.get("count"),
                "terminal_count": team.get("terminal_count"),
            })
        return JSONResponse({
            "ok": True, "count": len(public_agents), "agents": public_agents,
            "teams": public_teams,
            "global_admission": data.get("global_admission"),
        })

    async def steering_state(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        if steering is None:
            return JSONResponse({
                "ok": True,
                "schema_version": STEERING_SCHEMA_VERSION,
                "sessions": [],
                "recent": [],
                "session_ttl_minutes": 10,
                "generation_id": None,
            })
        return JSONResponse({
            "ok": True,
            "schema_version": STEERING_SCHEMA_VERSION,
            "sessions": steering.sessions(),
            "recent": steering.recent(30),
            "session_ttl_minutes": steering.session_ttl_minutes,
            "generation_id": steering.generation_id,
        })

    async def steering_send(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
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
        client_instruction_id = str(payload.get("client_instruction_id") or "").strip() or None
        generation_id = str(payload.get("generation_id") or "").strip() or None
        if not session_id:
            return JSONResponse({"ok": False, "error": "session_required"}, status_code=400)
        try:
            message = steering.enqueue(
                session_id,
                text,
                client_instruction_id=client_instruction_id,
                generation_id=generation_id,
            )
        except SteeringGenerationMismatch as exc:
            return JSONResponse({
                "ok": False,
                "error": "stale_generation",
                "outcome": "unknown",
                "provided_generation_id": exc.provided_generation_id,
                "current_generation_id": exc.current_generation_id,
            }, status_code=409)
        except SteeringIdempotencyExpired as exc:
            return JSONResponse({
                "ok": False,
                "error": "idempotency_expired",
                "outcome": "unknown",
                "reason": "canonical_result_outside_active_dedupe_window",
                "client_instruction_id": exc.client_instruction_id,
                "canonical_message_id": exc.canonical_message_id,
                "generation_id": steering.generation_id,
            }, status_code=409)
        except SteeringIdempotencyConflict as exc:
            return JSONResponse({
                "ok": False,
                "error": "idempotency_conflict",
                "reason": exc.reason,
                "client_instruction_id": exc.client_instruction_id,
                "canonical_message_id": exc.canonical_message_id,
            }, status_code=409)
        except KeyError:
            return JSONResponse({"ok": False, "error": "session_closed"}, status_code=409)
        except OverflowError:
            return JSONResponse({"ok": False, "error": "queue_full"}, status_code=409)
        except ValueError as exc:
            code = str(exc) or "invalid_message"
            return JSONResponse({"ok": False, "error": code}, status_code=400)
        return JSONResponse({
            "ok": True,
            "schema_version": STEERING_SCHEMA_VERSION,
            "generation_id": steering.generation_id,
            "status": message.get("status") or "queued",
            "message": {
                "id": message["id"],
                "session_id": message["session_id"],
                "created_at": message["created_at"],
                "status": message["status"],
                "client_instruction_id": message.get("client_instruction_id"),
                "idempotent_replay": bool(message.get("idempotent_replay")),
                "session_state": message.get("session_state"),
                "activity_state": message.get("activity_state"),
                "lifecycle_state": message.get("lifecycle_state"),
            },
        })


    async def steering_settings(request: Request) -> Response:
        denied = _dashboard_guard(request, dashboard_token)
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
        denied = _dashboard_guard(request, dashboard_token)
        if denied:
            return denied
        queue = telemetry.subscribe()

        async def generator():
            try:
                # Same browser_context as /dashboard/api/events, so a client can keep
                # its activity list from the stream instead of polling for it.
                hello = {"kind": "connected", "active": [_with_browser_context(item) for item in telemetry.active_calls()]}
                yield "event: telemetry\ndata: " + json.dumps(hello, ensure_ascii=False) + "\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if isinstance(event, dict) and event.get("kind") in {"call_started", "call_finished"}:
                        event = _with_browser_context(event)
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
        Route("/dashboard/api/diagnostics/runtime", runtime_diagnostics, methods=["GET"]),
        Route("/dashboard/api/diagnostics/permissions", permissions_view, methods=["GET"]),
        Route("/dashboard/api/decision-acceleration", decision_acceleration_status, methods=["GET"]),
        Route("/dashboard/api/decision-acceleration/verify", decision_acceleration_verify, methods=["POST"]),
        Route("/dashboard/api/security/semantics", security_semantics, methods=["GET"]),
        Route("/dashboard/api/security/profile", set_security_profile, methods=["POST"]),
        Route("/dashboard/api/security/server-approval", set_server_approval_profile, methods=["POST"]),
        Route("/dashboard/api/security/events", security_events, methods=["GET"]),
        Route("/dashboard/api/security/escalate", security_escalate, methods=["POST"]),
        Route("/dashboard/api/events", events, methods=["GET"]),
        Route("/dashboard/api/usage", usage, methods=["GET"]),
        Route("/dashboard/api/usage/settings", usage_settings, methods=["POST"]),
        Route("/dashboard/api/usage/clear", usage_clear, methods=["POST"]),
        Route("/dashboard/api/agents/cancel", agent_cancel, methods=["POST"]),
        Route("/dashboard/api/memory", memory_view, methods=["GET"]),
        Route("/dashboard/api/memory/export", memory_export, methods=["GET"]),
        Route("/dashboard/api/memory/clear", memory_clear_route, methods=["POST"]),
        Route("/dashboard/api/memory/settings", memory_settings, methods=["POST"]),
        Route("/dashboard/api/recipes", launcher_recipes, methods=["GET"]),
        Route("/dashboard/api/recipes/run", launcher_run_recipe, methods=["POST"]),
        Route("/dashboard/api/provider-usage", provider_usage, methods=["GET"]),
        Route("/dashboard/api/changes", changes, methods=["GET"]),
        Route("/dashboard/api/transactions", transactions, methods=["GET"]),
        Route("/dashboard/api/transactions/undo", undo_transaction_route, methods=["POST"]),
        Route("/dashboard/api/browser/show-tab", show_browser_tab, methods=["POST"]),
        Route("/dashboard/api/providers", providers, methods=["GET"]),
        Route("/dashboard/api/agent-catalog", agent_catalog_data, methods=["GET"]),
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
