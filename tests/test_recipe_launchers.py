from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import cli, recipes
from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_TOKEN = "dashboard-test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}
STEPS = [{"id": "find", "tool": "mac_app", "arguments": {"app": "Reminders", "action": "list_reminders", "query": "{{word}}"}}]


class LauncherApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mac-mcp-launcher-")
        self.env = patch.dict(os.environ, {"MAC_MCP_RECIPE_DIR": str(Path(self.temp.name) / "recipes")})
        self.env.start()
        self.runs = []

        async def runner(recipe_id, values):
            self.runs.append((recipe_id, values))
            if values.get("word") == "approve":
                return {"ok": False, "status": "approval_required", "reason_code": "APPROVAL_REQUIRED", "message": "needs approval"}
            return {"ok": True, "recipe": {"name": "Find"}, "plan_stats": {"steps_executed": 1}}

        telemetry = TelemetryManager(db_path=Path(self.temp.name) / "telemetry.sqlite3")
        self.client = TestClient(Starlette(routes=create_dashboard_routes(
            telemetry, load_settings(), DASHBOARD_TOKEN, recipe_runner=runner)))

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def _recipe(self, activate: bool = True) -> str:
        recipe_id = recipes.capture_draft("Find", steps=STEPS, plan_version=2, budgets={}, plan_result={"ok": True})["recipe_id"]
        recipes.update_recipe(recipe_id, parameters={"word": {"type": "string"}})
        if activate:
            recipes.set_status(recipe_id, "active", confirm=True)
        return recipe_id

    def test_list_needs_auth_and_hides_drafts(self) -> None:
        active = self._recipe()
        self._recipe(activate=False)
        self.assertEqual(401, self.client.get("/dashboard/api/recipes").status_code)
        listed = self.client.get("/dashboard/api/recipes", headers=AUTH).json()
        self.assertEqual([active], [item["recipe_id"] for item in listed["recipes"]])
        self.assertEqual({"word"}, set(listed["recipes"][0]["parameters"]))

    def test_run_accepts_only_an_id_and_plain_values(self) -> None:
        recipe_id = self._recipe()
        self.assertEqual(401, self.client.post("/dashboard/api/recipes/run", json={"recipe_id": recipe_id}).status_code)
        for payload, error in (
            ({"recipe_id": "run_command"}, "invalid_recipe_id"),
            ({"recipe_id": recipe_id, "values": {"word": {"nested": 1}}}, "invalid_values"),
            ({"recipe_id": recipe_id, "values": ["x"]}, "invalid_values"),
            ({"recipe_id": recipe_id, "values": {f"k{i}": i for i in range(21)}}, "invalid_values"),
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/dashboard/api/recipes/run", json=payload, headers=AUTH)
                self.assertEqual(400, response.status_code)
                self.assertEqual(error, response.json()["error"])
        self.assertEqual([], self.runs)
        done = self.client.post("/dashboard/api/recipes/run", json={"recipe_id": recipe_id, "values": {"word": "milk"}}, headers=AUTH).json()
        self.assertEqual({"ok": True, "status": "completed", "name": "Find", "steps_executed": 1},
                         {k: done[k] for k in ("ok", "status", "name", "steps_executed")})
        pending = self.client.post("/dashboard/api/recipes/run", json={"recipe_id": recipe_id, "values": {"word": "approve"}}, headers=AUTH).json()
        self.assertEqual("approval_required", pending["status"])
        self.assertEqual([(recipe_id, {"word": "milk"}), (recipe_id, {"word": "approve"})], self.runs)

    def test_launcher_runs_go_through_the_recipe_tool_as_the_local_launcher(self) -> None:
        source = (ROOT / "mcp_server" / "main.py").read_text(encoding="utf-8")
        runner = source[source.index("async def run_recipe_for_launcher"):source.index("recipe_runner=run_recipe_for_launcher")]
        self.assertIn('environment_policy_context(actor="local_launcher")', runner)
        self.assertIn('await mcp.call_tool("recipe", arguments)', runner)
        self.assertIn('"approval_required"', runner)
        self.assertIn("reset_policy_context(token)", runner)


class RecipeCliTests(unittest.TestCase):
    def run_cli(self, argv, reply):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "_recipe_request", return_value=reply) as request, \
             patch.object(cli, "_load_env"), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue(), request

    def test_list_and_run_exit_codes(self) -> None:
        code, out, _, _ = self.run_cli(["recipe", "list"], (0, {"recipes": [
            {"recipe_id": "rcp_0123456789ab", "status": "active", "name": "Find",
             "parameters": {"word": {"type": "string", "required": True}}}]}))
        self.assertEqual(0, code)
        self.assertIn("rcp_0123456789ab  [active]  Find  (word:string)", out)

        code, out, _, request = self.run_cli(
            ["recipe", "run", "rcp_0123456789ab", "--param", "word=milk", "--param", "note=a=b"],
            (0, {"ok": True, "name": "Find", "steps_executed": 1}))
        self.assertEqual(0, code)
        self.assertEqual(("POST", "/dashboard/api/recipes/run",
                          {"recipe_id": "rcp_0123456789ab", "values": {"word": "milk", "note": "a=b"}}), request.call_args.args)
        self.assertEqual(2, self.run_cli(["recipe", "run", "rcp_0123456789ab"], (0, {"ok": False, "status": "approval_required"}))[0])
        self.assertEqual(1, self.run_cli(["recipe", "run", "rcp_0123456789ab"], (0, {"ok": False, "status": "failed"}))[0])
        self.assertEqual(3, self.run_cli(["recipe", "run", "rcp_0123456789ab"], (3, {"error": "server_unreachable", "message": "down"}))[0])
        self.assertEqual(1, self.run_cli(["recipe", "run", "rcp_0123456789ab", "--param", "oops"], (0, {}))[0])

    def test_json_output_stays_json_when_the_server_is_down(self) -> None:
        down = (3, {"error": "server_unreachable", "message": "down"})
        for argv in (["recipe", "list", "--json"], ["recipe", "run", "rcp_0123456789ab", "--json"]):
            with self.subTest(argv=argv):
                code, out, err, _ = self.run_cli(argv, down)
                self.assertEqual(3, code)
                self.assertEqual("server_unreachable", json.loads(out)["error"])
                self.assertEqual("", err)

    def test_unreachable_server_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            token = Path(td) / "dashboard-token"
            token.write_text("test-token-value", encoding="utf-8")
            with patch.object(cli, "_default_port", return_value=1), \
                 patch.object(cli, "dashboard_token_path", return_value=token):
                code, body = cli._recipe_request("GET", "/dashboard/api/recipes")
        self.assertEqual(3, code)
        self.assertEqual("server_unreachable", body["error"])


