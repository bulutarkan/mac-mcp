"""#225: a run that never started is noticed by something other than the scheduler that missed it."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import time
from unittest.mock import patch

from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests import test_scheduled_tasks as base
from tests.test_scheduled_tasks import AUTH, StoreCase


class OverdueTests(StoreCase):
    def make(self, **extra):
        task = store.create_task(self.ai(schedule={"type": "daily", "time": "09:00"}, **extra))
        return task

    def test_a_due_run_the_scheduler_never_started_is_overdue(self) -> None:
        task = self.make()
        due = store.get_task(task["id"])["next_run_at"]
        self.assertEqual([], store.overdue(due + 60))  # within the tolerance: the scheduler may be about to start it
        late = store.overdue(due + store.OVERDUE_TOLERANCE_S + 1)
        self.assertEqual([(task["id"], due)], [(item["id"], item["expected_at"]) for item in late])
        self.assertEqual("Weekly ad report", late[0]["name"])

    def test_paused_running_folder_and_handled_tasks_are_not_overdue(self) -> None:
        paused = self.make()
        store.set_paused(paused["id"], True)
        running = self.make()
        due = store.get_task(running["id"])["next_run_at"]
        store.start_run(running["id"], trigger="schedule")
        store.create_task(self.ai(schedule={"type": "folder", "folder": str(self.folder)}))
        handled = self.make()
        store.advance(handled["id"], due + 1)  # the scheduler ran or recorded it and moved on
        self.assertEqual([], [item["id"] for item in store.overdue(due + store.OVERDUE_TOLERANCE_S + 1)
                              if item["id"] in {paused["id"], running["id"], handled["id"]}])

    def test_the_scheduler_reports_whether_its_loop_is_alive(self) -> None:
        scheduler = runner.Scheduler(None)
        self.assertEqual({"running": False, "last_wake_at": None}, scheduler.liveness())
        scheduler.begin()
        self.addCleanup(scheduler.end)
        deadline = time.time() + 5
        while scheduler.liveness()["last_wake_at"] is None and time.time() < deadline:
            time.sleep(0.05)
        alive = scheduler.liveness()
        self.assertTrue(alive["running"])
        self.assertIsNotNone(alive["last_wake_at"])


class LivenessApiTests(StoreCase):
    def test_the_app_reads_overdue_runs_and_what_is_due_next(self) -> None:
        task = store.create_task(self.ai(schedule={"type": "daily", "time": "09:00"}))
        due = store.get_task(task["id"])["next_run_at"]
        client = base.DashboardApiTests.client(self)
        with patch.object(store.time, "time", return_value=due + store.OVERDUE_TOLERANCE_S + 5):
            body = client.get("/dashboard/api/scheduled-tasks/liveness", headers=AUTH).json()
        self.assertEqual([task["id"]], [item["id"] for item in body["overdue"]])
        self.assertIn("scheduler", body)
        self.assertEqual(store.OVERDUE_TOLERANCE_S, body["tolerance_s"])
        with patch.object(store.time, "time", return_value=due - 600):
            ahead = client.get("/dashboard/api/scheduled-tasks/liveness", headers=AUTH).json()
        self.assertEqual([(task["id"], due)], [(item["id"], item["at"]) for item in ahead["due_soon"]])
        self.assertEqual(401, client.get("/dashboard/api/scheduled-tasks/liveness").status_code)
