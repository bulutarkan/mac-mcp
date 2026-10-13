"""When a scheduled task runs (#175): plain schedules and a cron subset, in the Mac's local time.

A schedule is a small dict:

    {"type": "once", "at": "2026-10-20T09:00"}
    {"type": "daily", "time": "09:00"}
    {"type": "weekly", "time": "09:00", "weekdays": [0]}          # 0 = Monday
    {"type": "monthly", "time": "09:00", "day": 31}               # past month end = last day
    {"type": "hours", "every": 3, "start": "08:00"}               # 08:00, 11:00, ... each day
    {"type": "cron", "expr": "0 9 * * 1"}                         # minute hour day month weekday

Times are wall-clock times on this Mac: a naive datetime is converted with the system's
own time zone rules, so a daily 09:00 stays 09:00 across daylight-saving changes.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

TYPES = ("once", "daily", "weekly", "monthly", "hours", "cron")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SEARCH_DAYS = 366 * 5


class ScheduleError(ValueError):
    """A schedule the owner or an agent wrote that cannot be used, with a plain reason."""


def _time(value: Any, field: str = "time") -> tuple[int, int]:
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        raise ScheduleError(f"{field} must be a time like 09:00")
    return int(match.group(1)), int(match.group(2))


def _cron_field(text: str, low: int, high: int, name: str) -> Set[int]:
    values: Set[int] = set()
    for part in text.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ScheduleError(f"cron {name}: bad step")
            step = int(raw_step)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ScheduleError(f"cron {name}: bad range")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step > 1:
                end = high
        else:
            raise ScheduleError(f"cron {name}: '{part}' is not a number")
        if start < low or end > high or start > end:
            raise ScheduleError(f"cron {name} must be between {low} and {high}")
        values.update(range(start, end + 1, step))
    return values


def _parse_cron(expr: str) -> Dict[str, Any]:
    fields = str(expr or "").split()
    if len(fields) != 5:
        raise ScheduleError("a cron schedule has 5 fields: minute hour day month weekday")
    minutes = _cron_field(fields[0], 0, 59, "minute")
    hours = _cron_field(fields[1], 0, 23, "hour")
    days = _cron_field(fields[2], 1, 31, "day")
    months = _cron_field(fields[3], 1, 12, "month")
    weekdays = {(value % 7) for value in _cron_field(fields[4], 0, 7, "weekday")}  # 0 and 7 = Sunday
    return {
        "minutes": sorted(minutes), "hours": sorted(hours), "days": days, "months": months,
        "weekdays": weekdays, "days_any": fields[2] == "*", "weekdays_any": fields[4] == "*",
    }


def validate(schedule: Any) -> Dict[str, Any]:
    """The schedule in normal form, or ScheduleError saying what is wrong."""
    if not isinstance(schedule, dict):
        raise ScheduleError("schedule must be an object such as {type: 'daily', time: '09:00'}")
    kind = str(schedule.get("type") or "").strip().lower()
    if kind not in TYPES:
        raise ScheduleError(f"schedule type must be one of: {', '.join(TYPES)}")
    if kind == "once":
        try:
            at = datetime.fromisoformat(str(schedule.get("at") or "").strip())
        except ValueError as exc:
            raise ScheduleError("a one-time schedule needs at, e.g. 2026-10-20T09:00") from exc
        return {"type": kind, "at": at.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes")}
    if kind == "cron":
        expr = " ".join(str(schedule.get("expr") or "").split())
        _parse_cron(expr)
        return {"type": kind, "expr": expr}
    hour, minute = _time(schedule.get("start" if kind == "hours" else "time", "00:00" if kind == "hours" else None),
                         "start" if kind == "hours" else "time")
    clock = f"{hour:02d}:{minute:02d}"
    if kind == "daily":
        return {"type": kind, "time": clock}
    if kind == "weekly":
        raw = schedule.get("weekdays")
        if not isinstance(raw, list) or not raw or not all(isinstance(day, int) and 0 <= day <= 6 for day in raw):
            raise ScheduleError("a weekly schedule needs weekdays, 0 (Monday) to 6 (Sunday)")
        return {"type": kind, "time": clock, "weekdays": sorted(set(raw))}
    if kind == "monthly":
        day = schedule.get("day")
        if not isinstance(day, int) or not 1 <= day <= 31:
            raise ScheduleError("a monthly schedule needs day, 1 to 31")
        return {"type": kind, "time": clock, "day": day}
    every = schedule.get("every")
    if not isinstance(every, int) or not 1 <= every <= 24:
        raise ScheduleError("every must be a whole number of hours, 1 to 24")
    return {"type": kind, "every": every, "start": clock}


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def _ts(moment: datetime) -> float:
    return moment.timestamp()  # naive = local wall time, with the system's DST rules


def _times_on(day: date, schedule: Dict[str, Any]) -> Iterable[datetime]:
    kind = schedule["type"]
    if kind == "daily":
        hour, minute = _time(schedule["time"])
        yield datetime(day.year, day.month, day.day, hour, minute)
    elif kind == "weekly":
        if day.weekday() in schedule["weekdays"]:
            hour, minute = _time(schedule["time"])
            yield datetime(day.year, day.month, day.day, hour, minute)
    elif kind == "monthly":
        last = calendar.monthrange(day.year, day.month)[1]
        if day.day == min(schedule["day"], last):
            hour, minute = _time(schedule["time"])
            yield datetime(day.year, day.month, day.day, hour, minute)
    elif kind == "hours":
        hour, minute = _time(schedule["start"])
        for slot in range(hour, 24, schedule["every"]):
            yield datetime(day.year, day.month, day.day, slot, minute)
    elif kind == "cron":
        rule = _parse_cron(schedule["expr"])
        if day.month not in rule["months"]:
            return
        dom = day.day in rule["days"]
        dow = ((day.weekday() + 1) % 7) in rule["weekdays"]  # cron: 0 = Sunday
        if rule["days_any"] and rule["weekdays_any"]:
            ok = True
        elif rule["days_any"]:
            ok = dow
        elif rule["weekdays_any"]:
            ok = dom
        else:
            ok = dom or dow  # cron: either restriction matches
        if ok:
            for hour in rule["hours"]:
                for minute in rule["minutes"]:
                    yield datetime(day.year, day.month, day.day, hour, minute)


def next_run(schedule: Dict[str, Any], after: float) -> Optional[float]:
    """The first run strictly after `after` (epoch seconds), or None when there is none."""
    schedule = validate(schedule)
    if schedule["type"] == "once":
        at = _ts(datetime.fromisoformat(schedule["at"]))
        return at if at > after else None
    start = _local(after).date()
    for offset in range(_SEARCH_DAYS):
        day = start + timedelta(days=offset)
        for moment in _times_on(day, schedule):
            stamp = _ts(moment)
            if stamp > after:
                return stamp
    return None


def runs_between(schedule: Dict[str, Any], start: float, end: float, limit: int = 50) -> List[float]:
    """Run times in (start, end], oldest first (to record what was skipped while asleep)."""
    found: List[float] = []
    cursor = start
    while len(found) < limit:
        nxt = next_run(schedule, cursor)
        if nxt is None or nxt > end:
            break
        found.append(nxt)
        cursor = nxt
    return found


def _clock(text: str) -> str:
    hour, minute = _time(text)
    return f"{hour:02d}:{minute:02d}"


def describe(schedule: Dict[str, Any]) -> str:
    """The schedule as a plain sentence fragment: 'Every Monday at 09:00'."""
    schedule = validate(schedule)
    kind = schedule["type"]
    if kind == "once":
        at = datetime.fromisoformat(schedule["at"])
        return f"Once on {at.strftime('%a %-d %b %Y')} at {at.strftime('%H:%M')}"
    if kind == "daily":
        return f"Every day at {_clock(schedule['time'])}"
    if kind == "weekly":
        days = schedule["weekdays"]
        if days == [0, 1, 2, 3, 4]:
            label = "weekday"
        elif days == [5, 6]:
            label = "weekend day"
        elif len(days) == 7:
            return f"Every day at {_clock(schedule['time'])}"
        else:
            names = [WEEKDAY_NAMES[day] for day in days]
            label = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"Every {label} at {_clock(schedule['time'])}"
    if kind == "monthly":
        day = schedule["day"]
        suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        tail = " (or the month's last day)" if day > 28 else ""
        return f"Every month on the {day}{suffix}{tail} at {_clock(schedule['time'])}"
    if kind == "hours":
        every = schedule["every"]
        return f"Every {every} hour{'s' if every != 1 else ''} from {_clock(schedule['start'])}"
    return f"Custom schedule ({schedule['expr']})"
