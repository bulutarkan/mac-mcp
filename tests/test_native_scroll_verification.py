"""#45: a native scroll reports whether it moved, hit an edge, or could not be verified."""
from __future__ import annotations

import itertools
import unittest
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.native_action_verification import scroll_outcome
from mcp_server.security import load_settings


def bars(vertical=None, horizontal=None, anchors=()):
    return {"scroll_area": True, "vertical": vertical, "horizontal": horizontal, "anchors": list(anchors)}


class ScrollOutcomeTests(unittest.TestCase):
    def test_moving_scroll_bar_is_observed(self) -> None:
        out = scroll_outcome(bars(0.2), bars(0.45), "down")
        self.assertEqual("scroll_observed", out["verification"])
        self.assertTrue(out["effect_observed"])
        self.assertEqual((0.2, 0.45), (out["offset_before"], out["offset_after"]))

    def test_edges_are_reported_as_boundaries_not_failures(self) -> None:
        self.assertEqual("scroll_at_boundary", scroll_outcome(bars(1.0), bars(1.0), "down")["verification"])
        self.assertEqual("scroll_at_boundary", scroll_outcome(bars(0.0), bars(0.0), "up")["verification"])
        # The top is not a boundary for scrolling down.
        self.assertEqual("scroll_not_observed", scroll_outcome(bars(0.0), bars(0.0), "down")["verification"])

    def test_unmoved_bar_away_from_an_edge_is_no_effect(self) -> None:
        out = scroll_outcome(bars(0.5), bars(0.5), "down")
        self.assertEqual("scroll_not_observed", out["verification"])
        self.assertFalse(out["effect_observed"])
        self.assertEqual("ACTION_NO_EFFECT", out["reason_code"])

    def test_axes_are_verified_independently(self) -> None:
        moved_vertically = (bars(0.2, 0.5), bars(0.6, 0.5))
        self.assertEqual("scroll_observed", scroll_outcome(*moved_vertically, "down")["verification"])
        self.assertEqual("scroll_not_observed", scroll_outcome(*moved_vertically, "right")["verification"])
        self.assertEqual("horizontal", scroll_outcome(*moved_vertically, "left")["axis"])

    def test_content_position_shows_movement_without_a_scroll_bar(self) -> None:
        out = scroll_outcome(bars(anchors=[(10, 100), (10, 140)]), bars(anchors=[(10, -60), (10, -20)]), "down")
        self.assertEqual("scroll_observed", out["verification"])
        self.assertEqual("content_position", out["signal"])

    def test_unreadable_or_unmoved_content_is_unverified_never_no_effect(self) -> None:
        still = scroll_outcome(bars(anchors=[(10, 100)]), bars(anchors=[(10, 100)]), "down")
        self.assertEqual("scroll_unverified", still["verification"])
        self.assertIsNone(still["effect_observed"])
        self.assertFalse(still["content_moved"])
        blind = scroll_outcome({}, {}, "down")
        self.assertEqual("scroll_unverified", blind["verification"])
        self.assertNotIn("reason_code", blind)


class ScrollProbeTests(unittest.TestCase):
    def test_probe_is_read_only(self) -> None:
        script = tools_ui._native_scroll_state_script("DemoApp", "w1/2/1", app_pid=7)
        self.assertIn('"AXVerticalScrollBar"', script)
        self.assertIn('"AXHorizontalScrollBar"', script)
        self.assertNotIn("perform action", script)
        self.assertNotIn("click", script)
        self.assertNotIn("set value", script)

    def test_parser_accepts_locale_decimals_and_missing_bars(self) -> None:
        raw = "\x1f".join(["__SCROLL__", "true", "0,25", "", "12,40;12,80;"])
        self.assertEqual(
            {"scroll_area": True, "vertical": 0.25, "horizontal": None, "anchors": [(12.0, 40.0), (12.0, 80.0)]},
            tools_ui._parse_native_scroll_state(raw),
        )
        self.assertEqual({}, tools_ui._parse_native_scroll_state("garbage"))


class ScrollActTests(unittest.TestCase):
    def _scroll(self, before, after):
        target = {"app": "DemoApp", "pid": 4321, "window_index": 1, "app_handle": "mapp", "window_handle": "mwin"}
        ready = {"ready": True, "state": {"connected": True}, "attempts": 1}
        probes = itertools.chain([before], itertools.repeat(after))
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_probe_native_scroll_state", side_effect=lambda *a, **k: next(probes)), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "semantic scroll completed")) as perform, \
             patch.object(tools_ui.time, "sleep"):
            result = tools_ui.act_ui(
                load_settings(), [{"type": "scroll", "element_id": "w1/2", "direction": "down"}],
                app="DemoApp", return_state=False, allow_risky=True, preserve_focus=False,
            )
        perform.assert_called_once()  # never scrolled twice, whatever the outcome
        return result

    def test_observed_scroll_succeeds(self) -> None:
        result = self._scroll(bars(0.1), bars(0.3))
        self.assertTrue(result["ok"], result)
        self.assertEqual("scroll_observed", result["actions"][0]["verification"])
        self.assertTrue(result["actions"][0]["effect_observed"])

    def test_scroll_at_the_bottom_is_ok_and_says_so(self) -> None:
        result = self._scroll(bars(1.0), bars(1.0))
        self.assertTrue(result["ok"], result)
        self.assertEqual("scroll_at_boundary", result["actions"][0]["verification"])
        self.assertTrue(result["actions"][0]["scroll"]["at_boundary"])

    def test_ignored_scroll_fails_without_automatic_retry(self) -> None:
        result = self._scroll(bars(0.5), bars(0.5))
        self.assertFalse(result["ok"])
        self.assertFalse(result["automatic_retry"])  # computer_plan never replays it
        failed = result["actions"][0]
        self.assertEqual("ACTION_NO_EFFECT", failed["reason_code"])
        self.assertFalse(failed["automatic_retry"])
        self.assertTrue(failed["observe_again"])

    def test_unverifiable_scroll_stays_successful(self) -> None:
        result = self._scroll({}, {})
        self.assertTrue(result["ok"], result)
        self.assertEqual("scroll_unverified", result["actions"][0]["verification"])


if __name__ == "__main__":
    unittest.main()
