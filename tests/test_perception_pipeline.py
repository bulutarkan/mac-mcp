from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from mcp_server import tools_browser_agent, tools_snapshot, tools_ui
from mcp_server.computer_use_perf import record_computer_use_sample, reset_computer_use_samples
from mcp_server.perception import (
    PERCEPTION_LADDER,
    finalize_perception_telemetry,
    safe_perception_metrics,
)
from mcp_server.policy import PolicyContext, reset_policy_context, set_policy_context
from mcp_server.security import load_settings


class PerceptionTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_computer_use_samples()

    def test_unified_metrics_never_copy_observed_content(self) -> None:
        payload = {"secret_text": "do-not-copy", "telemetry": {}}
        finalize_perception_telemetry(
            payload,
            stage="semantic",
            state_mode="full",
            node_count=3,
            duration_ms=12,
        )
        safe = safe_perception_metrics(payload)
        self.assertNotIn("do-not-copy", json.dumps(safe))
        self.assertEqual("semantic", safe["perception_stage"])
        self.assertEqual("full", safe["state_mode"])
        self.assertEqual(3, safe["node_count"])
        self.assertGreater(safe["payload_bytes"], 0)
        self.assertGreater(safe["payload_tokens_estimate"], 0)

    def test_byte_budget_excess_is_explicit_without_copying_content(self) -> None:
        payload = {"blob": "x" * 10_000, "telemetry": {}}
        finalize_perception_telemetry(
            payload,
            stage="semantic",
            state_mode="full",
            context_budget_bytes=4096,
        )
        context = payload["telemetry"]["context_budget"]
        self.assertFalse(context["within_budget"])
        self.assertTrue(context["budget_exceeded"])
        self.assertIn("expand_hint", context)
        safe = safe_perception_metrics(payload)
        self.assertTrue(safe["context_budget_exceeded"])
        self.assertNotIn("x" * 20, json.dumps(safe))

    def test_rolling_benchmark_tracks_visual_nodes_tokens_and_modes(self) -> None:
        summary = record_computer_use_sample(
            "fixture99",
            duration_ms=20,
            payload_bytes=400,
            payload_tokens_estimate=100,
            visual_bytes=1200,
            node_count=9,
            state_mode="full",
        )
        self.assertEqual(1200.0, summary["avg_visual_bytes"])
        self.assertEqual(9.0, summary["avg_node_count"])
        self.assertEqual(100.0, summary["avg_payload_tokens_estimate"])
        self.assertEqual({"full": 1}, summary["state_modes"])

    def test_perception_ladder_is_semantic_first_and_ocr_last(self) -> None:
        self.assertEqual("snapshot", PERCEPTION_LADDER[0])
        self.assertLess(PERCEPTION_LADDER.index("semantic"), PERCEPTION_LADDER.index("targeted_visual"))
        self.assertEqual("ocr_full_visual", PERCEPTION_LADDER[-1])


class BrowserConditionalObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        reset_computer_use_samples()
        tools_browser_agent._BROWSER_OBSERVATION_OWNERS.clear()
        tools_browser_agent._remember_browser_observation("bobs_page_old")

    def test_unchanged_browser_dom_returns_not_modified_without_full_scan(self) -> None:
        conditional = {
            "ok": True,
            "known": True,
            "not_modified": True,
            "previous_observation_id": "bobs_page_old",
            "dom_revision": 7,
            "url": "https://example.test/",
            "title": "Example",
        }
        with patch.object(
            tools_browser_agent,
            "_resolve_tab_target",
            return_value=(1, 1),
        ), patch.object(
            tools_browser_agent,
            "_run_json_js",
            return_value=conditional,
        ) as run_js, patch.object(
            tools_browser_agent,
            "_observe_payload",
        ) as full_scan:
            raw = tools_browser_agent._browser_observe_locked(
                self.settings,
                "Safari",
                tab_handle="tab-a",
                previous_observation_id="bobs_page_old",
            )

        payload = json.loads(raw)
        self.assertTrue(payload["not_modified"])
        self.assertEqual("not_modified", payload["state_mode"])
        self.assertEqual([], payload["elements"])
        self.assertEqual("conditional", payload["telemetry"]["perception_stage"])
        self.assertEqual(1, payload["telemetry"]["remote_js_calls"])
        full_scan.assert_not_called()
        run_js.assert_called_once()

    def test_changed_observe_parameters_force_full_scan_even_same_revision(self) -> None:
        conditional = {
            "ok": True,
            "known": True,
            "compatible": False,
            "not_modified": False,
            "dom_revision": 7,
            "url": "https://example.test/",
            "title": "Example",
        }
        full = {
            "ok": True,
            "observation_id": "bobs_page_new",
            "dom_revision": 7,
            "url": "https://example.test/",
            "title": "Example",
            "scope": "visible",
            "element_count": 0,
            "elements": [],
            "viewport": {"w": 800, "h": 600},
            "scroll": {"x": 0, "y": 0},
            "_remote_js_calls": 1,
        }
        with patch.object(
            tools_browser_agent, "_resolve_tab_target", return_value=(1, 1)
        ), patch.object(
            tools_browser_agent, "_run_json_js", return_value=conditional
        ), patch.object(
            tools_browser_agent, "_observe_payload", return_value=full
        ) as scan:
            raw = tools_browser_agent._browser_observe_locked(
                self.settings,
                "Safari",
                tab_handle="tab-a",
                scope="visible",
                max_elements=60,
                previous_observation_id="bobs_page_old",
            )
        payload = json.loads(raw)
        self.assertEqual("full", payload["state_mode"])
        scan.assert_called_once()

    def test_changed_browser_dom_falls_through_to_semantic_full_scan(self) -> None:
        conditional = {
            "ok": True,
            "known": True,
            "not_modified": False,
            "dom_revision": 8,
            "url": "https://example.test/",
            "title": "Example",
        }
        full = {
            "ok": True,
            "observation_id": "bobs_page_new",
            "dom_revision": 8,
            "url": "https://example.test/",
            "title": "Example",
            "scope": "interactive",
            "element_count": 1,
            "elements": [{"element_id": "e_1", "text": "Submit", "actionable": True}],
            "viewport": {"w": 800, "h": 600},
            "scroll": {"x": 0, "y": 0},
            "_remote_js_calls": 1,
        }
        with patch.object(
            tools_browser_agent,
            "_resolve_tab_target",
            return_value=(1, 1),
        ), patch.object(
            tools_browser_agent,
            "_run_json_js",
            return_value=conditional,
        ), patch.object(
            tools_browser_agent,
            "_observe_payload",
            return_value=full,
        ):
            raw = tools_browser_agent._browser_observe_locked(
                self.settings,
                "Safari",
                tab_handle="tab-a",
                previous_observation_id="bobs_page_old",
            )

        payload = json.loads(raw)
        self.assertFalse(payload["not_modified"])
        self.assertEqual("full", payload["state_mode"])
        self.assertEqual("semantic", payload["telemetry"]["perception_stage"])
        self.assertEqual(1, payload["telemetry"]["node_count"])
        self.assertEqual(2, payload["telemetry"]["remote_js_calls"])
        self.assertEqual("bobs_page_old", payload["previous_observation_id"])

    def test_foreign_agent_cannot_reuse_browser_conditional_cache(self) -> None:
        tools_browser_agent._BROWSER_OBSERVATION_OWNERS.clear()
        token_a = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_perception_a",
                agent_id="agt_perception_a",
                team_id="team_perception",
            )
        )
        try:
            tools_browser_agent._remember_browser_observation("bobs_foreign")
        finally:
            reset_policy_context(token_a)

        full = {
            "ok": True,
            "observation_id": "bobs_agent_b",
            "dom_revision": 9,
            "url": "https://example.test/",
            "title": "Example",
            "scope": "interactive",
            "element_count": 0,
            "elements": [],
            "viewport": {"w": 800, "h": 600},
            "scroll": {"x": 0, "y": 0},
            "_remote_js_calls": 1,
        }
        token_b = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_perception_b",
                agent_id="agt_perception_b",
                team_id="team_perception",
            )
        )
        try:
            with patch.object(
                tools_browser_agent, "_resolve_tab_target", return_value=(1, 1)
            ), patch.object(
                tools_browser_agent, "_run_json_js"
            ) as conditional_probe, patch.object(
                tools_browser_agent, "_observe_payload", return_value=full
            ) as full_scan:
                raw = tools_browser_agent._browser_observe_locked(
                    self.settings,
                    "Safari",
                    tab_handle="tab-a",
                    previous_observation_id="bobs_foreign",
                )
        finally:
            reset_policy_context(token_b)

        payload = json.loads(raw)
        conditional_probe.assert_not_called()
        full_scan.assert_called_once()
        self.assertEqual("full", payload["state_mode"])
        self.assertEqual("bobs_foreign", payload["previous_observation_id"])

    def test_targeted_visual_reports_bytes_and_dimensions(self) -> None:
        full = {
            "ok": True,
            "observation_id": "bobs_visual",
            "dom_revision": 1,
            "url": "https://example.test/",
            "title": "Example",
            "scope": "interactive",
            "element_count": 1,
            "elements": [{
                "element_id": "e_1",
                "text": "Submit",
                "actionable": True,
                "viewport_rect": {"x": 10, "y": 10, "w": 120, "h": 30},
            }],
            "viewport": {"w": 800, "h": 600},
            "scroll": {"x": 0, "y": 0},
            "_remote_js_calls": 1,
        }
        capture_meta = {
            "capture_method": "dom_rasterizer",
            "background_safe": True,
            "tab_activated": False,
            "disk_write": False,
            "output_width": 120,
            "output_height": 30,
            "bytes": 4,
        }
        with patch.object(
            tools_browser_agent,
            "_resolve_tab_target",
            return_value=(1, 1),
        ), patch.object(
            tools_browser_agent,
            "_observe_payload",
            return_value=full,
        ), patch.object(
            tools_browser_agent,
            "_capture_dom_visual",
            return_value=(b"jpeg", None, capture_meta),
        ):
            result = tools_browser_agent._browser_observe_locked(
                self.settings,
                "Safari",
                tab_handle="tab-a",
                visual="element",
                element_id="e_1",
            )

        self.assertIsInstance(result, list)
        payload = json.loads(result[0])
        telemetry = payload["telemetry"]
        self.assertEqual("targeted_visual", telemetry["perception_stage"])
        self.assertEqual(4, telemetry["visual_bytes"])
        self.assertEqual({"width": 120, "height": 30}, telemetry["visual_dimensions"])


class CompactObservationOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        reset_computer_use_samples()

    def _full_scan(self, count: int) -> dict:
        return {
            "ok": True,
            "observation_id": "bobs_compact",
            "dom_revision": 3,
            "url": "https://example.test/form",
            "title": "Form",
            "scope": "interactive",
            "element_count": count,
            "elements": [
                {
                    "element_id": f"e_{index}", "tag": "button", "role": "button",
                    "text": f"Action {index}", "actionable": True, "enabled": True,
                    "viewport_rect": {"x": 10, "y": 20 * index, "w": 120, "h": 32},
                }
                for index in range(count)
            ],
            "viewport": {"w": 1280, "h": 800},
            "scroll": {"x": 0, "y": 0},
            "_remote_js_calls": 1,
        }

    def test_browser_observation_is_compact_sized_exactly_and_serialized_at_most_four_times(self) -> None:
        from mcp_server import perception

        calls = []
        real = perception.json_bytes

        def counting(value):
            calls.append(1)
            return real(value)

        with patch.object(tools_browser_agent, "_resolve_tab_target", return_value=(1, 1)), \
             patch.object(tools_browser_agent, "_observe_payload", return_value=self._full_scan(40)), \
             patch.object(perception, "json_bytes", side_effect=counting), \
             patch.object(tools_browser_agent, "json_bytes", side_effect=counting):
            raw = tools_browser_agent._browser_observe_locked(self.settings, "Safari", tab_handle="tab-a")

        self.assertLessEqual(len(calls), 4)
        self.assertNotIn("\n", raw)
        payload = json.loads(raw)
        self.assertEqual(len(raw.encode("utf-8")), payload["telemetry"]["payload_bytes"])
        self.assertIn("benchmark", payload["telemetry"])
        self.assertEqual(40, len(payload["elements"]))
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        self.assertLessEqual(len(raw.encode("utf-8")), 0.9 * len(pretty.encode("utf-8")))

    def test_native_result_text_is_compact_and_matches_reported_size(self) -> None:
        payload = {"ok": True, "nodes": [{"role": "AXButton", "title": f"Item {i}"} for i in range(500)]}
        finalize_perception_telemetry(payload, stage="semantic", state_mode="full", node_count=500)
        text = tools_ui._format_result(payload)
        self.assertNotIn("\n", text)
        self.assertEqual(len(text.encode("utf-8")), json.loads(text)["telemetry"]["payload_bytes"])


class NativePerceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()

    def test_ocr_is_skipped_when_accessibility_already_has_text(self) -> None:
        metadata = {
            "active_app": "Demo",
            "pid": 111,
            "bundle_id": "com.example.demo",
            "frontmost": False,
            "window_count": 1,
            "window_names": ["Demo"],
            "windows": [{
                "index": 1,
                "title": "Demo",
                "position": {"x": 10, "y": 10, "width": 500, "height": 300},
                "window_handle": "mwin_demo",
                "identity_status": "stable",
            }],
        }
        nodes = [{
            "element_id": "w1/1",
            "parent_id": "w1",
            "role": "AXStaticText",
            "subrole": "",
            "title": "Already semantic",
            "description": "",
            "value": "",
            "position": {"x": 20, "y": 20, "width": 120, "height": 20},
            "enabled": True,
            "focused": False,
            "actions": [],
            "child_count": 0,
        }]
        with patch.object(
            tools_ui, "_run_osascript", return_value=(True, "raw", "")
        ), patch.object(
            tools_ui, "_parse_observation", return_value=(metadata, nodes)
        ), patch.object(
            tools_ui, "_decorate_native_metadata", side_effect=lambda row: row
        ), patch.object(
            tools_ui, "_save_observation", return_value="obs_demo"
        ), patch.object(
            tools_ui, "_capture_window"
        ) as capture, patch.object(
            tools_ui, "_ocr_image"
        ) as ocr:
            payload, image = tools_ui._collect_observation(
                self.settings,
                "Demo",
                1,
                3,
                20,
                False,
                True,
                app_pid=111,
            )

        self.assertIsNone(image)
        self.assertTrue(payload["ocr"]["skipped"])
        self.assertEqual("semantic_text_available", payload["ocr"]["reason"])
        self.assertEqual("semantic", payload["telemetry"]["perception_stage"])
        self.assertFalse(payload["telemetry"]["ocr_used"])
        capture.assert_not_called()
        ocr.assert_not_called()

    def test_native_conditional_probe_is_slim_and_preserves_state_fields(self) -> None:
        script = tools_ui._fingerprint_observation_script(
            "Demo", 1, 2, 24, 80, app_pid=123
        )
        self.assertIn("__FPMETA__", script)
        self.assertIn("__FPNODE__", script)
        self.assertIn("value of nodeRef as text", script)
        self.assertIn('attribute "AXIdentifier"', script)
        self.assertNotIn("name of actions of nodeRef", script)
        self.assertNotIn("position of nodeRef", script)
        self.assertNotIn("description of nodeRef", script)

    def test_native_large_ax_result_marks_context_truncated_with_expand_hint(self) -> None:
        metadata = {
            "active_app": "Demo",
            "pid": 111,
            "bundle_id": "com.example.demo",
            "frontmost": False,
            "window_count": 1,
            "window_names": ["Demo"],
            "windows": [{
                "index": 1,
                "title": "Demo",
                "position": {"x": 10, "y": 10, "width": 500, "height": 300},
                "window_handle": "mwin_demo",
                "identity_status": "stable",
            }],
        }
        nodes = [{
            "element_id": "w1/1",
            "parent_id": "w1",
            "role": "AXGroup",
            "subrole": "",
            "title": "",
            "description": "",
            "value": "",
            "position": {"x": 20, "y": 20, "width": 120, "height": 20},
            "enabled": True,
            "focused": False,
            "actions": [],
            "child_count": 12,
        }]
        with patch.object(
            tools_ui, "_run_osascript", return_value=(True, "raw", "")
        ), patch.object(
            tools_ui, "_parse_observation", return_value=(metadata, nodes)
        ), patch.object(
            tools_ui, "_decorate_native_metadata", side_effect=lambda row: row
        ), patch.object(
            tools_ui, "_save_observation", return_value="obs_large"
        ):
            payload, image = tools_ui._collect_observation(
                self.settings,
                "Demo",
                1,
                3,
                5,
                False,
                False,
                app_pid=111,
            )

        self.assertIsNone(image)
        context = payload["telemetry"]["context_budget"]
        self.assertTrue(context["truncated"])
        self.assertIn("expand_hint", context)
        self.assertEqual("semantic", payload["telemetry"]["perception_stage"])

    def test_ocr_runs_only_when_semantic_text_is_absent(self) -> None:
        metadata = {
            "active_app": "Demo",
            "pid": 111,
            "bundle_id": "com.example.demo",
            "frontmost": False,
            "window_count": 1,
            "window_names": ["Demo"],
            "windows": [{
                "index": 1,
                "title": "Demo",
                "position": {"x": 10, "y": 10, "width": 500, "height": 300},
                "window_handle": "mwin_demo",
                "identity_status": "stable",
            }],
        }
        nodes = [{
            "element_id": "w1/1",
            "parent_id": "w1",
            "role": "AXImage",
            "subrole": "",
            "title": "",
            "description": "",
            "value": "",
            "position": {"x": 20, "y": 20, "width": 120, "height": 80},
            "enabled": True,
            "focused": False,
            "actions": [],
            "child_count": 0,
        }]
        capture_meta = {
            "scope": "window",
            "capture_method": "cgwindow+screencapture",
            "encoded_bytes": 4,
            "output_width": 500,
            "output_height": 300,
            "capture_duration_ms": 7,
        }
        with patch.object(
            tools_ui, "_run_osascript", return_value=(True, "raw", "")
        ), patch.object(
            tools_ui, "_parse_observation", return_value=(metadata, nodes)
        ), patch.object(
            tools_ui, "_decorate_native_metadata", side_effect=lambda row: row
        ), patch.object(
            tools_ui, "_save_observation", return_value="obs_ocr"
        ), patch.object(
            tools_ui, "_capture_window", return_value=(b"jpeg", None, capture_meta)
        ) as capture, patch.object(
            tools_ui, "_ocr_image", return_value=("Canvas label", None)
        ) as ocr:
            payload, image = tools_ui._collect_observation(
                self.settings,
                "Demo",
                1,
                3,
                20,
                False,
                True,
                app_pid=111,
            )

        self.assertIsNone(image)
        capture.assert_called_once()
        ocr.assert_called_once()
        self.assertEqual("Canvas label", payload["ocr"]["text"])
        self.assertFalse(payload["ocr"]["skipped"])
        telemetry = payload["telemetry"]
        self.assertEqual("ocr_full_visual", telemetry["perception_stage"])
        self.assertTrue(telemetry["ocr_used"])
        self.assertEqual(4, telemetry["visual_bytes"])
        self.assertEqual({"width": 500, "height": 300}, telemetry["visual_dimensions"])

    def test_native_observation_cache_isolated_by_agent(self) -> None:
        metadata = {
            "active_app": "Demo",
            "pid": 222,
            "bundle_id": "com.example.demo",
            "windows": [],
        }
        nodes = [{
            "element_id": "w1/1",
            "parent_id": "w1",
            "role": "AXStaticText",
            "title": "Private state",
            "description": "",
            "value": "",
            "position": None,
            "enabled": True,
            "focused": False,
            "actions": [],
            "child_count": 0,
        }]
        token_a = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_native_a",
                agent_id="agt_native_a",
                team_id="team_perception",
            )
        )
        try:
            observation_id = tools_ui._save_observation(
                "Demo", 1, nodes, metadata
            )
            self.assertIsNotNone(tools_ui._get_observation(observation_id))
        finally:
            reset_policy_context(token_a)

        token_b = set_policy_context(
            PolicyContext(
                profile="trusted",
                actor="agent:agt_native_b",
                agent_id="agt_native_b",
                team_id="team_perception",
            )
        )
        try:
            self.assertIsNone(tools_ui._get_observation(observation_id))
            derived_id, derived = tools_ui._store_derived_observation(
                observation_id, {}
            )
            self.assertIsNone(derived_id)
            self.assertIsNone(derived)
        finally:
            reset_policy_context(token_b)

    def test_native_context_truncation_is_explicit(self) -> None:
        nodes = [{
            "element_id": "w1/1",
            "child_count": 9,
        }]
        self.assertTrue(
            tools_ui._native_context_truncated(nodes, max_depth=3, max_children=5)
        )


