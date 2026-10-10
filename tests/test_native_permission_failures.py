"""#47: Accessibility, Automation and Screen Recording failures are told apart."""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from mcp_server import permission_probe, tools_ui
from mcp_server.permission_probe import DENIED, GRANTED, UNKNOWN, explain_failure
from mcp_server.security import load_settings

FS, RS = "\x1f", "\x1e"
APPLE_EVENTS_DENIED = "execution error: Not authorized to send Apple events to System Events. (-1743)"
ASSISTIVE_DENIED = "System Events got an error: osascript is not allowed assistive access. (-25211)"


def _tree() -> str:
    meta = FS.join(["__META__", "DemoApp", "true", "1", "Main", "4321", "dev.demo"])
    node = FS.join(["__NODE__", "w1/1", "w1", "AXButton", "", "Save", "", "", "10", "10", "80", "20",
                    "true", "false", "AXPress", "0", ""])
    return RS.join([meta, node])


class ClassifierTests(unittest.TestCase):
    def test_apple_events_refusal_names_automation_and_the_target_app(self) -> None:
        out = explain_failure("accessibility", APPLE_EVENTS_DENIED)
        self.assertEqual("AUTOMATION_NOT_ALLOWED", out["reason_code"])
        self.assertIn("Privacy & Security > Automation", out["remediation"])
        self.assertIn("turn on System Events", out["remediation"])
        self.assertTrue(out["settings_url"].endswith("Privacy_Automation"))

    def test_assistive_access_refusal_names_accessibility(self) -> None:
        out = explain_failure("screen_recording", ASSISTIVE_DENIED)
        self.assertEqual("ACCESSIBILITY_NOT_ALLOWED", out["reason_code"])
        self.assertIn("Privacy & Security > Accessibility", out["remediation"])

    def test_state_decides_when_the_text_does_not(self) -> None:
        with patch.object(permission_probe, "screen_recording_state", return_value=DENIED):
            out = explain_failure("screen_recording", "could not create image from display")
        self.assertEqual("SCREEN_RECORDING_NOT_ALLOWED", out["reason_code"])
        self.assertIn("Screen & System Audio Recording", out["settings_path"])
        self.assertIn("OCR", out["degraded_features"])
        with patch.object(permission_probe, "accessibility_state", return_value=GRANTED):
            self.assertEqual({"permission": False, "capability": "accessibility"},
                             explain_failure("accessibility", "Invalid index. (-1719)"))
        with patch.object(permission_probe, "accessibility_state", return_value=UNKNOWN):
            self.assertEqual("unknown", explain_failure("accessibility", "boom")["permission"])

    def test_text_only_mode_never_reads_permission_state(self) -> None:
        with patch.object(permission_probe, "accessibility_state") as ax, \
             patch.object(permission_probe, "screen_recording_state") as sr:
            out = explain_failure("accessibility", "Invalid index. (-1719)", check_state=False)
        ax.assert_not_called()
        sr.assert_not_called()
        self.assertEqual("unknown", out["permission"])

    def test_classifier_never_prompts(self) -> None:
        source = inspect.getsource(permission_probe)
        self.assertNotIn("CGRequestScreenCaptureAccess", source)
        self.assertNotIn("AXIsProcessTrustedWithOptions", source)


class ObservationTests(unittest.TestCase):
    def _observe(self, *, tree, screenshot=(b"jpeg", None)):
        with patch.object(tools_ui, "_read_native_tree", return_value=tree), \
             patch.object(tools_ui, "_capture_screen", return_value=screenshot), \
             patch.object(tools_ui.ax_watch, "change_token", return_value=None):
            payload, _ = tools_ui._collect_observation(load_settings(), "DemoApp", 0, 4, 20, True, False)
        return payload

    def test_apple_events_refusal_is_not_reported_as_accessibility(self) -> None:
        payload = self._observe(tree=(False, "", APPLE_EVENTS_DENIED, "applescript"))
        self.assertFalse(payload["ok"])
        self.assertEqual("AUTOMATION_NOT_ALLOWED", payload["reason_code"])
        self.assertIn("Automation", payload["hint"])
        self.assertNotIn("> Accessibility", payload["hint"])

    def test_app_error_with_accessibility_allowed_sends_nobody_to_settings(self) -> None:
        with patch.object(permission_probe, "accessibility_state", return_value=GRANTED):
            payload = self._observe(tree=(False, "", "DemoApp got an error: Invalid index. (-1719)", "applescript"))
        self.assertEqual("APP_ACCESSIBILITY_ERROR", payload["reason_code"])
        self.assertNotIn("Privacy & Security", payload["hint"])

    def test_denied_accessibility_state_points_at_accessibility(self) -> None:
        with patch.object(permission_probe, "accessibility_state", return_value=DENIED):
            payload = self._observe(tree=(False, "", "System Events got an error.", "applescript"))
        self.assertEqual("ACCESSIBILITY_NOT_ALLOWED", payload["reason_code"])
        self.assertIn("Privacy & Security > Accessibility", payload["hint"])

    def test_screenshot_refusal_keeps_the_semantic_observation(self) -> None:
        with patch.object(permission_probe, "screen_recording_state", return_value=DENIED):
            payload = self._observe(tree=(True, _tree(), "", "applescript"),
                                    screenshot=(None, "could not create image from display"))
        self.assertTrue(payload["ok"])
        self.assertTrue(any(node.get("title") == "Save" for node in payload.get("nodes") or payload.get("elements") or []),
                        payload.keys())
        self.assertEqual("SCREEN_RECORDING_NOT_ALLOWED", payload["screenshot"]["reason_code"])
        self.assertIn("Screen & System Audio Recording", payload["screenshot"]["remediation"])
        self.assertEqual(["ocr", "screenshot"], payload["degraded"])

    def test_screenshot_failure_with_permission_granted_is_not_called_a_permission_problem(self) -> None:
        with patch.object(permission_probe, "screen_recording_state", return_value=GRANTED):
            payload = self._observe(tree=(True, _tree(), "", "applescript"),
                                    screenshot=(None, "screencapture timed out after 10s"))
        self.assertTrue(payload["ok"])
        self.assertNotIn("reason_code", payload["screenshot"])
        self.assertNotIn("degraded", payload)


class ActionTests(unittest.TestCase):
    def test_refused_apple_events_during_an_action_get_a_reason_and_no_replay(self) -> None:
        target = {"app": "DemoApp", "pid": 4321, "window_index": 1, "app_handle": "mapp", "window_handle": "mwin"}
        ready = {"ready": True, "state": {"connected": True}, "attempts": 1}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(False, APPLE_EVENTS_DENIED)):
            result = tools_ui.act_ui(
                load_settings(), [{"type": "accessibility_action", "element_id": "w1/1", "name": "AXPress"}],
                app="DemoApp", return_state=False, allow_risky=True, preserve_focus=False,
            )
        self.assertFalse(result["ok"])
        failed = result["actions"][0]
        self.assertEqual("AUTOMATION_NOT_ALLOWED", failed["reason_code"])
        self.assertFalse(failed["automatic_retry"])
        self.assertIn("System Events", failed["remediation"])


if __name__ == "__main__":
    unittest.main()
