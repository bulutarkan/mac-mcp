"""#257: a saved recipe as a scheduled task. It runs at a time or when a file arrives, with values
checked when the task is saved, through the server's normal recipe path, and fails clearly when the
recipe is a draft, paused or gone. No model is called."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import time
from unittest.mock import patch

from mcp_server import folder_triggers, recipes
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests.test_scheduled_tasks import StoreCase

STEPS = [{"id": "note", "tool": "mac_app",
          "arguments": {"app": "Notes", "action": "create_note", "title": "Report for {{month}}", "body": "{{body}}"}}]
PARAMETERS = {"month": {"type": "string", "required": True}, "count": {"type": "integer", "default": 3},
              "body": {"type": "string", "required": False}}


class RecipeTaskCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"MAC_MCP_RECIPE_DIR": str(self.dir / "recipes")})
        env.start()
        self.addCleanup(env.stop)
        draft = recipes.capture_draft("Monthly note", steps=STEPS, plan_version=2, budgets={}, plan_result={"ok": True})
        self.recipe_id = draft["recipe_id"]
        recipes.update_recipe(self.recipe_id, parameters=PARAMETERS)
        recipes.set_status(self.recipe_id, "active", confirm=True)
        self.calls = []
        runner.set_recipe_runner(self.fake_runner)
        self.addCleanup(runner.set_recipe_runner, None)
        self.answer = {"ok": True, "recipe": {"name": "Monthly note"}, "plan_stats": {"steps_executed": 1},
                       "run_id": "run_timeline1"}

    def fake_runner(self, recipe_id, values, limit_s):
        self.calls.append((recipe_id, values, limit_s))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def task(self, **extra):
        return {"name": "Monthly note", "kind": "recipe", "recipe_id": self.recipe_id, "values": {"month": "October"},
                "schedule": {"type": "monthly", "time": "09:00", "day": 1}, **extra}


class RecipeTaskDefinitionTests(RecipeTaskCase):
    def test_values_are_checked_when_the_task_is_saved(self) -> None:
        task = store.create_task(self.task(values={"month": "October", "count": "5"}))
        self.assertEqual({"month": "October", "count": 5}, task["values"])
        self.assertEqual(("Monthly note", "active"), (task["recipe"]["name"], task["recipe"]["status"]))
        for values, code in (({}, "recipe_invalid"), ({"month": "x", "colour": "red"}, "recipe_invalid"),
                             ({"month": "x", "count": "many"}, "recipe_invalid"), ("month", "recipe_values_invalid"),
                             ({"month": {"nested": 1}}, "recipe_values_invalid")):
            with self.subTest(values=values), self.assertRaises(store.TaskError) as bad:
                store.create_task(self.task(values=values))
            self.assertEqual(code, bad.exception.code)

    def test_a_recipe_task_has_no_prompt_or_script_and_needs_a_real_recipe(self) -> None:
        for extra, code in (({"prompt": "do it"}, "recipe_fields"), ({"script": "/tmp/x.sh"}, "recipe_fields"),
                            ({"recipe_id": "rcp_000000000000"}, "recipe_invalid"), ({"recipe_id": "nope"}, "recipe_invalid")):
            with self.subTest(extra=extra), self.assertRaises(store.TaskError) as bad:
                store.create_task(self.task(**extra))
            self.assertEqual(code, bad.exception.code)

    def test_file_and_date_words_are_kept_until_the_run_fills_them(self) -> None:
        with self.assertRaises(store.TaskError) as timed:
            store.create_task(self.task(values={"month": "{file}"}))
        self.assertEqual("file_token_needs_folder", timed.exception.code)
        task = store.create_task(self.task(values={"month": "{file}"},
                                           schedule={"type": "folder", "folder": str(self.folder)}))
        self.assertEqual("{file}", task["values"]["month"])
        filled = folder_triggers.with_file(store.definition_of(task["id"]), "/Users/me/Downloads/Invoice (2).pdf")
        self.assertEqual("/Users/me/Downloads/Invoice (2).pdf", filled["values"]["month"])


class RecipeTaskRunTests(RecipeTaskCase):
    def execute(self, definition):
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        runner.execute(task["id"], run_id, store.definition_of(task["id"]), None)
        return store.get_run(run_id)

    def test_a_run_replays_the_recipe_with_its_values(self) -> None:
        run = self.execute(self.task(values={"month": "{today}", "count": 2}, timeout_min=5))
        self.assertEqual(("done", "Monthly note ran: 1 step"), (run["status"], run["summary"]))
        recipe_id, values, limit_s = self.calls[0]
        self.assertEqual((self.recipe_id, 300), (recipe_id, limit_s))
        self.assertRegex(values["month"], r"^\d{4}-\d{2}-\d{2}$")  # the date word became this run's date
        self.assertEqual(2, values["count"])
        self.assertEqual("run_timeline1", run["detail"]["recipe"]["timeline_run_id"])

    def test_a_failed_or_refused_recipe_says_why(self) -> None:
        self.answer = {"ok": False, "reason_code": "RECIPE_PAUSED", "message": "This recipe is paused; resume it before running."}
        run = self.execute(self.task())
        self.assertEqual("failed", run["status"])
        self.assertIn("paused", run["reason"])
        self.answer = {"ok": False, "reason_code": "ACTION_NO_EFFECT", "message": "The click had no effect", "step_id": "note"}
        self.assertIn("(step note)", self.execute(self.task())["reason"])

    def test_the_time_limit_and_a_missing_runner_are_reported(self) -> None:
        self.answer = TimeoutError()
        run = self.execute(self.task(timeout_min=5))
        self.assertEqual(("timed_out", "Stopped at its 5-minute time limit"), (run["status"], run["reason"]))
        runner.set_recipe_runner(None)
        run = self.execute(self.task())
        self.assertEqual("failed", run["status"])
        self.assertIn("couldn't run recipes yet", run["reason"])

    def test_paused_observation_skips_a_recipe_run(self) -> None:
        self.privacy(observation_paused=True)
        run = self.execute(self.task())
        self.assertEqual(("skipped", runner.REASON_PAUSED_RECIPE), (run["status"], run["reason"]))
        self.assertEqual([], self.calls)

    def test_a_deleted_recipe_shows_on_the_task(self) -> None:
        task = store.create_task(self.task())
        recipes.delete_recipe(self.recipe_id, confirm=True)
        self.assertEqual("missing", store.get_task(task["id"])["recipe"]["status"])


if __name__ == "__main__":
    import unittest
    unittest.main()
