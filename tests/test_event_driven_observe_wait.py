from __future__ import annotations

import asyncio
import inspect
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from mcp_server.computer_plan import execute_computer_plan
from mcp_server.tools_browser_agent import (
    _event_wait_js,
    _event_wait_status_js,
    _wait_action,
)
from mcp_server import rest_routes, tools_ui
from mcp_server.computer_use_perf import record_computer_use_sample, reset_computer_use_samples


class BrowserEventDrivenWaitTests(unittest.TestCase):
    def test_event_waiter_wakes_from_dom_history_and_network_and_keeps_bounded_fallback(self) -> None:
        script = _event_wait_js(
            {"for": "network_idle", "timeout_s": 2.0, "stable_ms": 300},
            "https://example.test/start",
            2.0,
        )
        self.assertIn("MutationObserver", script)
        self.assertIn("s.mutationRevision+=1", script)
        self.assertIn("s.lastMutationAt=Date.now()", script)
        self.assertIn("history.pushState", script)
        self.assertIn("history.replaceState", script)
        self.assertIn("window.fetch", script)
        self.assertIn("XMLHttpRequest", script)
        self.assertIn("setInterval", script)
        self.assertIn("bounded_fallback", script)
        self.assertIn("readystatechange", script)
        self.assertNotIn("Page.bringToFront", script)
        self.assertNotIn("window.focus()", script)

    def test_chrome_event_wait_is_one_remote_js_call(self) -> None:
        event_result = {
            "ok": True,
            "type": "wait",
            "for": "selector",
            "matched": True,
            "timed_out": False,
            "settled_by": "mutation",
            "event_count": 1,
            "fallback_ticks": 0,
            "duration_ms": 42,
            "url": "https://example.test/app",
            "dom_revision": 3,
        }
        with patch("mcp_server.tools_browser_agent._run_json_js", return_value=event_result) as run:
            result = _wait_action(
                MagicMock(),
                "Google Chrome",
                {"type": "wait", "for": "selector", "selector": "#ready", "timeout_s": 2},
                1,
                2,
                "https://example.test/app",
                "tab-a",
            )
        self.assertTrue(result["matched"])
        self.assertEqual("event_driven", result["wait_strategy"])
        self.assertEqual(1, result["_js_calls"])
        self.assertIn("benchmark", result["telemetry"])
        self.assertEqual(1, result["telemetry"]["remote_js_calls"])
        run.assert_called_once()

    def test_safari_event_wait_uses_token_and_compact_status_probe(self) -> None:
        installed = {
            "ok": True,
            "pending": True,
            "wait_token": "bw_test",
            "matched": False,
        }
        done = {
            "ok": True,
            "type": "wait",
            "for": "selector",
            "matched": True,
            "pending": False,
            "settled_by": "mutation",
            "event_count": 1,
            "fallback_ticks": 0,
            "duration_ms": 15,
        }
        with patch("mcp_server.tools_browser_agent._run_json_js", side_effect=[installed, done]) as run, \
             patch("mcp_server.tools_browser_agent.cancellable_sleep"):
            result = _wait_action(
                MagicMock(),
                "Safari",
                {"type": "wait", "for": "selector", "selector": "#ready", "timeout_s": 2},
                1,
                1,
                "https://example.test/app",
                "tab-a",
            )
        self.assertTrue(result["matched"])
        self.assertEqual(2, result["_js_calls"])
        self.assertEqual("event_driven", result["wait_strategy"])
        self.assertEqual(2, run.call_count)
        self.assertIn("eventWaiters", _event_wait_status_js("bw_test"))

    def test_event_wait_failure_falls_back_to_bounded_polling(self) -> None:
        fallback = {
            "ok": True,
            "type": "wait",
            "for": "selector",
            "matched": True,
            "_js_calls": 3,
            "duration_ms": 100,
        }
        with patch(
            "mcp_server.tools_browser_agent._run_json_js",
            side_effect=HTTPException(status_code=500, detail="event bridge failed"),
        ), patch(
            "mcp_server.tools_browser_agent._wait_action_polling",
            return_value=fallback,
        ) as polling:
            result = _wait_action(
                MagicMock(),
                "Google Chrome",
                {"type": "wait", "for": "selector", "selector": "#ready", "timeout_s": 1},
                1,
                1,
                "https://example.test/app",
                "tab-a",
            )
        self.assertTrue(result["matched"])
        self.assertEqual("bounded_poll_fallback", result["wait_strategy"])
        self.assertTrue(result["telemetry"]["event_wait_failed"])
        polling.assert_called_once()

    def test_semantic_wait_source_respects_role_actionable_and_modal_scope(self) -> None:
        script = _event_wait_js(
            {
                "for": "semantic",
                "query": "Save",
                "role": "button",
                "actionable_only": True,
                "timeout_s": 2,
            },
            "",
            2,
        )
        self.assertIn("spec.kind==='semantic'", script)
        self.assertIn("__mcpTopBlockingModal", script)
        self.assertIn("__mcpSemanticVisible", script)
        self.assertIn("spec.actionable_only", script)
        self.assertIn("norm(d.role)!==norm(spec.role)", script)


class NativeConditionalObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS.clear()

    def _previous(self) -> tuple[str, dict]:
        metadata = {
            "active_app": "DemoApp",
            "pid": 123,
            "bundle_id": "demo.app",
            "app_handle": "mapp_demo",
            "window_count": 1,
            "windows": [{
                "index": 1,
                "title": "Demo",
                "document": "",
                "identifier": "win",
                "position": {"x": 10, "y": 20, "width": 500, "height": 400},
                "subrole": "AXStandardWindow",
                "focused": True,
                "main": True,
                "window_handle": "mwin_demo",
            }],
        }
        nodes = [
            {
                "element_id": "w1",
                "parent_id": None,
                "role": "AXWindow",
                "subrole": "AXStandardWindow",
                "title": "Demo",
                "description": "",
                "value": "",
                "position": {"x": 10, "y": 20, "width": 500, "height": 400},
                "enabled": True,
                "focused": True,
                "actions": [],
                "child_count": 1,
                "identifier": "win",
            },
            {
                "element_id": "w1/1",
                "parent_id": "w1",
                "role": "AXStaticText",
                "subrole": "",
                "title": "Ready",
                "description": "",
                "value": "",
                "position": {"x": 20, "y": 40, "width": 100, "height": 20},
                "enabled": True,
                "focused": False,
                "actions": [],
                "child_count": 0,
                "identifier": "label",
            },
        ]
        fp = tools_ui._native_fingerprint_digest(
            tools_ui._native_fingerprint_components(metadata, nodes, 1)
        )
        rev = tools_ui._native_tree_revision(metadata, nodes, 1)
        obs = tools_ui._save_observation(
            "DemoApp",
            1,
            nodes,
            metadata,
            max_depth=5,
            max_children=30,
            fingerprint=fp,
            tree_revision=rev,
        )
        return obs, tools_ui._get_observation(obs)

    def test_unchanged_conditional_observe_skips_full_ax_traversal(self) -> None:
        obs, previous = self._previous()
        with patch(
            "mcp_server.tools_ui._resolve_registered_native_target",
            return_value=("DemoApp", 123, None, None, None),
        ), patch(
            "mcp_server.tools_ui._probe_native_observation_fingerprint",
            return_value=(previous["fingerprint"], None),
        ), patch(
            "mcp_server.tools_ui._collect_observation",
        ) as collect:
            raw = tools_ui.observe_ui(
                MagicMock(),
                app="DemoApp",
                window_index=1,
                max_depth=5,
                max_children=30,
                include_screenshot=False,
                previous_observation_id=obs,
            )
        payload = json.loads(raw)
        self.assertTrue(payload["not_modified"])
        self.assertEqual("not_modified", payload["state_mode"])
        self.assertEqual(0, payload["telemetry"]["ax_traversals"])
        self.assertEqual("lightweight_fingerprint", payload["cache_validation"])
        collect.assert_not_called()

    def test_changed_nonstructural_observe_returns_delta(self) -> None:
        obs, previous = self._previous()
        new_nodes = [dict(v) for v in previous["nodes"].values()]
        new_nodes[1] = dict(new_nodes[1], title="Changed")
        metadata = {
            "active_app": "DemoApp",
            "pid": 123,
            "bundle_id": "demo.app",
            "app_handle": "mapp_demo",
            "window_count": 1,
            "windows": [{
                "index": 1,
                "title": "Demo",
                "document": "",
                "identifier": "win",
                "position": {"x": 10, "y": 20, "width": 500, "height": 400},
                "subrole": "AXStandardWindow",
                "focused": True,
                "main": True,
                "window_handle": "mwin_demo",
            }],
        }
        new_fp = tools_ui._native_fingerprint_digest(
            tools_ui._native_fingerprint_components(metadata, new_nodes, 1)
        )
        new_rev = tools_ui._native_tree_revision(metadata, new_nodes, 1)
        new_obs = tools_ui._save_observation(
            "DemoApp", 1, new_nodes, metadata,
            max_depth=5, max_children=30, fingerprint=new_fp, tree_revision=new_rev,
        )
        full_payload = {
            "ok": True,
            "observation_id": new_obs,
            "captured_at": "now",
            "active_app": "DemoApp",
            "app_handle": "mapp_demo",
            "app_pid": 123,
            "window_handle": "mwin_demo",
            "window_index": 1,
            "node_count": len(new_nodes),
            "native_revision": new_rev,
            "state_mode": "full",
            "telemetry": {"ax_traversals": 1, "payload_mode": "full"},
            "screenshot": {"requested": False, "included_as_image_content": False, "mime_type": None},
            "nodes": new_nodes,
        }
        with patch(
            "mcp_server.tools_ui._resolve_registered_native_target",
            return_value=("DemoApp", 123, None, None, None),
        ), patch(
            "mcp_server.tools_ui._probe_native_observation_fingerprint",
            return_value=("different", None),
        ), patch(
            "mcp_server.tools_ui._collect_observation",
            return_value=(full_payload, None),
        ):
            raw = tools_ui.observe_ui(
                MagicMock(), app="DemoApp", window_index=1,
                max_depth=5, max_children=30,
                previous_observation_id=obs,
            )
        payload = json.loads(raw)
        self.assertEqual("delta", payload["state_mode"])
        self.assertFalse(payload["structural_refresh"])
        self.assertEqual(1, payload["delta"]["changed_count"])
        self.assertEqual(0, payload["delta"]["added_count"])
        self.assertEqual(1, payload["telemetry"]["ax_traversals"])

    def test_structural_change_returns_full_refresh(self) -> None:
        obs, previous = self._previous()
        new_nodes = [dict(v) for v in previous["nodes"].values()]
        new_nodes.append({
            "element_id": "w1/2",
            "parent_id": "w1",
            "role": "AXButton",
            "subrole": "",
            "title": "New",
            "description": "",
            "value": "",
            "position": {"x": 30, "y": 80, "width": 80, "height": 20},
            "enabled": True,
            "focused": False,
            "actions": ["AXPress"],
            "child_count": 0,
            "identifier": "new-button",
        })
        metadata = {
            "active_app": "DemoApp", "pid": 123, "bundle_id": "demo.app",
            "app_handle": "mapp_demo", "window_count": 1,
            "windows": [{
                "index": 1, "title": "Demo", "document": "", "identifier": "win",
                "position": {"x": 10, "y": 20, "width": 500, "height": 400},
                "subrole": "AXStandardWindow", "focused": True, "main": True,
                "window_handle": "mwin_demo",
            }],
        }
        new_fp = tools_ui._native_fingerprint_digest(
            tools_ui._native_fingerprint_components(metadata, new_nodes, 1)
        )
        new_rev = tools_ui._native_tree_revision(metadata, new_nodes, 1)
        new_obs = tools_ui._save_observation(
            "DemoApp", 1, new_nodes, metadata,
            max_depth=5, max_children=30, fingerprint=new_fp, tree_revision=new_rev,
        )
        full_payload = {
            "ok": True, "observation_id": new_obs, "captured_at": "now",
            "active_app": "DemoApp", "app_handle": "mapp_demo", "app_pid": 123,
            "window_handle": "mwin_demo", "window_index": 1,
            "node_count": len(new_nodes), "nodes": new_nodes,
            "native_revision": new_rev, "state_mode": "full",
            "telemetry": {"ax_traversals": 1, "payload_mode": "full"},
            "screenshot": {"requested": False, "included_as_image_content": False, "mime_type": None},
        }
        with patch(
            "mcp_server.tools_ui._resolve_registered_native_target",
            return_value=("DemoApp", 123, None, None, None),
        ), patch(
            "mcp_server.tools_ui._probe_native_observation_fingerprint",
            return_value=("different", None),
        ), patch(
            "mcp_server.tools_ui._collect_observation",
            return_value=(full_payload, None),
        ):
            raw = tools_ui.observe_ui(
                MagicMock(), app="DemoApp", window_index=1,
                previous_observation_id=obs,
            )
        payload = json.loads(raw)
        self.assertEqual("full", payload["state_mode"])
        self.assertTrue(payload["structural_refresh"])
        self.assertEqual(obs, payload["previous_observation_id"])

    def test_expired_validation_window_forces_periodic_full_refresh(self) -> None:
        obs, previous = self._previous()
        self.assertLessEqual(tools_ui._OBSERVATION_CONDITIONAL_MAX_AGE_S, 0.75)
        previous["full_refresh_at"] = time.time() - 10
        with tools_ui._OBSERVATIONS_LOCK:
            tools_ui._OBSERVATIONS[obs]["full_refresh_at"] = previous["full_refresh_at"]
        with patch(
            "mcp_server.tools_ui._resolve_registered_native_target",
            return_value=("DemoApp", 123, None, None, None),
        ), patch(
            "mcp_server.tools_ui._probe_native_observation_fingerprint",
        ) as fingerprint, patch(
            "mcp_server.tools_ui._collect_observation",
            return_value=({"ok": False, "error": "fixture"}, None),
        ) as collect:
            tools_ui.observe_ui(
                MagicMock(), app="DemoApp", window_index=1,
                previous_observation_id=obs,
            )
        fingerprint.assert_not_called()
        collect.assert_called_once()



class ObserveContractAndBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_computer_use_samples()

    def test_mac_observe_screenshot_is_opt_in_for_core_and_rest_alias(self) -> None:
        signature = inspect.signature(tools_ui.observe_ui)
        self.assertIs(False, signature.parameters["include_screenshot"].default)
        with patch("mcp_server.rest_routes.observe_ui", return_value={"ok": True}) as observe:
            result = rest_routes._api_mac_observe_alias(payload={}, settings=MagicMock())
        self.assertEqual({"ok": True}, result)
        self.assertFalse(observe.call_args.kwargs["include_screenshot"])

    def test_rolling_benchmark_reports_p95_payload_js_and_ax_metrics(self) -> None:
        summary = None
        for index in range(10):
            summary = record_computer_use_sample(
                "fixture",
                duration_ms=10 + index * 5,
                payload_bytes=100 + index * 10,
                remote_js_calls=1 if index < 8 else 2,
                ax_traversals=0 if index < 9 else 1,
            )
        assert summary is not None
        self.assertEqual(10, summary["sample_count"])
        self.assertEqual(55, summary["p95_latency_ms"])
        self.assertEqual(145.0, summary["avg_payload_bytes"])
        self.assertEqual(1.2, summary["avg_remote_js_calls"])
        self.assertEqual(0.1, summary["avg_ax_traversals"])




