# Scheduled Tasks (#175, first step) — design

Agreed with the owner on 2026-10-13. The owner asked to see it built and review the real screens.

## What it is

The owner (and AI apps, with approval) can create tasks that run on their own at a set time or on a recurrence. Each task has a name and an optional description and is one of two kinds:

- **AI task**: a prompt run by a delegated agent (Codex, Claude Code or OpenCode, whichever is enabled), in a chosen folder.
- **Script task**: a script file run with the interpreter its extension implies (`.sh/.zsh/.bash` shell, `.py` Python or a chosen venv, `.js/.mjs` Node, `.applescript/.scpt` osascript, any executable directly), or a command line.

Positioned as experimental; built so a later paid tier can attach a local quota. No accounts, licensing or payment now.

## Decisions

| Topic | Decision |
|---|---|
| Kinds | AI task and script task (C). "Script, then an agent reads its output" is a later stage. |
| Who creates | Owner in Settings, and AI apps through MCP. A task an agent creates or changes waits as **Needs your approval** and never runs until the owner approves it in Settings. Agents cannot approve. |
| Results | macOS notification and the task's run history (A). An AI run shows the agent's summary, full report and changed files; a script run shows exit status and the end of its output. No email in this step. |
| Missed runs | Skipped (B): a run whose time passed while the Mac slept or Mac MCP was stopped is recorded as **Skipped** with the reason and is not run late. The owner can Run now. |
| Where it runs | Inside the Mac MCP server (approach 1). Start at login and the supervisor keep the server up. |
| Cost | No fixed polling: sleep until the next due run, waking at most once a minute to notice sleep and clock changes; no wakeups when there are no active tasks. Measured before release. |
| Look | Premium, plain language. Raw cron, script arguments, interpreter and time limit live in a collapsed **Advanced** section. |

## Screens (Settings › Scheduled Tasks)

- **List**: title, subtitle, **+ New Task**; an **Up next** strip; one card per task (icon by kind, name, plain schedule, last result: done / skipped with reason / failed, chevron). Tasks needing approval sit on top, highlighted, with **Review**. Paused tasks are dimmed.
- **Detail** (click a card, back arrow): icon, name, schedule and kind, description, **Run now / Edit / Pause|Resume / Delete**, recent runs (✓ done with summary, ⏭ skipped with reason, ✗ failed with reason); a run opens its full result.
- **New / Edit sheet**: kind cards (AI task / Script); name; optional description; AI: "What should the agent do?", agent, folder; Script: file picker that names the detected type; **When**: Once / Daily / Weekly / Monthly / Every few hours, weekday chips and time; a sentence that says exactly when it runs, the first run, and that a run is skipped if the Mac is asleep; Notify me toggle; **Advanced** (collapsed): AI model, time limit, custom cron (overrides When); Script: run with (interpreter/venv), arguments, command instead of a file, time limit.
- **Review sheet** (approval): who suggested it, everything it will run (prompt or file, interpreter, arguments, folder), schedule sentence; **Approve** / **Decline**.

## Architecture

- `mcp_server/schedule_rules.py` — schedules: simple forms (once/daily/weekly/monthly/every N hours) and a 5-field cron subset; `next_run(schedule, after)` in the Mac's local time; plain-language `describe(schedule)`.
- `mcp_server/scheduled_tasks.py` — SQLite at `paths.scheduled_tasks_db()` (`~/.mac-mcp/state/scheduled_tasks.sqlite3`, owner-only): tasks and runs; validation; approval state; retention (last 50 runs per task, 90 days).
- `mcp_server/scheduled_task_runner.py` — runs one task: AI via `spawn_agent` then waits for its result; script via a subprocess with the interpreter, folder, arguments and time limit, capturing the end of the output. Honors Settings › Privacy: paused observation skips AI runs; private mode keeps only the status.
- `mcp_server/scheduled_task_runner.py` (`Scheduler`) — the in-server loop: due → run (in a worker thread), overdue by more than 2 minutes → Skipped with the reason (asleep or Mac MCP was stopped), then the next run.
- Dashboard API (`/dashboard/api/scheduled-tasks…`): list, get, create, update, delete, pause/resume, run now, approve/decline, runs.
- MCP tool `scheduled_tasks` (list / create / update / delete / run_now): agent creations and edits become `pending_approval`; risk registry entry.
- Menu app: `ScheduledTasksPane.swift` (list, detail, sheets) and notifications for finished runs through `AgentNotificationController.scheduleMessage`.

## Testing

Unit: schedule math (DST, month ends, weekdays, cron), store validation and approval rules, scheduler due/skip decisions with a fake clock, runner for each script type and timeouts, API and MCP tool permissions. E2E on this Mac: a script task and an AI task created through the UI path, run on schedule and with Run now, an agent-created task waiting for approval, a skipped run after a simulated sleep, the notification, and the rendered screens. Idle CPU and wakeups measured before and after.
