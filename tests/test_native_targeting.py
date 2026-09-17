from __future__ import annotations

import unittest
from unittest.mock import patch

from mcp_server import native_targets, tools_ui
from mcp_server.security import load_settings


def metadata(*, pid: int = 111, windows: list[dict] | None = None) -> dict:
    return native_targets.decorate_metadata({
        "active_app": "DemoApp",
        "pid": pid,
        "bundle_id": "com.example.demo",
        "frontmost": True,
        "window_count": len(windows or []),
        "window_names": [str(row.get("title") or "") for row in (windows or [])],
        "windows": windows or [],
    })


def window(index: int, title: str, x: int, *, document: str = "", identifier: str = "") -> dict:
    return {
        "index": index,
        "title": title,
        "document": document,
        "identifier": identifier,
        "position": {"x": x, "y": 50, "width": 700, "height": 500},
        "subrole": "AXStandardWindow",
        "focused": index == 1,
        "main": index == 1,
    }


class NativeTargetIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        native_targets.reset_registries_for_tests()

    def test_app_handle_is_process_bound(self) -> None:
        first = native_targets.app_handle("DemoApp", 111, "com.example.demo")
        self.assertEqual(first, native_targets.app_handle("DemoApp", 111, "com.example.demo"))
        self.assertNotEqual(first, native_targets.app_handle("DemoApp", 222, "com.example.demo"))
        self.assertNotEqual(
            native_targets.app_handle("DemoApp", 111, "com.example.demo", "start-a"),
            native_targets.app_handle("DemoApp", 111, "com.example.demo", "start-b"),
        )

    def test_unique_title_handle_survives_window_reorder_and_move(self) -> None:
        first = metadata(windows=[window(1, "Editor", 20), window(2, "Preview", 800)])
        handles = {row["title"]: row["window_handle"] for row in first["windows"]}
        second = metadata(windows=[window(1, "Preview", 40), window(2, "Editor", 960)])
        second_handles = {row["title"]: row["window_handle"] for row in second["windows"]}
        self.assertEqual(handles, second_handles)
        self.assertTrue(all(row["identity_kind"] == "title" for row in second["windows"]))

    def test_duplicate_titles_fall_back_to_frame_fingerprint(self) -> None:
        result = metadata(windows=[window(1, "Untitled", 20), window(2, "Untitled", 800)])
        self.assertNotEqual(result["windows"][0]["window_handle"], result["windows"][1]["window_handle"])
        self.assertTrue(all(row["identity_kind"] == "fingerprint" for row in result["windows"]))
        self.assertTrue(all(row["identity_status"] == "conservative_fingerprint" for row in result["windows"]))

    def test_indistinguishable_windows_get_no_handle(self) -> None:
        same = window(1, "Untitled", 20)
        duplicate = dict(same)
        duplicate["index"] = 2
        result = metadata(windows=[same, duplicate])
        self.assertIsNone(result["windows"][0]["window_handle"])
        self.assertIsNone(result["windows"][1]["window_handle"])
        self.assertEqual("ambiguous", result["windows"][0]["identity_status"])

    def test_document_identity_beats_title(self) -> None:
        result = metadata(windows=[
            window(1, "Untitled", 20, document="file:///tmp/a.txt"),
            window(2, "Untitled", 800, document="file:///tmp/b.txt"),
        ])
        self.assertTrue(all(row["identity_kind"] == "document" for row in result["windows"]))

    def test_rebase_element_id_changes_only_window_root(self) -> None:
        self.assertEqual("w3/2/7", native_targets.rebase_element_id("w1/2/7", 3))


