"""Group 4: create_reminder, append_to_note, find_free_time, find_contacts, list/run shortcuts."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import subprocess
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from mcp_server import app_adapters_more as more
from mcp_server.app_adapters import mac_app
from mcp_server.policy import Capability, resolve_risk
from mcp_server.security import load_settings

_RS, _US = chr(30), chr(31)


def act(**kwargs):
    # Running commands on, whatever the environment says: these tests are about the adapters,
    # and the shell switch has a test of its own below.
    import dataclasses
    return mac_app(dataclasses.replace(load_settings(), allow_shell=True), **kwargs)


class ReminderTests(unittest.TestCase):
    def test_a_reminder_is_made_with_its_due_date_and_read_back(self) -> None:
        seen = {}

        def run(script, **kwargs):
            seen["script"] = script
            return f"OK{_US}x-apple-reminder://1{_US}Call the bank{_US}Inbox{_US}Monday 19 October 2026 09:30:00{_US}#"
        with patch("mcp_server.app_adapters_more._run", side_effect=run):
            result = act(app="reminders", action="create_reminder", title="Call the bank", start="2026-10-19T09:30",
                         list_name="Inbox", notes="About the card")
        self.assertTrue(result["ok"] and result["created"] and result["verified"])
        self.assertEqual({"id": "x-apple-reminder://1", "name": "Call the bank", "list": "Inbox",
                          "due": "Monday 19 October 2026 09:30:00"}, result["reminder"])
        self.assertIn('make new reminder at end of l with properties {name:"Call the bank", body:"About the card"}',
                      seen["script"])
        self.assertIn("set due date of r to dueDate", seen["script"])
        with patch("mcp_server.app_adapters_more._run", side_effect=run):
            act(app="reminders", action="create_reminder", title="Pay rent", start="2026-11-01")
        self.assertIn("set allday due date of r to dueDate", seen["script"])
        self.assertIn("set chosen to {default list}", seen["script"])

    def test_a_replay_finds_the_open_reminder_instead_of_a_twin(self) -> None:
        with patch("mcp_server.app_adapters_more._run",
                   return_value=f"DUPLICATE{_US}id-1{_US}Call the bank{_US}Inbox{_US}{_US}#"):
            result = act(app="reminders", action="create_reminder", title="Call the bank")
        self.assertEqual((True, False, True), (result["ok"], result["created"], result["duplicate"]))
        self.assertEqual("", result["reminder"]["due"])  # no due date: an empty field, not a missing one
        with patch("mcp_server.app_adapters_more._run", return_value="NO_LIST"):
            missing = act(app="reminders", action="create_reminder", title="x", list_name="Nope")
        self.assertEqual("REMINDERS_LIST_NOT_FOUND", missing["reason_code"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         act(app="reminders", action="create_reminder", title="")["reason_code"])


class NotesAppendTests(unittest.TestCase):
    def test_text_is_added_at_the_end_and_read_back(self) -> None:
        seen = {}

        def run(script, **kwargs):
            seen["script"] = script
            return f"OK{_US}note-1{_US}Groceries"
        with patch("mcp_server.app_adapters_more._run", side_effect=run):
            result = act(app="notes", action="append_to_note", item_id="note-1", body="milk\n<eggs> & bread")
        self.assertTrue(result["appended"] and result["verified"])
        self.assertIn("&lt;eggs&gt; &amp; bread", seen["script"])  # the text is escaped into the note's HTML
        self.assertIn('does not contain "<eggs> & bread"', seen["script"])  # read back by its last line
        self.assertNotIn("length of", seen["script"])  # Notes reads "length" as its own term
        with patch("mcp_server.app_adapters_more._run", return_value=f"COUNT{_US}2"):
            many = act(app="notes", action="append_to_note", query="Grocer", body="milk")
        self.assertEqual("NOTES_NOTE_NOT_UNIQUE", many["reason_code"])


class FreeTimeTests(unittest.TestCase):
    def rows(self, base: datetime, *events) -> str:
        out = []
        for uid, start, minutes, all_day, rule in events:
            out.append(_US.join([uid, uid, "Work", str(int((start - base).total_seconds())), str(minutes * 60),
                                 "true" if all_day else "false", rule, "#"]))
        return _RS.join(out)

    def test_free_slots_skip_meetings_weekends_and_all_day_events(self) -> None:
        now = datetime(2026, 10, 16, 8, 0)  # a Friday
        raw = self.rows(now,
                        ("standup", datetime(2026, 10, 12, 10, 0), 30, False, "FREQ=WEEKLY;BYDAY=MO,WE,FR"),
                        ("lunch", datetime(2026, 10, 16, 12, 0), 60, False, ""),
                        ("holiday", datetime(2026, 10, 19, 0, 0), 1440, True, ""))
        with patch("mcp_server.app_adapters_more._run", return_value=raw):
            result = more.calendar_free_time(date_from=None, date_to="2026-10-19", calendar=None, duration_min=60,
                                             hours="09:00-13:00", include_weekends=False, limit=10, timeout_s=5,
                                             now=now)
        # Friday: standup 10:00-10:30 (repeating from Monday), lunch 12:00; the weekend is skipped;
        # Monday: the standup again, and the holiday does not fill the day.
        self.assertEqual([("2026-10-16T09:00", "2026-10-16T10:00"), ("2026-10-16T10:30", "2026-10-16T12:00"),
                          ("2026-10-19T09:00", "2026-10-19T10:00"), ("2026-10-19T10:30", "2026-10-19T13:00")],
                         [(slot["start"], slot["end"]) for slot in result["free"]])
        self.assertEqual(["holiday"], result["all_day_events_ignored"])
        self.assertNotIn("approximate", result)

    def test_a_row_whose_last_field_is_empty_survives_the_stripped_output(self) -> None:
        # The unit separator counts as whitespace in Python, so a stripped output lost an empty last field.
        now = datetime(2026, 10, 16, 8, 0)
        raw = self.rows(now, ("lunch", datetime(2026, 10, 16, 12, 0), 60, False, "")).strip()
        with patch("mcp_server.app_adapters_more._run", return_value=raw):
            result = more.calendar_free_time(date_from=None, date_to="2026-10-16", calendar=None, duration_min=30,
                                             hours="09:00-13:00", include_weekends=False, limit=5, timeout_s=5, now=now)
        self.assertEqual(1, result["busy_events"])
        with patch("mcp_server.app_adapters_more._run",
                   return_value=_US.join(["p1", "Acme Ltd", "", "", "", "#"]).strip()):
            contact = act(app="contacts", action="find_contacts", query="Acme")
        self.assertEqual(([], []), (contact["contacts"][0]["emails"], contact["contacts"][0]["phones"]))

    def test_a_rule_it_cannot_expand_makes_the_answer_approximate(self) -> None:
        now = datetime(2026, 10, 16, 8, 0)
        raw = self.rows(now, ("board", datetime(2026, 1, 5, 9, 0), 60, False, "FREQ=MONTHLY;BYDAY=1MO"))
        with patch("mcp_server.app_adapters_more._run", return_value=raw):
            result = more.calendar_free_time(date_from=None, date_to=None, calendar=None, duration_min=30, hours=None,
                                             include_weekends=False, limit=5, timeout_s=5, now=now)
        self.assertTrue(result["approximate"])
        self.assertEqual(["board"], result["unexpanded_repeating_events"])

    def test_recurrence_rules(self) -> None:
        start = datetime(2026, 10, 1, 9, 0)
        hour = timedelta(hours=1)
        window = (datetime(2026, 10, 1), datetime(2026, 10, 31, 23, 59))
        daily = more.expand_recurrence(start, hour, "FREQ=DAILY;INTERVAL=2;COUNT=5", *window)
        self.assertEqual([1, 3, 5, 7, 9], [moment.day for moment, _ in daily])
        weekly = more.expand_recurrence(start, hour, "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH", *window)
        self.assertEqual([1, 13, 15, 27, 29], [moment.day for moment, _ in weekly])  # 1 Oct 2026 is a Thursday
        monthly = more.expand_recurrence(start, hour, "FREQ=MONTHLY;UNTIL=20261231T000000Z", datetime(2026, 11, 1),
                                         datetime(2027, 2, 1))
        self.assertEqual([(2026, 11, 1), (2026, 12, 1)], [(m.year, m.month, m.day) for m, _ in monthly])
        self.assertIsNone(more.expand_recurrence(start, hour, "FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR", *window))
        self.assertIsNone(more.expand_recurrence(start, hour, "FREQ=HOURLY", *window))

    def test_bad_arguments(self) -> None:
        for kwargs in ({"hours": "18:00-09:00"}, {"hours": "9-5"}, {"duration_min": 2},
                       {"date_to": "2027-03-01"}):
            with self.subTest(kwargs=kwargs), patch("mcp_server.app_adapters_more._run", return_value=""):
                result = act(app="calendar", action="find_free_time", **kwargs)
                self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])


class ContactsTests(unittest.TestCase):
    def test_contacts_come_back_with_their_emails_and_phones(self) -> None:
        raw = _US.join(["p1", "Ayşe Yılmaz", "Acme", "ayse@acme.com, ayse@home.tr", "+90 555 000 00 00", "#"])
        with patch("mcp_server.app_adapters_more._run", return_value=raw):
            result = act(app="contacts", action="find_contacts", query="Ayşe")
        self.assertEqual({"id": "p1", "name": "Ayşe Yılmaz", "organization": "Acme",
                          "emails": ["ayse@acme.com", "ayse@home.tr"], "phones": ["+90 555 000 00 00"]},
                         result["contacts"][0])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", act(app="contacts", action="find_contacts")["reason_code"])


class ShortcutsTests(unittest.TestCase):
    LISTED = ("Bed Time (11111111-2222-3333-4444-555555555555)\n"
              "Quick AI (AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE)\n"
              "Quick AI (FFFFFFFF-BBBB-CCCC-DDDD-EEEEEEEEEEEE)\n")

    def fake(self, calls, *, run_code=0, output="Done: 3 items"):
        def run(args, **kwargs):
            calls.append(args)
            if args[1] == "list":
                return subprocess.CompletedProcess(args, 0, self.LISTED, "")
            if "--output-path" in args:
                with open(args[args.index("--output-path") + 1], "w") as handle:
                    handle.write(output)
            if "--input-path" in args:
                with open(args[args.index("--input-path") + 1]) as handle:
                    calls.append(["input", handle.read()])
            return subprocess.CompletedProcess(args, run_code, "", "" if run_code == 0 else "Something went wrong")
        return run

    def test_list_and_run_by_exact_name_or_id_with_text_in_and_out(self) -> None:
        calls = []
        with patch("mcp_server.app_adapters_more.subprocess.run", side_effect=self.fake(calls)), \
                patch("mcp_server.app_adapters_more.os.path.exists", side_effect=lambda p: True):
            listed = act(app="shortcuts", action="list_shortcuts", query="bed")
            ran = act(app="shortcuts", action="run_shortcut", title="Bed Time", body="tonight")
        self.assertEqual([{"id": "11111111-2222-3333-4444-555555555555", "name": "Bed Time"}], listed["shortcuts"])
        self.assertEqual((True, "Done: 3 items"), (ran["ran"], ran["output"]))
        self.assertIn(["input", "tonight"], calls)
        run_args = next(args for args in calls if len(args) > 1 and args[1] == "run")
        self.assertEqual("11111111-2222-3333-4444-555555555555", run_args[2])

    def test_an_ambiguous_or_missing_name_runs_nothing(self) -> None:
        calls = []
        with patch("mcp_server.app_adapters_more.subprocess.run", side_effect=self.fake(calls)):
            twin = act(app="shortcuts", action="run_shortcut", title="Quick AI")
            missing = act(app="shortcuts", action="run_shortcut", title="Nope")
        self.assertEqual(("SHORTCUT_NOT_UNIQUE", "SHORTCUT_NOT_FOUND"), (twin["reason_code"], missing["reason_code"]))
        self.assertFalse(any(len(args) > 1 and args[1] == "run" for args in calls))

    def test_a_failed_run_is_outcome_unknown_and_shell_off_refuses(self) -> None:
        calls = []
        with patch("mcp_server.app_adapters_more.subprocess.run", side_effect=self.fake(calls, run_code=1)):
            failed = act(app="shortcuts", action="run_shortcut", item_id="11111111-2222-3333-4444-555555555555")
        self.assertEqual(("SHORTCUT_FAILED", True), (failed["reason_code"], failed["outcome_unknown"]))
        import dataclasses
        settings = dataclasses.replace(load_settings(), allow_shell=False)
        before = len(calls)
        refused = mac_app(settings, app="shortcuts", action="run_shortcut", title="Bed Time")
        self.assertEqual("SHELL_DISABLED", refused["reason_code"])
        self.assertEqual(before, len(calls))  # nothing was listed or run

    def test_policy_treats_running_a_shortcut_like_a_command(self) -> None:
        _, run = resolve_risk("mac_app", {"app": "shortcuts", "action": "run_shortcut"})
        _, listing = resolve_risk("mac_app", {"app": "shortcuts", "action": "list_shortcuts"})
        _, reminder = resolve_risk("mac_app", {"app": "reminders", "action": "create_reminder"})
        self.assertIn(Capability.RAW_EXECUTION, run.capabilities)
        self.assertNotIn(Capability.RAW_EXECUTION, listing.capabilities)
        self.assertIn(Capability.LOCAL_WRITE, reminder.capabilities)


if __name__ == "__main__":
    unittest.main()
