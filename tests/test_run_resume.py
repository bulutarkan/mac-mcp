"""#226: a failed AI run goes on from its agent step with the data it already has; checks can be redone."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
from unittest.mock import patch

from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests import test_scheduled_tasks as base
from tests.test_scheduled_tasks import AUTH, StoreCase


class AgentCase(StoreCase):
    fake_agents = base.AiRunTests.fake_agents  # borrowed, so AiRunTests' own tests aren't collected twice


class ResumeTests(AgentCase):
    def setUp(self) -> None:
        super().setUp()
        self.marker = self.folder / "data-runs"
        self.launched = []
        self.scheduler = runner.Scheduler(None, launch=lambda task_id, run_id, definition: self.launched.append(
            (task_id, run_id, definition)))

    def failed_run(self, body: str = None):
        """A run whose data step printed its data and whose agent then failed."""
        body = body or f"open({str(self.marker)!r}, 'a').write('x')\nprint('leads: 214')\n"
        script = self.script("data.py", body)
        self.fake_agents({"status": "failed", "error": "Facebook token expired", "provider": "codex"})
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder), data_step={
            "script": str(script), "interpreter": os.sys.executable}))
        self.task = store.create_task(definition)
        run_id = store.start_run(self.task["id"], trigger="manual")
        result = runner.execute(self.task["id"], run_id, definition, settings=None)
        self.assertEqual("failed", result["status"])
        return run_id

    def resume_and_run(self, run_id):
        started = self.scheduler.resume(run_id)
        task_id, new_run, definition = self.launched[-1]
        calls = self.fake_agents({"status": "completed", "provider": "codex",
                                  "result_envelope": {"outcome": "success", "summary": "Leads up 12%."},
                                  "result_full": {"text": "Report"}})
        result = runner.execute(task_id, new_run, definition, settings=None)
        return started, result, calls

    def test_the_agent_step_runs_again_with_the_kept_data_and_the_data_step_does_not(self) -> None:
        run_id = self.failed_run()
        self.assertTrue(runner.resume_plan(store.get_run(run_id), store.definition_of(self.task["id"]))["ok"])
        before = store.get_run(run_id)
        started, result, calls = self.resume_and_run(run_id)
        self.assertEqual("done", result["status"])
        self.assertEqual("x", self.marker.read_text())  # the data step ran once, in the first run only
        self.assertIn("<data>\nleads: 214", calls["spawn"][0]["prompt"])
        self.assertEqual(("resume", run_id), (result["trigger"], result["resumed_from"]))
        first = result["detail"]["steps"][0]
        self.assertEqual(("data", "done", run_id), (first["kind"], first["status"], first["reused_from"]))
        self.assertIn("earlier run", first["title"])
        self.assertEqual(before, store.get_run(run_id))  # the failed run stays as it was

    def test_a_run_is_resumed_once(self) -> None:
        run_id = self.failed_run()
        self.resume_and_run(run_id)
        with self.assertRaises(store.TaskError) as again:
            self.scheduler.resume(run_id)
        self.assertIn("already resumed", str(again.exception))
        # Two resumes racing: the store refuses the second in the same transaction.
        with self.assertRaises(store.TaskError):
            store.start_run(self.task["id"], trigger="resume", resumed_from=run_id)

    def test_anything_uncertain_refuses(self) -> None:
        run_id = self.failed_run()
        store.update_task(self.task["id"], {"prompt": "Something else."})
        plan = runner.resume_plan(store.get_run(run_id), store.definition_of(self.task["id"]))
        self.assertEqual((False, True), (plan["ok"], "changed" in plan["reason"]))
        with self.assertRaises(store.TaskError):
            self.scheduler.resume(run_id)
        self.assertEqual([], self.launched)

        # Data that was cut when kept is not reused.
        long_run = self.failed_run(f"print('y' * {store.OUTPUT_LIMIT + 50})\n")
        plan = runner.resume_plan(store.get_run(long_run), store.definition_of(self.task["id"]))
        self.assertEqual((False, True), (plan["ok"], "part of the data" in plan["reason"]))

        # Secrets masked in the kept copy make it differ from what the agent had.
        secret_run = self.failed_run("print('key sk-ant-api03-' + 'z' * 40)\n")
        self.assertFalse(runner.resume_plan(store.get_run(secret_run), store.definition_of(self.task["id"]))["ok"])

    def test_only_ai_runs_with_a_finished_data_step_and_kept_contents(self) -> None:
        run_id = self.failed_run("import sys\nsys.exit(2)\n")  # the data step itself failed
        plan = runner.resume_plan(store.get_run(run_id), store.definition_of(self.task["id"]))
        self.assertIn("didn't finish", plan["reason"])
        self.privacy(private_mode=True)
        private = self.failed_run()
        plan = runner.resume_plan(store.get_run(private), store.definition_of(self.task["id"]))
        self.assertFalse(plan["ok"])
        script_task = store.create_task(self.sh(cwd=str(self.folder)))
        script_run = store.start_run(script_task["id"], trigger="manual")
        store.finish_run(script_run, "failed", reason="Exited with code 1")
        plan = runner.resume_plan(store.get_run(script_run), store.definition_of(script_task["id"]))
        self.assertIn("data step", plan["reason"])

    def test_the_dashboard_offers_and_starts_it_with_the_owner_token(self) -> None:
        run_id = self.failed_run()
        client = base.DashboardApiTests.client(self)
        read = client.get(f"/dashboard/api/scheduled-tasks/run?run_id={run_id}", headers=AUTH).json()
        self.assertTrue(read["resume"]["ok"])
        self.assertEqual(401, client.post("/dashboard/api/scheduled-tasks/resume", json={"run_id": run_id}).status_code)
        with patch.object(runner.Scheduler, "_launch_thread", lambda *args: None):
            started = client.post("/dashboard/api/scheduled-tasks/resume", headers=AUTH, json={"run_id": run_id})
        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual(run_id, store.get_run(started.json()["result"]["run_id"])["resumed_from"])


class CheckAgainTests(AgentCase):
    def test_checks_are_looked_at_again_and_the_first_result_is_kept(self) -> None:
        target = self.folder / "out.csv"
        task = store.create_task(self.sh(cwd=str(self.folder), verify=[{"path": str(target), "written_this_run": False}]))
        run_id = store.start_run(task["id"], trigger="manual")
        definition = store.definition_of(task["id"])
        store.finish_run(run_id, "done", verification=runner._verification(run_id, definition, "done", False))
        self.assertEqual("failed", store.get_run(run_id)["verification"]["state"])
        target.write_text("a,b\n")
        with patch.object(runner, "_background", lambda work: work()):
            runner.check_again(run_id)
        verification = store.get_run(run_id)["verification"]
        self.assertEqual(("verified", "failed"), (verification["state"], verification["first_state"]))
        self.assertIn("rechecked_at", verification)
        self.assertFalse(runner.rechecking(run_id))

    def test_only_finished_runs_with_checks(self) -> None:
        task = store.create_task(self.sh(cwd=str(self.folder)))
        run_id = store.start_run(task["id"], trigger="manual")
        store.finish_run(run_id, "failed", reason="Exited with code 1")
        with self.assertRaises(store.TaskError):
            runner.check_again(run_id)

    def test_agents_have_no_way_to_resume_or_recheck(self) -> None:
        import json
        from tests.test_core_catalog_descriptions import _build_mcp
        tools = _build_mcp()._tool_manager.list_tools()
        described = json.dumps([tool.description for tool in tools if tool.name == "scheduled_tasks"]).lower()
        self.assertNotIn("resume", described)
        self.assertNotIn("check again", described)
