"""Scheduled Tasks (#175): the tasks the owner (or an agent, with approval) set up to run on their own.

A task is an AI task (a prompt for a delegated agent) or a script task (a file, or a
command line). Its definition is one JSON object:

    {"name", "description", "kind": "ai"|"script", "schedule": {...schedule_rules...},
     "notify": bool, "timeout_min": int,
     # ai:     "prompt", "provider", "model", "cwd",
     #         "data_step": {"script" or "command", "interpreter", "args"} (optional),
     #         "usage_limit": {"max_tokens", "max_cost_usd"} (optional, run_budget)
     # script: "script" (a file) or "command", "interpreter", "args", "cwd"}

An AI task's data step runs a script first and hands what it printed to the agent, so the
numbers come from the owner's script (with the owner's credentials) and the agent only reads
them. Arguments and commands can name dates relative to the run, such as {last_week_start}.

The owner's edits apply at once. An agent's create or edit is kept as a *proposal* next to
the approved definition: the task keeps running what the owner approved (or, for a new
task, never runs) until the owner approves the proposal in Settings. Agents cannot approve.

Storage is one owner-only SQLite file (paths.scheduled_tasks_db). Each task keeps its last
RUNS_PER_TASK runs, none older than RUN_RETENTION_DAYS.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import paths, run_budget, run_records, schedule_rules
from .runtime_settings import run_detail_privacy

KINDS = ("ai", "script")
RUN_STATUSES = ("running", "done", "failed", "skipped", "timed_out")
RUNS_PER_TASK = 50
RUN_RETENTION_DAYS = 90
MAX_TASKS = 200
DEFAULT_TIMEOUT_MIN = {"ai": 30, "script": 10}
MAX_TIMEOUT_MIN = 6 * 60
OUTPUT_LIMIT = 20_000
SUMMARY_LIMIT = 4_000
MAX_OUTPUT_CHECKS = 5
DATA_LIMIT = 60_000
DATA_TIMEOUT_MIN = 10
# Folder triggers (#193): how many tasks may watch a folder, and the {file} they are handed.
MAX_FOLDER_TASKS = 20
FILE_TOKEN = "{file}"

# What each script type is run with when the owner does not pick a program in Advanced.
SCRIPT_TYPES: Dict[str, Dict[str, Any]] = {
    ".sh": {"label": "Shell script", "program": "/bin/bash"},
    ".bash": {"label": "Shell script", "program": "/bin/bash"},
    ".zsh": {"label": "Shell script", "program": "/bin/zsh"},
    ".command": {"label": "Shell script", "program": "/bin/zsh"},
    ".py": {"label": "Python script", "program": "python3"},
    ".js": {"label": "JavaScript (Node)", "program": "node"},
    ".mjs": {"label": "JavaScript (Node)", "program": "node"},
    ".cjs": {"label": "JavaScript (Node)", "program": "node"},
    ".ts": {"label": "TypeScript (Node)", "program": "node"},
    ".applescript": {"label": "AppleScript", "program": "/usr/bin/osascript"},
    ".scpt": {"label": "AppleScript", "program": "/usr/bin/osascript"},
    ".rb": {"label": "Ruby script", "program": "ruby"},
    ".pl": {"label": "Perl script", "program": "perl"},
}
_PROGRAM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")

_LOCK = threading.RLock()
# A process commonly reads the task list many times while the menu is open.  Keep
# the schema/WAL setup for each database file to its first connection instead of
# doing DDL and a journal-mode round trip for every read.  The identity check
# still initializes a replacement database at the same path.
_DB_IDENTITIES: Dict[Path, tuple[int, int]] = {}


class TaskError(ValueError):
    """A request the store refuses, with a stable code and a plain message."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.code, "message": str(self)}


# ── Storage ──────────────────────────────────────────────────────────────────

