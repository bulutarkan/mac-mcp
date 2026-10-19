"""The undo trail for what mac_app changes in Notes, Reminders, Calendar and Mail (#259).

The apps are faked here: each test patches the state readers and the restore steps. The scripts
themselves run against the real apps in the owner's E2E check.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import app_undo
from mcp_server.policy import resolve_risk

NOTE_BEFORE = {"id": "x-coredata://n/1", "name": "Plan", "folder": "Notes", "attachments": 0,
               "body": "<div><h1>Plan</h1></div><div>old line</div>"}
NOTE_AFTER = {**NOTE_BEFORE, "body": "<div><h1>Plan</h1></div><div>new line</div>"}
REMINDER = {"id": "x-apple-reminder://r1", "name": "Call the bank", "list": "Inbox", "due": "2026-10-20T09:30:00",
            "allday": False, "completed": False, "flagged": False, "priority": 0, "body": "About the card"}


class UndoCase(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"MAC_MCP_APP_UNDO": "1"})
        env.start()
        self.addCleanup(env.stop)
        for path in app_undo._root().glob("*.json") if app_undo._root().is_dir() else []:
            path.unlink()

    def recorded_note_change(self, after=NOTE_AFTER):
        with patch.object(app_undo, "read_note", side_effect=[NOTE_BEFORE, after]):
            plan = app_undo.prepare("Notes", "update_note", item_id=NOTE_BEFORE["id"])
            return app_undo.record(plan, {"ok": True, "updated": True, "changed": True,
                                          "note": {"id": NOTE_BEFORE["id"], "title": "Plan"}})


class RecordTests(UndoCase):
    def test_a_change_gets_an_undo_id_and_an_owner_only_record(self) -> None:
        info = self.recorded_note_change()
        self.assertTrue(info["undoable"])
        self.assertTrue(info["undo_id"].startswith("appundo_"))
        path = app_undo._record_path(info["undo_id"])
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
        saved = json.loads(path.read_text())
        self.assertEqual(NOTE_BEFORE, saved["before"])
        self.assertEqual({"id": NOTE_BEFORE["id"], "title": "Plan"}, saved["item"])

    def test_sends_and_shortcuts_say_they_cannot_be_undone(self) -> None:
        for action in ("send_mail", "send_message", "run_shortcut"):
            plan = app_undo.prepare("Mail", action)
            info = app_undo.record(plan, {"ok": True, "sent": True, "ran": True})
            self.assertFalse(info["undoable"])
            self.assertIn("cannot", info["reason"])
        self.assertIsNone(app_undo.record(app_undo.prepare("Mail", "send_mail"), {"ok": True, "sent": False}))

    def test_nothing_changed_records_nothing(self) -> None:
        with patch.object(app_undo, "read_reminder", return_value={**REMINDER, "completed": True}):
            plan = app_undo.prepare("Reminders", "complete_reminder", item_id=REMINDER["id"])
        info = app_undo.record(plan, {"ok": True, "completed": True, "changed": False})
        self.assertFalse(info["undoable"])
        self.assertEqual([], app_undo.recent())

    def test_a_note_with_attachments_is_changed_but_not_recorded(self) -> None:
        with patch.object(app_undo, "read_note", return_value={**NOTE_BEFORE, "attachments": 2}):
            plan = app_undo.prepare("Notes", "update_note", item_id=NOTE_BEFORE["id"])
        info = app_undo.record(plan, {"ok": True, "changed": True})
        self.assertFalse(info["undoable"])
        self.assertIn("attachments", info["reason"])

    def test_an_unreadable_old_state_never_stops_the_change(self) -> None:
        with patch.object(app_undo, "read_event", side_effect=app_undo.AppAdapterError("X", "Calendar timed out")):
            plan = app_undo.prepare("Calendar", "update_event", item_id="uid-1")
        self.assertIn("could not be read", plan.reason)
        self.assertFalse(app_undo.record(plan, {"ok": True, "updated": True})["undoable"])

    def test_mail_reads_the_message_in_the_mailbox_it_is_in(self) -> None:
        message = {"message_id": "<a@b>", "account": "Work", "mailbox": "Drafts", "read": False, "flagged": False}
        with patch.object(app_undo, "read_message", return_value=message) as read:
            plan = app_undo.prepare("Mail", "update_message", item_id="<a@b>", mailbox="drafts")
        self.assertEqual("drafts mailbox", plan.context["mailbox_expr"])
        self.assertEqual("drafts mailbox", read.call_args.kwargs["mailbox_expr"])
        with patch.object(app_undo, "read_message", return_value=message):
            self.assertIn("could not be read", app_undo.prepare("Mail", "update_message", item_id="x", mailbox="outbox").reason)

    def test_switched_off_records_nothing(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_APP_UNDO": "0"}):
            self.assertIsNone(app_undo.prepare("Notes", "update_note", item_id="x"))

    def test_old_and_excess_records_are_pruned(self) -> None:
        with patch.object(app_undo, "MAX_RECORDS", 3):
            for index in range(5):
                with patch.object(app_undo, "read_reminder", side_effect=[REMINDER, {**REMINDER, "name": str(index)}]):
                    plan = app_undo.prepare("Reminders", "update_reminder", item_id=REMINDER["id"])
                    app_undo.record(plan, {"ok": True, "changed": True}, now=1000.0 + index)
        self.assertEqual(3, len(list(app_undo._root().glob("appundo_*.json"))))
        with patch.object(app_undo, "read_reminder", side_effect=[REMINDER, REMINDER]):
            plan = app_undo.prepare("Reminders", "update_reminder", item_id=REMINDER["id"])
            app_undo.record(plan, {"ok": True, "changed": True}, now=1000.0 + app_undo.RETENTION_S + 10)
        self.assertEqual(1, len(list(app_undo._root().glob("appundo_*.json"))))


class UndoTests(UndoCase):
    def test_undo_puts_the_old_state_back_and_forgets_the_copy(self) -> None:
        info = self.recorded_note_change()
        restored = []
        with patch.object(app_undo, "read_note", side_effect=[NOTE_AFTER, NOTE_BEFORE]), \
                patch.object(app_undo, "_restore_note", side_effect=lambda before, _t: restored.append(before)):
            out = app_undo.undo(info["undo_id"], app="Notes")
        self.assertEqual([NOTE_BEFORE], restored)
        self.assertTrue(out["undone"] and out["restored"] and out["verified"])
        saved = json.loads(app_undo._record_path(info["undo_id"]).read_text())
        self.assertEqual("undone", saved["state"])
        self.assertIsNone(saved["before"])  # the note's old text is not kept once it is back
        with self.assertRaises(app_undo.UndoError) as again:
            app_undo.undo(info["undo_id"], app="Notes")
        self.assertEqual("UNDO_ALREADY_DONE", again.exception.code)

    def test_a_later_edit_is_never_overwritten_without_force(self) -> None:
        info = self.recorded_note_change()
        edited = {**NOTE_AFTER, "body": NOTE_AFTER["body"] + "<div>the owner typed this</div>"}
        with patch.object(app_undo, "read_note", return_value=edited), \
                patch.object(app_undo, "_restore_note") as restore:
            with self.assertRaises(app_undo.UndoError) as conflict:
                app_undo.undo(info["undo_id"], app="Notes")
            self.assertEqual("UNDO_CONFLICT", conflict.exception.code)
            restore.assert_not_called()
        with patch.object(app_undo, "read_note", side_effect=[edited, NOTE_BEFORE]), \
                patch.object(app_undo, "_restore_note") as restore:
            out = app_undo.undo(info["undo_id"], app="Notes", force=True)
        restore.assert_called_once()
        self.assertTrue(out["restored"])

    def test_wrong_app_expired_and_unknown_ids_are_refused(self) -> None:
        info = self.recorded_note_change()
        with self.assertRaises(app_undo.UndoError) as wrong:
            app_undo.undo(info["undo_id"], app="Reminders")
        self.assertEqual("UNDO_WRONG_APP", wrong.exception.code)
        with self.assertRaises(app_undo.UndoError) as expired:
            app_undo.undo(info["undo_id"], app="Notes", now=info["expires_at"] + 1)
        self.assertEqual("UNDO_EXPIRED", expired.exception.code)
        for bad in ("appundo_missing", "../etc/passwd", "appundo_../../x"):
            with self.assertRaises(app_undo.UndoError):
                app_undo.undo(bad, app="Notes")

    def test_a_created_item_is_removed_and_a_deleted_reminder_made_again(self) -> None:
        created = {**REMINDER, "id": "x-apple-reminder://new"}
        with patch.object(app_undo, "read_reminder", return_value=created):
            plan = app_undo.prepare("Reminders", "create_reminder")
            info = app_undo.record(plan, {"ok": True, "created": True, "reminder": {"id": created["id"]}})
        with patch.object(app_undo, "read_reminder", side_effect=[created, None]), \
                patch.object(app_undo, "_run", return_value="OK") as run:
            out = app_undo.undo(info["undo_id"], app="Reminders")
        self.assertTrue(out["removed"] and out["verified"])
        self.assertIn("delete item 1 of matches", run.call_args.args[0])

        with patch.object(app_undo, "read_reminder", return_value=REMINDER):
            plan = app_undo.prepare("Reminders", "delete_reminder", item_id=REMINDER["id"])
        info = app_undo.record(plan, {"ok": True, "deleted": True, "changed": True})
        again = {**REMINDER, "id": "x-apple-reminder://again"}
        with patch.object(app_undo, "read_reminder", side_effect=[None, again]), \
                patch.object(app_undo, "_run", return_value=again["id"]) as run:
            out = app_undo.undo(info["undo_id"], app="Reminders")
        self.assertEqual(again["id"], out["new_item_id"])
        self.assertTrue(out["recreated"] and out["verified"])
        self.assertIn('set body of r to "About the card"', run.call_args.args[0])


class ScriptTests(unittest.TestCase):
    """Every script undo reads or writes with compiles, so a typo never surfaces only on the owner's Mac."""

    @unittest.skipUnless(shutil.which("osacompile"), "osacompile is macOS-only")
    def test_every_undo_script_compiles(self) -> None:
        scripts: list = []

        def capture(script, **_kwargs):
            scripts.append(script)
            return "GONE"
        event = {"uid": "u-1", "calendar": "Work", "summary": 'Title "quoted"', "start": "2026-10-31T14:30:00",
                 "end": "2026-10-31T15:00:00", "allday": False, "location": "Room", "description": "Line\nTwo"}
        message = {"message_id": "<a@b>", "account": "Work", "mailbox": "INBOX", "read": True, "flagged": False}
        with patch.object(app_undo, "_run", side_effect=capture):
            app_undo.read_note("n1"); app_undo.read_reminder("r1"); app_undo.read_event("u-1")
            app_undo.read_message("<a@b>", mailbox_expr="drafts mailbox")
            app_undo.read_message("<a@b>", account="Work", mailbox_name="Archive")
            app_undo._note_by_name("Plan", True, 5); app_undo._note_by_name("Plan", False, 5)
            for call in (lambda: app_undo._restore_note(NOTE_BEFORE, 5),
                         lambda: app_undo._restore_reminder(REMINDER, 5),
                         lambda: app_undo._restore_reminder({**REMINDER, "due": "", "allday": False}, 5),
                         lambda: app_undo._restore_reminder({**REMINDER, "due": "2026-10-21T00:00:00", "allday": True}, 5),
                         lambda: app_undo._restore_event(event, 5),
                         lambda: app_undo._restore_message({"before": message, "after": {**message, "mailbox": "Archive"}}, 5)):
                with self.assertRaises(app_undo.UndoError):
                    call()
            app_undo._recreate_reminder(REMINDER, 5)
            for app, after in (("Notes", {"id": "n1"}), ("Reminders", {"id": "r1"}), ("Calendar", {"uid": "u-1"}),
                               ("Mail", {"message_id": "<a@b>"})):
                with self.assertRaises(app_undo.UndoError):
                    app_undo._delete_created({"app": app, "after": after}, 5)
        self.assertEqual(18, len(scripts))
        with tempfile.TemporaryDirectory() as td:
            for index, script in enumerate(scripts):
                source = Path(td) / f"s{index}.applescript"
                source.write_text(script, encoding="utf-8")
                result = subprocess.run(["osacompile", "-o", str(Path(td) / f"s{index}.scpt"), str(source)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, f"script {index} failed to compile: {result.stderr}\n{script}")


class PolicyTests(unittest.TestCase):
    def test_undo_is_a_local_write_and_destructive_only_when_forced(self) -> None:
        _, plain = resolve_risk("mac_app", {"app": "Notes", "action": "undo", "item_id": "appundo_1"})
        _, forced = resolve_risk("mac_app", {"app": "Notes", "action": "undo", "item_id": "appundo_1", "force": True})
        self.assertFalse(plain.destructive)
        self.assertTrue(forced.destructive)


if __name__ == "__main__":
    unittest.main()
