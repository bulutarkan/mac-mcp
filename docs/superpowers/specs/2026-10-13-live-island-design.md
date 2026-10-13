# Live activity island (#174) — design

Agreed with the owner on 2026-10-13 after a live prototype on the real notch.

## What it is

A Dynamic Island–style surface that grows out of the MacBook notch and shows what Mac MCP is doing right now. It is a **style of the existing live-activity surface**, not a new one: Settings › Activity & Notifications › Live activity is **Off / Bubble / Notch island**. The island is off by default; an existing install keeps its current choice (bubble on stays bubble, off stays off). Macs without a notch get the same island hanging from the top centre of the screen.

## What it shows

Priority: needs you > problem > work in progress > just finished > nothing (the island is hidden).

- **Work in progress**: MCP tool calls with their intent sentence and the AI app that made them (from the session label), delegated agents and teams (title, provider, current step, elapsed), scheduled task runs.
- **Needs you** (orange): security approvals waiting for Allow Once / Block, human checkpoints and questions (`browser_checkpoint`, `ask_user`), tasks an AI app suggested (Review).
- **Problems** (red): the Mac MCP server stopped answering; an agent or scheduled run failed.
- **Indicators**: private mode shows a small eye-slash in the wing. While observation is paused the island stays quiet.

Never shown: prompts, results, page text. Only intent sentences, task and agent names and states.

## States and motion

- **Hidden**: nothing drawn; the window is ordered out.
- **Compact**: black wings grow from the notch. Left: the Mac MCP icon with a thin spinning ring while work runs, a green tick / orange or red badge when something ends or needs you. Right: elapsed time for one item, "N live" for several, "Done" / "Review" / "Stopped".
- **Peek** (about 3.5 s on an event): a row slides down under the notch: icon, title, one detail line, and a Review button for attention.
- **Expanded** (hover or click): Now (live rows), Needs you, Up next, and a footer with Pause observation, Dashboard and Settings.

Springs: open response 0.42 / damping 0.80, close 0.30 / 0.90. Content appears 0.10 s after the shape opens (0.18 s fade, 8 pt slide, slight blur); it leaves before the shape closes. Step changes slide in. Badges scale in. Reduce Motion uses fades only.

## Behaviour

- Clicks outside the visible shape pass through; the island never takes focus from the active app.
- Hidden on screen lock; repositions on display changes; no per-frame timers (the ring is a Core Animation loop and stops when hidden).
- Agent and run transitions come from the data the app already polls; tool calls from the activity stream.

## Out of scope

Usage gauges, multiple notches, swiping pages, customisation beyond the style choice.
