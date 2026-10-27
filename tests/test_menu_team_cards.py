"""Agent teams in the menu: one card while a team runs, one row once it finished.

The owner's 4-member "Vurucu Team" finished in 26 s; the menu showed its row only while it ran,
under the task title instead of the saved team's name, and then split it into 4 unrelated agent
rows. Now:

- a running team is a card named after its saved team (else its title), with "· task" and
  progress, listing each member: done, running (tool, turn time) or waiting ("Waiting for Atlas");
- a finished team folds into one row of the recent list (counted as one entry) that opens to its
  members, with the dashboard a link and a context action;
- the Live Island shows a running team as one row with the same name and says once when it ends.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests._menu_sources import feature_files

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"

HARNESS = r'''
import Foundation

func decode(_ json: String) -> AgentsEnvelope {
    try! JSONDecoder().decode(AgentsEnvelope.self, from: Data(json.utf8))
}

let running = #"""
{"agents": [
  {"agent_id": "agt_atlas", "team_id": "team_v", "team_task_id": "count", "status": "running", "phase": "tool",
   "title": "Atlas", "provider": "codex", "model": "gpt-6.1-sol", "reasoning": "high", "last_tool": "command_execution",
   "turn_elapsed_ms": 9000, "duration_ms": 12000},
  {"agent_id": "agt_solo_run", "status": "running", "title": "Fill four test forms in Safari", "provider": "codex"},
  {"agent_id": "agt_nova", "team_id": "team_v", "team_task_id": "readme", "status": "completed", "title": "Nova",
   "provider": "claude", "model": "opus", "reasoning": "high", "duration_ms": 11000, "ended_at": 1000011},
  {"agent_id": "agt_orion", "team_id": "team_v", "team_task_id": "commits", "status": "completed", "title": "Orion",
   "provider": "claude", "model": "sonnet", "reasoning": "high", "duration_ms": 6000, "ended_at": 1000006},
  {"agent_id": "agt_lost", "team_id": "team_unknown", "status": "completed", "title": "Lost member"},
  {"agent_id": "agt_solo_done", "status": "completed", "title": "Weekly report", "provider": "opencode"}
 ],
 "teams": [
  {"team_id": "team_v", "status": "running", "pending_count": 2, "failure_count": 0, "title": "kısa test",
   "team_preset": {"id": "tp_3802f446", "name": "Vurucu Team"},
   "budget": {"total_tokens_used": "not a number", "total_tokens_complete": true, "remaining_s": 120.5},
   "tasks": [
     {"id": "count", "title": "Atlas", "role": "coder", "state": "running", "depends_on": [], "agent_id": "agt_atlas",
      "provider": "codex", "model": "gpt-6.1-sol", "reasoning": "high"},
     {"id": "readme", "title": "Nova", "role": "coder", "state": "completed", "depends_on": [], "agent_id": "agt_nova"},
     {"id": "commits", "title": "Orion", "role": "coder", "state": "completed", "depends_on": [], "agent_id": "agt_orion"},
     {"id": "check", "title": "Echo", "role": "reviewer", "state": "blocked", "depends_on": ["count"],
      "review_of": "count", "agent_id": null, "provider": "codex", "model": "gpt-6-luna", "reasoning": "max"}
   ]}
 ]}
"""#

let finished = #"""
{"agents": [
  {"agent_id": "agt_echo", "team_id": "team_v", "team_task_id": "check", "status": "completed", "title": "Echo",
   "provider": "codex", "duration_ms": 13000, "ended_at": 1000026},
  {"agent_id": "agt_atlas", "team_id": "team_v", "team_task_id": "count", "status": "completed", "title": "Atlas",
   "provider": "codex", "duration_ms": 12000, "ended_at": 1000013},
  {"agent_id": "agt_nova", "team_id": "team_v", "team_task_id": "readme", "status": "completed", "title": "Nova",
   "provider": "claude", "duration_ms": 11000, "ended_at": 1000011},
  {"agent_id": "agt_orion", "team_id": "team_v", "team_task_id": "commits", "status": "completed", "title": "Orion",
   "provider": "claude", "duration_ms": 6000, "ended_at": 1000006},
  {"agent_id": "agt_solo_done", "status": "completed", "title": "Weekly report"}
 ],
 "teams": [
  {"team_id": "team_v", "status": "completed", "outcome": "completed", "pending_count": 0, "failure_count": 0,
   "title": "kısa test", "team_preset": {"id": "tp_3802f446", "name": "Vurucu Team"},
   "budget": {"total_tokens_used": 172782, "total_tokens_complete": true},
   "tasks": [
     {"id": "count", "title": "Atlas", "state": "completed", "agent_id": "agt_atlas"},
     {"id": "readme", "title": "Nova", "state": "completed", "agent_id": "agt_nova"},
     {"id": "commits", "title": "Orion", "state": "completed", "agent_id": "agt_orion"},
     {"id": "check", "title": "Echo", "role": "reviewer", "state": "completed", "depends_on": ["count"],
      "gate_result": "pass", "agent_id": "agt_echo"}
   ]}
 ]}