class NativeTargetActionTests(unittest.TestCase):
    def setUp(self) -> None:
        native_targets.reset_registries_for_tests()
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()
        self.settings = load_settings()

    def _observation(self, meta: dict, *, window_index: int = 1) -> str:
        nodes = [{
            "element_id": f"w{window_index}/1" if window_index else "w1/1",
            "parent_id": f"w{window_index}" if window_index else "w1",
            "role": "AXButton",
            "subrole": "",
            "title": "Safe button",
            "description": "",
            "value": "",
            "position": {"x": 100, "y": 100, "width": 50, "height": 20},
            "enabled": True,
            "focused": False,
            "actions": ["AXPress"],
            "child_count": 0,
        }]
        return tools_ui._save_observation("DemoApp", window_index, nodes, meta)

    def test_action_re_resolves_handle_and_rebases_element_after_reorder(self) -> None:
        first = metadata(windows=[window(1, "Editor", 20), window(2, "Preview", 800)])
        observation_id = self._observation(first)
        reordered = metadata(windows=[window(1, "Preview", 800), window(2, "Editor", 20)])

        ready = {
            "ready": True,
            "state": {
                "connected": True, "role": "AXButton", "subrole": "", "title": "Safe button",
                "value": "", "character_count": 0, "selected": False, "enabled": True,
                "position": {"x": 100, "y": 100, "width": 50, "height": 20},
                "window_position": {"x": 20, "y": 20, "width": 700, "height": 500},
                "window_title": "Editor", "window_count": 2, "window_child_count": 1,
                "sheet_count": 0, "popover_count": 0, "menu_count": 0,
            },
        }
        verified = {"effect_observed": True, "verification": "target_state_changed", "attempts": 1}
        with patch.object(tools_ui, "_scan_native_windows", return_value=(reordered, None)), \
             patch.object(tools_ui, "_wait_for_native_readiness", return_value=ready), \
             patch.object(tools_ui, "_perform_action", return_value=(True, "ok")) as perform, \
             patch.object(tools_ui, "_wait_for_native_effect", return_value=verified):
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=observation_id,
                return_state=False,
                allow_risky=True,
            )

        self.assertTrue(result["ok"])
        called_action = perform.call_args.args[1]
        self.assertEqual("w2/1", called_action["element_id"])
        self.assertEqual(111, perform.call_args.args[4])
        self.assertEqual("w2/1", result["actions"][0]["resolved_element_id"])
        self.assertEqual(2, result["actions"][0]["resolved_window_index"])

    def test_stale_window_handle_fails_before_action(self) -> None:
        first = metadata(windows=[window(1, "Editor", 20)])
        observation_id = self._observation(first)
        changed = metadata(windows=[window(1, "Different", 20)])
        with patch.object(tools_ui, "_scan_native_windows", return_value=(changed, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=observation_id,
                return_state=False,
                allow_risky=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("STALE_WINDOW_HANDLE", result["reason_code"])
        perform.assert_not_called()

    def test_process_restart_fails_before_action(self) -> None:
        first = metadata(windows=[window(1, "Editor", 20)])
        observation_id = self._observation(first)
        restarted = metadata(pid=222, windows=[window(1, "Editor", 20)])
        with patch.object(tools_ui, "_scan_native_windows", return_value=(restarted, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=observation_id,
                return_state=False,
                allow_risky=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("STALE_APP_HANDLE", result["reason_code"])
        perform.assert_not_called()

    def test_ambiguous_observed_window_fails_closed(self) -> None:
        same = window(1, "Untitled", 20)
        duplicate = dict(same)
        duplicate["index"] = 2
        first = metadata(windows=[same, duplicate])
        observation_id = self._observation(first)
        with patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "click", "element_id": "w1/1"}],
                observation_id=observation_id,
                return_state=False,
                allow_risky=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("WINDOW_IDENTITY_UNAVAILABLE", result["reason_code"])
        perform.assert_not_called()

    def test_stable_window_blocks_unbound_key_before_action(self) -> None:
        first = metadata(windows=[window(1, "Editor", 20)])
        observation_id = self._observation(first)
        with patch.object(tools_ui, "_scan_native_windows", return_value=(first, None)), \
             patch.object(tools_ui, "_perform_action") as perform:
            result = tools_ui.act_ui(
                self.settings,
                [{"type": "key", "key": "return"}],
                observation_id=observation_id,
                return_state=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual("WINDOW_BOUND_ACTION_REQUIRES_ELEMENT", result["reason_code"])
        perform.assert_not_called()

    def test_pid_bound_target_script_does_not_select_process_by_name(self) -> None:
        script = tools_ui._target_script("DemoApp", "w1/1", 'perform action "AXPress" of targetElement', app_pid=4321)
        self.assertIn("unix id is 4321", script)
        self.assertNotIn('whose name is "DemoApp"', script)


if __name__ == "__main__":
    unittest.main()
