"""#25: a question answered while the island shows or queues it leaves at once.

The island shows each new question (a security approval, a Safari upload) as a five-second peek
and queues the next ones. An answer used to leave the peek on screen until its time ran out, and
a queued one still came up later, saying "Waiting for your answer" for something no longer waiting.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
ISLAND = (SOURCES / "LiveIsland.swift").read_text(encoding="utf-8")

HARNESS = r'''
@main
struct WaitingPeekHarness {
    static func main() {
        let waiting: Set<String> = ["approval-q2", "review-t1"]
        for id in ["approval-q1", "approval-q2", "review-t1", "review-t9", "agent-done-a1", "task-done-t3"] {
            print(id, LivePeekQueue.isStale(id, waiting: waiting))
        }
    }
}
'''


class WaitingPeekTests(unittest.TestCase):
    def test_only_questions_and_reviews_no_longer_waiting_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, binary = Path(temp) / "harness.swift", Path(temp) / "harness"
            source.write_text(HARNESS, encoding="utf-8")
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(SOURCES / "AppModels+Live.swift"), str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, out.returncode, out.stderr)
        result = dict(line.split() for line in out.stdout.splitlines())
        self.assertEqual({"approval-q1": "true", "approval-q2": "false", "review-t1": "false", "review-t9": "true",
                          "agent-done-a1": "false", "task-done-t3": "false"}, result)

    def test_each_poll_drops_stale_peeks_from_the_queue_and_the_screen(self) -> None:
        poll = ISLAND[ISLAND.index("let waiting = attention + reviews"):ISLAND.index("attentionSeeded = true")]
        self.assertIn("peekQueue.removeAll { LivePeekQueue.isStale($0.id, waiting: waitingIDs) }", poll)
        shown = poll[poll.index("if let shown = model.peek, LivePeekQueue.isStale(shown.id, waiting: waitingIDs) {"):]
        self.assertLess(shown.index("peekTask?.cancel()"), shown.index("showNextPeek()"))


if __name__ == "__main__":
    unittest.main()