"""#

let adHoc = #"""
{"agents": [
  {"agent_id": "agt_a", "team_id": "team_x", "status": "failed", "phase": "failed", "title": "Scan A"},
  {"agent_id": "agt_b", "team_id": "team_x", "status": "completed", "title": "Scan B"}
 ],
 "teams": [
  {"team_id": "team_x", "status": "completed_with_failures", "outcome": "partial_failure", "pending_count": 0,
   "successful_count": 1, "failure_count": 1, "work_count": 2, "title": "Dependency scan",
   "budget": {"total_tokens_used": 5000, "total_tokens_complete": false}}
 ]}
"""#

@main
struct MenuTeamCardsHarness {
    static func main() {
        // While it runs: the saved team's name, the task beside it, progress through its plan.
        let live = decode(running)
        let team = live.teams![0]
        precondition(team.isActive)
        precondition(team.displayName == "Vurucu Team", team.displayName)
        precondition(team.taskTitle == "kısa test")
        precondition(team.progressText == "2/4 done", team.progressText)
        // An unreadable budget field drops out instead of failing the agents poll.
        precondition(team.budget?.totalTokensUsed == nil && team.budget?.totalTokensComplete == true)

        // Members in plan order, joined to their agents; a member not started yet keeps its own choice.
        let members = team.members(agents: live.agents)
        precondition(members.map(\.name) == ["Atlas", "Nova", "Orion", "Echo"], "\(members.map(\.name))")
        precondition(members.map(\.stage) == [.running, .done, .done, .waiting])
        precondition(members[0].agent?.agentID == "agt_atlas" && members[0].model == "gpt-6.1-sol")
        let echo = members[3]
        precondition(echo.agent == nil && echo.role == "reviewer")
        precondition(echo.waitingFor == "Waiting for Atlas", echo.waitingFor ?? "nil")
        precondition(echo.provider == "codex" && echo.model == "gpt-6-luna" && echo.reasoning == "max")
        precondition(team.durationMS(agents: live.agents) == nil, "no total while a member runs")

        // The list: the team once (not its agents one by one), agents on their own around it, and
        // agents of a team the menu has no summary for still listed.
        let list = MenuAgentEntry.list(agents: live.agents, teams: live.teams!)
        precondition(list.map(\.id) == ["team-team_v", "agt_solo_run", "agt_lost", "agt_solo_done"], "\(list.map(\.id))")

        // Once it finished: one row with its result, total time and tokens.
        let done = decode(finished)
        let past = done.teams![0]
        precondition(!past.isActive)
        precondition(past.resultText == "4/4 · review passed", past.resultText)
        precondition(past.totalTokens == 172782)
        precondition(past.durationMS(agents: done.agents) == 26000, "\(String(describing: past.durationMS(agents: done.agents)))")
        precondition(past.members(agents: done.agents).allSatisfy { $0.stage == .done && $0.waitingFor == nil })
        let doneList = MenuAgentEntry.list(agents: done.agents, teams: done.teams!)
        precondition(doneList.map(\.id) == ["team-team_v", "agt_solo_done"], "\(doneList.map(\.id))")

        // A team made up on the spot is named by its title; failures and partial tokens show honestly.
        let other = decode(adHoc)
        let scan = other.teams![0]
        precondition(scan.displayName == "Dependency scan" && scan.taskTitle == nil)
        precondition(scan.resultText == "1/2 · 1 failed", scan.resultText)
        precondition(scan.totalTokens == nil, "a lower bound is not shown as the total")
        let scanMembers = scan.members(agents: other.agents)
        precondition(scanMembers.map(\.name) == ["Scan A", "Scan B"] && scanMembers.map(\.stage) == [.failed, .done])
        print("MENU_TEAM_CARDS_PASS")
    }
}
'''


class MenuTeamCardModelTests(unittest.TestCase):
    def test_team_entries_members_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "MenuTeamCardsHarness.swift"
            binary = Path(td) / "menu-team-cards"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            cmd = [
                "xcrun", "swiftc", "-parse-as-library",
                *map(str, feature_files("AppState")),
                str(SOURCES / "AgentNotificationController.swift"),
                str(SOURCES / "SettingsStore.swift"),
                str(SOURCES / "ToolActivityBubbleController.swift"),
                str(SOURCES / "KeychainStore.swift"),
                str(source),
                "-framework", "SwiftUI", "-framework", "AppKit", "-framework", "Security",
                "-framework", "UserNotifications", "-framework", "Combine",
                "-o", str(binary),
            ]
            compiled = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("MENU_TEAM_CARDS_PASS", ran.stdout)


class MenuTeamCardSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.menu = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        cls.island = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")

    def section(self, start: str, end: str, text: str | None = None) -> str:
        text = text if text is not None else self.menu
        begin = text.index(start)
        return text[begin:text.index(end, begin)]

    def test_running_team_is_one_card_and_finished_team_one_row(self) -> None:
        card = self.section("private var agentCard", "private func compactAgentRow")
        self.assertIn("teamCard(team)", card)
        self.assertIn("case .team(let team): finishedTeamRow(team)", card)
        self.assertIn("case .agent(let agent): agentRow(agent)", card)
        # A running team's members are in its card, not repeated as solo rows below it.
        self.assertIn("if case .agent(let agent) = entry, agent.isActive { return agent }", card)
        self.assertIn("MenuAgentEntry.list(agents: state.agents, teams: state.agentTeams)", self.menu)
        self.assertIn("@MacMCPState private var expandedTeamIDs: Set<String> = []", self.menu)

    def test_cards_read_as_one_group_and_expanding_is_a_labelled_button(self) -> None:
        team_card = self.section("private func teamCard", "private func finishedTeamRow")
        self.assertIn("state.openDashboard(focusTeamID: team.teamID)", team_card)
        self.assertIn(".accessibilityElement(children: .contain)", team_card)
        self.assertIn('.accessibilityLabel("Team \\(teamSpokenName(team)), \\(team.progressText)")', team_card)
        row = self.section("private func finishedTeamRow", "private func teamMemberRow")
        self.assertIn('.accessibilityValue(expanded ? "Expanded" : "Collapsed")', row)
        self.assertIn(".accessibilityAddTraits(.isButton)", row)
        self.assertIn('Button("Show in Dashboard") { state.openDashboard(focusTeamID: team.teamID) }', row)
        self.assertIn("team.totalTokens", row)

    def test_no_endless_animation_in_the_menu(self) -> None:
        self.assertNotIn("repeatForever", self.menu)
        self.assertNotIn("repeatForever", self.island)

    def test_island_names_a_running_team_once(self) -> None:
        rebuild = self.section("private func rebuild()", "private func estimatedAgentStartedAt", self.island)
        self.assertIn('id: "team-\\(teamID)"', rebuild)
        self.assertIn("title: team.displayName", rebuild)
        self.assertIn("target: .team(teamID)", rebuild)
        self.assertIn("state.$agentTeams.sink", self.island)
        changed = self.section("private func agentsChanged", "private func isTeamMember", self.island)
        self.assertIn("if !isTeamMember(agent) {", changed)
        teams = self.section("private func teamsChanged", "private func scheduledRunFinished", self.island)
        self.assertIn('id: "team-end-\\(team.teamID)"', teams)
        self.assertIn("case .team(let id):\n            state.openDashboard(focusTeamID: id)", self.island)
        self.assertTrue(re.search(r"case team\(String\)", (SOURCES / "AppModels+Live.swift").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
