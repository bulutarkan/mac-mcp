"""#202: the island shows the maker's logo of the agent at work, not a generic symbol."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISLAND = (ROOT / "menu_app" / "Sources" / "LiveIsland.swift").read_text(encoding="utf-8")


class IslandProviderLogoTests(unittest.TestCase):
    def test_each_provider_maps_to_a_logo_that_ships_with_the_app(self) -> None:
        body = ISLAND.split("static func forProvider", 1)[1].split("private static var logos", 1)[0]
        logos = set(re.findall(r'\.provider\("([a-z]+)"\)', body))
        self.assertEqual({"claude", "openai", "opencode", "kimi", "grok"}, logos)
        for name in logos:
            self.assertTrue((ROOT / "menu_app" / "Onboarding" / "logos" / f"{name}.png").exists(), name)
        self.assertIn('id == "codex"', body)
        self.assertIn('return .symbol("sparkles")', body)  # unknown providers keep the generic symbol
        self.assertIn('subdirectory: "Onboarding/logos"', ISLAND)
        build = (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}/Onboarding" "${RESOURCES}/"', build)

    def test_running_agents_show_their_logo_in_the_slot_and_the_panel(self) -> None:
        self.assertIn("slot = .working(IslandIcon.forProvider(agent.provider)", ISLAND)
        self.assertIn('IconTile(icon: IslandIcon.forProvider(item.symbol), size: 26)', ISLAND)
        self.assertNotIn('slot = .working(.symbol("sparkles")', ISLAND)
        # Waiting for an agent is not counted as a second piece of work.
        self.assertIn("tools.count - agentCalls + runningAgents().count", ISLAND)
        # The ring only turns in the working slot; finished and empty slots have none.
        working = ISLAND.split("case .working(let icon, let count):", 1)[1].split("case .finished", 1)[0]
        self.assertIn("SpinnerRing", working)


if __name__ == "__main__":
    unittest.main()
