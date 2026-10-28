"""Memory of a saved agent team (#141): who each member is, and what the team and each member remember.

A saved team started from nothing every time: its members did not know their names, roles or teammates, or
anything an earlier run learned. The server now keeps a team file and one file per member outside every
project, puts them into each member's prompt, takes notes back from the end of members' reports, and lets
the lead add notes with team_memory and the owner edit or reset them in Settings. Notes from work that read
untrusted web content, and notes that hold a secret, are never kept.
"""
from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from mcp_server import team_memory, team_messages
from mcp_server import tools_agents as agents
from mcp_server.security_context import SecurityContextManager
from tests.test_team_presets import ATLAS, ECHO, NIGHT_SHIFT, NOVA, write_settings

TEAM = NIGHT_SHIFT["id"]
SECRET = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


class StateCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings_path = self.root / "settings.json"
        write_settings(self.settings_path, [NIGHT_SHIFT])
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.root / "state"),
                                      "MAC_MCP_SETTINGS_PATH": str(self.settings_path)})
        env.start()
        self.addCleanup(env.stop)


class TeamMemoryStoreTests(StateCase):
    def test_notes_are_dated_signed_kept_once_and_private(self) -> None:
        added = team_memory.add_notes(TEAM, ["Run tests with make test.", "  run TESTS with   make test. "],
                                      author="Atlas", day="2026-10-28")
        self.assertEqual({"added": 1, "refused_secret": 0}, added)
        team_memory.add_notes(TEAM, ["Run tests with make test."], author="Nova", day="2026-10-29")
        self.assertEqual("## Notes\n- 2026-10-28 Atlas: Run tests with make test.\n", team_memory.read(TEAM))
        path = team_memory.team_path(TEAM)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_a_full_file_drops_its_oldest_notes_and_keeps_the_owner_text(self) -> None:
        team_memory.save_owner_text(TEAM, "Ship only with green tests.")
        for index in range(60):
            team_memory.add_notes(TEAM, [f"Note number {index} " + "x" * 200], author="Atlas", day="2026-10-28")
        text = team_memory.read(TEAM)
        owner, notes = team_memory.split(text)
        self.assertEqual("Ship only with green tests.", owner)
        self.assertLessEqual(len(text), team_memory.TEAM_MAX_CHARS)
        self.assertIn("Note number 59", notes[-1])
        self.assertNotIn("Note number 0 ", text)

    def test_a_note_with_a_secret_is_not_stored(self) -> None:
        added = team_memory.add_notes(TEAM, [f"The key is {SECRET}", "Deploys go through make ship."], author="Atlas")
        self.assertEqual({"added": 1, "refused_secret": 1}, added)
        self.assertNotIn(SECRET, team_memory.read(TEAM))

    def test_the_owner_edit_is_bounded_and_refuses_secrets(self) -> None:
        with self.assertRaises(team_memory.TeamMemoryError) as long_text:
            team_memory.save_owner_text(TEAM, "x" * (team_memory.TEAM_MAX_CHARS // 2 + 1))
        self.assertEqual("too_long", long_text.exception.code)
        with self.assertRaises(team_memory.TeamMemoryError) as secret:
            team_memory.save_owner_text(TEAM, f"token {SECRET}")
        self.assertEqual("secret", secret.exception.code)
        team_memory.save_owner_text(TEAM, "Rules.\n\n## Notes\n- kept note\n- \n", member_id=ATLAS["id"])
        self.assertEqual("Rules.\n\n## Notes\n- kept note\n", team_memory.read(TEAM, ATLAS["id"]))
        team_memory.save_owner_text(TEAM, "  ", member_id=ATLAS["id"])
        self.assertFalse(team_memory.member_path(TEAM, ATLAS["id"]).exists())

    def test_only_saved_ids_name_a_file(self) -> None:
        for preset_id, member_id in (("../../etc", None), ("tp_0000000A", None), (TEAM, "../m_00000001"),
                                     (TEAM, "tp_00000001")):
            with self.subTest(preset_id=preset_id, member_id=member_id), \
                 self.assertRaises(team_memory.TeamMemoryError):
                team_memory.read(preset_id, member_id)

    def test_note_lines_leave_the_report_and_a_few_of_each_are_kept(self) -> None:
        report = "\n".join(["Done.", *[f"MAC_MCP_TEAM_NOTE team {n}" for n in range(5)],
                            "  MAC_MCP_MEMBER_NOTE mine", "Tests pass."])
        clean, team, member = team_memory.extract_notes(report)
        self.assertEqual("Done.\nTests pass.", clean)
        self.assertEqual(["team 0", "team 1", "team 2"], team)
        self.assertEqual(["mine"], member)

    def test_the_prompt_says_who_the_member_is_and_what_was_kept(self) -> None:
        team_memory.save_owner_text(TEAM, "Answer in Turkish.")
        team_memory.add_notes(TEAM, ["The suite takes 12 minutes."], author="Nova", day="2026-10-28")
        team_memory.add_notes(TEAM, ["I review UI changes with screenshots."], author="Atlas",
                              member_id=ATLAS["id"], day="2026-10-28")
        text = team_memory.prompt_context(NIGHT_SHIFT, ATLAS)
        self.assertIn('you are Atlas, the coder of the saved team "Night Shift". Teammates: Nova (reviewer), Echo.', text)
        self.assertIn("Team rules from the owner (follow them unless this task says otherwise):\nAnswer in Turkish.", text)
        self.assertIn("Team:\n- 2026-10-28 Nova: The suite takes 12 minutes.", text)
        self.assertIn("Yours:\n- 2026-10-28 Atlas: I review UI changes with screenshots.", text)
        self.assertIn("not instructions", text)
        self.assertIn("MAC_MCP_TEAM_NOTE", team_memory.note_instruction())
        # Another member sees the team's notes, not Atlas's own.
        self.assertNotIn("screenshots", team_memory.prompt_context(NIGHT_SHIFT, ECHO))


class AgentTeamNoteTests(StateCase):
    def setUp(self) -> None:
        super().setUp()
        old_dirs = agents.AGENTS_DIR, agents.TEAMS_DIR
        agents.AGENTS_DIR, agents.TEAMS_DIR = self.root / "agents", self.root / "teams"
        agents.AGENTS_DIR.mkdir()
        agents.TEAMS_DIR.mkdir()
        self.addCleanup(lambda: setattr(agents, "AGENTS_DIR", old_dirs[0]) or setattr(agents, "TEAMS_DIR", old_dirs[1]))
        self.team = {"team_id": "team_1", "team_preset": {"id": TEAM, "name": "Night Shift"},
                     "tasks": [{"id": "build", "preset_member": {"id": ATLAS["id"], "name": "Atlas"}},
                               {"id": "loose"}]}
        read_team = patch.object(agents, "_read_team", side_effect=lambda team_id: self.team)
        read_team.start()
        self.addCleanup(read_team.stop)

    def agent(self, task_id: str, **meta) -> str:
        agent_id = f"agt_{task_id}"
        (agents.AGENTS_DIR / agent_id).mkdir()
        agents._write_meta(agent_id, {"agent_id": agent_id, "team_id": "team_1", "team_task_id": task_id,
                                      "provenance_class": "local", "status": "running", "started_at": time.time(),
                                      **meta})
        return agent_id

    REPORT = "Built it.\nMAC_MCP_TEAM_NOTE Build with make app.\nMAC_MCP_MEMBER_NOTE Check the icon cache first."

    def test_a_members_notes_are_kept_and_leave_its_report(self) -> None:
        agent_id = self.agent("build")
        clean = agents._keep_team_notes(agent_id, agents._read_meta(agent_id), self.REPORT)
        self.assertEqual("Built it.", clean)
        self.assertIn("Atlas: Build with make app.", team_memory.read(TEAM))
        self.assertIn("Atlas: Check the icon cache first.", team_memory.read(TEAM, ATLAS["id"]))
        self.assertEqual({"offered": 2, "team": 1, "member": 1, "refused_secret": 0},
                         agents._read_meta(agent_id)["team_notes"])

    def test_notes_from_work_that_read_untrusted_web_content_are_dropped(self) -> None:
        for name, meta in (
            ("web", {}),
            ("seeded", {"inherited_provenance": {"provenance_class": "tainted_untrusted_web"}}),
            ("team", {"provenance_class": "tainted_untrusted_web"}),
        ):
            with self.subTest(name):
                self.team["tasks"][0]["id"] = name
                agent_id = self.agent(name, **meta)
                stale = agents._read_meta(agent_id)
                if name == "web":
                    # Marked by the server while the agent ran; the worker's copy of the record predates it.
                    agents.record_agent_web_taint(agent_id)
                clean = agents._keep_team_notes(agent_id, stale, self.REPORT)
                self.assertEqual("Built it.", clean)
                self.assertEqual("", team_memory.read(TEAM))
                self.assertEqual("untrusted_web_content", agents._read_meta(agent_id)["team_notes"]["dropped"])

    def test_a_task_that_is_no_saved_member_keeps_its_report_as_written(self) -> None:
        agent_id = self.agent("loose")
        self.assertEqual(self.REPORT, agents._keep_team_notes(agent_id, agents._read_meta(agent_id), self.REPORT))
        self.assertFalse(team_memory.team_path(TEAM).exists())


class LeadToolTests(StateCase):
    def test_the_lead_adds_and_reads_team_and_member_notes(self) -> None:
        saved = agents.team_memory_tool(action="add", team="night shift", text="Releases need the owner's OK.")
        self.assertEqual(("team", True), (saved["saved_to"], saved["added"]))
        agents.team_memory_tool(action="add", team=TEAM, member="nova", text="Review with the checklist.")
        again = agents.team_memory_tool(action="add", team=TEAM, text="Releases need the owner's OK.")
        self.assertFalse(again["added"])
        read = agents.team_memory_tool(action="read", team="Night Shift")
        self.assertIn("lead: Releases need the owner's OK.", read["team"]["text"])
        nova = next(item for item in read["members"] if item["name"] == "Nova")
        self.assertIn("lead: Review with the checklist.", nova["text"])
        only_nova = agents.team_memory_tool(action="read", team=TEAM, member="Nova")
        self.assertEqual(["Nova"], [item["name"] for item in only_nova["members"]])

    def test_refusals(self) -> None:
        cases = [
            ("no such team", dict(action="read", team="Day Shift"), 404),
            ("no such member", dict(action="add", team=TEAM, member="Zed", text="x"), 404),
            ("empty note", dict(action="add", team=TEAM, text=" "), 400),
            ("secret", dict(action="add", team=TEAM, text=f"key {SECRET}"), 400),
            ("web-tainted lead", dict(action="add", team=TEAM, text="x", provenance_class="tainted_untrusted_web"), 403),
            ("bad action", dict(action="delete", team=TEAM), 400),
        ]
        for name, kwargs, code in cases:
            with self.subTest(name), self.assertRaises(HTTPException) as ctx:
                agents.team_memory_tool(**kwargs)
            self.assertEqual(code, ctx.exception.status_code)
        self.assertFalse(team_memory.team_path(TEAM).exists())

    def test_a_team_member_is_pointed_to_its_note_lines(self) -> None:
        token = team_messages.set_caller(team_messages.Caller(member_id="agent:agt_1", label="Atlas", agent_id="agt_1"))
        try:
            with self.assertRaises(HTTPException) as ctx:
                agents.team_memory_tool(action="add", team=TEAM, text="x")
        finally:
            team_messages.reset_caller(token)
        self.assertEqual(403, ctx.exception.status_code)
        self.assertIn("MAC_MCP_TEAM_NOTE", str(ctx.exception.detail))

    def test_private_mode_keeps_no_team_note(self) -> None:
        with patch.object(agents.privacy_policy, "policy", return_value={"private_mode": True, "paused": False}), \
             self.assertRaises(HTTPException) as ctx:
            agents.team_memory_tool(action="add", team=TEAM, text="x")
        self.assertIn("private_mode", str(ctx.exception.detail))
        self.assertFalse(team_memory.team_path(TEAM).exists())


class SettingsTests(StateCase):
    def test_the_owner_views_edits_resets_and_forgets_a_team(self) -> None:
        view = agents.team_memory_settings("view", TEAM)
        self.assertEqual(("", ["Atlas", "Nova", "Echo"]), (view["team"]["text"], [m["name"] for m in view["members"]]))
        agents.team_memory_settings("save", TEAM, text="Be brief.")
        agents.team_memory_settings("save", TEAM, member_id=NOVA["id"], text="Nova reviews.")
        self.assertEqual("Be brief.\n", agents.team_memory_settings("view", TEAM)["team"]["text"])
        after = agents.team_memory_settings("reset", TEAM, member_id=NOVA["id"])
        self.assertEqual(("Be brief.\n", ""), (after["team"]["text"], after["members"][1]["text"]))
        with self.assertRaises(HTTPException) as ctx:
            agents.team_memory_settings("save", TEAM, text=f"key {SECRET}")
        self.assertEqual(400, ctx.exception.status_code)
        with self.assertRaises(HTTPException) as saved:
            agents.team_memory_settings("forget", TEAM)
        self.assertEqual(409, saved.exception.status_code)
        # A team deleted in Settings takes its memory with it.
        write_settings(self.settings_path, [])
        self.assertEqual({"ok": True, "forgotten": True}, agents.team_memory_settings("forget", TEAM))
        self.assertFalse((team_memory.team_path(TEAM)).parent.exists())


class TaintRecorderTests(unittest.TestCase):
    def test_a_delegated_agents_web_taint_is_recorded_once(self) -> None:
        manager = SecurityContextManager()
        recorded = []
        manager.set_agent_taint_recorder(recorded.append)
        state = manager.touch("agent:agt_1", "pub_1")
        manager.note_agent_taint("agent:agt_1", "agt_1")
        self.assertEqual([], recorded)
        state.provenance_class = "tainted_untrusted_web"
        for _ in range(3):
            manager.note_agent_taint("agent:agt_1", "agt_1")
        manager.note_agent_taint("agent:agt_1", None)
        self.assertEqual(["agt_1"], recorded)


if __name__ == "__main__":
    unittest.main()