HARNESS = r'''
import Foundation

@main
struct LauncherParseHarness {
    static func main() {
        let good = RecipeLauncher.parse(URL(string: "macmcp://recipe/run?id=rcp_0123456789ab&month=November&count=3")!)
        precondition(good == RecipeLauncher.LaunchRequest(recipeID: "rcp_0123456789ab", values: ["month": "November", "count": "3"]))
        let rejected = [
            "macmcp://recipe/run?id=../../etc",
            "macmcp://recipe/run?month=x",
            "macmcp://recipe/delete?id=rcp_0123456789ab",
            "macmcp://tool/run?id=rcp_0123456789ab",
            "https://recipe/run?id=rcp_0123456789ab",
            "macmcp://recipe/run?id=rcp_0123456789ab&Bad-Name=1",
        ]
        for raw in rejected {
            precondition(RecipeLauncher.parse(URL(string: raw)!) == nil, raw)
        }
        print("LAUNCHER_PARSE_PASS")
    }
}
'''


@unittest.skipUnless(shutil.which("xcrun"), "Swift toolchain is not installed")
class RecipeUrlParseTests(unittest.TestCase):
    def test_only_well_formed_recipe_links_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = Path(td) / "LauncherParseHarness.swift"
            binary = Path(td) / "launcher-parse"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            sources = ROOT / "menu_app" / "Sources"
            built = subprocess.run(
                ["xcrun", "swiftc", "-parse-as-library", str(sources / "RecipeLauncher.swift"),
                 str(sources / "AgentNotificationController.swift"), str(harness),
                 "-framework", "AppKit", "-framework", "UserNotifications", "-o", str(binary)],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn("LAUNCHER_PARSE_PASS", run.stdout)

    def test_scheme_is_registered_and_links_need_confirmation(self) -> None:
        plist = (ROOT / "menu_app" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("<string>macmcp</string>", plist)
        launcher = (ROOT / "menu_app" / "Sources" / "RecipeLauncher.swift").read_text(encoding="utf-8")
        run = launcher[launcher.index("private func run("):launcher.index("private func confirm(")]
        self.assertLess(run.index("guard confirm(summary"), run.index("dashboard/api/recipes/run"))
        self.assertIn("RecipeLauncher.swift", (ROOT / "menu_app" / "build_app.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
