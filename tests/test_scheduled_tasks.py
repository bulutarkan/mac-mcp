from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import asyncio
import json
import os
import threading
import shutil
import stat
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mcp_server import privacy_policy, schedule_rules
from mcp_server import scheduled_task_runner as runner
from mcp_server import scheduled_tasks as store

TOKEN = "scheduled-tasks-token-0123456789-abcdefghijklmnopqrstuvwxyz"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def local(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).isoformat(timespec="minutes")


@contextmanager
def time_zone(name: str):
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


class ScheduleTests(unittest.TestCase):
    def test_daily_weekly_monthly_and_hours(self) -> None:
        daily = {"type": "daily", "time": "9:00"}
        self.assertEqual("2026-10-13T09:00", local(schedule_rules.next_run(daily, ts("2026-10-13T08:59"))))
        self.assertEqual("2026-10-14T09:00", local(schedule_rules.next_run(daily, ts("2026-10-13T09:00"))))
        weekly = {"type": "weekly", "time": "18:30", "weekdays": [4, 0]}  # Friday and Monday
        self.assertEqual("2026-10-16T18:30", local(schedule_rules.next_run(weekly, ts("2026-10-13T12:00"))))
        self.assertEqual("2026-10-19T18:30", local(schedule_rules.next_run(weekly, ts("2026-10-16T18:30"))))
        monthly = {"type": "monthly", "time": "07:00", "day": 31}
        self.assertEqual("2027-02-28T07:00", local(schedule_rules.next_run(monthly, ts("2027-02-01T00:00"))))
        self.assertEqual("2028-02-29T07:00", local(schedule_rules.next_run(monthly, ts("2028-02-01T00:00"))))
        self.assertEqual("2027-03-31T07:00", local(schedule_rules.next_run(monthly, ts("2027-02-28T07:00"))))
        hours = {"type": "hours", "every": 5, "start": "08:15"}
        got = schedule_rules.runs_between(hours, ts("2026-10-13T08:00"), ts("2026-10-14T09:00"))
        self.assertEqual(["2026-10-13T08:15", "2026-10-13T13:15", "2026-10-13T18:15", "2026-10-13T23:15",
                          "2026-10-14T08:15"], [local(stamp) for stamp in got])

    def test_once_runs_a_single_time(self) -> None:
        once = {"type": "once", "at": "2026-10-20T09:00:45"}
        self.assertEqual("2026-10-20T09:00", schedule_rules.validate(once)["at"])
        self.assertEqual("2026-10-20T09:00", local(schedule_rules.next_run(once, ts("2026-10-13T09:00"))))
        self.assertIsNone(schedule_rules.next_run(once, ts("2026-10-20T09:00")))

    def test_cron_fields_steps_ranges_and_day_rules(self) -> None:
        work = {"type": "cron", "expr": "*/20 9-10 * * 1-5"}
        got = schedule_rules.runs_between(work, ts("2026-10-16T10:30"), ts("2026-10-19T09:30"))  # Fri → Mon
        self.assertEqual(["2026-10-16T10:40", "2026-10-19T09:00", "2026-10-19T09:20"], [local(s) for s in got])
        # Day-of-month and weekday both restricted: either one matches (cron's rule); 7 is Sunday.
        either = {"type": "cron", "expr": "0 8 1 * 7"}
        got = schedule_rules.runs_between(either, ts("2026-10-30T00:00"), ts("2026-11-09T00:00"))
        self.assertEqual(["2026-11-01T08:00", "2026-11-08T08:00"], [local(s) for s in got])
        for bad in ("0 9 * *", "61 * * * *", "0 24 * * *", "0 9 * * 8", "a b c d e", "0 9 */0 * *", "5-1 * * * *"):
            with self.subTest(expr=bad), self.assertRaises(schedule_rules.ScheduleError):
                schedule_rules.validate({"type": "cron", "expr": bad})

    def test_invalid_simple_schedules_say_what_is_wrong(self) -> None:
        cases = [({"type": "daily", "time": "25:00"}, "time"), ({"type": "weekly", "time": "09:00", "weekdays": []}, "weekdays"),
                 ({"type": "monthly", "time": "09:00", "day": 0}, "day"), ({"type": "hours", "every": 0}, "every"),
                 ({"type": "yearly"}, "type"), ("daily", "object"), ({"type": "once", "at": "soon"}, "at")]
        for schedule, word in cases:
            with self.subTest(schedule=schedule), self.assertRaisesRegex(schedule_rules.ScheduleError, word):
                schedule_rules.validate(schedule)

    def test_plain_descriptions(self) -> None:
        self.assertEqual("Every Monday at 09:00", schedule_rules.describe({"type": "weekly", "time": "9:00", "weekdays": [0]}))
        self.assertEqual("Every weekday at 08:30", schedule_rules.describe({"type": "weekly", "time": "08:30", "weekdays": [4, 3, 2, 1, 0]}))
        self.assertEqual("Every Monday, Wednesday and Friday at 07:00",
                         schedule_rules.describe({"type": "weekly", "time": "07:00", "weekdays": [0, 2, 4]}))
        self.assertEqual("Every day at 18:00", schedule_rules.describe({"type": "daily", "time": "18:00"}))
        self.assertEqual("Every month on the 1st at 09:00", schedule_rules.describe({"type": "monthly", "time": "09:00", "day": 1}))
        self.assertIn("last day", schedule_rules.describe({"type": "monthly", "time": "09:00", "day": 31}))
        self.assertEqual("Every 3 hours from 08:00", schedule_rules.describe({"type": "hours", "every": 3, "start": "08:00"}))
        self.assertEqual("Once on Tue 20 Oct 2026 at 09:00", schedule_rules.describe({"type": "once", "at": "2026-10-20T09:00"}))

    def test_wall_clock_holds_across_daylight_saving(self) -> None:
        with time_zone("America/New_York"):
            daily = {"type": "daily", "time": "09:00"}
            # Clocks go back on 1 Nov 2026: still 09:00 local, so 25 hours apart.
            first = schedule_rules.next_run(daily, ts("2026-10-31T10:00"))
            second = schedule_rules.next_run(daily, first)
            self.assertEqual(("2026-11-01T09:00", "2026-11-02T09:00"), (local(first), local(second)))
            self.assertEqual(24 * 3600, second - first)
            self.assertEqual(25 * 3600, first - schedule_rules.next_run(daily, ts("2026-10-30T10:00")))
            # 02:30 does not exist on 14 Mar 2027: that day's run happens once, then 02:30 again.
            early = {"type": "daily", "time": "02:30"}
            on_change = schedule_rules.next_run(early, ts("2027-03-14T00:00"))
            after = schedule_rules.next_run(early, on_change)
            self.assertEqual("2027-03-14", local(on_change)[:10])
            self.assertEqual("2027-03-15T02:30", local(after))


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mac-mcp-tasks-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.settings_file = self.dir / "settings.json"
        self.settings_file.write_text(json.dumps({"privacy": {}}))
        env = patch.dict(os.environ, {"MAC_MCP_STATE_DIR": str(self.dir / "state"),
                                       "MAC_MCP_SETTINGS_PATH": str(self.settings_file)})
        env.start()
        self.addCleanup(env.stop)
        privacy_policy._cache.update({"at": -1.0, "value": None})
        self.addCleanup(privacy_policy._cache.update, {"at": -1.0, "value": None})
        self.folder = self.dir / "work"
        self.folder.mkdir()

    def privacy(self, **values) -> None:
        self.settings_file.write_text(json.dumps({"privacy": values}))
        privacy_policy._cache.update({"at": -1.0, "value": None})

    def script(self, name: str, body: str, *, executable: bool = False) -> Path:
        path = self.folder / name
        path.write_text(body)
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def ai(self, **extra):
        return {"name": "Weekly ad report", "kind": "ai", "prompt": "Summarize last week's leads.",
                "schedule": {"type": "weekly", "time": "09:00", "weekdays": [0]}, **extra}

    def sh(self, body: str = "echo hello\n", **extra):
        return {"name": "Say hello", "kind": "script", "script": str(self.script("hello.sh", body)),
                "schedule": {"type": "daily", "time": "18:00"}, **extra}


