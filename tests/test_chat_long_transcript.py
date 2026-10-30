"""A long chat stays smooth while an answer streams (#121).

Laying out every turn on each change made a 500-item chat stall for hundreds of milliseconds per update.
Finished turns far from the owner's view are drawn as empty space of the height they were drawn at, so the
transcript keeps its exact height (a lazy stack's guessed heights made the scroll bar jump), and a streaming
answer redraws only its last Markdown block.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "menu_app" / "Sources"
TRANSCRIPT = (SOURCES / "ChatTranscript.swift").read_text(encoding="utf-8")
MARKDOWN = (SOURCES / "ChatMarkdown.swift").read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


class TranscriptShapeTests(unittest.TestCase):
    def test_history_and_the_last_turn_are_drawn_apart(self) -> None:
        stack = _section(TRANSCRIPT, "VStack(alignment: .leading, spacing: 26) {", 'Color.clear.frame(height: 1).id("end")')
        self.assertIn("TurnHistory(turns: model.turns.dropLast()", stack)
        self.assertIn("if let turn = model.turns.last {", stack)
        self.assertNotIn("ForEach(model.turns)", stack)

    def test_plain_stack_not_lazy(self) -> None:
        # A lazy stack re-estimates rows it has not drawn: the transcript jumped by whole screens while scrolling.
        self.assertNotIn("LazyVStack", TRANSCRIPT)

    def test_far_turns_are_space_of_their_drawn_height(self) -> None:
        history = _section(TRANSCRIPT, "private struct TurnHistory", "final class TurnHeights")
        self.assertIn("Color.clear.frame(height: height)", history)
        self.assertIn("heights.height(turn.id, revision: turn.revision)", history)
        self.assertIn("guard let band else { return [:] }", history)  # a chat just opened draws every turn

    def test_recording_a_height_never_draws_again(self) -> None:
        # A state change driven by layout made SwiftUI lay out forever (a0abd55): heights live in an object.
        self.assertIn("@MacMCPState private var heights = TurnHeights()", TRANSCRIPT)
        reader = _section(TRANSCRIPT, "private struct HeightReader", "// MARK: One turn")
        self.assertIn("heights.record(", reader)
        self.assertNotIn("@MacMCPState", reader)
        # Only a width change redraws, and only once, after layout.
        self.assertIn("widthChanged: { DispatchQueue.main.async { measuredAgain += 1 } }", TRANSCRIPT)

    def test_every_drawn_turn_reports_the_full_width(self) -> None:
        history = _section(TRANSCRIPT, "private struct TurnHistory", "final class TurnHeights")
        self.assertLess(history.index(".frame(maxWidth: .infinity, alignment: .leading)"), history.index("HeightReader("))


class MarkdownTests(unittest.TestCase):
    def test_blocks_redraw_only_when_they_change(self) -> None:
        view = _section(MARKDOWN, "struct MarkdownView: View", "private struct MarkdownTable")
        self.assertIn("MarkdownBlockView(block: block", view)
        self.assertIn(".equatable()", view)
        self.assertIn("private struct MarkdownBlockView: View, Equatable", view)

    def test_finished_text_is_parsed_once_and_streaming_text_never_kept(self) -> None:
        self.assertIn("streaming ? MarkdownParser.parse(text) : MarkdownParser.blocks(text)", MARKDOWN)
        streaming = _section(TRANSCRIPT, "private struct StreamingAnswer", "// MARK: Steps")
        self.assertIn("streaming: true", streaming)


HARNESS = r'''
import Foundation

@main
struct TurnHeightsHarness {
    static func main() {
        let heights = TurnHeights()
        precondition(heights.record("a", revision: 1, size: CGSize(width: 700, height: 120)) == false,
                     "the first width is no change")
        precondition(heights.height("a", revision: 1) == 120)
        precondition(heights.height("a", revision: 2) == nil, "a changed turn is drawn again")
        precondition(heights.record("b", revision: 1, size: CGSize(width: 700.3, height: 80)) == false)
        precondition(heights.height("a", revision: 1) == 120, "a fraction of a point is the same width")
        precondition(heights.record("b", revision: 1, size: CGSize(width: 500, height: 140)) == true)
        precondition(heights.height("a", revision: 1) == nil, "a new width makes every kept height stale")
        precondition(heights.height("b", revision: 1) == 140)
        print("ok")
    }
}
'''


@unittest.skipUnless(shutil.which("swiftc"), "needs swiftc")
class TurnHeightsHarnessTests(unittest.TestCase):
    def test_heights_follow_revision_and_width(self) -> None:
        source = _section(TRANSCRIPT, "final class TurnHeights", "private struct HeightReader")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "heights.swift").write_text("import AppKit\n" + source, encoding="utf-8")
            (Path(tmp) / "main.swift").write_text(HARNESS, encoding="utf-8")
            built = subprocess.run(["swiftc", "-parse-as-library", "-o", f"{tmp}/harness", f"{tmp}/heights.swift",
                                    f"{tmp}/main.swift"], capture_output=True, text=True, timeout=300)
            self.assertEqual(0, built.returncode, built.stderr)
            ran = subprocess.run([f"{tmp}/harness"], capture_output=True, text=True, timeout=30)
        self.assertEqual("ok", ran.stdout.strip(), ran.stderr)


if __name__ == "__main__":
    unittest.main()
