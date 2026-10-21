"""#68: today at a glance.

plan_day showed a day's events, free time and reminders due, but mail and messages needed separate calls.
daily_briefing puts the chosen sections into one short answer. A section excluded in Privacy is never read
(plan_day now leaves Reminders unread when asked), one that fails is reported while the rest return, no mail
or message text is included, and notify=true shows the summary as a notification.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import app_adapters_more as more
from mcp_server import daily_briefing as db
from mcp_server import messages_reader
from mcp_server.policy import RISK_REGISTRY, resolve_risk
from mcp_server.privacy_policy import PAUSED_TOOLS

DAY = datetime(2026, 10, 21, 8, 0)
SECTIONS = {
    "calendar": lambda day, limit: {"events": [{"title": "Standup", "start": "2026-10-21T09:00"}], "all_day": [],
                                    "free": [], "hours": "09:00-18:00"},
    "reminders": lambda day, limit: {"due": [{"name": "Pay rent", "overdue": True}], "overdue": 1, "total": 1,
                                     "truncated": False},
    "mail": lambda day, limit: {"received": 12, "unread": 3, "messages": [{"subject": "Invoice", "unread": True}],
                                "truncated": True, "mailbox": "inbox"},
    "messages": lambda day, limit: {"conversations": [{"name": "Ali", "unread": 2}], "unread": 2, "truncated": False},
}


class DailyBriefingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.excluded: set = set()
        check = patch.object(db.privacy_policy, "check_app", side_effect=self.privacy)
        check.start()
        self.addCleanup(check.stop)
        sections = patch.dict(db._SECTIONS, SECTIONS)
        sections.start()
        self.addCleanup(sections.stop)

    def privacy(self, name, bundle):
        if name in self.excluded:
            raise db.privacy_policy.PrivacyBlocked("privacy_excluded_app", f"excluded {name}")

    def test_one_call_brings_every_section_and_a_one_line_summary(self) -> None:
        out = db.daily_briefing(None, now=DAY)
        self.assertEqual("2026-10-21", out["date"])
        self.assertEqual(["Standup"], [event["title"] for event in out["calendar"]["events"]])
        self.assertEqual("Wednesday 21 October: 1 event, 1 reminder due (1 overdue), 3 unread of 12 new mail, "
                         "2 unread messages", out["summary"])
        self.assertFalse(out["partial"])
        self.assertNotIn("notified", out)

    def test_an_excluded_or_failing_section_leaves_the_rest(self) -> None:
        self.excluded = {"Mail"}
        called = []
        db._SECTIONS["mail"] = lambda day, limit: called.append("mail") or {}
        db._SECTIONS["calendar"] = lambda day, limit: (_ for _ in ()).throw(RuntimeError("Calendar timed out"))
        out = db.daily_briefing(None, now=DAY)
        self.assertEqual([], called)  # an excluded source is never read
        self.assertEqual("excluded_in_privacy", out["mail"]["reason"])
        self.assertEqual(("runtimeerror", False), (out["calendar"]["reason"], out["calendar"]["ok"]))
        self.assertTrue(out["reminders"]["ok"] and out["messages"]["ok"])
        self.assertTrue(out["partial"])
        self.assertEqual("Wednesday 21 October: 1 reminder due (1 overdue), 2 unread messages", out["summary"])

    def test_sources_date_and_notify(self) -> None:
        out = db.daily_briefing(None, sources=["mail"], date="2026-10-20", now=DAY)
        self.assertEqual(["mail"], [key for key in db.SOURCES if key in out])
        self.assertEqual("2026-10-20", out["date"])
        with patch("mcp_server.tools_macos.send_notification", return_value={"ok": True}) as shown:
            notified = db.daily_briefing(None, sources=["reminders"], notify=True, now=DAY)
        self.assertTrue(notified["notified"])
        self.assertEqual(("Today", notified["summary"]), (shown.call_args.kwargs["title"], shown.call_args.kwargs["message"]))
        for bad in ({"sources": ["photos"]}, {"date": "tomorrow"}):
            with self.subTest(bad=bad), self.assertRaises(HTTPException):
                db.daily_briefing(None, now=DAY, **bad)

    def test_policy_reads_and_a_notification_is_a_ui_action(self) -> None:
        self.assertIn("daily_briefing", RISK_REGISTRY)
        self.assertNotIn("ui_action", {str(getattr(c, "value", c)) for c in resolve_risk("daily_briefing", {})[1].capabilities})
        self.assertIn("ui_action", {str(getattr(c, "value", c)) for c in resolve_risk("daily_briefing", {"notify": True})[1].capabilities})
        self.assertIn("daily_briefing", PAUSED_TOOLS)


class SectionReaderTests(unittest.TestCase):
    def test_mail_section_reads_metadata_only_and_compiles(self) -> None:
        script = db._mail_script(datetime(2026, 10, 21), datetime(2026, 10, 21, 23, 59, 59), 5)
        self.assertNotIn("content of", script)  # no message text
        with tempfile.NamedTemporaryFile(suffix=".scpt") as out:
            compiled = subprocess.run(["/usr/bin/osacompile", "-o", out.name, "-e", script], capture_output=True, text=True)
        self.assertEqual(0, compiled.returncode, compiled.stderr)
        raw = "12\x1f3\x1em1\x1fInvoice\x1fa@b.com\x1fWednesday\x1ffalse\x1em2\x1fHi\x1fc@d.com\x1fWednesday\x1ftrue"
        with patch("mcp_server.app_adapter_core._run", return_value=raw):
            mail = db._mail(datetime(2026, 10, 21), 5)
        self.assertEqual((12, 3, True), (mail["received"], mail["unread"], mail["truncated"]))
        self.assertEqual([True, False], [row["unread"] for row in mail["messages"]])

    def test_messages_section_counts_without_text(self) -> None:
        with patch.object(messages_reader, "chats", return_value={7: {"chat_id": "c7", "name": "Ali"}}), \
                patch.object(messages_reader, "_query", return_value=[
                    {"chat_rowid": 7, "last_date": 0, "received": 4, "unread": 2},
                    {"chat_rowid": 8, "last_date": 0, "received": 3, "unread": 3}]) as query:
            out = db._messages(datetime(2026, 10, 21), 1)
        self.assertNotIn("mm_text", query.call_args.args[0])  # the query reads no message text
        self.assertEqual([{"chat_id": "c7", "name": "Ali", "last_message": None, "received": 4, "unread": 2}],
                         out["conversations"])
        self.assertEqual((5, 2, True), (out["unread"], out["with_new_messages"], out["truncated"]))  # all, not just listed

    def test_plan_day_can_leave_reminders_unread(self) -> None:
        with patch.object(more, "_busy", return_value={"events": [], "all_day": [], "unexpanded": []}), \
                patch.object(more, "_run", side_effect=AssertionError("Reminders must not be read")):
            plan = more.plan_day(date_from="2026-10-21", calendar=None, duration_min=None, hours=None,
                                 buffer_min=None, limit=5, timeout_s=5, now=DAY, reminders=False)
        self.assertNotIn("reminders_due", plan)


if __name__ == "__main__":
    unittest.main()
