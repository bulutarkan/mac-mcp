"""#137: a successful browser_do run can be kept as a recipe draft (browser_do save_as_recipe)."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server.main as main
from mcp_server import recipes
from mcp_server.computer_plan import execute_computer_plan
from tests.test_core_catalog_descriptions import _build_mcp

EXTRACT = {"ok": True, "type": "extract", "url": "https://shop.test/item", "title": "Item",
           "data": {"price": "12 EUR"}}


class BrowserDoRecipeStepsTests(unittest.TestCase):
    def test_only_the_arguments_the_caller_set_are_kept_and_the_step_is_a_valid_plan(self) -> None:
        steps = recipes.browser_do_steps(
            {"browser": "Safari", "url": "https://shop.test/item", "actions": None, "new_tab": True,
             "close_after": True, "extract": ["price"]},
            {"browser": None, "url": None, "actions": None, "new_tab": True, "close_after": False, "extract": None},
        )
        self.assertEqual([{"id": "browse", "tool": "browser_do", "arguments": {
            "browser": "Safari", "url": "https://shop.test/item", "close_after": True, "extract": ["price"]}}], steps)


class BrowserDoSaveAsRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp = _build_mcp()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-browser-do-recipe-")
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"MAC_MCP_PERMISSION_PROFILE": "trusted", "MAC_MCP_RECIPE_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def call(self, arguments: dict, act_result: dict) -> dict:
        with patch.object(main, "browser_act", return_value=act_result):
            result = asyncio.run(self.mcp.call_tool("browser_do", {"description": "Check the price", **arguments}))
        blocks = result[0] if isinstance(result, tuple) else result
        return json.loads(blocks[0].text)

    def test_a_successful_run_becomes_a_draft_that_replays_the_same_call(self) -> None:
        arguments = {"browser": "Safari", "tab_handle": "btab_test", "extract": ["price"]}
        payload = self.call({**arguments, "save_as_recipe": "Price check"},
                            {"ok": True, "actions": [EXTRACT], "action_count": 1})
        self.assertTrue(payload["ok"])
        draft = payload["recipe_draft"]
        self.assertEqual("draft", draft["status"])
        self.assertTrue(draft["review_required"])
        recipe = recipes.inspect_recipe(draft["recipe_id"])["recipe"]
        self.assertEqual([{"id": "browse", "tool": "browser_do", "arguments": arguments}], recipe["steps"])
        self.assertEqual("browser_do", recipe["source"]["captured_from"])
        self.assertTrue(recipe["consequential"])
        # Draft only: it runs after the owner's usual review and activation, never before.
        with self.assertRaises(recipes.RecipeError) as not_active:
            recipes.prepare_run(draft["recipe_id"], {})
        self.assertEqual("RECIPE_NOT_ACTIVE", not_active.exception.code)

        recipes.set_status(draft["recipe_id"], "active", confirm=True)
        prepared = recipes.prepare_run(draft["recipe_id"], {})
        calls: list[tuple[str, dict]] = []

        async def caller(tool: str, args: dict):
            calls.append((tool, dict(args)))
            return {"ok": True, "data": {"price": "12 EUR"}}

        result = asyncio.run(execute_computer_plan(caller, steps=prepared["steps"],
                                                   plan_version=prepared["plan_version"]))
        self.assertTrue(result["ok"])
        self.assertEqual([("browser_do", arguments)], calls)

    def test_a_failed_run_saves_nothing(self) -> None:
        failed = {**EXTRACT, "ok": False, "error": "selector_not_found"}
        payload = self.call({"browser": "Safari", "tab_handle": "btab_test", "extract": ["price"],
                             "save_as_recipe": "Price check"}, {"ok": False, "actions": [failed], "action_count": 1})
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["recipe_draft"]["saved"])
        self.assertEqual([], list(Path(self.temp.name).glob("rcp_*.json")))

    def test_without_save_as_recipe_nothing_is_saved(self) -> None:
        payload = self.call({"browser": "Safari", "tab_handle": "btab_test", "extract": ["price"]},
                            {"ok": True, "actions": [EXTRACT], "action_count": 1})
        self.assertNotIn("recipe_draft", payload)
        self.assertEqual([], list(Path(self.temp.name).glob("rcp_*.json")))


if __name__ == "__main__":
    unittest.main()
