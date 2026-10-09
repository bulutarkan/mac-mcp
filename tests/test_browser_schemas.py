from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

import mcp_server.main as main
from mcp_server import browser_schemas
from tests.test_core_catalog_descriptions import _build_mcp


class BrowserSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_tools())}

    def _call(self, name: str, arguments: dict):
        return asyncio.run(self.mcp.call_tool(name, {"description": "Schema check", **arguments}))

    def test_modes_are_advertised_as_enums_inline(self) -> None:
        observe = self.tools["browser_observe"].inputSchema["properties"]
        self.assertEqual(["interactive", "visible", "content", "leaf"], observe["scope"]["enum"])
        self.assertEqual(["none", "viewport", "element", "full_page"], observe["visual"]["enum"])
        act = self.tools["browser_act"].inputSchema
        self.assertEqual(["none", "compact", "full"], act["properties"]["return_state"]["enum"])
        self.assertEqual("compact", act["properties"]["return_state"]["default"])
        self.assertEqual("none", self.tools["browser_do"].inputSchema["properties"]["return_state"]["default"])
        for name in ("browser_observe", "browser_act", "browser_do"):
            text = json.dumps(self.tools[name].inputSchema)
            self.assertNotIn("$ref", text)
            self.assertLess(len(text), 4_000)

    def test_action_items_expose_types_and_per_type_requirements(self) -> None:
        actions = self.tools["browser_act"].inputSchema["properties"]["actions"]
        self.assertEqual(1, actions["minItems"])
        self.assertEqual(20, actions["maxItems"])
        item = actions["items"]
        self.assertIn("select", item["properties"]["type"]["enum"])
        self.assertEqual(["type"], item["required"])
        rules = {tuple(rule["if"]["properties"]["type"]["enum"]): rule["then"] for rule in item["allOf"]}
        self.assertEqual({"required": ["key"]}, rules[("key", "keyboard", "shortcut")])
        self.assertIn({"required": ["handoff_id"]}, rules[("type", "type_text", "paste")]["anyOf"])

    def test_invalid_values_fail_validation_before_the_tool_runs(self) -> None:
        cases = [
            ("browser_observe", {"browser": "Safari", "scope": "everything"}),
            ("browser_observe", {"browser": "Safari", "visual": "huge"}),
            ("browser_act", {"browser": "Safari", "actions": [{"type": "click"}], "return_state": "verbose"}),
            ("browser_act", {"browser": "Safari", "actions": []}),
            ("browser_act", {"browser": "Safari", "actions": [{"type": "hover", "query": "x"}]}),
            ("browser_act", {"browser": "Safari", "actions": [{"type": "type", "query": "Email"}]}),
            ("browser_act", {"browser": "Safari", "actions": [{"type": "key"}]}),
            ("browser_act", {"browser": "Safari", "actions": [{"type": "click"}] * 21}),
            ("browser_do", {"browser": "Safari", "url": "https://example.com", "actions": [{"type": "shortcut"}]}),
        ]
        for name, arguments in cases:
            with self.subTest(tool=name, arguments=arguments):
                with patch.object(main, "browser_observe", side_effect=AssertionError("ran")), \
                        patch.object(main, "browser_act", side_effect=AssertionError("ran")), \
                        patch.object(main, "browser_open_url", side_effect=AssertionError("ran")):
                    with self.assertRaises(ToolError) as ctx:
                        self._call(name, arguments)
                self.assertNotIn("ran", str(ctx.exception))

    def test_aliases_and_omitted_values_still_reach_the_tool(self) -> None:
        seen = {}

        def fake_observe(settings, **kwargs):
            seen.setdefault("observe", []).append(kwargs)
            return {"ok": True}

        def fake_act(settings, **kwargs):
            seen["act"] = kwargs
            return {"ok": True}

        with patch.object(main, "browser_observe", side_effect=fake_observe), \
                patch.object(main, "browser_act", side_effect=fake_act):
            self._call("browser_observe", {"browser": "Safari", "scope": "PAGE", "visual": "Viewport"})
            self._call("browser_observe", {"browser": "Safari"})
            self._call("browser_act", {"browser": "Safari", "return_state": "Full", "actions": [
                {"action": "Click", "query": "Continue"},
                {"type": "type", "handoff_id": "handoff_x"},
                {"type": "type", "text": ""},
                {"kind": "key", "key": "Enter"},
                {"type": "wait", "for": "text", "text": "Saved"},
            ]})
        aliased, default = seen["observe"]
        self.assertEqual(("content", "viewport"), (aliased["scope"], aliased["visual"]))
        self.assertEqual(("interactive", "none"), (default["scope"], default["visual"]))
        self.assertEqual("full", seen["act"]["return_state"])
        self.assertEqual("Click", seen["act"]["actions"][0]["action"])
        self.assertEqual(5, len(seen["act"]["actions"]))

    def test_explicit_null_mode_means_the_default(self) -> None:
        self.assertEqual("compact", browser_schemas._mode("compact")(None))
        self.assertEqual("content", browser_schemas._mode("interactive", {"all": "content"})(" ALL "))


if __name__ == "__main__":
    unittest.main()
