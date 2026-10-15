"""The island's Now list follows agents as they finish, not one change behind.

@Published publishes before it stores the new value. The island's agents sink rebuilt
Now from `state.agents`, which still held the previous list, so an agent that had just
finished stayed under Now with its timer running (while Recent already said it finished)
until something else changed the list.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ISLAND = (Path(__file__).resolve().parents[1] / "menu_app" / "Sources" / "LiveIsland.swift").read_text(encoding="utf-8")


class IslandAgentStateTests(unittest.TestCase):
    def test_now_is_built_from_the_published_agents(self) -> None:
        changed = ISLAND.split("private func agentsChanged(_ agents: [AgentInfo]) {", 1)[1]
        self.assertTrue(changed.lstrip().startswith("agentsSnapshot = agents"))
        self.assertIn("private func runningAgents() -> [AgentInfo] { agentsSnapshot.filter(\\.isActive) }", ISLAND)
        self.assertNotIn("state?.agents", ISLAND)

    def test_server_state_is_the_published_value_too(self) -> None:
        sink = ISLAND.split("state.$serverRunning.removeDuplicates().sink", 1)[1].split("}.store(", 1)[0]
        self.assertIn("self?.serverRunningNow = running", sink)
        rebuild = ISLAND.split("private func rebuild()", 1)[1].split("private func estimatedAgentStartedAt", 1)[0]
        self.assertIn("!serverRunningNow", rebuild)
        self.assertNotIn("state.serverRunning", rebuild)


if __name__ == "__main__":
    unittest.main()
