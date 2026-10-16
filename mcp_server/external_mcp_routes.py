"""Dashboard API for Settings › MCP Servers (the owner's own MCP servers).

GET  /dashboard/api/mcp-servers         status of every server and its tools (no secret values)
POST /dashboard/api/mcp-servers/action  {action: add|import|remove|enable|disable|list|unlist|test|reload, ...}

Writes go to ~/.mac-mcp/mcp_servers.json; the running server picks the change up at once.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import external_mcp

Guard = Callable[[Request], Optional[Response]]


def _status() -> Dict[str, Any]:
    manager = external_mcp.manager()
    if manager is not None and manager._loop is not None:
        return {"ok": True, "running": True, **manager.status()}
    # Not connected in this process: show what is configured.
    try:
        servers, errors = external_mcp.load_servers()
    except external_mcp.ConfigError as exc:
        servers, errors = {}, {"*": str(exc)}
    return {
        "ok": True, "running": False, "config_path": str(external_mcp.config_path()),
        "servers": [{**server.public(), "status": "disabled" if not server.enabled else "not_started",
                     "error": None, "tools": []} for server in sorted(servers.values(), key=lambda s: s.name)],
        "config_errors": errors,
    }


def _reload() -> None:
    manager = external_mcp.manager()
    if manager is not None and manager._loop is not None:
        manager.reload()


def _entry(body: Dict[str, Any]) -> Dict[str, Any]:
    """A server entry from the Settings form: command line or URL, plus env/headers."""
    config = body.get("config")
    if isinstance(config, dict):
        return dict(config)
    entry: Dict[str, Any] = {}
    if str(body.get("url") or "").strip():
        entry["url"] = str(body["url"]).strip()
        if body.get("headers"):
            entry["headers"] = body["headers"]
    else:
        command = body.get("command")
        if isinstance(command, str):
            import shlex

            parts = shlex.split(command)
            if not parts:
                raise external_mcp.ConfigError("enter the command that starts the server, e.g. npx -y some-mcp")
            entry["command"], entry["args"] = parts[0], parts[1:]
        if body.get("env"):
            entry["env"] = body["env"]
    return entry


def _run_action(action: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if action == "add":
        server = external_mcp.upsert_server(name, _entry(body))
        _reload()
        return {"server": server.public()}
    if action == "import":
        names = external_mcp.import_servers(body.get("json") if body.get("json") is not None else body.get("config"))
        _reload()
        return {"imported": names}
    if action == "remove":
        if not external_mcp.remove_server(name):
            raise external_mcp.ConfigError(f"no MCP server named {name!r}")
        _reload()
        return {"removed": name}
    if action in {"enable", "disable"}:
        external_mcp.set_server_fields(name, enabled=action == "enable")
        _reload()
        return {"name": name, "enabled": action == "enable"}
    if action in {"list", "unlist"}:
        external_mcp.set_server_fields(name, listed=action == "list")
        _reload()
        return {"name": name, "listed": action == "list"}
    if action == "reload":
        _reload()
        return {}
    raise external_mcp.ConfigError(f"unknown action {action!r}")


def create_routes(guard: Guard) -> List[Route]:
    async def status(request: Request) -> Response:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse(await asyncio.to_thread(_status))

    async def action(request: Request) -> Response:
        denied = guard(request)
        if denied:
            return denied
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        verb = str(body.get("action") or "").strip().lower()
        try:
            if verb == "test":
                name = str(body.get("name") or "").strip()
                if body.get("config") is not None or body.get("command") or body.get("url"):
                    server = external_mcp.parse_server(name or "test", _entry(body))
                else:
                    servers, _ = await asyncio.to_thread(external_mcp.load_servers)
                    if name not in servers:
                        raise external_mcp.ConfigError(f"no MCP server named {name!r}")
                    server = servers[name]
                result = await external_mcp.probe(server)
                return JSONResponse({"ok": result["ok"], "action": verb, "result": result,
                                     "message": None if result["ok"] else result.get("error")})
            result = await asyncio.to_thread(_run_action, verb, body)
        except external_mcp.ConfigError as exc:
            return JSONResponse({"ok": False, "error": "invalid_config", "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "action": verb, "result": result})

    return [
        Route("/dashboard/api/mcp-servers", status, methods=["GET"]),
        Route("/dashboard/api/mcp-servers/action", action, methods=["POST"]),
    ]
