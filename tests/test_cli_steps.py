"""Live steps: a spinner per step, fixed with ✓ and its duration, or ✗ and the reason."""
from __future__ import annotations

import io
import re
import sys
import unittest

from mcp_server.cli_style import Style, strip_ansi


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def visible(text: str) -> str:
    seen = "\n".join(line.rsplit("\r", 1)[-1] for line in strip_ansi(text).split("\n"))
    return re.sub(r"\d+\.\d+s", "0.0s", seen)


class StepsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = FakeTTY()
        self.style = Style.for_stream(self.stream, env={"TERM": "xterm-256color"}, width=60)

    def test_a_step_is_fixed_with_a_tick_and_its_duration(self) -> None:
        steps = self.style.steps()
        with steps.step("Starting the server") as state:
            print("mac-mcp started (pid 4321).")
        self.assertTrue(state.ok)
        self.assertEqual("mac-mcp started (pid 4321).\n", state.captured)
        self.assertEqual("  ✓ Starting the server" + " " * 17 + "0.0s\n", visible(self.stream.getvalue()))

    def test_a_failed_step_shows_what_it_printed(self) -> None:
        steps = self.style.steps()
        with steps.step("Starting the server") as state:
            print("configured port 8765 is used by Python (pid 99).")
            state.ok = False
        self.assertEqual(
            "  ✗ Starting the server\n"
            "      configured port 8765 is used by Python (pid 99).\n",
            visible(self.stream.getvalue()),
        )

    def test_ctrl_c_fixes_the_line_restores_the_cursor_and_propagates(self) -> None:
        steps = self.style.steps()
        with self.assertRaises(KeyboardInterrupt):
            with steps.step("Restarting"):
                raise KeyboardInterrupt
        text = self.stream.getvalue()
        self.assertIn("✗ Restarting", visible(text))
        self.assertIn("\x1b[?25h", text.rsplit("✗", 1)[1])  # the cursor is shown again

    def test_stdout_is_restored_after_a_step(self) -> None:
        before = sys.stdout
        with self.style.steps().step("x"):
            pass
        self.assertIs(before, sys.stdout)

    def test_a_failed_step_can_read_as_an_instruction(self) -> None:
        with self.style.steps().step("Started the server", failed_label="Start the server") as state:
            state.ok = False
        self.assertIn("✗ Start the server", visible(self.stream.getvalue()))

    def test_begin_end_for_steps_driven_from_outside(self) -> None:
        steps = self.style.steps()
        steps.begin("Stopping the server")
        steps.end(True)
        steps.begin("Starting the server")
        steps.end(False, "health check timed out")
        self.assertEqual(
            "  ✓ Stopping the server" + " " * 17 + "0.0s\n"
            "  ✗ Starting the server\n"
            "      health check timed out\n",
            visible(self.stream.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
