"""#179: secrets in a data step's output are masked before the agent sees them, and an approved
script whose contents change does not run until the owner reviews the change."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os

from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests import test_scheduled_tasks as base
from tests.test_scheduled_tasks import AUTH, StoreCase


class AgentCase(StoreCase):
    fake_agents = base.AiRunTests.fake_agents


class DataMaskingTests(AgentCase):
    def test_the_agent_never_sees_a_secret_the_data_step_printed(self) -> None:
        calls = self.fake_agents({"status": "completed", "provider": "codex",
                                  "result_envelope": {"outcome": "success", "summary": "ok"}})
        script = self.script("data.py", "print('leads: 214')\nprint('TOKEN=sk-ant-api03-' + 'q' * 40)\n")
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder), data_step={
            "script": str(script), "interpreter": os.sys.executable}))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        result = runner.execute(task["id"], run_id, definition, settings=None)
        prompt = calls["spawn"][0]["prompt"]
        self.assertIn("leads: 214", prompt)
        self.assertNotIn("q" * 40, prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertIn("replaced with [REDACTED]", prompt)  # the agent is told, so it doesn't report a broken value
        self.assertTrue(result["detail"]["data"]["masked"])
        self.assertIn("secrets masked", result["detail"]["data"]["title"])

    def test_clean_data_is_handed_over_unchanged(self) -> None:
        calls = self.fake_agents({"status": "completed", "provider": "codex"})
        script = self.script("data.py", "print('spend: 120.50 USD')\n")
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder), data_step={
            "script": str(script), "interpreter": os.sys.executable}))
        task = store.create_task(definition)
        result = runner.execute(task["id"], store.start_run(task["id"], trigger="manual"), definition, settings=None)
        self.assertIn("spend: 120.50 USD", calls["spawn"][0]["prompt"])
        self.assertNotIn("REDACTED", calls["spawn"][0]["prompt"])
        self.assertNotIn("masked", result["detail"]["data"])


class ScriptPinTests(StoreCase):
    def suggested(self, body: str = "echo approved\n"):
        script = self.script("job.sh", body)
        task = store.create_task(self.sh(cwd=str(self.folder), script=str(script)), by="Claude")
        return task, script

    def run_task(self, task_id):
        definition = store.definition_of(task_id)
        return runner.execute(task_id, store.start_run(task_id, trigger="manual"), definition, settings=None)

    def test_the_review_shows_the_script_and_approval_binds_its_contents(self) -> None:
        task, script = self.suggested()
        files = store.get_task(task["id"])["proposal"]["script_files"]
        self.assertEqual("echo approved\n", files[0]["text"])
        store.approve(task["id"], seen={str(script): files[0]["sha256"]})
        self.assertEqual("done", self.run_task(task["id"])["status"])
        self.assertEqual([], store.script_drift(task["id"]))

    def test_a_changed_script_does_not_run_and_waits_for_review_with_the_change(self) -> None:
        task, script = self.suggested()
        store.approve(task["id"])
        marker = self.folder / "ran"
        script.write_text(f"echo approved\ntouch {marker}\n")
        result = self.run_task(task["id"])
        self.assertEqual("failed", result["status"])
        self.assertIn("job.sh changed since you approved", result["reason"])
        self.assertFalse(marker.exists())
        shown = store.get_task(task["id"])
        self.assertTrue(shown["proposal"]["script_changed"])
        change = shown["proposal"]["script_files"][0]
        self.assertTrue(change["changed"])
        self.assertIn(f"+touch {marker}", change["diff"])
        # Approving the reviewed version lets it run again.
        store.approve(task["id"], seen={str(script): change["sha256"]})
        self.assertEqual("done", self.run_task(task["id"])["status"])
        self.assertTrue(marker.exists())

    def test_a_change_during_review_is_refused(self) -> None:
        task, script = self.suggested()
        shown = store.get_task(task["id"])["proposal"]["script_files"][0]["sha256"]
        script.write_text("echo swapped\n")
        with self.assertRaises(store.TaskError) as refused:
            store.approve(task["id"], seen={str(script): shown})
        self.assertEqual("script_changed_during_review", refused.exception.code)

    def test_the_owners_own_tasks_and_files_are_not_bound(self) -> None:
        script = self.script("mine.sh", "echo one\n")
        task = store.create_task(self.sh(cwd=str(self.folder), script=str(script)))
        script.write_text("echo two\n")
        self.assertEqual("done", self.run_task(task["id"])["status"])
        # An agent's task the owner points at another file: that file is the owner's choice.
        suggested, _ = self.suggested()
        store.approve(suggested["id"])
        other = self.script("other.sh", "echo other\n")
        store.update_task(suggested["id"], {"script": str(other)})
        other.write_text("echo edited\n")
        self.assertEqual([], store.script_drift(suggested["id"]))

    def test_an_owner_edit_keeps_the_binding_of_the_same_file(self) -> None:
        task, script = self.suggested()
        store.approve(task["id"])
        store.update_task(task["id"], {"name": "Renamed"})
        script.write_text("echo changed\n")
        self.assertEqual([str(script)], store.script_drift(task["id"]))

    def test_the_data_step_script_is_bound_too(self) -> None:
        data = self.script("data.sh", "echo 1\n")
        task = store.create_task(self.ai(cwd=str(self.folder), data_step={"script": str(data)}), by="Codex")
        self.assertTrue(store.get_task(task["id"])["proposal"]["script_files"][0]["data_step"])
        store.approve(task["id"])
        data.write_text("echo 2\n")
        self.assertEqual([str(data)], store.script_drift(task["id"]))

    def test_the_dashboard_passes_what_the_review_showed(self) -> None:
        task, script = self.suggested()
        client = base.DashboardApiTests.client(self)
        shown = store.get_task(task["id"])["proposal"]["script_files"][0]["sha256"]
        script.write_text("echo swapped\n")
        refused = client.post("/dashboard/api/scheduled-tasks/action", headers=AUTH,
                              json={"id": task["id"], "action": "approve", "seen": {str(script): shown}})
        self.assertEqual(409, refused.status_code)
        fresh = store.get_task(task["id"])["proposal"]["script_files"][0]["sha256"]
        approved = client.post("/dashboard/api/scheduled-tasks/action", headers=AUTH,
                               json={"id": task["id"], "action": "approve", "seen": {str(script): fresh}})
        self.assertEqual(200, approved.status_code, approved.text)
