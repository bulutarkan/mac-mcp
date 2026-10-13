"""Running Scheduled Tasks (#175): one run of a task, and the in-server loop that starts them on time.

The loop sleeps until the next due run and never polls on a fixed beat: it wakes when that
time comes, when a task changes, and at most once a minute while tasks are active (to notice
sleep and clock changes). With no active tasks it does not wake at all.

A run whose time passed more than OVERDUE_S ago (the Mac was asleep, or Mac MCP was not
running) is not run late: it is recorded as Skipped with the reason, and the owner can Run now.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths, privacy_policy, schedule_rules
from . import scheduled_tasks as store

log = logging.getLogger("mac_mcp.scheduled_tasks")

OVERDUE_S = 120.0
MAX_SLEEP_S = 60.0
AGENT_POLL_S = 5.0
KILL_GRACE_S = 5.0
MAX_COUNTED_SKIPS = 500
REASON_ASLEEP = "Your Mac was asleep"
REASON_STOPPED = "Mac MCP wasn't running"
REASON_BUSY = "The previous run was still going"
REASON_PAUSED = "Observation is paused in Settings › Privacy, so AI tasks don't run"
REASON_RESTARTED = "Mac MCP restarted before the run finished"


# ── One run ──────────────────────────────────────────────────────────────────

def _clean_env() -> Dict[str, str]:
    """What a script sees: the usual login basics, none of the server's keys or settings."""
    home = Path.home()
    user = os.getenv("USER") or home.name
    path = ":".join([str(home / ".local" / "bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"HOME": str(home), "USER": user, "LOGNAME": user, "SHELL": "/bin/zsh", "PATH": path,
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TMPDIR": os.getenv("TMPDIR", "/tmp"),
           "MAC_MCP_SCHEDULED_TASK": "1"}
    return env


def script_command(definition: Dict[str, Any]) -> Tuple[List[str], str]:
    """The argv and folder a script task runs with (also shown on the Review sheet)."""
    if definition.get("command"):
        return ["/bin/zsh", "-lc", definition["command"]], definition.get("cwd") or str(Path.home())
    info = store.inspect_script(definition["script"], definition.get("interpreter"))
    if not info["ok"]:
        raise store.TaskError(info["error"], info["message"])
    args = shlex.split(definition.get("args") or "")
    argv = ([info["program"]] if info.get("program") else []) + [info["path"], *args]
    return argv, definition.get("cwd") or info["folder"]


def _limit_s(definition: Dict[str, Any]) -> int:
    return int(definition.get("timeout_min") or store.DEFAULT_TIMEOUT_MIN[definition["kind"]]) * 60


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - store.OUTPUT_LIMIT * 2))
            return handle.read().decode("utf-8", errors="replace")[-store.OUTPUT_LIMIT:]
    except OSError:
        return ""