class StoreTests(StoreCase):
    def test_list_tasks_reads_latest_runs_once_and_keeps_the_public_shape(self) -> None:
        first = store.create_task(self.ai(name="First task"), now=100)
        second = store.create_task(self.ai(name="Second task"), now=101)
        store.record_skip(first["id"], scheduled_for=110, reason="first", now=110)
        store.record_skip(first["id"], scheduled_for=120, reason="newest", now=120)
        store.record_skip(second["id"], scheduled_for=130, reason="second", now=130)

        # get_task still uses the one-task lookup, so it is a useful oracle for
        # the batch list's unchanged public result.
        expected = [store.get_task(first["id"]), store.get_task(second["id"])]
        statements = []
        original_connect = store.sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(store.sqlite3, "connect", side_effect=traced_connect):
            listed = store.list_tasks()

        self.assertEqual(expected, listed)
        latest_queries = [sql for sql in statements if "latest_rank" in sql]
        self.assertEqual(1, len(latest_queries))
        self.assertIn("ROW_NUMBER() OVER", latest_queries[0])
        self.assertFalse(any("SELECT * FROM runs WHERE task_id=" in sql for sql in statements))
        self.assertEqual("newest", listed[0]["last_run"]["reason"])

    def test_owner_tasks_are_live_and_agent_tasks_wait_for_approval(self) -> None:
        now = ts("2026-10-13T12:00")
        mine = store.create_task(self.ai(description="For Monday's meeting."), now=now)
        self.assertEqual(("active", "Every Monday at 09:00"), (mine["state"], mine["schedule_text"]))
        self.assertEqual("2026-10-19T09:00", local(mine["next_run_at"]))
        self.assertEqual(0o600, stat.S_IMODE(store.db_path().stat().st_mode))
        suggested = store.create_task(self.sh(), by="Claude Code", now=now)
        self.assertEqual(("needs_approval", None, False), (suggested["state"], suggested["next_run_at"], suggested["approved"]))
        self.assertEqual(("Claude Code", True), (suggested["proposal"]["proposed_by"], suggested["proposal"]["is_new"]))
        self.assertEqual(suggested["id"], store.list_tasks()[0]["id"])  # waiting tasks come first
        self.assertEqual([], [t for t in store.due_tasks(ts("2026-10-20T00:00")) if t["id"] == suggested["id"]])
        approved = store.approve(suggested["id"], now=now)
        self.assertEqual(("active", "2026-10-13T18:00"), (approved["state"], local(approved["next_run_at"])))
        with self.assertRaises(store.TaskError):
            store.approve(suggested["id"])

    def test_agent_edit_is_a_proposal_and_the_approved_task_keeps_running(self) -> None:
        now = ts("2026-10-13T12:00")
        task = store.create_task(self.sh(), now=now)
        edited = store.update_task(task["id"], {"schedule": {"type": "daily", "time": "06:00"}}, by="Codex", now=now)
        self.assertEqual(("Every day at 18:00", "Every day at 06:00", False),
                         (edited["schedule_text"], edited["proposal"]["schedule_text"], edited["proposal"]["is_new"]))
        self.assertEqual([task["id"]], [t["id"] for t in store.due_tasks(ts("2026-10-13T18:00"))])
        self.assertEqual({"ok": True, "id": task["id"], "removed": False}, store.decline(task["id"]))
        self.assertNotIn("proposal", store.get_task(task["id"]))
        store.update_task(task["id"], {"name": "Hello"}, by="Codex", now=now)
        self.assertEqual("Hello", store.approve(task["id"], now=now)["name"])

    def test_declining_a_new_suggestion_removes_it(self) -> None:
        task = store.create_task(self.ai(), by="ChatGPT")
        self.assertTrue(store.decline(task["id"])["removed"])
        with self.assertRaises(store.TaskError):
            store.get_task(task["id"])

    def test_owner_edit_pause_resume_and_delete(self) -> None:
        now = ts("2026-10-13T12:00")
        task = store.create_task(self.sh(), now=now)
        paused = store.set_paused(task["id"], True, now=now)
        self.assertEqual(("paused", None), (paused["state"], paused["next_run_at"]))
        resumed = store.set_paused(task["id"], False, now=ts("2026-10-13T19:00"))
        self.assertEqual("2026-10-14T18:00", local(resumed["next_run_at"]))
        edited = store.update_task(task["id"], {"schedule": {"type": "hours", "every": 2, "start": "08:00"}}, now=now)
        self.assertEqual(("2026-10-13T14:00", True), (local(edited["next_run_at"]), "proposal" not in edited))
        store.delete_task(task["id"])
        self.assertEqual([], store.list_tasks())

    def test_validation_messages(self) -> None:
        cases = [
            ({**self.ai(), "name": ""}, "name_required"),
            ({**self.ai(), "kind": "robot"}, "kind_invalid"),
            ({**self.ai(), "prompt": " "}, "prompt_required"),
            ({**self.ai(), "cwd": "relative/path"}, "cwd_invalid"),
            ({**self.ai(), "cwd": str(self.dir / "nope")}, "cwd_missing"),
            ({**self.ai(), "timeout_min": 0}, "timeout_invalid"),
            ({**self.ai(), "schedule": {"type": "daily", "time": "nine"}}, "schedule_invalid"),
            ({**self.sh(), "command": "echo hi"}, "script_or_command"),
            ({**self.sh(), "script": str(self.folder / "missing.sh")}, "script_missing"),
            ({**self.sh(), "script": str(self.script("notes.txt", "hi"))}, "script_type_unknown"),
            ({**self.sh(), "args": "'unclosed"}, "args_invalid"),
            ({**self.sh(), "interpreter": "/nope/python9"}, "interpreter_missing"),
        ]
        for definition, code in cases:
            with self.subTest(code=code), self.assertRaises(store.TaskError) as caught:
                store.validate_definition(definition)
            self.assertEqual(code, caught.exception.code)

    def test_script_types_and_nearby_virtualenv(self) -> None:
        self.assertEqual(("Shell script", "/bin/bash"),
                         tuple(store.inspect_script(str(self.script("a.sh", "")))[k] for k in ("label", "program")))
        self.assertEqual("/usr/bin/osascript", store.inspect_script(str(self.script("a.applescript", "")))["program"])
        tool = self.script("tool", "#!/bin/sh\necho hi\n", executable=True)
        self.assertEqual(("Program", "direct"), (store.inspect_script(str(tool))["label"],
                                                 store.inspect_script(str(tool))["program_source"]))
        venv = self.folder / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(Path(os.sys.executable))
        sub = self.folder / "reports"
        sub.mkdir()
        (sub / "report.py").write_text("print('ok')\n")
        info = store.inspect_script(str(sub / "report.py"))
        self.assertEqual(("Python script", "venv", str(venv / "python")), (info["label"], info["program_source"], info["program"]))

    def test_run_history_is_trimmed(self) -> None:
        task = store.create_task(self.sh())
        old = time.time() - (store.RUN_RETENTION_DAYS + 1) * 86400
        store.record_skip(task["id"], scheduled_for=old, reason="old", now=old)
        for index in range(store.RUNS_PER_TASK + 5):
            run_id = store.start_run(task["id"], trigger="manual", now=time.time() + index)
            store.finish_run(run_id, "done", summary=f"run {index}", output="x" * (store.OUTPUT_LIMIT + 10))
        runs = store.list_runs(task["id"])
        self.assertEqual(store.RUNS_PER_TASK, len(runs))
        self.assertEqual(f"run {store.RUNS_PER_TASK + 4}", runs[0]["summary"])
        self.assertEqual(store.OUTPUT_LIMIT, len(store.get_run(runs[0]["id"])["output"]))
        self.assertNotIn("old", {run.get("reason") for run in runs})

    def test_deleting_a_task_while_it_runs_ends_the_run_quietly(self) -> None:
        # #94: delete_task removes the task's runs; the run still going then finished with
        # row["task_id"] on a missing row, a TypeError that killed the runner thread.
        task = store.create_task(self.sh())
        run_id = store.start_run(task["id"], trigger="manual")
        store.delete_task(task["id"])
        finished = store.finish_run(run_id, "done", summary="finished after the task was deleted")
        self.assertEqual((run_id, "done", True), (finished["id"], finished["status"], finished["task_deleted"]))
        self.assertEqual([], store.list_runs(task["id"]))
        # Once more is just as quiet; and nothing about it starts again.
        self.assertTrue(store.finish_run(run_id, "failed", reason="again")["task_deleted"])
        self.assertEqual([], store.due_tasks(time.time() + 10 * 86400))

    def test_an_agent_run_whose_task_was_deleted_is_let_go(self) -> None:
        task = store.create_task(self.ai())
        run_id = store.start_run(task["id"], trigger="manual")
        store.delete_task(task["id"])
        scheduler = runner.Scheduler(settings=None, launch=lambda *args: None)
        with patch.object(runner, "wait_for_agent", return_value={"status": "done", "summary": "ok"}):
            scheduler._follow_agent(run_id, "agt_gone", deadline=time.time() + 5)
        self.assertEqual([], store.list_runs(task["id"]))

    def test_one_run_at_a_time(self) -> None:
        task = store.create_task(self.sh())
        first = store.start_run(task["id"], trigger="manual")
        self.assertIsNotNone(first)
        self.assertIsNone(store.start_run(task["id"], trigger="manual"))
        self.assertEqual("running", store.get_task(task["id"])["state"])
        store.finish_run(first, "done")
        self.assertIsNotNone(store.start_run(task["id"], trigger="manual"))


