"""Dashboard API for Settings › MCP Servers (the owner's own MCP servers).

GET  /dashboard/api/mcp-servers         status of every server and its tools (no secret values), the
                                        catalog of services and any browser sign-in in progress
POST /dashboard/api/mcp-servers/action  {action: add|import|remove|enable|disable|list|unlist|test|reload|
                                        connect|cancel_sign_in|disconnect, ...}
GET  /connectors/oauth/callback         where a service's sign-in page sends the browser back (#260)

Writes go to ~/.mac-mcp/mcp_servers.json; the running server picks the change up at once. A service
connected from the catalog keeps its credential in the Keychain (mcp_connectors.py).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import external_mcp, mcp_connectors

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
        catalog = (external_mcp.read_raw_config()["mcpServers"].get(name) or {}).get("catalog")
        if not external_mcp.remove_server(name):
            raise external_mcp.ConfigError(f"no MCP server named {name!r}")
        if catalog:
            mcp_connectors.forget(name)
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


_CALLBACK_PAGE = """<!doctype html><meta charset="utf-8"><title>Mac MCP</title>
<body style="font:15px -apple-system,sans-serif;margin:15vh auto;max-width:28em;text-align:center">
<h2>{title}</h2><p>{text}</p></body>"""


async def _connector_action(verb: str, body: Dict[str, Any], port: Optional[int]) -> Dict[str, Any]:
    if verb == "connect":
        connector_id = str(body.get("connector") or "").strip()
        if mcp_connectors.connector(connector_id).auth == "token":
            return await mcp_connectors.connect_token(connector_id, str(body.get("token") or ""))
        return await mcp_connectors.start_sign_in(connector_id, port, client_id=str(body.get("client_id") or ""),
                                                  client_secret=str(body.get("client_secret") or ""))
    name = str(body.get("name") or "").strip()
    if verb == "cancel_sign_in":
        return {"name": name, "cancelled": await mcp_connectors.cancel_sign_in(name)}
    return await mcp_connectors.disconnect(name)


def create_routes(guard: Guard) -> List[Route]:
    async def status(request: Request) -> Response:
        denied = guard(request)
        if denied:
            return denied
        result = await asyncio.to_thread(_status)
        result["connectors"] = mcp_connectors.catalog_public(request.url.port)
        result["sign_ins"] = mcp_connectors.sign_ins()
        return JSONResponse(result)

    async def callback(request: Request) -> Response:
        # Reached by the owner's browser, so no dashboard token: only the random state of a
        # sign-in that is waiting right now is accepted.
        params = request.query_params
        flow = mcp_connectors.complete_callback(params.get("state", ""), params.get("code", ""),
                                                params.get("error", ""))
        if flow is None:
            return HTMLResponse(_CALLBACK_PAGE.format(
                title="This sign-in is no longer waiting",
                text="Start it again in Mac MCP Settings › MCP Servers."), status_code=400)
        item = mcp_connectors.connector(flow.connector_id)
        if flow.error:
            return HTMLResponse(_CALLBACK_PAGE.format(title=f"{item.title} was not connected",
                                                      text="Go back to Mac MCP Settings to try again."))
        return HTMLResponse(_CALLBACK_PAGE.format(title=f"{item.title} is connecting to Mac MCP",
                                                  text="You can close this tab and go back to Mac MCP Settings."))

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
            if verb in {"connect", "cancel_sign_in", "disconnect"}:
                result = await _connector_action(verb, body, request.url.port)
                ok = result.get("ok", True) is not False and result.get("state") != "failed"
                return JSONResponse({"ok": ok, "action": verb, "result": result,
                                     "message": None if ok else result.get("error")})
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
        except (external_mcp.ConfigError, mcp_connectors.ConnectorError) as exc:
            return JSONResponse({"ok": False, "error": "invalid_config", "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "action": verb, "result": result})

    return [
        Route("/dashboard/api/mcp-servers", status, methods=["GET"]),
        Route("/dashboard/api/mcp-servers/action", action, methods=["POST"]),
        Route(mcp_connectors.CALLBACK_PATH, callback, methods=["GET"]),
    ]
