from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server.native_action_verification import (
    effect_changed,
    readiness_reason,
    verification_required,
)
from mcp_server.security import load_settings
from mcp_server import tools_ui
from mcp_server.foreground_guard import foreground_authorization


def state(**overrides):
    base = {
        "connected": True,
        "role": "AXButton",
        "subrole": "",
        "title": "Continue",
        "value": "",
        "character_count": 0,
        "selected": False,
        "focused": False,
        "enabled": True,
        "hidden": None,
        "visible": None,
        "offscreen": None,
        "busy": False,
        "position": {"x": 100, "y": 100, "width": 120, "height": 30},
        "child_count": 0,
        "window_title": "Demo",
        "window_count": 1,
        "window_position": {"x": 20, "y": 20, "width": 800, "height": 600},
        "window_minimized": False,
        "window_child_count": 8,
        "sheet_count": 0,
        "popover_count": 0,
        "menu_count": 0,
        "in_sheet": False,
        "in_popover": False,
        "popover_covers_target": False,
        "modal_sheet_blocks_target": False,
    }
    base.update(overrides)
    return base


class NativeReadinessLogicTests(unittest.TestCase):
    def test_disabled_button_is_not_ready(self) -> None:
        self.assertEqual("ELEMENT_DISABLED", readiness_reason(state(enabled=False)))

    def test_zero_bounds_and_minimized_window_are_not_ready(self) -> None:
        self.assertEqual(
            "ELEMENT_ZERO_BOUNDS",
            readiness_reason(state(position={"x": 1, "y": 1, "width": 0, "height": 10})),
        )
        self.assertEqual("WINDOW_MINIMIZED", readiness_reason(state(window_minimized=True)))

    def test_modal_sheet_and_popover_geometry_block_underlying_target(self) -> None:
        self.assertEqual("ELEMENT_OCCLUDED", readiness_reason(state(modal_sheet_blocks_target=True)))
        self.assertEqual("ELEMENT_OCCLUDED", readiness_reason(state(popover_covers_target=True)))

    def test_stale_observation_identity_fails_closed(self) -> None:
        observed = {"role": "AXButton", "subrole": "", "title": "Continue"}
        self.assertEqual(
            "STALE_ELEMENT_PATH",
            readiness_reason(state(title="Delete"), observed_node=observed),
        )

    def test_effect_detection_is_action_specific(self) -> None:
        before = state(role="AXTextField", value="old", character_count=3)
        after = state(role="AXTextField", value="new value", character_count=9)
        changed, why = effect_changed(before, after, {"type": "type", "text": "new value"})
        self.assertTrue(changed)
        self.assertEqual("text_state_changed", why)

        changed, why = effect_changed(state(), state(sheet_count=1), {"type": "click"})
        self.assertTrue(changed)
        self.assertEqual("window_state_changed", why)

        changed, why = effect_changed(state(), state(connected=False), {"type": "menu"})
        self.assertTrue(changed)
        self.assertEqual("target_disconnected", why)

        changed, why = effect_changed(state(), state(), {"type": "click"})
        self.assertFalse(changed)
        self.assertEqual("state_unchanged", why)

    def test_toggle_effects_that_only_rename_the_control_or_rearrange_the_window_count(self) -> None:
        # #220: Calculator's sidebar button turns "Show Sidebar" into "Hide Sidebar" and keeps its title;
        # Finder's Preview button keeps its name and adds a pane a few levels inside the window.
        changed, why = effect_changed(state(title="", description="Show Sidebar"),
                                      state(title="", description="Hide Sidebar"), {"type": "click"})
        self.assertEqual((True, "target_state_changed"), (changed, why))
        changed, why = effect_changed(state(window_layout="|3:2,0,6|1:0"), state(window_layout="|3:3,0,6|1:0"),
                                      {"type": "click"})
        self.assertEqual((True, "window_state_changed"), (changed, why))
        # A side whose layout could not be read is not evidence either way.
        self.assertFalse(effect_changed(state(window_layout="|3:2,0,6"), state(), {"type": "click"})[0])

    def test_both_probes_read_the_same_window_layout(self) -> None:
        for script in (
            tools_ui._native_action_state_script("Finder", "w1/2/2", app_pid=627),
            tools_ui._native_effect_state_script("Finder", "w1/2/2", app_pid=627),
        ):
            self.assertIn(tools_ui._WINDOW_LAYOUT_SCRIPT.strip(), script)
        sep = tools_ui._FIELD_SEPARATOR
        effect = tools_ui._parse_native_effect_state(sep.join(["__EFFECT__", "true"] + [""] * 20 + ["|3:2,0"]))
        self.assertEqual("|3:2,0", effect["window_layout"])
        self.assertNotIn("window_layout", tools_ui._parse_native_effect_state(sep.join(["__EFFECT__", "true"] + [""] * 21)))
        ready = tools_ui._parse_native_action_state(sep.join(["__STATE__", "true"] + [""] * 36 + ["|3:2,0"]))
        self.assertEqual("|3:2,0", ready["window_layout"])

    def test_effect_state_merged_into_the_delta_reports_empty_attributes_as_empty(self) -> None:
        # The probe reads AppleScript's "missing value" for an empty title; observations report "".
        node = {"element_id": "w1/2/1", "role": "AXButton", "subrole": "", "title": "", "description": "Show Sidebar",
                "value": "", "position": {"x": 1, "y": 2, "width": 3, "height": 4}}
        merged = tools_ui._merge_effect_state_into_node(node, {
            "connected": True, "role": "AXButton", "subrole": "missing value", "title": "missing value",
            "description": "Hide Sidebar", "value": "missing value",
        })
        self.assertEqual(("", "", "", "Hide Sidebar"),
                         (merged["subrole"], merged["title"], merged["value"], merged["description"]))

    def test_the_delta_takes_only_what_the_action_changed_from_the_probe(self) -> None:
        # System Events reads a SwiftUI sidebar button's description as the generic "button" both before
        # and after the click; the observation's own label ("Usage") must survive the merge.
        node = {"element_id": "w2/1/8", "role": "AXButton", "subrole": "", "title": "", "description": "Usage",
                "value": "", "selected": False}
        before = {"connected": True, "role": "AXButton", "description": "button", "selected": False, "enabled": True}
        after = {"connected": True, "role": "AXButton", "description": "button", "selected": True, "enabled": True}
        merged = tools_ui._merge_effect_state_into_node(node, after, before)
        self.assertEqual(("Usage", True), (merged["description"], merged["selected"]))

    def test_a_pane_opening_inside_the_window_refreshes_the_whole_observation(self) -> None:
        # Finder's Preview pane: the button is unchanged, the window gained a pane (#220).
        self.assertTrue(tools_ui._effect_state_requires_structural_refresh(
            state(window_layout="|3:2,0,2"), state(window_layout="|3:3,0,2")))
        self.assertFalse(tools_ui._effect_state_requires_structural_refresh(
            state(window_layout="|3:2,0,2"), state(window_layout="|3:2,0,2")))

    def test_risky_words_match_where_a_word_starts_not_inside_another(self) -> None:
        # #223: "ode" (öde) inside "model", "Mode" and "Claude Code" made harmless controls risky.
        risky = ["Delete", "Delete All", "Send", "Send Message", "Pay", "Payment", "Buy now", "Sign Out",
                 "Sil", "Silinsin", "Öde", "Ödeme yap", "Gönder", "Satın Al", "ÇIKIŞ", "Çıkış yap", "Onayla",
                 "Move to Trash", "Submit"]
        harmless = ["Advanced, model, usage limit, check, time limit, cron", "Mode", "Claude Code", "Display",
                    "Encode", "Episode", "Codex", "Description", "Node"]
        for label in risky:
            self.assertTrue(tools_ui._is_risky_click({"description": label}), label)
        for label in harmless:
            self.assertFalse(tools_ui._is_risky_click({"description": label, "role": "AXButton"}), label)
        from mcp_server import decision_engine
        self.assertTrue(decision_engine.is_risky_label("Ödeme"))
        self.assertFalse(decision_engine.is_risky_label("Claude Code model"))

    def test_typing_text_the_field_already_holds_is_done_not_no_effect(self) -> None:
        held = state(role="AXTextArea", value="Summarize my week.", character_count=18)
        self.assertEqual((True, "text_already_present"),
                         effect_changed(held, dict(held), {"type": "type", "text": "Summarize my week."}))
        self.assertEqual((False, "text_state_unchanged"),
                         effect_changed(held, dict(held), {"type": "type", "text": "Something else"}))
        secure = state(role="AXSecureTextField", value="[redacted]")
        self.assertFalse(effect_changed(secure, dict(secure), {"type": "type", "text": "[redacted]"})[0])

    def test_verification_scope_keeps_non_element_actions_out(self) -> None:
        self.assertTrue(verification_required("click", "w1/1"))
        self.assertTrue(verification_required("paste", "w1/1"))
        self.assertFalse(verification_required("scroll", "w1/1"))
        self.assertFalse(verification_required("click", None))


class NativeVerificationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.target = {
            "app": "DemoApp",
            "pid": 4321,
            "window_index": 1,
            "app_handle": "mapp_test",
            "window_handle": "mwin_test",
        }

    def _act(self, action):
        return tools_ui.act_ui(
            self.settings,
            [action],
            app="DemoApp",
            return_state=False,
            allow_risky=True,
            preserve_focus=False,
        )

    def test_a_risky_click_later_in_a_batch_stops_it_before_anything_runs(self) -> None:
        # #224: the text was typed, then the risky click was refused, and the reply did not say the
        # typing had happened. Typed again, the field already held the text: "no effect".
        nodes = [
            {"element_id": "w1/1", "parent_id": "w1", "role": "AXTextArea", "subrole": "", "title": "",
             "description": "text entry area", "value": "", "position": {"x": 10, "y": 10, "width": 200, "height": 80},
             "enabled": True, "focused": False, "actions": [], "child_count": 0},
            {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "subrole": "", "title": "",
             "description": "Delete All", "value": "", "position": {"x": 10, "y": 100, "width": 80, "height": 24},
             "enabled": True, "focused": False, "actions": ["AXPress"], "child_count": 0},
        ]
        observation_id = tools_ui._save_observation("DemoApp", 1, nodes, {"pid": 4321, "app_handle": "mapp_test"})
        with patch.object(tools_ui, "_run_osascript") as script, \
                patch.object(tools_ui, "_resolve_action_native_target") as resolve:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "type", "element_id": "w1/1", "text": "Summarize my week."},
                 {"type": "click", "element_id": "w1/2"}],
                observation_id=observation_id, app="DemoApp", return_state=False, preserve_focus=False,
            )
        self.assertEqual((False, True, 1, "w1/2"),
                         (result["ok"], result["blocked"], result["failed_action_index"], result["element_id"]))
        self.assertEqual([], result["actions"])
        script.assert_not_called()
        resolve.assert_not_called()

    def test_readiness_wait_recovers_when_element_becomes_enabled(self) -> None:
        blocked = state(enabled=False)
        ready_state = state(enabled=True)
        with patch.object(tools_ui, "_probe_native_action_state", side_effect=[(blocked, None), (ready_state, None)]), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.2]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "DemoApp",
                {"type": "click", "element_id": "w1/1", "readiness_stable_ms": 0},
                {"role": "AXButton", "subrole": "", "title": "Continue"},
                app_pid=4321,
            )
        self.assertTrue(result["ready"])
        self.assertEqual(2, result["attempts"])

    def test_readiness_reports_why_when_no_time_is_left_for_another_look(self) -> None:
        # #222: a Save button below the window's edge. One probe of a big window takes ~0.85 s of
        # the 1.2 s budget; starting another one ended in "AppleScript timed out" (not retryable)
        # instead of the reason itself.
        outside = state(position={"x": 100, "y": 900, "width": 60, "height": 24})
        observed = {"role": "AXButton", "subrole": "", "title": "Continue"}
        with patch.object(tools_ui, "_probe_native_action_state", side_effect=[(outside, None)]) as probe, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.85]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "DemoApp", {"type": "click", "element_id": "w1/1"}, observed, app_pid=4321)
        self.assertEqual(1, probe.call_count)
        self.assertEqual(("ELEMENT_OUTSIDE_WINDOW", True, True),
                         (result["reason_code"], result["timed_out"], result["retryable"]))
        # A later probe that runs out of time also reports the reason found before it.
        with patch.object(tools_ui, "_probe_native_action_state",
                          side_effect=[(outside, None), (None, "AppleScript timed out after 0.25s")]), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 1.1]), \
             patch.object(tools_ui.time, "sleep"):
            late = tools_ui._wait_for_native_readiness(
                "DemoApp", {"type": "click", "element_id": "w1/1"}, observed, app_pid=4321)
        self.assertEqual(("ELEMENT_OUTSIDE_WINDOW", True), (late["reason_code"], late["timed_out"]))

    def test_the_first_look_may_outlast_the_readiness_budget(self) -> None:
        # One probe of Calculator's SwiftUI window takes 0.8-1.6 s. Cut at the 1.2 s budget it failed
        # ready keys with READINESS_PROBE_FAILED, so the click (and its desktop cursor) never happened.
        deadlines: list = []

        def probe(*_args, deadline=None, **_kwargs):
            deadlines.append(deadline)
            return state(), None

        with patch.object(tools_ui, "_probe_native_action_state", side_effect=probe), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 1.6, 1.7, 1.8]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "Calculator", {"type": "click", "element_id": "w1/1", "readiness_stable_ms": 0},
                {"role": "AXButton", "subrole": "", "title": "Continue"}, app_pid=4321)
        self.assertTrue(result["ready"])
        self.assertEqual(tools_ui._NATIVE_READINESS_FIRST_PROBE_S, deadlines[0])
        # The whole action's own deadline still bounds that first look.
        deadlines.clear()
        with patch.object(tools_ui, "_probe_native_action_state", side_effect=probe), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 1.6, 1.7, 1.8]), \
             patch.object(tools_ui.time, "sleep"):
            tools_ui._wait_for_native_readiness(
                "Calculator", {"type": "click", "element_id": "w1/1", "readiness_stable_ms": 0},
                {"role": "AXButton", "subrole": "", "title": "Continue"}, app_pid=4321, deadline=2.0)
        self.assertEqual(2.0, deadlines[0])

    def test_a_target_that_only_resized_gets_one_settling_look_past_the_budget(self) -> None:
        # The counter's press renamed it and narrowed it (78 -> 76 pt). Its geometry no longer
        # matched the observation, a second look was needed, and at ~0.7 s a probe it never fit in
        # the 1.2 s budget: the next press of the batch failed with ELEMENT_UNSTABLE.
        observed = {"role": "AXButton", "subrole": "", "title": "Count: 0", "identifier": "counter",
                    "identifier_unique": True, "position": {"x": 531, "y": 487, "width": 78, "height": 26}}
        renamed = state(title="Count: 1", identifier="counter",
                        position={"x": 531, "y": 487, "width": 76, "height": 26})
        with patch.object(tools_ui, "_probe_native_action_state", return_value=(renamed, None)) as probe, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.72, 1.5]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_readiness(
                "Cursor Lab", {"type": "click", "element_id": "w1/10"}, observed, app_pid=4321)
        self.assertTrue(result["ready"], result)
        self.assertEqual(2, probe.call_count)
        # Only one such look: a target still moving after it times out as before.
        moving = [state(position={"x": 100 + i, "y": 100, "width": 120, "height": 30}) for i in range(3)]
        with patch.object(tools_ui, "_probe_native_action_state", side_effect=[(m, None) for m in moving]) as probe, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.72, 1.5]), \
             patch.object(tools_ui.time, "sleep"):
            late = tools_ui._wait_for_native_readiness(
                "Cursor Lab", {"type": "click", "element_id": "w1/10"},
                {"role": "AXButton", "subrole": "", "title": "Continue"}, app_pid=4321)
        self.assertEqual(("ELEMENT_UNSTABLE", 2), (late["reason_code"], probe.call_count))

    def test_effect_wait_accepts_delayed_state_change(self) -> None:
        before = state()
        after = state(sheet_count=1)
        with patch.object(tools_ui, "_probe_native_effect_state", side_effect=[(before, None), (after, None)]), \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.2]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_effect(
                "DemoApp", {"type": "click", "element_id": "w1/1"}, before, app_pid=4321
            )
        self.assertTrue(result["effect_observed"])
        self.assertEqual("window_state_changed", result["verification"])
        self.assertEqual(2, result["attempts"])

    def test_effect_wait_times_out_without_replaying_action(self) -> None:
        before = state()
        with patch.object(tools_ui, "_probe_native_effect_state", return_value=(before, None)) as probe, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.7]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_effect(
                "DemoApp", {"type": "click", "element_id": "w1/1", "verify_timeout_s": 0.55}, before, app_pid=4321
            )
        self.assertFalse(result["effect_observed"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(2, probe.call_count)

    def test_a_press_that_changes_another_part_of_the_window_has_an_effect(self) -> None:
        # A Calculator key changes the display and a list row a status line, not themselves: the
        # target looked unchanged, so working presses were reported as ACTION_NO_EFFECT.
        before = state()
        with patch.object(tools_ui, "_probe_native_effect_state", return_value=(before, None)), \
             patch.object(tools_ui, "_native_window_content_digest", return_value="after") as digest, \
             patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.15]), \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui._wait_for_native_effect(
                "Calculator", {"type": "click", "element_id": "w1/1"}, before, app_pid=4321,
                window_before="before")
        self.assertTrue(result["effect_observed"])
        self.assertEqual("window_content_changed", result["verification"])
        digest.assert_called_once()
        # The same window text is still no effect; without a native reading nothing changes either.
        for now_digest in ("before", None):
            with patch.object(tools_ui, "_probe_native_effect_state", return_value=(before, None)), \
                 patch.object(tools_ui, "_native_window_content_digest", return_value=now_digest), \
                 patch.object(tools_ui.time, "perf_counter", side_effect=[0.0, 0.1, 0.15, 0.7, 0.75]), \
                 patch.object(tools_ui.time, "sleep"):
                result = tools_ui._wait_for_native_effect(
                    "Calculator", {"type": "click", "element_id": "w1/1", "verify_timeout_s": 0.55}, before,
                    app_pid=4321, window_before="before")
            self.assertEqual("ACTION_NO_EFFECT", result["reason_code"], now_digest)

    def test_window_digest_follows_the_window_text(self) -> None:
        nodes = [{"element_id": "w1/1", "role": "AXStaticText", "value": "12"},
                 {"element_id": "w1/2", "role": "AXButton", "description": "8"}]

        def digest(display: str):
            shown = [{**nodes[0], "value": display}, nodes[1]]
            with patch.object(tools_ui.ax_native, "observe", return_value=(True, "raw", "")), \
                 patch.object(tools_ui, "_parse_observation", return_value=({}, shown)):
                return tools_ui._native_window_content_digest("Calculator", "w1/2", app_pid=4321)

        self.assertEqual(digest("12"), digest("12"))
        self.assertNotEqual(digest("12"), digest("128"))
        with patch.object(tools_ui.ax_native, "observe", return_value=None):
            self.assertIsNone(tools_ui._native_window_content_digest("Calculator", "w1/2", app_pid=4321))
        self.assertIsNone(tools_ui._native_window_content_digest("Calculator", "w1/2", app_pid=None))

    def test_a_unique_identifier_outweighs_a_label_the_press_changed(self) -> None:
        # Cursor Lab: "Count: 0" became "Count: 1" after the first press, so the second press of the
        # same batch stopped with STALE_ELEMENT_PATH although the identifier still named that button.
        stored = {"nodes": {
            "w1/1": {"element_id": "w1/1", "parent_id": "w1", "role": "AXButton", "title": "Count: 0",
                     "identifier": "counter"},
            "w1/2": {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Row 1"},
        }}
        observed = tools_ui._native_identity_node(stored, stored["nodes"]["w1/1"])
        self.assertTrue(observed["identifier_unique"])
        pressed = state(title="Count: 1", identifier="counter")
        self.assertIsNone(readiness_reason(pressed, observed_node=observed))
        self.assertEqual("STALE_ELEMENT_PATH",
                         readiness_reason(state(title="Count: 1", identifier="other"), observed_node=observed))
        # Rows sharing one identifier: the label still tells them apart.
        shared = {"nodes": {
            "w1/1": {"element_id": "w1/1", "parent_id": "w1", "role": "AXButton", "title": "Row 1", "identifier": "row"},
            "w1/2": {"element_id": "w1/2", "parent_id": "w1", "role": "AXButton", "title": "Row 2", "identifier": "row"},
        }}
        row = tools_ui._native_identity_node(shared, shared["nodes"]["w1/1"])
        self.assertNotIn("identifier_unique", row)
        self.assertEqual("STALE_ELEMENT_PATH",
                         readiness_reason(state(title="Row 2", identifier="row"), observed_node=row))

    def test_readiness_probe_is_side_effect_free(self) -> None:
        script = tools_ui._native_action_state_script("DemoApp", "w1/1", app_pid=4321)
        self.assertIn("AXElementBusy", script)
        self.assertIn("AXOffScreen", script)
        self.assertIn("count of sheets of w", script)
        self.assertNotIn('perform action "AXPress"', script)
        self.assertNotIn("click targetElement", script)
        self.assertNotIn("keystroke", script)

    def test_disabled_element_never_executes_action(self) -> None:
        blocked = {
            "ready": False,
            "reason_code": "ELEMENT_DISABLED",
            "retryable": True,
            "state": {"enabled": False},
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=blocked), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertFalse(result["ok"])
        self.assertEqual("ELEMENT_DISABLED", result["reason_code"])
        perform.assert_not_called()

    def test_click_without_observable_effect_is_not_success(self) -> None:
        ready = {"ready": True, "state": state(), "stable_for_ms": 150, "attempts": 2}
        no_effect = {
            "effect_observed": False,
            "verification": "no_effect_after_bounded_wait",
            "reason_code": "ACTION_NO_EFFECT",
            "automatic_retry": False,
            "attempts": 4,
            "duration_ms": 550,
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=no_effect):
            result = self._act({"type": "click", "element_id": "w1/1"})
        perform.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertEqual("ACTION_NO_EFFECT", result["reason_code"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual("no_effect_after_bounded_wait", result["actions"][0]["verification"])

    def test_delayed_click_effect_is_accepted_without_second_action(self) -> None:
        ready = {"ready": True, "state": state(), "stable_for_ms": 150, "attempts": 2}
        effect = {
            "effect_observed": True,
            "verification": "window_state_changed",
            "attempts": 3,
            "duration_ms": 210,
        }
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic click completed")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["actions"][0]["effect_observed"])
        self.assertEqual("window_state_changed", result["actions"][0]["verification"])
        perform.assert_called_once()

    def test_a_press_reads_the_window_before_it_and_hands_that_to_the_effect_check(self) -> None:
        ready = {"ready": True, "state": state(), "stable_for_ms": 150, "attempts": 1}
        effect = {"effect_observed": True, "verification": "window_content_changed", "attempts": 1,
                  "duration_ms": 90}
        order: list = []
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_native_window_content_digest",
                          side_effect=lambda *a, **k: order.append("digest") or "before"), \
             patch.object(tools_ui, "_perform_action",
                          side_effect=lambda *a, **k: order.append("press") or (True, "semantic click completed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect) as wait:
            result = self._act({"type": "click", "element_id": "w1/1"})
        self.assertEqual(["digest", "press"], order)
        self.assertEqual("before", wait.call_args.kwargs["window_before"])
        self.assertTrue(result["ok"])
        self.assertEqual("window_content_changed", result["actions"][0]["verification"])
        # Typing checks its own field: no window reading for it.
        typed = {"ready": True, "state": state(role="AXTextField", value="a", character_count=1), "stable_for_ms": 150}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=typed), \
             patch.object(tools_ui, "_native_window_content_digest") as digest, \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")), \
             patch.object(tools_ui, "_wait_for_native_effect",
                          return_value={"effect_observed": True, "verification": "text_state_changed"}):
            self._act({"type": "type", "element_id": "w1/1", "text": "ab"})
        digest.assert_not_called()

    def test_a_shortcut_reports_whether_the_window_changed_apart_from_being_sent(self) -> None:
        # #41: a shortcut has no element to check, so it only said the keys were sent.
        def run(digests):
            readings = iter(digests)
            # An app-level shortcut the owner allowed to use the keyboard (shortcuts need the foreground).
            app_target = {key: value for key, value in self.target.items() if key != "window_handle"}
            with foreground_authorization("test"), \
                 patch.object(tools_ui, "_resolve_action_native_target", return_value=(app_target, None)), \
                 patch.object(tools_ui, "_native_window_content_digest", side_effect=lambda *a, **k: next(readings)), \
                 patch.object(tools_ui, "_perform_action", return_value=(True, "key sent")) as perform, \
                 patch.object(tools_ui, "cancellable_sleep"):
                result = self._act({"type": "shortcut", "key": "n", "modifiers": ["command"]})
            perform.assert_called_once()  # never sent again
            return result

        changed = run(["before", "after"])
        self.assertTrue(changed["ok"])
        self.assertEqual((True, "window_content_changed"),
                         (changed["actions"][0]["effect_observed"], changed["actions"][0]["verification"]))
        quiet = run(["before", "before", "before"])
        self.assertTrue(quiet["ok"], "an unseen effect does not fail the shortcut")
        self.assertEqual((False, "unverified"),
                         (quiet["actions"][0]["effect_observed"], quiet["actions"][0]["verification"]))

    def test_typing_requires_observable_text_change(self) -> None:
        ready = {
            "ready": True,
            "state": state(role="AXTextField", value="before", character_count=6),
            "stable_for_ms": 150,
        }
        effect = {"effect_observed": True, "verification": "text_state_changed", "attempts": 1, "duration_ms": 20}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(self.target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "text typed")), \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=effect):
            result = self._act({"type": "type", "element_id": "w1/1", "text": "after"})
        self.assertTrue(result["ok"])
        self.assertEqual("text_state_changed", result["actions"][0]["verification"])


if __name__ == "__main__":
    unittest.main()
