"""#44: screenshot pixels map to desktop points; #128: off-screen captures are labelled, displays reported."""
from __future__ import annotations

import json
import struct
import unittest
from unittest.mock import patch

from mcp_server import native_window_capture, tools_ui
from mcp_server.security import load_settings

# Built-in Retina display (main) and an external 1x display placed to its left.
MAIN = {"id": 1, "main": True, "x": 0, "y": 0, "width": 1470, "height": 956, "scale": 2}
LEFT = {"id": 2, "main": False, "x": -1920, "y": -124, "width": 1920, "height": 1080, "scale": 1}


def _jpeg(width: int, height: int) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + sof + b"\xff\xd9"


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


class GeometryTests(unittest.TestCase):
    def test_image_size_is_read_from_the_header(self) -> None:
        self.assertEqual((1600, 941), tools_ui._image_size(_jpeg(1600, 941)))
        self.assertEqual((800, 600), tools_ui._image_size(_png(800, 600)))
        self.assertEqual((None, None), tools_ui._image_size(b"not an image"))

    def test_resized_retina_window_capture_maps_back_to_points(self) -> None:
        # A 1470x865-point window, captured at 2x (2940x1730) and resized to 1600 px wide.
        frame = tools_ui._coordinate_frame({"x": 0, "y": 33, "width": 1470, "height": 865}, (1600, 941), source="window")
        self.assertEqual({"x": 0.0, "y": 33.0}, frame["image_origin"])
        self.assertAlmostEqual(1470 / 1600, frame["points_per_pixel"]["x"], places=5)
        # The window's top-left pixel and its bottom-right pixel land on its corners.
        x = frame["image_origin"]["x"] + 1599 * frame["points_per_pixel"]["x"]
        y = frame["image_origin"]["y"] + 940 * frame["points_per_pixel"]["y"]
        self.assertAlmostEqual(1469.08, x, places=1)
        self.assertAlmostEqual(897.08, y, places=1)

    def test_display_left_of_main_keeps_its_negative_origin(self) -> None:
        frame = tools_ui._coordinate_frame({"x": -1920, "y": -124, "width": 1920, "height": 1080}, (1600, 900),
                                           source="window")
        self.assertEqual({"x": -1920.0, "y": -124.0}, frame["image_origin"])
        self.assertEqual(1.2, frame["points_per_pixel"]["x"])

    def test_no_frame_or_size_means_uncalibrated(self) -> None:
        self.assertIsNone(tools_ui._coordinate_frame(None, (100, 100), source="screen"))
        self.assertIsNone(tools_ui._coordinate_frame({"x": 0, "y": 0, "width": 10, "height": 10}, (None, None),
                                                     source="screen"))

    def test_display_for_frame_picks_the_display_holding_most_of_it(self) -> None:
        self.assertEqual(2, native_window_capture.display_for_frame([MAIN, LEFT], {"x": -300, "y": 100, "width": 400, "height": 200})["id"])
        self.assertEqual(1, native_window_capture.display_for_frame([MAIN, LEFT], {"x": -100, "y": 100, "width": 400, "height": 200})["id"])
        self.assertIsNone(native_window_capture.display_for_frame([MAIN, LEFT], {"x": 5000, "y": 0, "width": 10, "height": 10}))


class WindowListTests(unittest.TestCase):
    def test_helper_output_with_displays_and_the_old_list_are_both_read(self) -> None:
        window = {"id": 7, "pid": 42, "owner": "Demo", "title": "Main", "layer": 0, "onScreen": True,
                  "x": 0, "y": 33, "width": 800, "height": 600}
        for stdout, displays in ((json.dumps({"windows": [window], "displays": [MAIN]}), [MAIN]),
                                 (json.dumps([window]), [])):
            proc = type("Proc", (), {"returncode": 0, "stdout": stdout})()
            with patch.object(native_window_capture, "_helper_path", return_value=("/bin/true", None)), \
                 patch.object(native_window_capture.subprocess, "run", return_value=proc):
                rows, found, error = native_window_capture._window_list(2)
            self.assertIsNone(error)
            self.assertEqual([window], rows)
            self.assertEqual(displays, found)

    def test_resolution_reports_frame_display_and_a_window_on_no_display(self) -> None:
        rows = [{"id": 9, "pid": 42, "title": "Main", "layer": 0, "onScreen": True, "x": 6000, "y": 0, "width": 800, "height": 600}]
        with patch.object(native_window_capture, "_window_list", return_value=(rows, [MAIN, LEFT], None)):
            window_id, error, details = native_window_capture.resolve_window_id(
                42, {"title": "Main", "position": {"x": 6000, "y": 0, "width": 800, "height": 600}})
        self.assertEqual(9, window_id)
        self.assertEqual({"x": 6000.0, "y": 0.0, "width": 800.0, "height": 600.0}, details["frame"])
        self.assertIsNone(details["display"])
        self.assertFalse(details["on_screen"])


