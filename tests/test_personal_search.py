"""#67: one search across the owner's own sources.

Documents had knowledge_search and each app its own mac_app search, but no single query covered them.
personal_search asks every chosen source at once and returns one list in one shape; a source that is
excluded in Privacy, closed or failing is reported while the rest still answer, pages never repeat, and
date bounds reach the sources that have dates (Mail gained optional received-date bounds for this).
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import app_adapters, personal_search as ps
from mcp_server.app_adapter_core import AppAdapterError
from mcp_server.policy import RISK_REGISTRY, resolve_risk
from mcp_server.privacy_policy import PAUSED_TOOLS


def rows(source: str, n: int, **extra):
    return [{"title": f"{source} {i}", "id": f"{source}-{i}", **extra} for i in range(n)]


class PersonalSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = {}
        excluded = patch.object(ps.privacy_policy, "check_app", side_effect=self.privacy)
        excluded.start()
        self.addCleanup(excluded.stop)
        self.excluded: set = set()
        self.fakes = {source: self.fake(source, 3) for source in ps.SOURCES}
        searches = patch.dict(ps._SEARCHES, self.fakes)
        searches.start()
        self.addCleanup(searches.stop)

    def privacy(self, name, bundle):
        if name in self.excluded:
            raise ps.privacy_policy.PrivacyBlocked("privacy_excluded_app", f"the owner excluded {name}")

    def fake(self, source, n, error=None):
        def search(query, limit, date_from=None, date_to=None):
            self.calls[source] = {"query": query, "limit": limit, "date_from": date_from, "date_to": date_to}
            if error:
                raise error
            return rows(source, min(n, limit))
        return search

    def test_one_query_returns_every_source_in_one_shape(self) -> None:
        result = ps.personal_search("invoice", limit=30)
        self.assertEqual(21, result["count"])
        self.assertEqual(list(ps.SOURCES), [row["source"] for row in result["results"][:7]])  # interleaved
        self.assertEqual({"source", "title", "id"}, set(result["results"][0]))
        self.assertTrue(all(entry == {"ok": True, "found": 3} for entry in result["sources"].values()))
        self.assertEqual({"invoice"}, {call["query"] for call in self.calls.values()})
        self.assertFalse(result["partial"])
        self.assertIsNone(result["next_cursor"])

    def test_a_failing_or_excluded_source_does_not_stop_the_others(self) -> None:
        self.excluded = {"Mail"}
        ps._SEARCHES["calendar"] = self.fake("calendar", 3, error=AppAdapterError("CALENDAR_FIND_FAILED", "Calendar timed out", retryable=True))
        ps._SEARCHES["documents"] = self.fake("documents", 0, error=ps._Unavailable("no_folders", "No document folders"))
        result = ps.personal_search("invoice")
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertNotIn("mail", self.calls)  # an excluded source is never read
        self.assertEqual({"ok": False, "reason": "excluded_in_privacy", "message": "Excluded in Settings › Privacy."},
                         result["sources"]["mail"])
        self.assertEqual(("calendar_find_failed", True),
                         (result["sources"]["calendar"]["reason"], result["sources"]["calendar"]["retryable"]))
        self.assertEqual("no_folders", result["sources"]["documents"]["reason"])
        self.assertEqual({"notes", "reminders", "contacts", "messages"}, {row["source"] for row in result["results"]})

    def test_sources_and_date_bounds_reach_only_the_chosen_sources(self) -> None:
        result = ps.personal_search("plan", sources=["messages", "notes"], date_from="2026-10-01", date_to="2026-10-21")
        self.assertEqual({"notes", "messages"}, set(self.calls))
        self.assertEqual("2026-10-01", self.calls["messages"]["date_from"])
        self.assertEqual("not_applied", result["sources"]["notes"]["date_filter"])  # notes carry no date
        self.assertNotIn("date_filter", result["sources"]["messages"])
        self.assertEqual(["notes", "messages"], list(result["sources"]))
        for bad in ({"sources": ["photos"]}, {"date_from": "yesterday"}):
            with self.subTest(bad=bad), self.assertRaises(HTTPException):
                ps.personal_search("plan", **bad)
        with self.assertRaises(HTTPException):
            ps.personal_search("x")

    def test_pages_cover_everything_once(self) -> None:
        for source in ps.SOURCES:
            ps._SEARCHES[source] = self.fake(source, 8)
        seen, cursor = [], None
        for _ in range(10):
            page = ps.personal_search("tax", limit=20, cursor=cursor)
            seen.extend((row["source"], row["id"]) for row in page["results"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(56, len(seen))
        self.assertEqual(len(seen), len(set(seen)))
        with self.assertRaises(HTTPException):
            ps.personal_search("other words", cursor=ps._cursor(20, "not-this-search"))
        with self.assertRaises(HTTPException):
            ps.personal_search("tax", cursor="garbage!")

    def test_rows_are_shaped_from_each_adapter(self) -> None:
        patch.stopall()
        with patch.object(app_adapters, "_mail_find", return_value={"messages": [
                {"message_id": "m1", "subject": "Invoice 42", "sender": "a@b.com", "date_received": "21 Ekim 2026"}]}) as mail:
            out = ps._mail("invoice", 5, date_from="2026-10-01", date_to=None)
        self.assertEqual("2026-10-01", mail.call_args.kwargs["date_from"])
        self.assertEqual({"title": "Invoice 42", "detail": "a@b.com", "when": "21 Ekim 2026", "id": "m1",
                          "open": {"tool": "mac_app", "app": "Mail", "action": "open_message", "item_id": "m1"}}, out[0])
        from mcp_server import messages_reader
        with patch.object(messages_reader, "search_messages", return_value={"messages": [
                {"message_id": "g1", "chat_id": "c1", "chat": "Ali", "sender": "me", "date": "2026-10-20T10:00:00",
                 "text": "my password is hunter2-secret-token sk-abcdefghijklmnopqrstuvwx " + "x" * 300}]}):
            message = ps._messages("password", 5, date_from=None, date_to=None)[0]
        self.assertLessEqual(len(message["snippet"]), ps.SNIPPET_CHARS)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", message["snippet"])
        self.assertEqual("read_messages", message["open"]["action"])
        from mcp_server import knowledge
        with patch.object(knowledge, "knowledge_search", return_value={"folders": [], "results": []}):
            with self.assertRaises(ps._Unavailable):
                ps._documents("tax", 5, date_from=None, date_to=None)

    def test_policy_reads_only_and_pauses_with_observation(self) -> None:
        self.assertIn("personal_search", RISK_REGISTRY)
        risk = resolve_risk("personal_search", {"query": "x"})[1]
        self.assertFalse(risk.destructive)
        self.assertIn("personal_search", PAUSED_TOOLS)


class MailDateBoundsTests(unittest.TestCase):
    def test_mail_search_gains_optional_received_date_bounds(self) -> None:
        scripts = []
        with patch.object(app_adapters, "_run", side_effect=lambda script, **_: scripts.append(script) or ""):
            app_adapters._mail_find("invoice", None, mailbox=None, exact=False, limit=5, timeout_s=5)
            app_adapters._mail_find("invoice", None, mailbox=None, exact=False, limit=5, timeout_s=5,
                                    date_from="2026-10-01", date_to="2026-10-21")
        self.assertNotIn("date received ≥", scripts[0])  # unchanged without bounds
        self.assertIn("date received ≥ fromDate", scripts[1])
        self.assertIn("date received ≤ toDate", scripts[1])
        self.assertIn("set year of fromDate to 2026", scripts[1])
        with tempfile.NamedTemporaryFile(suffix=".scpt") as out:
            compiled = subprocess.run(["/usr/bin/osacompile", "-o", out.name, "-e", scripts[1]],
                                      capture_output=True, text=True)
        self.assertEqual(0, compiled.returncode, compiled.stderr)


if __name__ == "__main__":
    unittest.main()
