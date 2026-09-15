from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_server.dashboard_routes import create_dashboard_routes
from mcp_server.observability import TelemetryManager
from mcp_server.security import load_settings
from mcp_server.steering import (
    SteeringGenerationMismatch,
    SteeringIdempotencyExpired,
    SteeringIdentity,
    SteeringManager,
)

DASHBOARD_TOKEN = "test-dashboard-token"
DASHBOARD_AUTH = {"authorization": f"Bearer {DASHBOARD_TOKEN}"}


class SteeringGenerationEpochTests(unittest.TestCase):
    def _session(self, manager: SteeringManager, key: str = "client:epoch") -> tuple[SteeringIdentity, str]:
        identity = SteeringIdentity(key=key, source="client_id")
        return identity, manager.session_id_for(identity)

    def test_generation_changes_between_daemon_instances(self) -> None:
        first = SteeringManager()
        second = SteeringManager()
        self.assertTrue(first.generation_id.startswith("gen_"))
        self.assertNotEqual(first.generation_id, second.generation_id)

    def test_old_generation_retry_is_rejected_before_enqueue(self) -> None:
        first = SteeringManager(generation_id="gen_old")
        _identity, sid = self._session(first)
        accepted = first.enqueue(sid, "change direction", client_instruction_id="cli-a", generation_id="gen_old")
        self.assertEqual(1, first.sessions()[0]["pending_instruction_count"])

        restarted = SteeringManager(generation_id="gen_new")
        _identity2, new_sid = self._session(restarted)
        with self.assertRaises(SteeringGenerationMismatch):
            restarted.enqueue(new_sid, "change direction", client_instruction_id="cli-a", generation_id="gen_old")
        self.assertEqual(0, restarted.sessions()[0]["pending_instruction_count"])
        self.assertTrue(accepted["id"].startswith("st_"))

    def test_evicted_idempotency_key_becomes_tombstone_not_new_instruction(self) -> None:
        manager = SteeringManager(
            generation_id="gen_same",
            max_messages_per_session=1,
            max_idempotency_keys_per_session=1,
            max_idempotency_tombstones_per_session=2,
        )
        identity, sid = self._session(manager, "client:tombstone")
        first = manager.enqueue(sid, "first", client_instruction_id="cli-old", generation_id="gen_same")
        manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/a"})
        manager.prepare_call(identity, tool="read_file", arguments={"path": "/tmp/ack-a"})
        manager.enqueue(sid, "second", client_instruction_id="cli-new", generation_id="gen_same")

        with self.assertRaises(SteeringIdempotencyExpired) as ctx:
            manager.enqueue(sid, "first", client_instruction_id="cli-old", generation_id="gen_same")
        self.assertEqual(first["id"], ctx.exception.canonical_message_id)
        self.assertEqual(1, manager.sessions()[0]["pending_instruction_count"])

    def test_dashboard_exposes_generation_and_rejects_stale_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            steering = SteeringManager(generation_id="gen_current")
            identity, sid = self._session(steering, "client:route-epoch")
            telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
            app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), DASHBOARD_TOKEN, steering))
            client = TestClient(app)

            state = client.get("/dashboard/api/steering", headers=DASHBOARD_AUTH)
            self.assertEqual(200, state.status_code)
            self.assertEqual("gen_current", state.json()["generation_id"])

            stale = client.post(
                "/dashboard/api/steering",
                json={
                    "session_id": sid,
                    "text": "retry old uncertain instruction",
                    "client_instruction_id": "cli-route-old",
                    "generation_id": "gen_previous",
                },
                headers=DASHBOARD_AUTH,
            )
            self.assertEqual(409, stale.status_code)
            payload = stale.json()
            self.assertEqual("stale_generation", payload["error"])
            self.assertEqual("unknown", payload["outcome"])
            self.assertEqual("gen_current", payload["current_generation_id"])
            self.assertEqual(0, steering.sessions()[0]["pending_instruction_count"])

            fresh = client.post(
                "/dashboard/api/steering",
                json={
                    "session_id": sid,
                    "text": "intentional resend",
                    "client_instruction_id": "cli-route-fresh",
                    "generation_id": "gen_current",
                },
                headers=DASHBOARD_AUTH,
            )
            self.assertEqual(200, fresh.status_code)
            self.assertEqual("gen_current", fresh.json()["generation_id"])


if __name__ == "__main__":
    unittest.main()
