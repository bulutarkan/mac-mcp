from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings

TOKEN = "resilience-dashboard-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class AgentResilienceDashboardTests(unittest.TestCase):
    def test_agents_route_exposes_turn_checkpoint_and_throttle_telemetry(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN))
            agent = {
                "agent_id": "agt_resilience", "team_id": "team_graph", "team_task_id": "review",
                "status": "running", "phase": "throttled",
                "provider": "chatgpt", "model": "GPT-5.6 Sol", "reasoning": "high",
                "turn_count": 3, "turn_elapsed_ms": 620000, "turn_budget_s": 900,
                "hard_tool_budget_s": 1200, "checkpoint_count": 2, "checkpoint_pending": False,
                "last_checkpoint_at": 100.0, "workflow_id": "wf_testresume1234",
                "resume_generation": 2, "checkpoint_state": "interrupted",
                "checkpoint_safety": "verified", "checkpoint_reason": None,
                "side_effect_receipt_count": 3, "pending_side_effect_count": 0,
                "checkpoint_cursor": {"seq": 7, "kind": "mcp_tool_completed", "resume_generation": 2, "at": 119.0},
                "last_durable_checkpoint_at": 120.0,
                "resumable": True, "throttle_count": 1, "last_throttled_at": 110.0,
                "last_throttle_reason": "requesting_too_fast", "cooldown_until": 200.0,
                "admission_generation": 7,
                "resource_activity": [
                    {"kind": "browser_tab", "mode": "write", "label": "Browser tab"},
                ],
                "admission_resources": [
                    {"kind": "browser_tab", "mode": "write", "id": "secret-tab-handle"},
                ],
            }
            admission = {
                "global_active": 2, "global_limit": 8, "provider_active": {"chatgpt": 1},
                "provider_limits": {"chatgpt": 8}, "queued_count": 3,
            }
            with patch(
                "mcp_server.dashboard_routes.list_agents",
                return_value={"ok": True, "count": 1, "agents": [agent], "global_admission": admission},
            ):
                response = TestClient(app).get("/dashboard/api/agents", headers=AUTH)
            self.assertEqual(200, response.status_code)
            row = response.json()["agents"][0]
            self.assertEqual("review", row["team_task_id"])
            self.assertEqual(620000, row["turn_elapsed_ms"])
            self.assertEqual(2, row["checkpoint_count"])
            self.assertEqual(1, row["throttle_count"])
            self.assertEqual("requesting_too_fast", row["last_throttle_reason"])
            self.assertEqual("wf_testresume1234", row["workflow_id"])
            self.assertEqual(2, row["resume_generation"])
            self.assertEqual("verified", row["checkpoint_safety"])
            self.assertEqual(3, row["side_effect_receipt_count"])
            self.assertEqual(0, row["pending_side_effect_count"])
            self.assertEqual("mcp_tool_completed", row["checkpoint_cursor"]["kind"])
            self.assertEqual(7, row["checkpoint_cursor"]["seq"])
            self.assertTrue(row["resumable"])
            self.assertEqual(7, row["admission_generation"])
            self.assertEqual(
                [{"kind": "browser_tab", "mode": "write", "label": "Browser tab"}],
                row["resource_activity"],
            )
            self.assertNotIn("admission_resources", row)
            self.assertNotIn("secret-tab-handle", response.text)
            global_admission = response.json()["global_admission"]
            self.assertEqual(2, global_admission["global_active"])
            self.assertEqual(8, global_admission["global_limit"])
            self.assertEqual(3, global_admission["queued_count"])

    def test_agents_route_exposes_bounded_team_summary(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN))
            agent = {
                "agent_id": "agt_team_child",
                "team_id": "team_notify",
                "team_task_id": "task_1",
                "status": "completed",
                "phase": "completed",
                "title": "Child title",
                "provider": "codex",
                "model": "gpt-test",
            }
            team = {
                "team_id": "team_notify",
                "status": "completed_with_failures",
                "success": False,
                "outcome": "partial_failure",
                "partial_failure": True,
                "successful_count": 4,
                "failure_count": 1,
                "pending_count": 0,
                "work_count": 5,
                "title": "Release audit",
                "provider": "codex",
                "model": "gpt-test",
                "created_at": 100.0,
                "updated_at": 120.0,
                "count": 5,
                "terminal_count": 5,
                "failure_reasons": [{"reason": "internal-detail"}],
                "result_fan_in": {"output": "internal-detail"},
                "scope": {"workspace_roots": ["private-workspace"]},
            }
            with patch(
                "mcp_server.dashboard_routes.list_agents",
                return_value={"ok": True, "count": 1, "agents": [agent], "global_admission": None},
            ), patch(
                "mcp_server.dashboard_routes.dashboard_team_summary",
                return_value=team,
            ) as team_summary:
                client = TestClient(app)
                response = client.get("/dashboard/api/agents", headers=AUTH)
                cached_response = client.get("/dashboard/api/agents", headers=AUTH)

            self.assertEqual(200, response.status_code)
            self.assertEqual(200, cached_response.status_code)
            team_summary.assert_called_once_with("team_notify")
            payload = response.json()
            self.assertEqual(1, len(payload["teams"]))
            row = payload["teams"][0]
            self.assertEqual("team_notify", row["team_id"])
            self.assertEqual("completed_with_failures", row["status"])
            self.assertEqual(5, row["work_count"])
            self.assertEqual(5, row["terminal_count"])
            self.assertNotIn("failure_reasons", row)
            self.assertNotIn("result_fan_in", row)
            self.assertNotIn("scope", row)
            self.assertNotIn("internal-detail", response.text)
            self.assertNotIn("private-workspace", response.text)

    def test_agents_route_keeps_all_teams_in_native_twenty_agent_window(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN))
            agents = [
                {
                    "agent_id": f"agt_{index}",
                    "team_id": f"team_{index}",
                    "team_task_id": "task",
                    "status": "running",
                    "phase": "running",
                    "provider": "codex",
                    "model": "gpt-test",
                }
                for index in range(20)
            ]

            def summary(team_id):
                return {
                    "team_id": team_id,
                    "status": "running",
                    "success": False,
                    "outcome": "running",
                    "partial_failure": False,
                    "successful_count": 0,
                    "failure_count": 0,
                    "pending_count": 1,
                    "work_count": 1,
                    "title": team_id,
                    "provider": "codex",
                    "model": "gpt-test",
                    "created_at": 100.0,
                    "updated_at": 100.0,
                    "count": 1,
                    "terminal_count": 0,
                }

            with patch(
                "mcp_server.dashboard_routes.list_agents",
                return_value={"ok": True, "count": 20, "agents": agents, "global_admission": None},
            ), patch(
                "mcp_server.dashboard_routes.dashboard_team_summary",
                side_effect=summary,
            ) as team_summary:
                response = TestClient(app).get("/dashboard/api/agents?limit=20", headers=AUTH)

            self.assertEqual(200, response.status_code)
            self.assertEqual(20, len(response.json()["teams"]))
            self.assertEqual(20, team_summary.call_count)

    def test_dashboard_js_surfaces_resilience_counts(self):
        source = (Path(__file__).resolve().parents[1] / "mcp_server/dashboard/dashboard.js").read_text(encoding="utf-8")
        self.assertIn("agent.turn_elapsed_ms", source)
        self.assertIn("agent.checkpoint_count", source)
        self.assertIn("agent.throttle_count", source)
        self.assertIn("checkpoints", source)
        self.assertIn("throttles", source)
        self.assertIn("Global scheduler", source)
        self.assertIn("admission.global_active", source)
        self.assertIn("admission.global_limit", source)
        self.assertIn("admission.queued_count", source)


if __name__ == "__main__":
    unittest.main()
