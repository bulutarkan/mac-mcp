"""One machine-readable failure contract for MCP, tool_invoke and REST.

Every failed call is described by the same fields wherever it surfaces:

  code     stable snake_case identifier (registry below, or the tool's own
           error/reason_code, or a status-based fallback)
  stage    validation | policy | preflight | execution
  outcome  not_executed (nothing was dispatched), completed (the tool ran and
           reported failure) or unknown (a side effect may have happened)
  retry    fix_arguments | safe_retry | observe_again | wait_for_user | never_retry

An unknown outcome never gets safe_retry: the caller must observe the current
state before deciding. MCP errors keep their human text and gain one
"error_contract={json}" line; REST error bodies keep "detail" and gain
"error". Completed tool results that report ok=false are not errors here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .workflow_checkpoints import exception_not_executed

CONTRACT_PREFIX = "error_contract="
_STEERING_PREFIX = "mac_mcp_steering_preempted:"
_CODE_PREFIX = re.compile(r"^([a-z][a-z0-9_]{2,63}):")


@dataclass(frozen=True)
class ErrorSpec:
    stage: str
    outcome: str
    retry: str
    http_status: int


_VALIDATION = ErrorSpec("validation", "not_executed", "fix_arguments", 400)
_POLICY = ErrorSpec("policy", "not_executed", "never_retry", 403)
_APPROVAL = ErrorSpec("policy", "not_executed", "wait_for_user", 403)

REGISTRY: Dict[str, ErrorSpec] = {
    "invalid_arguments": ErrorSpec("validation", "not_executed", "fix_arguments", 422),
    "intent_description_required": _VALIDATION,
    "intent_description_sensitive": _VALIDATION,
    "unknown_tool": ErrorSpec("validation", "not_executed", "fix_arguments", 404),
    "invalid_cursor": _VALIDATION,
    "cursor_mismatch": _VALIDATION,
    "profile_denied": _POLICY,
    "scope_denied": _POLICY,
    "agent_control_denied": _POLICY,
    "client_tool_unavailable": _POLICY,
    "secret_egress_approval_required": _APPROVAL,
    "server_risk_approval_required": _APPROVAL,
    "web_host_boundary_approval_required": _APPROVAL,
    "security_approval_rejected": _POLICY,
    "server_approval_config_invalid": _POLICY,
    "browser_no_progress": ErrorSpec("policy", "not_executed", "observe_again", 409),
    "stale_tab_handle": ErrorSpec("execution", "not_executed", "observe_again", 409),
    "ambiguous_tab_handle": ErrorSpec("execution", "not_executed", "observe_again", 409),
    "tab_target_closed": ErrorSpec("execution", "unknown", "observe_again", 409),
    "tab_identity_changed": ErrorSpec("execution", "unknown", "observe_again", 409),
    "stale_element": ErrorSpec("execution", "not_executed", "observe_again", 409),
    "provider_incompatible": ErrorSpec("preflight", "not_executed", "never_retry", 409),
    "outcome_unknown": ErrorSpec("execution", "unknown", "observe_again", 409),
    "timeout": ErrorSpec("execution", "unknown", "observe_again", 504),
    "tool_failed": ErrorSpec("execution", "unknown", "observe_again", 500),
}

_STATUS_CODES = {
    400: "invalid_arguments", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
    408: "timeout", 413: "too_large", 415: "unsupported_media_type", 422: "invalid_arguments",
    429: "rate_limited", 503: "unavailable", 504: "timeout",
}


def _status_retry(status: int, executed_unknown: bool) -> str:
    if executed_unknown:
        return "observe_again"
    if status in (400, 404, 413, 415, 422):
        return "fix_arguments"
    if status in (401, 403):
        return "never_retry"
    if status == 409:
        return "observe_again"
    return "safe_retry"


def _find(exc: BaseException, kind: type) -> Optional[BaseException]:
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, kind):
            return current
        current = current.__cause__ or current.__context__
    return None


def _http_code(exc: HTTPException) -> Optional[str]:
    detail = exc.detail
    if isinstance(detail, dict):
        for key in ("error", "reason_code", "code"):
            value = str(detail.get(key) or "").strip().lower()
            if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value):
                return value
    return None


def _message(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("reason") or detail.get("error") or "")
        return str(detail or "")
    return str(exc).split("\n" + CONTRACT_PREFIX, 1)[0]


def describe(exc: BaseException, *, tool: Optional[str] = None, mutating: bool = True) -> Dict[str, Any]:
    """The contract for one raised failure; `mutating` is whether the tool can change state."""
    from pydantic import ValidationError

    not_executed = exception_not_executed(exc)
    validation = _find(exc, ValidationError)
    http = _find(exc, HTTPException)
    status: Optional[int] = None
    if validation is not None:
        code, spec = "invalid_arguments", REGISTRY["invalid_arguments"]
    elif http is not None:
        status = int(http.status_code)
        code = _http_code(http) or _STATUS_CODES.get(status, "tool_failed")
        spec = REGISTRY.get(code)
    else:
        match = _CODE_PREFIX.match(str(exc))
        code = match.group(1) if match else "tool_failed"
        spec = REGISTRY.get(code)
        if spec is None and match:
            # Prefixed errors are raised by the policy layer before dispatch.
            spec = ErrorSpec("policy", "not_executed", "never_retry", 403)
    if spec is None:
        status = status or 500
        unknown = mutating and not not_executed and (status >= 500 or status in (408, 409))
        spec = ErrorSpec(
            "validation" if status in (400, 404, 422) else "execution",
            "unknown" if unknown else "not_executed",
            _status_retry(status, unknown),
            status,
        )
    outcome, retry = spec.outcome, spec.retry
    if not_executed:
        outcome = "not_executed"
    elif not mutating and outcome == "unknown":
        # A read-only tool cannot leave a side effect behind.
        outcome, retry = "not_executed", ("safe_retry" if retry == "observe_again" else retry)
    if outcome == "unknown" and retry == "safe_retry":
        retry = "observe_again"
    contract = {
        "code": code, "stage": spec.stage, "outcome": outcome, "retry": retry,
        "http_status": status or spec.http_status, "message": _message(http or exc)[:500],
    }
    if tool:
        contract["tool"] = tool
    return contract


def has_contract(exc: BaseException) -> bool:
    text = str(exc)
    return CONTRACT_PREFIX in text or text.startswith(_STEERING_PREFIX)


def annotate(text: str, contract: Dict[str, Any]) -> str:
    return f"{text}\n{CONTRACT_PREFIX}{json.dumps(contract, ensure_ascii=False, separators=(',', ':'))}"


def parse(text: str) -> Optional[Dict[str, Any]]:
    """Read the contract line back out of an MCP error text."""
    for line in reversed(str(text or "").splitlines()):
        if line.startswith(CONTRACT_PREFIX):
            try:
                value = json.loads(line[len(CONTRACT_PREFIX):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def install_rest_handlers(app: Any, can_mutate: Any) -> None:
    """Add the contract as "error" to REST error bodies, keeping "detail" and the status."""
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _tool(request: Any) -> Optional[str]:
        route = request.scope.get("route")
        return getattr(route, "operation_id", None) or None

    async def on_http(request: Any, exc: StarletteHTTPException):
        tool = _tool(request)
        wrapped = exc if isinstance(exc, HTTPException) else HTTPException(exc.status_code, exc.detail)
        contract = describe(wrapped, tool=tool, mutating=can_mutate(tool) if tool else True)
        body = {"detail": exc.detail, "error": contract}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    async def on_validation(request: Any, exc: RequestValidationError):
        response = await request_validation_exception_handler(request, exc)
        payload = json.loads(bytes(response.body))
        tool = _tool(request)
        spec = REGISTRY["invalid_arguments"]
        payload["error"] = {
            "code": "invalid_arguments", "stage": spec.stage, "outcome": spec.outcome, "retry": spec.retry,
            "http_status": response.status_code, "message": "request body failed validation",
            **({"tool": tool} if tool else {}),
        }
        return JSONResponse(payload, status_code=response.status_code)

    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(RequestValidationError, on_validation)
