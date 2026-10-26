"""/dashboard/api/chats/...: Mac MCP Chat for the native app. Local and dashboard-token only."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
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
        made = host.create(str(body.get("project") or ""), str(body.get("agent") or ""),
                           str(body.get("model") or ""), str(body.get("title") or ""))
        return host.connect(made["chat"]["id"])  # the agent starts now, so its models show at once

    async def chat(request: Request) -> Dict[str, Any]:
        try:
            after = max(0, int(request.query_params.get("after") or 0))
        except ValueError:
            after = 0
        return get_host().read(request.path_params["chat_id"], after)

    async def prompt(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        return await get_host().prompt(request.path_params["chat_id"], str(body.get("text") or ""), attachments)

    async def cancel(request: Request) -> Dict[str, Any]:
        return await get_host().cancel(request.path_params["chat_id"])

    async def permission(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return get_host().answer(request.path_params["chat_id"], str(body.get("item_id") or ""),
                                 str(body.get("option_id") or ""))

    async def queued(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return await get_host().queued(request.path_params["chat_id"], str(body.get("id") or ""),
                                       str(body.get("action") or ""))

    async def list_events(request: Request) -> Response:
        """Server-sent events of the chat list (the same object as GET /chats, without agents)."""
        denied = dashboard_guard(request)
        if denied is not None:
            return denied
        host = get_host()

        async def generator():
            async for listing in host.list_stream(request.is_disconnected):
                if listing is None:
                    yield ": keepalive\n\n"
                else:
                    yield "data: " + json.dumps(listing, ensure_ascii=False) + "\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})

    async def usage(request: Request) -> Dict[str, Any]:
        try:
            days = int(request.query_params.get("days") or 30)
        except ValueError:
            days = 30
        return get_host().usage(days)

    async def rename(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return get_host().rename(request.path_params["chat_id"], str(body.get("title") or ""))

    async def events(request: Request) -> Response:
        """Server-sent events: each `data:` is the same object as GET /chats/{id}?after=…"""
        denied = dashboard_guard(request)
        if denied is not None:
            return denied
        try:
            after = max(0, int(request.query_params.get("after") or 0))
        except ValueError:
            after = 0
        host = get_host()
        try:
            host.read(request.path_params["chat_id"], after)  # 404 before the stream starts
        except ChatError as exc:
            return _error(exc)

        async def generator():
            async for batch in host.stream(request.path_params["chat_id"], after, request.is_disconnected):
                if batch is None:
                    yield ": keepalive\n\n"
                else:
                    yield "data: " + json.dumps(batch, ensure_ascii=False) + "\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})

    async def connect(request: Request) -> Dict[str, Any]:
        return get_host().connect(request.path_params["chat_id"])

    async def config(request: Request) -> Dict[str, Any]:
        body = await _body(request)
        return await get_host().configure(request.path_params["chat_id"], str(body.get("config_id") or ""),
                                          str(body.get("value") or ""))

    async def delete(request: Request) -> Dict[str, Any]:
        body = await _body(request) if await request.body() else {}
        return await get_host().delete(request.path_params["chat_id"], only_if_empty=bool(body.get("only_if_empty")))

    return [
        Route("/dashboard/api/chats", guarded(chats), methods=["GET", "POST"]),
        Route("/dashboard/api/chats/usage", guarded(usage), methods=["GET"]),
        Route("/dashboard/api/chats/list-events", list_events, methods=["GET"]),
        Route("/dashboard/api/chats/{chat_id}", guarded(chat), methods=["GET"]),
        Route("/dashboard/api/chats/{chat_id}/prompt", guarded(prompt), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/cancel", guarded(cancel), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/permission", guarded(permission), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/queue", guarded(queued), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/events", events, methods=["GET"]),
        Route("/dashboard/api/chats/{chat_id}/connect", guarded(connect), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/rename", guarded(rename), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/config", guarded(config), methods=["POST"]),
        Route("/dashboard/api/chats/{chat_id}/delete", guarded(delete), methods=["POST"]),
    ]
