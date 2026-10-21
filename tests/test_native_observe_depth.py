"""#82: web and Electron apps are read down to their controls, and a search returns only what matches.

The ChatGPT app nests its page under single-child groups (and every web element offers AXShowMenu and
AXScrollToVisible): those wrappers cost no depth. max_depth goes up to 16, said when it is lowered; truncation
is judged by children not shown; query='New chat | text area' reads the whole window and returns the matches.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import tools_ui
from mcp_server.security import load_settings

SWIFT = Path(tools_ui.__file__).with_name("ax_observe.swift").read_text(encoding="utf-8")


def node(element_id: str, parent: str | None, child_count: int, role: str = "AXGroup", title: str = "") -> dict:
    return {"element_id": element_id, "parent_id": parent, "child_count": child_count, "role": role, "title": title,
            "description": "", "value": "", "actions": []}


class PassThroughRuleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xcrun"), "Xcode command line tools are not installed")
    def test_wrappers_that_hold_one_thing_cost_no_depth(self) -> None:
        start = SWIFT.index("let passThroughRoles")
        rule = SWIFT[start:SWIFT.index("func walk(", start)]
        harness = rule + '''
precondition(passesThrough(role: "AXGroup", value: "", actions: [], childCount: 1))
precondition(passesThrough(role: "AXGroup", value: "", actions: ["AXShowMenu", "AXScrollToVisible"], childCount: 1))
precondition(passesThrough(role: "AXScrollArea", value: "", actions: [], childCount: 1))
precondition(!passesThrough(role: "AXGroup", value: "", actions: [], childCount: 2))
precondition(!passesThrough(role: "AXGroup", value: "", actions: ["AXPress"], childCount: 1))
precondition(!passesThrough(role: "AXGroup", value: "text", actions: [], childCount: 1))
precondition(!passesThrough(role: "AXButton", value: "", actions: [], childCount: 1))
precondition(!passesThrough(role: "AXWebArea", value: "", actions: [], childCount: 1))
print("PASS_THROUGH_OK")
'''
        with tempfile.TemporaryDirectory() as td:
            source, binary = Path(td) / "main.swift", Path(td) / "rule"
            source.write_text(harness, encoding="utf-8")
            built = subprocess.run(["xcrun", "swiftc", str(source), "-o", str(binary)], capture_output=True, text=True,
                                   timeout=180)
            self.assertEqual(0, built.returncode, built.stderr[-1500:])
            self.assertIn("PASS_THROUGH_OK", subprocess.run([str(binary)], capture_output=True, text=True).stdout)

    def test_the_walk_uses_the_rule_and_keeps_full_id_paths(self) -> None:
        walk = SWIFT[SWIFT.index("func walk("):SWIFT.index("// System Events names a process")]
        self.assertIn("depth: through ? depth : depth + 1", walk)
        self.assertIn("(depth >= maxDepth && !through) || rawDepth >= maxRawDepth", walk)
        self.assertIn('id: "\\(id)/\\(index + 1)"', walk)


class TruncationTests(unittest.TestCase):
    def test_children_not_shown_is_truncation_whatever_the_id_depth(self) -> None:
        deep = [node("w1", None, 1)] + [node("w1" + "/1" * i, "w1" + "/1" * (i - 1), 1) for i in range(1, 12)]
        deep.append(node("w1" + "/1" * 12, "w1" + "/1" * 11, 0, role="AXButton"))
        self.assertFalse(tools_ui._native_context_truncated(deep, max_depth=4, max_children=30))
        deep[-1]["child_count"] = 2  # a deep node whose children were not read
        self.assertTrue(tools_ui._native_context_truncated(deep, max_depth=4, max_children=30))


class QueryTests(unittest.TestCase):
    NODES = [node("w1", None, 3, role="AXWindow", title="ChatGPT"),
             node("w1/1", "w1", 0, role="AXButton", title="New chat"),
             {**node("w1/2", "w1", 0, role="AXTextArea", title="Ask ChatGPT"), "description": "text entry area"},
             node("w1/3", "w1", 0, role="AXButton", title="Share")]

    def test_terms_and_matches(self) -> None:
        self.assertEqual([["new", "chat"], ["text", "area"]], tools_ui._query_terms(" New chat | text area |"))
        self.assertEqual([], tools_ui._query_terms(None))
        payload = {"nodes": list(self.NODES)}
        tools_ui._keep_query_matches(payload, tools_ui._query_terms("New chat | text area"))
        self.assertEqual(["w1/1", "w1/2"], [n["element_id"] for n in payload["nodes"]])
        self.assertEqual({"alternatives": ["new chat", "text area"], "matched": 2, "searched": 4, "shown": 2},
                         payload["query"])

    def test_nothing_matched_says_not_to_repeat(self) -> None:
        payload = {"nodes": list(self.NODES)}
        tools_ui._keep_query_matches(payload, tools_ui._query_terms("Send"))
        self.assertEqual([], payload["nodes"])
        self.assertIn("do not repeat the same search", payload["query"]["hint"])


class ObserveWiringTests(unittest.TestCase):
    def observe(self, **kwargs) -> tuple[dict, dict]:
        seen: dict = {}

        def collect(settings, app, window_index, max_depth, max_children, *args, **kw):
            seen.update(max_depth=max_depth, max_children=max_children, max_nodes=kw.get("max_nodes"))
            return {"ok": True, "observation_id": "obs_x", "nodes": list(QueryTests.NODES), "telemetry": {}}, None

        with patch.object(tools_ui, "_resolve_registered_native_target", return_value=("ChatGPT", 123, 1, None, None)), \
             patch.object(tools_ui, "_privacy_block_app", return_value=None), \
             patch.object(tools_ui, "_collect_observation", side_effect=collect):
            result = tools_ui._observe_ui(load_settings(), app="ChatGPT", **kwargs)
        text = result[0] if isinstance(result, list) else result
        return json.loads(text) if isinstance(text, str) else text, seen

    def test_a_deeper_request_is_lowered_and_said(self) -> None:
        payload, seen = self.observe(max_depth=20)
        self.assertEqual(16, seen["max_depth"])
        self.assertEqual({"requested": 20, "applied": 16, "limit": 16}, payload["depth"])
        self.assertNotIn("depth", self.observe(max_depth=8)[0])

    def test_a_search_reads_the_whole_window_with_a_larger_budget(self) -> None:
        payload, seen = self.observe(query="New chat")
        self.assertEqual((16, 60, tools_ui._QUERY_MAX_NODES), (seen["max_depth"], seen["max_children"], seen["max_nodes"]))
        self.assertEqual(["w1/1"], [n["element_id"] for n in payload["nodes"]])
        _, plain = self.observe()
        self.assertEqual(tools_ui._NATIVE_MAX_NODES, plain["max_nodes"])

    def test_the_tool_offers_query(self) -> None:
        source = (Path(tools_ui.__file__).with_name("main.py")).read_text(encoding="utf-8")
        tool = source[source.index('name="mac_observe"'):source.index('name="mac_act"')]
        self.assertIn("query: Optional[str] = None", tool)
        self.assertIn("query=query", tool)
        self.assertTrue(re.search(r"query='New chat \| text area'", tool))


if __name__ == "__main__":
    unittest.main()