class ScriptRunTests(StoreCase):
    def run_task(self, definition):
        definition = store.validate_definition(definition)
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        return runner.execute(task["id"], run_id, definition, settings=None)

    def test_shell_script_success_failure_and_output(self) -> None:
        done = self.run_task(self.sh("echo first\necho 'all good'\n"))
        self.assertEqual(("done", "all good", 0), (done["status"], done["summary"], done["exit_code"]))
        self.assertIn("first", done["output"])
        failed = self.run_task(self.sh("echo 'token expired' >&2\nexit 3\n"))
        self.assertEqual(("failed", "Exited with code 3", "token expired"),
                         (failed["status"], failed["reason"], failed["summary"]))
        self.assertEqual([], list((self.dir / "state" / "state" / "scheduled_runs").iterdir()))

    def test_python_with_arguments_in_its_folder_and_a_clean_environment(self) -> None:
        script = self.script("report.py", "import os, sys\nprint(os.getcwd())\nprint(sys.argv[1:])\n"
                                          "print('api key passed on:', 'MCP_API_KEY' in os.environ)\n")
        with patch.dict(os.environ, {"MCP_API_KEY": "must-not-leak"}):
            result = self.run_task({"name": "Report", "kind": "script", "script": str(script),
                                    "args": "--week 'last week'", "interpreter": os.sys.executable,
                                    "schedule": {"type": "daily", "time": "09:00"}})
        self.assertEqual("done", result["status"])
        self.assertIn(str(self.folder.resolve()).replace("/private", ""), result["output"].replace("/private", ""))
        self.assertIn("['--week', 'last week']", result["output"])
        self.assertIn("api key passed on: False", result["output"])  # (KEY=… lines are masked when stored, #199)

    def test_command_applescript_and_node(self) -> None:
        command = self.run_task({"name": "Cmd", "kind": "script", "command": "echo from-command && pwd",
                                 "cwd": str(self.folder), "schedule": {"type": "daily", "time": "09:00"}})
        self.assertEqual("done", command["status"])
        self.assertIn("from-command", command["output"])
        apple = self.run_task({"name": "AS", "kind": "script", "script": str(self.script("a.applescript", 'return "hi from AppleScript"')),
                               "schedule": {"type": "daily", "time": "09:00"}})
        self.assertEqual(("done", "hi from AppleScript"), (apple["status"], apple["summary"]))
        if store.find_program("node"):
            node = self.run_task({"name": "JS", "kind": "script", "script": str(self.script("a.mjs", "console.log(6 * 7)")),
                                  "schedule": {"type": "daily", "time": "09:00"}})
            self.assertEqual(("done", "42"), (node["status"], node["summary"]))

    def test_time_limit_stops_the_script_and_its_children(self) -> None:
        marker = self.folder / "child.pid"
        body = f"sleep 30 &\necho $! > {marker}\necho started\nwait\n"
        with patch.object(runner, "_limit_s", return_value=1):
            result = self.run_task(self.sh(body))
        self.assertEqual("timed_out", result["status"])
        self.assertIn("time limit", result["reason"])
        child = int(marker.read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_private_mode_keeps_only_how_it_ended(self) -> None:
        self.privacy(private_mode=True)
        result = self.run_task(self.sh("echo secret-output\n"))
        self.assertEqual(("done", None, None), (result["status"], result["summary"], result["output"]))
        self.assertEqual({"private_mode": True}, result["detail"])

    def test_private_mode_switched_on_during_the_run_covers_it(self) -> None:
        # Roadmap #54: the owner turns Private mode on while the task runs.
        self.privacy(private_mode=False)
        on = json.dumps({"privacy": {"private_mode": True}})
        result = self.run_task(self.sh(f"echo secret-output\nprintf '%s' '{on}' > '{self.settings_file}'\n"))
        self.assertEqual(("done", None, None), (result["status"], result["summary"], result["output"]))
        self.assertEqual({"private_mode": True}, result["detail"])


class AiRunTests(StoreCase):
    def fake_agents(self, final):
        calls = {"spawn": [], "polls": 0}

        def spawn(settings, provider, prompt, **kwargs):
            calls["spawn"].append({"provider": provider, "prompt": prompt, **kwargs})
            return {"agent_id": "agent_abc"}

        def get(settings, agent_id, **kwargs):
            calls["polls"] += 1
            return {"status": "running"} if calls["polls"] < 2 else final

        from mcp_server import tools_agents
        patches = [patch.object(tools_agents, "spawn_agent", spawn), patch.object(tools_agents, "get_agent", get),
                   patch.object(runner, "AGENT_POLL_S", 0)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return calls

    def execute(self):
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder)))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        return task, runner.execute(task["id"], run_id, definition, settings=None)

    def test_agent_result_becomes_the_run_summary_and_report(self) -> None:
        calls = self.fake_agents({"status": "completed", "provider": "codex", "model": "gpt-5",
                                  "result_envelope": {"outcome": "success", "summary": "Leads up 12% to 214.",
                                                      "artifacts": [{"ref": "~/Desktop/report.xlsx", "description": "Excel"}]},
                                  "result_full": {"text": "Full report text\n\nTASK_RESULT_ENVELOPE_V1\n{\"schema_version\":1}"}})
        task, result = self.execute()
        self.assertEqual(("done", "Leads up 12% to 214.", "Full report text", "agent_abc"),
                         (result["status"], result["summary"], result["output"], result["agent_id"]))
        self.assertEqual("~/Desktop/report.xlsx", result["detail"]["artifacts"][0]["ref"])
        self.assertEqual({"steps": [], "usage": {}}, {k: result["detail"][k] for k in ("steps", "usage")})
        spawned = calls["spawn"][0]
        self.assertEqual(("codex", "Summarize last week's leads.", str(self.folder), 1800, "Scheduled: Weekly ad report"),
                         (spawned["provider"], spawned["prompt"], spawned["cwd"], spawned["timeout_s"], spawned["title"]))

    def test_agent_failure_and_start_errors(self) -> None:
        self.fake_agents({"status": "failed", "error": "Facebook token expired",
                          "result_envelope": {"outcome": "failure", "summary": "Could not log in"}})
        _, result = self.execute()
        self.assertEqual(("failed", "Facebook token expired"), (result["status"], result["reason"]))
        from fastapi import HTTPException

        from mcp_server import tools_agents
        with patch.object(tools_agents, "spawn_agent", side_effect=HTTPException(403, "Provider codex is disabled.")):
            _, refused = self.execute()
        self.assertEqual("failed", refused["status"])
        self.assertIn("disabled", refused["reason"])

    def test_scheduled_agents_get_the_global_profile(self) -> None:
        calls = self.fake_agents({"status": "completed", "result_envelope": {"outcome": "success"}})
        from mcp_server import policy
        with patch.object(policy, "permission_profile_name", return_value="standard"):
            self.execute()
        self.assertEqual(("standard", "read_only"), (calls["spawn"][0]["parent_profile"], calls["spawn"][0]["access_mode"]))
        with patch.object(policy, "permission_profile_name", return_value="trusted"):
            self.execute()
        self.assertEqual(("trusted", "workspace_write"), (calls["spawn"][1]["parent_profile"], calls["spawn"][1]["access_mode"]))
        # The real scope check accepts both: a Standard run is narrowed, not refused.
        from mcp_server import tools_agents
        for profile, access in (("standard", "read_only"), ("trusted", "workspace_write")):
            scope, child, _ = tools_agents._requested_agent_scope(self.folder, access, None, None, profile)
            self.assertEqual(access, scope.access_mode.value)

    def test_paused_observation_skips_ai_runs(self) -> None:
        calls = self.fake_agents({"status": "completed"})
        self.privacy(observation_paused=True)
        _, result = self.execute()
        self.assertEqual(("skipped", runner.REASON_PAUSED), (result["status"], result["reason"]))
        self.assertEqual([], calls["spawn"])


