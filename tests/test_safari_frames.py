from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from mcp_server import safari_frames, tools_browser_agent as agent
from mcp_server.browser_tabs import TabTarget

PAGE = "https://events.example/webinar"
FRAME = "https://sdk.forms.example/register?id=7"
ROOT = "w1/1/1/1/1/1/1/3/1"


def _nodes():
    return [
        {"id": ROOT, "role": "AXWebArea", "url": FRAME},
        {"id": f"{ROOT}/1/1", "role": "AXStaticText", "value": "First name"},
        {"id": f"{ROOT}/1/1/1", "role": "AXStaticText", "value": "First name"},
        {"id": f"{ROOT}/1/2", "role": "AXTextField", "title": "First name", "actions": ["AXPress"], "x": "40", "y": "275", "w": "156", "h": "21"},
        {"id": f"{ROOT}/1/4", "role": "AXTextField", "title": "Email", "value": "old@example.com"},
        {"id": f"{ROOT}/1/6", "role": "AXPopUpButton", "title": "Country", "value": "Choose…"},
        {"id": f"{ROOT}/1/7/1", "role": "AXCheckBox", "title": "Send me reminders", "value": "0"},
        {"id": f"{ROOT}/1/8", "role": "AXButton", "title": "Register", "description": "Register now"},
        {"id": f"{ROOT}/2", "role": "AXStaticText", "value": "Thanks, Ada!"},
    ]


def _frame_answer(**extra):
    return {"ok": True, "pid": 501, "frame_url": FRAME, "page_url": PAGE, "frame_id": ROOT, "nodes": _nodes(), **extra}


def _target(active=True):
    return TabTarget(browser="Safari", window_index=1, tab_index=2, tab_handle="safari:abc", native_id="",
                     title="Webinar", url=PAGE, active=active)


class SafariFrameObserveTests(unittest.TestCase):
    def test_controls_get_element_ids_and_text_is_read_once(self) -> None:
        with patch.object(safari_frames.ax_native, "observe_frame", return_value=_frame_answer()) as seen:
            out = safari_frames.observe(PAGE, "sdk.forms.example")
        self.assertEqual({"bundle_id": "com.apple.Safari", "page_url": PAGE, "frame": "sdk.forms.example"}, seen.call_args.kwargs)
        self.assertTrue(out["ok"])
        self.assertEqual("accessibility", out["transport"])
        self.assertEqual(FRAME, out["url"])
        self.assertEqual(["a1", "a2", "a3", "a4", "a5"], [e["element_id"] for e in out["elements"]])
        self.assertEqual(["textbox", "textbox", "select", "checkbox", "button"], [e["role"] for e in out["elements"]])
        self.assertEqual("old@example.com", out["elements"][1]["value"])
        self.assertFalse(out["elements"][3]["checked"])
        self.assertEqual({"x": 40, "y": 275, "w": 156, "h": 21}, out["elements"][0]["rect"])
        # The nested repeat of "First name" is not listed twice.
        self.assertEqual(["First name", "Thanks, Ada!"], [t["text"] for t in out["texts"]])

    def test_failures_say_why_and_what_to_do(self) -> None:
        with patch.object(safari_frames.ax_native, "observe_frame", return_value=None):
            self.assertEqual("frame_accessibility_unavailable", safari_frames.observe(PAGE, "x")["error"])
        hidden = {"ok": False, "reason_code": "FRAME_PAGE_NOT_SHOWN", "error": "not shown"}
        with patch.object(safari_frames.ax_native, "observe_frame", return_value=hidden):
            out = safari_frames.observe(PAGE, "x")
        self.assertEqual("frame_page_not_shown", out["error"])
        self.assertIn("browser_activate_tab", out["hint"])
        missing = {"ok": False, "reason_code": "FRAME_NOT_FOUND", "error": "none", "frames": [FRAME]}
        with patch.object(safari_frames.ax_native, "observe_frame", return_value=missing):
            self.assertEqual([FRAME], safari_frames.observe(PAGE, "nope")["frames"])


class SafariFrameActTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.object(safari_frames.ax_native, "observe_frame", return_value=_frame_answer()):
            self.observation = safari_frames.observe(PAGE, "sdk.forms")["observation_id"]

    def _act(self, actions, answers=None):
        calls = []

        def act(**kwargs):
            calls.append(kwargs)
            return (answers or {}).get(kwargs["op"], {"ok": True, "verified": True})

        with patch.object(safari_frames.ax_native, "act", side_effect=act), \
                patch.object(safari_frames.ax_native, "observe_frame", return_value=_frame_answer()):
            out = safari_frames.act(PAGE, "sdk.forms", actions, self.observation)
        return out, calls

    def test_fill_by_label_uses_the_right_operation_for_each_control(self) -> None:
        out, calls = self._act([{"type": "fill", "values": {
            "first name": "Ada", "Email": "ada@example.com", "Country": "Turkey", "Send me reminders": True}}])
        self.assertTrue(out["ok"])
        self.assertEqual(["set_text", "set_text", "select_option", "press"], [c["op"] for c in calls])
        self.assertEqual(f"{ROOT}/1/2", calls[0]["element_id"])
        self.assertEqual("Ada", calls[0]["text"])
        self.assertEqual("Turkey", calls[2]["text"])
        self.assertEqual(501, calls[0]["app_pid"])
        # The helper refuses an element that is no longer the one observed.
        self.assertEqual({"role": "AXTextField", "title": "First name"}, calls[0]["expect"])
        self.assertEqual("Thanks, Ada!", out["state"]["texts"][-1]["text"])

    def test_a_box_already_in_the_wanted_state_is_left_alone(self) -> None:
        out, calls = self._act([{"type": "uncheck", "element_id": "a4"}])
        self.assertTrue(out["ok"])
        self.assertEqual([], calls)

    def test_click_select_and_expectations(self) -> None:
        out, calls = self._act([{"type": "select", "element_id": "a3", "option": "Germany"},
                                {"type": "click", "element_id": "a5"}])
        self.assertTrue(out["ok"])
        self.assertEqual(("select_option", "Germany"), (calls[0]["op"], calls[0]["text"]))
        self.assertEqual({"role": "AXButton", "title": "Register", "description": "Register now"}, calls[1]["expect"])

    def test_stops_at_the_first_failure(self) -> None:
        out, calls = self._act([{"type": "select", "element_id": "a3", "option": "Narnia"},
                                {"type": "click", "element_id": "a5"}],
                               answers={"select_option": {"ok": False, "reason_code": "OPTION_NOT_FOUND",
                                                          "options": ["Turkey"]}})
        self.assertFalse(out["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(["Turkey"], out["results"][0]["options"])

    def test_unknown_labels_ids_and_types_are_errors(self) -> None:
        out, calls = self._act([{"type": "fill", "values": {"Phone": "1"}}])
        self.assertEqual("field_not_found", out["results"][0]["fields"][0]["error"])
        self.assertIn("Email", out["results"][0]["fields"][0]["available"])
        out, _ = self._act([{"type": "click", "element_id": "a99"}])
        self.assertEqual("unknown_element_id", out["results"][0]["error"])
        out, _ = self._act([{"type": "scroll", "direction": "down"}])
        self.assertEqual("unsupported_in_accessibility_frame", out["results"][0]["error"])
        self.assertEqual([], calls)

    def test_an_observation_of_another_frame_is_refused(self) -> None:
        with patch.object(safari_frames.ax_native, "act") as act:
            out = safari_frames.act(PAGE, "other.example", [{"type": "click", "element_id": "a5"}], self.observation)
        self.assertEqual("stale_observation", out["error"])
        act.assert_not_called()


class SafariFrameRoutingTests(unittest.TestCase):
    @staticmethod
    def _lease(target):
        @contextmanager
        def lease(*args, **kwargs):
            yield target
        return lease

    def test_safari_frames_go_through_accessibility(self) -> None:
        with patch.object(agent, "_tab_lease", self._lease(_target())), \
                patch.object(agent.safari_frames, "observe", return_value={"ok": True, "url": FRAME}) as observe, \
                patch.object(agent, "_browser_observe_impl") as dom:
            out = json.loads(agent.browser_observe(None, "Safari", tab_handle="safari:abc", frame="sdk.forms"))
        dom.assert_not_called()
        self.assertEqual((PAGE, "sdk.forms", agent._DEFAULT_OBSERVE_ELEMENTS), observe.call_args.args)
        self.assertEqual({"selector": "sdk.forms", "url": FRAME, "transport": "accessibility"}, out["frame"])

    def test_observing_may_bind_the_tab_and_acting_may_not(self) -> None:
        leases = []

        @contextmanager
        def lease(*args, **kwargs):
            leases.append(kwargs)
            yield _target()

        with patch.object(agent, "_tab_lease", lease), patch.object(agent, "_require_stable_handle_for_mutation"), \
                patch.object(agent.safari_frames, "observe", return_value={"ok": True}), \
                patch.object(agent.safari_frames, "act", return_value={"ok": True}):
            agent.browser_observe(None, "Safari", tab_handle="safari:abc", frame="sdk.forms")
            agent.browser_act(None, "Safari", [{"type": "click", "element_id": "a1"}], tab_handle="safari:abc", frame="sdk.forms")
        self.assertEqual([{"allow_rebind": True, "mutation": False}, {"allow_rebind": False, "mutation": True}], leases)

    def test_a_tab_its_window_does_not_show_is_explained(self) -> None:
        with patch.object(agent, "_tab_lease", self._lease(_target(active=False))), \
                patch.object(agent.safari_frames, "observe") as observe:
            out = json.loads(agent.browser_observe(None, "Safari", tab_handle="safari:abc", frame="sdk.forms"))
        observe.assert_not_called()
        self.assertEqual("frame_tab_not_shown", out["error"])

    def test_act_needs_a_stable_handle_and_routes_to_accessibility(self) -> None:
        with patch.object(agent, "_tab_lease", self._lease(_target())), \
                patch.object(agent, "_require_stable_handle_for_mutation") as stable, \
                patch.object(agent.safari_frames, "act", return_value={"ok": True, "url": FRAME}) as act:
            out = agent.browser_act(None, "Safari", [{"type": "click", "element_id": "a5"}], observation_id="axf_1",
                                    tab_handle="safari:abc", frame="sdk.forms")
        stable.assert_called_once()
        self.assertEqual((PAGE, "sdk.forms", [{"type": "click", "element_id": "a5"}], "axf_1", "compact"), act.call_args.args)
        self.assertEqual("safari:abc", out["tab_handle"])

    def test_chrome_and_frameless_calls_are_unchanged(self) -> None:
        with patch.object(agent.safari_frames, "observe") as observe, \
                patch.object(agent, "_browser_observe_impl", return_value='{"ok":true}'):
            agent.browser_observe(None, "Google Chrome", tab_handle="t", frame="pay.example")
            agent.browser_observe(None, "Safari", tab_handle="t")
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
