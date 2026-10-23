"""/dashboard/api/chats/...: Mac MCP Chat for the native app. Local and dashboard-token only."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .chat_sessions import ChatError, get_host


def _error(exc: ChatError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.code, "message": str(exc)}, status_code=exc.status)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ChatError("Send a JSON object.")
    if not isinstance(body, dict):
        raise ChatError("Send a JSON object.")
    return body


def create_chat_routes(dashboard_guard: Callable[[Request], Optional[Response]]) -> List[Route]:
    def guarded(handler):
        async def endpoint(request: Request) -> Response:
            denied = dashboard_guard(request)
            if denied is not None:
                return denied
            try:
                return JSONResponse(await handler(request))
            except ChatError as exc:
                return _error(exc)
        return endpoint

    async def chats(request: Request) -> Dict[str, Any]:
        host = get_host()
        if request.method == "GET":
            return host.list()
        body = await _body(request)
        return host.create(str(body.get("project") or ""), str(body.get("agent") or ""),
                           str(body.get("model") or ""), str(body.get("title") or ""))

    async def chat(request: Request) -> Dict[str, Any]:
        try:
            after = max(0, int(request.query_params.get("after") or 0))
        except ValueError:
            after = 0
        return get_host().read(request.path_params["chat_id"], after)

    async def prompt(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return await get_host().prompt(request.path_params["chat_id"], str(body.get("text") or ""))

    async def cancel(request: Request) -> Dict[str, Any]:
        return await get_host().cancel(request.path_params["chat_id"])

    async def permission(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return get_host().answer(request.path_params["chat_id"], str(body.get("item_id") or ""),
                                 str(body.get("option_id") or ""))

    async def delete(request: Request) -> Dict[str, Any]:
        return await get_host().delete(request.path_params["chat_id"])

    return [
        Route("/dashboard/api/chats", guarded(chats), methods=["GET", "POST"]),
        Route("/dashboard/api/chats/{chat_id}", guarded(chat), methods=["GET"]),
        Route("/dashboard/api/chats/{chat_id}/prompt", guarded(prompt), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/cancel", guarded(cancel), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/permission", guarded(permission), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/delete", guarded(delete), methods=["POST"]),
    ]
