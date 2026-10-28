"""Messages read and search (#261): read-only, every message with its chat, sender and time.

A temporary chat.db with the tables Messages uses stands in for ~/Library/Messages/chat.db, and a
runner with the helper's request/result shape stands in for Mac MCP Messages Reader.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import base64
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters, messages_reader, privacy_policy

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT, service_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, date INTEGER,
                      is_from_me INTEGER, service TEXT, cache_has_attachments INTEGER, handle_id INTEGER,
                      associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, created_date INTEGER, filename TEXT, uti TEXT,
                         mime_type TEXT, transfer_name TEXT, total_bytes INTEGER, is_sticker INTEGER DEFAULT 0,
                         hide_attachment INTEGER DEFAULT 0, is_commsafety_sensitive INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def archived(text: str) -> bytes:
    """An attributedBody the way Messages writes one: the string follows NSString and a length."""
    raw = text.encode("utf-8")
    size = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject"
            b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + size + raw + b"\x86\x84\x02iI\x01")


def py_text(text, blob):
    """The helper's mm_text(), for the stand-in database."""
    if text:
        return text
    if not blob:
        return None
    start = blob.find(b"NSString")
    plus = blob.find(b"+", start + 8)
    index = plus + 1
    size = blob[index]
    index += 1
    if size == 0x81:
        size, index = int.from_bytes(blob[index:index + 2], "little"), index + 2
    return blob[index:index + size].decode("utf-8")


def py_fold(text):
    """The helper's mm_fold()."""
    if text is None:
        return None
    value = unicodedata.normalize("NFKD", text).casefold()
    return "".join(char for char in value if not unicodedata.combining(char)).replace("ı", "i")


def apple_ns(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> int:
    moment = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int((moment - EPOCH).total_seconds() * 1e9)


class FakeDatabase:
    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "chat.db"
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO handle VALUES (?, ?)", [(1, "+905551112233"), (2, "ayse@example.com"),
                                                           (3, "+905559998877")])
        db.executemany("INSERT INTO chat VALUES (?, ?, ?, ?, ?)", [
            (1, "any;-;+905551112233", "+905551112233", "", "iMessage"),
            (2, "any;+;chat777", "chat777", "Aile", "iMessage"),
            (3, "any;-;ayse@example.com", "ayse@example.com", "", "iMessage"),
        ])
        db.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 1), (2, 2), (2, 3), (3, 2)])
        self.next_id = 1
        self.db = db

    def add(self, chat: int, text, *, handle: int = 1, from_me: int = 0, when: int, body: bytes = None,
            reaction: int = 0, attachments: int = 0) -> int:
        rowid = self.next_id
        self.next_id += 1
        self.db.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                        (rowid, f"msg-{rowid}", text, body, when, from_me, "iMessage", attachments, handle, reaction))
        self.db.execute("INSERT INTO chat_message_join VALUES (?,?,?)", (chat, rowid, when))
        self.db.commit()
        return rowid

    def attach(self, message: int, name: str, mime: str, size: int, *, uti: str = "", hidden: int = 0,
               sensitive: int = 0, when: int = 0) -> None:
        rowid = self.db.execute("SELECT count(*) FROM attachment").fetchone()[0] + 1
        self.db.execute("INSERT INTO attachment (ROWID, guid, created_date, filename, uti, mime_type, transfer_name, "
                        "total_bytes, hide_attachment, is_commsafety_sensitive) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rowid, f"att-{rowid}", when, f"~/Library/Messages/Attachments/x/{name}", uti, mime, name,
                         size, hidden, sensitive))
        self.db.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (message, rowid))
        self.db.commit()

    def runner(self, sql, params, max_rows):
        """What Mac MCP Messages Reader returns, from a read-only connection."""
        bound = [base64.b64decode(item["blob"]) if isinstance(item, dict) else item for item in params]
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.create_function("mm_text", 2, py_text, deterministic=True)
        connection.create_function("mm_fold", 1, py_fold, deterministic=True)
        try:
            cursor = connection.execute(sql, bound)
            columns = [item[0] for item in cursor.description]
            rows = []
            for row in cursor.fetchmany(max_rows):
                rows.append([{"blob": base64.b64encode(value).decode()} if isinstance(value, bytes) else value
                             for value in row])
        finally:
            connection.close()
        return {"ok": True, "columns": columns, "rows": rows, "truncated": False}


class ReaderFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDatabase()
        patcher = patch.object(messages_reader, "_RUNNER", self.fake.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        contacts = patch.object(messages_reader, "_contact_handles", return_value=[])
        contacts.start()
        self.addCleanup(contacts.stop)


class ReadMessagesTests(ReaderFixture):
    def test_newest_first_with_chat_sender_and_time_then_older_by_cursor(self) -> None:
        for day in range(1, 6):
            self.fake.add(1, f"mesaj {day}", when=apple_ns(2026, 10, day))
        self.fake.add(1, None, body=archived("benden son mesaj ✓"), from_me=1, when=apple_ns(2026, 10, 6))
        first = messages_reader.read_messages("iMessage;-;+905551112233", None, limit=3)
        self.assertEqual("any;-;+905551112233", first["chat_id"])
        self.assertEqual(["benden son mesaj ✓", "mesaj 5", "mesaj 4"], [m["text"] for m in first["messages"]])
        newest = first["messages"][0]
        self.assertEqual(("me", True), (newest["sender"], newest["from_me"]))
        self.assertEqual("+905551112233", first["messages"][1]["sender"])
        self.assertEqual("any;-;+905551112233", newest["chat_id"])
        self.assertTrue(newest["date"].startswith("2026-10-06"))
        older = messages_reader.read_messages("any;-;+905551112233", None, limit=3, cursor=first["next_cursor"])
        self.assertEqual(["mesaj 3", "mesaj 2", "mesaj 1"], [m["text"] for m in older["messages"]])
        self.assertIsNone(older["next_cursor"])

    def test_a_chat_by_number_name_or_group_name(self) -> None:
        self.fake.add(2, "grup selam", handle=2, when=apple_ns(2026, 10, 1))
        self.fake.add(3, "ayşe selam", handle=2, when=apple_ns(2026, 10, 2))
        self.assertEqual("Aile", messages_reader.read_messages(None, "aile", limit=5)["chat"])
        self.assertEqual("any;-;ayse@example.com", messages_reader.read_messages(None, "ayse@", limit=5)["chat_id"])
        # The number is in the one-to-one chat and in the group: the one-to-one chat wins.
        self.assertEqual("any;-;+905551112233", messages_reader.read_messages(None, "0555 111 22 33", limit=5)["chat_id"])

    def test_reactions_and_group_events_are_not_messages(self) -> None:
        self.fake.add(1, "asıl mesaj", when=apple_ns(2026, 10, 1))
        self.fake.add(1, "Loved “asıl mesaj”", reaction=2000, when=apple_ns(2026, 10, 2))
        texts = [m["text"] for m in messages_reader.read_messages("any;-;+905551112233", None, limit=10)["messages"]]
        self.assertEqual(["asıl mesaj"], texts)

    def test_dates_bound_the_conversation(self) -> None:
        for day in (1, 10, 20):
            self.fake.add(1, f"gün {day}", when=apple_ns(2026, 10, day))
        result = messages_reader.read_messages("any;-;+905551112233", None, limit=10,
                                               date_from="2026-10-05", date_to="2026-10-15")
        self.assertEqual(["gün 10"], [m["text"] for m in result["messages"]])

    def test_unknown_chat_and_bad_cursor(self) -> None:
        with self.assertRaises(messages_reader.MessagesError) as missing:
            messages_reader.read_messages(None, "nobody-here", limit=5)
        self.assertEqual("MESSAGES_CHAT_NOT_FOUND", missing.exception.code)
        with self.assertRaises(messages_reader.MessagesError):
            messages_reader.read_messages("any;-;+905551112233", None, limit=5, cursor="drop table")

    def test_long_text_is_clipped_and_secrets_redacted(self) -> None:
        self.fake.add(1, None, body=archived("x" * 2500), when=apple_ns(2026, 10, 1))
        self.fake.add(1, "token: api_key=sk-ant-api03-" + "A" * 40, when=apple_ns(2026, 10, 2))
        messages = messages_reader.read_messages("any;-;+905551112233", None, limit=5)["messages"]
        self.assertNotIn("A" * 40, messages[0]["text"])
        self.assertEqual(messages_reader.TEXT_LIMIT, len(messages[1]["text"]))
        self.assertTrue(messages[1]["text_truncated"])


    def test_attachments_come_with_name_kind_size_and_date_never_their_path(self) -> None:
        # Only has_attachments used to come back (#132).
        photo = self.fake.add(1, None, attachments=1, when=apple_ns(2026, 10, 1))
        self.fake.attach(photo, "IMG_0001.HEIC", "image/heic", 2_400_000, uti="public.heic",
                         when=int((datetime(2026, 10, 1, 12, tzinfo=timezone.utc) - EPOCH).total_seconds()))
        self.fake.attach(photo, "link.pluginPayloadAttachment", "", 100)
        self.fake.attach(photo, "hidden.bin", "", 5, hidden=1)
        doc = self.fake.add(1, "sözleşme ekte", attachments=1, when=apple_ns(2026, 10, 2))
        self.fake.attach(doc, "Sozlesme.pdf", "application/pdf", 88_000)
        private = self.fake.add(1, None, attachments=1, when=apple_ns(2026, 10, 3))
        self.fake.attach(private, "private.jpg", "image/jpeg", 10, sensitive=1)
        many = self.fake.add(1, "çok foto", attachments=1, when=apple_ns(2026, 10, 4))
        for index in range(messages_reader.ATTACHMENTS_PER_MESSAGE + 3):
            self.fake.attach(many, f"p{index}.jpg", "image/jpeg", 1)
        plain = self.fake.add(1, "eksiz", when=apple_ns(2026, 10, 5))
        rows = {m["message_id"]: m for m in messages_reader.read_messages("any;-;+905551112233", None, limit=10)["messages"]}
        self.assertEqual([{"kind": "image", "name": "IMG_0001.HEIC", "type": "image/heic", "bytes": 2_400_000,
                           "date": rows[f"msg-{photo}"]["attachments"][0]["date"]}], rows[f"msg-{photo}"]["attachments"])
        self.assertTrue(rows[f"msg-{photo}"]["attachments"][0]["date"].startswith("2026-10-01"))
        self.assertEqual(("pdf", "Sozlesme.pdf"), (rows[f"msg-{doc}"]["attachments"][0]["kind"],
                                                  rows[f"msg-{doc}"]["attachments"][0]["name"]))
        self.assertNotIn("name", rows[f"msg-{private}"]["attachments"][0])
        self.assertIn("sensitive", rows[f"msg-{private}"]["attachments"][0]["name_hidden"])
        self.assertEqual(messages_reader.ATTACHMENTS_PER_MESSAGE, len(rows[f"msg-{many}"]["attachments"]))
        self.assertNotIn("attachments", rows[f"msg-{plain}"])
        self.assertNotIn("Library/Messages", json.dumps(rows))
        found = messages_reader.search_messages("sözleşme", limit=5)["messages"]
        self.assertEqual("Sozlesme.pdf", found[0]["attachments"][0]["name"])
        # Files by name, across chats, for personal_search: never the sensitive one, never a path.
        files = messages_reader.search_attachments("sozlesme", limit=5)["attachments"]
        self.assertEqual([("Sozlesme.pdf", "pdf", f"msg-{doc}", "any;-;+905551112233")],
                         [(row["name"], row["kind"], row["message_id"], row["chat_id"]) for row in files])
        self.assertEqual([], messages_reader.search_attachments("private", limit=5)["attachments"])
        self.assertEqual([], messages_reader.search_attachments("link", limit=5)["attachments"])


class SearchMessagesTests(ReaderFixture):
    def test_case_and_accent_insensitive_across_chats_including_attributed_text(self) -> None:
        self.fake.add(1, "Akşam ÇAY içelim mi?", when=apple_ns(2026, 10, 1))
        self.fake.add(2, None, body=archived("çay demledim"), handle=2, when=apple_ns(2026, 10, 2))
        self.fake.add(3, "kahve olsun", handle=2, when=apple_ns(2026, 10, 3))
        for query in ("çay", "CAY", "Çay"):
            result = messages_reader.search_messages(query, limit=10)
            self.assertEqual(["çay demledim", "Akşam ÇAY içelim mi?"], [m["text"] for m in result["messages"]], query)
        self.assertEqual(["Aile", "+905551112233"], [m["chat"] for m in result["messages"]])
        self.assertIsNone(result["next_cursor"])
        self.fake.add(1, "İstanbul'da ılık hava", when=apple_ns(2026, 10, 4))
        for query in ("istanbul", "ilik", "ILIK", "ılık"):
            self.assertEqual(1, messages_reader.search_messages(query, limit=5)["count"], query)

    def test_special_characters_are_literal(self) -> None:
        self.fake.add(1, "indirim %50_bugün", when=apple_ns(2026, 10, 1))
        self.fake.add(1, "indirim 5000 bugün", when=apple_ns(2026, 10, 2))
        self.fake.add(1, "O'Reilly \"kitap\"", when=apple_ns(2026, 10, 3))
        self.assertEqual(1, messages_reader.search_messages("%50_", limit=10)["count"])
        self.assertEqual(1, messages_reader.search_messages("O'Reilly \"kitap\"", limit=10)["count"])

    def test_empty_result_scope_and_paging(self) -> None:
        for day in range(1, 8):
            self.fake.add(1 if day % 2 else 3, f"toplantı {day}", handle=1 if day % 2 else 2, when=apple_ns(2026, 10, day))
        self.assertEqual([], messages_reader.search_messages("bulunmayan kelime", limit=5)["messages"])
        scoped = messages_reader.search_messages("toplantı", item_id="any;-;ayse@example.com", limit=10)
        self.assertEqual(["toplantı 6", "toplantı 4", "toplantı 2"], [m["text"] for m in scoped["messages"]])
        first = messages_reader.search_messages("toplantı", limit=4)
        self.assertEqual(4, first["count"])
        rest = messages_reader.search_messages("toplantı", limit=10, cursor=first["next_cursor"])
        self.assertEqual(["toplantı 3", "toplantı 2", "toplantı 1"], [m["text"] for m in rest["messages"]])

    def test_a_query_needs_two_characters(self) -> None:
        with self.assertRaises(messages_reader.MessagesError):
            messages_reader.search_messages(" a ", limit=5)


@unittest.skipUnless(shutil.which("swiftc"), "needs swiftc")
class ReaderHelperBinaryTests(unittest.TestCase):
    """The real helper, run on an in-memory database (--memory), for its SQL functions and guard."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp())
        cls.binary = cls.dir / "reader"
        subprocess.run(["swiftc", "-O", str(ROOT / "menu_app" / "MessagesReader" / "main.swift"), "-o", str(cls.binary)],
                       check=True, capture_output=True, timeout=300)

    def run_sql(self, sql, params=()):
        request, result = self.dir / "request.json", self.dir / "result.json"
        request.write_text(json.dumps({"sql": sql, "params": list(params)}))
        result.unlink(missing_ok=True)
        subprocess.run([str(self.binary), "--memory", "--request", str(request), "--result", str(result)],
                       capture_output=True, timeout=30)
        return json.loads(result.read_text())

    def blob(self, data: bytes) -> dict:
        return {"blob": base64.b64encode(data).decode()}

    def test_text_comes_from_text_or_the_archived_body(self) -> None:
        long_text = "ğüşiöç " * 40
        answer = self.run_sql("SELECT mm_text(?, NULL), mm_text(NULL, ?), mm_text('', ?), mm_text(NULL, ?)",
                              ["düz metin", self.blob(archived("merhaba ✓")), self.blob(archived(long_text)),
                               self.blob(b"no string here")])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([["düz metin", "merhaba ✓", long_text, None]], answer["rows"])

    def test_fold_matches_case_accents_and_dotless_i(self) -> None:
        answer = self.run_sql("SELECT mm_fold('Akşam ÇAY'), mm_fold('İstanbul ılık'), "
                              "instr(mm_fold('Bugün ÇAY var'), mm_fold('cay')) > 0")
        self.assertEqual([["aksam cay", "istanbul ilik", 1]], answer["rows"])

    def test_writes_are_refused(self) -> None:
        self.assertEqual("not_read_only", self.run_sql("CREATE TABLE x (a)")["error"])
        self.assertEqual("one_statement", self.run_sql("SELECT 1; DROP TABLE message")["error"])
        self.assertTrue(self.run_sql("SELECT 1;  ")["ok"])


class AdapterTests(ReaderFixture):
    settings = SimpleNamespace(allow_shell=True)

    def call(self, **kwargs):
        return app_adapters.mac_app(self.settings, app="Messages", **kwargs)

    def test_read_and_search_are_read_actions_of_the_messages_adapter(self) -> None:
        self.fake.add(1, "merhaba dünya", when=apple_ns(2026, 10, 1))
        capabilities = self.call(action="capabilities")
        self.assertIn("read_messages", capabilities["actions"])
        self.assertIn("search_messages", capabilities["actions"])
        self.assertTrue(self.call(action="read_messages", item_id="any;-;+905551112233")["ok"])
        found = self.call(action="search_messages", query="dünya")
        self.assertEqual(1, found["count"])
        self.assertTrue(found["read_only"])

    def test_full_disk_access_missing_says_so(self) -> None:
        with patch.object(messages_reader, "_RUNNER", return_value={"ok": False, "error": "full_disk_access"}):
            result = self.call(action="read_messages", item_id="any;-;+905551112233")
        self.assertFalse(result["ok"])
        self.assertEqual("MESSAGES_FULL_DISK_ACCESS_REQUIRED", result["reason_code"])
        self.assertIn("Full Disk Access", result["error"])

    def test_an_excluded_messages_app_is_never_read(self) -> None:
        rules = {**privacy_policy.policy(), "excluded_apps": ["messages"]}
        with patch.object(privacy_policy, "policy", return_value=rules), \
                patch.object(messages_reader, "_RUNNER", side_effect=AssertionError("read despite exclusion")):
            result = self.call(action="search_messages", query="dünya")
        self.assertFalse(result["ok"])
        self.assertEqual("privacy_excluded_app", result["reason_code"])

    def test_access_reports_each_state(self) -> None:
        with patch.object(messages_reader, "reader_app", return_value=None):
            self.assertEqual("reader_missing", messages_reader.access()["state"])
        with patch.object(messages_reader, "reader_app", return_value=Path("/x")):
            self.assertEqual("granted", messages_reader.access()["state"])
            with patch.object(messages_reader, "_RUNNER", return_value={"ok": False, "error": "full_disk_access"}):
                self.assertEqual("needs_full_disk_access", messages_reader.access()["state"])


class ReaderInstallTests(unittest.TestCase):
    """The helper runs from a copy outside Mac MCP.app, where macOS judges it as itself."""

    def fake_app(self, root: Path, version: str) -> Path:
        app = root / messages_reader.READER_NAME
        for part, data in (("Contents/Info.plist", version), ("Contents/MacOS/MacMCPMessagesReader", "bin" + version),
                           ("Contents/_CodeSignature/CodeResources", "sig" + version)):
            (app / part).parent.mkdir(parents=True, exist_ok=True)
            (app / part).write_text(data)
        return app

    def test_copied_once_refreshed_when_the_app_ships_a_new_one(self) -> None:
        root = Path(tempfile.mkdtemp())
        bundled = self.fake_app(root / "bundle", "1")
        target = root / "state" / "helpers" / messages_reader.READER_NAME
        with patch.object(messages_reader, "bundled_reader", return_value=bundled), \
                patch.object(messages_reader, "installed_reader", return_value=target), \
                patch.dict("os.environ", {"MAC_MCP_MESSAGES_READER": ""}):
            self.assertEqual(target, messages_reader.reader_app())
            self.assertEqual("bin1", (target / "Contents/MacOS/MacMCPMessagesReader").read_text())
            with patch.object(messages_reader, "_install") as install:
                messages_reader.reader_app()
            install.assert_not_called()  # unchanged: no copy
            self.fake_app(root / "bundle", "2")
            messages_reader.reader_app()
            self.assertEqual("bin2", (target / "Contents/MacOS/MacMCPMessagesReader").read_text())
        self.assertNotIn("Contents/Helpers", str(target))

    def test_no_app_and_no_copy_means_missing(self) -> None:
        target = Path(tempfile.mkdtemp()) / "helpers" / messages_reader.READER_NAME
        with patch.object(messages_reader, "bundled_reader", return_value=None), \
                patch.object(messages_reader, "installed_reader", return_value=target), \
                patch.dict("os.environ", {"MAC_MCP_MESSAGES_READER": ""}):
            self.assertIsNone(messages_reader.reader_app())


class ReaderHelperSourceTests(unittest.TestCase):
    def test_helper_opens_only_chat_db_read_only_and_runs_only_read_only_statements(self) -> None:
        source = (ROOT / "menu_app" / "MessagesReader" / "main.swift").read_text(encoding="utf-8")
        self.assertIn('"Library/Messages/chat.db"', source)
        self.assertIn("SQLITE_OPEN_READONLY", source)
        self.assertIn("?mode=ro", source)
        self.assertIn("sqlite3_stmt_readonly(statement) == 0", source)
        self.assertNotIn("--database", source)
        script = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('Helpers/Mac MCP Messages Reader.app', script)
        self.assertIn('"${SCRIPT_DIR}/MessagesReader/main.swift"', script)
        self.assertIn('"${SCRIPT_DIR}/Sources/MessagesAccessSection.swift"', script)

    def test_policy_and_plans_treat_both_actions_as_reads(self) -> None:
        from mcp_server import computer_plan, policy

        for action in ("read_messages", "search_messages"):
            self.assertIn(action, app_adapters._READ_ACTIONS)
            self.assertIn(action, computer_plan._READ_ONLY_MAC_APP_ACTIONS)
            self.assertNotIn("EXTERNAL_SIDE_EFFECT", str(policy._mac_app_risk({"action": action})))


if __name__ == "__main__":
    unittest.main()
