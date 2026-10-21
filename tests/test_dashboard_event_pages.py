"""#64: the dashboard's activity history reaches calls older than its first page.

The events API answered one page (limit capped at 500, no cursor) and the dashboard always asked for
the newest 160: with more calls in the chosen range the older ones could not be seen, and nothing
said the list was cut.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {TOKEN}"}
DASHBOARD = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"


class DashboardEventPagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=1000)
        for index in range(25):
            tool = "read_file" if index % 2 else "write_file"
            event_id = self.manager.start_call("mcp", tool, {"index": index})
            self.manager.finish_call(event_id, result={"ok": True})
        # Several calls in the same instant: the cursor must still not skip or repeat one.
        with sqlite3.connect(self.manager.db_path) as conn:
            conn.execute("UPDATE tool_events SET timestamp = ? WHERE rowid IN (SELECT rowid FROM tool_events LIMIT 6)",
                         (time.time() - 60,))
        self.client = TestClient(Starlette(routes=create_dashboard_routes(self.manager, load_settings(), TOKEN)))

    def pages(self, query: str) -> list:
        seen, cursor = [], None
        for _ in range(20):
            url = f"/dashboard/api/events?hours=24&limit=7{query}" + (f"&before={cursor}" if cursor else "")
            answer = self.client.get(url, headers=AUTH).json()
            seen.append([event["event_id"] for event in answer["events"]])
            if not answer["has_more"]:
                self.assertIsNone(answer["next_cursor"])
                return seen
            cursor = answer["next_cursor"]
        self.fail("paging did not end")

    def test_every_call_is_seen_once_across_pages(self) -> None:
        pages = self.pages("")
        flat = [event_id for page in pages for event_id in page]
        self.assertEqual(25, len(flat))
        self.assertEqual(25, len(set(flat)))
        self.assertEqual([7, 7, 7, 4], [len(page) for page in pages])

    def test_filters_hold_across_pages(self) -> None:
        flat = [event_id for page in self.pages("&tool=write_file") for event_id in page]
        self.assertEqual(13, len(flat))
        self.assertEqual(13, len(set(flat)))

    def test_a_bad_cursor_reads_as_the_first_page(self) -> None:
        first = self.client.get("/dashboard/api/events?limit=5", headers=AUTH).json()
        odd = self.client.get("/dashboard/api/events?limit=5&before=nonsense", headers=AUTH).json()
        self.assertEqual([e["event_id"] for e in first["events"]], [e["event_id"] for e in odd["events"]])

    def test_the_dashboard_can_load_older_calls(self) -> None:
        script = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
        page = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="events-more"', page)
        self.assertIn("async function loadOlderEvents()", script)
        self.assertIn('params.set("before", state.nextCursor)', script)
        self.assertIn("state.older = []", script)


if __name__ == "__main__":
    unittest.main()
