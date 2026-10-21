"""#57: the task editor offers "run it once after the last run" for timed tasks only, and the card says
what happens to a run that comes due while the previous one is still going."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._menu_sources import feature_source

PANE = (Path(__file__).resolve().parents[1] / "menu_app" / "Sources" / "ScheduledTasksPane.swift").read_text(encoding="utf-8")


class BusyRunAfterUITests(unittest.TestCase):
    def test_the_switch_sends_run_after_only_for_timed_tasks(self) -> None:
        self.assertIn('"busy": runAfterBusy && when != .file ? "run_after" : "skip"', PANE)
        self.assertIn('Text("If the last run is still going when it’s due, run it once after that run")', PANE)
        self.assertIn("runAfterBusy = task.runsAfterBusy", PANE)
        toggles = PANE[PANE.index("if model.when != .file {"):]
        self.assertLess(toggles.index("$model.catchUp"), toggles.index("$model.runAfterBusy"))

    def test_the_card_says_what_happens(self) -> None:
        self.assertIn('factRow("hourglass", "If the last run is still going"', PANE)
        self.assertIn('var runsAfterBusy: Bool { (raw["busy"] as? String) == "run_after" }', feature_source("AppState"))


if __name__ == "__main__":
    unittest.main()
