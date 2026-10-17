"""Settings › Automation › Recipes: the owner lists every recipe (drafts too), reads its steps, turns it
on, pauses, resumes and deletes it through owner-token dashboard routes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import recipes
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {TOKEN}"}
STEPS = [{"id": "status", "tool": "mac_snapshot", "arguments": {"description": "Check", "sections": ["system"]}}]


class RecipeManageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-recipes-manage-")
        env = patch.dict(os.environ, {"MAC_MCP_RECIPE_DIR": str(Path(self.temp.name) / "recipes")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.temp.cleanup)
        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3")
        self.client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))
        self.recipe_id = recipes.capture_draft("Status", steps=STEPS, plan_version=2, budgets={},
                                               plan_result={"ok": True})["recipe_id"]

    def action(self, name: str, **extra):
        return self.client.post("/dashboard/api/recipes/action", headers=AUTH,
                                json={"recipe_id": self.recipe_id, "action": name, **extra})

    def test_drafts_are_listed_with_their_steps_for_the_owner(self) -> None:
        self.assertEqual(401, self.client.get("/dashboard/api/recipes/manage").status_code)
        listed = self.client.get("/dashboard/api/recipes/manage", headers=AUTH).json()["recipes"]
        self.assertEqual([("Status", "draft")], [(item["name"], item["status"]) for item in listed])
        # The launcher list (Shortcuts, links) still shows only recipes that are turned on.
        self.assertEqual([], self.client.get("/dashboard/api/recipes", headers=AUTH).json()["recipes"])
        detail = self.client.get(f"/dashboard/api/recipes/detail?recipe_id={self.recipe_id}", headers=AUTH).json()
        self.assertEqual("mac_snapshot", detail["recipe"]["steps"][0]["tool"])

    def test_turn_on_pause_resume_and_delete(self) -> None:
        self.assertEqual(401, self.client.post("/dashboard/api/recipes/action",
                                               json={"recipe_id": self.recipe_id, "action": "activate"}).status_code)
        self.assertEqual("active", self.action("activate").json()["recipe"]["status"])
        self.assertEqual(1, len(self.client.get("/dashboard/api/recipes", headers=AUTH).json()["recipes"]))
        self.assertEqual("paused", self.action("pause").json()["recipe"]["status"])
        self.assertEqual("active", self.action("resume").json()["recipe"]["status"])
        self.assertTrue(self.action("delete").json()["deleted"])
        missing = self.client.get(f"/dashboard/api/recipes/detail?recipe_id={self.recipe_id}", headers=AUTH)
        self.assertEqual(404, missing.status_code)

    def test_a_secret_in_the_steps_still_blocks_turning_it_on(self) -> None:
        secret = recipes.capture_draft("Leaky", steps=[{"id": "s", "tool": "mac_app", "arguments": {
            "app": "Notes", "action": "create_note", "title": "key", "body": "sk-ant-api03-" + "x" * 40}}],
            plan_version=2, budgets={}, plan_result={"ok": True})["recipe_id"]
        refused = self.client.post("/dashboard/api/recipes/action", headers=AUTH,
                                   json={"recipe_id": secret, "action": "activate"})
        self.assertEqual((409, "RECIPE_CONTAINS_SECRET"), (refused.status_code, refused.json()["error"]))

    def test_bad_requests(self) -> None:
        self.assertEqual(400, self.client.post("/dashboard/api/recipes/action", headers=AUTH,
                                               json={"recipe_id": "nope", "action": "activate"}).status_code)
        self.assertEqual(400, self.action("run_everything").status_code)


if __name__ == "__main__":
    unittest.main()
