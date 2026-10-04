from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import native_window_capture, tools_ui
from mcp_server.security import load_settings


def ax_window(
    *,
    title: str = "Editor",
    x: int = 100,
    y: int = 80,
    width: int = 700,
    height: int = 500,
) -> dict:
    return {
        "index": 1,
        "title": title,
        "document": "",
        "identifier": "",
        "position": {"x": x, "y": y, "width": width, "height": height},
        "subrole": "AXStandardWindow",
        "focused": False,
        "main": False,
    }


def cg_row(
    window_id: int,
    *,
    pid: int = 111,
    title: str = "Editor",
    x: int = 100,
    y: int = 80,
    width: int = 700,
    height: int = 500,
    layer: int = 0,
) -> dict:
    return {
        "id": window_id,
        "pid": pid,
        "owner": "DemoApp",
        "title": title,
        "layer": layer,
        "onScreen": True,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


class NativeWindowIdResolutionTests(unittest.TestCase):
    def test_unique_pid_and_frame_resolves_window_id(self) -> None:
        window_id, error, meta = native_window_capture.resolve_window_id(
            111,
            ax_window(),
            rows=[cg_row(900), cg_row(901, pid=222)],
        )
        self.assertEqual(900, window_id)
        self.assertIsNone(error)
        self.assertEqual(1, meta["candidate_count"])

    def test_title_breaks_same_frame_tie(self) -> None:
        window_id, error, _ = native_window_capture.resolve_window_id(
            111,
            ax_window(title="Editor"),
            rows=[
                cg_row(900, title="Preview"),
                cg_row(901, title="Editor"),
            ],
        )
        self.assertEqual(901, window_id)
        self.assertIsNone(error)

    def test_indistinguishable_same_frame_windows_fail_closed(self) -> None:
        window_id, error, meta = native_window_capture.resolve_window_id(
            111,
            ax_window(),
            rows=[cg_row(900), cg_row(901)],
        )
        self.assertIsNone(window_id)
        self.assertEqual("WINDOW_CAPTURE_TARGET_AMBIGUOUS", error)
        self.assertEqual(2, meta["candidate_count"])

    def test_wrong_pid_never_matches_same_geometry(self) -> None:
        window_id, error, _ = native_window_capture.resolve_window_id(
            111,
            ax_window(),
            rows=[cg_row(900, pid=222)],
        )
        self.assertIsNone(window_id)
        self.assertEqual("WINDOW_CAPTURE_TARGET_NOT_FOUND", error)

    def test_missing_ax_frame_fails_without_guessing(self) -> None:
        target = ax_window()
        target["position"]["width"] = None
        window_id, error, _ = native_window_capture.resolve_window_id(
            111, target, rows=[cg_row(900)]
        )
        self.assertIsNone(window_id)
        self.assertEqual("WINDOW_CAPTURE_FRAME_UNAVAILABLE", error)

    def test_window_list_failure_is_propagated(self) -> None:
        with patch.object(
            native_window_capture,
            "_window_rows",
            return_value=(None, "WINDOW_CAPTURE_WINDOW_LIST_FAILED"),
        ):
            window_id, error, _ = native_window_capture.resolve_window_id(
                111, ax_window()
            )
        self.assertIsNone(window_id)
        self.assertEqual("WINDOW_CAPTURE_WINDOW_LIST_FAILED", error)


class NativeObserveWindowCaptureRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()

    @staticmethod
    def _metadata(windows: list[dict]) -> dict:
        return {
            "active_app": "DemoApp",
            "frontmost": False,
            "window_count": len(windows),
            "window_names": [row.get("title", "") for row in windows],
            "pid": 111,
            "bundle_id": "com.example.demo",
            "windows": windows,
        }

    @staticmethod
    def _node() -> dict:
        return {
            "element_id": "w1/1",
            "parent_id": "w1",
            "role": "AXButton",
            "subrole": "",
            "title": "Button",
            "description": "",
            "value": "",
            "position": {"x": 120, "y": 120, "width": 80, "height": 24},
            "enabled": True,
            "focused": False,
            "actions": ["AXPress"],
            "child_count": 0,
            "identifier": "",
        }

    def test_stable_selected_window_uses_targeted_capture(self) -> None:
        metadata = self._metadata([ax_window()])
        details = {
            "scope": "window",
            "capture_method": "cgwindow+screencapture",
            "match_basis": "pid+frame+title",
            "on_screen": True,
            "encoded_bytes": 4,
            "capture_duration_ms": 7,
        }
        with (
            patch.object(tools_ui, "_run_osascript", return_value=(True, "raw", None)),
            patch.object(tools_ui, "_parse_observation", return_value=(metadata, [self._node()])),
            patch.object(
                tools_ui,
                "_capture_window",
                return_value=(b"jpeg", None, details),
            ) as capture_window,
            patch.object(tools_ui, "_capture_screen") as capture_screen,
        ):
            payload, image = tools_ui._collect_observation(
                self.settings,
                "DemoApp",
                1,
                3,
                20,
                True,
                False,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(b"jpeg", image)
        self.assertEqual("window", payload["screenshot"]["scope"])
        self.assertEqual("cgwindow+screencapture", payload["screenshot"]["capture_method"])
        self.assertEqual(4, payload["telemetry"]["visual_bytes"])
        self.assertEqual(7, payload["telemetry"]["capture_duration_ms"])
        capture_window.assert_called_once()
        capture_screen.assert_not_called()

    def test_ambiguous_window_does_not_fallback_to_full_screen(self) -> None:
        first = ax_window()
        second = dict(first)
        second["index"] = 2
        metadata = self._metadata([first, second])

        with (
            patch.object(tools_ui, "_run_osascript", return_value=(True, "raw", None)),
            patch.object(tools_ui, "_parse_observation", return_value=(metadata, [self._node()])),
            patch.object(tools_ui, "_capture_window") as capture_window,
            patch.object(tools_ui, "_capture_screen") as capture_screen,
        ):
            payload, image = tools_ui._collect_observation(
                self.settings,
                "DemoApp",
                1,
                3,
                20,
                True,
                False,
            )

        self.assertTrue(payload["ok"])
        self.assertIsNone(image)
        self.assertEqual(
            "WINDOW_CAPTURE_IDENTITY_UNAVAILABLE",
            payload["screenshot"]["reason_code"],
        )
        self.assertIn("WINDOW_CAPTURE_IDENTITY_UNAVAILABLE", payload["screenshot"]["error"])
        capture_window.assert_not_called()
        capture_screen.assert_not_called()

    def test_all_windows_observe_retains_screen_capture(self) -> None:
        metadata = self._metadata([ax_window()])
        with (
            patch.object(tools_ui, "_run_osascript", return_value=(True, "raw", None)),
            patch.object(tools_ui, "_parse_observation", return_value=(metadata, [self._node()])),
            patch.object(tools_ui, "_capture_window") as capture_window,
            patch.object(
                tools_ui,
                "_capture_screen",
                return_value=(b"screen", None),
            ) as capture_screen,
        ):
            payload, image = tools_ui._collect_observation(
                self.settings,
                "DemoApp",
                0,
                3,
                20,
                True,
                False,
            )

        self.assertEqual(b"screen", image)
        self.assertEqual("screen", payload["screenshot"]["scope"])
        self.assertEqual("screencapture", payload["screenshot"]["capture_method"])
        capture_window.assert_not_called()
        capture_screen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
