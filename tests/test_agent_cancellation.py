from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp_server import tools_agents

STUBBORN = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.1)\n"
POLITE = "import time\nwhile True: time.sleep(0.1)\n"


class AgentCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mac-mcp-cancel-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = patch.object(tools_agents, "AGENTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._kill_all)

    def _kill_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def spawn(self, code: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.procs.append(proc)
        time.sleep(0.2)
        return proc

    def agent(self, agent_id: str, *, provider: subprocess.Popen | None, worker: subprocess.Popen | None,
              provider_offset: float = 0.0) -> None:
        now = time.time()
        meta = {
            "agent_id": agent_id, "status": "running", "provider": "codex", "title": "t",
            "started_at": now, "provider_pid": provider.pid if provider else None,
            "provider_started_at": now + provider_offset if provider else None,
            "worker_pid": worker.pid if worker else None, "worker_started_at": now if worker else None,
        }
        (self.root / agent_id).mkdir()
        (self.root / agent_id / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def meta(self, agent_id: str) -> dict:
        return json.loads((self.root / agent_id / "meta.json").read_text(encoding="utf-8"))

    def cancel(self, agent_id: str, **kwargs) -> dict:
        with patch.object(tools_agents, "_release_agent_admission") as release, \
             patch.object(tools_agents, "_refresh_agent_worktree"), \
             patch.object(tools_agents, "_wake_global_admission_queue"):
            result = tools_agents.agent_action(MagicMock(), action="cancel", agent_id=agent_id, **kwargs)
        result["_released"] = release.call_count
        return result

    def test_user_cancel_stops_both_processes_before_confirming(self) -> None:
        provider, worker = self.spawn(STUBBORN), self.spawn(POLITE)
        self.agent("agent_a", provider=provider, worker=worker)
        result = self.cancel("agent_a", requested_by="user")
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("confirmed", result["cancellation"]["state"])
        self.assertEqual("user", result["cancellation"]["requested_by"])
        self.assertIn("the user", result["cancellation"]["message"])
        self.assertIsNotNone(provider.poll(), "a provider ignoring TERM is escalated to KILL")
        self.assertIsNotNone(worker.poll())
        self.assertEqual(1, result["_released"], "capacity is released once, after the processes stopped")

    def test_orchestrator_cancel_is_labelled(self) -> None:
        self.agent("agent_b", provider=self.spawn(POLITE), worker=self.spawn(POLITE))
        result = self.cancel("agent_b")
        self.assertEqual("orchestrator", result["cancellation"]["requested_by"])
        self.assertIn("orchestrating agent", self.meta("agent_b")["note"])

    def test_unconfirmed_cancellation_keeps_capacity_and_is_reconciled_later(self) -> None:
        provider = self.spawn(STUBBORN)
        self.agent("agent_c", provider=provider, worker=self.spawn(POLITE))
        with patch.object(tools_agents, "_signal_agent_processes"):  # signals "lost": nothing stops
            result = self.cancel("agent_c", requested_by="user")
        self.assertEqual("unconfirmed", result["cancellation"]["state"])
        self.assertIn("provider", result["cancellation"]["still_running"])
        self.assertEqual(0, result["_released"])
        self.assertIn("not been confirmed", result["message"])
        # A later read (or a repeated cancel) finishes the job.
        meta = self.meta("agent_c")
        meta["cancellation"]["requested_at"] = time.time() - 10
        (self.root / "agent_c" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(tools_agents, "_release_agent_admission"), patch.object(tools_agents, "_refresh_agent_worktree"), \
             patch.object(tools_agents, "_wake_global_admission_queue"):
            tools_agents._normalize("agent_c", self.meta("agent_c"))
            self.assertIsNotNone(provider.wait(timeout=5))
            again = tools_agents._normalize("agent_c", self.meta("agent_c"))
        self.assertEqual("confirmed", again["cancellation"]["state"])

    def test_a_crash_after_the_intent_is_recovered_on_read(self) -> None:
        provider = self.spawn(POLITE)
        self.agent("agent_d", provider=provider, worker=self.spawn(POLITE))
        meta = self.meta("agent_d")
        meta.update(status="cancelled", cancellation={"state": "stopping", "requested_by": "user",
                                                      "requested_at": time.time() - 10})
        (self.root / "agent_d" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with patch.object(tools_agents, "_release_agent_admission") as release, \
             patch.object(tools_agents, "_refresh_agent_worktree"), patch.object(tools_agents, "_wake_global_admission_queue"):
            tools_agents._normalize("agent_d", self.meta("agent_d"))
            provider.wait(timeout=5)
            settled = tools_agents._normalize("agent_d", self.meta("agent_d"))
        self.assertEqual("confirmed", settled["cancellation"]["state"])
        release.assert_called_once()

    def test_a_reused_pid_is_never_signalled(self) -> None:
        stranger = self.spawn(POLITE)
        # The record says our provider started an hour before this process existed.
        self.agent("agent_e", provider=stranger, worker=self.spawn(POLITE), provider_offset=-3600)
        result = self.cancel("agent_e", requested_by="user")
        self.assertEqual("confirmed", result["cancellation"]["state"])
        self.assertIsNone(stranger.poll(), "an unrelated process with a reused PID keeps running")

    def test_repeated_cancel_is_idempotent(self) -> None:
        self.agent("agent_f", provider=self.spawn(POLITE), worker=self.spawn(POLITE))
        first = self.cancel("agent_f", requested_by="user")
        second = self.cancel("agent_f", requested_by="user")
        self.assertEqual("confirmed", first["cancellation"]["state"])
        self.assertEqual("Agent is already finished.", second["message"])
        self.assertEqual("user", second["cancellation"]["requested_by"])


class DashboardCancelRouteTests(unittest.TestCase):
    TOKEN = "cancel-route-token"

    def client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        td = tempfile.mkdtemp(prefix="mac-mcp-cancel-route-")
        self.addCleanup(shutil.rmtree, td, True)
        telemetry = TelemetryManager(db_path=Path(td) / "telemetry.sqlite3")
        app = Starlette(routes=create_dashboard_routes(telemetry, load_settings(), self.TOKEN))
        return TestClient(app)

    def test_the_owner_cancels_as_the_user(self) -> None:
        auth = {"authorization": f"Bearer {self.TOKEN}"}
        reply = {"status": "cancelled", "cancellation": {"state": "confirmed", "requested_by": "user", "message": "m"}}
        with patch("mcp_server.dashboard_routes.agent_action", return_value=reply) as action:
            client = self.client()
            self.assertEqual(401, client.post("/dashboard/api/agents/cancel", json={"agent_id": "agt_x"}).status_code)
            self.assertEqual(400, client.post("/dashboard/api/agents/cancel", json={"agent_id": "../x"}, headers=auth).status_code)
            response = client.post("/dashboard/api/agents/cancel", json={"agent_id": "agt_0123abcd"}, headers=auth)
        self.assertEqual(200, response.status_code)
        self.assertEqual("confirmed", response.json()["cancellation"]["state"])
        action.assert_called_once()
        self.assertEqual("user", action.call_args.kwargs["requested_by"])
        self.assertEqual("agt_0123abcd", action.call_args.kwargs["agent_id"])


if __name__ == "__main__":
    unittest.main()
