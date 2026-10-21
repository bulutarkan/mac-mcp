"""Today at a glance (#68): the day's events and free time, reminders due, mail that came in and conversations
with new messages, in one short answer.

Built on plan_day for the calendar, its reminders part for Reminders, the inbox for Mail and the Messages
database for Messages. Each section is optional (sources) and answers on its own: one excluded in Privacy is
never read, and one that fails is reported while the rest still come back. By default no message text is
included: mail shows subject, sender and time; conversations show who, when and how many are unread.
Nothing is stored. notify=True also shows a one-line summary as a macOS notification.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, status

from . import privacy_policy

SOURCES = ("calendar", "reminders", "mail", "messages")
MAX_ITEMS = 25
_APPS = {"calendar": ("Calendar", "com.apple.iCal"), "reminders": ("Reminders", "com.apple.reminders"),
         "mail": ("Mail", "com.apple.mail"), "messages": ("Messages", "com.apple.MobileSMS")}


def _calendar(day: datetime, limit: int) -> Dict[str, Any]:
    from .app_adapters_more import plan_day

    plan = plan_day(date_from=day.date().isoformat(), calendar=None, duration_min=None, hours=None, buffer_min=None,
                    limit=min(limit, 10), timeout_s=30.0, reminders=False)
    out = {"events": plan["events"][:limit], "all_day": plan["all_day"], "free": plan["free"], "hours": plan["hours"]}
    if plan.get("approximate"):
        out["approximate"] = True
    return out


def _reminders(day: datetime, limit: int) -> Dict[str, Any]:
    from .app_adapters_more import reminders_due

    start = datetime.combine(day.date(), datetime.min.time())
    due = reminders_due(start, start + timedelta(days=1) - timedelta(seconds=1), timeout_s=20.0)
    return {"due": due[:limit], "overdue": sum(1 for item in due if item["overdue"]), "total": len(due),
            "truncated": len(due) > limit}


def _mail_script(start: datetime, end: datetime, limit: int) -> str:
    from .app_adapter_core import _date_setup

    return f'''set rs to ASCII character 30
set us to ASCII character 31
{_date_setup("dayStart", start)}
{_date_setup("dayEnd", end)}
set outText to ""
tell application "Mail"
    set matches to (every message of inbox whose date received ≥ dayStart and date received ≤ dayEnd)
    set unreadCount to count of (every message of inbox whose read status is false and date received ≥ dayStart and date received ≤ dayEnd)
    set outText to ((count of matches) as text) & us & (unreadCount as text)
    set rowCount to 0
    repeat with m in matches
        if rowCount ≥ {limit} then exit repeat
        set rowText to (message id of m as text) & us & (subject of m as text) & us & (sender of m as text) & us & ¬
            (date received of m as text) & us & ((read status of m) as text)
        set outText to outText & rs & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''


def _mail(day: datetime, limit: int) -> Dict[str, Any]:
    from .app_adapter_core import _RS, _US, _run

    start = datetime.combine(day.date(), datetime.min.time())
    raw = _run(_mail_script(start, start + timedelta(days=1) - timedelta(seconds=1), limit), timeout_s=25.0,
               code="MAIL_FIND_FAILED")
    head, *lines = raw.split(_RS)
    total, _, unread = head.partition(_US)
    rows = []
    for line in lines:
        parts = line.split(_US)
        if len(parts) >= 5:
            rows.append({"message_id": parts[0], "subject": parts[1], "sender": parts[2], "received": parts[3],
                         "unread": parts[4] == "false"})
    return {"received": int(total or 0), "unread": int(unread or 0), "messages": rows,
            "truncated": int(total or 0) > len(rows), "mailbox": "inbox"}


def _messages(day: datetime, limit: int) -> Dict[str, Any]:
    from . import messages_reader

    found = messages_reader.recent_chats(day.date().isoformat(), day.date().isoformat(), limit=limit)
    return {"conversations": found["chats"], "unread": sum(chat["unread"] for chat in found["chats"]),
            "truncated": found["truncated"]}


_SECTIONS: Dict[str, Callable[[datetime, int], Dict[str, Any]]] = {
    "calendar": _calendar, "reminders": _reminders, "mail": _mail, "messages": _messages,
}


def _section(source: str, day: datetime, limit: int) -> Dict[str, Any]:
    try:
        privacy_policy.check_app(*_APPS[source])
    except privacy_policy.PrivacyBlocked:
        return {"ok": False, "reason": "excluded_in_privacy", "message": "Excluded in Settings › Privacy."}
    try:
        return {"ok": True, **_SECTIONS[source](day, limit)}
    except Exception as exc:  # one source failing must not take the others with it
        code = getattr(exc, "code", None) or type(exc).__name__
        return {"ok": False, "reason": str(code).lower(), "message": str(exc)[:240]}


def _summary(day: datetime, sections: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    calendar, reminders = sections.get("calendar", {}), sections.get("reminders", {})
    mail, messages = sections.get("mail", {}), sections.get("messages", {})
    if calendar.get("ok"):
        parts.append(f"{len(calendar['events'])} event{'s' * (len(calendar['events']) != 1)}")
    if reminders.get("ok"):
        parts.append(f"{reminders['total']} reminder{'s' * (reminders['total'] != 1)} due"
                     + (f" ({reminders['overdue']} overdue)" if reminders["overdue"] else ""))
    if mail.get("ok"):
        parts.append(f"{mail['unread']} unread of {mail['received']} new mail")
    if messages.get("ok"):
        parts.append(f"{messages['unread']} unread message{'s' * (messages['unread'] != 1)}")
    return f"{day.strftime('%A %d %B')}: " + (", ".join(parts) if parts else "nothing could be read")


def daily_briefing(settings: Any, *, date: Optional[str] = None, sources: Optional[List[str]] = None,
                   limit: int = 10, notify: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    chosen = list(dict.fromkeys(str(item).strip().lower() for item in (sources or SOURCES) if str(item).strip()))
    unknown = [item for item in chosen if item not in SOURCES]
    if unknown or not chosen:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"sources are some of: {', '.join(SOURCES)}.")
    chosen = [item for item in SOURCES if item in chosen]
    today = now or datetime.now()
    try:
        day = datetime.fromisoformat(date) if date else today
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date is YYYY-MM-DD (default today).") from None
    limit = max(1, min(int(limit), MAX_ITEMS))
    with ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="daily-briefing") as pool:
        futures = {source: pool.submit(_section, source, day, limit) for source in chosen}
        sections = {source: futures[source].result() for source in chosen}
    summary = _summary(day, sections)
    out: Dict[str, Any] = {
        "ok": any(section["ok"] for section in sections.values()),
        "date": day.date().isoformat(), "summary": summary, **sections,
        "partial": any(not section["ok"] for section in sections.values()),
        "read_only": True,
        "note": "Mail and message text is not included; open an item with mac_app (Mail open_message, Messages "
                "read_messages with the chat_id) when the owner wants more. To get this every morning, propose a "
                "scheduled AI task (scheduled_tasks) that asks for daily_briefing; the owner approves it.",
    }
    if notify:
        from .tools_macos import send_notification

        shown = send_notification(settings, title="Today", message=summary)
        out["notified"] = bool(shown.get("ok"))
    return out
