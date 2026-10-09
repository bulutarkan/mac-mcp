from __future__ import annotations

import re
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
NOTE_OK = US.join(["OK", "x-coredata://note/p1", "Groceries", "Notes"])
DRAFT_OK = US.join(["OK", "<draft-1@mac>", "Hello", "Work", "Me <me@example.com>"])


def _scripts(call, reply: str) -> list[str]:
    seen: list[str] = []

    def fake_run(script, **_kwargs):
        seen.append(script)
        return reply

    with patch.object(aa, "_run", side_effect=fake_run):
        call()
    return seen


class NotesAndMailDraftTests(unittest.TestCase):
    def test_registry_and_risk_treat_note_and_draft_creation_as_local_writes(self) -> None:
        self.assertIn("create_note", aa.supported_actions("notes"))
        self.assertIn("create_draft", aa.supported_actions("mail"))
        self.assertNotIn("send_message", aa.supported_actions("mail"))
        for app, action in (("Notes", "create_note"), ("Mail", "create_draft")):
            _, risk = resolve_risk("mac_app", {"app": app, "action": action})
            self.assertIn(Capability.LOCAL_WRITE, risk.capabilities)
            self.assertNotIn(Capability.UI_ACTION, risk.capabilities)
            self.assertNotIn(Capability.EXTERNAL_SIDE_EFFECT, risk.capabilities)

    @unittest.skipUnless(shutil.which("osacompile"), "osacompile is macOS-only")
    def test_every_generated_script_compiles(self) -> None:
        scripts: list[str] = []
        scripts += _scripts(lambda: aa._notes_create('T "q"', "a\n\nb <i>", folder=None, account=None, timeout_s=5), NOTE_OK)
        scripts += _scripts(lambda: aa._notes_create("T", None, folder="F", account="iCloud", timeout_s=5), NOTE_OK)
        scripts += _scripts(lambda: aa._notes_create("T", "x", folder="F", account=None, timeout_s=5), NOTE_OK)
        scripts += _scripts(lambda: aa._mail_create_draft(
            "S", "hi\nthere", to="a@b.co, c@d.org", cc="e@f.io", account=None, timeout_s=5), DRAFT_OK)
        scripts += _scripts(lambda: aa._mail_create_draft("S", "", to="", cc=None, account="me@x.com", timeout_s=5), DRAFT_OK)
        scripts += _scripts(lambda: aa._mail_create_draft("S", "", to="a@b.co", cc=None, account="Work", timeout_s=5), DRAFT_OK)
        with tempfile.TemporaryDirectory() as td:
            for index, script in enumerate(scripts):
                source = Path(td) / f"s{index}.applescript"
                source.write_text(script, encoding="utf-8")
                result = subprocess.run(["osacompile", "-o", str(Path(td) / f"s{index}.scpt"), str(source)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, f"script {index} failed to compile: {result.stderr}\n{script}")

    def test_draft_script_saves_and_never_sends(self) -> None:
        script = _scripts(lambda: aa._mail_create_draft(
            "Hello", "Body", to="a@b.co", cc=None, account="Work", timeout_s=5), DRAFT_OK)[0]
        self.assertIn("    save m", script)
        self.assertIn("visible:false", script)
        self.assertIsNone(re.search(r"\bsend\b", script, re.IGNORECASE))
        # Closing without saving could discard the draft that was just saved.
        self.assertNotIn("saving no", script)
        self.assertIn("set accountAddresses to email addresses of a", script)

    def test_note_body_is_escaped_html_led_by_the_title(self) -> None:
        script = _scripts(lambda: aa._notes_create(
            "<b>T</b>", "line <script>\n\nend", folder=None, account=None, timeout_s=5), NOTE_OK)[0]
        self.assertIn("<div><h1>&lt;b&gt;T&lt;/b&gt;</h1></div>", script)
        self.assertIn("<div>line &lt;script&gt;</div><div><br></div><div>end</div>", script)
        self.assertNotIn("<script>", script)

    def test_arguments_are_validated_before_running(self) -> None:
        with patch.object(aa, "_run") as run:
            for call in (
                lambda: aa._notes_create("", "x", folder=None, account=None, timeout_s=5),
                lambda: aa._notes_create("two\nlines", "x", folder=None, account=None, timeout_s=5),
                lambda: aa._mail_create_draft("", "x", to="a@b.co", cc=None, account=None, timeout_s=5),
                lambda: aa._mail_create_draft("S", "x", to="not an address", cc=None, account=None, timeout_s=5),
                lambda: aa._mail_create_draft("S", "x", to='a@b.co", "x', cc=None, account=None, timeout_s=5),
                lambda: aa._mail_create_draft("S", "x", to=",".join(f"u{i}@b.co" for i in range(21)),
                                              cc=None, account=None, timeout_s=5),
            ):
                with self.subTest(), self.assertRaises(aa.AppAdapterError) as ctx:
                    call()
                self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", ctx.exception.code)
            run.assert_not_called()

    def test_control_characters_are_removed_from_text(self) -> None:
        script = _scripts(lambda: aa._mail_create_draft(
            "S", "a" + US + "b" + chr(30) + "c\td", to=None, cc=None, account="Work", timeout_s=5), DRAFT_OK)[0]
        self.assertIn('content:"abc\td"', script)

    def test_sender_is_never_guessed_between_accounts(self) -> None:
        with patch.object(aa, "_run", return_value=US.join(["ACCOUNT_REQUIRED", "Work", "Home"])):
            with self.assertRaises(aa.AppAdapterError) as ctx:
                aa._mail_create_draft("S", "x", to="a@b.co", cc=None, account=None, timeout_s=5)
        self.assertEqual("MAIL_ACCOUNT_REQUIRED", ctx.exception.code)
        self.assertEqual(["Work", "Home"], ctx.exception.extra["accounts"])
        for reply, code in (("NO_ACCOUNT", "MAIL_ACCOUNT_NOT_FOUND"), ("ACCOUNT_NOT_UNIQUE", "MAIL_ACCOUNT_NOT_UNIQUE")):
            with patch.object(aa, "_run", return_value=reply), self.assertRaises(aa.AppAdapterError) as ctx:
                aa._mail_create_draft("S", "x", to=None, cc=None, account="Work", timeout_s=5)
            self.assertEqual(code, ctx.exception.code)

    def test_results_and_duplicates(self) -> None:
        with patch.object(aa, "_run", return_value=DRAFT_OK):
            draft = aa._mail_create_draft("Hello", "x", to="a@b.co", cc=None, account="Work", timeout_s=5)
        self.assertEqual((True, False, "<draft-1@mac>"), (draft["created"], draft["sent"], draft["draft"]["message_id"]))
        with patch.object(aa, "_run", return_value=DRAFT_OK.replace("OK", "DUPLICATE", 1)):
            again = aa._mail_create_draft("Hello", "x", to="a@b.co", cc=None, account="Work", timeout_s=5)
        self.assertEqual((False, True), (again["created"], again["duplicate"]))
        with patch.object(aa, "_run", return_value=NOTE_OK):
            note = aa._notes_create("Groceries", "milk", folder=None, account=None, timeout_s=5)
        self.assertEqual({"id": "x-coredata://note/p1", "title": "Groceries", "folder": "Notes"}, note["note"])
        with patch.object(aa, "_run", return_value=NOTE_OK.replace("OK", "DUPLICATE", 1)):
            self.assertFalse(aa._notes_create("Groceries", "milk", folder=None, account=None, timeout_s=5)["created"])
        for reply, code in (("NO_FOLDER", "NOTES_FOLDER_NOT_FOUND"), ("FOLDER_NOT_UNIQUE", "NOTES_FOLDER_NOT_UNIQUE")):
            with patch.object(aa, "_run", return_value=reply), self.assertRaises(aa.AppAdapterError) as ctx:
                aa._notes_create("T", None, folder="F", account=None, timeout_s=5)
            self.assertEqual(code, ctx.exception.code)

    def test_uncertain_writes_are_settled_or_reported_unknown(self) -> None:
        timeout = aa.AppAdapterError("NOTES_CREATE_FAILED", "AppleScript timed out.")
        found = US.join(["x-coredata://note/p2", "T", "Notes"])
        with patch.object(aa, "_run", side_effect=[timeout, found]):
            note = aa._notes_create("T", None, folder=None, account=None, timeout_s=5)
        self.assertEqual("lookup_after_error", note["verification"])
        with patch.object(aa, "_run", side_effect=[timeout, ""]), self.assertRaises(aa.AppAdapterError) as ctx:
            aa._notes_create("T", None, folder=None, account=None, timeout_s=5)
        self.assertTrue(ctx.exception.extra["outcome_unknown"])
        draft_timeout = aa.AppAdapterError("MAIL_DRAFT_FAILED", "AppleScript timed out.")
        with patch.object(aa, "_run", side_effect=draft_timeout), self.assertRaises(aa.AppAdapterError) as ctx:
            aa._mail_create_draft("S", "x", to=None, cc=None, account="Work", timeout_s=5)
        self.assertEqual((True, False), (ctx.exception.extra["outcome_unknown"], ctx.exception.extra["automatic_retry"]))
        compile_error = aa.AppAdapterError("NOTES_CREATE_FAILED", "syntax error: x (-2741)", not_executed=True)
        with patch.object(aa, "_run", side_effect=[compile_error]), self.assertRaises(aa.AppAdapterError) as ctx:
            aa._notes_create("T", None, folder=None, account=None, timeout_s=5)
        self.assertNotIn("outcome_unknown", ctx.exception.extra)

    def test_creation_does_not_yield_to_a_person_using_the_app(self) -> None:
        busy = {"reason_code": "HUMAN_ACTIVE_RESOURCE"}
        for app, action, reply, kwargs in (
            ("Notes", "create_note", NOTE_OK, {"title": "Groceries"}),
            ("Mail", "create_draft", DRAFT_OK, {"title": "Hello", "to": "a@b.co", "account": "Work"}),
        ):
            with self.subTest(app=app), \
                 patch.object(aa, "native_app_human_takeover", return_value=busy) as guard, \
                 patch.object(aa, "claim_delegated_resource", return_value=None), \
                 patch.object(aa, "_run", return_value=reply):
                result = aa.mac_app(SimpleNamespace(), app=app, action=action, **kwargs)
            self.assertTrue(result["ok"], result)
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