class SchedulerTests(StoreCase):
    def make(self, now: float):
        launched = []
        loop = runner.Scheduler(None, clock=lambda: now, launch=lambda *args: launched.append(args))
        return loop, launched

    def test_due_task_starts_and_moves_to_its_next_time(self) -> None:
        task = store.create_task(self.sh(), now=ts("2026-10-13T12:00"))
        loop, launched = self.make(ts("2026-10-13T12:00"))
        self.assertEqual([], loop.tick(ts("2026-10-13T17:59")))
        actions = loop.tick(ts("2026-10-13T18:00:30"))
        self.assertEqual(["started"], [a["action"] for a in actions])
        self.assertEqual(task["id"], launched[0][0])
        self.assertEqual("2026-10-14T18:00", local(store.get_task(task["id"])["next_run_at"]))

    def test_runs_missed_while_asleep_are_skipped_with_the_reason(self) -> None:
        task = store.create_task({**self.sh(), "schedule": {"type": "hours", "every": 1, "start": "00:00"}},
                                 now=ts("2026-10-13T08:30"))
        loop, launched = self.make(ts("2026-10-13T08:30"))
        actions = loop.tick(ts("2026-10-13T12:01"))  # 09, 10, 11 missed; 12:00 is one minute late
        self.assertEqual([("skipped", 3), ("started", None)], [(a["action"], a.get("count")) for a in actions])
        skipped = store.list_runs(task["id"])[0]
        self.assertEqual(("skipped", runner.REASON_ASLEEP, 3), (skipped["status"], skipped["reason"], skipped["skipped_count"]))
        self.assertEqual("2026-10-13T09:00", local(skipped["scheduled_for"]))
        self.assertEqual(1, len(launched))
        self.assertEqual("2026-10-13T13:00", local(store.get_task(task["id"])["next_run_at"]))

    def test_runs_missed_while_mac_mcp_was_stopped(self) -> None:
        task = store.create_task(self.sh(), now=ts("2026-10-12T12:00"))
        loop, launched = self.make(ts("2026-10-13T09:00"))  # the server started this morning
        loop.tick(ts("2026-10-13T09:00"))
        self.assertEqual(runner.REASON_STOPPED, store.list_runs(task["id"])[0]["reason"])
        self.assertEqual([], launched)

    def test_run_once_after_sleep_catches_up_with_one_run(self) -> None:
        task = store.create_task({**self.sh(), "missed": "run_once",
                                  "schedule": {"type": "hours", "every": 1, "start": "00:00"}}, now=ts("2026-10-13T08:30"))
        self.assertEqual("run_once", store.get_task(task["id"])["missed"])
        loop, launched = self.make(ts("2026-10-13T08:30"))
        actions = loop.tick(ts("2026-10-13T11:40"))  # 09, 10 and 11 were missed; nothing is due now
        self.assertEqual([("skipped", 2), ("started", None)], [(a["action"], a.get("count")) for a in actions])
        self.assertEqual(1, len(launched))  # one catch-up run, never one per missed hour
        runs = store.list_runs(task["id"])
        caught = next(run for run in runs if run["status"] == "running")
        self.assertEqual(("catch_up", "2026-10-13T11:00"), (caught["trigger"], local(caught["scheduled_for"])))
        skipped = next(run for run in runs if run["status"] == "skipped")
        self.assertEqual((2, runner.REASON_ASLEEP + runner.REASON_CAUGHT_UP), (skipped["skipped_count"], skipped["reason"]))
        self.assertEqual("2026-10-13T12:00", local(store.get_task(task["id"])["next_run_at"]))

    def test_run_once_does_not_add_a_run_when_one_is_due_now(self) -> None:
        task = store.create_task({**self.sh(), "missed": "run_once",
                                  "schedule": {"type": "hours", "every": 1, "start": "00:00"}}, now=ts("2026-10-13T08:30"))
        loop, launched = self.make(ts("2026-10-13T08:30"))
        actions = loop.tick(ts("2026-10-13T12:01"))  # 12:00 is due now: that run covers the missed ones
        self.assertEqual([("skipped", 3), ("started", None)], [(a["action"], a.get("count")) for a in actions])
        self.assertEqual(1, len(launched))
        self.assertEqual("schedule", next(r for r in store.list_runs(task["id"]) if r["status"] == "running")["trigger"])

    def test_skip_stays_the_default_and_run_once_is_for_timed_tasks(self) -> None:
        plain = store.create_task(self.sh(), now=ts("2026-10-13T12:00"))
        self.assertNotIn("missed", store.get_task(plain["id"]))
        self.assertNotIn("missed", store.create_task({**self.sh(), "missed": "skip"}))
        with self.assertRaises(store.TaskError) as bad:
            store.create_task({**self.sh(), "missed": "always"})
        self.assertEqual("missed_invalid", bad.exception.code)
        with self.assertRaises(store.TaskError) as folder:
            store.create_task({**self.sh(), "missed": "run_once", "schedule": {"type": "folder", "folder": str(self.folder)}})
        self.assertEqual("missed_needs_times", folder.exception.code)

    def test_a_missed_one_time_task_runs_once_when_back(self) -> None:
        task = store.create_task({**self.sh(), "missed": "run_once",
                                  "schedule": {"type": "once", "at": "2026-10-13T09:00"}}, now=ts("2026-10-13T08:00"))
        loop, launched = self.make(ts("2026-10-13T08:00"))
        loop.tick(ts("2026-10-13T14:00"))
        self.assertEqual(1, len(launched))
        self.assertEqual("catch_up", store.list_runs(task["id"])[0]["trigger"])

    def test_a_run_still_going_makes_the_next_one_skip(self) -> None:
        task = store.create_task(self.sh(), now=ts("2026-10-13T12:00"))
        store.start_run(task["id"], trigger="manual", now=ts("2026-10-13T17:00"))
        loop, launched = self.make(ts("2026-10-13T12:00"))
        loop.tick(ts("2026-10-13T18:00"))
        self.assertEqual([], launched)
        self.assertEqual(runner.REASON_BUSY, [r for r in store.list_runs(task["id"]) if r["status"] == "skipped"][0]["reason"])

    def test_run_after_waits_for_the_run_going_and_then_runs_once(self) -> None:
        # #57: with busy=run_after, times that come due during a long run are not dropped: one run follows it.
        task = store.create_task({**self.sh(), "busy": "run_after",
                                  "schedule": {"type": "hours", "every": 1, "start": "00:00"}}, now=ts("2026-10-13T11:30"))
        self.assertEqual("run_after", store.get_task(task["id"])["busy"])
        long_run = store.start_run(task["id"], trigger="manual", now=ts("2026-10-13T11:50"))
        loop, launched = self.make(ts("2026-10-13T11:30"))
        self.assertEqual([("queued", None)], [(a["action"], a.get("run_id")) for a in loop.tick(ts("2026-10-13T12:00"))])
        loop.tick(ts("2026-10-13T13:00"))  # another one due while it still runs: still one waiting
        self.assertEqual([], launched)
        self.assertEqual([], [r for r in store.list_runs(task["id"]) if r["status"] == "skipped"])
        store.finish_run(long_run, "done", now=ts("2026-10-13T13:20"))
        started = loop.tick(ts("2026-10-13T13:21"))
        self.assertEqual(["started"], [a["action"] for a in started])
        self.assertEqual(1, len(launched))
        follow = next(r for r in store.list_runs(task["id"]) if r["status"] == "running")
        self.assertEqual(("schedule", "2026-10-13T12:00"), (follow["trigger"], local(follow["scheduled_for"])))
        # Nothing waits any more: a restart (a new loop) does not start it again.
        store.finish_run(follow["id"], "done", now=ts("2026-10-13T13:30"))
        again, relaunched = self.make(ts("2026-10-13T13:31"))
        again.tick(ts("2026-10-13T13:31"))
        self.assertEqual([], relaunched)

    def test_a_waiting_run_is_dropped_when_the_task_is_paused(self) -> None:
        task = store.create_task({**self.sh(), "busy": "run_after"}, now=ts("2026-10-13T12:00"))
        store.start_run(task["id"], trigger="manual", now=ts("2026-10-13T17:00"))
        loop, launched = self.make(ts("2026-10-13T12:00"))
        loop.tick(ts("2026-10-13T18:00"))
        self.assertEqual([task["id"]], [row["id"] for row in store.waiting_after_busy()])
        store.set_paused(task["id"], True)
        store.set_paused(task["id"], False)
        self.assertEqual([], store.waiting_after_busy())

    def test_busy_is_skip_by_default_and_run_after_is_for_timed_tasks(self) -> None:
        self.assertNotIn("busy", store.get_task(store.create_task(self.sh())["id"]))
        with self.assertRaises(store.TaskError):
            store.create_task({**self.sh(), "busy": "later"})
        with self.assertRaises(store.TaskError) as folder:
            store.create_task({**self.sh(), "busy": "run_after",
                               "schedule": {"type": "folder", "folder": str(self.folder)}})
        self.assertEqual("busy_needs_times", folder.exception.code)

    def test_sleep_follows_the_next_run_and_idles_without_tasks(self) -> None:
        loop, _ = self.make(ts("2026-10-13T12:00"))
        self.assertIsNone(loop.sleep_for(ts("2026-10-13T12:00")))
        task = store.create_task(self.sh(), now=ts("2026-10-13T12:00"))
        self.assertEqual(runner.MAX_SLEEP_S, loop.sleep_for(ts("2026-10-13T12:00")))
        self.assertEqual(20, loop.sleep_for(ts("2026-10-13T17:59:40")))
        store.set_paused(task["id"], True)
        self.assertIsNone(loop.sleep_for(ts("2026-10-13T12:00")))

    def test_run_now_needs_an_approved_task_and_one_run_at_a_time(self) -> None:
        loop, launched = self.make(time.time())
        suggested = store.create_task(self.sh(), by="Codex")
        with self.assertRaises(store.TaskError) as caught:
            loop.run_now(suggested["id"])
        self.assertEqual("not_approved", caught.exception.code)
        store.approve(suggested["id"])
        self.assertEqual("started", loop.run_now(suggested["id"])["action"])
        with self.assertRaises(store.TaskError):
            loop.run_now(suggested["id"])
        self.assertEqual(1, len(launched))

    def test_restart_closes_script_runs_and_follows_agents(self) -> None:
        task = store.create_task(self.sh())
        orphan = store.start_run(task["id"], trigger="manual")
        ai_task = store.create_task(self.ai())
        agent_run = store.start_run(ai_task["id"], trigger="schedule")
        store.note_agent(agent_run, "agent_xyz")
        loop, _ = self.make(time.time())
        with patch.object(runner, "wait_for_agent", return_value={"status": "done", "summary": "Back"}) as waited:
            loop.recover()
            deadline = time.time() + 5
            while store.get_run(agent_run)["status"] == "running" and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(("failed", runner.REASON_RESTARTED), (store.get_run(orphan)["status"], store.get_run(orphan)["reason"]))
        self.assertEqual(("done", "Back"), (store.get_run(agent_run)["status"], store.get_run(agent_run)["summary"]))
        self.assertEqual("agent_xyz", waited.call_args.args[0])

    def test_a_script_still_running_after_a_restart_is_found_and_stopped(self) -> None:
        task = store.create_task(self.sh())
        run_id = store.start_run(task["id"], trigger="manual")
        log_path = runner._logs_dir() / f"{run_id}.log"
        marker = self.dir / "still-running"
        # The script a Mac MCP that stopped had started: it runs in its own session and keeps going.
        thread = threading.Thread(target=runner._run_logged, daemon=True, args=(
            ["/bin/sh", "-c", f"while true; do touch {marker}; sleep 0.2; done"], str(self.folder), 60, log_path,
            runner._tail))
        thread.start()
        record = runner._process_record(log_path)
        deadline = time.time() + 10
        while not record.exists() and time.time() < deadline:
            time.sleep(0.05)
        pid = json.loads(record.read_text())["pid"]
        self.assertTrue(runner.managed_process.pid_alive(pid))
        loop, _ = self.make(time.time())
        loop.recover()
        self.assertEqual(("failed", runner.REASON_SCRIPT_STOPPED),
                         (store.get_run(run_id)["status"], store.get_run(run_id)["reason"]))
        self.assertFalse(runner.managed_process.pid_alive(pid))
        self.assertFalse(record.exists())
        thread.join(10)

    def test_a_reused_pid_is_never_stopped(self) -> None:
        task = store.create_task(self.sh())
        run_id = store.start_run(task["id"], trigger="manual")
        runner._logs_dir().mkdir(parents=True, exist_ok=True)
        # A record whose pid now belongs to another process (here: this test) with another start time.
        (runner._logs_dir() / f"{run_id}.proc").write_text(json.dumps({"pid": os.getpid(), "start_time": "Mon Jan  1 00:00:00 2001"}))
        self.assertIsNone(runner.stop_left_scripts(run_id))
        self.assertTrue(runner.managed_process.pid_alive(os.getpid()))

    def test_the_loop_thread_runs_a_due_task_end_to_end(self) -> None:
        store.create_task(self.sh("echo loop-ran\n", schedule={"type": "once", "at": "2026-01-01T00:00"}))
        task = store.list_tasks()[0]
        self.assertEqual("finished", task["state"])  # a past one-time task never runs
        live = store.create_task(self.sh("echo loop-ran\n"))
        with store._db() as conn:
            conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (time.time() - 1, live["id"]))
        loop = runner.Scheduler(None)
        loop.begin()
        self.addCleanup(loop.end)
        deadline = time.time() + 10
        runs = []
        while time.time() < deadline:
            runs = [r for r in store.list_runs(live["id"]) if r["status"] == "done"]
            if runs:
                break
            time.sleep(0.05)
        self.assertEqual("loop-ran", runs[0]["summary"])