class BenchmarkHarnessTests(unittest.TestCase):
    def test_benchmark_aggregation_is_numeric_only(self) -> None:
        module_path = Path("scripts/benchmark_perception.py").resolve()
        spec = importlib.util.spec_from_file_location("benchmark_perception", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        summary = module.aggregate_samples([
            {
                "ok": True,
                "wall_ms": 10,
                "duration_ms": 8,
                "payload_bytes": 400,
                "payload_tokens_estimate": 100,
                "visual_bytes": 0,
                "node_count": 12,
                "state_mode": "full",
                "perception_stage": "semantic",
                "secret": "never-copy",
            },
            {
                "ok": True,
                "wall_ms": 3,
                "duration_ms": 2,
                "payload_bytes": 120,
                "payload_tokens_estimate": 30,
                "visual_bytes": 0,
                "node_count": 0,
                "state_mode": "not_modified",
                "perception_stage": "conditional",
                "secret": "never-copy",
                "not_modified": True,
            },
        ])
        rendered = json.dumps(summary)
        self.assertNotIn("never-copy", rendered)
        self.assertEqual(2, summary["sample_count"])
        self.assertEqual(1, summary["not_modified_count"])
        self.assertEqual({"full": 1, "not_modified": 1}, summary["state_modes"])

    def test_twenty_repeat_baseline_keeps_raw_metrics_reproducible(self) -> None:
        module_path = Path("scripts/benchmark_perception.py").resolve()
        spec = importlib.util.spec_from_file_location("benchmark_perception_20", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = [
            {
                "ok": True,
                "wall_ms": 5 + index,
                "duration_ms": 4 + index,
                "payload_bytes": 1000 + index * 10,
                "payload_tokens_estimate": 250 + index * 3,
                "visual_bytes": 0,
                "node_count": 20,
                "state_mode": "not_modified" if index else "full",
                "perception_stage": "conditional" if index else "semantic",
                "not_modified": bool(index),
            }
            for index in range(20)
        ]
        summary = module.aggregate_samples(rows)
        self.assertEqual(20, summary["sample_count"])
        self.assertEqual(20, summary["tool_calls"])
        self.assertEqual(19, summary["not_modified_count"])
        self.assertEqual({"full": 1, "not_modified": 19}, summary["state_modes"])
        self.assertIn("p95_payload_bytes", summary)


class SnapshotPerceptionTests(unittest.TestCase):
    def test_snapshot_budget_reports_truncation_and_expand_hint(self) -> None:
        reader = lambda settings, **kwargs: {
            "count": 200,
            "apps": ["Application-" + ("x" * 200)] * 200,
            "truncated": False,
        }
        with patch.dict(tools_snapshot._SECTION_READERS, {"apps": reader}, clear=False):
            result = tools_snapshot.unified_read_snapshot(
                load_settings(),
                sections=["apps"],
                max_output_bytes=4096,
            )
        telemetry = result["telemetry"]
        self.assertEqual("snapshot", telemetry["perception_stage"])
        self.assertEqual("snapshot", telemetry["state_mode"])
        self.assertTrue(result["output_truncated"])
        self.assertTrue(telemetry["context_budget"]["truncated"])
        self.assertIn("expand_hint", telemetry["context_budget"])
        self.assertLessEqual(result["output_bytes"], 4096)


if __name__ == "__main__":
    unittest.main()
