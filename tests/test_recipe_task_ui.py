"""#257: the task editor offers Recipe as a third kind with a recipe picker and its values; cards and
the approval sheet show which recipe a task replays and whether it can run."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests._menu_sources import feature_source

SOURCES = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
PANE = (SOURCES / "ScheduledTasksPane.swift").read_text(encoding="utf-8")


class RecipeTaskUITests(unittest.TestCase):
    def test_the_editor_has_a_recipe_kind_that_sends_recipe_fields(self) -> None:
        self.assertIn('kindCard(kind: "recipe", title: "Recipe", detail: "Replay saved steps")', PANE)
        self.assertIn('"kind": isRecipe ? "recipe" : isAI ? "ai" : "script"', PANE)
        self.assertIn('out["recipe_id"] = recipeID', PANE)
        self.assertIn('state.taskAPI("dashboard/api/recipes/manage")', PANE)
        self.assertIn("if model.isRecipe { recipeFields } else if model.isAI { aiFields } else { scriptFields }", PANE)

    def test_cards_and_review_show_the_recipe(self) -> None:
        self.assertIn('factRow("play.square.stack", "Replays", recipeLine', PANE)
        self.assertIn("— paused, so runs fail until you resume it in Recipes", PANE)
        self.assertIn('if (proposal["kind"] as? String) == "recipe" {', PANE)
        self.assertIn('var isRecipe: Bool { kind == "recipe" }', feature_source("AppState"))


if __name__ == "__main__":
    unittest.main()