class DashboardApiTests(StoreCase):
    def client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from mcp_server.dashboard_routes import create_dashboard_routes
        from mcp_server.observability import TelemetryManager
        from mcp_server.security import load_settings

        telemetry = TelemetryManager(db_path=self.dir / "t.sqlite3")
        return TestClient(Starlette(routes=create_dashboard_routes(telemetry, load_settings(), TOKEN)))

    def test_owner_creates_edits_approves_and_reads_runs(self) -> None:
        client = self.client()
        self.assertEqual(401, client.get("/dashboard/api/scheduled-tasks").status_code)
        bad = client.post("/dashboard/api/scheduled-tasks", headers=AUTH, json={"definition": {**self.ai(), "name": ""}})
        self.assertEqual((400, "name_required"), (bad.status_code, bad.json()["error"]))
        created = client.post("/dashboard/api/scheduled-tasks", headers=AUTH, json={"definition": self.sh()}).json()["task"]
        self.assertEqual("active", created["state"])
        edited = client.post("/dashboard/api/scheduled-tasks", headers=AUTH,
                             json={"id": created["id"], "definition": {"description": "Evening hello"}}).json()["task"]
        self.assertEqual("Evening hello", edited["description"])
        suggested = store.create_task(self.ai(), by="Claude Code")
        listing = client.get("/dashboard/api/scheduled-tasks", headers=AUTH).json()
        self.assertEqual([suggested["id"], created["id"]], [t["id"] for t in listing["tasks"]])
        self.assertEqual([created["id"]], [t["id"] for t in listing["up_next"]])
        self.assertIn("agents", listing)
        approved = client.post("/dashboard/api/scheduled-tasks/action", headers=AUTH,
                               json={"id": suggested["id"], "action": "approve"}).json()
        self.assertEqual("active", approved["result"]["state"])
        self.assertEqual(400, client.post("/dashboard/api/scheduled-tasks/action", headers=AUTH,
                                          json={"id": suggested["id"], "action": "explode"}).status_code)
        with patch.object(runner.Scheduler, "_launch_thread", lambda *args: None):
            started = client.post("/dashboard/api/scheduled-tasks/action", headers=AUTH,
                                  json={"id": created["id"], "action": "run_now"}).json()
        run_id = started["result"]["run_id"]
        store.finish_run(run_id, "done", summary="hello", output="hello\n")
        runs = client.get(f"/dashboard/api/scheduled-tasks/runs?task_id={created['id']}", headers=AUTH).json()
        self.assertEqual(["done"], [r["status"] for r in runs["runs"]])
        run = client.get(f"/dashboard/api/scheduled-tasks/run?run_id={run_id}", headers=AUTH).json()["run"]
        self.assertEqual("hello\n", run["output"])
        finished = client.get("/dashboard/api/scheduled-tasks/finished?since=0", headers=AUTH).json()["runs"]
        self.assertEqual([("Say hello", True)], [(r["task_name"], r["notify"]) for r in finished])
        self.assertEqual(404, client.get("/dashboard/api/scheduled-tasks/run?run_id=run_x", headers=AUTH).status_code)

    def test_preview_sentence_and_script_check(self) -> None:
        client = self.client()
        preview = client.post("/dashboard/api/scheduled-tasks/preview", headers=AUTH, json={
            "schedule": {"type": "weekly", "time": "09:00", "weekdays": [0]},
            "script": str(self.script("x.py", "print(1)"))}).json()
        self.assertEqual("Every Monday at 09:00", preview["schedule"]["text"])
        self.assertEqual(3, len(preview["schedule"]["next_runs"]))
        self.assertEqual("Python script", preview["script"]["label"])
        broken = client.post("/dashboard/api/scheduled-tasks/preview", headers=AUTH,
                             json={"schedule": {"type": "cron", "expr": "nope"}}).json()
        self.assertFalse(broken["schedule"]["ok"])

    def test_preview_shows_dates_for_the_next_run_and_checks_the_data_script(self) -> None:
        client = self.client()
        preview = client.post("/dashboard/api/scheduled-tasks/preview", headers=AUTH, json={
            "schedule": {"type": "weekly", "time": "09:00", "weekdays": [0]},
            "data": {"script": str(self.script("d.py", "print(1)")), "args": "--since {last_week_start}"},
            "args": "--day {yesterday}"}).json()
        next_run = preview["schedule"]["next_runs"][0]
        self.assertEqual("--since " + store.expand_dates("{last_week_start}", next_run), preview["data"]["next_run"])
        self.assertEqual("--day " + store.expand_dates("{yesterday}", next_run), preview["args"]["next_run"])
        self.assertEqual("Python script", preview["data"]["script"]["label"])
        self.assertEqual(store.date_choices(next_run), preview["dates"])
        wrong = client.post("/dashboard/api/scheduled-tasks/preview", headers=AUTH,
                            json={"data": {"command": "echo {last_monday}"}}).json()
        self.assertFalse(wrong["data"]["ok"])
        self.assertIn("{last_monday}", wrong["data"]["message"])


