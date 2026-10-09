"""Shared paging and truncation metadata for model-facing collections.

Collections that can exceed one page report the same shape:
page = {limit, returned, has_more, next_cursor}. Cursors are keyset cursors:
they hold the sort key of the last item returned (not an offset), so a page
boundary stays put when entries are added or removed before it. A cursor is
bound to the request that produced it (path, pattern, filter) and is refused
for a different request instead of silently paging something else.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, Optional, Sequence

from fastapi import HTTPException, status


def _request_key(parts: Sequence[Any]) -> str:
    raw = json.dumps([str(part) for part in parts], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def encode_cursor(request: Sequence[Any], after: Any) -> str:
    payload = json.dumps({"k": _request_key(request), "a": after}, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str], request: Sequence[Any]) -> Any:
    """Return the sort key to resume after, or None for the first page."""
    if not cursor:
        return None
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        key, after = payload["k"], payload["a"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            {"error": "invalid_cursor", "message": "cursor is not valid; start again without it"}) from exc
    if key != _request_key(request):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {
            "error": "cursor_mismatch",
            "message": "cursor belongs to a different request; repeat the same arguments or start without a cursor",
        })
    return after


def bounded_limit(limit: Any, default: int, maximum: int) -> int:
    try:
        value = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def page_meta(limit: int, returned: int, has_more: bool, next_cursor: Optional[str]) -> Dict[str, Any]:
    return {"limit": limit, "returned": returned, "has_more": bool(has_more),
            "next_cursor": next_cursor if has_more else None}
