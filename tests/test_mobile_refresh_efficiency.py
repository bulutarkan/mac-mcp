from __future__ import annotations

import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from tests import test_mobile_dashboard as mobile_fixture

ROOT = Path(__file__).resolve().parents[1] / "mcp_server"
AGENTS = {"ok": True, "count": 2, "agents": [
    {"agent_id": "agt_run", "status": "running", "title": "Running"},
    {"agent_id": "agt_done", "status": "completed", "title": "Done", "ended_at": time.time() - 60},
]}
REPLY = {"status": "cancelled", "cancellation": {"state": "confirmed"}}


class MobileAgentSnapshotTests(unittest.TestCase):
    def paired_phone(self, td: str) -> TestClient:
        helper = mobile_fixture.MobileDashboardTests()
        app, _telemetry, _store = helper.make_app(Path(td))
        return helper.paired_phone(app)

    def test_one_refresh_lists_agents_once_and_keeps_both_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            phone = self.paired_phone(td)
            with patch("mcp_server.mobile_routes.list_agents", return_value=AGENTS) as listing:
                status = phone.get("/mobile/api/status").json()
                agents = phone.get("/mobile/api/agents").json()
            self.assertEqual(1, listing.call_count)
            self.assertEqual((1, "ok"), (status["active_agents"], status["sources"]["agents"]))
            self.assertEqual((True, 1, 2), (agents["available"], agents["active_count"], agents["count"]))

    def test_parallel_status_and_agents_requests_share_one_listing(self) -> None:
        def slow_listing(*_args, **_kwargs):
            time.sleep(0.2)
            return AGENTS

        with tempfile.TemporaryDirectory() as td:
            phone = self.paired_phone(td)
            results: dict[str, dict] = {}
            with patch("mcp_server.mobile_routes.list_agents", side_effect=slow_listing) as listing:
                threads = [
                    threading.Thread(target=lambda path=path: results.__setitem__(path, phone.get(path).json()))
                    for path in ("/mobile/api/status", "/mobile/api/agents")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(1, listing.call_count)
            self.assertEqual(1, results["/mobile/api/status"]["active_agents"])
            self.assertEqual(1, results["/mobile/api/agents"]["active_count"])

    def test_snapshot_expires_and_a_stop_forgets_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            phone = self.paired_phone(td)
            with patch("mcp_server.mobile_routes.list_agents", return_value=AGENTS) as listing, \
                 patch("mcp_server.mobile_routes.agent_action", return_value=REPLY):
                phone.get("/mobile/api/status")
                phone.post("/mobile/api/agents/cancel", json={"agent_id": "agt_run"},
                           headers={"origin": "https://testserver"})
                phone.get("/mobile/api/agents")
                self.assertEqual(2, listing.call_count, "the refresh after a stop must list agents again")
                with patch("mcp_server.mobile_routes.time.monotonic", return_value=time.monotonic() + 5):
                    phone.get("/mobile/api/status")
                self.assertEqual(3, listing.call_count, "an old snapshot is never reused")

    def test_failed_listing_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            phone = self.paired_phone(td)
            with patch("mcp_server.mobile_routes.list_agents", side_effect=RuntimeError("db locked")):
                self.assertEqual("unavailable", phone.get("/mobile/api/status").json()["sources"]["agents"])
            with patch("mcp_server.mobile_routes.list_agents", return_value=AGENTS) as listing:
                self.assertTrue(phone.get("/mobile/api/agents").json()["available"])
            self.assertEqual(1, listing.call_count)


class RefreshSchedulingSourceTests(unittest.TestCase):
    def test_mobile_pauses_when_hidden_and_never_overlaps(self) -> None:
        js = (ROOT / "mobile" / "mobile.js").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(refresh", js)
        self.assertIn('document.addEventListener("visibilitychange"', js)
        self.assertIn("if (!state.polling || document.hidden) return;", js)
        self.assertIn("if (state.refreshing) { state.refreshQueued = true; return state.refreshing; }", js)
        self.assertIn("state.activeWork ? REFRESH_ACTIVE_MS : REFRESH_IDLE_MS", js)

    def test_dashboard_polls_are_single_flight_and_adaptive(self) -> None:
        js = (ROOT / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn("window.setInterval(refreshAgents", js)
        self.assertNotIn("window.setInterval(refreshSummary", js)
        for name in ("refreshSummary", "refreshAgents", "refreshEvents", "refreshAttention"):
            self.assertIn(f"{name} = singleFlight({name});", js)
        self.assertIn("pollAdaptive(refreshAgents, 2200, 10000);", js)
        self.assertIn("document.hidden ? null : window.setTimeout(poll.run", js)


class SharedVisualSystemTests(unittest.TestCase):
    @staticmethod
    def token_block(css: str) -> str:
        match = re.search(r"/\* Mac MCP shared tokens.*?/\* End shared tokens\. \*/", css, re.S)
        assert match, "shared token block missing"
        return match.group(0)

    def test_dashboard_and_mobile_share_tokens_and_mark(self) -> None:
        dashboard_css = (ROOT / "dashboard" / "dashboard.css").read_text(encoding="utf-8")
        mobile_css = (ROOT / "mobile" / "mobile.css").read_text(encoding="utf-8")
        self.assertEqual(self.token_block(dashboard_css), self.token_block(mobile_css))
        # The same monogram, byte for byte (#204).
        mark = re.search(r'<svg viewBox="0 0 64 64">.*?</svg>', (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")).group(0)
        self.assertIn('d="M14 49 V15 L32 33 L50 15 V29"', mark)
        mobile_html = (ROOT / "mobile" / "index.html").read_text(encoding="utf-8")
        self.assertIn(mark, mobile_html)
        self.assertNotIn('<div class="mark">M</div>', mobile_html)
        # Dark only, and the old near-black palette is gone from the phone.
        self.assertNotIn("prefers-color-scheme: light", mobile_css)
        self.assertNotIn("#0a0a0b", mobile_css + mobile_html)

    def test_mobile_secondary_text_keeps_readable_contrast(self) -> None:
        mobile_css = (ROOT / "mobile" / "mobile.css").read_text(encoding="utf-8")
        # Secondary text uses --muted (about 6:1 on the card surface); --muted-deep is
        # reserved for separators and placeholders.
        self.assertNotIn("color:var(--soft)", mobile_css)
        text_uses = re.findall(r"(?<![-\w])color:var\(--muted-deep\)", mobile_css)
        self.assertLessEqual(len(text_uses), 2)


if __name__ == "__main__":
    unittest.main()