class OutputCheckTests(StoreCase):
    """#198: "it ran" and "it made what it should" are told apart, when the owner asks for a check."""

    def run_with(self, body: str, verify, **extra):
        definition = store.validate_definition(self.sh(body, cwd=str(self.folder), verify=verify, **extra))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        return runner.execute(task["id"], run_id, definition, settings=None)

    def test_checks_are_validated_and_relative_paths_follow_the_task_folder(self) -> None:
        definition = store.validate_definition(self.sh(cwd=str(self.folder), verify=[{"path": "out/report.csv"}]))
        self.assertEqual([{"path": str(self.folder / "out/report.csv"), "min_bytes": 1, "written_this_run": True}],
                         definition["verify"])
        self.assertNotIn("verify", store.validate_definition(self.sh()))
        for bad, code in (([{"path": "report.csv"}], "verify_needs_folder"),
                          ([{"path": ""}], "verify_path_required"),
                          ([{"path": "/tmp/a", "min_bytes": -1}], "verify_min_bytes_invalid"),
                          ([{"path": "/tmp/a", "written_this_run": "yes"}], "verify_written_invalid"),
                          ([{"path": f"/tmp/{n}"} for n in range(6)], "verify_too_many"),
                          ("report.csv", "verify_invalid")):
            with self.subTest(code=code), self.assertRaises(store.TaskError) as caught:
                store.validate_definition(self.sh(verify=bad))
            self.assertEqual(code, caught.exception.code)

    def test_exit_zero_without_the_file_is_done_but_not_verified(self) -> None:
        result = self.run_with("echo finished\n", [{"path": "report.csv"}])
        self.assertEqual("done", result["status"])
        self.assertEqual("failed", result["verification"]["state"])
        check = result["verification"]["checks"][0]
        self.assertEqual((False, "missing"), (check["ok"], check["problem"]))

    def test_a_file_written_by_this_run_is_verified(self) -> None:
        result = self.run_with("printf 'a,b\\n1,2\\n' > report.csv\n", [{"path": "report.csv", "min_bytes": 5}])
        self.assertEqual(("done", "verified"), (result["status"], result["verification"]["state"]))
        check = result["verification"]["checks"][0]
        self.assertEqual((True, 8), (check["ok"], check["size"]))
        self.assertNotIn("a,b", json.dumps(result["verification"]))  # metadata only, never the contents

    def test_too_small_old_or_linked_files_fail_their_check(self) -> None:
        small = self.run_with(": > empty.csv\n", [{"path": "empty.csv", "min_bytes": 10}])
        self.assertEqual("too_small", small["verification"]["checks"][0]["problem"])
        old = self.folder / "old.csv"
        old.write_text("kept from yesterday")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        stale = self.run_with("echo nothing new\n", [{"path": "old.csv"}])
        self.assertEqual("not_written_this_run", stale["verification"]["checks"][0]["problem"])
        any_age = self.run_with("echo nothing new\n", [{"path": "old.csv", "written_this_run": False}])
        self.assertEqual("verified", any_age["verification"]["state"])
        (self.folder / "link.csv").symlink_to(old)
        linked = self.run_with("touch old.csv\n", [{"path": "link.csv"}])
        self.assertEqual("link", linked["verification"]["checks"][0]["problem"])
        (self.folder / "folder.csv").mkdir()
        folder = self.run_with("echo hi\n", [{"path": "folder.csv"}])
        self.assertEqual("not_a_file", folder["verification"]["checks"][0]["problem"])

    def test_a_failed_run_stays_failed_and_is_not_checked(self) -> None:
        (self.folder / "report.csv").write_text("written before the failure")
        result = self.run_with("exit 3\n", [{"path": "report.csv", "written_this_run": False}])
        self.assertEqual("failed", result["status"])
        self.assertIsNone(result["verification"])
        unchecked = self.run_with("echo hi\n", None)
        self.assertIsNone(unchecked["verification"])

    def test_private_mode_keeps_only_whether_the_checks_passed(self) -> None:
        self.privacy(private_mode=True)
        result = self.run_with("echo x > report.csv\n", [{"path": "report.csv"}])
        self.assertEqual({"state": "verified"}, result["verification"])

    def test_history_lists_carry_the_outcome_and_old_databases_gain_the_column(self) -> None:
        result = self.run_with("echo finished\n", [{"path": "report.csv"}])
        brief = store.list_runs(result["task_id"])[0]
        self.assertEqual("failed", brief["verification"]["state"])
        self.assertEqual("failed", store.get_task(result["task_id"])["last_run"]["verification"]["state"])
        # A database from before #198 has no verification column; opening it adds one.
        import sqlite3
        path = store.db_path()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE runs DROP COLUMN verification")
        conn.commit()
        conn.close()
        store._DB_IDENTITIES.clear()
        self.assertIsNone(store.get_run(result["id"])["verification"])
        again = self.run_with("echo x > report.csv\n", [{"path": "report.csv"}])
        self.assertEqual("verified", again["verification"]["state"])


