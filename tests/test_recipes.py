from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import recipes
from mcp_server.computer_plan import execute_computer_plan

STEPS = [
    {"id": "tabs", "tool": "browser_list_tabs", "arguments": {"browser": "Safari"}},
    {"id": "type", "tool": "browser_act", "arguments": {
        "browser": "Safari", "tab_handle": "btab_x",
        "actions": [{"type": "type", "role": "textbox", "text": "Invoice October 2026"}],
    }},
]
OK_RESULT = {"ok": True, "plan_stats": {"steps_executed": 2}}


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-recipes-")
        self.env = patch.dict(os.environ, {"MAC_MCP_RECIPE_DIR": self.temp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def capture(self, steps=STEPS) -> str:
        draft = recipes.capture_draft("Monthly invoice", steps=steps, plan_version=2,
                                      budgets={"max_seconds": 30}, plan_result=OK_RESULT)
        return draft["recipe_id"]

    def test_capture_saves_a_private_draft_only_for_a_successful_plan(self) -> None:
        with self.assertRaises(recipes.RecipeError) as failed:
            recipes.capture_draft("x", steps=STEPS, plan_version=2, budgets={}, plan_result={"ok": False})
        self.assertEqual("RECIPE_CAPTURE_FAILED_PLAN", failed.exception.code)
        recipe_id = self.capture()
        path = Path(self.temp.name) / f"{recipe_id}.json"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(Path(self.temp.name).stat().st_mode))
        recipe = recipes.inspect_recipe(recipe_id)["recipe"]
        self.assertEqual("draft", recipe["status"])
        self.assertTrue(recipe["consequential"])
        self.assertIn({"path": "1.arguments.actions.0.text", "value": "Invoice October 2026"}, recipe["review"]["literal_values"])
        with self.assertRaises(recipes.RecipeError) as draft_run:
            recipes.prepare_run(recipe_id, {})
        self.assertEqual("RECIPE_NOT_ACTIVE", draft_run.exception.code)

    def test_parameterize_only_touches_argument_values(self) -> None:
        recipe_id = self.capture()
        updated = recipes.update_recipe(
            recipe_id,
            parameters={"month": {"type": "string", "enum": ["October", "November"]}, "year": {"type": "integer"}},
            parameterize=[{"literal": "October", "param": "month"}, {"literal": "2026", "param": "year"}],
        )
        self.assertEqual({"month": 1, "year": 1}, updated["replaced"])
        text = updated["recipe"]["steps"][1]["arguments"]["actions"][0]["text"]
        self.assertEqual("Invoice {{month}} {{year}}", text)
        with self.assertRaises(recipes.RecipeError) as missing:
            recipes.update_recipe(recipe_id, parameterize=[{"literal": "Not There", "param": "x"}])
        self.assertEqual("RECIPE_LITERAL_NOT_FOUND", missing.exception.code)
        with self.assertRaises(recipes.RecipeError) as structural:
            recipes.update_recipe(recipe_id, parameterize=[{"literal": "browser_act", "param": "tool_name"}])
        self.assertEqual("RECIPE_LITERAL_NOT_FOUND", structural.exception.code)

    def test_templates_in_structural_keys_are_rejected(self) -> None:
        recipe_id = self.capture([{"id": "a", "tool": "{{tool}}", "arguments": {}}])
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.inspect_recipe(recipe_id)
        self.assertEqual("RECIPE_TEMPLATE_NOT_ALLOWED", ctx.exception.code)

    def test_activation_needs_confirmation_and_no_secret_like_values(self) -> None:
        recipe_id = self.capture()
        preview = recipes.set_status(recipe_id, "active")
        self.assertFalse(preview["ok"])
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual("active", recipes.set_status(recipe_id, "active", confirm=True)["recipe"]["status"])

        secret_steps = [{"id": "a", "tool": "browser_act", "arguments": {
            "browser": "Safari", "tab_handle": "t", "actions": [{"type": "type", "role": "textbox", "text": "api_key=sk-abcdefghijklmnop123456"}]}}]
        secret_id = self.capture(secret_steps)
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.set_status(secret_id, "active", confirm=True)
        self.assertEqual("RECIPE_CONTAINS_SECRET", ctx.exception.code)

    def test_run_validates_values_and_keeps_types(self) -> None:
        steps = [{"id": "find", "tool": "mac_app", "arguments": {
            "app": "Reminders", "action": "list_reminders", "query": "{{word}}", "limit": "{{count}}"}}]
        recipe_id = self.capture(steps)
        recipes.update_recipe(recipe_id, parameters={"word": {"type": "string", "max_length": 10},
                                                     "count": {"type": "integer", "default": 5}})
        recipes.set_status(recipe_id, "active", confirm=True)
        prepared = recipes.prepare_run(recipe_id, {"word": "milk"})
        self.assertEqual({"app": "Reminders", "action": "list_reminders", "query": "milk", "limit": 5},
                         prepared["steps"][0]["arguments"])
        prepared = recipes.prepare_run(recipe_id, {"word": "eggs", "count": "3"})
        self.assertEqual(3, prepared["steps"][0]["arguments"]["limit"])
        for values, code in (
            ({}, "RECIPE_PARAMETER_MISSING"),
            ({"word": "x" * 11}, "RECIPE_PARAMETER_INVALID"),
            ({"word": "a", "count": "many"}, "RECIPE_PARAMETER_INVALID"),
            ({"word": "a", "other": 1}, "RECIPE_PARAMETER_INVALID"),
        ):
            with self.subTest(values=values), self.assertRaises(recipes.RecipeError) as ctx:
                recipes.prepare_run(recipe_id, values)
            self.assertEqual(code, ctx.exception.code)

    def test_editing_steps_requires_review_again_and_pause_blocks_runs(self) -> None:
        recipe_id = self.capture()
        recipes.set_status(recipe_id, "active", confirm=True)
        recipes.update_recipe(recipe_id, description="Download the monthly invoice")
        self.assertEqual("active", recipes.inspect_recipe(recipe_id)["recipe"]["status"])
        recipes.update_recipe(recipe_id, parameterize=[{"literal": "October", "param": "month"}])
        self.assertEqual("draft", recipes.inspect_recipe(recipe_id)["recipe"]["status"])
        recipes.set_status(recipe_id, "active", confirm=True)
        recipes.set_status(recipe_id, "paused")
        with self.assertRaises(recipes.RecipeError) as ctx:
            recipes.prepare_run(recipe_id, {"month": "May"})
        self.assertEqual("RECIPE_PAUSED", ctx.exception.code)
        self.assertEqual("active", recipes.set_status(recipe_id, "active")["recipe"]["status"])

    def test_delete_needs_confirmation(self) -> None:
        recipe_id = self.capture()
        self.assertTrue(recipes.delete_recipe(recipe_id)["confirmation_required"])
        self.assertTrue(recipes.delete_recipe(recipe_id, confirm=True)["deleted"])
        with self.assertRaises(recipes.RecipeError):
            recipes.inspect_recipe(recipe_id)
        with self.assertRaises(recipes.RecipeError) as bad:
            recipes.inspect_recipe("../etc/passwd")
        self.assertEqual("RECIPE_ID_INVALID", bad.exception.code)

    def test_prepared_steps_run_through_computer_plan_and_record_the_run(self) -> None:
        steps = [{"id": "find", "tool": "mac_app", "arguments": {
            "app": "Reminders", "action": "list_reminders", "query": "{{word}}"}}]
        recipe_id = self.capture(steps)
        recipes.update_recipe(recipe_id, parameters={"word": {"type": "string"}})
        recipes.set_status(recipe_id, "active", confirm=True)
        prepared = recipes.prepare_run(recipe_id, {"word": "milk"})
        calls = []

        async def call_tool(name, arguments):
            calls.append((name, dict(arguments)))
            return {"ok": True, "count": 0, "reminders": []}

        result = asyncio.run(execute_computer_plan(call_tool, steps=prepared["steps"], plan_version=prepared["plan_version"]))
        self.assertTrue(result["ok"], result)
        self.assertEqual([("mac_app", {"app": "Reminders", "action": "list_reminders", "query": "milk"})], calls)
        last = recipes.record_run(recipe_id, result)
        self.assertTrue(last["ok"])
        self.assertEqual(1, recipes.inspect_recipe(recipe_id)["recipe"]["run_count"])


if __name__ == "__main__":
    unittest.main()
