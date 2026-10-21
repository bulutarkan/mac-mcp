"""More first-party app actions (Group 4, #194-#196): reminders, notes, free time and day plans, contacts, shortcuts, mail.

They follow app_adapters' rules: data goes through each app's own scripting model (or the
`shortcuts` command), never its window; every write reads back what it changed; a replayed
create_reminder finds the reminder it made; unclear outcomes say outcome_unknown, and
append_to_note relies on the tool's idempotency_key rather than guessing from the note's text.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .app_adapter_core import (
    _US, AppAdapterError, _clean_field, _date_setup, _event_datetime, _parse_date_bound, _parse_records,
    _plain_text, _run, _single_line,
)
from .tools_ui import _apple_string

# ── Reminders: create_reminder ───────────────────────────────────────────────

_REMINDER_ROW = ("id", "name", "list", "due")


def reminders_create(title: Optional[str], *, list_name: Optional[str], due: Optional[str], notes: Optional[str],
                     timeout_s: float) -> Dict[str, Any]:
    name = _single_line(title, "title", 300)
    if not name:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Reminders create_reminder requires title.")
    chosen = _single_line(list_name, "list_name", 200)
    text = _plain_text(notes, "notes", 5000)
    due_text = str(due or "").strip()
    due_lines: List[str] = []
    if due_text:
        when = _event_datetime(due_text, "start")
        due_lines = [_date_setup("dueDate", when),
                     "set allday due date of r to dueDate" if len(due_text) == 10 else "set due date of r to dueDate"]
    target = (f"set chosen to lists whose name is {_apple_string(chosen)}" if chosen else "set chosen to {default list}")
    readback = '(id of r as text) & us & (name of r as text) & us & (name of l as text) & us & dueText & us & "#"'
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Reminders"',
        f"    {target}",
        '    if (count of chosen) is 0 then return "NO_LIST"',
        "    set l to item 1 of chosen",
        # A replayed request finds the open reminder it already made instead of adding a twin.
        f"    set existing to (reminders of l whose name is {_apple_string(name)} and completed is false)",
        '    set dueText to ""',
        "    if (count of existing) > 0 then",
        "        set r to item 1 of existing",
        "        try",
        "            if due date of r is not missing value then set dueText to (due date of r) as text",
        "        end try",
        f'        return "DUPLICATE" & us & {readback}',
        "    end if",
        f"    set r to make new reminder at end of l with properties {{name:{_apple_string(name)}"
        + (f", body:{_apple_string(text)}" if text else "") + "}",
        *[f"    {line}" for block in due_lines for line in block.split("\n")],
        "    set newId to id of r",
        "    set readBack to (reminders of l whose id is newId)",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        "    set r to item 1 of readBack",
        "    try",
        "        if due date of r is not missing value then set dueText to (due date of r) as text",
        "    end try",
        f'    return "OK" & us & {readback}',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="REMINDERS_CREATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The reminder may or may not exist; check with list_reminders before "
                              "trying again.", outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status == "NO_LIST":
        raise AppAdapterError("REMINDERS_LIST_NOT_FOUND", "No Reminders list has that name; nothing was created.")
    if status == "DUPLICATE":
        return {"ok": True, "created": False, "duplicate": True, "verified": True,
                "verification": "existing_reminder_matched", "reminder": dict(zip(_REMINDER_ROW, parts[1:5])),
                "message": "An open reminder with this title is already in that list; nothing was created."}
    if status != "OK":
        raise AppAdapterError("REMINDERS_CREATE_NOT_VERIFIED", "Reminders did not confirm the new reminder.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "created": True, "verified": True, "verification": "read_back",
            "reminder": dict(zip(_REMINDER_ROW, parts[1:5]))}


# ── Notes: append_to_note ────────────────────────────────────────────────────

def notes_append(item_id: Optional[str], query: Optional[str], body: Optional[str], *, exact: bool,
                 timeout_s: float) -> Dict[str, Any]:
    text = _plain_text(body, "body", 20000).strip("\n")
    if not text.strip():
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes append_to_note requires body.")
    note_id = str(item_id or "").strip()
    q = str(query or "").strip()
    if not note_id and not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes append_to_note requires item_id or query.")
    selector = (f"id is {_apple_string(note_id)}" if note_id
                else f"name is {_apple_string(q)}" if exact else f"name contains {_apple_string(q)}")
    lines = text.split("\n")
    added = "".join(f"<div>{html.escape(line) if line.strip() else '<br>'}</div>" for line in lines)
    tail = next(line.strip() for line in reversed(lines) if line.strip())
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Notes"',
        f"    set matches to every note whose {selector}",
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set n to item 1 of matches",
        f"    set body of n to (body of n) & {_apple_string(added)}",
        "    set readBack to every note whose id is (id of n)",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        "    set n to item 1 of readBack",
        f"    if (plaintext of n) does not contain {_apple_string(tail)} then return \"NOT_VERIFIED\"",
        '    return "OK" & us & (id of n as text) & us & (name of n as text)',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="NOTES_APPEND_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The text may or may not have been added; open the note before "
                              "trying again.", outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    status = parts[0] if parts else ""
    if status == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("NOTES_NOTE_NOT_UNIQUE" if count else "NOTES_NOTE_NOT_FOUND",
                              "The note was not uniquely identified; nothing was added. Use item_id from find_notes.",
                              match_count=count)
    if status != "OK":
        raise AppAdapterError("NOTES_APPEND_NOT_VERIFIED", "Notes did not confirm the added text.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "appended": True, "changed": True, "verified": True, "verification": "read_back",
            "note": {"id": parts[1], "title": parts[2]}}


# ── Calendar: find_free_time ─────────────────────────────────────────────────

_HOURS_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-4]):([0-5]\d)$")
_MAX_FREE_DAYS = 31
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _hours(value: Optional[str]) -> Tuple[int, int]:
    text = str(value or "09:00-18:00").replace(" ", "")
    match = _HOURS_RE.match(text)
    if not match:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "hours must look like 09:00-18:00.")
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if end <= start:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "hours must end after they start, e.g. 09:00-18:00.")
    return start, end


def _rule(text: str) -> Dict[str, str]:
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip().upper()] = value.strip().upper()
    return out


def _until(value: str) -> Optional[datetime]:
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            moment = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.endswith("Z"):
            moment = datetime.fromtimestamp(moment.replace(tzinfo=timezone.utc).timestamp())
        return moment if fmt != "%Y%m%d" else moment.replace(hour=23, minute=59, second=59)
    return None


def expand_recurrence(start: datetime, duration: timedelta, rule_text: str, window_start: datetime,
                      window_end: datetime) -> Optional[List[Tuple[datetime, datetime]]]:
    """Occurrences of a simple RRULE inside the window, or None when the rule is beyond what this handles."""
    rule = _rule(rule_text)
    freq = rule.get("FREQ")
    supported = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}
    if freq not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"} or set(rule) - supported:
        return None
    interval = int(rule.get("INTERVAL", "1") or 1)
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = _until(rule["UNTIL"]) if rule.get("UNTIL") else None
    days = [day for day in rule.get("BYDAY", "").split(",") if day]
    if any(day not in _WEEKDAYS for day in days) or (days and freq != "WEEKLY"):
        return None  # "2MO" (the second Monday) and BYDAY outside weekly rules are not handled
    if rule.get("BYMONTHDAY") and (freq != "MONTHLY" or not rule["BYMONTHDAY"].isdigit()):
        return None
    weekdays = {_WEEKDAYS.index(day) for day in days} or {start.weekday()}
    month_day = int(rule.get("BYMONTHDAY") or start.day)
    out: List[Tuple[datetime, datetime]] = []
    seen = 0
    # Whether a day is an occurrence depends only on its distance from the first one, so without COUNT the walk
    # starts just before the window: a weekly meeting begun years ago still fills this week (roadmap #38).
    # COUNT needs every earlier occurrence counted; it also ends the walk, as UNTIL and the window do.
    first = 0 if count is not None else max(0, (window_start.date() - start.date()).days - duration.days - 1)
    for offset in range(first, (window_end.date() - start.date()).days + 1):
        day = start.date() + timedelta(days=offset)
        moment = datetime.combine(day, start.time())
        if moment > window_end or (until and moment > until):
            break
        if freq == "DAILY":
            hit = offset % interval == 0
        elif freq == "WEEKLY":
            week = ((day - timedelta(days=day.weekday())) - (start.date() - timedelta(days=start.weekday()))).days // 7
            hit = day.weekday() in weekdays and week % interval == 0
        elif freq == "MONTHLY":
            months = (day.year - start.year) * 12 + day.month - start.month
            hit = day.day == month_day and months % interval == 0
        else:
            hit = (day.month, day.day) == (start.month, start.day) and (day.year - start.year) % interval == 0
        if not hit:
            continue
        seen += 1
        if count is not None and seen > count:
            break
        if moment + duration > window_start:
            out.append((moment, moment + duration))
    return out


def _free_time_script(start: datetime, end: datetime, calendar: str) -> str:
    calendars = f"calendars whose name is {_apple_string(calendar)}" if calendar else "calendars"
    return f'''set rs to ASCII character 30
set us to ASCII character 31
{_date_setup("fromDate", start)}
{_date_setup("toDate", end)}
set outText to ""
tell application "Calendar"
    set chosen to {calendars}
    if (count of chosen) is 0 then return "NO_CALENDAR"
    repeat with c in chosen
        set inWindow to every event of c whose start date < toDate and end date > fromDate
        set repeating to {{}}
        try
            set repeating to every event of c whose start date < toDate and recurrence is not missing value and recurrence is not ""
        end try
        repeat with e in (inWindow & repeating)
            set ruleText to ""
            try
                set ruleText to recurrence of e as text
            end try
            if ruleText is "missing value" then set ruleText to ""
            set rowText to (uid of e as text) & us & (summary of e as text) & us & (name of c as text) & us & ¬
                (((start date of e) - fromDate) as text) & us & (((end date of e) - (start date of e)) as text) & us & ¬
                ((allday event of e) as text) & us & ruleText & us & "#"
            if outText is not "" then set outText to outText & rs
            set outText to outText & rowText
        end repeat
    end repeat
end tell
return outText'''


def _busy(start: datetime, end: datetime, chosen: str, timeout_s: float) -> Dict[str, Any]:
    """Timed events in the window (repeats expanded), all-day event names, and repeats left unexpanded."""
    raw = _run(_free_time_script(start, end, chosen), timeout_s=timeout_s, code="CALENDAR_FREE_TIME_FAILED")
    if raw == "NO_CALENDAR":
        raise AppAdapterError("CALENDAR_NOT_FOUND", "No calendar has that name.")
    # Each row ends in "#": the output is stripped, and an empty last field would vanish with it.
    rows = _parse_records(raw, ("uid", "summary", "calendar", "offset_s", "length_s", "all_day", "rule", "end"))
    base = start.timestamp()
    events: List[Dict[str, Any]] = []
    all_day: List[str] = []
    unexpanded: List[str] = []
    seen = set()
    for row in rows:
        key = (row["uid"], row["offset_s"])
        if key in seen:
            continue  # a repeating event inside the window is listed twice
        seen.add(key)
        first = datetime.fromtimestamp(base + float(row["offset_s"].replace(",", ".")))
        length = timedelta(seconds=float(row["length_s"].replace(",", ".")))
        if row["all_day"] == "true":
            all_day.append(row["summary"])  # holidays and birthdays do not fill the day
            continue
        if row["rule"]:
            occurrences = expand_recurrence(first, length, row["rule"], start, end)
            if occurrences is None:
                unexpanded.append(row["summary"])
                occurrences = [(first, first + length)] if first < end and first + length > start else []
        else:
            occurrences = [(first, first + length)]
        events.extend({"start": a, "end": b, "title": row["summary"], "calendar": row["calendar"], "uid": row["uid"]}
                      for a, b in occurrences)
    events.sort(key=lambda item: item["start"])
    return {"events": events, "all_day": all_day, "unexpanded": unexpanded}


def _free_slots(busy: List[Tuple[datetime, datetime]], start: datetime, end: datetime, *, day_start: int,
                day_end: int, need: timedelta, include_weekends: bool, limit: int) -> List[Dict[str, Any]]:
    slots: List[Dict[str, Any]] = []
    day = start.date()
    while day <= end.date() and len(slots) <= limit:
        if include_weekends or day.weekday() < 5:
            open_at = max(datetime.combine(day, datetime.min.time()) + timedelta(minutes=day_start), start)
            close_at = min(datetime.combine(day, datetime.min.time()) + timedelta(minutes=day_end), end)
            cursor = open_at
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= close_at:
                    continue
                if busy_start - cursor >= need:
                    slots.append({"start": cursor.isoformat(timespec="minutes"),
                                  "end": busy_start.isoformat(timespec="minutes"),
                                  "minutes": int((busy_start - cursor).total_seconds() // 60)})
                cursor = max(cursor, busy_end)
            if close_at - cursor >= need:
                slots.append({"start": cursor.isoformat(timespec="minutes"), "end": close_at.isoformat(timespec="minutes"),
                              "minutes": int((close_at - cursor).total_seconds() // 60)})
        day += timedelta(days=1)
    return slots


def _buffer(value: Optional[int]) -> timedelta:
    minutes = int(value or 0)
    if not 0 <= minutes <= 120:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "buffer_min must be 0 to 120 minutes.")
    return timedelta(minutes=minutes)


def calendar_free_time(*, date_from: Optional[str], date_to: Optional[str], calendar: Optional[str],
                       duration_min: Optional[int], hours: Optional[str], include_weekends: bool, limit: int,
                       timeout_s: float, now: Optional[datetime] = None,
                       buffer_min: Optional[int] = None) -> Dict[str, Any]:
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    start = max(_parse_date_bound(date_from, end=False), now) if date_from else now
    end = _parse_date_bound(date_to, end=True) if date_to else datetime.combine(start.date() + timedelta(days=6),
                                                                                  datetime.max.time()).replace(microsecond=0)
    if end <= start:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "date_to must be after date_from (and after now).")
    if (end - start).days > _MAX_FREE_DAYS:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", f"Look at most {_MAX_FREE_DAYS} days ahead at once.")
    minutes = int(duration_min or 30)
    if not 5 <= minutes <= 12 * 60:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "duration_min must be 5 to 720 minutes.")
    day_start, day_end = _hours(hours)
    pad = _buffer(buffer_min)
    chosen = _single_line(calendar, "calendar", 200)
    found = _busy(start, end, chosen, timeout_s)
    all_day, unexpanded = found["all_day"], found["unexpanded"]
    # A buffer keeps room around each event (travel, a break): the event counts as that much longer each side.
    busy = sorted((item["start"] - pad, item["end"] + pad) for item in found["events"])
    slots = _free_slots(busy, start, end, day_start=day_start, day_end=day_end, need=timedelta(minutes=minutes),
                        include_weekends=include_weekends, limit=limit)
    out: Dict[str, Any] = {
        "ok": True, "count": len(slots[:limit]), "free": slots[:limit], "truncated": len(slots) > limit,
        "date_from": start.isoformat(timespec="minutes"), "date_to": end.isoformat(timespec="minutes"),
        "hours": f"{day_start // 60:02d}:{day_start % 60:02d}-{day_end // 60:02d}:{day_end % 60:02d}",
        "duration_min": minutes, "weekends": include_weekends, "busy_events": len(busy),
        **({"buffer_min": int(pad.total_seconds() // 60)} if pad else {}),
        "all_day_events_ignored": sorted(set(all_day))[:20],
    }
    if unexpanded:
        # Said plainly instead of offering time that may be taken.
        out["approximate"] = True
        out["unexpanded_repeating_events"] = sorted(set(unexpanded))[:20]
        out["message"] = ("Some repeating events use rules Mac MCP does not expand; only their first time is counted, "
                          "so check these slots against them.")
    return out


# ── Calendar: plan_day (a read-only look at one day) ────────────────────────

def _due_script(day_start: datetime, day_end: datetime) -> str:
    return f'''set rs to ASCII character 30
set us to ASCII character 31
{_date_setup("dayStart", day_start)}
{_date_setup("dayEnd", day_end)}
set outText to ""
tell application "Reminders"
    repeat with l in lists
        -- Read in bulk and compared here: a whose-clause on due date fails on reminders that have none.
        set ids to id of (every reminder of l whose completed is false)
        set names to name of (every reminder of l whose completed is false)
        set dues to due date of (every reminder of l whose completed is false)
        repeat with i from 1 to count of ids
            set d to item i of dues
            if d is not missing value then
                if d ≤ dayEnd then
                    set rowText to (item i of ids as text) & us & (item i of names as text) & us & (name of l as text) & us & ¬
                        ((d - dayStart) as text) & us & "#"
                    if outText is not "" then set outText to outText & rs
                    set outText to outText & rowText
                end if
            end if
        end repeat
    end repeat
end tell
return outText'''


def plan_day(*, date_from: Optional[str], calendar: Optional[str], duration_min: Optional[int], hours: Optional[str],
             buffer_min: Optional[int], limit: int, timeout_s: float, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One day at a glance: its events, the free time between them, and reminders due by its end (overdue too).

    Read-only: it suggests nothing it would then do; the agent proposes and the person decides."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    day = _parse_date_bound(date_from, end=False).date() if date_from else now.date()
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(day, datetime.max.time()).replace(microsecond=0)
    day_start, day_end = _hours(hours)
    pad = _buffer(buffer_min)
    minutes = int(duration_min or 30)
    if not 5 <= minutes <= 12 * 60:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "duration_min must be 5 to 720 minutes.")
    found = _busy(start, end, _single_line(calendar, "calendar", 200), timeout_s)
    busy = sorted((item["start"] - pad, item["end"] + pad) for item in found["events"])
    free = _free_slots(busy, max(start, now), end, day_start=day_start, day_end=day_end,
                       need=timedelta(minutes=minutes), include_weekends=True, limit=limit)
    raw = _run(_due_script(start, end), timeout_s=timeout_s, code="REMINDERS_LIST_FAILED")
    due = []
    for row in _parse_records(raw, ("id", "name", "list", "offset_s", "end")):
        when = start + timedelta(seconds=float(row["offset_s"].replace(",", ".")))
        due.append({"id": row["id"], "name": row["name"], "list": row["list"],
                    "due": when.isoformat(timespec="minutes"), "overdue": when < start})
    due.sort(key=lambda item: item["due"])
    out: Dict[str, Any] = {
        "ok": True, "date": day.isoformat(),
        "events": [{"title": item["title"], "calendar": item["calendar"], "uid": item["uid"],
                    "start": item["start"].isoformat(timespec="minutes"), "end": item["end"].isoformat(timespec="minutes")}
                   for item in found["events"]][:50],
        "all_day": sorted(set(found["all_day"]))[:20],
        "free": free[:limit], "reminders_due": due[:50],
        "hours": f"{day_start // 60:02d}:{day_start % 60:02d}-{day_end // 60:02d}:{day_end % 60:02d}",
        "duration_min": minutes, **({"buffer_min": int(pad.total_seconds() // 60)} if pad else {}),
    }
    if found["unexpanded"]:
        out["approximate"] = True
        out["unexpanded_repeating_events"] = sorted(set(found["unexpanded"]))[:20]
    return out


# ── Reminders: update_reminder, delete_reminder ─────────────────────────────

def _reminder_error(parts: List[str], what: str) -> None:
    status = parts[0] if parts else ""
    if status == "COUNT":
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        raise AppAdapterError("REMINDER_NOT_UNIQUE" if count else "REMINDER_NOT_FOUND",
                              f"Reminder was not uniquely identified; nothing was {what}. Use item_id from list_reminders.",
                              match_count=count)


def reminders_update(item_id: Optional[str], *, title: Optional[str], due: Optional[str], notes: Optional[str],
                     timeout_s: float) -> Dict[str, Any]:
    """Rename a reminder, move its due date (defer it) or replace its notes; read back after the change."""
    rid = str(item_id or "").strip()
    if not rid:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Reminders update_reminder requires item_id from list_reminders.")
    name = _single_line(title, "title", 300)
    text = None if notes is None else _plain_text(notes, "notes", 5000)
    due_text = str(due or "").strip()
    if not (name or due_text or text is not None):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Pass title, start (the new due date) and/or notes to change.")
    changes: List[str] = []
    if name:
        changes.append(f"set name of r to {_apple_string(name)}")
    if due_text:
        when = _event_datetime(due_text, "start")
        changes += _date_setup("dueDate", when).split("\n")
        changes.append("set allday due date of r to dueDate" if len(due_text) == 10 else "set due date of r to dueDate")
    if text is not None:
        changes.append(f"set body of r to {_apple_string(text)}")
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Reminders"',
        f"    set matches to every reminder whose id is {_apple_string(rid)}",
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set r to item 1 of matches",
        *[f"    {line}" for line in changes],
        f"    set readBack to every reminder whose id is {_apple_string(rid)}",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        "    set r to item 1 of readBack",
        '    set dueText to ""',
        "    try",
        "        if due date of r is not missing value then set dueText to (due date of r) as text",
        "    end try",
        '    return "OK" & us & (id of r as text) & us & (name of r as text) & us & (name of container of r as text) & us & dueText & us & "#"',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="REMINDERS_UPDATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} Check with list_reminders before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    _reminder_error(parts, "changed")
    if parts[0] != "OK" or (name and parts[2] != name):
        raise AppAdapterError("REMINDER_UPDATE_NOT_VERIFIED", "Reminders did not confirm the change.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "updated": True, "changed": True, "verified": True, "verification": "read_back",
            "reminder": dict(zip(_REMINDER_ROW, parts[1:5]))}


def reminders_delete(item_id: Optional[str], *, timeout_s: float) -> Dict[str, Any]:
    rid = str(item_id or "").strip()
    if not rid:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Reminders delete_reminder requires item_id from list_reminders.")
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Reminders"',
        f"    set matches to every reminder whose id is {_apple_string(rid)}",
        '    if (count of matches) is 0 then return "GONE"',
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set r to item 1 of matches",
        "    set gone to (name of r as text)",
        "    delete r",
        f"    if (count of (every reminder whose id is {_apple_string(rid)})) is not 0 then return \"NOT_VERIFIED\"",
        '    return "OK" & us & gone',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="REMINDERS_DELETE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} Check with list_reminders before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    if parts[0] == "GONE":
        # A replayed delete finds nothing left to delete.
        return {"ok": True, "deleted": False, "changed": False, "verified": True, "verification": "already_absent",
                "message": "No reminder has that id (it may already be deleted); nothing was changed."}
    _reminder_error(parts, "deleted")
    if parts[0] != "OK":
        raise AppAdapterError("REMINDER_DELETE_NOT_VERIFIED", "Reminders did not confirm the reminder is gone.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "deleted": True, "changed": True, "verified": True, "verification": "read_back",
            "reminder": {"id": rid, "name": parts[1]}}


# ── Notes: update_note ───────────────────────────────────────────────────────

def _note_body(title: str, body: str) -> str:
    # Notes names a note after its first line, so the title leads the body.
    lines = [f"<div><h1>{html.escape(title)}</h1></div>"]
    lines += [f"<div>{html.escape(line) if line.strip() else '<br>'}</div>" for line in body.split("\n")] if body else []
    return "".join(lines)


def notes_update(item_id: Optional[str], *, title: Optional[str], body: Optional[str], timeout_s: float) -> Dict[str, Any]:
    """Replace a note's text (and/or title). Only by item_id: replacing text is never done on a name guess."""
    note_id = str(item_id or "").strip()
    if not note_id:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Notes update_note requires item_id from find_notes.")
    name = _single_line(title, "title", 300)
    text = None if body is None else _plain_text(body, "body", 20000).strip("\n")
    if not name and text is None:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Pass title and/or body to change.")
    selector = f"every note whose id is {_apple_string(note_id)}"
    if text is None:
        change = [f"    set name of n to {_apple_string(name)}"]
        check = f"(name of n as text) is {_apple_string(name)}"
    else:
        if not name:  # keep the current title as the first line
            current = _run("\n".join(['tell application "Notes"', f"    set matches to {selector}",
                                       '    if (count of matches) is not 1 then return ""',
                                       "    return name of (item 1 of matches) as text", "end tell"]),
                           timeout_s=timeout_s, code="NOTES_UPDATE_FAILED")
            name = current or "Untitled"
        tail = next((line.strip() for line in reversed(text.split("\n")) if line.strip()), name)
        change = [f"    set body of n to {_apple_string(_note_body(name, text))}"]
        check = f"(plaintext of n) contains {_apple_string(tail)}"
    script = "\n".join([
        "set us to ASCII character 31",
        'tell application "Notes"',
        f"    set matches to {selector}",
        '    if (count of matches) is not 1 then return "COUNT" & us & ((count of matches) as text)',
        "    set n to item 1 of matches",
        *change,
        f"    set readBack to {selector}",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        "    set n to item 1 of readBack",
        f'    if not ({check}) then return "NOT_VERIFIED"',
        '    return "OK" & us & (id of n as text) & us & (name of n as text)',
        "end tell",
    ])
    try:
        raw = _run(script, timeout_s=timeout_s, code="NOTES_UPDATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The note may or may not have changed; open it before trying again.",
                              outcome_unknown=True, automatic_retry=False) from exc
    parts = raw.split(_US)
    if parts[0] == "COUNT":
        raise AppAdapterError("NOTES_NOTE_NOT_FOUND", "No note has that id; nothing was changed. Use item_id from find_notes.")
    if parts[0] != "OK":
        raise AppAdapterError("NOTES_UPDATE_NOT_VERIFIED", "Notes did not confirm the change.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "updated": True, "changed": True, "verified": True, "verification": "read_back",
            "note": {"id": parts[1], "title": parts[2]}}


