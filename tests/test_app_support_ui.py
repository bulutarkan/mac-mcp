"""#262: Settings › Apps shows the adapters' own support matrix (what each app can do, how, and its
limits) instead of a hand-written summary."""
from __future__ import annotations

import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server import app_adapters

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "apps-support-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class AppSupportOverviewTests(unittest.TestCase):
    def test_every_app_and_action_of_the_matrix_is_shown_once(self) -> None:
        matrix = app_adapters.support_matrix()
        rows = {row["name"]: row for row in app_adapters.owner_overview()}
        self.assertEqual(set(matrix), set(rows))
        for name, entry in matrix.items():
            row = rows[name]
            self.assertEqual(entry["support"], row["support"])
            self.assertEqual(list(entry.get("actions") or []), row["actions"])
            shown = sum(len(row[key]) for key in ("reads", "opens", "changes", "sends", "runs"))
            self.assertEqual(len(row["actions"]), shown, name)  # every action in exactly one group

    def test_limits_follow_the_adapter_rules(self) -> None:
        rows = {row["name"]: row for row in app_adapters.owner_overview()}
        self.assertEqual(["Send mail"], rows["Mail"]["sends"])
        self.assertEqual(["Run shortcut"], rows["Shortcuts"]["runs"])
        self.assertIn("Delete reminder", rows["Reminders"]["changes"])
        self.assertEqual("By looking at the window", rows["Slack"]["support_words"])
        self.assertEqual("Built in", rows["Messages"]["support_words"])
        self.assertEqual([], rows["Messages"]["other_ways"])
        self.assertIn("Read messages", rows["Messages"]["reads"])
        self.assertEqual(["Send message"], rows["Messages"]["sends"])
        ways = {way["task"]: way["how"] for way in rows["Xcode"]["other_ways"]}
        self.assertEqual("By looking at the window and clicking", ways["Inspect window"])
        order = [row["support"] for row in app_adapters.owner_overview()]
        self.assertEqual(sorted(order, key=["first_party", "partial_first_party", "generic_ax"].index), order)

    def test_a_new_adapter_action_shows_up_without_touching_the_app(self) -> None:
        from unittest.mock import patch
        with patch.dict(app_adapters._ACTIONS, {"Notes": app_adapters._ACTIONS["Notes"] + ("export_note",)}):
            notes = next(row for row in app_adapters.owner_overview() if row["name"] == "Notes")
        self.assertIn("Export note", notes["opens"])

    def test_the_owner_route_serves_it(self) -> None:
        import os
        import tempfile
        from unittest.mock import patch as env_patch
        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings
        with tempfile.TemporaryDirectory() as temp, env_patch.dict(os.environ, {"MAC_MCP_STATE_DIR": temp}):
            telemetry = TelemetryManager(db_path=Path(temp) / "t.sqlite3", usage_enabled=False)
            client = TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))
            self.assertEqual(401, client.get("/dashboard/api/apps/support").status_code)
            body = client.get("/dashboard/api/apps/support", headers={"authorization": f"Bearer {TOKEN}"}).json()
        self.assertEqual(app_adapters.owner_overview(), body["apps"])

    def test_settings_lists_the_live_matrix_not_a_written_summary(self) -> None:
        source = (ROOT / "menu_app" / "Sources" / "SettingsView.swift").read_text(encoding="utf-8")
        pane = source[source.index("private var appsPane"):source.index("private func appSendRow")]
        self.assertIn('state.taskAPI("dashboard/api/apps/support")', pane)
        self.assertNotIn("Finder, Notes, Calendar, Reminders, Preview", pane)


if __name__ == "__main__":
    unittest.main()
