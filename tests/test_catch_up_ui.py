"""#256: the task editor offers catch-up for timed tasks only, the card says what happens to a missed run,
and a catch-up run is labeled in the history."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._menu_sources import feature_source

PANE = (Path(__file__).resolve().parents[1] / "menu_app" / "Sources" / "ScheduledTasksPane.swift").read_text(encoding="utf-8")


class CatchUpUITests(unittest.TestCase):
    def test_the_switch_sends_run_once_only_for_timed_tasks(self) -> None:
        self.assertIn('"missed": catchUp && when != .file ? "run_once" : "skip"', PANE)
        self.assertIn('Text("Run it once when the Mac is back if it was asleep or Mac MCP was off")', PANE)
        self.assertIn("if model.when != .file {", PANE)
        self.assertIn("catchUp = task.catchUp", PANE)

    def test_card_and_history_say_what_happened(self) -> None:
        self.assertIn('factRow("moon.zzz", "If the Mac is asleep"', PANE)
        self.assertIn('if run.trigger == "catch_up" { parts.append("Catch-up") }', PANE)
        self.assertIn('var catchUp: Bool { (raw["missed"] as? String) == "run_once" }', feature_source("AppState"))


if __name__ == "__main__":
    unittest.main()
