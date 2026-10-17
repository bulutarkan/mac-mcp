"""Running Scheduled Tasks (#175): one run of a task, and the in-server loop that starts them on time.

The loop sleeps until the next due run and never polls on a fixed beat: it wakes when that
time comes, when a task changes, and at most once a minute while tasks are active (to notice
sleep and clock changes). With no active tasks it does not wake at all.

An AI task with a data step runs its script first (like a script task, for at most
DATA_TIMEOUT_MIN minutes) and gives the agent what it printed. If the script fails or prints
nothing, the agent is not started and the run says why.

A run whose time passed more than OVERDUE_S ago (the Mac was asleep, or Mac MCP was not
running) is not run late: it is recorded as Skipped with the reason, and the owner can Run now.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import folder_triggers, paths, privacy_policy, run_budget, schedule_rules
from . import scheduled_tasks as store

log = logging.getLogger("mac_mcp.scheduled_tasks")

OVERDUE_S = 120.0
MAX_SLEEP_S = 60.0
AGENT_POLL_S = 5.0
# With a usage limit the agent's usage is checked this often, so it stops soon after the limit.
LIMIT_POLL_S = 2.0
LIMIT_STOP_WAIT_S = 30.0
# After the time limit the agent is stopped and given this long to write out what it did (#226).
CANCEL_SETTLE_S = 10.0
KILL_GRACE_S = 5.0
MAX_COUNTED_SKIPS = 500
REASON_ASLEEP = "Your Mac was asleep"
REASON_STOPPED = "Mac MCP wasn't running"
REASON_BUSY = "The previous run was still going"
REASON_PAUSED = "Observation is paused in Settings › Privacy, so AI tasks don't run"
REASON_RESTARTED = "Mac MCP restarted before the run finished"


# ── One run ──────────────────────────────────────────────────────────────────

def _clean_env(trigger_file: Optional[str] = None) -> Dict[str, str]:
    """What a script sees: the usual login basics, none of the server's keys or settings."""
    home = Path.home()
    user = os.getenv("USER") or home.name
    path = ":".join([str(home / ".local" / "bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"HOME": str(home), "USER": user, "LOGNAME": user, "SHELL": "/bin/zsh", "PATH": path,
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TMPDIR": os.getenv("TMPDIR", "/tmp"),
           "MAC_MCP_SCHEDULED_TASK": "1"}
    if trigger_file:
        env["MAC_MCP_TRIGGER_FILE"] = trigger_file  # the file that started this run (#193)
    return env


def script_command(definition: Dict[str, Any], at: Optional[float] = None) -> Tuple[List[str], str]:
    """The argv and folder a script (or a data step) runs with, its dates filled in for a run at `at`."""
    if definition.get("command"):
        return ["/bin/zsh", "-lc", store.expand_dates(definition["command"], at)], definition.get("cwd") or str(Path.home())
    info = store.inspect_script(definition["script"], definition.get("interpreter"))
    if not info["ok"]:
        raise store.TaskError(info["error"], info["message"])
    args = shlex.split(store.expand_dates(definition.get("args") or "", at))
    argv = ([info["program"]] if info.get("program") else []) + [info["path"], *args]
    return argv, definition.get("cwd") or info["folder"]


def describe_program(definition: Dict[str, Any], at: Optional[float] = None) -> str:
    """The script or command as the owner reads it, e.g. "report.py --since 2026-10-06"."""
    if definition.get("command"):
        return store.expand_dates(definition["command"], at)
    args = store.expand_dates(definition.get("args") or "", at)
    return " ".join(part for part in (Path(definition.get("script") or "").name, args) if part)


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


def _head(path: Path, limit: int) -> Tuple[str, int]:
    """The first `limit` characters of a log and its full size in bytes."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            return handle.read(limit * 4).decode("utf-8", errors="replace")[:limit], size
    except OSError:
        return "", 0


def _run_logged(argv: List[str], cwd: str, limit_s: int, log_path: Path,
                read: Callable[[Path], Any], trigger_file: Optional[str] = None) -> Dict[str, Any]:
    """Run argv with the clean environment, its output in an owner-only log that is read, then removed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as out:
            os.chmod(log_path, 0o600)
            try:
                proc = subprocess.Popen(argv, cwd=cwd, env=_clean_env(trigger_file), stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                return {"error": str(exc.strerror or exc)}
            timed_out = False
            try:
                code = proc.wait(timeout=limit_s)
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
        return {"code": code, "timed_out": timed_out, "read": read(log_path)}
    finally:
        log_path.unlink(missing_ok=True)


def _logs_dir() -> Path:
    return paths.state_dir() / "state" / "scheduled_runs"


def run_script(definition: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """Run a script task to the end (or its time limit) and say how it went."""
    try:
        argv, cwd = script_command(definition)
    except store.TaskError as exc:
        return {"status": "failed", "reason": str(exc)}
    ran = _run_logged(argv, cwd, _limit_s(definition), _logs_dir() / f"{run_id}.log", _tail,
                      definition.get("trigger_file"))
    if "error" in ran:
        return {"status": "failed", "reason": f"It could not start: {ran['error']}"}
    code, output = ran["code"], ran["read"]
    last = _last_line(output)
    if ran["timed_out"]:
        minutes = int(definition.get("timeout_min") or store.DEFAULT_TIMEOUT_MIN["script"])
        return {"status": "timed_out", "reason": f"Stopped at its {minutes}-minute time limit",
                "summary": last, "output": output, "exit_code": code}
    if code == 0:
        return {"status": "done", "summary": last or "Finished without output", "output": output, "exit_code": 0}
    reason = f"Exited with code {code}" if code and code > 0 else f"Stopped by signal {-(code or 0)}"
    return {"status": "failed", "reason": reason, "summary": last, "output": output, "exit_code": code}


def run_data_step(definition: Dict[str, Any], run_id: str, at: Optional[float] = None) -> Dict[str, Any]:
    """Run an AI task's data step. ok=True carries what the script printed, for the agent."""
    step = definition["data_step"]
    at = time.time() if at is None else at
    shown = describe_program(step, at)
    label = Path(step["script"]).name if step.get("script") else "the command"
    started = time.time()
    try:
        argv, cwd = script_command(step, at)
    except store.TaskError as exc:
        return {"ok": False, "reason": f"The data step could not run: {exc}", "label": label, "shown": shown}
    ran = _run_logged(argv, cwd, store.DATA_TIMEOUT_MIN * 60, _logs_dir() / f"{run_id}.data.log",
                      lambda path: _head(path, store.DATA_LIMIT), definition.get("trigger_file"))
    info: Dict[str, Any] = {"label": label, "shown": shown, "duration_s": round(time.time() - started, 1)}
    if "error" in ran:
        return {**info, "ok": False, "reason": f"The data step could not start: {ran['error']}"}
    text, size = ran["read"]
    info.update({"exit_code": ran["code"], "output": text})
    if ran["timed_out"]:
        return {**info, "ok": False, "reason": f"The data step ran past {store.DATA_TIMEOUT_MIN} minutes and was stopped, "
                                               "so the agent was not started"}
    if ran["code"] != 0:
        how = f"exited with code {ran['code']}" if ran["code"] and ran["code"] > 0 else f"was stopped by signal {-(ran['code'] or 0)}"
        return {**info, "ok": False, "reason": f"The data step {how}, so the agent was not started"}
    if not text.strip():
        return {**info, "ok": False, "reason": "The data step printed nothing, so the agent was not started"}
    if len(text) >= store.DATA_LIMIT and size > len(text.encode("utf-8")):
        return {**info, "ok": False, "reason": f"The data step printed more than {store.DATA_LIMIT:,} characters, "
                                               "too much to hand to the agent. Have the script print a summary, or "
                                               "write the full data to a file and print its path."}
    return {**info, "ok": True}


def data_prompt(prompt: str, data: Dict[str, Any], at: float) -> str:
    """The owner's prompt with the data step's output after it."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
    return (f"{prompt}\n\n---\nData for this run, printed just now ({when}) by `{data['shown']}`. "
            "Take numbers and facts from it; do not invent values it does not contain. If it looks incomplete "
            "or wrong, say so in your report.\n\n<data>\n" + data["output"].strip() + "\n</data>\n")


def _data_step_view(data: Dict[str, Any]) -> Dict[str, Any]:
    """The data step as shown in the run report: first in the steps, with what it printed."""
    return {"kind": "data", "status": "done" if data.get("ok") else "failed",
            "title": f"Got the data from {data['label']}" if data.get("ok") else "The data step failed",
            "detail": data["shown"], "output": (data.get("output") or "")[: store.OUTPUT_LIMIT],
            "duration_s": data.get("duration_s"), "exit_code": data.get("exit_code")}


def run_ai(definition: Dict[str, Any], run_id: str, settings: Any,
           on_agent: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Start a delegated agent with the task's prompt (and its data step's output) and wait for its result."""
    prompt = definition["prompt"]
    data: Optional[Dict[str, Any]] = None
    if definition.get("data_step"):
        at = time.time()
        data = run_data_step(definition, run_id, at)
        if not data["ok"]:
            return {"status": "failed", "reason": data["reason"], "summary": _last_line(data.get("output") or ""),
                    "output": data.get("output") or "", "exit_code": data.get("exit_code"),
                    "detail": {"data": _data_step_view(data)}}
        prompt = data_prompt(prompt, data, at)
    result = _start_agent(definition, prompt, settings, on_agent)
    if data is not None:
        detail = result.setdefault("detail", {})
        detail["data"] = _data_step_view(data)
        detail["steps"] = [detail["data"], *(detail.get("steps") or [])]
    return result


def _start_agent(definition: Dict[str, Any], prompt: str, settings: Any,
                 on_agent: Optional[Callable[[str], None]]) -> Dict[str, Any]:
    from fastapi import HTTPException

    from . import tools_agents
    from .policy import PROFILES, environment_policy_context, reset_policy_context, set_policy_context
    from .policy_scope import access_mode_allows

    limit = _limit_s(definition)
    usage_limit = definition.get("usage_limit")
    provider = definition.get("provider") or (run_budget.default_provider() if usage_limit else None)
    problem = run_budget.unenforceable(usage_limit, provider, store.provider_label(provider))
    if problem:  # e.g. the Default Agent changed since the owner set the limit
        return {"status": "failed", "reason": f"{problem} The agent was not started."}
    context = environment_policy_context(actor="scheduled_task")
    token = set_policy_context(context)
    try:
        # The global permission profile caps scheduled agents like any other: under a read-only
        # ceiling (Standard) the agent runs read-only instead of being refused.
        profile = PROFILES.get(context.profile)
        access = "workspace_write"
        if profile is not None and not access_mode_allows(profile.access_mode_ceiling, access):
            access = "read_only"
        spawned = tools_agents.spawn_agent(
            settings, definition.get("provider"), prompt, model=definition.get("model"),
            cwd=definition.get("cwd") or str(Path.home()), timeout_s=limit,
            title=f"Scheduled: {definition['name']}"[:120], result_style="detailed",
            access_mode=access, parent_scope=context.scope, parent_profile=context.profile,
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
    return wait_for_agent(agent_id, settings, deadline=time.time() + limit + 180,
                          usage_limit=usage_limit, provider=provider)


def _cancel(settings: Any, agent_id: str) -> None:
    from . import tools_agents

    try:
        tools_agents.agent_action(settings, action="cancel", agent_id=agent_id, requested_by="user")
    except Exception:
        pass


def wait_for_agent(agent_id: str, settings: Any, *, deadline: float,
                   usage_limit: Optional[Dict[str, Any]] = None, provider: Optional[str] = None) -> Dict[str, Any]:
    from fastapi import HTTPException

    from . import tools_agents

    meter = run_budget.Meter(tools_agents._agent_dir(agent_id) / "stdout.log", provider or "") if usage_limit else None
    stopped: Optional[str] = None
    stop_by = 0.0
    deadline_hit = False
    while True:
        try:
            state = tools_agents.get_agent(settings, agent_id, result_mode="full")
        except HTTPException as exc:
            return {"status": "failed", "reason": f"The agent's record could not be read: {exc.detail}",
                    "detail": {"agent_id": agent_id}}
        status = state.get("status")
        if status in tools_agents.TERMINAL_STATUSES:
            break
        if meter is not None and stopped is None:
            stopped = run_budget.over(usage_limit, meter.read())
            if stopped:
                _cancel(settings, agent_id)
                stop_by = time.time() + LIMIT_STOP_WAIT_S
        if (stopped or deadline_hit) and time.time() > stop_by:
            break  # asked to stop and still reporting running: the run ends here all the same
        if time.time() > deadline and not deadline_hit:
            # Stopped, then read like any other end: the steps and usage seen so far are kept (#226).
            _cancel(settings, agent_id)
            deadline_hit = True
            stop_by = time.time() + CANCEL_SETTLE_S
        time.sleep(min(AGENT_POLL_S, LIMIT_POLL_S) if meter is not None else AGENT_POLL_S)
    envelope = state.get("result_envelope") or {}
    report = str((state.get("result_full") or {}).get("text") or state.get("result") or "")
    # The machine-readable result block the agent appends is for Mac MCP, not the owner's report.
    report = report.split(tools_agents.RESULT_ENVELOPE_MARKER, 1)[0].strip()
    detail = {
        "agent_id": agent_id, "provider": state.get("provider"),
        "model": state.get("effective_model") or state.get("model"),
        "outcome": envelope.get("outcome"),
        "artifacts": [{"ref": item.get("ref"), "description": item.get("description")}
                      for item in (envelope.get("artifacts") or [])[:20] if isinstance(item, dict)],
    }
    detail.update(_agent_steps(agent_id, report))
    summary = str(envelope.get("summary") or _last_line(report) or "")
    finished = status == "completed" and envelope.get("outcome") != "failure" and not stopped
    if not finished:
        detail["evidence"] = _partial_evidence(agent_id, detail, provider or state.get("provider"))
    if deadline_hit and status != "completed":
        return {"status": "timed_out", "reason": "The agent ran past the time limit and was stopped",
                "summary": summary, "output": report, "detail": detail}
    if meter is not None:
        used = meter.read()
        detail["usage_limit"] = {**usage_limit, "used_tokens": used["tokens"], "used_cost_usd": used["cost_usd"],
                                 "stopped": stopped}
        if stopped:
            reason = run_budget.stop_reason(usage_limit, stopped, used, store.provider_label(provider))
            return {"status": "failed", "reason": reason,
                    "summary": summary, "output": report, "detail": detail}
    if status == "completed" and envelope.get("outcome") != "failure":
        return {"status": "done", "summary": summary or "Finished", "output": report, "detail": detail}
    if status == "timeout":
        return {"status": "timed_out", "reason": "The agent ran past the time limit and was stopped",
                "summary": summary, "output": report, "detail": detail}
    reason = str(state.get("error") or state.get("failure_reason") or "") or _reported_problem(detail, report) or {
        "cancelled": "The agent was stopped", "stalled": "The agent stopped responding"}.get(status, "The agent failed")
    return {"status": "failed", "reason": reason[:300], "summary": summary, "output": report, "detail": detail}


MAX_STEPS_BYTES = 400_000


def _partial_evidence(agent_id: str, detail: Dict[str, Any], provider: Optional[str]) -> Dict[str, Any]:
    """What a run that did not finish can and cannot show: never 'no usage' or 'nothing changed'."""
    from . import tools_agents

    usage = detail.get("usage") or {}
    observed = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    if not observed and provider:
        # Some agents total their usage only at the end; count what was reported along the way.
        observed = int(run_budget.Meter(tools_agents._agent_dir(agent_id) / "stdout.log", provider).read()["tokens"])
    evidence: Dict[str, Any] = {"steps": "partial", "usage": "partial" if observed else "unavailable"}
    if observed:
        evidence["observed_tokens"] = observed
    evidence["external_effects"] = "unknown"  # a step may have changed something before the stop
    return evidence


def _reported_problem(detail: Dict[str, Any], report: str) -> Optional[str]:
    """What the agent or its provider said went wrong, instead of a bare 'The agent failed'."""
    from . import agent_timeline

    reason = agent_timeline.failure_reason(detail.get("steps") or [])
    if reason:
        return reason
    for line in report.splitlines():
        line = line.strip()
        if line.lower().startswith("error:"):
            return line[6:].strip()[:300] or None
    return None


def _agent_steps(agent_id: str, report: str) -> Dict[str, Any]:
    """The agent's steps, kept with the run so the report outlives the agent's own files."""
    from . import agent_timeline, tools_agents

    timeline = agent_timeline.for_agent(tools_agents._agent_dir(agent_id))
    steps = timeline["steps"]
    final = report.strip()
    if steps and steps[-1].get("kind") == "message" and final and str(steps[-1].get("text") or "").strip()[:200] == final[:200]:
        steps = steps[:-1]  # the closing message is the report shown above the steps
    while steps and len(json.dumps(steps)) > MAX_STEPS_BYTES:
        for step in steps:
            step.pop("output", None)
        if len(json.dumps(steps)) > MAX_STEPS_BYTES:
            steps = steps[: len(steps) // 2]
    return {"steps": steps, "usage": timeline["usage"]}


# ── Output checks (#198) ─────────────────────────────────────────────────────

# File times can be a little behind the clock that stamped the run's start.
_MTIME_SLACK_S = 2.0


def check_outputs(checks: List[Dict[str, Any]], started_at: float, now: Optional[float] = None) -> Dict[str, Any]:
    """The owner's expected files, checked by metadata only: never opened, links not followed."""
    from . import run_postconditions

    results = []
    for check in checks:
        if check.get("kind") in {"url", "tool"}:
            results.append(run_postconditions.run_check(check, lambda text: store.expand_dates(text, now)))
            continue
        entry: Dict[str, Any] = {"path": check["path"], "min_bytes": check["min_bytes"],
                                 "written_this_run": check["written_this_run"], "ok": False}
        try:
            info = os.lstat(check["path"])
        except FileNotFoundError:
            problem: Optional[str] = "missing"
        except OSError:
            problem = "unreadable"
        else:
            if stat.S_ISLNK(info.st_mode):
                problem = "link"
            elif not stat.S_ISREG(info.st_mode):
                problem = "not_a_file"
            else:
                entry["size"] = info.st_size
                entry["modified_at"] = info.st_mtime
                if info.st_size < check["min_bytes"]:
                    problem = "too_small"
                elif check["written_this_run"] and info.st_mtime < started_at - _MTIME_SLACK_S:
                    problem = "not_written_this_run"
                else:
                    problem = None
        if problem:
            entry["problem"] = problem
        else:
            entry["ok"] = True
        results.append(entry)
    # A check that could not look (unreachable, server not connected) leaves the run unverified, never
    # verified; a check that looked and did not find what it should fails it.
    if all(item["ok"] for item in results):
        state = "verified"
    elif any(not item["ok"] and not item.get("unknown") for item in results):
        state = "failed"
    else:
        state = "unverified"
    return {"state": state, "checks": results, "checked_at": time.time() if now is None else now}


def _verification(run_id: str, definition: Dict[str, Any], status: str, private: bool) -> Optional[Dict[str, Any]]:
    # Only a run that finished can be checked: a check never turns a failure into a success.
    if status != "done" or not definition.get("verify"):
        return None
    try:
        started_at = float(store.get_run(run_id)["started_at"])
        verification = check_outputs(definition["verify"], started_at)
    except Exception:
        log.exception("output checks for run %s could not run", run_id)
        verification = {"state": "failed", "checks": [], "error": "The checks could not run."}
    return {"state": verification["state"]} if private else verification


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
    verification = _verification(run_id, definition, result["status"], privacy["private_mode"])
    return store.finish_run(run_id, result["status"], reason=result.get("reason"), summary=result.get("summary"),
                            output=result.get("output"), exit_code=result.get("exit_code"), detail=result.get("detail"),
                            verification=verification)


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
        self.last_wake_at: Optional[float] = None
        self.folders = folder_triggers.FolderTriggers(self._start_for_file, clock=clock)

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
        trigger_file = definition.get("trigger_file")
        if trigger_file and privacy_policy.policy(fresh=True)["private_mode"]:
            trigger_file = None  # Private mode keeps that it ran, not which file started it
        run_id = store.start_run(task_id, trigger=trigger, scheduled_for=scheduled_for, now=now,
                                 trigger_file=trigger_file)
        if run_id is None:
            if trigger == "schedule":
                store.record_skip(task_id, scheduled_for=scheduled_for or (now or self.clock()), reason=REASON_BUSY, now=now)
            return {"task_id": task_id, "action": "busy"}
        self._launch(task_id, run_id, definition)
        return {"task_id": task_id, "action": "started", "run_id": run_id}

    def _start_for_file(self, task_id: str, definition: Dict[str, Any], path: str) -> str:
        return self.start(task_id, folder_triggers.with_file(definition, path), trigger="file")["action"]

    def run_now(self, task_id: str) -> Dict[str, Any]:
        definition = store.definition_of(task_id)
        if definition is None:
            raise store.TaskError("not_approved", "This task is waiting for your approval.", 409)
        if definition["schedule"]["type"] == "folder":
            # Run now on a task that waits for files: with the newest file there.
            path = folder_triggers.newest_file(definition["schedule"])
            if path is None:
                raise store.TaskError("no_file", "There is no file in the folder to run it with yet.", 409)
            definition = folder_triggers.with_file(definition, path)
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
                threading.Thread(target=self._follow_agent, args=(run["id"], agent_id, run["started_at"] + limit + 180,
                                                                   definition),
                                 name=f"scheduled-task-{run['id']}", daemon=True).start()
            else:
                store.finish_run(run["id"], "failed", reason=REASON_RESTARTED)

    def _follow_agent(self, run_id: str, agent_id: str, deadline: float,
                      definition: Optional[Dict[str, Any]] = None) -> None:
        definition = definition or {}
        usage_limit = definition.get("usage_limit")
        provider = definition.get("provider") or (run_budget.default_provider() if usage_limit else None)
        result = wait_for_agent(agent_id, self.settings, deadline=deadline, usage_limit=usage_limit, provider=provider)
        private = privacy_policy.policy(fresh=True)["private_mode"]
        if private:
            result = {key: result.get(key) for key in ("status", "reason")}
            result["detail"] = {"private_mode": True}  # as execute() marks it, so the report says why
        task_id = store.get_run(run_id)["task_id"]
        verification = _verification(run_id, store.definition_of(task_id) or {}, result["status"], private)
        store.finish_run(run_id, result["status"], reason=result.get("reason"), summary=result.get("summary"),
                         output=result.get("output"), detail=result.get("detail"), verification=verification)

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
            self.last_wake_at = self.clock()
            try:
                store.apply_retention()  # also on start: contents expire with no run finishing (#199)
                self.tick()
                delay = self.sleep_for()
            except Exception:
                log.exception("scheduled tasks: tick failed")
                delay = MAX_SLEEP_S
            self._wake.wait(delay)
            self._wake.clear()

    def poke(self) -> None:
        self._wake.set()

    def liveness(self) -> Dict[str, Any]:
        """Whether the loop thread is alive and when it last woke (#225), for observers outside it."""
        return {"running": bool(self._thread and self._thread.is_alive()), "last_wake_at": self.last_wake_at}

    def begin(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        store.on_change(self.poke)
        store.on_change(self.folders.poke)
        self._thread = threading.Thread(target=self._loop, name="mac-mcp-scheduled-tasks", daemon=True)
        self._thread.start()
        self.folders.begin()

    def end(self) -> None:
        self._stop.set()
        self._wake.set()
        self.folders.end()


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
