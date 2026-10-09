from __future__ import annotations

import unittest
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"


class NativeReduceMotionTests(unittest.TestCase):
    def test_menu_bar_pulse_stops_with_reduce_motion_and_follows_changes(self) -> None:
        state = (SOURCES / "AppState.swift").read_text(encoding="utf-8")
        should = state[state.index("private var shouldPulse: Bool"):state.index("private func observeReduceMotion")]
        self.assertIn("activeAgents > 0 && !reduceMotion", should)
        self.assertIn("NSWorkspace.shared.accessibilityDisplayShouldReduceMotion", state)
        self.assertIn("NSWorkspace.accessibilityDisplayOptionsDidChangeNotification", state)
        observer = state[state.index("private func observeReduceMotion"):]
        self.assertIn("self.updatePulseTask()", observer[:800])
        init = state[state.index("init(startBackgroundTasks: Bool = true)"):state.index("deinit {")]
        self.assertIn("observeReduceMotion()", init)

    def test_active_state_stays_visible_without_motion(self) -> None:
        app = (SOURCES / "MacMCPMenuApp.swift").read_text(encoding="utf-8")
        # Without the pulse the active symbol is the static "cpu", distinct from idle.
        self.assertIn('return state.pulse ? "cpu.fill" : "cpu"', app)
        self.assertIn('return "server.rack"', app)
        self.assertIn('"Mac MCP, active work"', app)

    def test_robot_draws_a_still_pose_under_reduce_motion(self) -> None:
        view = (SOURCES / "MenuBarView.swift").read_text(encoding="utf-8")
        robot = view[view.index("struct RobotRunner: View"):]
        robot = robot[:robot.index("\n}\n") + 3]
        self.assertIn("@Environment(\\.accessibilityReduceMotion) private var reduceMotion", robot)
        self.assertIn("if active && !reduceMotion {", robot)
        self.assertIn("TimelineView(.animation(minimumInterval: 0.12))", robot)
        self.assertEqual(1, robot.count("TimelineView("), "the still pose must not schedule redraws")
        self.assertIn("moving: false", robot)
        self.assertIn('.accessibilityLabel(active ? "Agent working" : "Agent idle")', robot)


if __name__ == "__main__":
    unittest.main()
