"""Settings › Agents › Teams: saved agent teams in settings.json (`subagents.team_presets`), built in
the native app. Saving a team rewrites only that list; every other setting survives, and the app's
general save never drops the list."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"


def source(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


class TeamPresetsSourceTests(unittest.TestCase):
    def test_teams_is_an_agents_tab_after_general_with_a_link_and_search(self) -> None:
        tabs = source("SettingsTabs.swift")
        agents = tabs[tabs.index("enum AgentsTab"):tabs.index("enum UsageTab")]
        self.assertLess(agents.index("case general"), agents.index("case teams"))
        self.assertLess(agents.index("case teams"), agents.index("case providers"))
        self.assertIn('case .teams: return "Teams"', agents)
        self.assertIn('case .teams: return "person.3"', agents)
        self.assertIn('"teams": .agents', tabs)
        self.assertIn('case "teams", "team-presets": agents = .teams', tabs)
        view = source("SettingsView.swift")
        self.assertIn("case .teams:", view)
        self.assertIn("TeamPresetsSection(", view)
        search = source("SettingsSearch.swift")
        self.assertIn('"agents/teams": ["Teams"]', search)
        self.assertIn('"agents/teams": [', source("SettingsSearchLabels.swift"))

    def test_build_compiles_the_new_sources(self) -> None:
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        for name in ("AgentChoice.swift", "TeamPresets.swift", "TeamPresetsSection.swift"):
            self.assertIn(f'"${{SCRIPT_DIR}}/Sources/{name}"', build)

    def test_default_agent_and_team_members_share_one_status_check(self) -> None:
        view = source("SettingsView.swift")
        section = source("TeamPresetsSection.swift")
        self.assertIn("AgentChoice.status(", view)
        self.assertIn("AgentChoice.status(", section)
        self.assertIn("AgentChoice.providerOptions(", view)
        self.assertIn("AgentChoice.providerOptions(", section)

    def test_only_enabled_and_detected_providers_join_a_team(self) -> None:
        choice = source("AgentChoice.swift")
        self.assertIn("providers.filter { $0.enabled && $0.detected }", choice)
        section = source("TeamPresetsSection.swift")
        # The bench lists usable providers only, and a drop or click checks again (fail closed).
        self.assertIn("private var usable: [String] { AgentChoice.usableProviders(providers) }", section)
        self.assertGreaterEqual(section.count("guard AgentChoice.isUsable(provider, in: providers)"), 2)

    def test_the_builder_never_force_unwraps_the_draft(self) -> None:
        # Binding($draft) force-unwraps; the builder is still drawn while it leaves after Save or Close
        # sets draft to nil, and saving a team crashed the app (2026-10-27).
        section = source("TeamPresetsSection.swift")
        code = "\n".join(line.split("//", 1)[0] for line in section.splitlines())
        self.assertNotRegex(code, re.compile(r"Binding\(\$"))
        self.assertIn("Binding(get: { draft ?? current }, set: { draft = $0 })", section)

    def test_section_is_accessible_and_uses_the_state_alias(self) -> None:
        section = source("TeamPresetsSection.swift")
        self.assertNotRegex(section, re.compile(r"(?<![A-Za-z0-9_])@State\b"))
        self.assertIn('.accessibilityAction(named: "Move Up")', section)
        self.assertIn('Button("Delete Team", role: .destructive)', section)
        self.assertIn("Build your first team", section)


HARNESS = r'''
import Foundation

func object(_ path: String) -> [String: Any] {
    try! JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as! [String: Any]
}
func presets(_ path: String) -> [[String: Any]] {
    (object(path)["subagents"] as! [String: Any])["team_presets"] as! [[String: Any]]
}

@main struct TeamPresetsHarness {
    @MainActor static func main() throws {
        let path = ProcessInfo.processInfo.environment["MAC_MCP_SETTINGS_PATH"]!

        // Checks, without the store.
        var team = TeamPreset(id: "tp_00000001", name: "  ", members: [])
        precondition(team.issues(among: []).name != nil && team.issues(among: []).members != nil)
        for _ in 0..<10 { precondition(team.addMember(provider: "codex")) }
        precondition(!team.addMember(provider: "codex"), "an 11th member must not fit")
        precondition(Set(team.members.map { $0.name.lowercased() }).count == 10, "suggested names are unique")
        precondition(team.members.allSatisfy { $0.id.range(of: "^m_[0-9a-f]{8}$", options: .regularExpression) != nil })
        team.name = "Night Shift"
        precondition(team.issues(among: []).isEmpty)
        precondition(team.issues(among: [TeamPreset(id: "tp_other", name: "night shift ", members: [])]).name != nil,
                     "team names are unique without case")
        team.members[1].name = " ATLAS "
        precondition(team.issues(among: []).memberNames.count == 2, "member names are unique without case")
        team.members[1].name = String(repeating: "x", count: 41)
        precondition(team.issues(among: []).memberNames.count == 1)
        team.members.append(TeamPresetMember(id: "m_0000000b", name: "Eleven", provider: "codex"))
        precondition(team.issues(among: []).members != nil, "more than 10 members is refused")
        var order = TeamPreset(id: "tp_2", name: "Order", members: ["a", "b", "c", "d"].map {
            TeamPresetMember(id: $0, name: $0, provider: "codex")
        })
        order.moveMember(id: "a", to: 2)
        precondition(order.members.map(\.id) == ["b", "c", "a", "d"], "\(order.members.map(\.id))")
        order.moveMember(id: "d", to: 0)
        precondition(order.members.map(\.id) == ["d", "b", "c", "a"])
        order.moveMember(id: "b", to: 9)
        precondition(order.members.map(\.id) == ["d", "c", "a", "b"])
        precondition(order.duplicate(among: [order]).name == "Order Copy")
        precondition(TeamPreset.newPresetID(existing: []).range(of: "^tp_[0-9a-f]{8}$", options: .regularExpression) != nil)

        // The store: one unreadable entry and one known team are on disk with other settings.
        let store = SettingsStore()
        precondition(store.teamPresets.map(\.id) == ["tp_aaaaaaaa"], "\(store.teamPresets.map(\.id))")
        precondition(!store.teamPresetsIssue.isEmpty, "an unreadable team is reported")

        var created = TeamPreset(id: TeamPreset.newPresetID(existing: store.teamPresets), name: "  Night Shift ", members: [])
        created.addMember(provider: "codex")
        created.addMember(provider: "claude")
        created.members[1].model = "opus"
        created.members[1].reasoning = "high"
        created.members[1].role = .reviewer
        let stored = try store.saveTeamPreset(created)
        precondition(stored.name == "Night Shift")
        precondition(store.teamPresets.count == 2)

        // A duplicate name is refused and nothing is written.
        let before = try Data(contentsOf: URL(fileURLWithPath: path))
        var clash = TeamPreset(id: "tp_bbbbbbbb", name: "night shift", members: [])
        clash.addMember(provider: "codex")
        var refused = false
        do { try store.saveTeamPreset(clash) } catch { refused = true }
        let after = try Data(contentsOf: URL(fileURLWithPath: path))
        precondition(refused && after == before)

        // Editing the known team keeps keys this app does not know about.
        var known = store.teamPresets.first { $0.id == "tp_aaaaaaaa" }!
        known.name = "Day Shift"
        try store.saveTeamPreset(known)

        // The general save never drops the teams.
        store.serverPort = 8899
        try store.save()
        precondition(presets(path).count == 3, "general save kept every team entry")

        // Delete one; the others stay.
        var doomed = TeamPreset(id: "tp_cccccccc", name: "Doomed", members: [])
        doomed.addMember(provider: "codex")
        try store.saveTeamPreset(doomed)
        try store.deleteTeamPreset(id: "tp_cccccccc")
        precondition(presets(path).count == 3)

        let reloaded = SettingsStore()
        precondition(reloaded.teamPresets.map(\.name) == ["Day Shift", "Night Shift"], "\(reloaded.teamPresets.map(\.name))")
        print("CREATED=" + stored.id)
        print("TEAM_PRESETS_PASS")
    }
}
'''

LOCKED_HARNESS = r'''
import Foundation

@main struct LockedHarness {
    @MainActor static func main() {
        let store = SettingsStore()
        precondition(store.providerSettingsLocked)
        var team = TeamPreset(id: "tp_dddddddd", name: "Locked", members: [])
        team.addMember(provider: "codex")
        var refused = false
        do { try store.saveTeamPreset(team) } catch { refused = true }
        precondition(refused, "a settings file that cannot be read is never replaced")
        print("LOCKED_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class TeamPresetsStoreTests(unittest.TestCase):
    def build(self, td: Path, harness: str) -> Path:
        main = td / "Harness.swift"
        binary = td / "harness"
        main.write_text(textwrap.dedent(harness), encoding="utf-8")
        cmd = [
            "xcrun", "swiftc", "-parse-as-library",
            str(SOURCES / "SettingsStore.swift"), str(SOURCES / "KeychainStore.swift"),
            str(SOURCES / "TeamPresets.swift"), str(main),
            "-framework", "Security", "-o", str(binary),
        ]
        built = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(0, built.returncode, built.stderr)
        return binary

    def env(self, settings: Path) -> dict[str, str]:
        return {
            **os.environ,
            "MAC_MCP_SETTINGS_PATH": str(settings),
            "MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.settings-test",
            "MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT": "test",
            "MAC_MCP_DECISIONS_KEYCHAIN_SERVICE": "com.bulutarkan.mac-mcp.settings-test",
            "MAC_MCP_DECISIONS_KEYCHAIN_ACCOUNT": "test",
        }

    def test_saving_a_team_rewrites_only_team_presets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            settings = td / "settings.json"
            original = {
                "custom_owner_key": {"keep": True},
                "server": {"port": 8765, "cli_path": "/x/mac-mcp", "ngrok_on_start": False},
                "subagents": {
                    "providers": {"codex": {"enabled": True}, "claude": {"enabled": True}},
                    "default": {"provider": "codex", "model": "gpt-5.5"},
                    "max_active": 6,
                    "team_presets": [
                        {"id": "tp_broken"},
                        {"id": "tp_aaaaaaaa", "name": "Old Name", "note": "kept",
                         "members": [{"id": "m_00000001", "name": "Atlas", "provider": "codex"}],
                         "updated_at": "2026-10-01T09:00:00Z"},
                    ],
                },
            }
            settings.write_text(json.dumps(original), encoding="utf-8")
            binary = self.build(td, HARNESS)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30, env=self.env(settings))
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("TEAM_PRESETS_PASS", ran.stdout)
            created_id = re.search(r"CREATED=(\S+)", ran.stdout).group(1)

            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual({"keep": True}, data["custom_owner_key"])
            self.assertEqual({"provider": "codex", "model": "gpt-5.5"}, data["subagents"]["default"])
            self.assertEqual(6, data["subagents"]["max_active"])
            self.assertTrue(data["subagents"]["providers"]["codex"]["enabled"])
            self.assertEqual(8899, data["server"]["port"])  # the general save in the harness
            presets = data["subagents"]["team_presets"]
            self.assertEqual({"id": "tp_broken"}, presets[0])  # unreadable entries are left alone
            known = presets[1]
            self.assertEqual(("tp_aaaaaaaa", "Day Shift", "kept"), (known["id"], known["name"], known["note"]))
            self.assertRegex(known["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertNotEqual("2026-10-01T09:00:00Z", known["updated_at"])
            created = presets[2]
            self.assertEqual(created_id, created["id"])
            self.assertRegex(created["id"], r"^tp_[0-9a-f]{8}$")
            self.assertEqual("Night Shift", created["name"])
            self.assertEqual(["codex", "claude"], [m["provider"] for m in created["members"]])
            first, second = created["members"]
            self.assertRegex(first["id"], r"^m_[0-9a-f]{8}$")
            self.assertEqual({"id", "name", "provider"}, set(first))  # defaults and no role leave the keys out
            self.assertEqual(("opus", "high", "reviewer"), (second["model"], second["reasoning"], second["role"]))
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))

    def test_an_unreadable_settings_file_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            settings = td / "settings.json"
            settings.write_text("{not json", encoding="utf-8")
            binary = self.build(td, LOCKED_HARNESS)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30, env=self.env(settings))
            self.assertEqual(0, ran.returncode, ran.stderr)
            self.assertIn("LOCKED_PASS", ran.stdout)
            self.assertEqual("{not json", settings.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
