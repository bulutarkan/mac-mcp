from __future__ import annotations
import tests._state_isolation
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from pydantic import TypeAdapter, ValidationError
from mcp_server import browser_schemas as schemas
from mcp_server import tools_browser_agent as agent
from tests.test_core_catalog_descriptions import _build_mcp
import mcp_server.main as main

class ExtractionContractTests(unittest.TestCase):
    def test_fields_contract_accepts_selector_objects_and_rejects_malformed_fields(self):
        adapter = TypeAdapter(schemas.BrowserActions)
        good = [{"type": "extract", "fields": [{"name": "reviews", "selector": ".review", "all": True}]}]
        self.assertEqual(good, adapter.validate_python(good))
        props = adapter.json_schema()["items"]["properties"]
        self.assertEqual("object", props["fields"]["items"]["type"])
        for fields in ([], ["reviews"], [{"name": "reviews"}], [{"name": "x", "selector": ""}], [{"name": "x", "selector": "p", "offset": -1}]):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                adapter.validate_python([{"type": "extract", "fields": fields}])

    def test_direct_call_rejects_malformed_extract_before_browser_transport(self):
        with patch.object(agent, "_run_json_js", side_effect=AssertionError("transport reached")):
            result = agent._extract_action(None, "Safari", {"fields": [{"name": "x"}]}, 1, 1)
        self.assertFalse(result["ok"])
        self.assertEqual("invalid_extract_fields", result["error"])

    def test_browser_do_preserves_extraction_counts_and_continuation(self):
        profile = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"})
        profile.start()
        self.addCleanup(profile.stop)
        mcp = _build_mcp()
        extracted = {"ok": True, "type": "extract", "data": {"reviews": ["A"]}, "matched_counts": {"reviews": 3}, "fields": {"reviews": {"matched_count": 3, "eligible_count": 3, "returned_count": 1, "continuation": {"offset": 1, "text_offset": 0}}}, "truncated": True, "complete": False, "chars": 1}
        with patch.object(main, "browser_act", return_value={"ok": True, "actions": [extracted], "action_count": 1}):
            result = asyncio.run(mcp.call_tool("browser_do", {"browser": "Safari", "tab_handle": "test", "extract": ["rating"], "description": "Read bounded records"}))
        blocks = result[0] if isinstance(result, tuple) else result
        payload = json.loads(blocks[0].text)
        self.assertEqual(3, payload["matched_counts"]["reviews"])
        self.assertEqual(1, payload["fields"]["reviews"]["continuation"]["offset"])
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["truncated"])

    def test_browser_do_budget_preserves_prefix_and_exact_resume_position(self):
        original = "BEGIN " + "a" * 19000 + " END"
        payload = {"ok": True, "data": {"reviews": [original, "SECOND"]}, "matched_counts": {"reviews": 2},
            "fields": {"reviews": {"matched_count": 2, "eligible_count": 2, "returned_count": 2,
                "offset": 0, "text_offset": 0, "has_more": False, "continuation": None}}, "complete": True, "truncated": False}
        result = main._fit_browser_do_output(payload, budget_bytes=2048)
        self.assertLessEqual(main._browser_json_bytes(result), 2048)
        self.assertFalse(result["complete"])
        returned = result["data"]["reviews"][0]
        self.assertTrue(original.startswith(returned))
        self.assertEqual({"offset": 0, "text_offset": len(returned)}, result["fields"]["reviews"]["continuation"])
        self.assertEqual(1, result["fields"]["reviews"]["returned_count"])

    def test_compact_output_resume_offset_uses_browser_utf16_units(self):
        original = "😀" * 1000
        payload = {"ok": True, "data": {"x": [original]}, "matched_counts": {"x": 1},
            "fields": {"x": {"eligible_count": 1, "returned_count": 1, "offset": 0, "text_offset": 0, "has_more": False, "continuation": None}}, "complete": True}
        result = main._fit_browser_do_output(payload, budget_bytes=2048)
        returned = result["data"]["x"][0]
        self.assertEqual(len(returned) * 2, result["fields"]["x"]["continuation"]["text_offset"])


