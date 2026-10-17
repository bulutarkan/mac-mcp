"""#194-#196 remainders: reminder update/delete, note update, free-time buffer, plan_day, mail update,
draft attachments, recipients from Contacts, contacts by email or phone, and owner-allowed shortcuts."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mcp_server import app_adapters_more as more
from mcp_server.app_adapters import mac_app
from mcp_server.policy import Capability, resolve_risk
from mcp_server.security import load_settings
from tests import test_app_adapters_more as base  # its classes by module, so they aren't collected twice

_RS, _US = chr(30), chr(31)


def act(allow_shell: bool = True, **kwargs):
    return mac_app(dataclasses.replace(load_settings(), allow_shell=allow_shell), **kwargs)


class ReminderLifecycleTests(unittest.TestCase):
    def test_update_defers_and_renames_with_a_read_back(self) -> None:
        seen = {}

        def run(script, **kwargs):
            seen["script"] = script
            return f"OK{_US}rid-1{_US}Call the bank{_US}Inbox{_US}Tuesday 20 October 2026 10:00:00{_US}#"
        with patch("mcp_server.app_adapters_more._run", side_effect=run):
            result = act(app="reminders", action="update_reminder", item_id="rid-1", title="Call the bank",
                         start="2026-10-20T10:00")
        self.assertTrue(result["ok"] and result["updated"] and result["verified"])
        self.assertEqual("Tuesday 20 October 2026 10:00:00", result["reminder"]["due"])
        self.assertIn('set name of r to "Call the bank"', seen["script"])
        self.assertIn("set due date of r to dueDate", seen["script"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", act(app="reminders", action="update_reminder", item_id="rid-1")["reason_code"])
        with patch("mcp_server.app_adapters_more._run", return_value=f"COUNT{_US}0"):
            missing = act(app="reminders", action="update_reminder", item_id="nope", title="x")
        self.assertEqual("REMINDER_NOT_FOUND", missing["reason_code"])

    def test_delete_reads_back_and_a_replay_changes_nothing(self) -> None:
        with patch("mcp_server.app_adapters_more._run", return_value=f"OK{_US}Old task"):
            deleted = act(app="reminders", action="delete_reminder", item_id="rid-1")
        self.assertEqual((True, True), (deleted["deleted"], deleted["verified"]))
        with patch("mcp_server.app_adapters_more._run", return_value="GONE"):
            again = act(app="reminders", action="delete_reminder", item_id="rid-1")
        self.assertEqual((True, False), (again["ok"], again["deleted"]))

    def test_policy_marks_delete_and_note_replacement_as_destructive_local_writes(self) -> None:
        for action, destructive in (("update_reminder", False), ("delete_reminder", True), ("update_note", True),
                                    ("update_message", False)):
            _, risk = resolve_risk("mac_app", {"app": "x", "action": action})
            self.assertIn(Capability.LOCAL_WRITE, risk.capabilities, action)
            self.assertEqual(destructive, risk.destructive, action)
        _, plan = resolve_risk("mac_app", {"app": "calendar", "action": "plan_day"})
        self.assertNotIn(Capability.LOCAL_WRITE, plan.capabilities)


class NoteUpdateTests(unittest.TestCase):
    def test_only_by_id_and_the_title_stays_first(self) -> None:
        scripts = []

        def run(script, **kwargs):
            scripts.append(script)
            return "Groceries" if len(scripts) == 1 else f"OK{_US}nid-1{_US}Groceries"
        with patch("mcp_server.app_adapters_more._run", side_effect=run):
            result = act(app="notes", action="update_note", item_id="nid-1", body="milk\neggs")
        self.assertTrue(result["ok"] and result["updated"])
        self.assertIn("<h1>Groceries</h1>", scripts[1])  # the current title is kept as the first line
        self.assertIn('contains "eggs"', scripts[1])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         act(app="notes", action="update_note", query="Groceries", body="x")["reason_code"])


class PlanningTests(unittest.TestCase):
    def test_a_buffer_keeps_room_around_events(self) -> None:
        now = datetime(2026, 10, 16, 8, 0)
        raw = base.FreeTimeTests.rows(None, now, ("lunch", datetime(2026, 10, 16, 12, 0), 60, False, ""))
        with patch("mcp_server.app_adapters_more._run", return_value=raw):
            result = more.calendar_free_time(date_from=None, date_to="2026-10-16", calendar=None, duration_min=30,
                                             hours="11:00-14:00", include_weekends=True, limit=10, timeout_s=5,
                                             now=now, buffer_min=15)
        self.assertEqual([("2026-10-16T11:00", "2026-10-16T11:45"), ("2026-10-16T13:15", "2026-10-16T14:00")],
                         [(slot["start"], slot["end"]) for slot in result["free"]])
        self.assertEqual(15, result["buffer_min"])

    def test_plan_day_shows_events_free_time_and_reminders_due(self) -> None:
        now = datetime(2026, 10, 16, 8, 0)
        day = datetime(2026, 10, 16)
        events = base.FreeTimeTests.rows(None, day, ("review", datetime(2026, 10, 16, 10, 0), 60, False, ""))
        due = _RS.join([_US.join(["r1", "Pay rent", "Home", str(-86400), "#"]),
                        _US.join(["r2", "Send report", "Work", str(15 * 3600), "#"])])
        with patch("mcp_server.app_adapters_more._run", side_effect=[events, due]):
            plan = more.plan_day(date_from="2026-10-16", calendar=None, duration_min=60, hours="09:00-12:00",
                                 buffer_min=None, limit=10, timeout_s=5, now=now)
        self.assertEqual(["review"], [event["title"] for event in plan["events"]])
        self.assertEqual([("2026-10-16T09:00", "2026-10-16T10:00"), ("2026-10-16T11:00", "2026-10-16T12:00")],
                         [(slot["start"], slot["end"]) for slot in plan["free"]])
        self.assertEqual([("Pay rent", True), ("Send report", False)],
                         [(item["name"], item["overdue"]) for item in plan["reminders_due"]])


class MailTests(unittest.TestCase):
    def test_mark_read_flag_and_archive_with_a_read_back(self) -> None:
        seen = {}

        def run(script, **kwargs):
            seen["script"] = script
            return f"OK{_US}true{_US}true{_US}Archive"
        with patch("mcp_server.app_adapters._run", side_effect=run):
            result = act(app="mail", action="update_message", item_id="<m1@x>", read=True, flagged=True,
                         move_to="archive")
        self.assertEqual((True, True, True, "Archive"), (result["ok"], result["read"], result["flagged"], result["moved_to"]))
        self.assertIn("set read status of m to true", seen["script"])
        self.assertIn('set destination to mailbox "Archive" of acct', seen["script"])
        with patch("mcp_server.app_adapters._run", return_value=f"NO_MAILBOX{_US}INBOX{_US}Sent"):
            missing = act(app="mail", action="update_message", item_id="<m1@x>", move_to="Receipts")
        self.assertEqual(("MAIL_MAILBOX_NOT_FOUND", ["INBOX", "Sent"]), (missing["reason_code"], missing["mailboxes"]))
        with patch("mcp_server.app_adapters._run", return_value=f"OK{_US}false{_US}false{_US}"):
            unconfirmed = act(app="mail", action="update_message", item_id="<m1@x>", read=True)
        self.assertEqual("MAIL_UPDATE_NOT_VERIFIED", unconfirmed["reason_code"])

    def contacts(self, *people):
        return {"ok": True, "contacts": [{"id": str(i), "name": name, "organization": "", "emails": emails, "phones": []}
                                         for i, (name, emails) in enumerate(people)]}

    def test_names_become_addresses_only_when_they_lead_to_one(self) -> None:
        scripts = []

        def run(script, **kwargs):
            scripts.append(script)
            return f"OK{_US}<d@x>{_US}Hi{_US}Work{_US}Me <me@x.com>{_US}0"
        with patch("mcp_server.app_adapters_more.contacts_find", return_value=self.contacts(("Ayşe Kaya", ["ayse@x.com"]))), \
                patch("mcp_server.app_adapters._run", side_effect=run):
            draft = act(app="mail", action="create_draft", title="Hi", to="Ayşe Kaya, bob@x.com", account="Work")
        self.assertEqual(["ayse@x.com", "bob@x.com"], draft["to"])
        self.assertEqual({"Ayşe Kaya": "ayse@x.com"}, draft["resolved_from_contacts"])
        with patch("mcp_server.app_adapters_more.contacts_find",
                   return_value=self.contacts(("Ali", ["a@x.com", "ali@home.com"]))):
            ambiguous = act(app="mail", action="create_draft", title="Hi", to="Ali", account="Work")
        self.assertEqual("MAIL_RECIPIENT_AMBIGUOUS", ambiguous["reason_code"])
        with patch("mcp_server.app_adapters_more.contacts_find", return_value=self.contacts()):
            nobody = act(app="mail", action="create_draft", title="Hi", to="Nobody", account="Work")
        self.assertEqual("MAIL_RECIPIENT_NOT_FOUND", nobody["reason_code"])

    def test_a_draft_carries_attachments_and_says_how_many_mail_shows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.pdf"
            report.write_bytes(b"%PDF-1.4")
            scripts = []

            def run(script, **kwargs):
                scripts.append(script)
                return f"OK{_US}<d@x>{_US}Report{_US}Work{_US}Me <me@x.com>{_US}1"
            with patch("mcp_server.app_adapters._run", side_effect=run):
                draft = act(app="mail", action="create_draft", title="Report", to="a@x.com", account="Work",
                            attachments=[str(report)])
            self.assertEqual((["report.pdf"], True), (draft["attachments"], draft["attachments_verified"]))
            self.assertIn(f'make new attachment with properties {{file name:(POSIX file "{report}")}}', scripts[0])
            missing = act(app="mail", action="create_draft", title="Report", to="a@x.com", account="Work",
                          attachments=[str(Path(folder) / "nope.pdf")])
            self.assertEqual("MAIL_ATTACHMENT_NOT_FOUND", missing["reason_code"])

    def test_attachments_are_checked_against_a_scoped_agents_paths(self) -> None:
        from mcp_server.policy import evaluate_tool_scope
        from mcp_server.policy_scope import ResourceScope
        scope = ResourceScope(path_roots=("/tmp/allowed",))
        decision = evaluate_tool_scope(scope, "mac_app", {"app": "mail", "action": "create_draft",
                                                          "attachments": ["/Users/someone/secret.txt"]})
        self.assertFalse(decision.allowed)


class ContactsByAddressTests(unittest.TestCase):
    RAW = _RS.join([_US.join(["1", "Ayşe Kaya", "", "ayse@x.com, ayse@home.com", "+90 532 111 22 33", "#"]),
                    _US.join(["2", "Bob", "", "bob@x.com", "", "#"])])

    def test_an_email_or_phone_finds_the_person(self) -> None:
        with patch("mcp_server.app_adapters_more._run", return_value=self.RAW):
            by_mail = act(app="contacts", action="find_contacts", query="ayse@home.com")
            by_phone = act(app="contacts", action="find_contacts", query="0532 111 2233")
        self.assertEqual((["Ayşe Kaya"], "email"), ([c["name"] for c in by_mail["contacts"]], by_mail["matched_on"]))
        self.assertEqual((["Ayşe Kaya"], "phone"), ([c["name"] for c in by_phone["contacts"]], by_phone["matched_on"]))


class AllowedShortcutsTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.mkdtemp(prefix="mac-mcp-shortcut-settings-")
        self.settings_file = Path(folder) / "settings.json"
        env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(self.settings_file)})
        env.start()
        self.addCleanup(env.stop)

    def allow(self, *names):
        self.settings_file.write_text(json.dumps({"apps": {"shortcuts": {"allowed": list(names)}}}))

    def test_with_commands_off_only_the_owners_shortcuts_run(self) -> None:
        self.allow("Bed Time")
        calls = []
        fake = base.ShortcutsTests.fake(base.ShortcutsTests(), calls)
        with patch("mcp_server.app_adapters_more.subprocess.run", side_effect=fake), \
                patch("mcp_server.app_adapters_more.os.path.exists", side_effect=lambda p: True):
            allowed = act(allow_shell=False, app="shortcuts", action="run_shortcut", title="Bed Time")
            other = act(allow_shell=False, app="shortcuts", action="run_shortcut",
                        item_id="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        self.assertTrue(allowed["ran"])
        self.assertEqual("SHELL_DISABLED", other["reason_code"])
        self.assertEqual(1, sum(1 for args in calls if len(args) > 1 and args[1] == "run"))

    def test_an_empty_list_keeps_todays_behaviour(self) -> None:
        self.allow()
        refused = act(allow_shell=False, app="shortcuts", action="run_shortcut", title="Bed Time")
        self.assertEqual("SHELL_DISABLED", refused["reason_code"])


if __name__ == "__main__":
    unittest.main()
