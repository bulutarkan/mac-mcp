"""#185 follow-up: scrolling SwiftUI (and background AppKit) scroll areas.

Their AXScroll*ByPage actions fail with "cannot complete", do nothing, or page the
other way. A scroll now moves the area's scroll bar by a page when the page action did
not, never reports a scroll the other way as success, and reads the labels that tell
twin scroll areas apart through the native observer, within the readiness budget.
"""
from __future__ import annotations

import itertools
import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.native_action_verification import scroll_outcome
from mcp_server.security import load_settings

FS, RS = chr(31), chr(30)


def bars(vertical=None, horizontal=None):
    return {"scroll_area": True, "vertical": vertical, "horizontal": horizontal, "anchors": []}


def node(element_id, role, description="", value=""):
    fields = ["__NODE__", element_id, element_id.rsplit("/", 1)[0], role, "", "", description, value,
              "0", "0", "10", "10", "true", "false", "", "0", ""]
    return FS.join(fields)


class WrongDirectionTests(unittest.TestCase):
    def test_a_bar_moving_the_other_way_is_not_the_scroll_asked_for(self) -> None:
        up_instead = scroll_outcome(bars(0.5), bars(0.4), "down")
        self.assertEqual("scroll_wrong_direction", up_instead["verification"])
        self.assertFalse(up_instead["effect_observed"])
        self.assertEqual("SCROLL_WRONG_DIRECTION", up_instead["reason_code"])
        self.assertEqual("scroll_wrong_direction", scroll_outcome(bars(0.2), bars(0.3), "up")["verification"])
        self.assertEqual("scroll_wrong_direction", scroll_outcome(bars(None, 0.6), bars(None, 0.5), "right")["verification"])
        self.assertEqual("scroll_observed", scroll_outcome(bars(None, 0.6), bars(None, 0.5), "left")["verification"])

    def test_act_reports_it_as_a_failure_without_a_retry(self) -> None:
        result = _act(bars(0.5), bars(0.4))
        self.assertFalse(result["ok"])
        failed = result["actions"][0]
        self.assertEqual("SCROLL_WRONG_DIRECTION", failed["reason_code"])
        self.assertFalse(failed["automatic_retry"])
        self.assertTrue(failed["observe_again"])


class ScrollBarFallbackTests(unittest.TestCase):
    def _script(self, direction: str, stdout: str = "") -> tuple:
        scripts = []

        def run(script, timeout_s=None):
            scripts.append(script)
            return True, stdout, ""

        with patch.object(tools_ui, "_run_osascript", side_effect=run):
            result = tools_ui._scroll("DemoApp", {"direction": direction, "element_id": "w1/2", "pages": 2},
                                      None, app_pid=7, activate_target=False)
        return result, scripts[0]

    def test_the_bar_moves_from_where_it_started_only_when_the_page_action_did_not(self) -> None:
        _, script = self._script("down")
        self.assertIn('value of attribute "AXVerticalScrollBar" of cur', script)
        self.assertIn("if not (barAfter > barBefore + 0.001) then", script)
        self.assertIn("set targetValue to barBefore + (knobShare / (1 - knobShare)) * 0.9 * 2", script)
        self.assertIn('set value of attribute "AXValue" of barRef to targetValue', script)
        # The page action still runs first; its error only counts when nothing moved the bar.
        self.assertLess(script.index("perform action scrollAction"), script.index('set value of attribute "AXValue"'))
        self.assertIn('if outcomeText is "page_action" and actionError is not "" then error actionError', script)

    def test_a_page_is_the_knobs_share_of_the_bar(self) -> None:
        _, script = self._script("down")
        # `item 2 of (size of x)` does not evaluate inside System Events: read the size first.
        self.assertIn("set knobSize to size of knobRef", script)
        self.assertIn("set knobShare to (item 2 of knobSize) / (item 2 of barSize)", script)
        _, sideways = self._script("left")
        self.assertIn('"AXHorizontalScrollBar"', sideways)
        self.assertIn("(item 1 of knobSize) / (item 1 of barSize)", sideways)
        self.assertIn("set targetValue to barBefore - (", sideways)

    def test_at_the_edge_nothing_is_sent(self) -> None:
        _, down = self._script("down")
        self.assertIn('if barBefore ≥ 0.999 then return "at_edge"', down)
        self.assertLess(down.index('return "at_edge"'), down.index("perform action scrollAction"))
        _, up = self._script("up")
        self.assertIn('if barBefore ≤ 0.001 then return "at_edge"', up)

    def test_the_result_says_which_way_it_scrolled(self) -> None:
        (ok, message), _ = self._script("down", "scroll_bar_value")
        self.assertTrue(ok)
        self.assertEqual(tools_ui._SCROLL_BAR_FALLBACK_MESSAGE, message)
        (ok, message), _ = self._script("down", "at_edge")
        self.assertEqual((True, "already at that edge, so nothing was scrolled"), (ok, message))
        (ok, message), _ = self._script("down", "page_action")
        self.assertEqual((True, "semantic scroll completed"), (ok, message))

    def test_act_marks_the_fallback_in_the_scroll_details(self) -> None:
        result = _act(bars(0.0), bars(0.18), message=tools_ui._SCROLL_BAR_FALLBACK_MESSAGE)
        self.assertTrue(result["ok"], result)
        self.assertEqual("scroll_bar_value", result["actions"][0]["scroll"]["method"])
        self.assertEqual("scroll_observed", result["actions"][0]["verification"])