class CaptureLabelTests(unittest.TestCase):
    def _describe(self, details):
        observation_id = tools_ui._save_observation("Demo", 1, [], {"windows": []})
        with patch.object(tools_ui, "_native_displays", return_value=([MAIN, LEFT], None)):
            tools_ui._describe_capture_geometry(observation_id, details, _jpeg(1600, 941), None)
        return details, tools_ui._get_observation(observation_id)

    def test_off_screen_window_capture_is_not_presented_as_the_current_screen(self) -> None:
        details, _ = self._describe({"scope": "window", "on_screen": False,
                                     "frame": {"x": 0, "y": 33, "width": 1470, "height": 865}})
        self.assertEqual("not_on_screen", details["visual_state"])
        self.assertIn("last rendering", details["visual_note"])

    def test_screen_capture_uses_the_main_display_and_stores_the_frame(self) -> None:
        details, stored = self._describe({"scope": "screen"})
        self.assertEqual("current", details["visual_state"])
        self.assertEqual(1, details["display"]["id"])
        self.assertTrue(details["coordinates_calibrated"])
        self.assertEqual(details["coordinate_frame"], stored["capture_frame"])


class MappingTests(unittest.TestCase):
    FRAME = {"image_size": {"width": 1600, "height": 941}, "image_origin": {"x": 0.0, "y": 33.0},
             "points_per_pixel": {"x": 0.91875, "y": 0.919235}}

    def _map(self, action, stored=None, displays=(MAIN, LEFT)):
        with patch.object(tools_ui, "_native_displays", return_value=(list(displays) or None, None)):
            return tools_ui._map_action_coordinates(action, stored, None)

    def test_image_pixels_become_desktop_points(self) -> None:
        mapped, mapping, error = self._map({"type": "click", "x": 800, "y": 470, "coordinate_space": "image"},
                                           {"capture_frame": self.FRAME})
        self.assertIsNone(error)
        self.assertEqual((735, 465), (mapped["x"], mapped["y"]))
        self.assertNotIn("coordinate_space", mapped)
        self.assertEqual("on_display", mapping["display_check"])

    def test_drag_endpoints_are_both_mapped(self) -> None:
        mapped, _, error = self._map({"type": "drag", "from": {"x": 0, "y": 0}, "to_x": 100, "to_y": 100,
                                      "coordinate_space": "image"}, {"capture_frame": self.FRAME})
        self.assertIsNone(error)
        self.assertEqual({"x": 0, "y": 33}, mapped["from"])
        self.assertEqual({"x": 92, "y": 125}, mapped["to"])
        self.assertNotIn("to_x", mapped)

    def test_unmappable_points_fail_before_input(self) -> None:
        _, _, outside = self._map({"type": "click", "x": 1700, "y": 10, "coordinate_space": "image"},
                                  {"capture_frame": self.FRAME})
        self.assertEqual("COORDINATE_OUTSIDE_IMAGE", outside["reason_code"])
        _, _, no_frame = self._map({"type": "click", "x": 10, "y": 10, "coordinate_space": "image"}, {})
        self.assertEqual("COORDINATE_FRAME_UNAVAILABLE", no_frame["reason_code"])
        _, _, off = self._map({"type": "click", "x": 3000, "y": 10})
        self.assertEqual("COORDINATE_OFF_DISPLAY", off["reason_code"])
        _, _, bad = self._map({"type": "click", "x": 3, "y": 10, "coordinate_space": "retina"})
        self.assertEqual("COORDINATE_SPACE_INVALID", bad["reason_code"])

    def test_negative_coordinates_on_a_left_display_are_valid(self) -> None:
        mapped, mapping, error = self._map({"type": "click", "x": -500, "y": 300})
        self.assertIsNone(error)
        self.assertEqual((-500, 300), (mapped["x"], mapped["y"]))

    def test_unknown_display_layout_does_not_block_desktop_points(self) -> None:
        mapped, mapping, error = self._map({"type": "click", "x": 3000, "y": 10}, displays=())
        self.assertIsNone(error)
        self.assertEqual("unavailable", mapping["display_check"])

    def test_off_display_click_never_reaches_the_pointer(self) -> None:
        # Pointer coordinates are an app-level action: window-bound targets need an element_id.
        target = {"app": "Demo", "pid": 42, "window_index": 1, "app_handle": "mapp"}
        with patch.object(tools_ui, "_resolve_action_native_target", return_value=(target, None)), \
             patch.object(tools_ui, "_native_displays", return_value=([MAIN], None)), \
             patch.object(tools_ui, "_perform_action") as perform, \
             patch.object(tools_ui, "_run_cliclick") as cliclick:
            result = tools_ui.act_ui(load_settings(), [{"type": "click", "x": 5000, "y": 5000}], app="Demo",
                                     return_state=False, allow_risky=True, preserve_focus=False)
        self.assertFalse(result["ok"])
        self.assertEqual("COORDINATE_OFF_DISPLAY", result["reason_code"])
        perform.assert_not_called()
        cliclick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
