from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
SECTION = SOURCES / "ProviderUsageSection.swift"


def _swift_block(path: Path, start: str) -> str:
    """The top-level declaration starting with `start`, up to its closing brace at column 0."""
    text = path.read_text(encoding="utf-8")
    begin = text.index(start)
    end = text.index("\n}\n", begin) + 3
    return text[begin:end]


class ProviderUsageSectionTests(unittest.TestCase):
    def test_usage_pane_lists_providers_instead_of_a_three_column_grid(self) -> None:
        settings = (SOURCES / "SettingsView.swift").read_text(encoding="utf-8")
        self.assertIn("ProviderUsageSection(", settings)
        self.assertNotIn("providerUsageCard(", settings)
        self.assertNotIn("usageCards", settings)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Sources/ProviderUsageSection.swift"', build)

        section = SECTION.read_text(encoding="utf-8")
        # Rows start folded and the busiest agent comes first.
        self.assertIn("@MacMCPState private var expanded: Set<String> = []", section)
        self.assertIn("if lhs.total != rhs.total { return lhs.total > rhs.total }", section)
        # Kimi reports no tokens; it is named as such rather than shown as zero.
        self.assertIn('providersWithoutUsageReports: Set<String> = ["kimi"]', section)

    def test_every_known_agent_has_its_own_style_and_formatting_holds(self) -> None:
        providers = _swift_block(SOURCES / "SettingsStore.swift", "enum AgentProviders {")
        models = "\n".join(
            _swift_block(SOURCES / "AppModels+Dashboard.swift", name)
            for name in (
                "struct ProviderUsageModel:",
                "struct ProviderUsageProvider:",
                "struct ProviderUsageSummaryEnvelope:",
            )
        )
        ids = re.findall(r'"([a-z]+)": "', providers.split("static let more")[0])
        self.assertGreaterEqual(len(ids), 10)
        harness = f"""
import Foundation
import SwiftUI
{providers}
{models}
let ids: [String] = [{", ".join(f'"{i}"' for i in ids)}]
for id in ids where ProviderUsageStyle.of(id).symbol == "cpu" {{
    print("UNSTYLED \\(id)"); exit(1)
}}
precondition(Set(ids) == Set(AgentProviders.names.keys))
precondition(ProviderUsageFormat.compact(102_000_000) == "102.0M")
precondition(ProviderUsageFormat.compact(nil) == "—")
precondition(ProviderUsageFormat.share(414_000, of: 123_614_000) == "<1%")
precondition(ProviderUsageFormat.share(102_000_000, of: 123_614_000) == "83%")
precondition(ProviderUsageFormat.share(0, of: 0) == "—")
precondition(ProviderUsageFormat.since("not-a-date") == "not-a-date")
precondition(ProviderUsageFormat.since(nil) == nil)
print("PROVIDER_USAGE_SECTION_PASS")
"""
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main.swift"
            main.write_text(harness, encoding="utf-8")
            binary = Path(tmp) / "harness"
            built = subprocess.run(
                [
                    "xcrun", "swiftc", "-target", "arm64-apple-macos13.0",
                    str(SOURCES / "SwiftUICompat.swift"), str(SECTION), str(main), "-o", str(binary),
                ],
                capture_output=True, text=True, timeout=240,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertIn("PROVIDER_USAGE_SECTION_PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
