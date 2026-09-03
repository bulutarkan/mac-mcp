import signal
import subprocess
import unittest
from unittest.mock import patch

from mcp_server import tools_interactive


class FakeProcess:
    def __init__(self, output="", timeout=False):
        self.pid = 12345
        self.returncode = 0
        self.output = output
        self.timeout = timeout
        self.wait_calls = 0

    def communicate(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("osascript", timeout)
        return self.output, ""

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.timeout and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("osascript", timeout)
        return self.returncode


class InteractiveToolTests(unittest.TestCase):
    def test_ask_choice_returns_selected_choice_and_index(self):
        process = FakeProcess("Review\n")
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process) as popen:
            result = tools_interactive.ask_choice(
                None,
                question="How should we continue?",
                choices=["Deploy", "Review"],
                sender="Test AI",
                default_choice="Review",
            )

        self.assertEqual(
            {
                "ok": True,
                "choice": "Review",
                "choice_index": 1,
                "cancelled": False,
                "timed_out": False,
            },
            result,
        )
        script = popen.call_args.args[0][2]
        self.assertIn('buttons {"Cancel", "Deploy", "Review"}', script)
        self.assertIn('default button "Review"', script)

    def test_three_choices_use_all_native_buttons_without_extra_cancel(self):
        process = FakeProcess("Ship\n")
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process) as popen:
            result = tools_interactive.ask_choice(
                None,
                question="What should we do?",
                choices=["Wait", "Review", "Ship"],
            )

        self.assertEqual(
            {
                "ok": True,
                "choice": "Ship",
                "choice_index": 2,
                "cancelled": False,
                "timed_out": False,
            },
            result,
        )
        script = popen.call_args.args[0][2]
        self.assertIn('buttons {"Wait", "Review", "Ship"}', script)
        self.assertNotIn('buttons {"Cancel", "Wait", "Review", "Ship"}', script)
        self.assertIn('default button "Wait"', script)

    def test_ask_confirmation_only_explicit_yes_confirms(self):
        process = FakeProcess("Yes\n")
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process) as popen:
            result = tools_interactive.ask_confirmation(None, "Proceed with the action?")

        self.assertTrue(result["confirmed"])
        self.assertEqual("confirmed", result["decision"])
        script = popen.call_args.args[0][2]
        self.assertIn('buttons {"No", "Yes"}', script)
        self.assertIn('default button "No"', script)

    def test_confirmation_denial_is_not_cancelled(self):
        process = FakeProcess("No\n")
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process):
            result = tools_interactive.ask_confirmation(None, "Proceed with the action?")

        self.assertEqual(
            {
                "ok": True,
                "confirmed": False,
                "decision": "denied",
                "cancelled": False,
                "timed_out": False,
            },
            result,
        )

    def test_timeout_is_fail_closed_and_terminates_process_group(self):
        process = FakeProcess(timeout=True)
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process), patch.object(
            tools_interactive.os, "killpg"
        ) as killpg:
            result = tools_interactive.ask_confirmation(None, "Proceed?", timeout_s=2)

        self.assertFalse(result["confirmed"])
        self.assertEqual("timed_out", result["decision"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(
            [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
            [call.args for call in killpg.call_args_list],
        )

    def test_invalid_choice_list_is_rejected_before_launching_dialog(self):
        with patch.object(tools_interactive.subprocess, "Popen") as popen:
            result = tools_interactive.ask_choice(None, "Pick one", ["Only one"])

        self.assertFalse(result["ok"])
        self.assertIn("between 2 and 3", result["error"])
        popen.assert_not_called()

    def test_four_choices_are_rejected_before_launching_dialog(self):
        with patch.object(tools_interactive.subprocess, "Popen") as popen:
            result = tools_interactive.ask_choice(None, "Pick one", ["A", "B", "C", "D"])

        self.assertFalse(result["ok"])
        self.assertIn("between 2 and 3", result["error"])
        popen.assert_not_called()

    def test_second_dialog_is_rejected_while_one_is_active(self):
        self.assertTrue(tools_interactive._DIALOG_LOCK.acquire(blocking=False))
        try:
            with patch.object(tools_interactive.subprocess, "Popen") as popen:
                result = tools_interactive.ask_choice(None, "Pick one", ["A", "B"])
        finally:
            tools_interactive._DIALOG_LOCK.release()

        self.assertEqual("prompt_busy", result["error"])
        popen.assert_not_called()

    def test_ask_user_keeps_legacy_response_shape(self):
        process = FakeProcess("hello\n")
        with patch.object(tools_interactive.subprocess, "Popen", return_value=process):
            result = tools_interactive.ask_user(None, "What is your answer?", timeout_s=2)

        self.assertEqual(
            {"ok": True, "response": "hello", "timed_out": False, "skipped": False},
            result,
        )


if __name__ == "__main__":
    unittest.main()