# ── Contacts: find_contacts ──────────────────────────────────────────────────

_CONTACT_FIELDS = ("id", "name", "organization", "emails", "phones", "end")


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _contacts_by_address(q: str, *, limit: int, timeout_s: float) -> Dict[str, Any]:
    """People with an email or phone number containing q (#196), read in bulk: one request per field, not per person."""
    script = '''set rs to ASCII character 30
set us to ASCII character 31
set AppleScript's text item delimiters to ", "
tell application "Contacts"
    set idValues to id of every person
    set nameValues to name of every person
    set mailValues to value of every email of every person
    set phoneValues to value of every phone of every person
end tell
set outText to ""
repeat with i from 1 to count of idValues
    set rowText to (item i of idValues as text) & us & (item i of nameValues as text) & us & "" & us & ((item i of mailValues) as text) & us & ((item i of phoneValues) as text) & us & "#"
    if outText is not "" then set outText to outText & rs
    set outText to outText & rowText
end repeat
set AppleScript's text item delimiters to ""
return outText'''
    raw = _run(script, timeout_s=max(timeout_s, 15.0), code="CONTACTS_FIND_FAILED")
    needle, digits = q.casefold(), _digits(q)
    rows = []
    for row in _parse_records(raw, _CONTACT_FIELDS):
        row.pop("end", None)
        emails = [item for item in row["emails"].split(", ") if item]
        phones = [item for item in row["phones"].split(", ") if item]
        hit = any(needle in item.casefold() for item in emails) if "@" in q else \
            any(digits and digits[-9:] in _digits(item) for item in phones)
        if hit:
            rows.append({**row, "emails": emails, "phones": phones})
        if len(rows) >= limit:
            break
    return {"ok": True, "count": len(rows), "contacts": rows, "truncated": len(rows) >= limit,
            "matched_on": "email" if "@" in q else "phone"}


