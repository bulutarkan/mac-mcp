# Live activity island (#174) — design

Agreed with the owner on 2026-10-13 after a live prototype on the real notch.

## What it is

A Dynamic Island–style surface that grows out of the MacBook notch and shows what Mac MCP is doing right now. It is a **style of the existing live-activity surface**, not a new one: Settings › Activity & Notifications › Live activity is **Off / Bubble / Notch island**. The island is off by default; an existing install keeps its current choice (bubble on stays bubble, off stays off). Macs without a notch get the same island hanging from the top centre of the screen.

## What it shows

Priority: needs you > problem > work in progress > just finished > nothing (the island is hidden).

- **Work in progress**: MCP tool calls with their intent sentence and the AI app that made them (from the session label), delegated agents and teams (title, provider, current step, elapsed), scheduled task runs.
- **Needs you** (orange): security approvals waiting for Allow Once / Block, human checkpoints and questions (`browser_checkpoint`, `ask_user`), tasks an AI app suggested (Review).
- **Problems** (red): the Mac MCP server stopped answering; an agent or scheduled run failed.
- **Indicators**: private mode and paused observation show on the Mac MCP mark; while paused no rows slide down.

Never shown: prompts, results, page text. Only intent sentences, task and agent names and states.

## States and motion

Revised after the owner tried the first version: the island never appears and disappears, and ordinary tool calls never move it.

- **Rest** (always, while the style is on): two small wings beside the notch. Left: the Mac MCP mark (dimmed with a pause glyph while observation is paused, an eye-slash in private mode). Right: empty.
- **Working**: only the right slot changes, with the native icon of what is being used (Safari or Chrome for the browser, Terminal for commands, Finder for files, the app itself for app tools, a symbol otherwise) inside a thin spinning ring, plus a count when several things run. When work stops (no new call for 0.6 s) a green tick (red cross on failure) shows for 1.4 s, then the slot empties. The shape keeps its size.
- **Waiting**: an orange count in the slot while something needs the owner.
- **Peek** (3.5 s, 5 s for attention): a row slides down only for events that matter: an agent or scheduled run finished or failed, a new approval request, a task an AI app suggested (with Review).
- **Expanded** (hover, after 0.16 s; closes 0.26 s after the cursor leaves): a status header with Pause, Dashboard and Settings; Needs you; Now (with elapsed time); Recent (last work with the app that did it, when, and how it ended, seeded from the activity history); Scheduled (next runs). It opens with content even when idle.

Springs: open response 0.45 / damping 0.78; close 0.38 / 0.92. Content fades in 0.12 s after the shape opens and fades out before the shape closes, so the shape never snaps. Turning the style off fades the window out. Reduce Motion uses short ease curves.

## Behaviour

- Clicks outside the visible shape pass through; the island never takes focus from the active app.
- Hidden on screen lock; repositions on display changes; no per-frame timers (the ring is a Core Animation loop and stops when hidden).
- Agent and run transitions come from the data the app already polls; tool calls from the activity stream.

## Out of scope

Usage gauges, multiple notches, swiping pages, customisation beyond the style choice.