class AiOutputCheckTests(AiRunTests):
    def test_an_agent_that_says_done_without_the_file_is_not_verified(self) -> None:
        self.fake_agents({"status": "completed", "result_envelope": {"outcome": "success", "summary": "Report saved."}})
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder),
                                                       verify=[{"path": "report.xlsx"}]))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        result = runner.execute(task["id"], run_id, definition, settings=None)
        self.assertEqual(("done", "failed"), (result["status"], result["verification"]["state"]))

    def test_a_run_followed_after_a_restart_is_checked_too(self) -> None:
        self.fake_agents({"status": "completed", "result_envelope": {"outcome": "success"}})
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder),
                                                       verify=[{"path": "report.xlsx"}]))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        store.note_agent(run_id, "agent_abc")
        runner.Scheduler(settings=None)._follow_agent(run_id, "agent_abc", time.time() + 30)
        self.assertEqual("failed", store.get_run(run_id)["verification"]["state"])


class DateTokenTests(unittest.TestCase):
    def test_dates_relative_to_the_run(self) -> None:
        wednesday = ts("2026-10-14T09:00")
        text = ("{today} {yesterday} {this_week_start} {last_week_start} {last_week_end} "
                "{this_month_start} {last_month_start} {last_month_end} {days_ago:7}")
        self.assertEqual("2026-10-14 2026-10-13 2026-10-12 2026-10-05 2026-10-11 "
                         "2026-10-01 2026-09-01 2026-09-30 2026-10-07", store.expand_dates(text, wednesday))
        new_year = ts("2026-01-01T08:00")  # a Thursday
        self.assertEqual("2025-12-01 2025-12-31 2025-12-22 2025-12-28",
                         store.expand_dates("{last_month_start} {last_month_end} {last_week_start} {last_week_end}", new_year))
        monday = ts("2026-10-12T09:00")
        self.assertEqual("2026-10-12 2026-10-05", store.expand_dates("{this_week_start} {last_week_start}", monday))

    def test_other_braces_are_left_alone_and_unknown_dates_are_refused(self) -> None:
        untouched = "awk '{print $1}' ${HOME} {a,b} '{\"k\": 1}' {FOO}"
        self.assertEqual(untouched, store.expand_dates(untouched, ts("2026-10-14T09:00")))
        store.check_dates(untouched, "args")
        for bad in ("{last_monday}", "{days_ago}", "{today:3}", "{days_ago:400}"):
            with self.subTest(bad), self.assertRaises(store.TaskError) as caught:
                store.check_dates(f"--since {bad}", "args")
            self.assertIn("{last_week_start}", str(caught.exception))
        choices = store.date_choices(ts("2026-10-14T09:00"))
        self.assertEqual({"token": "{last_week_start}", "label": "Last week's Monday", "value": "2026-10-05"}, choices[3])
        self.assertEqual("{days_ago:7}", choices[-1]["token"])


