"""A team's whole conversation for its lead, its agents and the owner (#104)."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import team_messages as tm
from mcp_server import tools_agents as agents
from mcp_server import tools_team_messages
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

A1 = tm.Caller(member_id="agent:agt_1", label="task research", agent_id="agt_1", team_id="team_x")
A2 = tm.Caller(member_id="agent:agt_2", label="task writer", agent_id="agt_2", team_id="team_x")
OUTSIDER = tm.Caller(member_id="agent:agt_9", label="task other", agent_id="agt_9", team_id="team_y")
LEAD = tm.Caller(member_id="session:lead", label="Claude Code")
ROOM = tm.team_room("team_x")
TOKEN = "team-messages-dashboard-token-0123456789"


class HistoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = tm.Store(Path(tempfile.mkdtemp()) / "team_messages.sqlite3")
        self.store.set_lead(ROOM, LEAD.member_id, tasks=["research", "writer"])
        self.store.join(ROOM, LEAD.member_id, label=LEAD.label, kind="session")
        self.store.join(ROOM, A1.member_id, label=A1.label, kind="agent", task_id="research")
        self.store.join(ROOM, A2.member_id, label=A2.label, kind="agent", task_id="writer")
        self.store.join(tm.team_room("team_y"), OUTSIDER.member_id, label=OUTSIDER.label, kind="agent", task_id="other")
        patcher = patch.object(tm, "_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, caller: tm.Caller, **kwargs):
        token = tm.set_caller(caller)
        try:
            return tools_team_messages.team_messages(**kwargs)
        finally:
            tm.reset_caller(token)


class StoreHistoryTests(HistoryFixture):
    def test_everything_in_order_with_who_saw_and_opened_it(self) -> None:
        first = self.store.send(ROOM, A1, "schema is v3", to="writer")
        self.store.send(ROOM, LEAD, "everyone: stop at noon")
        self.store.claim(A2.member_id, channel="hook")
        self.store.inbox(A2.member_id, unread_only=True)
        history = self.store.history(ROOM)
        self.assertEqual(["schema is v3", "everyone: stop at noon"], [m["body"] for m in history["messages"]])
        direct = history["messages"][0]
        self.assertEqual((first["message_id"], "task writer"), (direct["message_id"], direct["to_label"]))
        self.assertEqual("task research", direct["from"])
        self.assertEqual(["agent:agt_2"], [d["member"] for d in direct["deliveries"]])
        self.assertEqual("hook", direct["deliveries"][0]["channel"])
        self.assertIsNotNone(direct["deliveries"][0]["read_at"])
        broadcast = history["messages"][1]
        self.assertEqual("everyone", broadcast["to_label"])
        self.assertTrue(broadcast["from_lead"])

    def test_reading_history_marks_nothing(self) -> None:
        self.store.send(ROOM, A1, "unseen", to="writer")
        self.store.history(ROOM)
        self.assertEqual(["unseen"], [m["body"] for m in self.store.claim(A2.member_id, channel="hook")])

    def test_pages_by_after(self) -> None:
        for index in range(5):
            self.store.send(ROOM, LEAD, f"note {index}")
        page = self.store.history(ROOM, limit=2)
        self.assertTrue(page["has_more"])
        rest = self.store.history(ROOM, after=page["next_after"], limit=10)
        self.assertEqual(["note 2", "note 3", "note 4"], [m["body"] for m in rest["messages"]])
        self.assertFalse(rest["has_more"])
        self.assertIsNone(rest["next_after"])

    def test_messages_to_finished_tasks_are_marked_and_their_copies_shown(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        self.store.send(ROOM, A2, "late", to="research")
        messages = self.store.history(ROOM)["messages"]
        self.assertEqual("recipient_finished", messages[0]["delivery_note"])
        self.assertEqual(messages[0]["message_id"], messages[1]["forwarded_from"])
        self.assertEqual("lead", messages[1]["to_label"])


class ToolHistoryTests(HistoryFixture):
    def test_lead_and_team_agents_read_it(self) -> None:
        self.store.send(ROOM, A1, "hello writer", to="writer")
        for caller, kwargs in ((LEAD, {"team_id": "team_x"}), (A1, {}), (A2, {})):
            result = self.call(caller, action="history", **kwargs)
            self.assertTrue(result["ok"], result)
            self.assertEqual(["hello writer"], [m["body"] for m in result["messages"]])

    def test_another_teams_agent_cannot(self) -> None:
        self.store.send(ROOM, A1, "private to the team", to="writer")
        result = self.call(OUTSIDER, action="history", team_id="team_x")
        self.assertFalse(result["ok"])
        self.assertEqual("not_a_member", result["error"])
        own = self.call(OUTSIDER, action="history")
        self.assertEqual([], own["messages"])  # its own team has no messages

    def test_history_is_in_the_description(self) -> None:
        self.assertIn("history", tools_team_messages.ACTIONS)
        self.assertIn("history (the whole team conversation", tools_team_messages.DESCRIPTION)


class TeamMessagesEndpointTests(HistoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(Path(self.temp.name) / "state")}, clear=False)
        self.env.start()
        self.dirs = [patch.object(agents, "AGENTS_DIR", Path(self.temp.name) / "agents"),
                     patch.object(agents, "TEAMS_DIR", Path(self.temp.name) / "teams")]
        for item in self.dirs:
            item.start()
        agents._write_team("team_x", {"team_id": "team_x", "messaging": {"enabled": True}, "tasks": [],
                                      "dependency_reads": {"writer->research": {"reader_task": "writer",
                                                                                "target_task": "research", "count": 2}}})
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3", max_events=10)
        app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN))
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        for item in reversed(self.dirs):
            item.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_requires_the_owner_token(self) -> None:
        response = self.client.get("/dashboard/api/agents/teams/team_x/messages")
        self.assertIn(response.status_code, {401, 403})

    def test_owner_gets_messages_counts_and_reads(self) -> None:
        self.store.send(ROOM, A1, "for the writer", to="writer")
        response = self.client.get("/dashboard/api/agents/teams/team_x/messages?limit=500",
                                   headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(["for the writer"], [m["body"] for m in body["messages"]])
        self.assertEqual({"sent": 1, "to_finished_recipients": 0, "forwarded": 0}, body["message_counts"])
        self.assertEqual([{"reader_task": "writer", "target_task": "research", "count": 2}], body["dependency_reads"])

    def test_bad_and_unknown_team_ids(self) -> None:
        auth = {"authorization": f"Bearer {TOKEN}"}
        self.assertEqual(400, self.client.get("/dashboard/api/agents/teams/bad-id/messages", headers=auth).status_code)
        self.assertEqual(404, self.client.get("/dashboard/api/agents/teams/team_none/messages", headers=auth).status_code)


if __name__ == "__main__":
    unittest.main()
