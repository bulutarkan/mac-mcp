"""#42: after send_message, the conversation history is checked for the message.

The send returned sent=true, verified=false although Messages Reader can read the conversation. Now
the new row is looked for: same conversation, from this Mac, at or after the send, same text. Found,
the result is verified=in_conversation_history (written to the history, not proof of delivery);
not found or unreadable, it stays accepted_by_messages. Nothing is ever sent twice.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters, messages_reader
from mcp_server.app_adapters import mac_app
from tests.test_messages_reader import EPOCH, ReaderFixture

SETTINGS = SimpleNamespace()
RS, US = chr(30), chr(31)
CHATS = RS.join([US.join(["iMessage;-;+905551112233", "missing value", "Ada", "+905551112233"]),
                 US.join(["iMessage;+;chat777", "Aile", "Ada; Ayşe", "+905551112233; ayse@example.com"])])
APPROVED = {"approved": True, "decision": "send"}


def apple_now(offset_s: float = 0.0) -> int:
    return int((datetime.fromtimestamp(time.time() + offset_s, timezone.utc) - EPOCH).total_seconds() * 1e9)


class FindSentTests(ReaderFixture):
    def test_the_new_row_in_that_chat_from_me_is_found(self) -> None:
        sent_at = time.time()
        row = self.fake.add(1, "Yoldayım", from_me=1, when=apple_now(0.5))
        found = messages_reader.find_sent("iMessage;-;+905551112233", "Yoldayım", since_s=sent_at)
        self.assertEqual(f"msg-{row}", found["message_id"])
        self.assertNotIn("text", found)

    def test_the_same_words_elsewhere_earlier_or_from_them_do_not_count(self) -> None:
        sent_at = time.time()
        self.fake.add(2, "Yoldayım", from_me=1, when=apple_now(0.5))          # another chat
        self.fake.add(1, "Yoldayım", from_me=1, when=apple_now(-3600))        # an hour before the send
        self.fake.add(1, "Yoldayım", from_me=0, when=apple_now(0.5))          # sent by them
        self.fake.add(1, "Yoldayım, geliyorum", from_me=1, when=apple_now(0.5))  # other text
        self.assertIsNone(messages_reader.find_sent("iMessage;-;+905551112233", "Yoldayım", since_s=sent_at))


class SendThenCheckTests(ReaderFixture):
    def setUp(self) -> None:
        super().setUp()
        self.fake.db.commit()  # the fixture's chats are committed with the first message; some tests add none

    def send(self, rows_after_send=(), **kwargs):
        """Send to the one-to-one chat; rows_after_send are added to the history as Messages would."""
        def run(script, **_kw):
            if "send " in script:
                for when_offset in rows_after_send:
                    self.fake.add(1, "Yoldayım", from_me=1, when=apple_now(when_offset))
                return "SENT"
            return CHATS

        with patch.object(app_adapters, "_run", side_effect=run) as runner, \
                patch.object(app_adapters, "request_send_approval", return_value=APPROVED), \
                patch.object(app_adapters, "_send_dry_run", return_value=False), \
                patch.object(app_adapters.time, "sleep") as sleep, \
                patch("mcp_server.privacy_policy.check_app", return_value=None):
            result = mac_app(SETTINGS, app="Messages", action="send_message", body="Yoldayım",
                             item_id="iMessage;-;+905551112233", **kwargs)
        sends = [call for call in runner.call_args_list if "send " in call.args[0]]
        return result, sends, sleep

    def test_a_message_in_the_history_is_verified_without_claiming_delivery(self) -> None:
        result, sends, _ = self.send(rows_after_send=(0.3,))
        self.assertTrue(result["verified"], result)
        self.assertEqual("in_conversation_history", result["verification"])
        self.assertTrue(result["message_id"].startswith("msg-"))
        self.assertIn("not that the other person received it", result["note"])
        self.assertEqual(1, len(sends))

    def test_a_late_row_is_found_on_a_later_look(self) -> None:
        calls = {"n": 0}
        real = messages_reader.find_sent

        def slow(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the history is written a moment after the send
            return real(*args, **kwargs)

        with patch.object(messages_reader, "find_sent", side_effect=slow):
            result, sends, sleep = self.send(rows_after_send=(0.3,))
        self.assertTrue(result["verified"])
        self.assertEqual(2, calls["n"])
        self.assertEqual(1, sleep.call_count)
        self.assertEqual(1, len(sends))

    def test_nothing_in_the_history_stays_accepted_and_is_not_resent(self) -> None:
        result, sends, sleep = self.send()
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual("accepted_by_messages", result["verification"])
        self.assertEqual("not_found_yet", result["history_check"])
        self.assertIn("Do not send it again", result["note"])
        self.assertEqual(1, len(sends))
        self.assertEqual(app_adapters._MESSAGES_HISTORY_ATTEMPTS - 1, sleep.call_count)

    def test_no_reader_access_keeps_the_old_answer(self) -> None:
        with patch.object(messages_reader, "_RUNNER", return_value={"ok": False, "error": "full_disk_access"}):
            result, sends, _ = self.send(rows_after_send=(0.3,))
        self.assertFalse(result["verified"])
        self.assertEqual("unavailable", result["history_check"])
        self.assertEqual("MESSAGES_FULL_DISK_ACCESS_REQUIRED", result["history_check_reason"])
        self.assertEqual(1, len(sends))


if __name__ == "__main__":
    unittest.main()
