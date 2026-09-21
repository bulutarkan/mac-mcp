from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import HTTPException, status


@dataclass(frozen=True)
class ForegroundAuthorization:
    source: str
    issued_at: float


_CURRENT_FOREGROUND_AUTHORIZATION: contextvars.ContextVar[Optional[ForegroundAuthorization]] = contextvars.ContextVar(
    "mac_mcp_foreground_authorization",
    default=None,
)


@contextmanager
def foreground_authorization(source: str) -> Iterator[ForegroundAuthorization]:
    """Grant foreground browser capability only inside one trusted internal call scope.

    This helper is intentionally not exposed as an MCP/REST tool. Model-visible
    booleans such as allow_foreground are request intent only and cannot mint this
    capability.
    """
    normalized = str(source or "").strip()
    if not normalized:
        raise ValueError("foreground authorization source is required")
    grant = ForegroundAuthorization(source=normalized, issued_at=time.time())
    token = _CURRENT_FOREGROUND_AUTHORIZATION.set(grant)
    try:
        yield grant
    finally:
        _CURRENT_FOREGROUND_AUTHORIZATION.reset(token)


def current_foreground_authorization() -> Optional[ForegroundAuthorization]:
    return _CURRENT_FOREGROUND_AUTHORIZATION.get()


def foreground_authorized() -> bool:
    return current_foreground_authorization() is not None


def require_foreground_authorization(operation: str, *, browser: Optional[str] = None) -> ForegroundAuthorization:
    grant = current_foreground_authorization()
    if grant is not None:
        return grant
    detail = {
        "ok": False,
        "error": "foreground_not_authorized",
        "reason_code": "FOREGROUND_NOT_AUTHORIZED",
        "foreground_required": True,
        "operation": str(operation or "browser_foreground_action"),
        "message": (
            "Foreground browser activation is reserved for an explicit local-user UI action. "
            "Model/tool parameters cannot authorize focus changes. Continue with stable tab_handle background DOM tools, "
            "or let the user click Show Tab in Mac MCP when they want that real tab brought forward."
        ),
    }
    if browser:
        detail["browser"] = str(browser)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