class BrowserDoScanTests(unittest.TestCase):
    """browser_do returns what a scan read; it never reads more than it can return."""

    SCAN = {"ok": True, "type": "scan", "items": [{"text": "Review one", "record_id": "r1", "text_offset": 0, "text_complete": True},
                                                  {"text": "Review two", "record_id": "r2", "text_offset": 0, "text_complete": True}],
            "count": 2, "stopped": "payload_budget", "complete": False, "has_more": True,
            "next_cursor": "scan_" + "a" * 32, "total_seen": 30, "steps": 3, "key_field": "text",
            "container": {"tag": "document"}, "other_containers": [], "coverage": "observed_rendered_rows", "duration_ms": 40}

    def setUp(self):
        profile = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"})
        profile.start()
        self.addCleanup(profile.stop)
        self.mcp = _build_mcp()

    def call(self, scan_result, actions=None):
        actions = actions or [{"type": "scan", "item_selector": "div.review", "max_items": 100}]
        with patch.object(main, "browser_act", return_value={"ok": True, "actions": [scan_result], "action_count": 1}) as act:
            result = asyncio.run(self.mcp.call_tool("browser_do", {"browser": "Safari", "tab_handle": "test", "actions": actions,
                                                                   "description": "Scan the reviews"}))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text), act

    def test_browser_do_returns_the_scan_and_its_cursor(self):
        payload, _ = self.call(self.SCAN)
        self.assertEqual(["Review one", "Review two"], [item["text"] for item in payload["scan"]["items"]])
        self.assertEqual("scan_" + "a" * 32, payload["scan"]["next_cursor"])
        self.assertFalse(payload["scan"]["complete"])
        self.assertEqual("payload_budget", payload["scan"]["stopped"])
        self.assertEqual(2, payload["scan"]["count"])

    def test_browser_do_caps_the_scan_to_what_it_can_return(self):
        _, act = self.call(self.SCAN)
        scan = [action for action in act.call_args.kwargs["actions"] if action["type"] == "scan"][0]
        self.assertLessEqual(scan["max_bytes"], main._BROWSER_DO_OUTPUT_BUDGET_BYTES // 2 + 1024)
        self.assertGreaterEqual(scan["max_bytes"], 1024)

    def test_the_output_budget_never_cuts_scan_records(self):
        items = [{"text": "x" * 900, "record_id": f"r{i}", "text_offset": 0, "text_complete": True} for i in range(5)]
        payload = {"ok": True, "data": {"other": ["y" * 9000]}, "scan": {**self.SCAN, "items": items, "count": 5}}
        fitted = main._fit_browser_do_output(payload)
        self.assertEqual(items, fitted["scan"]["items"])
        self.assertEqual(self.SCAN["next_cursor"], fitted["scan"]["next_cursor"])


class ScanByteCapTests(unittest.TestCase):
    def test_a_scan_stops_at_the_byte_cap_it_was_given(self):
        rows = [{"key": f"k{i}", "text": "z" * 400, "record_id": f"r{i}", "text_offset": 0, "text_complete": True} for i in range(20)]
        step = {"ok": True, "items": rows, "total_seen": 20, "container": {"tag": "document", "scroll_top": 0},
                "next_cursor": "scan_" + "b" * 32}
        with patch.object(agent, "_run_json_js", return_value=step) as run, patch.object(agent, "cancellable_sleep"):
            result = agent._scan_action(None, "Safari", {"item_selector": ".r", "max_bytes": 2000}, 1, 1)
        self.assertIn('"max_bytes": 2000', run.call_args.args[2])
        self.assertEqual("payload_budget", result["stopped"])
        self.assertEqual("scan_" + "b" * 32, result["next_cursor"])
