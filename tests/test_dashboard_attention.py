"""#67: dashboard health strip and read-only "Needs your attention"; #68: per-agent activity timeline."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings
from mcp_server.security_context import SecurityContextManager

TOKEN = "attention-dashboard-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}
DASHBOARD = Path(__file__).resolve().parents[1] / "mcp_server" / "dashboard"


class AttentionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-attention-")
        self.telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=100,
                                          usage_enabled=False, async_writes=False)
        self.security = SecurityContextManager(pending_ttl_s=60)
        app = Starlette(routes=create_dashboard_routes(self.telemetry, load_settings(), TOKEN,
                                                       security_context=self.security))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def pending(self, tool: str, fingerprint: str, *, expires_in: float = 60.0):
        state = self.security.touch("key-" + fingerprint, "session-" + fingerprint)
        with self.security._lock:
            item = self.security._pending_for_locked(state, tool, fingerprint, "web_to_host", f"{tool}: target")
            item.expires_at = time.time() + expires_in
        return item

    def test_requires_the_dashboard_token(self) -> None:
        self.assertEqual(401, self.client.get("/dashboard/api/attention").status_code)

    def test_nothing_pending_and_nothing_blocked(self) -> None:
        data = self.client.get("/dashboard/api/attention", headers=AUTH).json()
        self.assertEqual([], data["pending"])
        self.assertEqual(0, data["blocked_24h"])
        self.assertIn("Allow Once / Block", data["answer_where"])

    def test_pending_requests_are_listed_newest_first_and_expired_ones_drop_out(self) -> None:
        self.pending("run_command", "fp-old")
        newest = self.pending("write_file", "fp-new")
        self.pending("delete_path", "fp-expired", expires_in=-1)
        data = self.client.get("/dashboard/api/attention", headers=AUTH).json()
        self.assertEqual(["write_file", "run_command"], [row["tool"] for row in data["pending"]])
        row = data["pending"][0]
        self.assertEqual(newest.request_id, row["request_id"])
        self.assertEqual({"request_id", "tool", "origin", "tab_title", "reason_code", "target_summary",
                          "created_at", "expires_at"}, set(row))
        self.assertNotIn("session_id", row)  # read-only: nothing here can be used to grant

    def test_an_open_owner_question_is_listed_until_its_dialog_closes(self) -> None:
        from mcp_server.tools_interactive import owner_question

        self.pending("run_command", "fp-security")
        with owner_question("browser_upload_artifact", origin="example.com", summary="report.pdf"):
            rows = self.client.get("/dashboard/api/attention", headers=AUTH).json()["pending"]
            self.assertEqual(["browser_upload_artifact", "run_command"], [row["tool"] for row in rows])
            self.assertEqual(("example.com", "report.pdf"), (rows[0]["origin"], rows[0]["target_summary"]))
            self.assertEqual(set(rows[1]), set(rows[0]))
        rows = self.client.get("/dashboard/api/attention", headers=AUTH).json()["pending"]
        self.assertEqual(["run_command"], [row["tool"] for row in rows])

    def test_recently_blocked_calls_are_counted_and_summarised(self) -> None:
        for index in range(7):
            self.telemetry.record_security_event(
                session_id="s", event_type="SERVER_RISK_BLOCK", tool="run_command", tool_class="terminal",
                origin=None, decision="deny", reason_code="server_risk_approval_required", profile="trusted",
                actor="mcp", agent_id=None, target_summary=f"terminal:run_command #{index}",
            )
        self.telemetry.record_security_event(
            session_id="s", event_type="WEB_TO_HOST_APPROVAL", tool="run_command", tool_class="terminal",
            origin=None, decision="grant", reason_code="local_user_one_shot_grant", profile=None,
            actor="dashboard-local-user", agent_id=None, target_summary="terminal:run_command",
        )
        # The card must not hydrate and filter an arbitrary event page.  Its
        # summary query counts every denial and reads only the five visible rows.
        with patch.object(self.telemetry, "query_security_events", side_effect=AssertionError("unbounded read")):
            data = self.client.get("/dashboard/api/attention", headers=AUTH).json()
        self.assertEqual(7, data["blocked_24h"])
        self.assertEqual(5, len(data["blocked_recent"]))
        self.assertEqual("SERVER_RISK_BLOCK", data["blocked_recent"][0]["event_type"])

    def test_dashboard_offers_no_approval_action(self) -> None:
        script = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
        start = script.index("async function refreshAttention")
        attention = script[start:script.index("\n  }\n", start)]
        self.assertNotIn("postJSON", attention)
        self.assertNotIn("/security/escalate", script)


class AgentTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-timeline-")
        self.telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=100,
                                          usage_enabled=False, async_writes=False)
        self.client = TestClient(Starlette(routes=create_dashboard_routes(self.telemetry, load_settings(), TOKEN)))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, tool: str, agent_id, team_id=None, error: str | None = None) -> str:
        event_id = self.telemetry.start_call("mcp", tool, {"path": "/tmp/x"},
                                             metadata={"agent_id": agent_id, "team_id": team_id})
        self.telemetry.finish_call(event_id, result=None if error else {"ok": True}, error=error)
        return event_id

    def test_events_can_be_filtered_by_agent_and_team(self) -> None:
        self.call("read_file", "agt_a", "team_1")
        self.call("write_file", "agt_a", "team_1", error="permission denied")
        self.call("run_command", "agt_b", "team_2")
        self.call("browser_observe", None)
        mine = self.client.get("/dashboard/api/events?agent_id=agt_a&hours=24", headers=AUTH).json()["events"]
        self.assertEqual({"read_file", "write_file"}, {row["tool"] for row in mine})
        self.assertEqual({"success", "error"}, {row["status"] for row in mine})
        team = self.client.get("/dashboard/api/events?team_id=team_2", headers=AUTH).json()["events"]
        self.assertEqual(["run_command"], [row["tool"] for row in team])
        empty = self.client.get("/dashboard/api/events?agent_id=agt_none", headers=AUTH).json()["events"]
        self.assertEqual([], empty)
        everything = self.client.get("/dashboard/api/events", headers=AUTH).json()["events"]
        self.assertEqual(4, len(everything))

    def test_agent_cards_have_a_keyboard_reachable_activity_button(self) -> None:
        script = (DASHBOARD / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('<button class="agent-activity" type="button" data-agent-activity=', script)
        self.assertIn("/dashboard/api/events?hours=168&limit=100&agent_id=", script)
        # Only completed is green; cancelled and other end states are neutral.
        self.assertIn('status === "completed" ? "" : "neutral"', script)
        self.assertIn(".status-badge.neutral", (DASHBOARD / "dashboard.css").read_text(encoding="utf-8"))
        # A call opened from the agent's activity offers a way back to that list.
        self.assertIn("els.drawerBack.hidden = !fromTimeline", script)
        self.assertIn("await openAgentDrawer(state.timelineAgent)", script)
        self.assertIn('id="drawerBack"', (DASHBOARD / "index.html").read_text(encoding="utf-8"))
        page = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        for element in ("timelineSection", "agentSummarySection", "healthRow", "attentionList"):
            self.assertIn(f'id="{element}"', page)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_dashboard_script_parses(self) -> None:
        proc = subprocess.run(["node", "--check", str(DASHBOARD / "dashboard.js")], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)


if __name__ == "__main__":
    unittest.main()