class DataStepTests(AiRunTests):
    def data_task(self, body: str, args: str = "", **step):
        script = self.script("data.py", body)
        definition = store.validate_definition(self.ai(provider="codex", cwd=str(self.folder), data_step={
            "script": str(script), "args": args, "interpreter": os.sys.executable, **step}))
        task = store.create_task(definition)
        run_id = store.start_run(task["id"], trigger="manual")
        return definition, runner.execute(task["id"], run_id, definition, settings=None)

    def test_the_agent_gets_the_scripts_output_and_the_report_shows_it_first(self) -> None:
        calls = self.fake_agents({"status": "completed", "provider": "codex",
                                  "result_envelope": {"outcome": "success", "summary": "Spend up 4%."},
                                  "result_full": {"text": "Report"}})
        with patch.dict(os.environ, {"MCP_API_KEY": "must-not-leak"}):
            definition, result = self.data_task(
                "import os, sys, json\nprint(json.dumps({'args': sys.argv[1:], 'key': os.environ.get('MCP_API_KEY', 'absent')}))\n",
                args="--since {last_week_start} --until {last_week_end}")
        self.assertEqual(str(self.script("data.py", "").resolve()).replace("/private", ""),
                         definition["data_step"]["script"].replace("/private", ""))
        self.assertEqual("done", result["status"])
        prompt = calls["spawn"][0]["prompt"]
        self.assertTrue(prompt.startswith("Summarize last week's leads.\n\n---\nData for this run"))
        self.assertIn('"key": "absent"', prompt)  # the script runs with the clean environment
        self.assertIn("<data>\n{", prompt)
        expected = store.expand_dates("{last_week_start}")
        self.assertIn(f'"--since", "{expected}"', prompt)
        first = result["detail"]["steps"][0]
        self.assertEqual(("data", "done", "Got the data from data.py"), (first["kind"], first["status"], first["title"]))
        self.assertIn(f"data.py --since {expected}", first["detail"])
        self.assertEqual(first, result["detail"]["data"])

    def test_a_failing_or_silent_data_step_does_not_start_the_agent(self) -> None:
        calls = self.fake_agents({"status": "completed"})
        cases = {
            "exit": ("import sys\nprint('token expired')\nsys.exit(2)\n", "exited with code 2"),
            "silent": ("pass\n", "printed nothing"),
            "too much": (f"print('x' * {store.DATA_LIMIT + 10})\n", "more than 60,000 characters"),
        }
        for name, (body, reason) in cases.items():
            with self.subTest(name):
                _, result = self.data_task(body)
                self.assertEqual("failed", result["status"])
                self.assertIn(reason, result["reason"])
                self.assertEqual(("data", "failed"), (result["detail"]["data"]["kind"], result["detail"]["data"]["status"]))
                self.assertNotIn("agent_id", result["detail"])
        _, expired = self.data_task("import sys\nprint('token expired')\nsys.exit(2)\n")
        self.assertEqual(("token expired", 2), (expired["summary"], expired["exit_code"]))
        self.assertEqual([], calls["spawn"])
        self.assertEqual([], list((self.dir / "state" / "state" / "scheduled_runs").iterdir()))

    def test_data_step_time_limit(self) -> None:
        self.fake_agents({"status": "completed"})
        with patch.object(store, "DATA_TIMEOUT_MIN", 1 / 60):
            _, result = self.data_task("import time\ntime.sleep(20)\n")
        self.assertEqual("failed", result["status"])
        self.assertIn("was stopped", result["reason"])

    def test_paused_observation_skips_before_the_data_step(self) -> None:
        marker = self.folder / "ran"
        self.fake_agents({"status": "completed"})
        self.privacy(observation_paused=True)
        _, result = self.data_task(f"open({str(marker)!r}, 'w').write('x')\nprint(1)\n")
        self.assertEqual("skipped", result["status"])
        self.assertFalse(marker.exists())


class DataStepStoreTests(StoreCase):
    def test_saved_with_the_task_removed_with_an_empty_step_and_ai_only(self) -> None:
        script = str(self.script("data.sh", "echo 1\n"))
        task = store.create_task(self.ai(data_step={"script": script, "args": "--day {yesterday}"}))
        self.assertEqual({"script": script, "command": None, "args": "--day {yesterday}", "interpreter": None},
                         task["data_step"])
        edited = store.update_task(task["id"], {"data_step": {}})
        self.assertNotIn("data_step", edited)
        command = store.update_task(task["id"], {"data_step": {"command": "echo {today}"}})
        self.assertEqual("echo {today}", command["data_step"]["command"])
        with self.assertRaises(store.TaskError) as caught:
            store.create_task(self.sh(data_step={"command": "echo 1"}))
        self.assertEqual("data_step_ai_only", caught.exception.code)
        with self.assertRaises(store.TaskError) as caught:
            store.create_task(self.ai(data_step={"command": "echo {last_monday}"}))
        self.assertEqual("data_command_date_unknown", caught.exception.code)
        with self.assertRaises(store.TaskError) as caught:
            store.create_task(self.ai(data_step={"script": script, "command": "echo 1"}))
        self.assertEqual("data_script_or_command", caught.exception.code)

    def test_script_tasks_fill_in_dates_too(self) -> None:
        definition = store.validate_definition({"name": "Cmd", "kind": "script", "command": "echo day={yesterday}",
                                                "schedule": {"type": "daily", "time": "09:00"}})
        task = store.create_task(definition)
        result = runner.execute(task["id"], store.start_run(task["id"], trigger="manual"), definition, settings=None)
        self.assertEqual(f"day={store.expand_dates('{yesterday}')}", result["summary"])


class McpToolTests(StoreCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_core_catalog_descriptions import _build_mcp
        cls.mcp = _build_mcp()

    def call(self, **arguments):
        from mcp_server.main import _unwrap_tool_result
        return _unwrap_tool_result(asyncio.run(self.mcp.call_tool("scheduled_tasks", arguments)))

    def test_agents_can_suggest_but_not_run_or_approve(self) -> None:
        created = self.call(action="create", name="Clean Downloads", kind="script", command="echo tidy",
                            schedule={"type": "weekly", "time": "08:00", "weekdays": [5]})
        self.assertEqual(("needs_approval", "needs_approval"), (created["status"], created["task"]["state"]))
        task_id = created["task"]["id"]
        refused = self.call(action="run_now", task_id=task_id)
        self.assertEqual("not_approved", refused["error"])
        self.assertNotIn("approve", self.mcp_actions())
        store.approve(task_id)
        changed = self.call(action="update", task_id=task_id, command="rm -rf ~/Downloads/*")
        self.assertEqual("echo tidy", changed["task"]["command"])  # the approved command still runs
        self.assertEqual("rm -rf ~/Downloads/*", changed["task"]["proposal"]["command"])
        listed = self.call(action="list")
        self.assertEqual([task_id], [t["id"] for t in listed["tasks"]])
        self.assertEqual("action_invalid", self.call(action="approve", task_id=task_id)["error"])

    def test_agents_can_suggest_a_data_step_for_the_owner_to_review(self) -> None:
        created = self.call(action="create", name="Weekly ad report", kind="ai", prompt="Summarize it.",
                            schedule={"type": "weekly", "time": "09:00", "weekdays": [0]},
                            data_step={"command": "python3 report.py --since {last_week_start}"})
        self.assertEqual("needs_approval", created["status"])
        self.assertEqual("python3 report.py --since {last_week_start}", created["task"]["proposal"]["data_step"]["command"])

    def test_agents_can_suggest_output_checks_for_the_owner_to_review(self) -> None:
        created = self.call(action="create", name="Nightly export", kind="script", command="echo hi",
                            folder=str(self.folder), schedule={"type": "daily", "time": "02:00"},
                            expected_files=[{"path": "export.csv", "min_bytes": 100}])
        proposal = created["task"]["proposal"]
        self.assertEqual([{"path": str(self.folder / "export.csv"), "min_bytes": 100, "written_this_run": True}],
                         proposal["verify"])
        store.approve(created["task"]["id"])
        cleared = self.call(action="update", task_id=created["task"]["id"], expected_files=[])
        self.assertNotIn("verify", cleared["task"]["proposal"])

    def test_listing_works_with_intent_descriptions_on(self) -> None:
        # Every tool gains a required "description" (intent) field; a tool argument of that name breaks listing.
        with patch.object(self.mcp, "_intent_descriptions_provider", lambda: True):
            tools = {tool.name: tool for tool in asyncio.run(self.mcp.list_available_tools(compact=False))}
        schema = tools["scheduled_tasks"].inputSchema
        self.assertIn("summary", schema["properties"])
        self.assertIn("description", schema["required"])
        created = self.call(action="create", name="Notes", summary="For Fridays", kind="script", command="echo hi",
                            schedule={"type": "daily", "time": "09:00"})
        self.assertEqual("For Fridays", created["task"]["proposal"]["description"])

    def mcp_actions(self) -> str:
        tool = asyncio.run(self.mcp.list_available_tools(compact=False))
        return next(t.description for t in tool if t.name == "scheduled_tasks").split("action=")[1].split(".")[0]


if __name__ == "__main__":
    unittest.main()
