"""#225: the app's watchdog says once when a run never started, and stays quiet after sleep or a restart."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menu_app" / "Sources" / "ScheduledTaskWatchdog.swift"

HARNESS = r'''
import Foundation

func check(_ ok: Bool, _ what: String) { if !ok { print("FAIL: \(what)"); exit(1) } }
let t0 = Date(timeIntervalSince1970: 1_800_000_000)
let tol: TimeInterval = 300
func due(_ id: String, _ offset: TimeInterval) -> ScheduledTaskWatchdog.Due {
    .init(id: id, name: id, at: t0.addingTimeInterval(offset), notify: true)
}

// A run past its time, reported by Mac MCP: one alert, never twice.
var w = ScheduledTaskWatchdog()
var alerts = w.answered(now: t0, overdue: [due("a", -600)], dueSoon: [], tolerance: tol)
check(alerts.map(\.kind) == [.notStarted] && alerts[0].task.id == "a", "an overdue run is announced")
check(w.answered(now: t0.addingTimeInterval(60), overdue: [due("a", -600)], dueSoon: [], tolerance: tol).isEmpty,
      "the same run is announced once")

// Right after waking the scheduler gets the tolerance to record what it slept through.
w = ScheduledTaskWatchdog()
w.lastWakeAt = t0
var found: [ScheduledTaskWatchdog.Alert] = []
for minute in 1...6 {  // the app asks once a minute
    let got = w.answered(now: t0.addingTimeInterval(Double(minute) * 60), overdue: [due("b", -900)], dueSoon: [],
                         tolerance: tol)
    if minute < 5 { check(got.isEmpty, "quiet right after waking (minute \(minute))") }
    found += got
}
check(found.count == 1, "still not started after the tolerance: announced once")

// Mac MCP answering again after a gap is the same: time to catch up first.
w = ScheduledTaskWatchdog()
_ = w.answered(now: t0, overdue: [], dueSoon: [], tolerance: tol)
// Ten minutes without an answer, then Mac MCP is back and asked every minute again.
found = []
for minute in 0...6 {
    let got = w.answered(now: t0.addingTimeInterval(600 + Double(minute) * 60), overdue: [due("c", 100)], dueSoon: [],
                         tolerance: tol)
    if minute < 5 { check(got.isEmpty, "quiet right after Mac MCP answers again (minute \(minute))") }
    found += got
}
check(found.count == 1, "announced once the scheduler had its time")

// Mac MCP stops answering: a run due since its last answer is at risk, unless it was stopped on purpose.
w = ScheduledTaskWatchdog()
_ = w.answered(now: t0, overdue: [], dueSoon: [due("d", 100), due("e", -50)], tolerance: tol)
check(w.unanswered(now: t0.addingTimeInterval(100 + tol - 1), intendedRunning: true, tolerance: tol).isEmpty,
      "not before the tolerance")
check(w.unanswered(now: t0.addingTimeInterval(100 + tol), intendedRunning: false, tolerance: tol).isEmpty,
      "the owner stopped Mac MCP: no alert")
alerts = w.unanswered(now: t0.addingTimeInterval(100 + tol), intendedRunning: true, tolerance: tol)
check(alerts.map(\.kind) == [.notAnswering] && alerts.map(\.task.id) == ["d"],
      "only the run due after the last answer is at risk")
check(w.unanswered(now: t0.addingTimeInterval(100 + tol + 60), intendedRunning: true, tolerance: tol).isEmpty,
      "announced once")
print("WATCHDOG_PASS")
'''


class WatchdogTests(unittest.TestCase):
    def test_watchdog_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "main.swift"
            harness.write_text(HARNESS, encoding="utf-8")
            binary = Path(td) / "watchdog"
            built = subprocess.run(["xcrun", "swiftc", str(SOURCE), str(harness), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(0, built.returncode, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
            self.assertIn("WATCHDOG_PASS", ran.stdout)


if __name__ == "__main__":
    unittest.main()
