"""What every first-party app adapter shares: its error, the AppleScript runner, and field and date helpers.

app_adapters and app_adapters_more both build on this, so neither imports the other's internals.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .process_supervision import run_osascript
from .tools_ui import _run_osascript

_RS = chr(30)
_US = chr(31)


class AppAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _run(script: str, *, timeout_s: float = 10.0, code: str = "APP_ADAPTER_SCRIPT_FAILED") -> str:
    ok, stdout, stderr = _run_osascript(script, timeout_s=max(0.5, min(float(timeout_s), 30.0)))
    if not ok:
        detail = (stderr or stdout or "AppleScript failed").strip()
        # A compile error (-2741) means no statement ran at all.
        compile_error = "syntax error" in detail and "(-2741)" in detail
        raise AppAdapterError(code, detail[:800], **({"not_executed": True} if compile_error else {}))
    return (stdout or "").rstrip("\r\n")


def _run_js(script: str, args: list[str], *, timeout_s: float = 10.0, code: str = "APP_ADAPTER_SCRIPT_FAILED") -> Dict[str, Any]:
    """Run a JXA script whose run(argv) returns JSON; {"error": CODE, "message": ...} becomes an AppAdapterError.

    JXA looks up an app's terms when it runs, so a script for an app that is not installed still starts
    and says so, where AppleScript would not even compile. Data goes in as argv, never into the source.
    """
    result = run_osascript(script, max(0.5, min(float(timeout_s), 30.0)), args=[str(value) for value in args],
                           language="JavaScript")
    if result.timed_out:
        raise AppAdapterError(code, f"The app did not answer within {timeout_s:g}s.", retryable=True)
    if not result.ok:
        raise AppAdapterError(code, (result.stderr or result.stdout or "JXA failed").strip()[:800])
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise AppAdapterError("APP_ADAPTER_RESPONSE_INVALID", "The app's answer was not readable.") from exc
    if not isinstance(payload, dict):
        raise AppAdapterError("APP_ADAPTER_RESPONSE_INVALID", "The app's answer was not readable.")
    if payload.get("error"):
        raise AppAdapterError(str(payload["error"]), str(payload.get("message") or payload["error"]),
                              **({"retryable": False} if payload.get("retryable") is False else {}))
    return payload


def _parse_records(raw: str, fields: tuple[str, ...]) -> list[Dict[str, str]]:
    rows: list[Dict[str, str]] = []
    if not raw:
        return rows
    for record in raw.split(_RS):
        if not record:
            continue
        values = record.split(_US)
        if len(values) != len(fields):
            raise AppAdapterError(
                "APP_ADAPTER_RESPONSE_INVALID",
                f"Expected {len(fields)} fields from semantic adapter, got {len(values)}.",
            )
        rows.append(dict(zip(fields, values)))
    return rows


def _parse_date_bound(value: Optional[str], *, end: bool) -> datetime:
    if not value:
        now = datetime.now()
        return now + timedelta(days=365 if end else -30)
    text = str(value).strip()
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d")
            if end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            "Calendar date bounds must use YYYY-MM-DD or ISO-8601 local datetime.",
        ) from exc


def _date_setup(name: str, dt: datetime) -> str:
    # Day 1 first: setting the month while today is the 31st would roll over.
    return f'''set {name} to current date
set day of {name} to 1
set year of {name} to {dt.year}
set month of {name} to {dt.month}
set day of {name} to {dt.day}
set hours of {name} to {dt.hour}
set minutes of {name} to {dt.minute}
set seconds of {name} to {dt.second}'''


def _event_datetime(value: Optional[str], field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{field} is required (ISO-8601 local datetime).")
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d")
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError as exc:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            f"{field} must be YYYY-MM-DD or an ISO-8601 local datetime such as 2026-10-09T14:30.",
        ) from exc


def _clean_field(value: Optional[str], field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{field} must be at most {limit} characters.")
    return text


def _single_line(value: Optional[str], field: str, limit: int) -> str:
    text = _clean_field(value, field, limit)
    if any(ord(ch) < 32 for ch in text):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"{field} must be a single line of text.")
    return text


def _plain_text(value: Optional[str], field: str, limit: int) -> str:
    text = _clean_field(value, field, limit).replace("\r\n", "\n").replace("\r", "\n")
    # Our own field separators (and other control characters) never belong in note or mail text.
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


# Mail's standard mailboxes by the names adapters accept (mailbox=...).
_MAILBOX_EXPRESSIONS = {
    "inbox": "inbox",
    "sent": "sent mailbox",
    "drafts": "drafts mailbox",
    "junk": "junk mailbox",
    "trash": "trash mailbox",
}


def _mailbox_expr(mailbox: Optional[str]) -> tuple[str, str]:
    key = str(mailbox or "inbox").strip().lower()
    expr = _MAILBOX_EXPRESSIONS.get(key)
    if not expr:
        raise AppAdapterError(
            "APP_ADAPTER_ARGUMENT_INVALID",
            f"mailbox must be one of: {', '.join(_MAILBOX_EXPRESSIONS)}.",
        )
    return key, expr
