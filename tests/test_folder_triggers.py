"""#193: a task that runs when a file arrives in a folder: settle, once per file, no loops, an hourly cap."""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import time
import unittest
from unittest.mock import patch

from mcp_server import folder_triggers, schedule_rules
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store
from tests.test_scheduled_tasks import StoreCase


class FolderScheduleTests(StoreCase):
    def test_a_folder_schedule_has_no_run_times_and_reads_as_a_sentence(self) -> None:
        schedule = schedule_rules.validate({"type": "folder", "folder": str(self.folder), "match": "pdf, Invoice*"})
        self.assertEqual({"type": "folder", "folder": str(self.folder), "match": "*.pdf, Invoice*"}, schedule)
        self.assertIsNone(schedule_rules.next_run(schedule, time.time()))
        self.assertEqual([], schedule_rules.runs_between(schedule, 0, time.time()))
        self.assertTrue(schedule_rules.describe(schedule).startswith("When a file matching *.pdf, Invoice* arrives in "))
        self.assertTrue(schedule_rules.matches(schedule, "march.PDF"))
        self.assertTrue(schedule_rules.matches(schedule, "Invoice 12.png"))
        self.assertFalse(schedule_rules.matches(schedule, "notes.txt"))
        for bad in ({"type": "folder"}, {"type": "folder", "folder": "Downloads"},
                    {"type": "folder", "folder": str(self.folder / "missing")}):
            with self.subTest(bad=bad), self.assertRaises(schedule_rules.ScheduleError):
                schedule_rules.validate(bad)
        # A folder removed later does not break reading the task back.
        gone = {"type": "folder", "folder": str(self.folder / "gone")}
        self.assertIn("gone", schedule_rules.describe(gone))

    def test_folder_tasks_are_active_and_only_they_can_name_the_file(self) -> None:
        task = store.create_task(self.ai(prompt="File {file} under Invoices.",
                                         schedule={"type": "folder", "folder": str(self.folder)}))
        self.assertEqual(("active", None), (task["state"], task["next_run_at"]))
        with self.assertRaises(store.TaskError) as refused:
            store.create_task(self.ai(prompt="Summarize {file}."))
        self.assertEqual("file_token_needs_folder", refused.exception.code)
        script = store.create_task(self.sh(args="{file} --archive", schedule={"type": "folder", "folder": str(self.folder)}))
        self.assertEqual("{file} --archive", script["args"])

    def test_the_number_of_watching_tasks_is_bounded(self) -> None:
        folder = {"type": "folder", "folder": str(self.folder)}
        with patch.object(store, "MAX_FOLDER_TASKS", 2):
            store.create_task(self.ai(schedule=folder))
            store.create_task(self.ai(schedule=folder), by="An AI app")
            with self.assertRaises(store.TaskError) as full:
                store.create_task(self.ai(schedule=folder))
            self.assertEqual("too_many_folder_tasks", full.exception.code)
            store.create_task(self.ai())  # a timed task is not counted


class WithFileTests(unittest.TestCase):
    def test_the_file_is_named_in_the_prompt_and_quoted_for_the_shell(self) -> None:
        path = "/Users/me/Downloads/Invoice March (2).pdf"
        ai = folder_triggers.with_file({"kind": "ai", "prompt": "File {file} under Invoices."}, path)
        self.assertEqual(f"File {path} under Invoices.", ai["prompt"])
        appended = folder_triggers.with_file({"kind": "ai", "prompt": "Summarize it.",
                                              "data_step": {"command": "pdftotext {file} -"}}, path)
        self.assertEqual(f"Summarize it.\n\nThe file that started this run: {path}", appended["prompt"])
        self.assertEqual("pdftotext '/Users/me/Downloads/Invoice March (2).pdf' -", appended["data_step"]["command"])
        script = folder_triggers.with_file({"kind": "script", "args": "--in {file}"}, path)
        import shlex
        self.assertEqual(["--in", path], shlex.split(script["args"]))
        self.assertEqual(path, script["trigger_file"])


class WatcherTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        (self.inbox / "old.pdf").write_text("already here")
        self.launched = []
        self.scheduler = runner.Scheduler(None, launch=lambda task_id, run_id, definition:
                                          self.launched.append((run_id, definition)))
        self.task = store.create_task(self.ai(prompt="File {file}.",
                                              schedule={"type": "folder", "folder": str(self.inbox), "match": "pdf"}))
        self.now = time.time()

    def check(self, after: float = 0.0, triggers=None) -> bool:
        self.now += after
        return (triggers or self.scheduler.folders).check(self.now)

    def drop(self, name: str, body: str = "content", mtime: float = None) -> None:
        path = self.inbox / name
        path.write_text(body)
        mtime = self.now - 60 if mtime is None else mtime  # by default a minute ago: only settling decides
        os.utime(path, (mtime, mtime))

    def finish_all(self) -> None:
        for run in store.running_runs():
            store.finish_run(run["id"], "done", now=self.now)

    def test_a_new_file_runs_the_task_once_after_it_settles(self) -> None:
        self.assertFalse(self.check())  # the file already there does not start it
        self.drop("march.pdf")
        self.drop("notes.txt")
        self.drop(".hidden.pdf")
        self.drop("big.pdf.crdownload")
        self.assertTrue(self.check(1))  # seen, settling
        self.assertEqual([], self.launched)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))
        run_id, definition = self.launched[0]
        self.assertEqual(f"File {self.inbox / 'march.pdf'}.", definition["prompt"])
        run = store.get_run(run_id)
        self.assertEqual(("file", str(self.inbox / "march.pdf")), (run["trigger"], run["trigger_file"]))
        self.finish_all()
        for _ in range(3):
            self.check(10)
        self.assertEqual(1, len(self.launched))  # never twice for the same file

    def test_a_file_still_being_written_waits(self) -> None:
        self.check()
        self.drop("scan.pdf", "a")
        self.check(1)
        for size in range(2, 5):
            self.drop("scan.pdf", "a" * size)  # still growing
            self.check(folder_triggers.SETTLE_S)
            self.assertEqual([], self.launched)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))

    def test_one_run_at_a_time_and_its_own_output_does_not_start_it_again(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.drop("b.pdf")
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))  # b waits while a's run goes on
        (self.inbox / "a-summary.pdf").write_text("written by the run")  # the run writes into the folder
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))
        self.finish_all()
        self.check(folder_triggers.QUIET_AFTER_RUN_S + 1)
        self.assertEqual(2, len(self.launched))
        self.assertTrue(self.launched[1][1]["prompt"].endswith("b.pdf."))
        self.finish_all()
        for _ in range(3):
            self.check(10)
        self.assertEqual(2, len(self.launched))  # a-summary.pdf never ran it

    def test_a_download_that_arrives_during_a_run_is_kept_and_runs_next(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))
        self.drop("downloaded.pdf")  # arrives from outside while a's run goes on
        (self.inbox / "a-out.pdf").write_text("written by the run")
        marked = {str(self.inbox / "downloaded.pdf")}
        with patch.object(folder_triggers, "arrived_from_outside", side_effect=lambda path: path in marked):
            for _ in range(3):
                self.check(folder_triggers.SETTLE_S + 0.5)
            self.assertEqual(1, len(self.launched))  # held, not dropped, while the run goes on
            self.assertEqual({"downloaded.pdf", "a-out.pdf"}, set(store.watch_state(self.task["id"])["held"]))
            self.finish_all()
            self.check(1)
            self.check(folder_triggers.SETTLE_S + 0.5)
            self.assertEqual(2, len(self.launched))
            self.assertTrue(self.launched[1][1]["prompt"].endswith("downloaded.pdf."))
            self.finish_all()
            for _ in range(3):
                self.check(folder_triggers.QUIET_AFTER_RUN_S + 1)
        self.assertEqual(2, len(self.launched))  # a-out.pdf, the run's own output, never started it

    def test_the_download_mark_is_read_from_the_file(self) -> None:
        path = self.inbox / "x.pdf"
        path.write_text("x")
        self.assertFalse(folder_triggers.arrived_from_outside(str(path)))
        import subprocess
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0083;6720f000;Safari;", str(path)], check=True)
        self.assertTrue(folder_triggers.arrived_from_outside(str(path)))
        self.assertFalse(folder_triggers.arrived_from_outside(str(self.inbox / "missing.pdf")))

    def test_a_restart_neither_repeats_a_file_nor_misses_one(self) -> None:
        self.check()
        self.drop("a.pdf")
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.finish_all()
        self.now += 60
        self.drop("while-off.pdf", mtime=self.now - 5)  # after that run ended
        fresh = folder_triggers.FolderTriggers(self.scheduler._start_for_file)  # Mac MCP started again
        self.check(1, fresh)
        self.check(folder_triggers.SETTLE_S + 0.5, fresh)
        self.assertEqual(["a.pdf.", "while-off.pdf."], [definition["prompt"][-len("while-off.pdf."):].split("/")[-1]
                                                         for _, definition in self.launched])

    def test_a_file_replaced_with_the_same_name_size_and_time_runs_again(self) -> None:
        # #56: a file is remembered by name, size and time; a new file put in its place with the same
        # three (copied keeping its time, or swapped in atomically) was taken for the old one.
        self.check()
        stamp = self.now - 60
        self.drop("scan.pdf", body="version 1", mtime=stamp)
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.finish_all()
        self.assertEqual(1, len(self.launched))
        replacement = self.inbox / "incoming.tmp"
        replacement.write_text("version 2")  # same length as "version 1"
        os.utime(replacement, (stamp, stamp))
        os.replace(replacement, self.inbox / "scan.pdf")
        self.check(folder_triggers.QUIET_AFTER_RUN_S + 1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(2, len(self.launched))
        for _ in range(3):
            self.check(10)
        self.assertEqual(2, len(self.launched), "and only once")

    def test_files_remembered_before_the_upgrade_do_not_all_run(self) -> None:
        # Saved state from before #56 holds [size, time] only; those files must not look new.
        self.check()
        self.drop("a.pdf")
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.finish_all()
        state = store.watch_state(self.task["id"])
        state["seen"] = {name: value[:2] for name, value in state["seen"].items()}
        store.save_watch_state(self.task["id"], state, self.now)
        fresh = folder_triggers.FolderTriggers(self.scheduler._start_for_file)
        self.now += 120
        self.check(1, fresh)
        self.check(folder_triggers.SETTLE_S + 0.5, fresh)
        self.assertEqual(1, len(self.launched))

    def test_more_files_than_the_hourly_cap_are_recorded_as_skipped(self) -> None:
        self.check()
        with patch.object(folder_triggers, "MAX_RUNS_PER_HOUR", 1):
            self.drop("a.pdf")
            self.drop("b.pdf")
            self.drop("c.pdf")
            self.check(1)
            self.check(folder_triggers.SETTLE_S + 0.5)
            self.finish_all()
            self.check(folder_triggers.QUIET_AFTER_RUN_S + 1)
            self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual(1, len(self.launched))
        skipped = [run for run in store.list_runs(self.task["id"]) if run["status"] == "skipped"]
        self.assertEqual(1, len(skipped))
        self.assertEqual((2, "file"), (skipped[0]["skipped_count"], skipped[0]["trigger"]))
        self.assertIn("within an hour", skipped[0]["reason"])

    def test_a_paused_or_deleted_task_stops_watching(self) -> None:
        self.check()
        store.set_paused(self.task["id"], True)
        self.drop("a.pdf")
        self.check(1)
        self.check(folder_triggers.SETTLE_S + 0.5)
        self.assertEqual([], self.launched)
        store.delete_task(self.task["id"])
        self.assertIsNone(store.watch_state(self.task["id"]))

    def test_run_now_uses_the_newest_file(self) -> None:
        self.drop("a.pdf")
        newest = self.inbox / "b.pdf"
        newest.write_text("x")
        self.scheduler.run_now(self.task["id"])
        self.assertEqual(f"File {newest}.", self.launched[0][1]["prompt"])
        empty = self.dir / "empty"
        empty.mkdir()
        other = store.create_task(self.ai(schedule={"type": "folder", "folder": str(empty)}))
        with self.assertRaises(store.TaskError) as none:
            self.scheduler.run_now(other["id"])
        self.assertEqual("no_file", none.exception.code)


class ScriptFileTests(StoreCase):
    def test_a_script_gets_the_file_as_an_argument_and_in_its_environment(self) -> None:
        script = self.script("file.sh", 'echo "arg=$1 env=$MAC_MCP_TRIGGER_FILE"\n')
        definition = store.validate_definition(self.sh(script=str(script), args="{file}",
                                                       schedule={"type": "folder", "folder": str(self.folder)}))
        path = str(self.folder / "My Invoice.pdf")
        result = runner.run_script(folder_triggers.with_file(definition, path), "run_test")
        self.assertEqual("done", result["status"])
        self.assertEqual(f"arg={path} env={path}", result["summary"])


class RealWatcherTests(StoreCase):
    def test_the_watcher_thread_sees_a_file_arrive(self) -> None:
        inbox = self.dir / "inbox"
        inbox.mkdir()
        launched = []
        scheduler = runner.Scheduler(None, launch=lambda task_id, run_id, definition: launched.append(definition))
        store.create_task(self.ai(schedule={"type": "folder", "folder": str(inbox)}))
        store.on_change(scheduler.folders.poke)
        with patch.object(folder_triggers, "SETTLE_S", 0.3), patch.object(folder_triggers, "PENDING_POLL_S", 0.1):
            scheduler.folders.begin()
            self.addCleanup(scheduler.folders.end)
            deadline = time.time() + 5
            while scheduler.folders.scans == 0 and time.time() < deadline:
                time.sleep(0.05)
            (inbox / "report.csv").write_text("a,b\n1,2\n")
            deadline = time.time() + 10
            while not launched and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(1, len(launched))
        self.assertEqual(str(inbox / "report.csv"), launched[0]["trigger_file"])


if __name__ == "__main__":
    unittest.main()