def contacts_find(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    q = _single_line(query, "query", 200)
    if not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Contacts find_contacts requires query.")
    if "@" in q or (len(_digits(q)) >= 7 and not re.search(r"[^\d\s()+\-.]", q)):
        return _contacts_by_address(q, limit=limit, timeout_s=timeout_s)
    script = f'''set rs to ASCII character 30
set us to ASCII character 31
set maxRows to {limit}
set outText to ""
set rowCount to 0
set q to {_apple_string(q)}
tell application "Contacts"
    set matches to every person whose name contains q or organization contains q
    repeat with p in matches
        if rowCount >= maxRows then exit repeat
        set orgText to ""
        try
            if organization of p is not missing value then set orgText to organization of p
        end try
        set AppleScript's text item delimiters to ", "
        set mailText to (value of emails of p) as text
        set phoneText to (value of phones of p) as text
        set AppleScript's text item delimiters to ""
        set rowText to (id of p as text) & us & (name of p as text) & us & orgText & us & mailText & us & phoneText & us & "#"
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="CONTACTS_FIND_FAILED")
    rows = []
    for row in _parse_records(raw, _CONTACT_FIELDS):
        row.pop("end", None)
        rows.append({**row, "emails": [item for item in row["emails"].split(", ") if item],
                     "phones": [item for item in row["phones"].split(", ") if item]})
    return {"ok": True, "count": len(rows), "contacts": rows, "truncated": len(rows) >= limit}


# ── Contacts: create_contact, update_contact (#43) ───────────────────────────

def _person_view(person: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": person["id"], "name": person["name"], "organization": person["organization"] or None,
            "emails": [value for _label, value in person["emails"]],
            "phones": [value for _label, value in person["phones"]]}


def _split_name(name: str) -> Tuple[str, str]:
    """The last word is the family name ("Ahmet Can Yılmaz" → "Ahmet Can", "Yılmaz"); one word is a first name."""
    parts = name.split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (name, "")


def _same_phone(a: str, b: str) -> bool:
    da, db = _digits(a), _digits(b)
    return bool(da) and bool(db) and da[-9:] == db[-9:]


def _contact_inputs(email: Optional[str], phone: Optional[str]) -> Tuple[str, str]:
    mail = _single_line(email, "email", 320)
    if mail and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", mail):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "email is not an email address.")
    number = _single_line(phone, "phone", 64)
    if number and len(_digits(number)) < 3:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "phone is not a phone number.")
    return mail, number


def contacts_create(title: Optional[str], *, email: Optional[str], phone: Optional[str], organization: Optional[str],
                    notes: Optional[str], timeout_s: float) -> Dict[str, Any]:
    from . import app_undo

    name = " ".join(_single_line(title, "title", 200).split())
    if not name:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Contacts create_contact requires title (the person's name).")
    mail, number = _contact_inputs(email, phone)
    org = _single_line(organization, "organization", 200)
    note = _plain_text(notes, "notes", 5000)
    # A replayed request finds the person it already made: same name, and the same address, number and company.
    for person in _people_named(name, timeout_s):
        values = [value for _label, value in person["emails"]]
        if ((not mail or mail.lower() in (v.lower() for v in values))
                and (not number or any(_same_phone(number, v) for _l, v in person["phones"]))
                and (not org or org == person["organization"])):
            return {"ok": True, "created": False, "duplicate": True, "verified": True,
                    "verification": "existing_contact_matched", "contact": _person_view(person),
                    "message": "A contact with this name and these details is already there; nothing was created."}
    first, last = _split_name(name)
    props = [f"first name:{_apple_string(first)}"] + ([f"last name:{_apple_string(last)}"] if last else []) \
        + ([f"organization:{_apple_string(org)}"] if org else []) + ([f"note:{_apple_string(note)}"] if note else [])
    script = "\n".join([
        'tell application "Contacts"',
        f"    set p to make new person with properties {{{', '.join(props)}}}",
        *([f"    make new email at end of emails of p with properties {{label:\"work\", value:{_apple_string(mail)}}}"]
          if mail else []),
        *([f"    make new phone at end of phones of p with properties {{label:\"mobile\", value:{_apple_string(number)}}}"]
          if number else []),
        "    save",
        "    return id of p as text",
        "end tell",
    ])
    try:
        new_id = _run(script, timeout_s=timeout_s, code="CONTACTS_CREATE_FAILED").strip()
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} The contact may or may not exist; check with find_contacts before "
                              "trying again.", outcome_unknown=True, automatic_retry=False) from exc
    person = app_undo.read_person(new_id, timeout_s) if new_id else None
    if person is None:
        raise AppAdapterError("CONTACTS_CREATE_NOT_VERIFIED", "Contacts did not confirm the new contact.",
                              outcome_unknown=True, automatic_retry=False)
    checks = [person["name"] == name or (person["first_name"], person["last_name"]) == (first, last),
              not mail or mail in [v for _l, v in person["emails"]],
              not number or any(_same_phone(number, v) for _l, v in person["phones"]),
              not org or person["organization"] == org]
    return {"ok": True, "created": True, "verified": all(checks), "verification": "read_back",
            "contact": _person_view(person)}


def _people_named(name: str, timeout_s: float) -> List[Dict[str, Any]]:
    from . import app_undo

    raw = _run("\n".join([
        "set rs to ASCII character 30",
        'tell application "Contacts"',
        f"    set matches to every person whose name is {_apple_string(name)}",
        '    set outText to ""',
        "    repeat with p in matches",
        "        if outText is not \"\" then set outText to outText & rs",
        "        set outText to outText & (id of p as text)",
        "    end repeat",
        "    return outText",
        "end tell",
    ]), timeout_s=timeout_s, code="CONTACTS_FIND_FAILED")
    people = [app_undo.read_person(pid, timeout_s) for pid in raw.split(chr(30)) if pid.strip()][:20]
    return [person for person in people if person]


def contacts_resolve(query: Optional[str], *, timeout_s: float) -> str:
    """The id of the one person a query names; several or none change nothing and say so."""
    if not str(query or "").strip():
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Contacts update_contact requires item_id or a query "
                              "that matches one contact.")
    found = contacts_find(query, limit=20, timeout_s=timeout_s)
    people = found["contacts"]
    if not people:
        raise AppAdapterError("CONTACTS_PERSON_NOT_FOUND", "No contact matches; nothing was changed. Use find_contacts.")
    if len(people) > 1:
        raise AppAdapterError("CONTACTS_PERSON_NOT_UNIQUE",
                              "Several contacts match; pass item_id from find_contacts. Nothing was changed.",
                              candidates=[{"id": p["id"], "name": p["name"], "organization": p["organization"] or None,
                                           "emails": p["emails"]} for p in people[:10]])
    return people[0]["id"]


def contacts_update(item_id: Optional[str], *, title: Optional[str], email: Optional[str], phone: Optional[str],
                    organization: Optional[str], notes: Optional[str], timeout_s: float) -> Dict[str, Any]:
    from . import app_undo

    person_id = str(item_id or "").strip()
    if not person_id:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Contacts update_contact requires item_id or a query "
                              "that matches one contact.")
    mail, number = _contact_inputs(email, phone)
    name = " ".join(_single_line(title, "title", 200).split()) if title is not None else None
    org = _single_line(organization, "organization", 200) if organization is not None else None
    note = _plain_text(notes, "notes", 5000) if notes is not None else None
    if not any([name, mail, number, org is not None, note is not None]):
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                              "update_contact needs a field to change: title, email, phone, organization or notes.")
    person = app_undo.read_person(person_id, timeout_s)
    if person is None:
        raise AppAdapterError("CONTACTS_PERSON_NOT_FOUND", "No contact has that id; nothing was changed.")
    lines: List[str] = []
    changed: List[str] = []
    if name:
        first, last = _split_name(name)
        lines += [f"    set first name of p to {_apple_string(first)}", f"    set last name of p to {_apple_string(last)}"]
        changed.append("name")
    if org is not None:
        lines.append(f"    set organization of p to {_apple_string(org)}")
        changed.append("organization")
    if note is not None:
        lines.append(f"    set note of p to {_apple_string(note)}")
        changed.append("note")
    for kind, value, label in (("email", mail, "work"), ("phone", number, "mobile")):
        if not value:
            continue
        present = person[f"{kind}s"]
        # Which of several addresses to replace is not guessed: nothing changes and the person is told.
        if len(present) > 1:
            raise AppAdapterError("CONTACTS_FIELD_AMBIGUOUS",
                                  f"This contact has {len(present)} {kind}s, so which one to replace is unclear; "
                                  "nothing was changed.", **{f"{kind}s": [v for _l, v in present]})
        lines.append(f"    set value of item 1 of {kind}s of p to {_apple_string(value)}" if present else
                     f"    make new {kind} at end of {kind}s of p with properties "
                     f"{{label:{_apple_string(label)}, value:{_apple_string(value)}}}")
        changed.append(kind)
    script = "\n".join([
        'tell application "Contacts"',
        f"    set matches to every person whose id is {_apple_string(person_id)}",
        '    if (count of matches) is not 1 then return "GONE"',
        "    set p to item 1 of matches",
        *lines,
        "    save",
        '    return "OK"',
        "end tell",
    ])
    try:
        status = _run(script, timeout_s=timeout_s, code="CONTACTS_UPDATE_FAILED")
    except AppAdapterError as exc:
        if exc.extra.get("not_executed"):
            raise
        raise AppAdapterError(exc.code, f"{exc} Some fields may have changed; read the contact with find_contacts "
                              "before trying again.", outcome_unknown=True, automatic_retry=False) from exc
    if status != "OK":
        raise AppAdapterError("CONTACTS_PERSON_NOT_FOUND", "The contact is no longer there; nothing was changed.")
    after = app_undo.read_person(person_id, timeout_s)
    if after is None:
        raise AppAdapterError("CONTACTS_UPDATE_NOT_VERIFIED", "The contact could not be read back after the change.",
                              outcome_unknown=True, automatic_retry=False)
    mismatched = [field for field, ok in (
        ("name", not name or (after["first_name"], after["last_name"]) == _split_name(name)),
        ("organization", org is None or after["organization"] == org),
        ("note", note is None or after["note"] == note),
        ("email", not mail or mail in [v for _l, v in after["emails"]]),
        ("phone", not number or any(_same_phone(number, v) for _l, v in after["phones"])),
    ) if not ok]
    out = {"ok": True, "changed": True, "changed_fields": changed, "verified": not mismatched,
           "verification": "read_back", "contact": _person_view(after)}
    if mismatched:
        out["not_verified_fields"] = mismatched
    return out


# ── Shortcuts: list_shortcuts, run_shortcut ──────────────────────────────────

SHORTCUTS = "/usr/bin/shortcuts"
SHORTCUT_OUTPUT_LIMIT = 20_000
_LISTED = re.compile(r"^(?P<name>.*) \((?P<id>[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\)$")


def _shortcuts(args: List[str], timeout_s: float) -> subprocess.CompletedProcess:
    if not os.path.exists(SHORTCUTS):
        raise AppAdapterError("SHORTCUTS_UNAVAILABLE", "The shortcuts command is not on this Mac (macOS 12 or later).")
    try:
        return subprocess.run([SHORTCUTS, *args], capture_output=True, text=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise AppAdapterError("SHORTCUT_TIMED_OUT", f"The shortcut did not finish within {int(timeout_s)} s.",
                              outcome_unknown=True, automatic_retry=False) from exc


def shortcuts_list(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    q = _single_line(query, "query", 200).lower()
    done = _shortcuts(["list", "--show-identifiers"], timeout_s)
    if done.returncode != 0:
        raise AppAdapterError("SHORTCUTS_LIST_FAILED", (done.stderr or "shortcuts list failed").strip()[:400])
    rows = []
    for line in done.stdout.splitlines():
        match = _LISTED.match(line.strip())
        if not match or (q and q not in match.group("name").lower()):
            continue
        rows.append({"id": match.group("id"), "name": match.group("name")})
    return {"ok": True, "count": min(len(rows), limit), "shortcuts": rows[:limit], "truncated": len(rows) > limit}


def owner_allowed_shortcuts() -> List[str]:
    """apps.shortcuts.allowed: shortcuts the owner lets agents run even while running commands is off (#195)."""
    from .runtime_settings import load_runtime_settings

    apps = load_runtime_settings().get("apps")
    entry = apps.get("shortcuts") if isinstance(apps, dict) else None
    names = entry.get("allowed") if isinstance(entry, dict) else None
    return [str(name) for name in names if isinstance(name, str) and name.strip()] if isinstance(names, list) else []


def shortcuts_run(item_id: Optional[str], title: Optional[str], body: Optional[str], *, timeout_s: float,
                  allowed_only: bool = False) -> Dict[str, Any]:
    wanted = str(item_id or "").strip().upper()
    name = _single_line(title, "title", 200)
    if not wanted and not name:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID",
                              "Shortcuts run_shortcut requires item_id (from list_shortcuts) or title (its exact name).")
    listed = shortcuts_list(None, limit=10_000, timeout_s=min(timeout_s, 15.0))["shortcuts"]
    matches = [row for row in listed if row["id"] == wanted] if wanted else [row for row in listed if row["name"] == name]
    if len(matches) != 1:
        raise AppAdapterError("SHORTCUT_NOT_FOUND" if not matches else "SHORTCUT_NOT_UNIQUE",
                              "No shortcut has that id or exact name; nothing ran." if not matches
                              else "More than one shortcut has that name; pass item_id. Nothing ran.",
                              match_count=len(matches))
    shortcut = matches[0]
    if allowed_only and shortcut["name"] not in owner_allowed_shortcuts():
        raise AppAdapterError("SHELL_DISABLED", f"Running commands is off, and {shortcut['name']!r} isn't one of the "
                              "shortcuts you allowed in Settings › Apps; nothing ran.", shortcut=shortcut)
    text = _plain_text(body, "body", 20000)
    with tempfile.TemporaryDirectory(prefix="mac-mcp-shortcut-") as folder:
        out_path = os.path.join(folder, "output.txt")
        args = ["run", shortcut["id"], "--output-path", out_path, "--output-type", "public.plain-text"]
        if text:
            in_path = os.path.join(folder, "input.txt")
            with open(in_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            args += ["--input-path", in_path]
        done = _shortcuts(args, timeout_s)
        output = ""
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
                output = handle.read(SHORTCUT_OUTPUT_LIMIT + 1)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:400] or f"exit code {done.returncode}"
        # A shortcut may have done part of its work before it failed.
        raise AppAdapterError("SHORTCUT_FAILED", f"The shortcut stopped with an error: {detail}",
                              outcome_unknown=True, automatic_retry=False, shortcut=shortcut)
    return {"ok": True, "ran": True, "shortcut": shortcut, "output": output[:SHORTCUT_OUTPUT_LIMIT],
            "output_truncated": len(output) > SHORTCUT_OUTPUT_LIMIT}


__all__ = ["calendar_free_time", "contacts_find", "expand_recurrence", "notes_append", "notes_update",
           "owner_allowed_shortcuts", "plan_day", "reminders_create", "reminders_delete", "reminders_update",
           "shortcuts_list", "shortcuts_run"]
