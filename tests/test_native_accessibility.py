"""Rows open from the keyboard and accessibility too, switches have names, and a failed load
says so with a way to retry (#181)."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
ONBOARDING = Path(__file__).resolve().parents[1] / "menu_app" / "Onboarding"


def source(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


class NativeAccessibilityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xcrun"), "needs the Swift toolchain")
    def test_island_mark_says_private_mode_in_every_state(self) -> None:
        # Roadmap #29: the badge showed Private mode, but VoiceOver only said "Mac MCP".
        text = source("LiveIsland.swift")
        self.assertIn(".accessibilityLabel(Self.spokenLabel(paused: paused, privateMode: privateMode))", text)
        start = text.index("    static func spokenLabel(")
        function = text[start:text.index("\n    }\n", start) + 6].replace("    static func", "func")
        script = function + '''
for (paused, privateMode) in [(false, false), (true, false), (false, true), (true, true)] {
    print(spokenLabel(paused: paused, privateMode: privateMode))
}
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "label.swift"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(["xcrun", "swift", str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(["Mac MCP", "Mac MCP, observation paused", "Mac MCP, private mode",
                          "Mac MCP, observation paused, private mode"], out.stdout.splitlines())

    def test_every_switch_has_an_accessible_name(self) -> None:
        for path in SOURCES.glob("*.swift"):
            with self.subTest(file=path.name):
                self.assertNotIn('Toggle("", isOn', path.read_text(encoding="utf-8"))

    def test_task_and_report_rows_open_without_a_mouse(self) -> None:
        helper = source("RunReportView.swift")
        self.assertIn("struct ActivatableRow: ViewModifier", helper)
        self.assertIn(".accessibilityAction(.default, action)", helper)
        self.assertIn(".onKeyPress(.return)", helper)
        for name in ("ScheduledTasksPane.swift", "RunReportView.swift", "LiveIsland.swift"):
            text = source(name)
            taps = [line for line in text.splitlines() if ".onTapGesture" in line]
            # The only tap gesture left is the helper's own, which carries the same action for
            # accessibility and the keyboard.
            self.assertTrue(all("perform: action" in line for line in taps), (name, taps))
        panes = source("ScheduledTasksPane.swift")
        self.assertIn('.activatable(task.approved ? "Opens the task" : "Reviews the suggestion")', panes)
        self.assertIn('row.activatable("Opens the run report")', panes)
        # The island's rows and peek open through accessibility too (the panel never takes focus).
        self.assertEqual(2, source("LiveIsland.swift").count('.activatable("Opens it in Mac MCP")'))

    def test_a_failed_first_load_offers_try_again(self) -> None:
        self.assertIn("struct LoadProblem: View", source("RunReportView.swift"))
        self.assertIn('Button("Try Again")', source("RunReportView.swift"))
        panes = source("ScheduledTasksPane.swift")
        self.assertIn("LoadProblem(message: message) { await load() }", panes)
        self.assertIn("LoadProblem(message: problem) { await loadRuns() }", panes)
        self.assertIn("LoadProblem(message: problem) { await loadFull() }", source("RunReportView.swift"))
        self.assertIn("LoadProblem(message: message) { await load() }", source("PrivacyPane.swift"))
        # The task list no longer spins forever when the first load fails.
        self.assertRegex(panes, re.compile(r"if !loaded \{\s*if message\.isEmpty \{\s*ProgressView", re.S))

    def test_the_tour_does_not_mistake_a_failed_scan_for_no_apps(self) -> None:
        tour = (ONBOARDING / "onboarding.js").read_text(encoding="utf-8")
        self.assertIn("appsProblem = (found.reason && found.reason.message)", tour)
        self.assertIn("Could not look for AI apps", tour)
        self.assertIn("Try again", tour)


if __name__ == "__main__":
    unittest.main()
