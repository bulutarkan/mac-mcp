"""More first-party app actions (Group 4): reminders, appending to notes, free time, contacts, shortcuts.

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
    readback = ("(id of r as text) & us & (name of r as text) & us & (name of l as text) & us & dueText")
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
                "verification": "existing_reminder_matched", "reminder": dict(zip(_REMINDER_ROW, parts[1:])),
                "message": "An open reminder with this title is already in that list; nothing was created."}
    if status != "OK":
        raise AppAdapterError("REMINDERS_CREATE_NOT_VERIFIED", "Reminders did not confirm the new reminder.",
                              outcome_unknown=True, automatic_retry=False)
    return {"ok": True, "created": True, "verified": True, "verification": "read_back",
            "reminder": dict(zip(_REMINDER_ROW, parts[1:]))}


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
        "    set lengthBefore to length of (plaintext of n)",
        f"    set body of n to (body of n) & {_apple_string(added)}",
        "    set readBack to every note whose id is (id of n)",
        '    if (count of readBack) is not 1 then return "NOT_VERIFIED"',
        "    set n to item 1 of readBack",
        f"    if (plaintext of n) does not contain {_apple_string(tail)} then return \"NOT_VERIFIED\"",
        '    return "OK" & us & (id of n as text) & us & (name of n as text) & us & (lengthBefore as text) & us & ((length of (plaintext of n)) as text)',
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
    for offset in range(0, 366 * 6):
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
                ((allday event of e) as text) & us & ruleText
            if outText is not "" then set outText to outText & rs
            set outText to outText & rowText
        end repeat
    end repeat
end tell
return outText'''


def calendar_free_time(*, date_from: Optional[str], date_to: Optional[str], calendar: Optional[str],
                       duration_min: Optional[int], hours: Optional[str], include_weekends: bool, limit: int,
                       timeout_s: float, now: Optional[datetime] = None) -> Dict[str, Any]:
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
    chosen = _single_line(calendar, "calendar", 200)
    raw = _run(_free_time_script(start, end, chosen), timeout_s=timeout_s, code="CALENDAR_FREE_TIME_FAILED")
    if raw == "NO_CALENDAR":
        raise AppAdapterError("CALENDAR_NOT_FOUND", "No calendar has that name.")
    rows = _parse_records(raw, ("uid", "summary", "calendar", "offset_s", "length_s", "all_day", "rule"))
    base = start.timestamp()
    busy: List[Tuple[datetime, datetime]] = []
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
            busy.extend(occurrences)
        else:
            busy.append((first, first + length))
    busy.sort()
    slots: List[Dict[str, Any]] = []
    need = timedelta(minutes=minutes)
    day = start.date()
    while day <= end.date() and len(slots) < limit:
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
    out: Dict[str, Any] = {
        "ok": True, "count": len(slots[:limit]), "free": slots[:limit], "truncated": len(slots) > limit,
        "date_from": start.isoformat(timespec="minutes"), "date_to": end.isoformat(timespec="minutes"),
        "hours": f"{day_start // 60:02d}:{day_start % 60:02d}-{day_end // 60:02d}:{day_end % 60:02d}",
        "duration_min": minutes, "weekends": include_weekends, "busy_events": len(busy),
        "all_day_events_ignored": sorted(set(all_day))[:20],
    }
    if unexpanded:
        # Said plainly instead of offering time that may be taken.
        out["approximate"] = True
        out["unexpanded_repeating_events"] = sorted(set(unexpanded))[:20]
        out["message"] = ("Some repeating events use rules Mac MCP does not expand; only their first time is counted, "
                          "so check these slots against them.")
    return out


# ── Contacts: find_contacts ──────────────────────────────────────────────────

_CONTACT_FIELDS = ("id", "name", "organization", "emails", "phones")


def contacts_find(query: Optional[str], *, limit: int, timeout_s: float) -> Dict[str, Any]:
    q = _single_line(query, "query", 200)
    if not q:
        raise AppAdapterError("APP_ADAPTER_ARGUMENT_INVALID", "Contacts find_contacts requires query.")
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
        set rowText to (id of p as text) & us & (name of p as text) & us & orgText & us & mailText & us & phoneText
        if outText is not "" then set outText to outText & rs
        set outText to outText & rowText
        set rowCount to rowCount + 1
    end repeat
end tell
return outText'''
    raw = _run(script, timeout_s=timeout_s, code="CONTACTS_FIND_FAILED")
    rows = []
    for row in _parse_records(raw, _CONTACT_FIELDS):
        rows.append({**row, "emails": [item for item in row["emails"].split(", ") if item],
                     "phones": [item for item in row["phones"].split(", ") if item]})
    return {"ok": True, "count": len(rows), "contacts": rows, "truncated": len(rows) >= limit}


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


def shortcuts_run(item_id: Optional[str], title: Optional[str], body: Optional[str], *, timeout_s: float) -> Dict[str, Any]:
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


__all__ = ["calendar_free_time", "contacts_find", "expand_recurrence", "notes_append", "reminders_create",
           "shortcuts_list", "shortcuts_run"]
