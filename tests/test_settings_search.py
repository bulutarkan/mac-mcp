from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ROOT / "menu_app" / "Sources" / "SettingsSearch.swift"
LABELS = ROOT / "menu_app" / "Sources" / "SettingsSearchLabels.swift"
VIEW = ROOT / "menu_app" / "Sources" / "SettingsView.swift"
TABS = ROOT / "menu_app" / "Sources" / "SettingsTabs.swift"

HARNESS = r'''
import Foundation

let titles = ["general": "General", "agents": "Agents", "usage": "Usage", "permissions": "Permissions & Safety",
              "connections": "Connections", "voice": "Voice", "advanced": "Advanced",
              "activity": "Activity & Notifications", "mcpServers": "MCP Servers"]

// The sidebar, in its order (SettingsSection.allCases).
let sidebar: [(id: String, title: String)] = [
    ("start", "Get Started"), ("general", "General"), ("agents", "Agents"), ("scheduled", "Automation"),
    ("activity", "Activity & Notifications"), ("usage", "Usage"), ("permissions", "Permissions & Safety"),
    ("privacy", "Privacy"), ("apps", "Apps"), ("mcpServers", "MCP Servers"), ("macs", "Devices"),
    ("connections", "Connections"), ("voice", "Voice"), ("advanced", "Advanced"), ("help", "Help & Diagnostics"),
]

func sections(_ query: String) -> [String] {
    titles.keys.sorted().filter {
        SettingsSearchIndex.matches(query: query, title: titles[$0]!, terms: SettingsSearchIndex.terms[$0] ?? "")
    }
}

func listed(_ query: String) -> [String] {
    sidebar.filter {
        SettingsSearchIndex.matches(query: query, title: $0.title, terms: SettingsSearchIndex.searchTerms(for: $0.id))
    }.map { $0.id }
}

func lands(_ query: String, from current: String? = nil) -> String {
    guard let hit = SettingsSearchIndex.target(query: query, sections: sidebar, current: current) else { return "nil" }
    return hit.tab.map { "\(hit.section)/\($0)" } ?? hit.section
}

@main
struct SearchHarness {
    static func main() {
        let cases: [(String, String)] = [
            ("agent notifications", "activity"),
            ("tool activity bubble", "activity"),
            ("issues", "general"),
            ("public tunnel", "connections"),
            ("server port", "advanced"),
            ("  Server   PORT ", "advanced"),
            ("notif", "activity"),
            ("usage retention", "usage"),
            ("perm", "permissions"),
            ("mcp", "mcpServers"),
            ("exa", "mcpServers"),
        ]
        for (query, expected) in cases {
            let found = sections(query)
            precondition(found.contains(expected), "\(query) -> \(found)")
        }
        precondition(sections("qwerty zebra").isEmpty, "nonsense must match nothing")
        precondition(sections("").count == titles.count, "empty query lists everything")

        // #125: labels people see, and what they call them, land on the pane and tab that has them.
        let landing: [(String, String?, String)] = [
            ("login", nil, "general"),
            ("log in", nil, "general"),
            ("start at login", nil, "general"),
            ("login", "permissions", "general"),
            ("max active", nil, "agents/agents-general"),
            ("max active agents", nil, "agents/agents-general"),
            ("agents at once", nil, "agents/agents-general"),
            ("browser activity", nil, "connections/browser"),
            ("browser activity", "activity", "connections/browser"),
            ("browser", nil, "connections/browser"),
            ("brows", nil, "connections/browser"),  // while it is still being typed
            ("safari", nil, "connections/browser"),
            ("chrome visual companion", nil, "connections/browser"),
            ("mobile", nil, "connections/mobile"),
            ("skills", nil, "agents/skills"),
            ("team history", nil, "agents/team-history"),
            ("recipes", nil, "scheduled/recipes"),
            ("general", nil, "general"),
            ("privacy", nil, "privacy"),
            ("voice name", nil, "voice"),
            ("speech rate", nil, "voice"),
            ("decision scope", nil, "advanced"),
            ("openai decisions", nil, "advanced"),
            ("who can use this mac", nil, "macs"),
            ("glow", nil, "activity"),
        ]
        for (query, current, expected) in landing {
            let found = lands(query, from: current)
            precondition(found == expected, "\(query) from \(current ?? "-") -> \(found), wanted \(expected)")
            precondition(listed(query).contains(String(expected.split(separator: "/")[0])), "\(query) is not listed")
        }
        // The open pane stays while it matches as well as any other.
        precondition(lands("agent", from: "agents") == "agents", "agent from agents -> \(lands("agent", from: "agents"))")
        precondition(lands("") == "nil" && lands("qwerty zebra") == "nil", "empty or nonsense lands nowhere")
        precondition(listed("qwerty zebra").isEmpty, "nonsense lists nothing")
        precondition(SettingsSearchIndex.phraseContains(["login"], "Start Mac MCP when you log in"), "login in log in")
        precondition(!SettingsSearchIndex.phraseContains(["active", "max"], "max active agents"), "words in a row only")
        print("SETTINGS_SEARCH_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class SettingsSearchTests(unittest.TestCase):
    def test_multiword_queries_find_their_section_and_nonsense_finds_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "SearchHarness.swift"
            binary = Path(td) / "settings-search-harness"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SEARCH), str(LABELS), str(harness), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("SETTINGS_SEARCH_PASS", run.stdout)

    def test_empty_results_show_a_message_instead_of_a_stale_pane(self) -> None:
        view = VIEW.read_text(encoding="utf-8")
        self.assertIn("No settings found for", view)
        self.assertIn('Button("Clear Search") { settingsSearch = "" }', view)
        # #125: the search lands on the best match and its tab instead of the first listed pane.
        self.assertIn("SettingsSearchIndex.target(", view)
        self.assertIn("if let tab = hit.tab { openSearchTab(tab) }", view)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("Sources/SettingsSearch.swift", build)
        self.assertIn("Sources/SettingsSearchLabels.swift", build)


class SettingsSearchLabelsTests(unittest.TestCase):
    """#125: the visible labels come from the views, and every tab a search names can be opened."""

    def test_the_generated_labels_match_the_views(self) -> None:
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "settings_search_labels.py"), "--check"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(0, run.returncode, run.stderr)
        labels = LABELS.read_text(encoding="utf-8")
        for label in ('"Start Mac MCP when you log in"', '"Agents at Once"', '"Use OpenAI Decisions"',
                      '"Who can use this Mac"', '"Agents in Safari and Chrome"'):
            self.assertIn(label, labels)

    def test_every_tab_a_search_names_opens(self) -> None:
        sources = SEARCH.read_text(encoding="utf-8") + LABELS.read_text(encoding="utf-8")
        # The pane ids, from the terms table ("start": "get started …").
        panes = "|".join(re.findall(r'^        "([A-Za-z]+)": "', SEARCH.read_text(encoding="utf-8"), re.M))
        self.assertIn("connections", panes)
        tabs = set(re.findall(rf'"(?:{panes})/([a-z-]+)"', sources))
        self.assertTrue({"browser", "agents-general", "recipes", "skills"} <= tabs, tabs)
        view = VIEW.read_text(encoding="utf-8")
        opener = view[view.index("private func openSearchTab"):view.index("private func sidebarRow")]
        links = TABS.read_text(encoding="utf-8")
        links = links[links.index("func select(link: String)"):]
        for tab in tabs:
            self.assertTrue(f'case "{tab}"' in opener or f'"{tab}"' in links, tab)


if __name__ == "__main__":
    unittest.main()
