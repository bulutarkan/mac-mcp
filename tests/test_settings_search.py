from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ROOT / "menu_app" / "Sources" / "SettingsSearch.swift"
VIEW = ROOT / "menu_app" / "Sources" / "SettingsView.swift"

HARNESS = r'''
import Foundation

let titles = ["general": "General", "agents": "Agents", "usage": "Usage", "permissions": "Permissions & Safety",
              "connections": "Connections", "voice": "Voice", "advanced": "Advanced"]

func sections(_ query: String) -> [String] {
    titles.keys.sorted().filter {
        SettingsSearchIndex.matches(query: query, title: titles[$0]!, terms: SettingsSearchIndex.terms[$0] ?? "")
    }
}

@main
struct SearchHarness {
    static func main() {
        let cases: [(String, String)] = [
            ("agent notifications", "general"),
            ("public tunnel", "connections"),
            ("server port", "advanced"),
            ("  Server   PORT ", "advanced"),
            ("notif", "general"),
            ("usage retention", "usage"),
            ("perm", "permissions"),
        ]
        for (query, expected) in cases {
            let found = sections(query)
            precondition(found.contains(expected), "\(query) -> \(found)")
        }
        precondition(sections("qwerty zebra").isEmpty, "nonsense must match nothing")
        precondition(sections("").count == titles.count, "empty query lists everything")
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
                ["xcrun", "swiftc", "-parse-as-library", str(SEARCH), str(harness), "-o", str(binary)],
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
        self.assertIn("if !matches.contains(selection), let first = matches.first", view)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("Sources/SettingsSearch.swift", build)


if __name__ == "__main__":
    unittest.main()
