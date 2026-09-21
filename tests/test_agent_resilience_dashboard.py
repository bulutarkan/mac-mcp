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
            global_admission = response.json()["global_admission"]
            self.assertEqual(2, global_admission["global_active"])
            self.assertEqual(8, global_admission["global_limit"])
            self.assertEqual(3, global_admission["queued_count"])

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