def _last_line(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()[:300]
    return ""


def run_script(definition: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """Run a script task to the end (or its time limit) and say how it went."""
    try:
        argv, cwd = script_command(definition)
    except store.TaskError as exc:
        return {"status": "failed", "reason": str(exc)}
    logs = paths.state_dir() / "state" / "scheduled_runs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run_id}.log"
    limit = _limit_s(definition)
    try:
        with log_path.open("wb") as out:
            os.chmod(log_path, 0o600)
            try:
                proc = subprocess.Popen(argv, cwd=cwd, env=_clean_env(), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                return {"status": "failed", "reason": f"It could not start: {exc.strerror or exc}"}
            timed_out = False
            try:
                code = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        proc.wait(timeout=KILL_GRACE_S)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                code = proc.returncode
        output = _tail(log_path)
    finally:
        log_path.unlink(missing_ok=True)
    last = _last_line(output)
    if timed_out:
        minutes = int(definition.get("timeout_min") or store.DEFAULT_TIMEOUT_MIN["script"])
        return {"status": "timed_out", "reason": f"Stopped at its {minutes}-minute time limit",
                "summary": last, "output": output, "exit_code": code}
    if code == 0:
        return {"status": "done", "summary": last or "Finished without output", "output": output, "exit_code": 0}
    reason = f"Exited with code {code}" if code and code > 0 else f"Stopped by signal {-(code or 0)}"
    return {"status": "failed", "reason": reason, "summary": last, "output": output, "exit_code": code}


def run_ai(definition: Dict[str, Any], run_id: str, settings: Any,
           on_agent: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Start a delegated agent with the task's prompt and wait for its result."""
    from fastapi import HTTPException

    from . import tools_agents
    from .policy import environment_policy_context, reset_policy_context, set_policy_context

    limit = _limit_s(definition)
    token = set_policy_context(environment_policy_context(actor="scheduled_task"))
    try:
        spawned = tools_agents.spawn_agent(
            settings, definition.get("provider"), definition["prompt"], model=definition.get("model"),
            cwd=definition.get("cwd") or str(Path.home()), timeout_s=limit,
            title=f"Scheduled: {definition['name']}"[:120], result_style="detailed",
        )
    except HTTPException as exc:
        detail = re.sub(r"^[a-z_]+: ", "", str(exc.detail))  # "default_agent_not_configured: choose ..." → the sentence
        return {"status": "failed", "reason": f"The agent could not start: {detail}"}
    except Exception as exc:  # a provider problem must not take the scheduler down
        log.exception("scheduled AI task could not start")
        return {"status": "failed", "reason": f"The agent could not start: {exc}"}
    finally:
        reset_policy_context(token)
    agent_id = str(spawned.get("agent_id") or "")
    if not agent_id:
        return {"status": "failed", "reason": "The agent could not start"}
    if on_agent:
        on_agent(agent_id)
    return wait_for_agent(agent_id, settings, deadline=time.time() + limit + 180)


def wait_for_agent(agent_id: str, settings: Any, *, deadline: float) -> Dict[str, Any]:
    from fastapi import HTTPException

    from . import tools_agents

    while True:
        try:
            state = tools_agents.get_agent(settings, agent_id, result_mode="full")
        except HTTPException as exc:
            return {"status": "failed", "reason": f"The agent's record could not be read: {exc.detail}",
                    "detail": {"agent_id": agent_id}}
        status = state.get("status")
        if status in tools_agents.TERMINAL_STATUSES:
            break
        if time.time() > deadline:
            try:
                tools_agents.agent_action(settings, action="cancel", agent_id=agent_id, requested_by="user")
            except Exception:
                pass
            return {"status": "timed_out", "reason": "The agent ran past the time limit and was stopped",
                    "detail": {"agent_id": agent_id}}
        time.sleep(AGENT_POLL_S)
    envelope = state.get("result_envelope") or {}
    report = str((state.get("result_full") or {}).get("text") or state.get("result") or "")
    detail = {
        "agent_id": agent_id, "provider": state.get("provider"),
        "model": state.get("effective_model") or state.get("model"),
        "outcome": envelope.get("outcome"),
        "artifacts": [{"ref": item.get("ref"), "description": item.get("description")}
                      for item in (envelope.get("artifacts") or [])[:20] if isinstance(item, dict)],
    }
    summary = str(envelope.get("summary") or _last_line(report) or "")
    if status == "completed" and envelope.get("outcome") != "failure":
        return {"status": "done", "summary": summary or "Finished", "output": report, "detail": detail}
    if status == "timeout":
        return {"status": "timed_out", "reason": "The agent ran past the time limit and was stopped",
                "summary": summary, "output": report, "detail": detail}
    reason = str(state.get("error") or state.get("failure_reason") or "") or {
        "cancelled": "The agent was stopped", "stalled": "The agent stopped responding"}.get(status, "The agent failed")
    return {"status": "failed", "reason": reason[:300], "summary": summary, "output": report, "detail": detail}


def execute(task_id: str, run_id: str, definition: Dict[str, Any], settings: Any) -> Dict[str, Any]:
    """Run one task and record the result, honoring Settings › Privacy."""
    privacy = privacy_policy.policy(fresh=True)
    try:
        if definition["kind"] == "ai":
            if privacy["paused"]:
                result = {"status": "skipped", "reason": REASON_PAUSED}
            else:
                result = run_ai(definition, run_id, settings, on_agent=lambda agent_id: store.note_agent(run_id, agent_id))
        else:
            result = run_script(definition, run_id)
    except Exception as exc:
        log.exception("scheduled task %s failed unexpectedly", task_id)
        result = {"status": "failed", "reason": f"Mac MCP hit an error running it: {exc}"}
    if privacy["private_mode"]:
        # Private mode: keep that it ran and how it ended, not what it printed or said.
        result = {key: result.get(key) for key in ("status", "reason", "exit_code")}
        result["detail"] = {"private_mode": True}
    return store.finish_run(run_id, result["status"], reason=result.get("reason"), summary=result.get("summary"),
                            output=result.get("output"), exit_code=result.get("exit_code"), detail=result.get("detail"))


# ── The loop ─────────────────────────────────────────────────────────────────

class Scheduler:
    def __init__(self, settings: Any, *, clock: Callable[[], float] = time.time,
                 launch: Optional[Callable[[str, str, Dict[str, Any]], None]] = None):
        self.settings = settings
        self.clock = clock
        self.started_at = clock()
        self._launch = launch or self._launch_thread
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.wakeups = 0

    # Decisions (pure enough to test with a fake clock and launcher)

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = self.clock() if now is None else now
        actions: List[Dict[str, Any]] = []
        for task in store.due_tasks(now):
            definition = task["definition"]
            due = float(task["next_run_at"])
            slots = [due] + schedule_rules.runs_between(definition["schedule"], due, now, limit=MAX_COUNTED_SKIPS)
            missed = [slot for slot in slots if now - slot > OVERDUE_S]
            fresh = [slot for slot in slots if now - slot <= OVERDUE_S]
            if missed:
                reason = REASON_STOPPED if missed[0] < self.started_at else REASON_ASLEEP
                store.record_skip(task["id"], scheduled_for=missed[0], reason=reason, count=len(missed), now=now)
                actions.append({"task_id": task["id"], "action": "skipped", "count": len(missed), "reason": reason})
            if fresh:
                actions.append(self.start(task["id"], definition, trigger="schedule", scheduled_for=fresh[-1], now=now))
            store.advance(task["id"], now)
        return actions

    def start(self, task_id: str, definition: Dict[str, Any], *, trigger: str,
              scheduled_for: Optional[float] = None, now: Optional[float] = None) -> Dict[str, Any]:
        run_id = store.start_run(task_id, trigger=trigger, scheduled_for=scheduled_for, now=now)
        if run_id is None:
            if trigger == "schedule":
                store.record_skip(task_id, scheduled_for=scheduled_for or (now or self.clock()), reason=REASON_BUSY, now=now)
            return {"task_id": task_id, "action": "busy"}
        self._launch(task_id, run_id, definition)
        return {"task_id": task_id, "action": "started", "run_id": run_id}

    def run_now(self, task_id: str) -> Dict[str, Any]:
        definition = store.definition_of(task_id)
        if definition is None:
            raise store.TaskError("not_approved", "This task is waiting for your approval.", 409)
        result = self.start(task_id, definition, trigger="manual")
        if result["action"] == "busy":
            raise store.TaskError("already_running", "This task is already running.", 409)
        return result

    def _launch_thread(self, task_id: str, run_id: str, definition: Dict[str, Any]) -> None:
        threading.Thread(target=execute, args=(task_id, run_id, definition, self.settings),
                         name=f"scheduled-task-{run_id}", daemon=True).start()

    def recover(self) -> None:
        """After a restart: keep following agents that are still working; close the other open runs."""
        for run in store.running_runs():
            agent_id = run.get("agent_id")
            if agent_id:
                definition = store.definition_of(run["task_id"]) or {}
                limit = int(definition.get("timeout_min") or store.DEFAULT_TIMEOUT_MIN["ai"]) * 60
                threading.Thread(target=self._follow_agent, args=(run["id"], agent_id, run["started_at"] + limit + 180),
                                 name=f"scheduled-task-{run['id']}", daemon=True).start()
            else:
                store.finish_run(run["id"], "failed", reason=REASON_RESTARTED)

    def _follow_agent(self, run_id: str, agent_id: str, deadline: float) -> None:
        result = wait_for_agent(agent_id, self.settings, deadline=deadline)
        if privacy_policy.policy(fresh=True)["private_mode"]:
            result = {key: result.get(key) for key in ("status", "reason")}
        store.finish_run(run_id, result["status"], reason=result.get("reason"), summary=result.get("summary"),
                         output=result.get("output"), detail=result.get("detail"))

    # The thread

    def sleep_for(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds until the next wake; None means wait for a change (no active tasks)."""
        now = self.clock() if now is None else now
        earliest = store.earliest_next_run()
        if earliest is None:
            return None
        return max(0.5, min(MAX_SLEEP_S, earliest - now))

    def _loop(self) -> None:
        try:
            self.recover()
        except Exception:
            log.exception("scheduled tasks: recovery failed")
        while not self._stop.is_set():
            self.wakeups += 1
            try:
                self.tick()
                delay = self.sleep_for()
            except Exception:
                log.exception("scheduled tasks: tick failed")
                delay = MAX_SLEEP_S
            self._wake.wait(delay)
            self._wake.clear()

    def poke(self) -> None:
        self._wake.set()

    def begin(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        store.on_change(self.poke)
        self._thread = threading.Thread(target=self._loop, name="mac-mcp-scheduled-tasks", daemon=True)
        self._thread.start()

    def end(self) -> None:
        self._stop.set()
        self._wake.set()


_scheduler: Optional[Scheduler] = None


def start(settings: Any) -> Scheduler:
    """Start the loop once per server process."""
    loop = scheduler(settings)
    loop.begin()
    return loop


def scheduler(settings: Any = None) -> Scheduler:
    """The running loop, or one that only acts on request (tests, servers without the loop)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(settings)
    elif settings is not None and _scheduler.settings is None:
        _scheduler.settings = settings
    return _scheduler
