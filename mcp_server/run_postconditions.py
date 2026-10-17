"""Checks against outside systems after a finished run (#228), next to #198's file checks.

A run can say it is done while the sheet row, the CRM field or the posted reply is not there. These
checks look, read-only:

- url:  GET a page (no headers, so no credentials leave this Mac) and expect a status and, optionally,
        a piece of text, for example a Reddit reply's permalink;
- tool: call one of the owner's MCP server tools (#229) that is read-only by both its own annotation
        and Mac MCP's risk entry, and expect a piece of text in its answer, for example a Google Sheets
        read. A tool that may change anything is never called.

Nothing is written or sent again. Text that shows up late is looked for a bounded number of times.
What could not be observed (unreachable, server not connected, timeout) is unknown: never a pass, and
reported apart from a plain failure. Page and tool contents are never stored, and a URL is kept
without its query, which may carry a token.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

RETRY_WAITS_S: Tuple[float, ...] = (5.0, 10.0)
HTTP_TIMEOUT_S = 10.0
TOOL_TIMEOUT_S = 20.0
MAX_BODY_BYTES = 1_000_000
TEXT_LIMIT = 500
ARGUMENTS_LIMIT = 5000


class CheckError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate(item: Dict[str, Any]) -> Dict[str, Any]:
    """A url or tool check in normal form; CheckError says what is wrong."""
    contains = str(item.get("contains") or "")
    if len(contains) > TEXT_LIMIT:
        raise CheckError("verify_contains_too_long", f"The text to look for must be at most {TEXT_LIMIT} characters.")
    if item.get("url") is not None:
        url = str(item.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise CheckError("verify_url_invalid", "A URL check needs a full http(s) address.")
        if parts.username or parts.password:
            raise CheckError("verify_url_credentials", "A URL check cannot carry a user name or password.")
        if len(url) > 2000:
            raise CheckError("verify_url_too_long", "The URL must be at most 2000 characters.")
        status = item.get("status", 200)
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise CheckError("verify_status_invalid", "The expected status must be an HTTP status such as 200.")
        out: Dict[str, Any] = {"kind": "url", "url": url}
        if contains:
            out["contains"] = contains
        out["status"] = status
        return out
    tool = str(item.get("tool") or "").strip()
    if "__" not in tool or len(tool) > 128:
        raise CheckError("verify_tool_invalid",
                         "A tool check names one of your MCP server tools, such as sheets__read_range.")
    arguments = item.get("arguments", {})
    if not isinstance(arguments, dict) or len(json.dumps(arguments)) > ARGUMENTS_LIMIT:
        raise CheckError("verify_arguments_invalid", "The tool's arguments must be a JSON object (up to 5000 characters).")
    out = {"kind": "tool", "tool": tool, "arguments": arguments}
    if contains:
        out["contains"] = contains
    return out


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _http_get(url: str, **_: Any) -> Any:
    import httpx

    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True, max_redirects=5,
                      headers={"User-Agent": "Mac MCP run check"}) as client:
        with client.stream("GET", url) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) >= MAX_BODY_BYTES:
                    break
            text = body[:MAX_BODY_BYTES].decode(response.encoding or "utf-8", errors="replace")
            return _Answer(response.status_code, text)


class _Answer:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _look(attempt) -> Dict[str, Any]:
    """Run a read-only look, again after each wait while it reports text that is not there yet."""
    result = attempt()
    for wait in RETRY_WAITS_S:
        if result.get("problem") != "missing_text":
            break
        time.sleep(wait)
        result = attempt()
    return result


def _url_check(check: Dict[str, Any], expand) -> Dict[str, Any]:
    url = expand(check["url"])
    shown = _without_query(url)
    contains = expand(check.get("contains") or "")

    def attempt() -> Dict[str, Any]:
        try:
            answer = _http_get(url)
        except Exception:
            return {"kind": "url", "url": shown, "ok": False, "problem": "unreachable", "unknown": True}
        if answer.status_code != check.get("status", 200):
            return {"kind": "url", "url": shown, "ok": False, "problem": "status", "status": answer.status_code}
        if contains and contains not in (answer.text or ""):
            return {"kind": "url", "url": shown, "ok": False, "problem": "missing_text", "status": answer.status_code}
        return {"kind": "url", "url": shown, "ok": True, "status": answer.status_code}
    return _look(attempt)


def _central_read_only(name: str) -> bool:
    from .policy import RISK_REGISTRY, Capability

    entry = RISK_REGISTRY.get(name)
    return bool(entry) and Capability.EXTERNAL_SIDE_EFFECT not in entry.capabilities and not entry.destructive


def _tool_check(check: Dict[str, Any], expand) -> Dict[str, Any]:
    from . import external_mcp

    name = check["tool"]
    base = {"kind": "tool", "tool": name}
    manager = external_mcp.manager()
    found: Optional[Tuple[str, Dict[str, Any]]] = None
    connected = False
    for server in (manager.status().get("servers") or []) if manager else []:
        for tool in server.get("tools") or []:
            if tool.get("name") == name:
                found, connected = (server["name"], tool), server.get("status") == "connected"
    if manager is None or found is None or not connected:
        return {**base, "ok": False, "problem": "server_unavailable", "unknown": True}
    server_name, tool = found
    # Both the server's own annotation and Mac MCP's risk entry must say read-only, or it is not called.
    if not tool.get("read_only") or not _central_read_only(name):
        return {**base, "ok": False, "problem": "not_read_only"}
    contains = expand(check.get("contains") or "")
    arguments = json.loads(expand(json.dumps(check.get("arguments") or {})))

    def attempt() -> Dict[str, Any]:
        try:
            result = manager.call(server_name, tool["remote_name"], arguments, timeout=TOOL_TIMEOUT_S)
        except Exception:
            return {**base, "ok": False, "problem": "timeout", "unknown": True}
        payload = external_mcp.result_payload(server_name, tool["remote_name"], result)
        if not payload.get("ok"):
            return {**base, "ok": False, "problem": "tool_error"}
        text = str(payload.get("text") or "") + json.dumps(payload.get("structured") or {}, ensure_ascii=False)
        if contains and contains not in text:
            return {**base, "ok": False, "problem": "missing_text"}
        return {**base, "ok": True}
    return _look(attempt)


def run_check(check: Dict[str, Any], expand: Callable[[str], str] = lambda text: text) -> Dict[str, Any]:
    """`expand` fills in {today} and the other date words (the caller's scheduled_tasks.expand_dates)."""
    return _url_check(check, expand) if check.get("kind") == "url" else _tool_check(check, expand)