def db_path() -> Path:
    return paths.scheduled_tasks_db()


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        try:
            identity = (path.stat().st_dev, path.stat().st_ino)
        except OSError:
            identity = None
        initialize = path not in _DB_IDENTITIES or _DB_IDENTITIES[path] != identity
        conn = sqlite3.connect(path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            if initialize:
                try:
                    path.parent.chmod(0o700)
                except OSError:
                    pass
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        definition TEXT,
                        proposal TEXT,
                        proposed_by TEXT,
                        proposed_at REAL,
                        paused INTEGER NOT NULL DEFAULT 0,
                        created_by TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        next_run_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        trigger TEXT NOT NULL,
                        scheduled_for REAL,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        status TEXT NOT NULL,
                        reason TEXT,
                        summary TEXT,
                        output TEXT,
                        exit_code INTEGER,
                        agent_id TEXT,
                        skipped_count INTEGER NOT NULL DEFAULT 0,
                        detail TEXT
                    );
                    CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id, started_at DESC);
                    CREATE TABLE IF NOT EXISTS diagnoses (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        agent_id TEXT,
                        started_at REAL NOT NULL,
                        finished_at REAL,
                        summary TEXT,
                        report TEXT,
                        reason TEXT
                    );
                    CREATE INDEX IF NOT EXISTS diagnoses_by_run ON diagnoses(run_id, started_at DESC);
                    CREATE TABLE IF NOT EXISTS folder_watch (
                        task_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    """
                )
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
                # Databases from before output checks (#198) and expiring contents (#199). Two
                # processes may open an old database at once; the second finds the column there.
                for column, kind in (("verification", "TEXT"), ("details_expired_at", "REAL"), ("trigger_file", "TEXT"),
                                     ("resumed_from", "TEXT")):
                    if column not in columns:
                        try:
                            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {kind}")
                        except sqlite3.OperationalError as exc:
                            if "duplicate column" not in str(exc).lower():
                                raise
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
                stat = path.stat()
                _DB_IDENTITIES[path] = (stat.st_dev, stat.st_ino)
            yield conn
            conn.commit()
        finally:
            conn.close()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _load(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# ── Validation ───────────────────────────────────────────────────────────────

def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise TaskError(f"{field}_required", f"{field.replace('_', ' ').capitalize()} is required.")
    if len(text) > limit:
        raise TaskError(f"{field}_too_long", f"{field.replace('_', ' ').capitalize()} must be at most {limit} characters.")
    return text


def _folder(value: Any, field: str = "cwd") -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        raise TaskError(f"{field}_invalid", "The folder must be a full path such as ~/Projects/site.")
    if not folder.is_dir():
        raise TaskError(f"{field}_missing", f"The folder {text} does not exist.")
    return str(folder)


def _output_checks(raw: Any, cwd: Optional[str]) -> List[Dict[str, Any]]:
    """Files the owner expects a run to leave behind (#198); checked by size and time, never read."""
    if not isinstance(raw, list):
        raise TaskError("verify_invalid", "verify must be a list of files to check, such as [{\"path\": \"report.csv\"}].")
    if len(raw) > MAX_OUTPUT_CHECKS:
        raise TaskError("verify_too_many", f"A task can check at most {MAX_OUTPUT_CHECKS} files.")
    checks = []
    for item in raw:
        item = item if isinstance(item, dict) else {"path": item}
        if item.get("url") is not None or item.get("tool") is not None:
            # A page or one of the owner's read-only MCP tools, checked after the run (#228).
            from .run_postconditions import CheckError, validate
            try:
                check = validate(item)
            except CheckError as exc:
                raise TaskError(exc.code, str(exc)) from exc
            for field in ("url", "contains"):
                check_dates(str(check.get(field) or ""), "verify_" + field)
            checks.append(check)
            continue
        text = _text(item.get("path"), "verify_path", 1024, required=True)
        path = Path(text).expanduser()
        if not path.is_absolute():
            if not cwd:
                raise TaskError("verify_needs_folder",
                                f"{text} is a relative path; give the full path, or choose the task's folder.")
            path = Path(cwd) / path
        min_bytes = item.get("min_bytes", 1)
        if not isinstance(min_bytes, int) or isinstance(min_bytes, bool) or min_bytes < 0:
            raise TaskError("verify_min_bytes_invalid", "The smallest size must be a whole number of bytes (0 or more).")
        written = item.get("written_this_run", True)
        if not isinstance(written, bool):
            raise TaskError("verify_written_invalid", "written_this_run must be true or false.")
        checks.append({"path": os.path.normpath(str(path)), "min_bytes": min_bytes, "written_this_run": written})
    return checks


def find_program(name: str) -> Optional[str]:
    """A program by name in the usual places (the server's PATH is minimal under launchd)."""
    if "/" in name:
        return name if os.access(name, os.X_OK) else None
    for folder in [str(Path.home() / ".local" / "bin"), *_PROGRAM_DIRS]:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _nearby_venv_python(script: Path) -> Optional[str]:
    """A virtualenv next to the script or in a folder above it (the common project layout)."""
    for folder in [script.parent, *list(script.parent.parents)[:3]]:
        for name in (".venv", "venv"):
            candidate = folder / name / "bin" / "python"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def inspect_script(path_text: str, interpreter: Optional[str] = None) -> Dict[str, Any]:
    """What a script file is and what will run it, in words for the New Task sheet."""
    script = Path(str(path_text or "").strip()).expanduser()
    info: Dict[str, Any] = {"path": str(script), "name": script.name, "folder": str(script.parent)}
    if not script.is_absolute():
        return {**info, "ok": False, "error": "script_path_invalid", "message": "Choose the script file."}
    if not script.is_file():
        return {**info, "ok": False, "error": "script_missing", "message": f"{script.name} was not found."}
    kind = SCRIPT_TYPES.get(script.suffix.lower())
    executable = os.access(script, os.X_OK)
    info["label"] = kind["label"] if kind else ("Program" if executable else "File")
    chosen = str(interpreter or "").strip()
    if chosen:
        program = find_program(str(Path(chosen).expanduser()) if "/" in chosen else chosen)
        if not program:
            return {**info, "ok": False, "error": "interpreter_missing", "message": f"{chosen} was not found."}
        info.update({"program": program, "program_source": "chosen"})
    elif kind and kind["program"] == "python3" and _nearby_venv_python(script):
        info.update({"program": _nearby_venv_python(script), "program_source": "venv"})
    elif kind:
        program = find_program(kind["program"])
        if not program:
            return {**info, "ok": False, "error": "interpreter_missing",
                    "message": f"{kind['label']}s need {kind['program']}, which is not installed."}
        info.update({"program": program, "program_source": "type"})
    elif executable:
        info.update({"program": None, "program_source": "direct"})
    else:
        return {**info, "ok": False, "error": "script_type_unknown",
                "message": f"Mac MCP does not know how to run {script.suffix or 'this'} files. "
                           "Pick a program under Advanced › Run with."}
    info["ok"] = True
    return info


# ── Dates in arguments ───────────────────────────────────────────────────────

# {token}: what it means, in the run's local time. Values are YYYY-MM-DD; weeks start on Monday.
DATE_TOKENS: Dict[str, str] = {
    "today": "Today",
    "yesterday": "Yesterday",
    "this_week_start": "This week's Monday",
    "last_week_start": "Last week's Monday",
    "last_week_end": "Last week's Sunday",
    "this_month_start": "The 1st of this month",
    "last_month_start": "The 1st of last month",
    "last_month_end": "The last day of last month",
}
_TOKEN_RE = re.compile(r"\{([a-z_]+)(?::(\d{1,3}))?\}")


def _date_value(name: str, number: Optional[str], day: date) -> Optional[date]:
    if name == "days_ago":
        return day - timedelta(days=int(number)) if number is not None else None
    if number is not None:
        return None
    monday = day - timedelta(days=day.weekday())
    first = day.replace(day=1)
    last_month_end = first - timedelta(days=1)
    return {
        "today": day, "yesterday": day - timedelta(days=1),
        "this_week_start": monday, "last_week_start": monday - timedelta(days=7),
        "last_week_end": monday - timedelta(days=1),
        "this_month_start": first, "last_month_start": last_month_end.replace(day=1),
        "last_month_end": last_month_end,
    }.get(name)


def _day(at: Optional[float]) -> date:
    return datetime.fromtimestamp(time.time() if at is None else at).date()


def expand_dates(text: str, at: Optional[float] = None) -> str:
    """{last_week_start} and friends replaced by dates for a run at `at` (now if not given)."""
    day = _day(at)

    def value(match: "re.Match[str]") -> str:
        resolved = _date_value(match.group(1), match.group(2), day)
        return resolved.isoformat() if resolved else match.group(0)
    return _TOKEN_RE.sub(value, text or "")


def date_choices(at: Optional[float] = None) -> List[Dict[str, str]]:
    """The date tokens with what each one means on a run at `at`, for the New Task sheet."""
    day = _day(at)
    choices = [{"token": "{%s}" % name, "label": label, "value": _date_value(name, None, day).isoformat()}
               for name, label in DATE_TOKENS.items()]
    choices.append({"token": "{days_ago:7}", "label": "7 days ago (any number up to 366)",
                    "value": _date_value("days_ago", "7", day).isoformat()})
    return choices


def check_dates(text: str, field: str) -> None:
    for match in _TOKEN_RE.finditer(text or ""):
        name, number = match.group(1), match.group(2)
        if name == "file" and number is None:
            continue
        if name == "days_ago" and number is not None and int(number) <= 366:
            continue
        if name in DATE_TOKENS and number is None:
            continue
        known = ", ".join("{%s}" % key for key in DATE_TOKENS) + ", {days_ago:N}"
        raise TaskError(f"{field}_date_unknown", f"{match.group(0)} is not a date Mac MCP knows. Use one of {known}.")


def _program(raw: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """A script file or a command line, with its arguments and program, checked for running."""
    script = _text(raw.get("script"), prefix + "script", 1024)
    command = _text(raw.get("command"), prefix + "command", 4000)
    if bool(script) == bool(command):
        where = "for the data step" if prefix else "under Advanced"
        raise TaskError(prefix + "script_or_command", f"Choose a script file, or enter a command {where} (not both).")
    args = _text(raw.get("args"), prefix + "args", 2000)
    try:
        shlex.split(args)
    except ValueError as exc:
        raise TaskError(prefix + "args_invalid", f"The arguments could not be read: {exc}.") from exc
    check_dates(args, prefix + "args")
    check_dates(command, prefix + "command")
    interpreter = _text(raw.get("interpreter"), prefix + "interpreter", 1024) or None
    if script:
        info = inspect_script(script, interpreter)
        if not info["ok"]:
            raise TaskError(info["error"], info["message"])
        script = info["path"]
    return {"script": script or None, "command": command or None, "args": args, "interpreter": interpreter}


def _usage_limit(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        return run_budget.validate(raw)
    except run_budget.BudgetError as exc:
        raise TaskError(exc.code, str(exc)) from exc


def provider_label(provider: Optional[str]) -> Optional[str]:
    if not provider:
        return None
    from .tools_agents import _PROVIDER_LABELS

    return _PROVIDER_LABELS.get(provider, provider)


def validate_definition(raw: Any) -> Dict[str, Any]:
    """The definition in normal form, or TaskError with a message the owner can act on."""
    if not isinstance(raw, dict):
        raise TaskError("definition_invalid", "A task needs a name, a kind and a schedule.")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise TaskError("kind_invalid", "kind must be ai (a prompt for an agent) or script.")
    try:
        schedule = schedule_rules.validate(raw.get("schedule"))
    except schedule_rules.ScheduleError as exc:
        raise TaskError("schedule_invalid", str(exc)) from exc
    timeout = raw.get("timeout_min", DEFAULT_TIMEOUT_MIN[kind])
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_MIN:
        raise TaskError("timeout_invalid", f"The time limit must be 1 to {MAX_TIMEOUT_MIN} minutes.")
    definition: Dict[str, Any] = {
        "name": _text(raw.get("name"), "name", 80, required=True),
        "description": _text(raw.get("description"), "description", 500),
        "kind": kind,
        "schedule": schedule,
        "notify": raw.get("notify", True) is not False,
        "timeout_min": timeout,
        "cwd": _folder(raw.get("cwd")),
    }
    if raw.get("verify"):
        definition["verify"] = _output_checks(raw["verify"], definition["cwd"])
    if kind == "ai":
        definition["prompt"] = _text(raw.get("prompt"), "prompt", 8000, required=True)
        definition["provider"] = _text(raw.get("provider"), "provider", 40) or None
        definition["model"] = _text(raw.get("model"), "model", 120) or None
        data = raw.get("data_step")
        if data is not None and not isinstance(data, dict):
            raise TaskError("data_step_invalid", "data_step must be an object with a script or a command.")
        if data and any(str(data.get(key) or "").strip() for key in ("script", "command")):
            definition["data_step"] = _program(data, "data_")
        limit = _usage_limit(raw.get("usage_limit"))
        if limit:
            provider = definition["provider"] or run_budget.default_provider()
            problem = run_budget.unenforceable(limit, provider, provider_label(provider))
            if problem:
                raise TaskError("usage_limit_unenforceable", problem)
            definition["usage_limit"] = limit
        return _check_file_token(definition)
    if raw.get("data_step"):
        raise TaskError("data_step_ai_only", "Only AI tasks have a data step; a script task already runs a script.")
    if _usage_limit(raw.get("usage_limit")):
        raise TaskError("usage_limit_ai_only", "Only AI tasks have a usage limit; a script task has its time limit.")
    definition.update(_program(raw))
    return _check_file_token(definition)


def _check_file_token(definition: Dict[str, Any]) -> Dict[str, Any]:
    """{file} names the file that started a run, so only a task that runs when a file arrives has one."""
    if definition["schedule"]["type"] == "folder":
        return definition
    data = definition.get("data_step") or {}
    texts = [definition.get(key) for key in ("prompt", "args", "command")] + [data.get("args"), data.get("command")]
    if any(FILE_TOKEN in str(text or "") for text in texts):
        raise TaskError("file_token_needs_folder",
                        "{file} is the file that started the run, so it only works when the task runs as a file arrives.")
    return definition


# ── Tasks ────────────────────────────────────────────────────────────────────

def _next_run(definition: Optional[Dict[str, Any]], paused: bool, after: float) -> Optional[float]:
    if not definition or paused:
        return None
    return schedule_rules.next_run(definition["schedule"], after)


def _public(row: sqlite3.Row, last: Optional[sqlite3.Row] = None, running: bool = False) -> Dict[str, Any]:
    definition = _load(row["definition"])
    proposal = _load(row["proposal"])
    shown = definition or proposal or {}
    task: Dict[str, Any] = {
        "id": row["id"],
        **shown,
        "schedule_text": schedule_rules.describe(shown["schedule"]) if shown.get("schedule") else "",
        "state": ("needs_approval" if definition is None else "paused" if row["paused"]
                  else "running" if running else "active" if row["next_run_at"] or _watches(definition)
                  else "finished"),
        "paused": bool(row["paused"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "next_run_at": row["next_run_at"],
        "approved": definition is not None,
    }
    if proposal is not None:
        task["proposal"] = {**proposal, "schedule_text": schedule_rules.describe(proposal["schedule"]),
                            "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
                            "is_new": definition is None}
    if last is not None:
        task["last_run"] = _run_public(last, brief=True)
    return task


def _watches(definition: Optional[Dict[str, Any]]) -> bool:
    return bool(definition) and definition["schedule"]["type"] == "folder"


def _run_public(row: sqlite3.Row, *, brief: bool = False) -> Dict[str, Any]:
    run = {key: row[key] for key in ("id", "task_id", "trigger", "scheduled_for", "started_at", "finished_at",
                                      "status", "reason", "exit_code", "agent_id", "skipped_count", "trigger_file",
                                      "resumed_from")}
    run["summary"] = (row["summary"] or "")[:300] if brief else row["summary"]
    if row["finished_at"] and row["status"] != "skipped":
        run["duration_s"] = round(row["finished_at"] - row["started_at"], 1)
    run["verification"] = _load(row["verification"])
    run["details_expired_at"] = row["details_expired_at"]
    if not brief:
        run["output"] = row["output"]
        run["detail"] = _load(row["detail"])
    return run


def _running_ids(conn: sqlite3.Connection) -> set:
    return {row["task_id"] for row in conn.execute("SELECT task_id FROM runs WHERE status='running'")}


def _latest_runs(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    """The most recent run for every task, fetched in one set-based query.

    `rowid` makes ties match SQLite's existing index order for the former
    per-task `ORDER BY started_at DESC LIMIT 1` lookup.
    """
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT runs.*, ROW_NUMBER() OVER (
                PARTITION BY task_id ORDER BY started_at DESC, rowid ASC
            ) AS latest_rank
            FROM runs
        ) WHERE latest_rank = 1
        """
    ).fetchall()
    return {row["task_id"]: row for row in rows}


def list_tasks() -> List[Dict[str, Any]]:
    with _db() as conn:
        _expire_contents(conn, time.time())  # reads never hand back expired contents
        running = _running_ids(conn)
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        latest = _latest_runs(conn)
        out = [_public(row, latest.get(row["id"]), row["id"] in running) for row in rows]
    order = {"needs_approval": 0}
    out.sort(key=lambda task: (order.get(task["state"], 1) if "proposal" not in task else 0, task["created_at"]))
    return out


def get_task(task_id: str) -> Dict[str, Any]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("task_not_found", "That task no longer exists.", 404)
        last = conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
        return _public(row, last, task_id in _running_ids(conn))


def definition_of(task_id: str) -> Optional[Dict[str, Any]]:
    """The approved definition the scheduler runs (None while only a proposal exists)."""
    with _db() as conn:
        row = conn.execute("SELECT definition FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _load(row["definition"]) if row else None


def _check_folder_room(conn: sqlite3.Connection, definition: Dict[str, Any], task_id: Optional[str] = None) -> None:
    if not _watches(definition):
        return
    watching = sum(1 for row in conn.execute("SELECT id, definition, proposal FROM tasks")
                   if row["id"] != task_id and (_watches(_load(row["definition"])) or _watches(_load(row["proposal"]))))
    if watching >= MAX_FOLDER_TASKS:
        raise TaskError("too_many_folder_tasks", f"At most {MAX_FOLDER_TASKS} tasks can wait for files.")


def create_task(raw: Any, *, by: str = "owner", now: Optional[float] = None) -> Dict[str, Any]:
    """The owner's task is live at once; anyone else's waits for the owner's approval."""
    definition = validate_definition(raw)
    now = time.time() if now is None else now
    owner = by == "owner"
    task_id = _new_id("tsk")
    with _db() as conn:
        if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= MAX_TASKS:
            raise TaskError("too_many_tasks", f"Mac MCP keeps at most {MAX_TASKS} scheduled tasks.")
        _check_folder_room(conn, definition)
        conn.execute(
            "INSERT INTO tasks (id, definition, proposal, proposed_by, proposed_at, paused, created_by, created_at,"
            " updated_at, next_run_at) VALUES (?,?,?,?,?,0,?,?,?,?)",
            (task_id, json.dumps(definition) if owner else None, None if owner else json.dumps(definition),
             None if owner else by, None if owner else now, by, now, now,
             _next_run(definition, False, now) if owner else None),
        )
    _changed()
    return get_task(task_id)


def update_task(task_id: str, raw: Any, *, by: str = "owner", now: Optional[float] = None) -> Dict[str, Any]:
    """The owner's edit replaces the task; an agent's edit becomes a proposal for the owner to review."""
    now = time.time() if now is None else now
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("task_not_found", "That task no longer exists.", 404)
        base = _load(row["proposal"]) if by != "owner" and row["proposal"] else _load(row["definition"]) or _load(row["proposal"]) or {}
        merged = {**base, **(raw if isinstance(raw, dict) else {})}
        definition = validate_definition(merged)
        _check_folder_room(conn, definition, task_id)
        if by == "owner":
            conn.execute(
                "UPDATE tasks SET definition=?, proposal=NULL, proposed_by=NULL, proposed_at=NULL, updated_at=?,"
                " next_run_at=? WHERE id=?",
                (json.dumps(definition), now, _next_run(definition, bool(row["paused"]), now), task_id),
            )
        else:
            conn.execute("UPDATE tasks SET proposal=?, proposed_by=?, proposed_at=?, updated_at=? WHERE id=?",
                         (json.dumps(definition), by, now, now, task_id))
    _changed()
    return get_task(task_id)


def approve(task_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """The owner accepts a proposal: it becomes the task that runs."""
    now = time.time() if now is None else now
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("task_not_found", "That task no longer exists.", 404)
        proposal = _load(row["proposal"])
        if proposal is None:
            raise TaskError("nothing_to_approve", "This task has no change waiting for approval.", 409)
        try:
            definition = validate_definition(proposal)  # files and folders are checked again now
        except TaskError as exc:
            raise TaskError(exc.code, f"It cannot be approved as it is: {exc}", 409) from exc
        conn.execute(
            "UPDATE tasks SET definition=?, proposal=NULL, proposed_by=NULL, proposed_at=NULL, updated_at=?,"
            " next_run_at=? WHERE id=?",
            (json.dumps(definition), now, _next_run(definition, bool(row["paused"]), now), task_id),
        )
    _changed()
    return get_task(task_id)


def decline(task_id: str) -> Dict[str, Any]:
    """The owner rejects a proposal: a new suggested task is removed, an edit is dropped."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["proposal"] is None:
            raise TaskError("nothing_to_decline", "This task has no change waiting for approval.", 409)
        if row["definition"] is None:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
            removed = True
        else:
            conn.execute("UPDATE tasks SET proposal=NULL, proposed_by=NULL, proposed_at=NULL WHERE id=?", (task_id,))
            removed = False
    _changed()
    return {"ok": True, "id": task_id, "removed": removed}


def set_paused(task_id: str, paused: bool, *, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskError("task_not_found", "That task no longer exists.", 404)
        conn.execute("UPDATE tasks SET paused=?, updated_at=?, next_run_at=? WHERE id=?",
                     (int(paused), now, _next_run(_load(row["definition"]), paused, now), task_id))
    _changed()
    return get_task(task_id)


def delete_task(task_id: str) -> Dict[str, Any]:
    with _db() as conn:
        found = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount
        conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM folder_watch WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM diagnoses WHERE task_id=?", (task_id,))
    if not found:
        raise TaskError("task_not_found", "That task no longer exists.", 404)
    _changed()
    return {"ok": True, "id": task_id, "deleted": True}


def due_tasks(now: float) -> List[Dict[str, Any]]:
    """Approved, unpaused tasks whose next run time has come."""
    with _db() as conn:
        rows = conn.execute("SELECT id, definition, next_run_at FROM tasks WHERE paused=0 AND definition IS NOT NULL"
                            " AND next_run_at IS NOT NULL AND next_run_at<=? ORDER BY next_run_at", (now,)).fetchall()
    return [{"id": row["id"], "definition": _load(row["definition"]), "next_run_at": row["next_run_at"]} for row in rows]


# A run this long past its time with no start and no skip recorded did not start (#225). The scheduler
# wakes at least once a minute while tasks are active and records a late run as skipped after two.
OVERDUE_TOLERANCE_S = 300.0


def overdue(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Approved, unpaused timed tasks whose run time passed beyond the tolerance and that never started.

    Read from the task store by whoever asks (the HTTP side, the app), not by the scheduler loop,
    so a scheduler that stopped waking is still noticed."""
    now = time.time() if now is None else now
    with _db() as conn:
        running = _running_ids(conn)
        rows = conn.execute("SELECT id, definition, next_run_at FROM tasks WHERE paused=0 AND definition IS NOT NULL"
                            " AND next_run_at IS NOT NULL AND next_run_at<? ORDER BY next_run_at",
                            (now - OVERDUE_TOLERANCE_S,)).fetchall()
    out = []
    for row in rows:
        definition = _load(row["definition"]) or {}
        if row["id"] in running or _watches(definition):
            continue
        out.append({"id": row["id"], "name": definition.get("name"), "expected_at": row["next_run_at"],
                    "notify": definition.get("notify", True)})
    return out


def due_soon(now: Optional[float] = None, within_s: float = 6 * 3600) -> List[Dict[str, Any]]:
    """Timed runs due in the next hours, so the app can still say one was due if Mac MCP stops answering."""
    now = time.time() if now is None else now
    with _db() as conn:
        rows = conn.execute("SELECT id, definition, next_run_at FROM tasks WHERE paused=0 AND definition IS NOT NULL"
                            " AND next_run_at IS NOT NULL AND next_run_at>=? AND next_run_at<=? ORDER BY next_run_at"
                            " LIMIT 50", (now, now + within_s)).fetchall()
    return [{"id": row["id"], "name": (_load(row["definition"]) or {}).get("name"), "at": row["next_run_at"],
             "notify": (_load(row["definition"]) or {}).get("notify", True)} for row in rows]


def earliest_next_run() -> Optional[float]:
    with _db() as conn:
        row = conn.execute("SELECT MIN(next_run_at) FROM tasks WHERE paused=0 AND definition IS NOT NULL").fetchone()
    return row[0] if row else None


def advance(task_id: str, after: float) -> Optional[float]:
    """Move a task's next run past `after` (after it ran or was skipped)."""
    with _db() as conn:
        row = conn.execute("SELECT definition, paused FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        nxt = _next_run(_load(row["definition"]), bool(row["paused"]), after)
        conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (nxt, task_id))
    return nxt


def up_next(limit: int = 1) -> List[Dict[str, Any]]:
    tasks = [task for task in list_tasks() if task["state"] == "active" and task.get("next_run_at")]
    tasks.sort(key=lambda task: task["next_run_at"])
    return [{"id": task["id"], "name": task["name"], "at": task["next_run_at"]} for task in tasks[:limit]]


# ── Runs ─────────────────────────────────────────────────────────────────────

def start_run(task_id: str, *, trigger: str, scheduled_for: Optional[float] = None,
              now: Optional[float] = None, trigger_file: Optional[str] = None,
              resumed_from: Optional[str] = None) -> Optional[str]:
    """Record a run as started; None when this task already has one in progress."""
    now = time.time() if now is None else now
    run_id = _new_id("run")
    with _db() as conn:
        if conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)).fetchone():
            return None
        # A run is resumed once (#226): checked in the same transaction as the insert.
        if resumed_from and conn.execute("SELECT 1 FROM runs WHERE resumed_from=?", (resumed_from,)).fetchone():
            raise TaskError("already_resumed", "This run was already resumed; resume the newer run instead.", 409)
        conn.execute("INSERT INTO runs (id, task_id, trigger, scheduled_for, started_at, status, trigger_file,"
                     " resumed_from) VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, task_id, trigger, scheduled_for, now, "running", trigger_file, resumed_from))
    _changed()
    return run_id


def resumed_by(run_id: str) -> Optional[str]:
    """The run that resumed this one (#226), or None."""
    with _db() as conn:
        row = conn.execute("SELECT id FROM runs WHERE resumed_from=?", (run_id,)).fetchone()
    return row["id"] if row else None


def set_verification(run_id: str, verification: Dict[str, Any]) -> None:
    """A finished run's checks, looked at again (#226's Check Again); nothing else of the run changes."""
    with _db() as conn:
        conn.execute("UPDATE runs SET verification=? WHERE id=? AND status='done'", (json.dumps(verification), run_id))
    _changed()


def note_agent(run_id: str, agent_id: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE runs SET agent_id=? WHERE id=?", (agent_id, run_id))


def finish_run(run_id: str, status: str, *, reason: Optional[str] = None, summary: Optional[str] = None,
               output: Optional[str] = None, exit_code: Optional[int] = None,
               detail: Optional[Dict[str, Any]] = None, verification: Optional[Dict[str, Any]] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
    if status not in RUN_STATUSES or status == "running":
        raise ValueError(f"bad run status {status}")
    now = time.time() if now is None else now
    # Fingerprinted and masked for known secrets before anything is written (#199).
    summary, output, detail = run_records.prepare(summary, output, detail)
    reason = run_records.prepare(reason, None, None)[0]
    with _db() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, reason=?, summary=?, output=?, exit_code=?, detail=?,"
            " verification=? WHERE id=?",
            (status, now, reason, (summary or "")[:SUMMARY_LIMIT] or None,
             (output or "")[-OUTPUT_LIMIT:] or None, exit_code, json.dumps(detail) if detail else None,
             json.dumps(verification) if verification else None, run_id),
        )
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        _prune(conn, row["task_id"], now)
    _changed()
    return _run_public(row)


def record_skip(task_id: str, *, scheduled_for: float, reason: str, count: int = 1,
                now: Optional[float] = None, trigger: str = "schedule",
                trigger_file: Optional[str] = None) -> Dict[str, Any]:
    """One history entry for a run (or a stretch of runs) whose time passed without running."""
    now = time.time() if now is None else now
    run_id = _new_id("run")
    with _db() as conn:
        conn.execute("INSERT INTO runs (id, task_id, trigger, scheduled_for, started_at, finished_at, status, reason,"
                     " skipped_count, trigger_file) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (run_id, task_id, trigger, scheduled_for, now, now, "skipped", reason, count, trigger_file))
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        _prune(conn, task_id, now)
    _changed()
    return _run_public(row)


def list_runs(task_id: str, limit: int = RUNS_PER_TASK) -> List[Dict[str, Any]]:
    with _db() as conn:
        _expire_contents(conn, time.time())  # reads never hand back expired contents
        rows = conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT ?",
                            (task_id, max(1, min(int(limit), RUNS_PER_TASK)))).fetchall()
    return [_run_public(row, brief=True) for row in rows]


def get_run(run_id: str) -> Dict[str, Any]:
    with _db() as conn:
        _expire_contents(conn, time.time())  # reads never hand back expired contents
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise TaskError("run_not_found", "That run is no longer in the history.", 404)
    return _run_public(row)


def running_runs() -> List[Dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM runs WHERE status='running'").fetchall()
    return [_run_public(row) for row in rows]


def finished_since(since: float) -> List[Dict[str, Any]]:
    """Runs that ended after `since`, with their task's name and notify choice (for notifications)."""
    with _db() as conn:
        _expire_contents(conn, time.time())  # reads never hand back expired contents
        rows = conn.execute("SELECT runs.*, tasks.definition AS def FROM runs JOIN tasks ON tasks.id=runs.task_id"
                            " WHERE runs.finished_at>? ORDER BY runs.finished_at", (since,)).fetchall()
    out = []
    for row in rows:
        definition = _load(row["def"]) or {}
        out.append({**_run_public(row, brief=True), "task_name": definition.get("name"),
                    "notify": definition.get("notify", True)})
    return out


def _prune(conn: sqlite3.Connection, task_id: str, now: float) -> None:
    conn.execute("DELETE FROM runs WHERE task_id=? AND status!='running' AND id NOT IN"
                 " (SELECT id FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT ?)",
                 (task_id, task_id, RUNS_PER_TASK))
    _expire(conn, now)


def apply_retention(now: Optional[float] = None) -> None:
    """Drop runs past 90 days and contents past the owner's period, with no run finishing (#199)."""
    with _db() as conn:
        _expire(conn, time.time() if now is None else now)


def _expire(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM runs WHERE status!='running' AND started_at<?", (now - RUN_RETENTION_DAYS * 86400,))
    conn.execute("DELETE FROM diagnoses WHERE run_id NOT IN (SELECT id FROM runs)")  # gone with their run
    _expire_contents(conn, now)


def _expire_contents(conn: sqlite3.Connection, now: float) -> None:
    # The contents go first (Settings > Privacy > run details); how the run ended stays (#199).
    cutoff = now - run_detail_privacy()["retention_days"] * 86400
    for row in conn.execute(
            "SELECT id, detail FROM runs WHERE status!='running' AND details_expired_at IS NULL"
            " AND finished_at IS NOT NULL AND finished_at<?"
            " AND (summary IS NOT NULL OR output IS NOT NULL OR detail IS NOT NULL)", (cutoff,)).fetchall():
        skeleton = run_records.skeleton(_load(row["detail"]))
        conn.execute("UPDATE runs SET summary=NULL, output=NULL, detail=?, details_expired_at=? WHERE id=?",
                     (json.dumps(skeleton) if skeleton else None, now, row["id"]))
        # A diagnosis quotes the run, so its text goes with the run's contents (#227).
        conn.execute("UPDATE diagnoses SET summary=NULL, report=NULL WHERE run_id=?", (row["id"],))


# ── Diagnoses (#227) ─────────────────────────────────────────────────────────

_DIAGNOSIS_FIELDS = ("id", "run_id", "task_id", "status", "agent_id", "started_at", "finished_at", "summary",
                     "report", "reason")


def diagnosis_for(run_id: str) -> Optional[Dict[str, Any]]:
    """The latest diagnosis of a run, or None."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM diagnoses WHERE run_id=? ORDER BY started_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    return {key: row[key] for key in _DIAGNOSIS_FIELDS} if row else None


def create_diagnosis(run_id: str, task_id: str, *, status: str, report: Optional[str] = None,
                     reason: Optional[str] = None, now: Optional[float] = None) -> str:
    diagnosis_id = _new_id("dgn")
    now = time.time() if now is None else now
    report, _, _ = run_records.prepare(report, None, None)
    with _db() as conn:
        conn.execute("INSERT INTO diagnoses (id, run_id, task_id, status, started_at, finished_at, report, reason)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (diagnosis_id, run_id, task_id, status, now, None if status == "running" else now, report, reason))
    _changed()
    return diagnosis_id


def update_diagnosis(diagnosis_id: str, *, now: Optional[float] = None, **fields: Any) -> None:
    allowed = {"status", "agent_id", "summary", "report", "reason"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if "summary" in values or "report" in values:
        # Masked for known secrets before it is written, like run contents (#199).
        summary, report, _ = run_records.prepare(values.get("summary"), values.get("report"), None)
        values.update({key: value for key, value in (("summary", summary), ("report", report)) if key in values})
    if values.get("status") not in (None, "running"):
        values["finished_at"] = time.time() if now is None else now
    if not values:
        return
    with _db() as conn:
        conn.execute(f"UPDATE diagnoses SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
                     (*values.values(), diagnosis_id))
    _changed()


# ── Folder triggers (#193) ───────────────────────────────────────────────────

def folder_tasks() -> List[Dict[str, Any]]:
    """Approved, unpaused tasks that run when a file arrives."""
    with _db() as conn:
        rows = conn.execute("SELECT id, definition FROM tasks WHERE paused=0 AND definition IS NOT NULL").fetchall()
    out = []
    for row in rows:
        definition = _load(row["definition"])
        if _watches(definition):
            out.append({"id": row["id"], "definition": definition})
    return out


def watch_state(task_id: str) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT state FROM folder_watch WHERE task_id=?", (task_id,)).fetchone()
    return _load(row["state"]) if row else None


def save_watch_state(task_id: str, state: Dict[str, Any], now: Optional[float] = None) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO folder_watch (task_id, state, updated_at) VALUES (?,?,?) ON CONFLICT(task_id)"
                     " DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                     (task_id, json.dumps(state), time.time() if now is None else now))


def file_runs_since(task_id: str, since: float) -> int:
    with _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM runs WHERE task_id=? AND trigger='file' AND status!='skipped'"
                            " AND started_at>=?", (task_id, since)).fetchone()[0]


def last_run_window(task_id: str) -> Optional[tuple]:
    """(started_at, finished_at or None) of the task's latest run that ran."""
    with _db() as conn:
        row = conn.execute("SELECT started_at, finished_at FROM runs WHERE task_id=? AND status!='skipped'"
                           " ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
    return (row["started_at"], row["finished_at"]) if row else None


def is_running(task_id: str) -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)).fetchone() is not None


# ── Change signal for the scheduler loop ─────────────────────────────────────

_listeners: List[Any] = []


def on_change(callback: Any) -> None:
    _listeners.append(callback)


def _changed() -> None:
    for callback in list(_listeners):
        try:
            callback()
        except Exception:
            pass
