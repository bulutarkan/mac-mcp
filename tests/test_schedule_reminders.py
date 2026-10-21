"""#70: reminders get blocks of time in the calendar.

find_free_time found gaps and Reminders listed tasks, but nothing turned a task into time. Now each
reminder gets a block in the first free time that fits after the ones before it: no clash with any
calendar's timed events, working hours and buffers kept, its id in the block's notes, each block
read back and undoable; a replay finds the blocks it made; reminders themselves are not changed.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters
from mcp_server import app_adapters_more as more
from mcp_server import app_undo
from mcp_server.app_adapter_core import AppAdapterError
from mcp_server.app_adapters import mac_app

SETTINGS = SimpleNamespace()
NOW = datetime(2026, 10, 22, 8, 0)  # a Thursday


def reminder(rid, name):
    return {"id": rid, "name": name, "list": "Inbox", "due": "", "allday": False, "completed": False,
            "flagged": False, "priority": 0, "body": ""}


REMINDERS = {"r1": reminder("r1", "Write the report"), "r2": reminder("r2", "Call the bank"),
             "r3": reminder("r3", "Plan the trip")}


class Scheduler:
    def __init__(self, events=(), existing=None, fail=None):
        self.events = [{"start": a, "end": b, "title": "busy", "calendar": "Work", "uid": f"u{i}"}
                       for i, (a, b) in enumerate(events)]
        self.existing = existing or {}
        self.fail = fail or {}
        self.created = []

    def create(self, title, start, end, *, calendar, location, notes, timeout_s):
        if title in self.fail:
            raise self.fail[title]
        self.created.append({"title": title, "start": start, "end": end, "calendar": calendar, "notes": notes})
        return {"ok": True, "created": True, "verified": True, "verification": "read_back",
                "event": {"uid": f"new{len(self.created)}", "summary": title, "start": start, "end": end}}

    def run(self, item_id, **kwargs):
        kwargs.setdefault("hours", "09:00-18:00")
        with patch.object(more, "_busy", return_value={"events": self.events, "all_day": [], "unexpanded": []}), \
                patch.object(app_adapters, "_scheduled_blocks", return_value=self.existing), \
                patch.object(app_undo, "read_reminder", side_effect=lambda rid, _t=10.0: REMINDERS.get(rid)), \
                patch.object(app_adapters, "_calendar_create", side_effect=self.create), \
                patch.object(app_adapters, "_undo_record", return_value={"undoable": True, "undo_id": "appundo_x"}):
            return app_adapters._calendar_schedule_reminders(item_id, calendar=kwargs.pop("calendar", None),
                                                             date_from=kwargs.pop("date_from", "2026-10-22"),
                                                             date_to=kwargs.pop("date_to", "2026-10-23"),
                                                             duration_min=kwargs.pop("duration_min", None),
                                                             include_weekends=False, buffer_min=kwargs.pop("buffer_min", None),
                                                             timeout_s=10.0, now=NOW, **kwargs)


class ScheduleTests(unittest.TestCase):
    def test_blocks_fill_free_time_in_order_without_clashing(self) -> None:
        busy = [(datetime(2026, 10, 22, 9, 0), datetime(2026, 10, 22, 10, 0)),
                (datetime(2026, 10, 22, 10, 30), datetime(2026, 10, 22, 12, 0))]
        sched = Scheduler(busy)
        result = sched.run("r1=60, r2, r3=30")
        self.assertEqual([("Write the report", "2026-10-22T12:00", "2026-10-22T13:00"),
                          ("Call the bank", "2026-10-22T10:00", "2026-10-22T10:30"),
                          ("Plan the trip", "2026-10-22T13:00", "2026-10-22T13:30")],
                         [(e["title"], e["start"], e["end"]) for e in sched.created])
        self.assertEqual(3, result["counts"]["scheduled"])
        self.assertFalse(result["reminders_changed"])
        self.assertIn("mac-mcp-reminder=r1", sched.created[0]["notes"])
        self.assertTrue(all(item["undo"]["undoable"] and item["verified"] for item in result["scheduled"]))

    def test_buffers_and_working_hours_are_kept(self) -> None:
        busy = [(datetime(2026, 10, 22, 9, 0), datetime(2026, 10, 22, 17, 0))]
        sched = Scheduler(busy)
        result = sched.run("r1=45", buffer_min=15)
        # 17:00 + 15 min buffer = 17:15; 45 minutes fit before 18:00.
        self.assertEqual("2026-10-22T17:15", result["scheduled"][0]["start"])
        late = Scheduler([(datetime(2026, 10, 22, 9, 0), datetime(2026, 10, 22, 17, 30)),
                          (datetime(2026, 10, 23, 9, 0), datetime(2026, 10, 23, 18, 0))]).run("r1=45")
        self.assertEqual("no_free_time", late["unscheduled"][0]["reason"])
        self.assertTrue(late["partial"])

    def test_a_replay_finds_the_block_it_made(self) -> None:
        sched = Scheduler(existing={"r1": {"uid": "u9", "summary": "Write the report", "calendar": "Work",
                                           "start": "2026-10-22T09:00"}})
        result = sched.run("r1, r2")
        self.assertEqual(["r1"], [item["reminder_id"] for item in result["already_scheduled"]])
        self.assertEqual(["Call the bank"], [e["title"] for e in sched.created])

    def test_missing_reminders_and_failures_are_reported_apart(self) -> None:
        unclear = AppAdapterError("CALENDAR_CREATE_FAILED", "timed out", outcome_unknown=True)
        sched = Scheduler(fail={"Call the bank": unclear})
        result = sched.run("gone, r1, r2, r3")
        reasons = {item["reminder_id"]: item["reason"] for item in result["unscheduled"]}
        self.assertEqual({"gone": "reminder_not_found", "r2": "CALENDAR_CREATE_FAILED", "r3": "not_tried"}, reasons)
        self.assertEqual(["r1"], [item["reminder_id"] for item in result["scheduled"]])
        self.assertTrue(next(i for i in result["unscheduled"] if i["reminder_id"] == "r2")["outcome_unknown"])

    def test_bad_input_changes_nothing(self) -> None:
        for item_id, extra in (("", {}), ("r1=2", {}), (",".join(f"r{i}" for i in range(25)), {})):
            with self.subTest(item_id=item_id[:20]):
                with self.assertRaises(AppAdapterError):
                    Scheduler().run(item_id, **extra)

    def test_it_is_a_calendar_data_write_with_its_own_description(self) -> None:
        from mcp_server.policy import Capability, resolve_risk

        _, risk = resolve_risk("mac_app", {"app": "Calendar", "action": "schedule_reminders"})
        self.assertIn(Capability.LOCAL_WRITE, risk.capabilities)
        self.assertIn("schedule_reminders", str(mac_app(SETTINGS, app="Calendar", action="capabilities")))


if __name__ == "__main__":
    unittest.main()
