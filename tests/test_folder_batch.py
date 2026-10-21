"""#58: a folder task can handle the files that arrive together in one run.

Every matching file used to start its own run, and past MAX_RUNS_PER_HOUR the rest were skipped: a
stack of invoices meant many runs, or files never handled. With batch on, once every new file is
written and none came for BATCH_QUIET_S, one run gets them all; the run records each file. An AI task
reads them as a list; a script gets the path of a JSON list, never the names in its command line.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import json
import os
import shlex
import time
from unittest.mock import patch

from mcp_server import folder_triggers, schedule_rules
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests.test_scheduled_tasks import StoreCase


class BatchCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        (self.inbox / "old.pdf").write_text("already here")
        self.launched = []
        self.scheduler = runner.Scheduler(None, launch=lambda task_id, run_id, definition:
                                          self.launched.append((run_id, definition)))
        self.task = store.create_task(self.ai(prompt="File these invoices.", schedule={
            "type": "folder", "folder": str(self.inbox), "match": "pdf", "batch": True}))
        self.now = time.time()

    def check(self, after: float = 0.0, triggers=None) -> bool:
        self.now += after
        return (triggers or self.scheduler.folders).check(self.now)

    def drop(self, name: str, body: str = "content") -> None:
        path = self.inbox / name
        path.write_text(body)
        os.utime(path, (self.now - 60, self.now - 60))

    def settle(self, triggers=None) -> None:
        for _ in range(4):
            self.check(folder_triggers.SETTLE_S, triggers)
        self.check(folder_triggers.BATCH_QUIET_S, triggers)

    def finish_all(self) -> None:
        for run in store.running_runs():
            store.finish_run(run["id"], "done", now=self.now)


class BatchTests(BatchCase):
    def test_ten_files_make_one_run_that_names_each(self) -> None:
        self.check()
        names = [f"invoice-{index:02d}.pdf" for index in range(10)]
        for name in names:
            self.drop(name)
            self.check(0.5)
        self.settle()
        self.assertEqual(1, len(self.launched))
        run_id, definition = self.launched[0]
        for name in names:
            self.assertIn(str(self.inbox / name), definition["prompt"])
        self.assertIn("The 10 files that started this run:", definition["prompt"])
        run = store.get_run(run_id)
        self.assertEqual([str(self.inbox / name) for name in names], run["trigger_files"])
        self.finish_all()
        for _ in range(3):
            self.check(30)
        self.assertEqual(1, len(self.launched))  # never again for the same files

    def test_a_file_still_copying_holds_the_whole_group(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.drop("b.pdf", "b")
        self.check(1)
        for size in range(2, 6):
            self.drop("b.pdf", "b" * size)  # still growing
            self.check(folder_triggers.SETTLE_S)
        self.assertEqual([], self.launched)
        self.settle()
        self.assertEqual(1, len(self.launched))
        self.assertEqual(2, len(self.launched[0][1]["trigger_files"]))

    def test_files_arriving_during_a_run_make_the_next_batch(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.settle()
        self.assertEqual(1, len(self.launched))
        with patch.object(folder_triggers, "arrived_from_outside", return_value=True):
            self.drop("b.pdf")
            self.drop("c.pdf")
            self.settle()
            self.assertEqual(1, len(self.launched))  # one run at a time
            self.finish_all()
            self.settle()
            self.settle()
        self.assertEqual(2, len(self.launched))
        self.assertEqual({str(self.inbox / "b.pdf"), str(self.inbox / "c.pdf")}, set(self.launched[1][1]["trigger_files"]))

    def test_a_restart_neither_loses_nor_repeats_files(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.check(1)
        fresh = folder_triggers.FolderTriggers(self.scheduler._start_for_file,
                                               start_batch=self.scheduler._start_for_files)  # Mac MCP started again
        self.settle(fresh)
        self.assertEqual(1, len(self.launched))
        self.finish_all()
        again = folder_triggers.FolderTriggers(self.scheduler._start_for_file, start_batch=self.scheduler._start_for_files)
        self.settle(again)
        self.assertEqual(1, len(self.launched))

    def test_a_batch_is_one_run_against_the_hourly_cap_and_large_groups_are_split(self) -> None:
        self.check()
        with patch.object(folder_triggers, "MAX_BATCH_FILES", 3):
            for index in range(5):
                self.drop(f"f{index}.pdf")
            self.settle()
            self.assertEqual(3, len(self.launched[0][1]["trigger_files"]))
            self.finish_all()
            with patch.object(folder_triggers, "arrived_from_outside", return_value=True):
                self.settle()
                self.settle()
        self.assertEqual(2, len(self.launched))
        self.assertEqual(2, len(self.launched[1][1]["trigger_files"]))
        self.assertEqual(2, store.file_runs_since(self.task["id"], self.now - 3600))


class BatchOffTests(BatchCase):
    def test_without_batch_each_file_has_its_own_run(self) -> None:
        store.update_task(self.task["id"], {"schedule": {"type": "folder", "folder": str(self.inbox), "match": "pdf"}})
        self.check()
        self.drop("a.pdf")
        self.drop("b.pdf")
        self.settle()
        self.finish_all()
        self.settle()
        self.assertEqual(2, len(self.launched))
        self.assertTrue(all("trigger_files" not in definition for _run, definition in self.launched))


class BatchScheduleTests(StoreCase):
    def test_batch_is_kept_and_read_as_a_sentence(self) -> None:
        schedule = schedule_rules.validate({"type": "folder", "folder": str(self.folder), "batch": True})
        self.assertTrue(schedule["batch"])
        self.assertIn("together in one run", schedule_rules.describe(schedule))
        self.assertNotIn("batch", schedule_rules.validate({"type": "folder", "folder": str(self.folder), "batch": "yes"}))

    def test_a_script_gets_a_json_list_never_the_names_in_its_command(self) -> None:
        script = self.script("files.sh", 'cat "$1"; echo; echo "env=$MAC_MCP_TRIGGER_FILES"\n')
        definition = store.validate_definition(self.sh(script=str(script), args="{file}",
                                                       schedule={"type": "folder", "folder": str(self.folder), "batch": True}))
        paths = [str(self.folder / "A $(rm -rf ~).pdf"), str(self.folder / "B; echo hi.pdf")]
        batch = folder_triggers.with_files(definition, paths)
        self.assertEqual("{file}", batch["args"])  # nothing pasted in before the run
        listed = runner._with_file_list(batch, "run_batch")
        self.assertEqual(1, len(shlex.split(listed["args"])))
        result = runner.run_script(listed, "run_batch")
        self.assertEqual("done", result["status"], result)
        printed = result["output"].splitlines()
        self.assertEqual(paths, json.loads(printed[0]))
        self.assertEqual(f"env={listed['trigger_list']}", printed[-1])
        os.unlink(listed["trigger_list"])


class BatchUITests(StoreCase):
    def test_the_editor_offers_batch_and_the_report_lists_the_files(self) -> None:
        from pathlib import Path

        sources = Path(__file__).resolve().parents[1] / "menu_app" / "Sources"
        pane = (sources / "ScheduledTasksPane.swift").read_text(encoding="utf-8")
        self.assertIn('Toggle("Handle files that arrive together in one run", isOn: $model.watchTogether)', pane)
        self.assertIn('if watchTogether { schedule["batch"] = true }', pane)
        self.assertIn('watchTogether = schedule["batch"] as? Bool ?? false', pane)
        report = (sources / "RunReportView.swift").read_text(encoding="utf-8")
        self.assertIn("ForEach(shown.triggerFiles, id: \\.self)", report)
        model = (sources / "AppModels+ScheduledTasks.swift").read_text(encoding="utf-8")
        self.assertIn('raw["trigger_files"] as? [String]', model)


class RealBatchWatcherTests(StoreCase):
    def test_the_watcher_thread_hands_a_dropped_stack_to_one_run(self) -> None:
        inbox = self.dir / "stack"
        inbox.mkdir()
        launched = []
        scheduler = runner.Scheduler(None, launch=lambda task_id, run_id, definition: launched.append(definition))
        store.create_task(self.ai(schedule={"type": "folder", "folder": str(inbox), "batch": True}))
        store.on_change(scheduler.folders.poke)
        with patch.object(folder_triggers, "SETTLE_S", 0.3), patch.object(folder_triggers, "PENDING_POLL_S", 0.1), \
                patch.object(folder_triggers, "BATCH_QUIET_S", 1.0):
            scheduler.folders.begin()
            self.addCleanup(scheduler.folders.end)
            deadline = time.time() + 5
            while scheduler.folders.scans == 0 and time.time() < deadline:
                time.sleep(0.05)
            for index in range(10):
                (inbox / f"scan-{index}.pdf").write_bytes(b"%PDF" + bytes(index))
                time.sleep(0.1)  # a scanner dropping pages one by one
            deadline = time.time() + 15
            while not launched and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(2.0)  # nothing more may start
        self.assertEqual(1, len(launched))
        self.assertEqual(10, len(launched[0]["trigger_files"]))
