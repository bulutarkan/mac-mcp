"""Settings › Usage › Teams: a tab after Delegated Agents with each saved team's tokens by member, day and run,
read from /dashboard/api/team-usage, and one quiet line in Delegated Agents for the share teams used."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


def source(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


def swift_block(name: str, start: str) -> str:
    text = source(name)
    begin = text.index(start)
    return text[begin:text.index("\n}\n", begin) + 3]


class TeamUsageSourceTests(unittest.TestCase):
    def test_teams_is_a_usage_tab_after_delegated_agents_with_a_link_and_search(self) -> None:
        tabs = source("SettingsTabs.swift")
        usage = tabs[tabs.index("enum UsageTab"):tabs.index("enum PrivacyTab")]
        cases = re.findall(r"^    case (\w+)$", usage, re.MULTILINE)
        self.assertEqual(["tools", "agents", "teams", "chat", "data"], cases)
        self.assertIn('case .teams: return "Teams"', usage)
        self.assertIn('"team-usage": .usage', tabs)
        self.assertIn('case "team-usage": usage = .teams', tabs)
        view = source("SettingsView.swift")
        pane = view[view.index("    private var usagePane: some View {"):view.index("    private func usageOverviewCard(")]
        order = ["case .agents:", "ProviderUsageSection(", "showTeams: { tabs.usage = .teams }",
                 "case .teams:", "TeamUsageSection(state: state)", "case .chat:"]
        self.assertEqual(sorted(order, key=pane.index), order)
        self.assertIn('"usage/team-usage": ["Teams"]', source("SettingsSearch.swift"))
        self.assertIn('"usage/team-usage": [', source("SettingsSearchLabels.swift"))
        self.assertIn('"TeamUsageSection.swift": "usage/team-usage"',
                      (ROOT / "scripts" / "settings_search_labels.py").read_text(encoding="utf-8"))
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        for name in ("TeamUsage.swift", "TeamUsageSection.swift"):
            self.assertIn(f'"${{SCRIPT_DIR}}/Sources/{name}"', build)

    def test_section_follows_the_settings_rules(self) -> None:
        for name in ("TeamUsage.swift", "TeamUsageSection.swift"):
            text = source(name)
            self.assertNotRegex(text, re.compile(r"(?<![A-Za-z0-9_])@State\b"), name)
            self.assertNotIn("repeatForever", text, name)
            self.assertNotIn("Up to date", text, name)
            self.assertNotIn("NEW", text, name)
        section = source("TeamUsageSection.swift")
        # The Delegated Agents tab's pieces: the period picker, the rows' box, the bar chart and facts.
        for piece in ("ProviderUsagePeriod.allCases", ".usageListBox()", "UsageBarChart(", "UsageFactsRow(",
                      "UsageShareMeter(", 'state.taskAPI("dashboard/api/team-usage?days=\\(period.days)")'):
            self.assertIn(piece, section)
        for label in ('.accessibilityHint(open ? "Hides the details" : "Shows the details")',
                      '.accessibilityHint(open ? "Hides each member" : "Shows each member")',
                      '.accessibilityLabel("About team usage")'):
            self.assertIn(label, section)
        self.assertIn("Deleted teams (", section)
        self.assertIn("No saved team ran in this period.", section)

    def test_delegated_agents_says_how_much_came_from_teams_only_when_some_did(self) -> None:
        section = source("ProviderUsageSection.swift")
        line = section[section.index("    private var teamsLine: some View {"):section.index("    private var footer")]
        self.assertIn("if teamTokens > 0 {", line)
        self.assertIn('of this came from teams', line)
        self.assertIn('Button("Show in Teams ›", action: showTeams)', line)
        self.assertIn('case teamTokens = "team_tokens"', source("AppModels+Dashboard.swift"))


HARNESS = r'''
import AppKit
import SwiftUI

final class AppState: ObservableObject {
    func taskAPI(_ path: String, body: [String: Any]? = nil) async -> (ok: Bool, object: [String: Any]) { (false, [:]) }
}
enum MenuAppDelegate { @MainActor static var openSettings: ((String) -> Void)? }
enum AgentChoice {
    static func providerName(_ id: String) -> String { AgentProviders.name(id) ?? id }
    static func reasoningDisplayName(_ value: String) -> String { value.capitalized }
}

@main struct TeamUsageHarness {
    @MainActor static func main() {
        let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let summary = TeamUsageSummary(try! JSONSerialization.jsonObject(with: data) as! [String: Any])
        precondition(summary.totalTokens == 2_900 && summary.runs == 2)
        precondition(summary.saved.map(\.name) == ["Night Shift"] && summary.deleted.map(\.name) == ["Old Crew"])
        precondition(summary.otherTokens == 70 && summary.otherRuns == 1 && summary.availableSince == "2026-10-01")
        let night = summary.saved[0]
        precondition(night.members.map(\.name) == ["Atlas", "Nova"] && night.members[0].tokens == 1_700)
        precondition(night.daily["2026-10-27"] == ["m_a": 1_700, "m_b": 300])
        let run = night.runList[0]
        precondition(run.title == "Night Shift docs" && run.rows.count == 2 && run.gatesPassed == 1)
        precondition(TeamUsageFormat.outcome(run).text == "2/2 · review passed" && TeamUsageFormat.outcome(run).good == true)
        precondition(TeamUsageFormat.status(run.rows[1]) == "review passed" && TeamUsageFormat.status(run.rows[0]) == "done")
        precondition(TeamUsageFormat.runTitle(run.title, team: "Night Shift") == "docs")
        precondition(TeamUsageFormat.runTitle("Night Shift", team: "Night Shift") == "Night Shift")

        // Formatting.
        precondition(TeamUsageFormat.initials("Atlas") == "At" && TeamUsageFormat.initials("Night Shift") == "NS")
        precondition(TeamUsageFormat.duration(25.8) == "26 s" && TeamUsageFormat.duration(360) == "6 min")
        precondition(TeamUsageFormat.duration(3_900) == "1 h 5 min" && TeamUsageFormat.duration(nil) == "—")
        let now = Date()
        precondition(TeamUsageFormat.when(now, now: now).hasPrefix("today "))
        precondition(TeamUsageFormat.when(now.addingTimeInterval(-86_400), now: now).hasPrefix("yesterday "))
        let failed = TeamUsageRun(teamID: "t", title: "", startedAt: now, durationSeconds: nil, tokens: 0, members: 4,
                                  completed: 3, running: 0, failed: 1, gates: 1, gatesPassed: 1, rows: [])
        precondition(TeamUsageFormat.outcome(failed).text == "3/4 · 1 failed · review passed")
        precondition(TeamUsageFormat.outcome(failed).good == false)
        let going = TeamUsageRun(teamID: "t", title: "", startedAt: now, durationSeconds: nil, tokens: 0, members: 4,
                                 completed: 2, running: 2, failed: 0, gates: 0, gatesPassed: 0, rows: [])
        precondition(TeamUsageFormat.outcome(going).text == "2/4 done, running" && TeamUsageFormat.outcome(going).good == nil)

        // Days and months as bars, the same as the other Usage tabs.
        let calendar = Calendar(identifier: .gregorian)
        let today = calendar.date(from: DateComponents(year: 2026, month: 10, day: 27, hour: 12))!
        let days = ProviderUsagePeriod.month.buckets(night.daily, today: today, calendar: calendar)
        precondition(days.count == 30 && days.last?.total == 2_000 && days.last?.title == "Today")
        let months = ProviderUsagePeriod.year.buckets(night.daily, today: today, calendar: calendar)
        precondition(months.count == 12 && months.last?.total == 2_000)
        precondition(TeamUsageColors.members(night.members).count == 2)

        // The section draws its overview, an open team and the empty state without a server.
        for view in [AnyView(TeamUsageSection(state: AppState(), summary: summary)),
                     AnyView(TeamUsageSection(state: AppState(), summary: summary, expanded: [night.id])),
                     AnyView(TeamUsageSection(state: AppState(), summary: TeamUsageSummary([:])))] {
            let renderer = ImageRenderer(content: view.frame(width: 760))
            guard let image = renderer.cgImage, image.width > 100, image.height > 50 else {
                print("NOT RENDERED"); exit(1)
            }
        }
        print("TEAM_USAGE_NATIVE_PASS")
    }
}
'''

PAYLOAD = {
    "ok": True, "days": 30, "total_tokens": 2900, "runs": 2, "available_since": "2026-10-01",
    "other_teams": {"tokens": 70, "runs": 1}, "unreported_turns": 0, "providers": {},
    "teams": [
        {"id": "tp_0000000a", "name": "Night Shift", "exists": True, "tokens": 2000, "share": 0.69, "runs": 1,
         "last_run_at": 1793105110.0, "average_duration_s": 26.0, "gates": 1, "gates_passed": 1,
         "members": [
             {"key": "m_a", "name": "Atlas", "provider": "codex", "model": "gpt-6.1-sol", "reasoning": "high",
              "role": "coder", "tokens": 1700, "share": 0.85, "runs": 1},
             {"key": "m_b", "name": "Nova", "provider": "codex", "model": None, "reasoning": None,
              "role": "reviewer", "tokens": 300, "share": 0.15, "runs": 1}],
         "daily": [{"date": "2026-10-27", "members": {"m_a": 1700, "m_b": 300}}],
         "run_list": [{"team_id": "team_1", "title": "Night Shift docs", "started_at": 1793105110.0,
                       "ended_at": 1793105136.0, "duration_s": 26.0, "tokens": 2000,
                       "outcome": {"members": 2, "completed": 2, "running": 0, "failed": 0, "gates": 1,
                                   "gates_passed": 1},
                       "members": [
                           {"key": "m_a", "name": "Atlas", "provider": "codex", "model": "gpt-6.1-sol",
                            "reasoning": "high", "role": "coder", "tokens": 1700, "duration_s": 12.0,
                            "status": "completed", "agents": 1},
                           {"key": "m_b", "name": "Nova", "provider": "codex", "model": None, "reasoning": None,
                            "role": "reviewer", "tokens": 300, "duration_s": 13.0, "status": "completed",
                            "gate": "pass", "agents": 1}]}]},
        {"id": "tp_0000000b", "name": "Old Crew", "exists": False, "tokens": 900, "share": 0.31, "runs": 1,
         "last_run_at": 1792000000.0, "average_duration_s": None, "gates": 0, "gates_passed": 0,
         "members": [{"key": "m_c", "name": "Kaan", "provider": "claude", "tokens": 900, "share": 1, "runs": 1}],
         "daily": [], "run_list": []},
    ],
}


class TeamUsageHarnessTests(unittest.TestCase):
    def test_summary_formatting_buckets_and_rendering(self) -> None:
        stubs = "\n".join([
            swift_block("SettingsStore.swift", "enum AgentProviders {"),
            *(swift_block("AppModels+Dashboard.swift", name) for name in (
                "struct ProviderUsageModel:", "struct ProviderUsageProvider:", "struct ProviderUsageSummaryEnvelope:")),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main.swift"
            main.write_text(stubs + HARNESS, encoding="utf-8")
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps(PAYLOAD), encoding="utf-8")
            binary = Path(tmp) / "harness"
            files = ["SwiftUICompat.swift", "UsageComponents.swift", "ProviderUsageSection.swift", "TeamUsage.swift",
                     "TeamUsageSection.swift"]
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", "-target", "arm64-apple-macos13.0",
                 *(str(SOURCES / name) for name in files), str(main), "-o", str(binary)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary), str(payload)], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("TEAM_USAGE_NATIVE_PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
