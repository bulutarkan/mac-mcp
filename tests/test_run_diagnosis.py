"""#227: the owner can ask a read-only agent why a run failed; it reports, it never repairs."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
from unittest.mock import patch

from mcp_server import run_diagnosis as diagnosis
from mcp_server import scheduled_tasks as store
from tests import test_scheduled_tasks as base
from tests.test_scheduled_tasks import AUTH, StoreCase


class DiagnosisTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.spawned = []
        from mcp_server import scheduled_task_runner as runner
        from mcp_server import tools_agents

        def spawn(settings, provider, prompt, **kwargs):
            self.spawned.append({"provider": provider, "prompt": prompt, **kwargs})
            return {"agent_id": "agent_diag"}

        self.agent_result = {"status": "done", "output": "Likely cause: the token expired. sk-ant-api03-" + "x" * 40,
                             "summary": "The Facebook token expired"}
        for item in (patch.object(tools_agents, "spawn_agent", spawn),
                     patch.object(runner, "wait_for_agent", lambda *a, **k: dict(self.agent_result)),
                     patch.object(diagnosis, "_background", lambda work: work())):
            item.start()
            self.addCleanup(item.stop)
        self.task = store.create_task(self.ai(provider="claude", cwd=str(self.folder)))

    def failed_run(self, **finish):
        run_id = store.start_run(self.task["id"], trigger="schedule")
        store.finish_run(run_id, finish.pop("status", "failed"), reason=finish.pop("reason", "Exited with code 1"),
                         summary="Could not log in", output="Traceback: token expired", **finish)
        return run_id

    def test_a_failed_run_gets_a_read_only_diagnosis_and_stays_as_it_was(self) -> None:
        run_id = self.failed_run()
        before = store.get_run(run_id)
        started = diagnosis.start(run_id, settings=None)
        self.assertEqual("done", started["status"])
        spawn = self.spawned[0]
        self.assertEqual(("read_only", "claude"), (spawn["access_mode"], spawn["provider"]))
        self.assertIn("Exited with code 1", spawn["prompt"])
        self.assertIn("Do not change anything", spawn["prompt"])
        latest = store.diagnosis_for(run_id)
        self.assertEqual("The Facebook token expired", latest["summary"])
        self.assertNotIn("x" * 40, latest["report"])  # known secret shapes are masked like run contents
        self.assertEqual(before, store.get_run(run_id))

    def test_only_runs_that_went_wrong_and_one_at_a_time(self) -> None:
        done = self.failed_run(status="done", reason=None)
        with self.assertRaises(store.TaskError) as refused:
            diagnosis.start(done, settings=None)
        self.assertEqual("nothing_to_diagnose", refused.exception.code)
        run_id = self.failed_run()
        with patch.object(diagnosis, "_background", lambda work: None):  # still running
            first = diagnosis.start(run_id, settings=None)
            second = diagnosis.start(run_id, settings=None)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(self.spawned))

    def test_without_the_run_contents_there_is_no_agent_only_insufficient_evidence(self) -> None:
        run_id = self.failed_run(detail={"private_mode": True})
        result = diagnosis.start(run_id, settings=None)
        self.assertEqual(("insufficient_evidence", []), (result["status"], self.spawned))
        self.assertIn("private mode", result["report"])

    def test_the_dashboard_starts_and_reads_it(self) -> None:
        run_id = self.failed_run()
        client = base.DashboardApiTests.client(self)
        started = client.post("/dashboard/api/scheduled-tasks/diagnose", headers=AUTH, json={"run_id": run_id}).json()
        self.assertTrue(started["ok"])
        read = client.get(f"/dashboard/api/scheduled-tasks/diagnosis?run_id={run_id}", headers=AUTH).json()
        self.assertEqual("done", read["diagnosis"]["status"])
        self.assertEqual(401, client.post("/dashboard/api/scheduled-tasks/diagnose", json={"run_id": run_id}).status_code)

    def test_agents_have_no_way_to_start_one(self) -> None:
        from tests.test_core_catalog_descriptions import _build_mcp
        mcp = _build_mcp()
        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        self.assertFalse([name for name in names if "diagnos" in name])
        self.assertNotIn("diagnos", json.dumps([t.description for t in mcp._tool_manager.list_tools()
                                                if t.name == "scheduled_tasks"]))