class PlanWaitPipelineTests(unittest.TestCase):
    def test_browser_find_wait_until_delegates_timeout_to_event_waiter_once(self) -> None:
        async def run() -> None:
            calls: list[tuple[str, dict]] = []

            async def caller(tool: str, args: dict):
                calls.append((tool, dict(args)))
                self.assertEqual("browser_find", tool)
                self.assertGreater(args.get("wait_timeout_s", 0), 0)
                match = {
                    "element_id": "e_ready",
                    "role": "button",
                    "text": "Ready",
                    "confidence": 0.98,
                }
                return {
                    "ok": True,
                    "observation_id": "bobs_ready",
                    "best_match": match,
                    "matches": [match],
                    "wait": {"strategy": "event_driven", "remote_js_calls": 1},
                }

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                steps=[{
                    "id": "wait_ready",
                    "type": "wait_until",
                    "tool": "browser_find",
                    "arguments": {
                        "browser": "Google Chrome",
                        "tab_handle": "tab-a",
                        "query": "Ready",
                        "role": "button",
                        "actionable_only": True,
                    },
                    "target": {"role": "button", "title": "Ready"},
                    "timeout_s": 2.0,
                    "poll_ms": 20,
                }],
            )
            self.assertTrue(result["ok"])
            self.assertEqual(1, len(calls))
            self.assertEqual("event_driven", result["steps"][0]["wait_strategy"])

        asyncio.run(run())

    def test_native_wait_until_chains_previous_observation_id(self) -> None:
        async def run() -> None:
            calls: list[dict] = []
            count = 0

            async def caller(tool: str, args: dict):
                nonlocal count
                self.assertEqual("mac_observe", tool)
                calls.append(dict(args))
                count += 1
                if count == 1:
                    return {"ok": True, "observation_id": "o1", "nodes": []}
                if count == 2:
                    self.assertEqual("o1", args.get("previous_observation_id"))
                    return {
                        "ok": True,
                        "state_mode": "not_modified",
                        "not_modified": True,
                        "observation_id": "o1",
                        "previous_observation_id": "o1",
                    }
                self.assertEqual("o1", args.get("previous_observation_id"))
                return {
                    "ok": True,
                    "observation_id": "o2",
                    "nodes": [{
                        "element_id": "w1/1",
                        "role": "AXButton",
                        "title": "Allow",
                        "identifier": "allow-button",
                    }],
                }

            result = await execute_computer_plan(
                caller,
                plan_version=2,
                steps=[{
                    "id": "wait_native",
                    "type": "wait_until",
                    "tool": "mac_observe",
                    "arguments": {"app": "DemoApp", "include_screenshot": False},
                    "target": {
                        "identifier": "allow-button",
                        "role": "AXButton",
                        "title": "Allow",
                    },
                    "timeout_s": 1.0,
                    "poll_ms": 20,
                }],
            )
            self.assertTrue(result["ok"])
            self.assertGreaterEqual(len(calls), 3)
            self.assertNotIn("previous_observation_id", calls[0])
            self.assertEqual("o1", calls[1]["previous_observation_id"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
