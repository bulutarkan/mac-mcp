from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters as aa
from mcp_server.policy import Capability, resolve_risk

US = chr(31)


def _capture_scripts(call, *, reply: str = "OK" + US + "uid-1" + US + "T" + US + "C" + US + "s" + US + "e") -> list[str]:
    scripts: list[str] = []

    def fake_run(script, **_kwargs):
        scripts.append(script)
        return reply

    with patch.object(aa, "_run", side_effect=fake_run):
        call()
    return scripts


class AppDataActionTests(unittest.TestCase):
    def test_registry_and_risk_treat_data_changes_as_local_writes(self) -> None:
        self.assertEqual(("find_events", "open_event", "create_event", "update_event"), aa.supported_actions("calendar"))
        self.assertEqual(("list_reminders", "complete_reminder"), aa.supported_actions("reminders"))
        self.assertTrue(aa.is_read_action("list_reminders"))
        _, write = resolve_risk("mac_app", {"app": "Calendar", "action": "create_event"})
        self.assertIn(Capability.LOCAL_WRITE, write.capabilities)
        self.assertNotIn(Capability.UI_ACTION, write.capabilities)
        _, read = resolve_risk("mac_app", {"app": "Reminders", "action": "list_reminders"})
        self.assertNotIn(Capability.LOCAL_WRITE, read.capabilities)

    @unittest.skipUnless(shutil.which("osacompile"), "osacompile is macOS-only")
    def test_every_generated_script_compiles(self) -> None:
        scripts: list[str] = []
        scripts += _capture_scripts(lambda: aa._calendar_create(
            "Title \"quoted\"", "2026-10-31T14:30", "2026-10-31T15:00", calendar="Work",
            location="Place", notes="Line", timeout_s=5))
        scripts += _capture_scripts(lambda: aa._calendar_create(
            "All day", "2026-11-03", None, calendar=None, location=None, notes=None, timeout_s=5))
        for kwargs in (
            dict(title="New", start=None, end=None, location=None, notes=None),
            dict(title=None, start="2026-11-02T09:00", end=None, location="X", notes="Y"),
            dict(title=None, start=None, end="2026-11-02T11:00", location=None, notes=None),
            dict(title=None, start="2026-11-02T09:00", end="2026-11-02T10:00", location=None, notes=None),
        ):
            scripts += _capture_scripts(lambda kwargs=kwargs: aa._calendar_update("uid-1", timeout_s=5, **kwargs))
        scripts += _capture_scripts(lambda: aa._reminders_list("milk", list_name="Home", include_completed=False, limit=5, timeout_s=5), reply="")
        scripts += _capture_scripts(lambda: aa._reminders_list(None, list_name=None, include_completed=True, limit=5, timeout_s=5), reply="")
        scripts += _capture_scripts(lambda: aa._reminders_complete("x-apple-reminder://1", timeout_s=5),
                                    reply="OK" + US + "id" + US + "name" + US + "true")
        scripts.append(aa._date_setup("probe", aa.datetime(2026, 2, 28, 9, 0)))
        with tempfile.TemporaryDirectory() as td:
            for index, script in enumerate(scripts):
                source = Path(td) / f"s{index}.applescript"
                source.write_text(script, encoding="utf-8")
                result = subprocess.run(["osacompile", "-o", str(Path(td) / f"s{index}.scpt"), str(source)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, f"script {index} failed to compile: {result.stderr}\n{script}")

    def test_reminder_without_due_date_reports_an_empty_due(self) -> None:
        scripts = _capture_scripts(lambda: aa._reminders_list(None, list_name=None, include_completed=False, limit=5, timeout_s=5), reply="")
        self.assertIn("if dueValue is not missing value then set dueText to dueValue as text", scripts[0])
        reply = US.join(["x-apple-reminder://1", "Milk", "Home", "", "false"])
        with patch.object(aa, "_run", return_value=reply):
            listed = aa._reminders_list(None, list_name=None, include_completed=False, limit=5, timeout_s=5)
        self.assertEqual("", listed["reminders"][0]["due"])
        self.assertIs(False, listed["reminders"][0]["completed"])

    def test_date_setup_sets_day_one_before_the_month(self) -> None:
        script = aa._date_setup("d", aa.datetime(2026, 9, 30, 8, 0))
        self.assertLess(script.index("set day of d to 1"), script.index("set month of d to 9"))
        self.assertLess(script.index("set month of d to 9"), script.index("set day of d to 30"))

    def test_create_validates_arguments_before_running(self) -> None:
        with patch.object(aa, "_run") as run:
            for kwargs, code in (
                (dict(title="", start="2026-10-31T10:00", end=None), "APP_ADAPTER_ARGUMENT_INVALID"),
                (dict(title="T", start="tomorrow", end=None), "APP_ADAPTER_ARGUMENT_INVALID"),
                (dict(title="T", start="2026-10-31T10:00", end="2026-10-31T09:00"), "APP_ADAPTER_ARGUMENT_INVALID"),
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(aa.AppAdapterError) as ctx:
                    aa._calendar_create(calendar=None, location=None, notes=None, timeout_s=5, **kwargs)
                self.assertEqual(code, ctx.exception.code)
            with self.assertRaises(aa.AppAdapterError):
                aa._calendar_update("uid-1", title=None, start=None, end=None, location=None, notes=None, timeout_s=5)
            run.assert_not_called()

    def test_uncertain_create_is_settled_by_lookup_or_reported_unknown(self) -> None:
        timeout = aa.AppAdapterError("CALENDAR_CREATE_FAILED", "AppleScript timed out.")
        found = {"uid": "uid-9", "summary": "T", "calendar": "C", "start": "s", "end": "e"}
        with patch.object(aa, "_run", side_effect=timeout), patch.object(aa, "_calendar_lookup", return_value=found):
            result = aa._calendar_create("T", "2026-10-31T10:00", None, calendar="C", location=None, notes=None, timeout_s=5)
        self.assertEqual("lookup_after_error", result["verification"])
        self.assertEqual("uid-9", result["event"]["uid"])
        with patch.object(aa, "_run", side_effect=timeout), patch.object(aa, "_calendar_lookup", return_value=None):
            with self.assertRaises(aa.AppAdapterError) as ctx:
                aa._calendar_create("T", "2026-10-31T10:00", None, calendar="C", location=None, notes=None, timeout_s=5)
        self.assertTrue(ctx.exception.extra["outcome_unknown"])
        self.assertFalse(ctx.exception.extra["automatic_retry"])

    def test_compile_error_is_not_reported_as_uncertain(self) -> None:
        compile_error = aa.AppAdapterError("CALENDAR_CREATE_FAILED", "1:2: syntax error: oops (-2741)", not_executed=True)
        with patch.object(aa, "_run", side_effect=compile_error), patch.object(aa, "_calendar_lookup") as lookup:
            with self.assertRaises(aa.AppAdapterError) as ctx:
                aa._calendar_create("T", "2026-10-31T10:00", None, calendar=None, location=None, notes=None, timeout_s=5)
        lookup.assert_not_called()
        self.assertNotIn("outcome_unknown", ctx.exception.extra)

    def test_duplicate_and_not_unique_replies(self) -> None:
        with patch.object(aa, "_run", return_value="DUPLICATE" + US + "uid-1" + US + "T" + US + "C" + US + "s" + US + "e"):
            result = aa._calendar_create("T", "2026-10-31T10:00", None, calendar=None, location=None, notes=None, timeout_s=5)
        self.assertFalse(result["created"])
        self.assertTrue(result["duplicate"])
        with patch.object(aa, "_run", return_value="COUNT" + US + "2"):
            with self.assertRaises(aa.AppAdapterError) as ctx:
                aa._calendar_update("uid-1", title="x", start=None, end=None, location=None, notes=None, timeout_s=5)
        self.assertEqual("CALENDAR_EVENT_NOT_UNIQUE", ctx.exception.code)
        with patch.object(aa, "_run", return_value="ALREADY" + US + "rid" + US + "Milk"):
            done = aa._reminders_complete("rid", timeout_s=5)
        self.assertFalse(done["changed"])

    def test_data_actions_do_not_yield_to_a_person_using_the_app(self) -> None:
        busy = {"reason_code": "HUMAN_ACTIVE_RESOURCE"}
        reply = "OK" + US + "uid-1" + US + "T" + US + "C" + US + "s" + US + "e"
        with patch.object(aa, "native_app_human_takeover", return_value=busy) as guard, \
             patch.object(aa, "claim_delegated_resource", return_value=None), \
             patch.object(aa, "_run", return_value=reply):
            result = aa.mac_app(SimpleNamespace(), app="Calendar", action="create_event",
                                title="T", start="2026-10-31T10:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual("uid-1", result["event"]["uid"])
        guard.assert_not_called()
        with patch.object(aa, "native_app_human_takeover", return_value=busy), \
             patch.object(aa, "claim_delegated_resource", return_value=None):
            ui_action = aa.mac_app(SimpleNamespace(), app="Calendar", action="open_event", item_id="uid-1")
        self.assertEqual("HUMAN_ACTIVE_RESOURCE", ui_action["reason_code"])


if __name__ == "__main__":
    unittest.main()
