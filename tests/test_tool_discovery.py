from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import tool_discovery
from tests.test_core_catalog_descriptions import _build_mcp


class RankingUnitTests(unittest.TestCase):
    def test_stopwords_and_stemming(self) -> None:
        self.assertEqual(["read", "file"], tool_discovery.query_tokens("How do I read the files"))

    def test_empty_query_keeps_registry_order(self) -> None:
        ranked = tool_discovery.rank([("b_tool", ""), ("a_tool", "")], "")
        self.assertEqual(["b_tool", "a_tool"], [item.name for item in ranked])

    def test_unrelated_tools_are_dropped_and_name_beats_description(self) -> None:
        ranked = tool_discovery.rank(
            [("other", "Reads a file"), ("read_file", "Opens text"), ("noise", "nothing here")], "read file",
        )
        self.assertEqual(["read_file", "other"], [item.name for item in ranked])
        self.assertIn("name matches", ranked[0].reasons[-1])

    def test_compat_tools_lose_ties_to_core(self) -> None:
        ranked = tool_discovery.rank([("x_click", ""), ("y_click", "")], "click", core={"y_click"}, demoted={"x_click"})
        self.assertEqual("y_click", ranked[0].name)

    def test_cursor_round_trip_and_query_binding(self) -> None:
        cursor = tool_discovery.encode_cursor("read file", 8)
        self.assertEqual(8, tool_discovery.decode_cursor(cursor, " Read File "))
        with self.assertRaises(tool_discovery.CursorError):
            tool_discovery.decode_cursor(cursor, "delete")
        with self.assertRaises(tool_discovery.CursorError):
            tool_discovery.decode_cursor("garbage", "read file")

    def test_short_description_keeps_whole_sentences(self) -> None:
        text = "First sentence here. " + "Second sentence is long. " * 30
        short, truncated = tool_discovery.short_description(text, None, limit=60)
        self.assertTrue(truncated)
        self.assertTrue(short.endswith("."))
        self.assertEqual(("Sum.", True), tool_discovery.short_description(text, "Sum."))

    def test_parameter_summary_reports_bounds_through_optional(self) -> None:
        spec = {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 50}, {"type": "null"}], "default": None}
        self.assertEqual({"type": "integer", "default": None, "minimum": 1, "maximum": 50},
                         tool_discovery.parameter_summary(spec))
        self.assertEqual(["a", "b"], tool_discovery.parameter_summary({"type": "string", "enum": ["a", "b"]})["enum"])


class DiscoverToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def _profile(self, name: str) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": name}, clear=False)
        patcher.start()
        os.environ.pop("MAC_MCP_TOOL_PROFILE", None)
        self.addCleanup(patcher.stop)

    def _discover(self, query: str, **kwargs) -> dict:
        result = asyncio.run(self.mcp.call_tool(
            "tool_discover", {"query": query, "description": "Find a tool", **kwargs}))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_every_use_case_entry_names_a_registered_tool(self) -> None:
        registered = {tool.name for tool in asyncio.run(FastMCP.list_tools(self.mcp))}
        self.assertEqual(set(), set(tool_discovery.TOOL_USE_CASES) - registered)

    def test_natural_intents_rank_the_expected_tool_first(self) -> None:
        self._profile("trusted")
        for query, expected in (
            ("read browser page", "browser_observe"),
            ("run long command", "start_background_job"),
            ("read several files", "read_multiple_files"),
            ("send email", "mac_app"),
            ("delete a file", "delete_path"),
        ):
            with self.subTest(query=query):
                payload = self._discover(query)
                self.assertEqual(expected, payload["tools"][0]["name"])
                self.assertTrue(payload["tools"][0]["why"])
                self.assertFalse(payload["tools"][0]["description"].endswith("..."))

    def test_pagination_never_stops_silently(self) -> None:
        self._profile("trusted")
        first = self._discover("file", limit=3)
        self.assertGreater(first["total"], 8)
        self.assertTrue(first["has_more"])
        seen = [item["name"] for item in first["tools"]]
        cursor = first["next_cursor"]
        while cursor:
            page = self._discover("file", limit=5, cursor=cursor)
            seen.extend(item["name"] for item in page["tools"])
            cursor = page["next_cursor"]
            self.assertEqual(bool(cursor), page["has_more"])
        self.assertEqual(first["total"], len(seen))
        self.assertEqual(len(seen), len(set(seen)))
        with self.assertRaises(ToolError):
            self._discover("delete", cursor=first["next_cursor"])

    def test_ranking_does_not_reveal_tools_the_profile_hides(self) -> None:
        self._profile("read_only")
        names = {item["name"] for item in self._discover("delete file write", limit=100)["tools"]}
        self.assertNotIn("delete_path", names)
        self.assertNotIn("write_file", names)
        self._profile("trusted")
        names = {item["name"] for item in self._discover("delete file write", limit=100)["tools"]}
        self.assertIn("delete_path", names)


if __name__ == "__main__":
    unittest.main()
