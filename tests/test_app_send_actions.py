"""Mail send_mail and Messages send_message: nothing goes out without a confirmed Send."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import app_adapters, send_approval
from mcp_server.app_adapters import mac_app
from mcp_server.policy import Capability, resolve_risk
from mcp_server.security import load_settings

SETTINGS = load_settings()
US, RS = "\x1f", "\x1e"
ROOT = Path(__file__).resolve().parents[1]
APPROVED = {"approved": True, "decision": "send", "ui": "panel"}
CANCELLED = {"approved": False, "decision": "cancel", "ui": "panel"}


class _SettingsFile:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        path = Path(self.dir.name) / "settings.json"
        path.write_text(json.dumps(self.payload), encoding="utf-8")
        self.env = patch.dict(os.environ, {"MAC_MCP_SETTINGS_PATH": str(path)})
        self.env.start()
        return path

    def __exit__(self, *exc):
        self.env.stop()
        self.dir.cleanup()


class ApprovalTests(unittest.TestCase):
    def _ask(self):
        return send_approval.request_send_approval(
            None, app="mail", app_bundle_id="com.apple.mail", title="Send this email?",
            fields=[{"label": "To", "value": "a@example.com"}, {"label": "Cc", "value": ""}], body="Hi",
        )

    def test_confirmation_is_on_unless_explicitly_turned_off(self) -> None:
        for payload, expected in (({}, True), ({"apps": {"mail": {}}}, True), ({"apps": "junk"}, True),
                                  ({"apps": {"mail": {"confirm_send": "no"}}}, True),
                                  ({"apps": {"mail": {"confirm_send": False}}}, False)):
            with self.subTest(payload=payload), _SettingsFile(payload):
                self.assertEqual(expected, send_approval.confirm_send_enabled("mail"))

    def test_turned_off_setting_skips_the_question(self) -> None:
        with _SettingsFile({"apps": {"mail": {"confirm_send": False}}}), \
             patch.object(send_approval, "_panel") as panel:
            result = self._ask()
        panel.assert_not_called()
        self.assertEqual({"approved": True, "decision": "not_asked"}, {k: result[k] for k in ("approved", "decision")})

    def test_only_a_click_on_send_approves(self) -> None:
        for decision, approved in (("send", True), ("cancel", False), ("timeout", False)):
            with self.subTest(decision=decision), _SettingsFile({}), \
                 patch.object(send_approval, "_panel", return_value=decision):
                self.assertEqual(approved, self._ask()["approved"])

    def test_panel_request_hides_empty_fields_and_names_the_setting(self) -> None:
        with _SettingsFile({}), patch.object(send_approval, "_panel", return_value="cancel") as panel:
            self._ask()
        request = panel.call_args.args[0]
        self.assertEqual([{"label": "To", "value": "a@example.com"}], request["fields"])
        self.assertIn("Settings › Apps", request["footnote"])
        self.assertEqual("com.apple.mail", request["app_bundle_id"])

    def test_plain_dialog_is_the_fallback_and_no_question_means_no_send(self) -> None:
        with _SettingsFile({}), patch.object(send_approval, "_panel", return_value=None), \
             patch.object(send_approval, "_dialog", return_value="send"):
            self.assertEqual({"approved": True, "decision": "send", "ui": "dialog"}, self._ask())
        with _SettingsFile({}), patch.object(send_approval, "_panel", return_value=None), \
             patch.object(send_approval, "_dialog", return_value=None):
            result = self._ask()
        self.assertFalse(result["approved"])
        self.assertEqual("unavailable", result["decision"])

    def test_panel_output_is_parsed_strictly(self) -> None:
        proc = type("Proc", (), {"stdout": 'noise\n{"decision":"send"}\n'})()
        with patch.object(send_approval, "_helper", return_value=Path("/bin/true")), \
             patch.object(send_approval.subprocess, "run", return_value=proc) as run:
            self.assertEqual("send", send_approval._panel({"title": "x"}, 30))
        self.assertEqual({"title": "x"}, json.loads(run.call_args.kwargs["input"]))  # text travels on stdin
        proc.stdout = '{"decision":"maybe"}'
        with patch.object(send_approval, "_helper", return_value=Path("/bin/true")), \
             patch.object(send_approval.subprocess, "run", return_value=proc):
            self.assertIsNone(send_approval._panel({}, 30))

    def test_panel_never_sends_on_a_keystroke_or_takes_focus(self) -> None:
        source = (ROOT / "mcp_server" / "send_approval.swift").read_text(encoding="utf-8")
        send_button = source[source.index('Button("Send")'):source.index('Button("Send")') + 200]
        self.assertNotIn("keyboardShortcut", send_button)
        self.assertIn(".nonactivatingPanel", source)
        self.assertIn("readDataToEndOfFile", source)
        self.assertIn('finish("timeout")', source)
        self.assertIn('func windowWillClose(_ notification: Notification) { finish("cancel") }', source)


class MailSendTests(unittest.TestCase):
    RESOLVED = "OK" + US + "Work" + US + "Ada <ada@example.com>"

    def _send(self, approval, outputs, **kwargs):
        args = {"title": "Report", "body": "Numbers attached.", "to": "bob@example.com", "cc": "", **kwargs}
        with patch.object(app_adapters, "_run", side_effect=list(outputs)) as run, \
             patch.object(app_adapters, "request_send_approval", return_value=approval) as ask, \
             patch.object(app_adapters.time, "sleep"):
            result = mac_app(SETTINGS, app="Mail", action="send_mail", **args)
        return result, run, ask

    def test_confirmed_email_is_sent_and_found_in_sent(self) -> None:
        result, run, ask = self._send(APPROVED, [self.RESOLVED, "SENT", "1"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["sent"])
        self.assertTrue(result["verified"])
        self.assertEqual("sent_mailbox", result["verification"])
        fields = {f["label"]: f["value"] for f in ask.call_args.kwargs["fields"]}
        self.assertEqual({"From": "Ada <ada@example.com>", "To": "bob@example.com", "Cc": "", "Subject": "Report"}, fields)
        self.assertEqual("Numbers attached.", ask.call_args.kwargs["body"])
        self.assertNotIn("send m", run.call_args_list[0].args[0])  # resolving the sender sends nothing
        self.assertIn("if (send m) then", run.call_args_list[1].args[0])

    def test_cancelled_email_never_reaches_the_send_script(self) -> None:
        result, run, _ = self._send(CANCELLED, [self.RESOLVED])
        self.assertFalse(result["ok"])
        self.assertFalse(result["sent"])
        self.assertEqual("SEND_CANCELLED", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(1, run.call_count)

    def test_dry_run_stops_after_the_confirmation(self) -> None:
        with patch.dict(os.environ, {"MAC_MCP_SEND_DRY_RUN": "1"}):
            result, run, _ = self._send(APPROVED, [self.RESOLVED])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["sent"])
        self.assertEqual(1, run.call_count)

    def test_mail_that_is_not_yet_in_sent_is_reported_honestly(self) -> None:
        result, _, _ = self._send(APPROVED, [self.RESOLVED, "SENT"] + ["0"] * 8)
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual("accepted_by_mail", result["verification"])

    def test_missing_recipient_or_ambiguous_account_sends_nothing(self) -> None:
        result, run, ask = self._send(APPROVED, [], to="")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])
        ask.assert_not_called()
        result, _, ask = self._send(APPROVED, ["ACCOUNT_REQUIRED" + US + "Work" + US + "Home"])
        self.assertEqual("MAIL_ACCOUNT_REQUIRED", result["reason_code"])
        self.assertEqual(["Work", "Home"], result["accounts"])
        ask.assert_not_called()

    def test_send_skips_the_window_ownership_check(self) -> None:
        with patch.object(app_adapters, "native_app_human_takeover") as guard:
            self._send(CANCELLED, [self.RESOLVED])
        guard.assert_not_called()


class MessagesSendTests(unittest.TestCase):
    CHATS = RS.join([
        US.join(["iMessage;-;+905551112233", "missing value", "Ada Lovelace", "+905551112233"]),
        US.join(["iMessage;+;chat42", "Engine Team", "Ada Lovelace; Charles Babbage", "ada@example.com; cb@example.com"]),
    ])

    def _send(self, approval, outputs, **kwargs):
        with patch.object(app_adapters, "_run", side_effect=list(outputs)) as run, \
             patch.object(app_adapters, "request_send_approval", return_value=approval) as ask:
            result = mac_app(SETTINGS, app="Messages", action="send_message", body="On my way", **kwargs)
        return result, run, ask

    def test_confirmed_message_goes_to_exactly_that_chat(self) -> None:
        result, run, ask = self._send(APPROVED, [self.CHATS, "SENT"], item_id="iMessage;+;chat42")
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual("accepted_by_messages", result["verification"])
        self.assertIn('to chat id "iMessage;+;chat42"', run.call_args_list[1].args[0])
        fields = {f["label"]: f["value"] for f in ask.call_args.kwargs["fields"]}
        self.assertEqual("Engine Team", fields["To"])
        self.assertEqual("On my way", ask.call_args.kwargs["body"])

    def test_query_must_match_one_chat(self) -> None:
        result, run, ask = self._send(APPROVED, [self.CHATS], query="ada")
        self.assertEqual("MESSAGES_CHAT_AMBIGUOUS", result["reason_code"])
        self.assertEqual(2, len(result["candidates"]))
        ask.assert_not_called()
        result, _, _ = self._send(APPROVED, [self.CHATS, "SENT"], query="engine")
        self.assertEqual("iMessage;+;chat42", result["chat"]["chat_id"])

    def test_unknown_chat_or_empty_text_sends_nothing(self) -> None:
        result, _, ask = self._send(APPROVED, [self.CHATS], item_id="iMessage;-;nobody")
        self.assertEqual("MESSAGES_CHAT_NOT_FOUND", result["reason_code"])
        ask.assert_not_called()
        with patch.object(app_adapters, "_run") as run:
            result = mac_app(SETTINGS, app="Messages", action="send_message", body="  ", item_id="x")
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID", result["reason_code"])
        run.assert_not_called()

    def test_cancelled_message_never_reaches_the_send_script(self) -> None:
        result, run, _ = self._send(CANCELLED, [self.CHATS], item_id="iMessage;+;chat42")
        self.assertEqual("SEND_CANCELLED", result["reason_code"])
        self.assertEqual(1, run.call_count)
        self.assertNotIn("send ", run.call_args_list[0].args[0].replace("send_", ""))


class PolicyAndSettingsTests(unittest.TestCase):
    def test_sends_are_external_side_effects_without_a_second_approval(self) -> None:
        for action in ("send_mail", "send_message"):
            _, risk = resolve_risk("mac_app", {"app": "Mail", "action": action})
            self.assertIn(Capability.EXTERNAL_SIDE_EFFECT, risk.capabilities)
            self.assertFalse(risk.destructive)
        for action in ("find_chats", "list_workspaces"):
            _, risk = resolve_risk("mac_app", {"app": "Messages", "action": action})
            self.assertNotIn(Capability.UI_ACTION, risk.capabilities)

    def test_settings_app_has_an_apps_section_with_both_switches_on_by_default(self) -> None:
        store = (ROOT / "menu_app" / "Sources" / "SettingsStore.swift").read_text(encoding="utf-8")
        view = (ROOT / "menu_app" / "Sources" / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn("apps: Apps(mail: AppSend(confirm_send: true), messages: AppSend(confirm_send: true))", store)
        self.assertIn("mailConfirmSend = current.apps?.mail?.confirm_send ?? true", store)
        self.assertIn("messagesConfirmSend = current.apps?.messages?.confirm_send ?? true", store)
        self.assertIn('case .apps: return "Apps"', view)
        self.assertIn("case .apps: appsPane", view)


if __name__ == "__main__":
    unittest.main()