class ScrollStateParserTests(unittest.TestCase):
    def test_a_bar_survives_empty_trailing_fields(self) -> None:
        # osascript output is stripped, and strip() also eats the ASCII 31 separators.
        trimmed = FS.join(["__SCROLL__", "true", "0,25"])
        self.assertEqual({"scroll_area": True, "vertical": 0.25, "horizontal": None, "anchors": []},
                         tools_ui._parse_native_scroll_state(trimmed))
        self.assertEqual({}, tools_ui._parse_native_scroll_state("__SCROLL__"))


class NativeSubtreeLabelTests(unittest.TestCase):
    def _probe(self, native):
        scripts = []

        def run(script, timeout_s=None):
            scripts.append(script)
            state = ["__STATE__", "true", "AXScrollArea"] + [""] * 35
            return True, FS.join(state), ""

        with patch.object(tools_ui.ax_native, "observe", return_value=native), \
             patch.object(tools_ui, "_run_osascript", side_effect=run):
            state, error = tools_ui._probe_native_action_state("DemoApp", "w1/2", app_pid=7, label_limit=3)
        self.assertIsNone(error)
        return state, scripts[0]

    def records(self, budget_hit: bool = False) -> str:
        return RS.join([
            node("w1/2", "AXScrollArea", "scroll area"),
            node("w1/2/1", "AXStaticText", "", "Row zero"),
            node("w1/2/2", "AXStaticText", "", "Row one"),
            FS.join(["__BUDGET__", "1", "40", "1500", "true" if budget_hit else "false"]),
        ])

    def test_labels_come_from_the_native_observer_and_applescript_skips_them(self) -> None:
        state, script = self._probe((True, self.records(), ""))
        self.assertEqual(["Row zero", "Row one"], state["subtree_labels"])
        self.assertNotIn("on axLabel", script)

    def test_applescript_reads_them_when_the_native_read_is_incomplete_or_unavailable(self) -> None:
        for native in (None, (False, "", "boom"), (True, self.records(budget_hit=True), ""), (True, "", "")):
            with self.subTest(native=native):
                _, script = self._probe(native)
                self.assertIn("on axLabel", script)

    def test_no_native_read_when_no_labels_are_needed(self) -> None:
        with patch.object(tools_ui.ax_native, "observe") as observe, \
             patch.object(tools_ui, "_run_osascript", return_value=(True, "", "")):
            tools_ui._probe_native_action_state("DemoApp", "w1/2", app_pid=7, label_limit=0)
        observe.assert_not_called()


class ActionListTests(unittest.TestCase):
    def test_applescript_action_names_are_joined_with_the_comma_python_splits_on(self) -> None:
        scripts = [
            tools_ui._observation_script("DemoApp", 1, 3, 10, app_pid=7),
            tools_ui._native_action_state_script("DemoApp", "w1/2", app_pid=7),
        ]
        for script in scripts:
            self.assertIn("set AppleScript's text item delimiters to \",\"\n", script.replace("\r", ""))
            self.assertNotIn("set actionText to name of actions of targetElement as text", script)


def _act(before, after, message="semantic scroll completed"):
    target = {"app": "DemoApp", "pid": 4321, "window_index": 1, "app_handle": "mapp", "window_handle": "mwin"}
    ready = {"ready": True, "state": {"connected": True}, "attempts": 1}
    probes = itertools.chain([before], itertools.repeat(after))
    with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
         patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
         patch.object(tools_ui, "_probe_native_scroll_state", side_effect=lambda *a, **k: next(probes)), \
         patch.object(tools_ui, "_perform_action", return_value=(True, message)) as perform, \
         patch.object(tools_ui.time, "sleep"):
        result = tools_ui.act_ui(
            load_settings(), [{"type": "scroll", "element_id": "w1/2", "direction": "down"}],
            app="DemoApp", return_state=False, allow_risky=True, preserve_focus=False,
        )
    perform.assert_called_once()
    return result


if __name__ == "__main__":
    unittest.main()
