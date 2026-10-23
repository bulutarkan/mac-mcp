"""A message to an agent that already finished is never lost silently (#107)."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import team_messages as tm
from mcp_server import tools_team_messages

A1 = tm.Caller(member_id="agent:agt_1", label="task research", agent_id="agt_1", team_id="team_x")
A2 = tm.Caller(member_id="agent:agt_2", label="task writer", agent_id="agt_2", team_id="team_x")
LEAD = tm.Caller(member_id="session:lead", label="Claude Code")
ROOM = tm.team_room("team_x")


class FinishedRecipientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = tm.Store(Path(tempfile.mkdtemp()) / "team_messages.sqlite3")
        self.store.set_lead(ROOM, LEAD.member_id, tasks=["research", "writer", "synthesis"])
        self.store.join(ROOM, LEAD.member_id, label=LEAD.label, kind="session")
        self.store.join(ROOM, A1.member_id, label=A1.label, kind="agent", task_id="research")
        self.store.join(ROOM, A2.member_id, label=A2.label, kind="agent", task_id="writer")
        patcher = patch.object(tm, "_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lead_sees(self) -> list:
        return self.store.claim(LEAD.member_id, channel="mcp_result")

    def test_send_to_a_finished_task_says_so_and_the_lead_gets_a_copy(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        sent = self.store.send(ROOM, A2, "the API is v3, not v2", to="research", kind="correction")
        self.assertEqual("recipient_finished", sent["delivery"])
        self.assertEqual("research", sent["finished_task"])
        self.assertTrue(sent["recipient_finished_at"])
        self.assertEqual(["lead"], sent["forwarded_to"])
        self.assertIn("already finished", sent["warning"])
        copies = self.lead_sees()
        self.assertEqual(1, len(copies))
        self.assertEqual(sent["message_id"], copies[0]["forwarded_from"])
        self.assertEqual("correction", copies[0]["kind"])
        self.assertTrue(copies[0]["body"].startswith("[forwarded: task research's agent had finished] the API is v3"))

    def test_a_finished_member_is_still_known_by_its_id(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        sent = self.store.send(ROOM, A2, "late note", to="agt_1")
        self.assertEqual("recipient_finished", sent["delivery"])
        self.assertEqual(["lead"], sent["forwarded_to"])

    def test_the_lead_writing_to_a_finished_task_is_told_but_not_copied_to_itself(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        sent = self.store.send(ROOM, LEAD, "one more thing", to="research")
        self.assertEqual("recipient_finished", sent["delivery"])
        self.assertEqual([], sent["forwarded_to"])
        self.assertEqual([], self.lead_sees())

    def test_unseen_messages_go_to_the_lead_when_the_agent_finishes(self) -> None:
        sent = self.store.send(ROOM, A2, "use the new file name", to="research")
        self.assertNotIn("delivery", sent)  # research is still working
        passed = self.store.finish_member(ROOM, A1.member_id)
        self.assertEqual([sent["message_id"]], [item["message_id"] for item in passed])
        copies = self.lead_sees()
        self.assertEqual([sent["message_id"]], [item["forwarded_from"] for item in copies])
        self.assertEqual({"sent": 1, "to_finished_recipients": 1, "forwarded": 1}, self.store.room_stats(ROOM))

    def test_seen_messages_are_not_passed_on(self) -> None:
        self.store.send(ROOM, A2, "seen in time", to="research")
        self.store.claim(A1.member_id, channel="hook")
        self.assertEqual([], self.store.finish_member(ROOM, A1.member_id))
        self.assertEqual([], self.lead_sees())

    def test_a_task_that_runs_again_still_gets_the_message(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        self.store.send(ROOM, A2, "for the revision", to="research")
        rerun = tm.Caller(member_id="agent:agt_9", label="task research (revision)", agent_id="agt_9", team_id="team_x")
        self.store.join(ROOM, rerun.member_id, label=rerun.label, kind="agent", task_id="research")
        self.assertEqual(["for the revision"], [m["body"] for m in self.store.claim(rerun.member_id, channel="hook")])

    def test_forwarded_copies_do_not_use_the_senders_budget(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        for index in range(tm.AGENT_MESSAGE_BUDGET):
            sent = self.store.send(ROOM, A2, f"note {index}", to="research")
        self.assertEqual(0, sent["messages_left"])

    def test_dependents_that_have_not_ended_get_a_copy(self) -> None:
        self.store.finish_member(ROOM, A1.member_id)
        caller = tm.set_caller(A2)
        self.addCleanup(tm.reset_caller, caller)
        calls = []
        with patch.object(tm, "_DEPENDENTS", lambda team, task: calls.append((team, task)) or ["synthesis"]):
            result = tools_team_messages.team_messages(action="send", body="numbers changed", to="research")
        self.assertEqual([("team_x", "research")], calls)
        self.assertEqual(["lead", "task:synthesis"], result["sent"]["forwarded_to"])
        self.assertIn("a copy went to the lead; a copy also went to task synthesis", result["warning"])
        synthesis = tm.Caller(member_id="agent:agt_5", label="task synthesis", agent_id="agt_5", team_id="team_x")
        self.store.join(ROOM, synthesis.member_id, label=synthesis.label, kind="agent", task_id="synthesis")
        bodies = [m["body"] for m in self.store.claim(synthesis.member_id, channel="hook")]
        self.assertEqual(["[forwarded: task research's agent had finished] numbers changed"], bodies)

    def test_finish_member_reaches_dependents_too(self) -> None:
        self.store.send(ROOM, A2, "unseen", to="research")
        with patch.object(tm, "_DEPENDENTS", lambda team, task: ["synthesis"]):
            tm.finish_member(ROOM, A1.member_id)
        synthesis = tm.Caller(member_id="agent:agt_6", label="task synthesis", agent_id="agt_6", team_id="team_x")
        self.store.join(ROOM, synthesis.member_id, label=synthesis.label, kind="agent", task_id="synthesis")
        self.assertEqual(1, len(self.store.claim(synthesis.member_id, channel="hook")))

    def test_tools_agents_registers_who_depends_on_a_task(self) -> None:
        from mcp_server import tools_agents

        self.assertIs(tm._DEPENDENTS, tools_agents.unfinished_dependents)

    def test_correction_is_a_kind(self) -> None:
        self.assertIn("correction", tm.KINDS)
        self.assertEqual("correction", self.store.send(ROOM, A2, "fix", to="lead", kind="correction")["kind"])

    def test_an_old_database_gains_the_new_columns(self) -> None:
        import sqlite3

        path = Path(tempfile.mkdtemp()) / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(tm._SCHEMA)
        conn.close()
        store = tm.Store(path)
        store.set_lead(ROOM, LEAD.member_id, tasks=["research"])
        store.join(ROOM, LEAD.member_id, label="lead", kind="session")
        self.assertEqual({"sent": 0, "to_finished_recipients": 0, "forwarded": 0}, store.room_stats(ROOM))


class BridgeFinishTests(unittest.TestCase):
    def test_a_finished_agent_passes_on_what_it_never_saw(self) -> None:
        from mcp_server import agent_message_bridge as bridge_module

        bridge = bridge_module.Bridge(member_id="agent:agt_1", room_id=ROOM, mode="hook", files=[])
        with patch.object(tm, "finish_member") as finish, \
                patch.object(tm.Store, "revoke_hook_tokens"), patch.object(tm.Store, "set_push_file"):
            bridge_module.finish(bridge, terminal=True)
        finish.assert_called_once_with(ROOM, "agent:agt_1")


if __name__ == "__main__":
    unittest.main()
