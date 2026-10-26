## Unreleased

### Mac MCP Chat: your own agents in a project folder
- A new Chat window (menu › Chat…, ⇧⌘N): pick a project folder and talk to Claude, Codex, OpenCode or GitHub Copilot, each signed in as you through its own CLI, with Mac MCP added for the Mac. Chats are grouped by project and continue where they stopped, also after a restart.
- The model, thinking level and mode come from the agent itself (your own account) and are picked under the message box; a long model list has a search. The pick is kept when the agent restarts.
- Answers stream in; each step is a small card that opens to show what it did; when the agent asks before changing something, the question is in the chat (Allow once, Always, Deny) and shrinks to one line once answered. Stop ends the answer.
- Its own look: warm paper and Mac MCP's champagne, the agent's logo on each answer, and a sidebar that slides away (⌃⌘S) with chats lined up under their project. Each answer starts with "Worked for 1m 12s"; the steps fold under it and open while the agent works, and Mac MCP steps show the app they used (Finder, Terminal, Safari). Under the answer: Copy, more (copy your message, ask again), the time, the model and the tokens the CLI reported. Answers render tables, code blocks with Copy, headings, lists and quotes; text keeps a reading width; a button goes back to the latest message; the gauge says context.
- Fixed: the scroll bar jumped while scrolling up through a chat (and the chat list): they were lazy lists that guessed the heights of rows not drawn yet, so the content changed height as you scrolled. Both are plain lists now and keep one height.
- The "/" command list works from the keyboard: ↑ ↓ to choose, Return or Tab to use, Esc to close.
- Chats working in the background keep their state in the sidebar (working dot, waiting for you), and a notification says when one finishes or asks before it continues; clicking it opens that chat. The chat list follows the server over its own event stream (`/dashboard/api/chats/list-events`).
- Delete Chat asks first. While the Chat window is open, Mac MCP is in the Dock and ⌘Tab; it goes back to the menu bar only when the window closes.
- File edits show their change: the step reads "Edited greet.py +2 −1" and opens to the diff, with added and removed lines in colour and Copy.
- ⌘V in the message box pastes an image (saved as a file and attached) or copied files; text pastes as before. Hover your message and click the pencil to edit it and send it again.
- Settings › Usage › Chat, simpler: one line for the total, the days as bars split by agent (hover for the day), the agents in the same colours with their share; a row opens to the models it used, and Sent, Written and From cache sit under Token details with what each means.
- Settings › Usage › Tools and Delegated Agents in the same language as Chat, with nothing left out. Tools: the total with one line on calls and active days, the days as bars split into results and arguments (daily, weekly or running total; hover for calls and failures), then calls with how many failed, the typical and slowest response, and payload with images and files, each spelled out; top tools with their share and failures named; memory and skills as facts with the skills used. Delegated Agents: the total in a sentence, one bar of each agent's share with its name and percent, and a row per agent with its logo that opens to input, cache read and write, output, reasoning, models and who reported them.
- Settings › Usage › Data & Storage as System Settings lists things: each setting with one sentence on what it does and its control on the right (Record usage says since when), and Memory opens with how many, the dates they span and the size on disk.
- Settings: the window no longer shows "Mac MCP Settings" over the sidebar, and Search Settings is a full-size field.
- Settings everywhere in the Usage tabs' language: every group is the same quiet box with its title above, and the type is one scale (title, text, note). General opens with Server, Version and Active agents as facts; Get Started numbers its steps under a progress bar; Permissions are choice rows with a symbol and a check; Providers, Connections › AI Clients and the Browser Companions show each tool's own logo or icon with a status dot; Activity picks Off, Bubble or Notch island from cards.
- Agents › General: Agents at Once, Messages Between Agents and Agent History are rows in one Running Agents box. Agents › Skills groups the skills by where they come from, with "5 of 5 on" per group and one line of description each.
- Connections uses the same tabs as the other panes and remembers the last one; Safari and Chrome are one list with their state as a dot (the separate "Safari status" line is gone).
- Voice: Voice Questions, Transcription and Audio boxes with switches and rows. Apps › Shortcuts has a filter, "N of M allowed" and a switch per shortcut; Apps › App Support counts the apps by how Mac MCP works with them and marks each with a coloured badge (Built in, Partly built in, By looking at the window).
- Privacy: Protection as switch rows with app icons for excluded apps, Documents with a folder list, and Your Data as a total with a coloured bar per place.
- Write while the agent works: the message waits above the box and goes out when the answer ends (kept on the server, so it goes even with the window closed). "Send Now" stops the answer and sends it; × takes it back. After Stop nothing goes on its own.
- Commands show their output as a terminal: Codex streams it live (the newest lines under the step while it runs), Claude when the command ends; the step then shows all of it with the exit code and Copy. The agents still run their commands themselves.
- Fixed: Stop while the agent was still starting waited 10 seconds and then shut the agent down; it now stops at once.
- Antigravity in Chat: Google's Antigravity ACP server (the `agy` CLI has no ACP mode) is downloaded at the first chat, pinned to a version and checked against its SHA-256, and signs in with your Antigravity Google account; model, thinking and mode come from Antigravity. Turn Antigravity on in Settings › Agents.
- Goose without a provider of its own starts on your OpenRouter key (as delegated Goose does), on a free model; its model, mode and thinking pickers are Goose's own.
- Code blocks are coloured by language (Swift, Python, JavaScript/TypeScript, Go, Rust, shell, SQL, JSON, HTML, CSS, YAML, diffs and more).
- Fixed: the app could hang in a chat after an answer (reported after `/mcp`): whether the transcript was at its end was read from which rows the list had loaded, and at some window sizes that flipped on every layout. It is now read from the scroll view itself; the "latest message" button shows whenever you are away from the end and the agent is idle.
- Fixed: the app could hang (one CPU core busy) while an agent worked in a long chat. The working dot's endless SwiftUI animation laid the whole transcript out again on every frame; it is now a Core Animation layer, turns are grouped once per change and only a turn that changed is redrawn (about 15–30% of a core while an answer streams, 1% idle).
- Streaming answers come in smoothly, the newest words faint and settling in; tables fit the answer's width with each row as tall as its longest cell; Codex's source notes (`:codex-file-citation{…}`) become links such as "Google Sheets ↗" or "README.md:12 ↗"; the Codex adapter's MCP start reports are no longer shown as steps.
- Add files and images to a message with the paperclip or by dropping them on the message box; images go to the agent as pictures (all four agents read them), other files as links the agent opens, and the sent message shows them (click an image to open it, a file to show it in Finder). Up to 10 files, 25 MB each.
- Live updates: the open chat follows the server over server-sent events (`/dashboard/api/chats/{id}/events`) instead of asking three times a second, and reconnects on its own after a restart or sleep.
- On/off settings (Fast mode, Copilot's allow-all) are named switches and the agent's other settings sit under a named "More" menu; a chat left without a message is removed when you move on; a streaming table's columns no longer shift; the transcript keeps to the newest words until you scroll up.
- More agents: Kilo Code, Kimi Code, Goose and Cline (turn them on in Settings › Agents); an agent that is not signed in says which command to run (`kimi login`, `goose configure`, `cline auth`, …).
- Copilot gets a model and thinking picker: the models its own CLI documents; picking one starts Copilot again with it and the conversation continues.
- Codex in a chat uses the chat's own Mac MCP, limited to the project (as `mac-mcp-chat`), with your `mac-mcp` entry and ChatGPT apps (your Mac MCP connector among them) turned off for it, as for delegated Codex agents.
- Rename a chat (right-click › Rename…, or double-click its title) and search chats in the sidebar (⌘F). Type `/` for the agent's own commands (`/compact`, `/review`, …) with what they do.
- Settings › Usage has a Chat section of its own: tokens, answers, chats and time worked in Mac MCP Chat by agent, model and day, apart from Delegated Agents.
- Agents speak the Agent Client Protocol: Copilot and OpenCode natively, Claude Code and Codex through their official adapters (`npx`). Mac MCP reaches them on a credential limited to the project folder; an agent whose own config already names a `mac-mcp` server keeps that one. Only agents turned on in Settings › Agents are offered. Dashboard API: `/dashboard/api/chats`.

### Maintainers: `mac-mcp promote`
- `mac-mcp promote` puts the source checkout's committed, pushed HEAD into the runtime between checkpoints, with the updater's runtime merge, backup, menu app rebuild, restart and health check, and restores the backup when a file does not compile or the health check fails. The runtime then records HEAD as its deployed commit, so the menu no longer offers a checkpoint the runtime already runs. `--dry-run` shows what would change. Dependency changes stay with `mac-mcp update`.

### Menu: hover captions that read
- The caption over a menu chip or row is drawn once over the whole panel, below its target (above it near the bottom), centred and kept inside the panel; short captions stay on one line and longer ones wrap. It fades in from its target and moves straight to the next chip; Reduce Motion keeps only the fade.

### Menu app: a calmer, plainer look (premium UI, phase 1)
- One set of colours, spacing, radii and motion for the whole native app, adapting to light and dark.
- Menu rows lose their bright icon tiles; colour now only means a state (blue working, red a problem).
- Callouts arrive, sections open and counts roll smoothly, and Settings panes change with a short fade; with Reduce Motion nothing slides or loops.
- Plain words: Connected, Partly working, Not connected; the Endpoint row is now Remote access (This Mac only, On, On but stopped, Your own address); hover captions count actions instead of tool calls.
- The menu bar mark shows a waiting question (M!) and paused observation (M‖) beside the M, besides stopped, working and idle; VoiceOver names every mode.

### Menu app: one card under the header (premium UI, phase 2)
- Only one card shows under the header, the most important first: Mac MCP stopped or not answering, then unfinished setup, then a new version.
- A stopped server says "Your AI apps can't use this Mac" with Start; setup reads "Get your AI apps ready · 2 of 3" with the next step, Continue and a bar per step; a new version is a compact row with its version and Update.
- The header says only "Ready" while idle; the line under the name is for news such as agents working.

### Agent teams: settings checked first, full dependency reports, no lost messages, team conversation (#104–#107)
- `spawn_agents` checks every team and task setting before it creates anything (#105): one 400 lists every problem with what is allowed ("3 problems in this request, nothing was started: …"), so a bad value never leaves an empty failed team. Non-integer numbers are a 400, not a crash, and the tool schema lists `result_style` values. A task that still cannot start marks the tasks that depend on it skipped at once, with the reason.
- A team task can read the full report of a task it depends on or reviews (#106): `get_agent(result_mode="full")` pages through it, and the task's prompt names those agents and says how. Tasks it does not depend on, other teams, and control actions stay closed. Team History and the team result show which task read which result.
- A message to an agent that already finished is no longer lost (#107): the sender is told (`delivery: recipient_finished`, when it finished, where copies went), and copies go to the lead and to the team's unfinished tasks that use that task's result. Messages an agent never saw before it finished are passed on the same way. A task that runs again still gets them. New kind `correction`. Forwarded copies do not use the sender's message budget.
- `team_messages action=history` shows a team's whole conversation, oldest first, with who was shown and who opened each message, without marking anything read (#104). Only the lead, the team's agents and the owner can read it; the owner sees it under Settings › Agents › Team History, in each team's Messages, with counts of messages that reached a finished agent or were passed on. Dashboard API: `/dashboard/api/agents/teams/{team_id}/messages`.
- A delegated agent reaches the Mac MCP that started it on that server's port (`MAC_MCP_PORT`), not always 8765.

### Memory and skills for delegated agents (roadmap #110, part 2)
- Agents Mac MCP starts get a short memory-and-skills section right after their task, since MCP clients need not pass the server's instructions on: look up the owner's facts with `memory_search` before searching files or saying something cannot be found, `skill_search` and `skill_get` before procedure work (design or UI, frameworks, file formats, review, release and report routines), and save a lasting preference, decision or correction with `memory_update` or `memory_add`, quietly. Only agents that can use those tools get it; work from outside content never saves.
- Read-only agents are told that saving to the owner's memory and team messages are allowed, so they no longer drop a preference they were asked to keep.
- A memory's `source` is text, not a path: `memory_add` with `source: "owner instruction"` was refused for scoped agents.
- `tool_invoke` accepts the name an MCP client shows (`mcp__<server>__<tool>`), and a read-only agent's `memory_add` or `memory_update` through `tool_invoke` is allowed like the direct call.
- Settings › Usage shows Memory and Skills: lookups and how many found something, memories saved, updated or refused for holding a secret, and the skills used most. Counts only, under the same usage switch and retention; nothing is counted in private mode.
- The secret refusal tells the caller to save the fact without the secret once, or skip it.
- A result envelope followed only by stray closing brackets (`{...}}`) is kept instead of failing the agent; any other trailing text still fails closed.
- Measured with the same three tasks before and after (default model and reasoning): an owner fact in memory went from 20 tool calls, 1.3M tokens, 173 s and no answer to one `memory_search`, about 112K tokens, 21 s and the right answer; a design task now uses the design skill; a "from now on" preference is saved once.

### GitHub Copilot as an agent provider
- `provider=copilot` delegates work to the GitHub Copilot CLI (`copilot -p --output-format json`), off by default like the other extra agents. It runs without a Copilot subscription on your own key: a model named `openrouter/<model>`, `openai/<model>` or `anthropic/<model>` uses that provider (OpenRouter's key is also read from the Keychain); other model names use GitHub Copilot with a GitHub token.
- Each agent gets a private Copilot home with only Mac MCP's server; Copilot's GitHub servers, subagents and workflows are off. Read-only and workspace-write runs are inside the Seatbelt boundary. Follow-ups continue the same Copilot session; run reports, the menu and the island show its commands, file writes and Mac MCP tools; Settings → Usage counts its tokens from its session journal.
- Mac MCP can be added to the Copilot CLI itself too (Settings → Apps, `mac-mcp connect --client copilot`).

## 2.3.0 r3 - 2026-10-22

**In short:** a signed checkpoint on the 2.3.0 line; the version stays 2.3.0. Agents can ask you a quick question in Mac MCP's own window (with one-click answers, files and pictures) and keep working whatever you answer, and the approvals before sending a mail or uploading a file open there too. Agents find your skills and memories and use them quietly. `personal_search` and `daily_briefing` read across your apps; the Photos, Contacts, Notes and Calendar adapters grow; web and Electron apps can be read and pressed down to their controls. Settings let you review memories, lessons and skills; the dashboard and phone get a new look; a running team shows its plan limits; your other Macs can get full access for a while. Update with `mac-mcp update` or the new package.

### Memory and skills where agents look for them (roadmap #110, part 1)
- `skill_search` and `skill_get` are in the default tool list, so agents see them without `tool_discover` (about 255 more tokens, 2.4% of the list).
- The memory and skill tools now say when to use them: `memory_search` when a request touches earlier work, a preference or a person or project the owner may have mentioned, and before asking them; `skill_search` before a task that has a written procedure (Swift, Expo, frontend and design, Remotion, Office and PDF files, ads and SEO, debugging, code review); `memory_update` when the owner corrects something remembered.
- Agents save and recall without saying so: they no longer announce "I saved this" or "from memory" and simply use what they know.
- A read-only agent may save what it learned with `memory_add` and `memory_update`; secrets and private mode are refused as before, and nothing else opens for it.

### Asking the owner without stopping the work
- `ask_user` is described as what it is: a quick question in the middle of the work. The agent waits up to `timeout_s`, then goes on with the answer, or with its own judgment when the owner skips or does not answer in time. It is meant for background agents, scheduled tasks and long jobs, instead of ending a turn to ask in chat.
- `ask_user` and `ask_confirmation` only show a question, so they are marked read-only: MCP clients no longer ask for approval before the question itself. Read-only and browser-only delegated agents may now use them too; the voice tools still may not.
- `ask_choice` is gone (MCP tool, `/api/interactive/choice` and `/api/v2/ask_choice`): `ask_user` with `suggestions` gives the same one-click answers, and the owner can still type a different one.
- `ask_confirmation`, and with it Mac MCP's own approvals before sending a mail or message and before uploading a file to a site, opens in Mac MCP's window too: the question and two buttons, no text field and no files. Only the confirm button approves; the other button, Skip, Esc and running out of time are all a no. Return presses neither. Without the menu app the macOS dialog is still used.
- Questions from several agents wait in a queue, and a question's time now starts when it comes to the front, so one you have not seen yet no longer runs out behind another. A question waits at most 10 minutes beyond its own time.

### Reading web and Electron apps down to their controls (roadmap #82, part 1)
- `mac_observe` now reads apps built on web views (the ChatGPT app among them) down to their buttons and text boxes. They wrap their page in many containers that hold just one thing; those no longer use up `max_depth`, and element ids keep their full path. Before, "New chat" and the message box of the ChatGPT app never showed at any depth.
- `max_depth` goes up to 16 (it was silently cut to 8); when a request asks for more, the result says what was used. The hint for a cut-off tree now says what can actually help.
- `mac_observe` and `mac_act` replies now keep to their 64 KB budget, which was only reported before. In a live run the ChatGPT agent read the ChatGPT app's whole tree on every step (140-430 KB a reply, 2 MB in all): Mac MCP worked for 9 seconds of a 146-second task, and the rest went into reading. Empty containers go first and each element drops fields an agent does not use (about half its size), then elements with nothing to press or read; long text is shortened only after that, to 1,500 characters. When it must cut, it keeps the message box and its form's buttons, then the page's text, before other buttons (a long sidebar goes first); element ids stay the same, and the reply says what was left out and to use `query`.
- `mac_observe(query='New chat | text area')` reads the whole window (with a larger node budget) and returns only the elements whose label, value or role has those words, with element ids for `mac_act`: about 8 KB instead of 176 KB for the ChatGPT app.
- Buttons in apps built on Chromium (the ChatGPT app among them) can be pressed in the background: such an app offers "press" on its web controls only after an assistive client asks for its full Accessibility tree, as VoiceOver does. When `mac_observe` finds web controls that cannot be pressed yet, it asks for that once per running app and reads again until they can be pressed, for up to 5 seconds (`web_accessibility: full_tree_ready`, or `full_tree_requested` when the app was slower). Browsers (Safari, Chrome, Firefox, Edge, Brave) are left as they are. Shown on the ChatGPT app: a new chat opened, the question typed and sent once, and the answer read, without taking focus.

### Updating from the menu: you see it start, run and end (roadmap #78)
- Before it starts, the menu says Mac MCP will close and open again by itself, so the app going away is not taken for a crash. While the update runs the menu shows a spinner, the step it is on (for example "Step 4 of 7") and the same note.
- The app that opens again keeps following an update still under way, and when it ends says once how it went, in the menu and as a notification: "Mac MCP updated to 2.3.0 r2", or that the update did not finish and Mac MCP went back to the version you had, with where the logs are.
- A passing network hiccup no longer rolls a good update back: the updater now waits for the restarted server's health check by time (up to two minutes) instead of giving up after thirty quick "still checking" answers, and stops at once when the check fails; the check of your public address keeps trying for up to 45 seconds, with growing pauses, before it counts as down.

### Browser: a draft shown again is not taken as saved (roadmap #81)
- When an agent checks for its text with `expect={text: ...}` and the page also keeps that text in the browser's own storage (as many sites do with drafts), the check now says so: `evidence: page_and_browser_storage`, `saved_on_site: unverified` and a hint that seeing a draft again, even after a reload, does not show the site saved it, so check the site's own list, another device or a private window, and do not submit again. Only where the text is kept is reported, never what is stored.

### Your other Macs: full access for a while (roadmap #80)
- When you invite a Mac with full access you choose how long: no time limit (as before), 1 hour, until the end of today or 7 days. The time runs from when you allow it, and the request you allow shows it.
- When the time is over that Mac keeps reading here: its pairing stays, and anything that changes something is refused, from its next request on. Only you give full access again, in **Settings › Devices** with **Access** next to the Mac; the other Mac cannot ask for it. The same menu lowers a Mac to read only or status only.
- Settings › Devices shows until when a Mac has full access, or that it ended. Macs you paired before keep the access they had.

### Your other Macs: read-only access writes nothing, by any path (roadmap #79)
- A Mac you gave read-only access can no longer run the tools of your own MCP servers (Notion, GitHub, …) here. Those tools say themselves that they only read, and this Mac cannot check that; with full access, and for your own apps on this Mac, they work as before.
- A read-only Mac's plan no longer saves a recipe draft on this Mac: it writes nothing here at all.
- Shown by behaviour: a read-only Mac's attempts to run commands, write files, send mail, make a POST request, start an agent or pass a call on to another Mac are refused before anything happens, also as a step of a plan; full access never goes past this Mac's own Permissions & Safety setting; and a Mac you remove is refused from its next request.

## 2.3.0 r2 - 2026-10-20

**In short:** a signed checkpoint on the 2.3.0 line; the version stays 2.3.0. A security fix: delegated Codex agents can no longer reach your own ChatGPT connectors (a Mac MCP one among them) past their sandbox. Agent teams can take a new task while they run, give each task its own provider and model, pick up where they stopped and coordinate with a guide that ships with Mac MCP; team messages are no longer repeated or lost; and the agent's browser tab glows while you look elsewhere. Update with `mac-mcp update` or the new package.

### Browser tabs: the glow stays on the agent's tab, and Chrome tab lists survive a closing tab
- The tab glow no longer lands on your own tab when the agent's tab closes while it plays and you have another tab with the same title; it stops instead.
- A short tab title (for example "Mac") no longer matches a longer, different tab, and the glow stops on any sign that you switched to the agent's tab.
- Listing Chrome tabs no longer fails with "Can't make id of tab id … into type Unicode text" when a tab closes at that moment; the list is read again.

### Security: Codex agents no longer see your ChatGPT connectors
- Codex 0.16x turns ChatGPT apps on by default, and a delegated Codex agent signs in with your ChatGPT account, so it also got your own ChatGPT connectors, including a Mac MCP connector. Through it, a read-only agent could run commands outside its sandbox and scope, as you. Delegated Codex agents now start with apps turned off; their only Mac MCP is their own scoped connection. Found in a live test, where an agent ran `sleep 5` this way.

### Team messages: fixes from a live team review
- A message an agent already opened in its inbox is no longer shown to it again as a note after its next tool call.
- A Codex agent in a team can no longer hang at start when Codex's app-server stays silent: setting up its message hook gives up after 20 seconds and the agent runs with its inbox.
- A Goose agent no longer loses a message when Mac MCP cannot write its message file: the message comes again with the next one, and the sender is not failed.
- A message hook whose setup fails half-way no longer leaves a live hook token behind.
- Delivery records are removed with the old messages they belong to.

### Agent teams: add a task while the team runs (roadmap #74)
- `agent_action(team_id, action="add_task", task={...})` adds one task to a team that is running or has finished, so new work found along the way stays in the same team instead of a new one. The task is checked like a `spawn_agents` task (id, prompt, `depends_on`, scope, resources, its own provider or model) and starts once its `depends_on` complete, with their results and, in an isolated Git team, their changes. Finished tasks are not run again.
- The team's plan has a revision (`plan_revision` in the team summary); pass it as `expected_revision` and a change someone made in the meantime returns a conflict instead of both winning. Only the session that started the team (or its parent) can add tasks; the team-size limit, its deadline and its budgets still apply, and a cancelled team must be resumed first. A reviewer gate (`review_of`) is still set only when the team starts.

### Agent teams: each task on its own model, and pick up a stopped team (roadmap #52, #51)
- A team task can name its own `provider`, `model` and `reasoning`, for example the builder on OpenCode and its reviewer on Codex in one team. Each is checked like the team's (on in Settings, installed, signed in, a valid model); a task on another provider gets that provider's defaults, never the team's model. Access stays one per team. The team summary shows each task's provider and model, and a retry keeps them.
- `agent_action(team_id, action="resume")` picks a stopped team up where it stopped: finished tasks stay finished, an interrupted task continues its own provider session from its checkpoint, a task that had not touched anything yet starts over, and one that already did something it could repeat is left as it is and listed. Tasks skipped because of it wait for it again.

### Agent teams: no skipped messages, and a guide to working together (roadmap #73, #77)
- An agent reading its team inbox gets the oldest unread messages first, a page at a time: with more unread than the page holds, the rest stay unread and `has_more` says so. Before, the newest ones were shown and the older ones were marked read without ever being shown. A note that arrives on its own says when more are waiting.
- Mac MCP ships a team coordination guide (the `mac-mcp-team-coordination` skill): when to message a teammate, whom, which kind, how to read the inbox and what not to send. Every agent can load it with `skill_get`, whatever CLI it runs in, and each team task now starts by pointing to it. A skill of yours with the same name wins.

### The agent's browser tab glows while you look elsewhere (roadmap #79)
- When an agent works in a Safari or Chrome tab you are not on, that tab glows briefly in the tab bar each time it acts: a pale blue wave sweeps across it twice and fades, about 1.6 s. The tab you are on never glows, and switching to the agent's tab stops it at once.
- The glow follows its tab while it plays and comes back above the browser after you click in it; when tabs open or close beside it, that pulse stops and the next one finds the tab again. It shows on whichever desktop the browser is on, ignores the mouse, never takes focus and stays out of screenshots. Reduce Motion gets a still tint.
- On by default; **Settings › Live activity › Glow on the agent's browser tab** turns it off.

## [2.3.0] - 2026-10-20

**In short:** agents can use your other Macs and your iPhone; what they change in Notes, Reminders, Calendar and Mail can be undone; Safari uploads ask you first and uploads are checked for secrets inside the file; a browser click can wait for the file it downloads, Chrome opens tabs in the window you ask for and saved logins work inside sign-in frames; Claude Desktop keeps its connection and its key; and many reports that said "done" too early now tell the truth. Install with npm, `install.sh` or the package; existing installs get it with `mac-mcp update`.

### Your other Macs (#147)
- **Settings › Devices** connects your Macs so an agent on one can use the others: run commands, read and change files, use apps and the browser there. No server in between: each Mac is reached at its own address (Cloudflare, ngrok or a custom HTTPS URL, whichever it uses).
- Pairing: the Mac to be added shows a one-time link, or its address and an 8-character code (2 minutes); the other Mac presents it; its owner gets a notification with who asks and a 6-digit check both Macs show, and allows or refuses in Settings › Devices. Only then is a credential issued. The Mac MCP API key is never shared.
- Three access levels: **Full access**, **Read only** (tools that change something are hidden and refused) and **Status only**. A Mac's own profile in Permissions & Safety comes first: full access on a Read Only Mac reads only, and both Macs say so.
- **Busy**: one toggle stops a Mac taking anything from other Macs without removing them; the others show it in orange.
- Agents use the `fleet` tool: `list`, `tools` (or a search of that Mac's tools), `call`, `recipes`, `run_recipe`. Everything runs on the other Mac under its own permission profile and approvals. Either Mac can end it at any time.

### Your iPhone
- **Settings › Devices** (formerly Macs) now has an **iPhone** section, next to your Macs; each folds away. Agents use your iPhone through iPhone Mirroring: no app on the phone, no cable.
- Access is yours to choose: **Full access** (tap, type, scroll, open apps), **Read only** (the default: see the screen and read its text) or **Off**. This Mac's own Permissions & Safety setting still comes first, and Privacy's pause and excluded apps apply.
- The `iphone` tool sees the screen (no picture when nothing changed), reads its text with positions (Apple Vision, Turkish and English) and, with full access, runs a whole job in one call with `act`: open an app, tap a text, type (Turkish letters included), scroll, swipe, wait for a text, and get the screen back after the last step, or where it stopped. A closed iPhone Mirroring is opened and waited for, and opening an app waits for the screen instead of fixed pauses.
- Fewer calls and fewer words: `region` reads only part of the screen (Vision reads just that part), `find` looks for several texts at once, `tap_text` can be kept to a region, and `expect_text` checks the screen inside a job, so sending a message with its checks takes one call instead of five.
- `open: true` opens a row in Files (and similar lists) that the first tap only selects, tapping again only when nothing changed. Zooming uses `double_tap`: iPhone Mirroring passes no pinch, not even a real trackpad's.
- The agent cursor shows where the iPhone is touched, labelled ("Tapping", "Typing", "Scrolling"). `back` taps the app's own back button; `key` takes ⌘/⇧/⌥/⌃ for an app's shortcuts, and refuses the ones iPhone Mirroring keeps (⌘W would close its window).
- `close_app` closes an app from the app switcher, touching only a card labelled with its name; `swipe` is quick by default, since iPhone Mirroring takes a slow drag as a tap.
- While the agent taps or types on the iPhone, the owner's pointer is hidden and only the agent cursor shows; it reappears where it was.
- Fixed: Spotlight could lose the first letter of an app's name when typing began before its field was ready.

### Undo for changes in Notes, Reminders, Calendar and Mail (#259)
- What an agent changes in your apps can be undone, like file changes. A changed note, reminder, event or email gets its old state back; a note, reminder, event or draft Mac MCP made is removed (notes go to Recently Deleted); a deleted reminder is made again (new id). The change's result carries an `undo_id`, and `mac_app` `action=undo` puts it back.
- Undo never overwrites an edit made since: if the item changed after Mac MCP changed it, nothing is changed (`force=true` overrides, classed as destructive). Notes with attachments are not recorded, since writing their old text back could lose them.
- Sends and shortcut runs say they cannot be undone. Recording adds no question and never stops a change.
- The dashboard lists app changes next to file transactions with the same Undo button, and change summaries now say what changed ("Changed note", "Deleted reminder") instead of "Used app adapter", without the item's text. Finding, listing and opening no longer count as changes.
- Records are owner-only, last 7 days (at most 200) and drop their copy once undone; `MAC_MCP_APP_UNDO=0` turns them off.

### Browser automation: downloads, windows, frames and waits (roadmap #33–#37)
- A click can now wait for the file it downloads: `expect={download: true}` (or part of the file name) on a `browser_act` click returns the finished file as an artifact. It looks from the moment the click began, so a download that finishes at once is no longer missed; a separate `browser_wait_for_download` that runs out of time now says `timed_out` (#34).
- Chrome opens a new tab in the window you ask for (`window_index`, or the window an agent already works in) instead of the last focused one. If the Chrome companion is too old to do that and the tab lands elsewhere, it is closed again and you are asked to reload the companion (#35).
- `browser_sign_in` fills a login form inside a child frame in Chrome (`frame` = part of the frame's URL), such as the Apple sign-in on icloud.com. The login is chosen for, and checked against, the frame's own site, never the page around it (#36).
- `network_idle` also counts requests that were already running when the wait began, once they finish, and its result says the network was watched from the start of the wait (`network_scope`) (#33).
- A Safari upload whose selector does not point to a file input now fails at once, before you are asked to bring Safari forward [SEC-FOCUS-001] (#37).

### Claude Desktop: one key, no dropped sessions
- Mac MCP always takes `MCP_API_KEY` from its `.env`. An older key still exported in a shell used to win when the server was started from that shell, so every app using the `.env` key was refused; the log now says when the two differ, without showing either.
- MCP sessions forgotten after 30 idle minutes: `mac-mcp stdio` (what Claude Desktop runs) already sets them up again by itself; for clients that do not, such as `mcp-remote`, the limit is now `MAC_MCP_SESSION_IDLE_TIMEOUT_S` in `.env` (60 seconds to 1 day).
- `mac-mcp connect-config --client claude-desktop` no longer says Claude Desktop is unsupported; it points to `mac-mcp connect --client claude-desktop` and shows the entry it adds.

### Safari Visual Companion shows again
- The Safari extension is signed sandboxed again: macOS ignored it when the app was signed with a development identity, so agent cursor, click effects and the activity sidebar did not appear in Safari.

### Agent teams and disk space (roadmap #47–#50)
- A team task left half-started by a server stop takes over the agent that did start, instead of failing.
- When an agent's worker dies, its CLI is stopped (then forced after a few seconds) before the agent counts as failed and frees its place.
- An agent that fails before writing a typed result says its result is missing, instead of pending for ever.
- A team held back by memory pressure starts again by itself once the pressure passes.
- Finished agents drop the caches in their private provider folders (npm and CLI caches, temp files, Codex plugin copies and the copy of the Codex sign-in); conversations and settings stay. On one Mac that freed 2.4 GB.

### Live Island: a heads-up before the iPhone takes your pointer
- The first time a chat or agent drives the iPhone (`iphone` `act`, directly or through `tool_invoke`), the Live Island opens a short note: "iPhone Mirroring · Each step briefly takes your pointer and keyboard." It shows once per session for about five seconds, asks nothing and stops nothing. Looking at or reading the iPhone screen (`see`, `read`, `find`) takes neither, so it shows no note.

### Activity bubble names its lanes (roadmap #26)
- When several pieces of work run at once, each row of the tool activity bubble says where it comes from: the chat app ("ChatGPT", "Claude Code") or the agent's provider and task ("Codex · Fix the tests"). Before, every row was "Agent 1", "Agent 2". Rows the app cannot place are "Session N" or "Agent N".
- The header counts by kind ("2 sessions · 3 agents") instead of calling every row an agent, and more than three rows scroll instead of ending in "+2 more working". VoiceOver reads every row. The single-row bubble shows the source too ("Mac MCP · ChatGPT").

### Answered questions leave the Live Island; readable REST activity (roadmap #25)
- A question answered (or expired) while the Live Island shows it as a peek leaves at once, and one waiting in the peek queue no longer comes up later. Before, the island kept saying "Waiting for your answer" until the peek's five seconds ran out. Agent and scheduled-task peeks keep their normal time.
- A REST v2 call that gives no description shows the tool in words in the menu, the island and the dashboard ("Browser upload artifact via the REST API") instead of "REST v2 browser_upload_artifact".

### Agent cursor click effects and Live Island rows (roadmap #27, #23)
- The desktop agent cursor and the browser Visual Companion (Safari and Chrome) show a click only for the event that clicked. A quick next step (a scroll, a read, another move) cancels a press still waiting for the arrow to arrive; before, its ripple played later at a spot the cursor had already left.
- A "Waiting for your answer" row in the Live Island is plain text now. The question's own dialog already sits above every window, and the row did nothing when clicked while VoiceOver said it opened something. Rows that lead somewhere (an agent, Settings, the dashboard) still open it.

### Activity bubble and Live Island (roadmap #24, #29)
- The tool activity bubble stays when new work starts while it is fading out after the last batch: the fade turns around and the new work shows. Before, the end of the fade hid the bubble and cleared the new work.
- VoiceOver reads the Live Island mark with every mode its badge shows, in every island state: "Mac MCP, private mode", "Mac MCP, observation paused", or both. Before, it said only "Mac MCP" when Private mode was on.

### Sign-in checkpoints, browser_do and Goose messages (roadmap #30, #32, #46)
- A Safari sign-in checkpoint keeps its tab when the person is sent to another site (an identity provider, then back) and Safari gives the tab a new process. Before, `wait`/`status` could answer `tab_closed` for a tab that was still open. The same guarded rebind as after Mac MCP's own navigation applies (same window and position, same tab count, old process gone), kept open while the checkpoint lives.
- `browser_do` with `close_after` and no `url` (or `new_tab=false`) is refused before anything runs. Before, the actions ran on the current tab and the call failed afterwards.
- A Goose agent no longer loses team messages that arrive between two of its turns: its message file now shows the recent messages, newest first (up to 20), instead of only the latest batch.

### No more false "done" reports (roadmap #38, #39, #40, #63)
- Calendar free-time search counts repeating events that began more than six years ago; before, a weekly meeting started in 2018 left its slot looking free.
- Mail's sent check matches the subject and every To address, and only counts a message that was not already in Sent before this send. An earlier email with the same subject no longer marks a new one as verified.
- iPhone `open_app` fails with `IPHONE_APP_NOT_OPENED` when Spotlight finds no app by that name and the Home Screen comes back, instead of reporting success and letting the next steps run on the wrong screen.
- CLI exit codes tell the truth: `mac-mcp uninstall` exits 1 and says so when the server or tunnel did not stop, `mac-mcp update --check` on a package install exits 1 (`"ok": false`) when the check could not reach GitHub, and `mac-mcp autostart on` exits 1 when its login job could not be loaded.

### Four privacy and security fixes (roadmap #31, #62, #54, #55)
- Saved logins are no longer filled into sign-up or password-change forms. When a page shows only new-password fields, `browser_sign_in` fills nothing and says `not_a_sign_in_form`; before, the last fallback could pick such a field.
- A REST key limited to some folders now stays inside them on the older `/api/...` routes too: `search_files` without a path searched the whole home folder, and `spotlight_search` was not filtered. Those routes now see the key's profile and folders, as `/mcp` and `/api/v2` already did.
- Turning Private mode on while a scheduled task runs now keeps that run's output and summary out of its record, as when Private mode was on from the start.
- A folder removed from the knowledge folders stops showing in `knowledge_search` at once, even when the index is busy and its rows are only dropped by the next scan.

### Uploads are checked for secrets inside the file (#236)
- Before a file goes to a web page, Mac MCP now reads the file itself (up to 8 MB) instead of judging it only by its path. A key, token or password in a file with an ordinary name (`notes.txt`, a picture), or a value the agent read earlier from a secret file, gets the same Allow Once / Block question a sensitive path already got. Files without them upload as before. No new kind of question.
- The check runs off the server's main loop, so a large file does not hold up other calls.

### Safari uploads ask you first [SEC-FOCUS-001]
- When an agent wants to upload a file in Safari, Mac MCP asks you in a dialog that names the file, its size and the site. Allow brings Safari forward for that one upload (its Open window picks the exact registered file) and puts your window back; Don't Allow, or no answer within a minute, tells the agent you declined and it carries on with the rest of its work. Before, Safari uploads were always refused. Chrome uploads still happen in the background without asking.

### Fixes
- A Mail draft made by `create_draft` keeps the message id it returns: Mail saved the draft again under a new id when the compose window closed, so `update_message` and `open_message` could no longer find it. A draft whose id Mail had not assigned yet no longer fails as `MAIL_DRAFT_FAILED` after the draft was in fact saved.

## [2.2.1] - 2026-10-19

**In short:** agents that work at the same time can now talk to each other, whichever CLI they run in; agents can work on a desktop of their own; Mac MCP reads and searches Messages; GitHub, Notion, Slack and Google Drive connect from Settings in a few clicks, and your own MCP servers are offered to every agent; your documents (scanned ones too) can be asked about; scheduled tasks run when a file arrives, keep to a usage limit and say when a run went wrong; memory learns on its own; and Settings is easier to read and find your way in. Install with npm, `install.sh` or the package; existing installs get it with `mac-mcp update`.

### Agents that work at the same time can talk to each other (#208)
- The agents of a team, the session that started it (the lead) and Mac MCP sessions in a named room can message each other with the new `team_messages` tool: send to a task, a role, `lead` or `everyone`, read the inbox, list members.
- Messages reach an agent on their own while it works, after its next tool call, even when it only uses its CLI's own tools and never calls Mac MCP: through Claude Code, Codex, Grok Build and Cline hooks, OpenCode and Kilo Code plugins, a pi extension, Goose's message file and an Antigravity hook. Kimi Code is asked to check its inbox every few steps. Nothing is interrupted, and each message is shown once, however it arrives.
- Every Mac MCP session sees the tool and is told about it, so a lead can pass on a change of plan to running agents instead of restarting them. A message from the lead updates the agent's task; messages from other agents are information, never instructions.
- Agents are asked to write only when a teammate's work would otherwise go wrong or to answer a question, not with progress reports, and may send at most 10 messages to their team. When nobody will see a message (its agent has finished), the sender is told.
- The hook holds a messaging-only token, not the agent's Mac MCP credential, and everything is removed when the agent ends. Your own `~/.codex` and other CLI settings are never changed: each agent gets private copies.
- **Settings › Subagents › Messages Between Agents** turns it all off and on (on by default).

### Agents can work on a desktop of their own
- When you ask an agent to work apart, it can take a desktop of its own in Mission Control and use your real apps and accounts there while you keep working on yours. It reads which displays and desktops you have and what is open on them, picks an empty one (or adds one) and opens apps straight onto it: nothing appears on the desktops you are looking at and your focus stays put. Your windows are never moved or closed.
- Safari and apps you are using open there too: their new window is caught before it is drawn and moved to the agent's desktop. Agents still work alongside you by default.

### Messages: read and search conversations, read-only (#261)
- `mac_app` Messages can read a chat (newest first, in pages) and search messages across chats or in one, in any case and with or without accents, without opening Messages. `find_chats` lists your chats even while Messages is closed.
- The text is read by Mac MCP Messages Reader, a small helper you give Full Disk Access; it opens only the Messages database, read-only. Settings › Apps › Reading Messages shows its state and the steps to turn it on.

### Connect GitHub, Notion, Slack and Google Drive in a few clicks (#260)
- **Settings › MCP Servers › Connect a Service** lists these services' own hosted MCP servers, each with its logo. GitHub takes a token that is tested before it is saved; Notion signs in through the browser; Slack and Google Drive use an app of your own. Their tools are then offered to every AI app and agent.
- Keys stay in your login Keychain, never in `mcp_servers.json`. A background reconnect never opens a browser; it says when a new sign-in is needed. A new sign-in replaces the saved one only once it finishes, and Disconnect revokes and deletes everything.

### Settings that is easier to find your way in (#250, #263)
- The sidebar groups its sections under a few headings and scrolls by itself to the section a link opens, also in a new window and in the smallest window size. VoiceOver reads the headings in normal case.
- **Settings › Agents** has two folding sections: **Providers** and **Team History**. Each past team shows how many of its agents succeeded, when it ran, its agents, tokens, tool calls and time; opened, each agent has a token bar and a tool-call bar. Only counts are kept: no prompts or results.
- **Usage** leads with one overview of Mac MCP's own tools; the rare settings are folded away under Data & Storage.

### Agent Skills from your CLIs
- Agent Skills installed for your CLI agents (for example in `~/.claude/skills`) are found and indexed on their own, quickly and without holding up search, so `skill_search` finds them too.

### Fixes
- An agent's typed summary, which `wait_agents` shows the lead, now carries the agent's answer in the language it answered in, instead of an English description of what it did.
- Two agents starting at the same moment on a fresh install no longer make one of them fail with "database is locked".
- An external MCP tool with a `description` or `idempotency_key` parameter no longer breaks the tool list.
- On Python 3.10, an error from a connected MCP server is reported instead of stopping the connection code with a NameError (2.2.1 r2).
- On Python 3.10 and 3.11, connecting your own MCP servers and services (Connect a Service, the Test button, `mac-mcp mcp test`) works instead of failing with "Attempted to exit cancel scope in a different task" (2.2.1 r3).
- On Python 3.10 to 3.12, `find_files` with a folder in its pattern (`**/*.md`, `docs/*`) works instead of failing (2.2.1 r3).

### Permissions & Safety in plain words (#246)
- Settings › Permissions & Safety now starts with two plain answers you can change in place: **What AI apps can do** (Trusted, Standard or Read Only, each said in one sentence) and **When Mac MCP asks you first** (Off, Critical or High Risk, likewise). Saved logins follow; the policy terms (allowed capabilities, destructive families, the agent access ceiling, headless behavior, timeouts) are under **Details**. Warnings about an invalid setting stay at the top.

### Settings shows what agents can do in each app (#262)
- Settings › Apps now lists every app Mac MCP knows, straight from the server's own list: whether it is **Built in** (Mac MCP works with the app directly, even while you use it), **Partly built in**, or used **by looking at the window**, and, when you open a row, what agents can read, open, change, send (asking you first) or run there. A new app action shows up by itself; the old hand-written summary is gone.

### The menu tells conversations from agents (#248)
- The menu's **Sessions** section is now **Conversations**, and its rows say **Chat 1**, **Chat 2** instead of "Agent 1", so they are not mixed up with the agents you hand work to under **Agents**.
- Sending a message says what happens next in plain words: "Sent. This conversation will see your message before its next step." instead of "the agent's next tool will be preempted"; the other messages, the details popover and VoiceOver labels follow.

### Cut-off installs and updates put themselves right (#241)
- A package install that is cut off half way (the Mac shut down, the installer was force quit) leaves a record before it moves anything. Opening the package again puts the previous version back from it and then installs, instead of refusing because Mac MCP looked half installed. The copy of the previous app a cut-off upgrade kept is not overwritten by the retry.
- An update that stops while the menu bar app is being replaced is recovered by the next `mac-mcp update` with the app rebuilt from the restored runtime, so the app and the server end on the same version. Signature and release checks are unchanged.

### Log rotation problems are no longer silent (#244)
- If a log can't be rotated (for example its permissions changed), the server log says which one and why, the other logs are still rotated, and `mac-mcp doctor` shows a warning naming it and since when, because that log can keep growing. If the rotation thread can't start, the next start tries again.

### Less needless work on frequent paths (#255)
- `find_files` reads a folder tree only as far as a page needs, in the same order and with the same matches as before: the first 200 of 50,000 files now take about 7 ms and under 1 MB instead of about 1.6 s and 36 MB, and the next page skips the folders it already passed.
- Scheduled tasks that watch the same folder share one read of it per pass, instead of one read per task.
- Live Island asks for a short view of your scheduled tasks (name, state, next run, last run, a suggestion waiting) instead of every task's full prompt and settings each time it refreshes.

### A dashboard left open costs less (#254)
- The security record's check no longer re-hashes every event each minute. It walks the whole record on the first check and at least every 10 minutes; checks in between read only new events (about 13 ms instead of about 3 s with 500,000 events). Any edit or delete of an earlier event is still caught on the very next check: SQLite triggers count them, and a changed count, a missing trigger or a pruned record sends the check back to a full walk.
- The dashboard summary is recomputed at most every 5 seconds on a busy server, its cache keeps 8 time windows, and its slowest-call figure is read as one row instead of every duration. A burst of finished calls refreshes the summary once.
- The attention card follows the live event stream and polls every 4 seconds only while work runs (30 seconds when idle), instead of 900 requests an hour.

### Cut results say so and can be continued (#252)
- `search_files` returns about 50,000 characters of whole matching lines a call, with `total_matches` (every match, not only the ones shown) and, when more follow, `has_more` and `next_offset` to pass as `offset`. Before, it cut mid-line and the count was only of what fit.
- `spotlight_search` returns at most 500 paths a call, says how many it found in all (`total`) and continues with `offset`.
- `scheduled_tasks` lists in pages of 50 (up to 200) with the total, a count per state and `next_cursor`; `brief: true` leaves prompts and scripts out; the new `action: "get_run"` with a `run_id` gives one run's full summary, output and details (`get` shows the last runs in short).

### Faster, self-repairing document search (#242, #253)
- Searching folders with many documents is faster: Mac MCP reads the text of only the best matches instead of every candidate (a search over 100,000 passages went from about 0.5 s to under 0.2 s), and indexing no longer counts the whole index once per file.
- Very large folders are walked less often while nothing happens (idle scans stay near 1% of a core), and a search no longer waits for a long walk: it answers from the index and the refresh runs in the background.
- If the index file is ever damaged, Mac MCP deletes it and rebuilds it from your folders by itself; the search that found the damage says so. A file that fails to index keeps the text it had before, instead of dropping out.

### Scanned documents are searchable (#258)
- Folders Mac MCP can search now read scanned PDFs and photos or screenshots of pages (PNG, JPEG, HEIC, TIFF) with the Mac's own text recognition, on this Mac, in Turkish, English and other languages. Pages that already have text are read as before; a result read from an image says so (`ocr: true`) so the AI app can mention that it may hold recognition errors.
- Scans that were found "without text" before are read once more on the next refresh, by themselves.

### Recipes on a schedule (#257)
- A Scheduled Task can now be a **Recipe**: pick a saved recipe and its values, and it replays those steps at the time you set or each time a file arrives, with no AI model asked (no model cost, the same steps every time). Values can hold `{file}` or a date word such as `{today}`.
- The recipe runs through the same path as running it yourself (your permissions, checks and its run timeline). If the recipe is paused, still a draft or deleted, the run fails and says why, and the task card shows it. A recipe task an AI app suggests waits for your approval, showing which recipe and values.
- AI apps can create them with `scheduled_tasks` (`kind: "recipe"`, `recipe_id`, `values`); `missed: "run_once"` can now actually be passed to that tool too (it was described but not accepted).

### Smaller fixes (#243, #249, #251)
- **Tests give the same result in any terminal.** Every run starts from the same settings CI uses (a fake API key, sign-in required, shell allowed) and drops runtime settings from the shell; the test state paths always point at a throwaway folder. `MAC_MCP_TEST_AMBIENT_ENV=1` keeps the caller's environment on purpose.
- **Settings say what they change.** Choosing the Bubble says it turns on Require Tool Descriptions and that AI apps should reconnect. Settings › Agents explains that it chooses which installed AI tools Mac MCP may hand work to, and says "Found on this Mac" instead of developer terms.
- **Clearer tools for AI apps.** `spawn_agents` says budgets gate new starts and running agents may overshoot; `mac_app` names Contacts and Shortcuts; `computer_plan` shows what a step looks like; asking `tool_discover` how to sign in with a saved password now finds `browser_sign_in`, while 2FA codes and captchas still go to `browser_checkpoint`.

### Setup that doesn't wait for permissions you don't need (#247)
- Get Started is three steps: start Mac MCP, connect an AI app, try a first request. Accessibility and Screen Recording are optional rows that say what each one is for (clicking and typing in Mac apps; looking at a window), so someone who only works with files, the terminal or the browser sees "All set" and the menu stops reminding them.
- The first-run tour asks for them after the first request, as an optional step with **Skip for now**.

### Scheduled tasks can catch up after sleep (#256)
- A task can **Run it once when the Mac is back** (a switch under When): a run missed while the Mac slept or Mac MCP was off runs once when it is back, marked **Catch-up**. A long sleep starts one run, never one per missed time; if a run is due right then, that run covers the missed ones. Off by default, so tasks skip missed runs as before. AI apps can set it too (`missed: "run_once"`).

### Fixes from the repository review (#237, #238, #239, #240, #245)
- **Memories saved at the same moment are all kept.** Every change to the memory files now waits its turn, across threads and processes; before, two saves at once could each rewrite the day's file and one memory was lost.
- **Secrets stay out of memory tags and sources too**, not only the text, on save and on update. The audit log now keeps a failed call's status and error code, not its message, which could hold a file path or other caller text.
- **A file that arrives while a folder task is running is no longer dropped.** It waits until the run ends; a download (Safari, Mail, AirDrop, Messages: files macOS marks as downloaded) then starts the task, and a file written on this Mac during the run is taken as the run's own output, as before.
- **A scheduled script still running when Mac MCP restarts is found and stopped.** Its process and start time are kept while it runs; after a restart the run says its script was stopped (or that it could not be stopped), and a different process that reused the number is never touched.
- **What the app says matches what it does:** Private mode says the security log still records tool names and outcomes (it no longer says nothing is kept); Privacy says "try again" when Mac MCP answered with an error and "start it" only when it is stopped; Usage calls its total **Tool data processed**, an estimate, not your AI app's model tokens or bill.

### Memory that learns on its own
- Your AI apps now remember what matters without being told to: a preference you state or show, a correction, a decision, or a lasting fact about your work, people, projects or setup. They check what they already know first and update an old memory instead of adding a near-copy, and mention briefly at the end what they remembered. After solving something non-obvious they record a lesson so the next run starts from it.
- Mac MCP itself refuses to store passwords, API keys, tokens and private keys in memory (on save and on update), keeps an identical memory once, and private mode still saves nothing. `memory_add` and `memory_update` are now in the default tool list, and the connection instructions say what to remember and what never to.
- The instructions every AI app gets on connecting also have a short "which tool for what" map: your documents, Mac apps, memory and lessons, recipes and scheduled tasks, delegated agents, asking you, and your own MCP servers.

### Ask about what is in your folders (#197)
- Settings › Privacy has **Folders Mac MCP can search**. Add a folder (Contracts, Invoices, Notes…) and your AI apps can ask about its contents with the new `knowledge_search` tool: answers come with the file and, for PDFs, the page. It understands meaning across languages ("when does the lease end" finds "Sözleşme 1 Ocak 2027de biter").
- Everything happens on this Mac: text files, PDFs page by page and Word/RTF/OpenDocument/HTML files are read locally into a private index (owner-only, listed under What Mac MCP keeps, where it can be moved to the Trash). New and changed files are picked up within minutes; deleted or renamed files and removed folders leave the index. Scanned PDFs without text are counted, not read (no OCR yet).
- The whole home folder, the disk, system folders, ~/Library and Mac MCP's own data can't be added. Agents with a limited scope only get results from inside it, and private mode pauses indexing.

### Know when an update is ready (#232)
- Mac MCP now looks for a new release in the background, a couple of minutes after the app starts and then every six hours, and says so the same way however it was installed (install.sh, npm or the .pkg): one "A new update is ready" notification per release, a row at the top of the menu with an **Update** button, and the same words in Settings › General. It only looks; nothing installs until you click Update.
- Behind the button, install.sh and npm installs run the signed updater as before; a package install opens the newer package's release page. A package install's check now really compares its signed checkpoint with the newest GitHub Release that has a .pkg (before, it only pointed at the Releases page), and checking no longer opens the browser by itself.
- Settings › General › Updates has **Let me know when an update is ready** (on by default); turning it off stops the background check. The Updates card shows the installed version without the commit; versions and commits stay under Details.

### Install with npm (#210)
- `npx @bulutarkan/mac-mcp install` installs Mac MCP with the repository's own installer (signed-release verification included). The npm package runs nothing when it is installed, leaves an existing install alone, and passes `update`, `status` and the other commands to the installed `mac-mcp` CLI by its full path; its command is `mac-mcp-setup`, so it never shadows `mac-mcp`.
- `scripts/build_npm_package.sh` packs it with a fresh copy of `install.sh` and refuses a version that differs from the product's or any npm script.

### Settings › Automation: Scheduled Tasks and Recipes in one place
- The sidebar's **Scheduled Tasks** is now **Automation**, with two tabs: Scheduled Tasks (as before) and **Recipes**, so Settings doesn't grow another item.
- **Recipes** lists every saved recipe, drafts included: its steps in words, the values it asks for, how many times it ran and how the last run went. Turn a draft on after checking it (a step with a secret in it is still refused), pause, resume, delete, run it with its values, or copy its Shortcuts link.
- With no recipes yet, the tab explains what a recipe is and gives example requests to ask your AI app.
- An (i) on each tab explains it in a few lines: what Scheduled Tasks and Recipes are, how they differ, and what keeps you in charge.
- `macmcp://settings/recipes` opens the Recipes tab; `macmcp://settings/scheduled-tasks` keeps opening Scheduled Tasks.

### Scheduled Tasks: approved scripts stay as approved, and data reaches the agent masked (#179)
- Approving a task an AI app suggested now shows its script's contents and binds them: if the script (or its data step script) changes later, the task stops and asks for a review with the change shown; a change during the review is refused. Your own tasks and files you pick are not bound.
- Secrets a data step prints (tokens, keys, passwords) are masked before the agent sees the data; the agent is told, and the report says so.

### Scheduled Tasks: know when a run went wrong, and why (#225, #226, #227, #228)
- **Runs that never started:** the app asks Mac MCP once a minute, outside the scheduler, and says "<task> didn't start" once when a run is more than 5 minutes late; the card shows "Didn't start at 09:00". After the Mac wakes or Mac MCP comes back, the scheduler gets those minutes to catch up first. If Mac MCP stops answering while it should be running, a run that came due gets "<task> may not have run".
- **A run stopped at its time limit keeps its steps and usage** (they were lost before), and every run that didn't finish says what is partial and that outside effects are unknown.
- **Checks outside this Mac:** a check can now fetch a URL (a status and, optionally, text, with no headers) or call one of your MCP servers' tools that is read-only by both its own annotation and Mac MCP's risk entry. Late text is looked for twice more; what couldn't be looked at is "Couldn't check", never a pass. Nothing is written or sent again.
- **Resume:** an AI task with a data step that failed after the data step finished can go on from the agent step: the data step is not run again and the agent gets the data it printed. Only when the task hasn't changed and the data was kept exactly; once per run; you are told the agent's earlier actions are unknown. The failed run stays as it was; the new one says **Resumed**.
- **Check Again:** a run whose checks failed or couldn't look can be checked once more, read-only, keeping what the first check said.
- The editor's **Check the result** now also takes a read-only MCP tool (`server__tool`, with its arguments as JSON) and, for a page or a tool, the text it must contain.
- **Diagnose:** a failed, stopped or check-failed run's report has a Diagnose button. A read-only agent answers with the likely cause, the evidence, what is unknown and the smallest fix, without applying it (10 minutes, 300k tokens where the agent can keep that). Only you can start one; agents have no tool for it. Without the run's contents (private mode, expired) no agent starts.

### Your own MCP servers (Settings › MCP Servers)
- Mac MCP now also connects to MCP servers you add: remote ones by URL (Streamable HTTP or SSE, with optional headers) and local ones by command (stdio, with optional env). Paste the `mcpServers` block from Claude Desktop, Cursor or Claude Code to add several at once.
- Each of their tools becomes a Mac MCP tool named `<server>__<tool>` (e.g. `exa__web_search_exa`), so every AI app and agent connected to Mac MCP can use it, through `tool_discover`/`tool_invoke` or directly in the tool list ("Show in tool list", on by default). Calls pass the same permission profile, scope, Tool Activity, idempotency and error rules as Mac MCP's own tools before they are forwarded.
- Risk comes from the server's own tool annotations: read-only tools also work in the Read Only profile; tools marked destructive need Trusted. Developer subagents may use them; browser-only subagents may not.
- Servers connect once and stay connected; adding, turning off or removing one applies within seconds without a restart. Keys stay in `~/.mac-mcp/mcp_servers.json` (owner-only) and are never shown again in Settings, status or the CLI.
- `mac-mcp mcp list|add|import|remove|enable|disable|test|call` does the same from the terminal.

### More done in your Mac's own apps (#194, #195, #196)
- **Reminders:** rename a reminder, move its due date (defer it) or change its notes, and delete one; each change is read back.
- **Notes:** replace a note's text or title (`update_note`, by its id only, so a name guess never overwrites the wrong note).
- **Calendar:** `find_free_time` can keep minutes free around each event (`buffer_min`); `plan_day` shows one day at a glance: events, free time between them and reminders due (overdue ones marked). It only reads.
- **Mail:** mark a message read or unread, flag it, move it to another mailbox or archive it; attach files to a draft; write a contact's name instead of an address when it leads to exactly one address (otherwise you get the choices, and nothing is done).
- **Contacts:** find someone by email address or phone number.
- **Shortcuts:** Settings › Apps lists your shortcuts; the ones you turn on can run even while running commands is off. The task editor's command field has a **Run a shortcut** menu.
- The menu app can be signed with this Mac's own development identity (`~/.mac-mcp/codesign-identity`, a SHA-1 from `security find-identity`); Shortcuts and Siri only talk to apps signed with a team, so ad-hoc builds show the action but cannot list or run recipes. Developer ID signing keeps the hardened runtime and timestamp; a development identity signs plainly.
- **Run Mac MCP Recipe** in Shortcuts and Siri: pick one of your saved recipes; Mac MCP shows the same confirmation as a recipe link before it runs. Built when Xcode is installed (App Intents metadata); with Command Line Tools only, the app builds without this action.
- **Reminders:** AI apps can now add a reminder (title, list, due date or time, notes), not only list and complete them; a repeated request finds the reminder it made instead of adding a twin.
- **Notes:** add text to the end of an existing note (`append_to_note`), read back after the change.
- **Calendar:** `find_free_time` finds free stretches of a given length within working hours (09:00-18:00 by default, weekdays unless asked) over the next days. Holidays and other all-day events don't count as busy; simple repeating events (daily, weekly on chosen days, monthly, yearly) are counted on every day they fall, and a rule it can't expand is named and the answer marked approximate instead of offering time that may be taken.
- **Contacts:** `find_contacts` by name or company, with emails and phone numbers.
- **Shortcuts:** list your shortcuts and run one by id or exact name, with text in and its text out. A shortcut can do anything its actions do, so it runs only when running commands is allowed, the same as a command; a failed run says its outcome is unknown and is not retried.

### Scheduled Tasks can run when a file arrives (#193)
- **When › New file**: a task runs once for each new file in a folder you choose, optionally only files like `pdf, Invoice*`. Files already there don't start it, a file counts once it has finished copying, and downloads still in progress are ignored.
- The agent is told which file it was (or put `{file}` in the prompt); a script gets `{file}` in its arguments or command, quoted, and `$MAC_MCP_TRIGGER_FILE`. Run now uses the newest file. The run report and the history name the file.
- Each file runs the task once, also across restarts, and files that arrived while Mac MCP was off are picked up. One run at a time; files the task's own run writes into the folder never start it again; at most 30 runs an hour, the rest recorded as skipped. The folder is watched with kqueue: no polling while nothing happens.
- AI apps can suggest such a task with `scheduled_tasks` (`schedule: {type: "folder", folder, match}`); it waits for your approval like any other.

### An optional usage limit for scheduled AI tasks (#211)
- **Advanced › Usage limit**: stop the agent once a run reaches a number of tokens or a cost in dollars. Off by default; the time limit still applies.
- Only agents that report usage while they work can keep one: tokens with Claude Code, OpenCode, Kilo Code and pi; cost with OpenCode and Kilo Code. For any other agent the limit is refused with the reason instead of being promised, and a run whose agent changed meanwhile doesn't start.
- A stopped run says so ("Stopped at its 500,000-token limit (used 512,330)") and keeps what the agent did before the stop; the report shows the limit next to the usage. A suggested change that raises or removes a limit shows in the review.
- No limit that can't be kept: the editor names the agents that can keep each kind (tokens: Claude Code, OpenCode, Kilo Code, pi; cost: OpenCode, Kilo Code) and keeps Save off for any other, and a cost limit stops the run when a step comes without its price, instead of letting spending pass the limit unseen.

### mac_act refuses less by mistake, and never halfway (#223, #224)
- A consequential word counts only where a word starts, so "model", "Mode", "Encode" or "Claude Code" no longer need allow_risky while Delete, Send, Pay, Sil (Silinsin), Öde (Ödeme) and Gönder still do. "ÇIKIŞ" in capitals now matches too.
- A batch with a consequential click is refused before any of its actions runs. It used to run the actions before that click, then refuse without saying they had happened, so an agent typed the same text again and was told it had no effect.
- Typing text that a field already holds reports the field as done (text_already_present), not as no effect.

### Task sheets fit the Settings window (#222)
- The New Task, Edit, Review and run report sheets are never taller than the Settings window (they scroll instead), so Cancel and Save stay inside it; the Edit sheet hung about 60 points below the window's edge.
- mac_act reports why a control isn't ready ("ELEMENT_OUTSIDE_WINDOW", retryable) when there is no time left for another look, instead of starting one more probe and failing with "AppleScript timed out".
- Scheduled Tasks no longer carries an "Experimental" badge in Settings.

### mac_act on Mac MCP's own windows, and toggles that only rename themselves (#220, #221)
- The desktop agent cursor no longer counts as a window to Accessibility. It shifted the window numbers mac_act had just resolved, so clicks in Mac MCP's own Settings hit the empty cursor window ("Can't get UI element 1 of window 2").
- A toolbar toggle that only renames itself (Calculator's "Show Sidebar" → "Hide Sidebar") or opens a pane deeper in the window (Finder's Preview) is no longer reported as having no effect; the change it made shows up in the returned tree, and the observation's own labels are kept.

### Scheduled task runs keep less, for less long (#199)
- A run's contents (result, output, each step's text and inputs) are removed after **Settings › Privacy › Scheduled task run details**: 7, 30 (default) or 90 days. How the run ended, when, its usage and checks stay for the run's 90 days. The report says when the details were removed, and nothing is read back from the agent's files afterwards.
- Known secrets (tokens, `NAME=value` keys, bearer headers, private keys) are masked before a run is written.
- Each step with inputs or a command gets a keyed fingerprint (HMAC under a key that stays on this Mac), and each run one over its steps, so two runs that did the same thing can be told apart from ones that did not, even after their contents are gone.
- Contents expire on time even when no task runs: whenever runs are read, when the scheduler wakes and when the period is changed. A run followed after a restart in private mode is marked private like any other, and Settings › Privacy says how long agent folders and run contents are kept.

### Browser companions show whether they work (#188)
- Settings › Connections › Browser shows the Chrome companion live: **Connected** with its version, **Reload in Chrome…** when the loaded companion is older than the server (missing a feature it should report, even on the same version number), or Chrome Setup… when it is not loaded (Chrome Beta, Dev, Canary and Chromium count as Chrome). Safari's state refreshes while the pane is open, not only when the app starts. The Get Started checklist says which ones are on.
- Chrome Setup… also copies the companion's folder path, so in Load unpacked ⌘⇧G and ⌘V go straight to it, and Mac MCP announces "Chrome companion connected" once it is loaded, with no check by hand.
- The tour has an optional Browsers step after Permissions: Safari Enable and Chrome Set up, each turning to On or Connected by itself; Skip for now moves on.
- The server keeps the companion's version and when it last spoke; `GET /dashboard/api/diagnostics/runtime` reports them as `chrome_companion`. What a Chrome Web Store listing would take is in docs/chrome-web-store.md.

### Scheduled Tasks can check that a run made what it should (#198)
- **Check the result** under Advanced: name a file the run should leave. After a run that finished, Mac MCP checks it is there, is not empty and was written by that run, by its size and time only (it never opens the file; a link or a folder fails). The run stays Done and reads **Checked** or **Done, but report.csv is missing**, in the history, the run report and the notification.
- AI apps can suggest checks with the `scheduled_tasks` tool (`expected_files`, up to five, with a smallest size and whether this run must have written the file); like any suggestion, they apply only after you approve them.
- A failed or skipped run is never checked, so a check never turns a failure into a success, and nothing is run again on its own. Private mode keeps only whether the checks passed.

### Less memory and less work behind each tool call (#184)
- Usage metering no longer keeps an unbounded number of raw tool results while an agent keeps calling tools: past 16 MB of waiting results (`MAC_MCP_USAGE_BATCH_BYTES`) the batch is measured and written without waiting for a pause. With a steady stream of 1 MB results that the meter keeps up with, its memory stayed at about 16 MB instead of growing with every call (78 MB after six seconds in the benchmark, and rising); a stream faster than the meter is still bounded by the queue's 2,048 results.
- The usage token count runs about 2.5 times faster (a 5 MB result: 0.93 s → 0.42 s) and counts exactly as before; the metric's definition (`mac_mcp_payload_v1`) is unchanged and pinned by a test.
- A tool result whose text ends in half an emoji (text cut in JavaScript) is recorded again; measuring its size used to throw, and the call never reached the activity history.
- The first memory search after a new embedding model arrives answers within a short budget (0.75 s, `MAC_MCP_MEMORY_MIGRATION_BUDGET_S`) and re-embeds the remaining day files in the background; until then older notes are found through their previous vectors and the word match.

## 2.2.0 r2 - 2026-10-16

**In short:** a signed checkpoint on the 2.2.0 line; the version stays 2.2.0. An agent's clicks in other apps now show as a cursor on the desktop, each session in the menu shows its age and calls, seven more AI apps and agents work with Mac MCP (Antigravity, pi, Cline, Kilo Code, Goose, Kimi Code, Grok Build), the browser reads a whole page in one call, the CLI has a look of its own, and Mac MCP has a new mark. Update with `mac-mcp update` or the new package. The Mac MCP Server helper is unchanged, so granted permissions carry over.

### An agent cursor on the desktop
- When an agent clicks, types, scrolls or presses a control in another app's window through Accessibility, a cursor glides there and shows the step (for example "Agent 2 · Clicking Button"), like the browser companion's cursor. The real pointer does not move.
- The cursor sits in the window stack directly above the window being driven, not above everything: a window you have in front covers it and its click ripple, just as it covers the background window.
- It ignores the mouse, never takes focus and stays out of screen captures, so agents never see it in their own screenshots. It carries a verb and a control kind only, never titles or typed text.
- Coordinate input, which moves the real pointer, shows no second cursor. Turn it off in Settings › Activity & Notifications › Agent cursor on the desktop.

### Session details in the menu
- Each session row under Sessions shows how long the session has been open and how many tool calls it made, plus a queued or undelivered prompt as before. Clicking the row selects it for prompts and opens a small panel beside it with the session's status, start time and age, its call count, the calls running now and the current or last tool.
- Calls are counted per request, denied and preempted ones included; a parallel batch counts once. An older server shows the count as "—" rather than 0.

### Activity toggles that stay as you set them
- Turning the agent cursor back on, or the Live Island off, now lasts. Before, the change held only until Settings reopened or the app relaunched.

### A new Mac MCP mark
- One thin line draws an M; the browser companion's pointer, turned upright, carries on its right leg. Champagne on dark backgrounds, bronze on light ones.
- The app icon is the mark on an obsidian tile, with hand-tuned 16, 32 and 64 px sizes for Finder and small Dock views. The menu bar shows the mark itself: dimmed while the server is stopped, with a dot while work runs.
- The same mark is in the menu header, the Live Island, the tour, the site (header, island, social image), the demo video and its poster (so the README too), the mobile page (with home-screen icons), the dashboard and the ChatGPT panel.
- `assets/brand/` holds the SVG sources; `scripts/build_brand.py` renders every image from them and `--check` keeps them in step.

### The CLI has a look of its own
- In a terminal, `mac-mcp`, `--help`, `status`, `start`, `stop`, `restart` and `doctor` open with the same header, line their facts up in one column and end with one sentence that says what happened, plus the command to run next when something is wrong. `mac-mcp` alone lists the commands in groups (Everyday, Connect, Troubleshoot, Advanced) under a one-line status.
- `start`, `stop` and `restart` show each step live, with its time; in a terminal `restart` now waits for the outcome. `doctor` groups its checks and folds the ones that passed (`--verbose` lists every check).
- Without a terminal (scripts, the menu app, the installer), with `--json`, `NO_COLOR` or `MAC_MCP_PLAIN=1`, the output is exactly what it was.
- `clients`, `connect`, `connect-config`, `update`, `logs` and `privacy delete` have the same look: `clients` shows which AI apps have Mac MCP and suggests the next `connect`; `connect` and `update` run as live steps (a failed update shows the end of its log); `update --check` compares the installed commit with the newest verified release; `logs` and the `connect-config` snippet stay raw so they can be copied as they are; `privacy delete` previews what it would move and, with `--yes`, stops, moves and starts again step by step.
- The remaining commands have the same look too: `dashboard`, `credential`, `autostart`, `recipe`, `logins`, `catalog`, `conformance`, `repair` and `uninstall`. `repair --yes` and `uninstall --yes` run as live steps, and `repair` keeps pip's and the app installer's output under its step unless that step fails. `uninstall` lists each part with its path. `conformance` groups its checks like `doctor`. `catalog enable-script` shows the held script raw, exactly as it would run. A Cloudflare token or a login password typed into the terminal (`credential cloudflare save`, `logins add`) is now read without being echoed. Piped input works as before.

### Browser extraction and scan you can trust (#215)
- `browser_act` and `browser_do` take one extraction field shape (`name`, `selector`, `attr`, `all`, `max_items`). A missing field, a broken CSS selector or a broken regex is an error; a selector never falls back to the whole page.
- Extracted text and `scan` leave out scripts, styles, templates, `noscript` and hidden content. Results say how many matched, how many came back and where they were cut, and a cut result can be continued from where it stopped.
- `scan` reads and scrolls several times in one call, and `next_cursor` continues after an item, text, size, step or time limit. A cursor is refused after a navigation, a scroll in between or five minutes.
- A `scan` inside `browser_do` now returns its records and cursor under `scan`; before, `browser_do` dropped them and answered with empty data.
- New `{type:'read'}` action: `browser_do` with `actions:[{type:'read'}]` returns a whole page's text in one call, after scrolling so content that loads on scroll is there (it waits on loading skeletons and never skips a screen while something is loading), with `next_offset` for longer pages. The review page that took an agent 35 calls now reads in about 6 seconds, all 77 reviews included. Agents are pointed at it for reading pages instead of paging through `extract` or `observe`.

### Settings no longer hides the Live Island
- Opening Settings closed the Live Island together with the menu popup. Now only the popup closes.

### Antigravity, pi, Cline, Kilo Code and Goose as AI apps
- Mac MCP can now be added to these five CLIs too, so an `agy`, `pi`, `cline`, `kilo` or `goose` you start yourself can use your Mac. They appear with Connect in the tour, in Settings › Connections and in `mac-mcp clients` / `mac-mcp connect --client antigravity|pi|cline|kilo|goose`. Antigravity and pi are set up through their own `mcp add`; Cline, Kilo Code and Goose get `mac-mcp stdio` in their config (`cline_mcp_settings.json`, `kilo.json`, Goose's `config.yaml` as a stdio extension) next to the servers already there, with the old file kept as `*.before-mac-mcp`. No address or key goes into any of them.

### Antigravity, pi, Cline, Kilo Code and Goose as agents
- Five more agent providers, off by default and turned on in Settings › Subagents or in the tour: Antigravity (`agy`, on the owner's Google sign-in), pi, Cline, Kilo Code and Goose. Each runs headless with only the scoped Mac MCP server, and its tool calls show properly in run reports, the menu and the island: Mac MCP's own tools by name, shell commands as commands, file writes as files, failures as failed. The island shows each one's own logo while it works.
- Token usage is recorded per agent and appears in Settings › Usage once the agent is on; follow-up messages continue the same session (not for Cline, whose CLI cannot resume a session headless).
- Antigravity runs with full access only: its sign-in is read with `/usr/bin/security`, which the restricted boundary refuses so that no agent can read other Keychain secrets.
- A run that used a native tool that may have changed something is never replayed automatically, as for the other providers.

### Tour › Connect: five logos, then +N
- A logo stack shows five logos; the rest wait behind a small +N that opens them in place when clicked. The agents row names at most three, then says how many more are on or off.

### Kimi Code and Grok Build as AI apps
- Besides handing them work as agents, Mac MCP can now be added to Kimi Code and Grok Build themselves, so a `kimi` or `grok` you start yourself can use your Mac too. They appear with Connect in the tour, in Settings › Connections and in `mac-mcp clients` / `mac-mcp connect --client kimi|grok`. Kimi gets `mac-mcp stdio` in `~/.kimi-code/mcp.json` next to the servers already there (the old file is kept as `mcp.json.before-mac-mcp`); Grok is set up through `grok mcp add`. No address or key goes into either.

### The island no longer shows a finished agent as still working
- The island rebuilt its Now list from the app's agent list while that list was still being replaced (a published value arrives before it is stored), so an agent that had just finished stayed under Now, its timer running, while Recent already said it finished. It stayed there until another agent changed. Now and the server-stopped row now use the values as they arrive.

### Tour › Connect fits any number of apps and agents
- The card keeps one height: apps that need a click first (at most two), then one row for the apps Mac MCP is already added to, with their logos, and All apps for the full list. Agents are one row with every agent's logo; Choose opens the list with a switch for each, Kimi Code and Grok Build included.
- While the apps and agents are looked up, the card shows a loader ("Looking for AI apps on this Mac…") and fades in once both answered, instead of an empty card that filled in row by row. Both lookups run at the same time, and the agent lookup no longer waits for the model catalogs.
- Instead of "Connected. Your AI app called run_command.", a small card says what happened in words ("Mac MCP is connected · Your AI app ran a command on this Mac.") or, while waiting, what to do. The First request screen uses the same card.

### Kimi Code and Grok Build as agents (experimental)
- Two more agent providers, off by default: Kimi Code (`provider=kimi`) and Grok Build (`provider=grok`), turned on in Settings › Subagents. They run like Codex, Claude Code and OpenCode: headless, in the same macOS Seatbelt boundary for `read_only` and `workspace_write`, with only the scoped Mac MCP server, follow-up messages in the same session, cancel, run reports, resume safety and (for Grok) token usage.
- Each agent gets a private Kimi/Grok home: the owner's model settings and sign-in are copied in for the run and removed when it ends or is cancelled; the owner's own MCP servers are left out.
- A spawn is refused up front when the CLI cannot reach a model (no sign-in, API key or configured model).
- Cancelling an agent now also removes its generated provider config, which held the (already revoked) scoped token; before, a cancelled OpenCode or Claude Code agent kept it on disk.
- Tested with Kimi Code 2.2.0 and Grok Build 1.0.52 against a free OpenRouter model, end to end through an isolated Mac MCP server: reads, writes, the read-only boundary, Mac MCP tools, follow-ups and cancel.

### Native scrolls work on SwiftUI and background windows (#185 follow-up)
- Scroll areas in SwiftUI windows (Mac MCP's own Settings included) and AppKit views of a background app answer the page-scroll action with an error, do nothing, or, in a SwiftUI list, page the other way. When the page action does not move the area's scroll bar the asked way, `mac_act` now moves the bar by one page (the visible share of the content) itself and says so (`scroll.method: "scroll_bar_value"`). At the edge it sends nothing (`scroll_at_boundary`).
- A scroll bar that moved the other way is `scroll_wrong_direction` (`ok: false`, no automatic retry) instead of a successful scroll.
- A scroll area without an anchor row lost its readable scroll bar to a parsing slip (stripping the output also stripped its empty fields), so scrolls there were `scroll_unverified`; they are verified now.
- An action on one of two look-alike elements (two scroll areas, say) checks which is which by the labels below them. AppleScript took 1.4 to 3.9 seconds to read those in a SwiftUI window, past the 1.2-second readiness budget, so the action always failed with `READINESS_PROBE_FAILED`. The native helper reads them now; AppleScript only when the helper is unavailable or stopped at its node budget.
- When observation fell back to AppleScript, an element's actions came back run together as one name (`AXScrollLeftByPageAXScrollRightByPage…`); they are a list again.

### The remaining page scripts are .js assets (#107 follow-up)
- The DOM capture, observe, find, extract and scan page scripts moved out of Python f-strings into `mcp_server/browser_js/`, like batch and wait before them. The scripts sent to pages are byte-for-byte the same (35 inputs compared, including quotes, backslashes and placeholder-like data).

### Rich-text typing is checked after the editor re-renders (#205)
- A rich-text editor that draws typed text at once is read again 0.2 s later, after its re-render. A controlled editor (React) can put its old model back; that is now `editor_rerender_lost` (the batch stops before any submit) instead of `value_applied`. Text the editor kept in its own form, such as a "1. " line turned into a list item, is `editor_rerendered_text`, unconfirmed but not an error.
- When an editor takes the input but never shows the text, the step is `editor_render_unconfirmed` (outcome unconfirmed, no automatic retry) instead of `editor_did_not_accept`: nothing seen says the editor rejected it, and typing again could enter the text twice.
- Plain inputs and textareas get no extra read. Reported on Reddit (r/PromptEngineering).

### The island's clicks follow its shape (#203)
- The island lets clicks through everywhere except its current shape. That was decided only when the mouse moved, so with a resting pointer the first click after the island appeared, changed shape (a peek opening or closing, the panel resizing) or moved to another display could miss the island, or a click beside a shrunken island could be swallowed instead of reaching the menu bar. The click-through is now re-decided at each of those moments; only real mouse moves still open or close the panel, so a peek appearing under a resting pointer does not expand by itself. The island still never takes keyboard focus. Reported in a source review on Reddit (r/SwiftUI).
- An app named by its executable or display name (for example `MacMCPMenu`) shows its own icon in the island instead of a dashed placeholder.

### A calmer, clearer menu (#201)
- One surface instead of stacked boxes: rows with small colour tiles (Agents violet, Endpoint teal, Safety gray, Recent activity blue, Sessions orange; red or orange when something is wrong), separated by thin dividers, with Settings and More at the bottom.
- Under the name, a line says what Mac MCP is doing (Ready, N agents working, Server stopped), and four chips show the tool calls and success rate of the last hour, the running agents and the sessions.
- Restart Server is a small round button beside the status instead of a full-width bar (it is also in More). When the server is stopped, a red callout with a Start Server button takes its place; connection problems and progress use the same tinted callout.
- The agent list opens and closes from its header's chevron, like Recent activity; it shows up to 20 recent agents in about five rows and scrolls inside. Summary rows can now be pressed by VoiceOver and other assistive tools.

### The menu fits its content; the island shows which agent works (#200, #202)
- The menu bar popover is as tall as what it shows instead of a fixed 660 points with empty space below. Opening Recent activity, Sessions or the agent list grows it and closing shrinks it, with its top edge fixed under the menu bar; content taller than the screen scrolls.
- While a delegated agent works, the island shows its maker's logo (Claude for Claude Code, OpenAI for Codex, OpenCode) inside the turning ring instead of a generic symbol, in the notch slot and in the panel's agent rows. An unknown provider keeps the generic symbol. A call that only starts or waits for agents no longer counts as a second piece of work.

### Mac apps are read natively by any of their names
- `mac_observe` asked the native Accessibility helper for an app by its bundle name only, so an app named by its executable (System Events calls Mac MCP's menu app `MacMCPMenu`) or bundle id was not found and every read fell back to AppleScript: 3 to 9 seconds instead of tens of milliseconds. The helper now accepts the displayed name, the bundle name, the executable name or the bundle id (an ambiguous name still falls back).
- `mac_act` and `mac_observe` no longer refuse a window or app handle with `TARGET_HANDLE_MISMATCH` when the app is named differently from how the handle recorded it (for example `MacMCPMenu` for `Mac MCP`).
- The first `mac_observe` after Mac MCP starts or updates is native too: the helper is made ready when the server starts instead of on the first read, which used to take the AppleScript path (5 to 9 seconds).
- `mac_act`'s observation check accepts the same names, so acting on an observation of `MacMCPMenu` no longer fails with "app does not match the application in observation_id".
- Native `scroll` uses the `AXScrollDownByPage` (and up, left, right) action that scroll areas offer; it asked for `AXScrollDown`, which they do not have, and failed.

### Faster reading of Mac app windows (#185)
- `mac_observe` started the native Accessibility helper as a new process for every observation, and again for the quick "has this window changed" check; starting it took about 40 ms, often more than reading the window. One helper now stays running while observations come in and stops after a minute without any, so neither pays for a launch: a 100-element window reads in about 13 ms instead of 51 ms, a 250-element one in about 30 ms instead of 67 ms, a 500-element one in about 62 ms instead of 94 ms, and the unchanged-window check takes 1.5 to 3 ms instead of about 42 ms. The output is byte-for-byte the same.
- A helper that crashes, hangs or answers wrongly is stopped and replaced; the call it was serving runs the one-shot helper as before (a hung one reports the same time-out as before), and a call that arrives while the helper is busy runs its own. `MAC_MCP_AX_WORKER=0` keeps one process per observation.

### A browser wait with only a time pauses
- `browser_act` `{type: 'wait', wait_s: 1.5}` with nothing named to wait for now pauses for that long (default 1 s, at most 30 s) without touching the page. It used to wait 10 seconds for a selector nobody named, time out, and stop the rest of the actions.

### Faster page reading when a dialog is open
- With a dialog open, `browser_observe` and action recovery looked for the dialog again for every element on the page, so a page with many components could use up the scan's time before reaching the dialog's own fields and report none. The dialog is now found once per scan and everything outside it is skipped first: on the test page with 300 components, reading a login dialog went from about 160 ms to about 14 ms.

### Saved logins stay on their own site (#178)
- A saved login is now filled only on the exact site it was saved for (`www.` counts as the same site). Before, a login for `example.com` was also filled on every `*.example.com` page, and a subdomain can be run by someone else.
- Each login in **Settings › Permissions & Safety › Saved Logins** has a **Subdomains** checkbox (or `mac-mcp logins subdomains <id> on|off`, `mac-mcp logins add --subdomains`). Logins saved before this change keep it on, so nothing that worked stops working.
- On shared hosting such as `github.io`, `netlify.app` or `blogspot.com`, where anyone can own a subdomain, a login is never used on another subdomain.
- When a login exists only for the parent site, `browser_sign_in` says so (`login_for_other_site`) instead of reporting no saved login.

### Scheduled Tasks: data first (#175)
- An AI task can run a script first and give the agent what it printed (**Give the agent data from a script first** in the New Task sheet, or `data_step` in the `scheduled_tasks` tool). The numbers come from the owner's script, which runs with its own access; the agent only reads and explains them. If the script fails, times out (10 minutes), prints nothing or prints more than 60,000 characters, the agent does not start and the run says why. The run report shows the data step first in Steps, with what it printed.
- Arguments and commands can use dates that move with each run: `{today}`, `{yesterday}`, `{this_week_start}`, `{last_week_start}`, `{last_week_end}`, `{this_month_start}`, `{last_month_start}`, `{last_month_end}` and `{days_ago:N}`. The sheet has a date menu and shows how the arguments read on the next run; unknown dates are refused when saving.
- The Agent and Works in fields in the New Task sheet are aligned to the left.

## [2.2.0] - 2026-10-13

**In short:** schedule AI tasks and scripts and read a report of each run; see what your AI is doing in an optional island beside the notch; and the guard against leaking secrets now also catches encoded ones. Mac MCP also has a product page and a 40-second demo.

### Scheduled Tasks (experimental, #175)
- **Settings › Scheduled Tasks**: AI tasks (a prompt for a delegated agent) and script tasks (a file or command) that run once, daily, weekly, monthly, every few hours or on a cron expression, in local time. Cards with the last result, an Up next strip, a detail page with recent runs and full reports, Run now, Pause and Edit, and a New Task sheet that states in a sentence when the task runs; developer options sit under Advanced.
- Each run has a report: the result as formatted text, and the agent's steps (its thinking, tool calls and commands) for Codex, Claude Code and OpenCode, with token usage and cost where the provider reports them. A failed run says why, in the provider's own words.
- Runs missed while the Mac slept or Mac MCP was stopped are recorded as Skipped with the reason instead of running late. The scheduler sleeps until the next run and does not wake at all without active tasks.
- AI apps can suggest tasks and changes with the `scheduled_tasks` tool; nothing they suggest runs until the owner approves it in Settings. `macmcp://settings/<pane>` opens Settings on a pane.

### Live activity island (#174)
- An optional island beside the notch (Settings › Activity & Notifications › Live activity: Off, Bubble or Notch island; off by default). At rest it shows the Mac MCP mark; while a tool runs, the app it uses turns in one slot and a tick follows. Only events that need you slide a row down.
- Hover opens a panel with Needs you, Now, Recent and Scheduled, also when nothing is running. Long titles fade and scroll while hovered. The island stays on screen when Mac MCP is in the background and names the AI app that made each call.

### Security fix (#7)
- The secret-egress guard now also checks the decoded forms of an outgoing payload: base64/base64url (padded, unpadded, line-split), hex and percent-encoding, two layers deep, including tokens inside JSON. Before, a secret read on this Mac could be sent without approval by encoding it first. Values of `NAME_TOKEN=` / `SERVICE_API_KEY=` style lines are now remembered when such a file is read. Encryption or splitting a secret across requests is not detected.

### Product page and README (#57, #101)
- A product page at https://bulutarkan.github.io/mac-mcp/ (`site/`): the promise, how it works, workflows, what Mac MCP is and is not, and install choices with the exact package version, download link and SHA-256 taken from the published release at deploy time (`scripts/build_site.py`, `.github/workflows/pages.yml`).
- A 40-second demo video made with Remotion (`video/`, development only; the installer never uses Node).
- The README now opens with what Mac MCP does and who it is for; the ChatGPT section moved to Workflows.

### Also
- Codex agents start again when the owner's Codex config has the keyless `mac-mcp` entry: the agent's scoped server has its own name (`mac-mcp-agent`), and the owner's home folder is not trusted as a Codex project. This also works on Python 3.10.
- Settings panes share one header and margin; the Default Agent pickers are right-aligned.

## 2.1.9 checkpoints (r2–r10)

### Privacy, browser outcomes and startup (2.1.9 r10)
- **Settings › Privacy**: pause observation (no screen, page, clipboard or voice reads, no new agents) and private mode (nothing about the work is kept), enforced by the server for every caller and available from the menu (More → Pause Observation / Private Mode). Never-observe lists for apps and sites, optional category rules (password managers, banking and finance, health), every local store with size, retention and owner-only state (move one or all to the Trash), where data can go, FileVault status and an option to keep Mac MCP data out of Time Machine. Tool results that read a page or app carry `privacy_source`. `mac-mcp privacy delete` and `mac-mcp status` cover the same.
- **Browser outcomes**: every `browser_act` action reports `outcome` (confirmed, page_reacted, unconfirmed, failed, blocked) and takes `expect` to check its result; editor punctuation (smart quotes, dashes) no longer reads as `input_not_applied`. Typing replaces the whole field and partial edits are refused, stated in the tool schema.
- **One-click connect** also in Settings › Connections; apps added the old way with the key in their settings show Update.
- **Start at login**: the menu bar app is a login item and shows Starting… until the server answers.
- Restarts finish while MCP streams are open (3 s graceful shutdown, 10 s stop wait); one path resolver (`mcp_server/paths.py`, docs/CONFIGURATION.md).

### First run and setup (2.1.9 r9)
- A first-run tour opens after install: four short animated slides about what Mac MCP does, then live setup steps (start, permissions, connect your AI app, first request). It can be reopened from the menu (More → Take the Tour…) and from Settings → Get Started.
- macOS permissions now belong to **Mac MCP** instead of Python: the server runs under a small Mac MCP Server helper app, and the tour's Allow buttons open macOS's own prompt naming Mac MCP. After a grant the tour restarts the server so the permission takes effect. Existing installs need to allow Accessibility and Screen Recording once more, this time for Mac MCP.
- One-click Connect: the tour lists the AI apps on this Mac (Claude Desktop, Claude Code, Codex, OpenCode, ChatGPT) with a single button each. `mac-mcp connect --client <app>` adds Mac MCP to the app as a `mac-mcp stdio` command server, so no address or API key is written into any app's config; for ChatGPT the connector address is copied to the clipboard. `mac-mcp clients` shows which apps are set up.
- `mac-mcp stdio` bridges stdin/stdout to the running server, replays the session after a server restart, waits briefly for a starting server, and never resends a request that was cut off mid-reply.
- Mac MCP can start at login when it was left running; `mac-mcp repair` and `mac-mcp uninstall` (preview first) were added; Settings → Get Started has a checklist from install to a first request.
- `system_info` reports the macOS version.
- r8 fixed r7's updater, which stopped with an ImportError while building Mac MCP Server and rolled back. r9 makes the updater start the server again under Mac MCP Server (so permissions keep working after an update) and bounds server shutdown to 5 seconds so open MCP streams cannot stall a restart. Neither r7 nor r8 was published as a package.

### Subagents
- Added Claude Code as a delegated-agent provider (`provider=claude`, Settings → Subagents). It runs the `claude` CLI headless with stream-json events, model aliases (`fable`, `opus`, `sonnet`, `haiku`) or full ids, `--effort` reasoning levels, session resume, provider-reported token usage and cost, and the scoped Mac MCP server only.
- Sandboxed Claude Code agents (`read_only`, `workspace_write`) run inside the Mac MCP Seatbelt boundary with a private config directory and only the short-lived access token of the owner's sign-in; tools that outlive the agent (schedules, remote triggers, push notifications, cross-session messages, uploads, workflows, worktrees) are removed in every mode.
- Removed the ChatGPT Web CLI provider, its installer step, turn-budget checkpointing, rate-limit cooldown gate and the ChatGPT-only `project` parameter of `spawn_agent`/`spawn_agents`.

## [2.1.9] - 2026-10-08

### ChatGPT plugin panel (MCP Apps)
- Added a Mac MCP app for ChatGPT plugins with both a global (sidebar, fullscreen) and a thread (side panel) entrypoint, a monochrome `>_` tool icon, and a bundled single-file MCP Apps UI served as `ui://mac-mcp/panel-v3.html` (`text/html;profile=mcp-app`).
- The panel shows 24-hour MCP tool calls, active agents, mean latency and errors, the 20 most recent delegated agents, 7-day provider-reported token usage for Codex/OpenCode/ChatGPT Web (unreported totals stay unavailable, never estimated), and MCP server uptime.
- Data comes from the host's initial tool result; refreshes are manual through an icon-only button, with no polling. A single fallback read runs only if a host opens the panel without delivering a result.
- Settings tab: the only writable setting is the default delegated agent (`subagents.default`). The provider must be enabled, the model must exist in that provider's live-discovered catalog (exact lookup, so large OpenCode catalogs are not truncated away), and the reasoning level must be supported by the model. Unknown fields, disabled providers and invalid values fail closed without touching the settings file, and unrelated settings are preserved through the atomic `0600` merge write.
- Agent completion notifications and the activity bubble are shown read-only (owned by the native app and macOS notification permission); Connection shows version, permission profile, endpoint mode and public host only, never the URL query or API key.
- Added `mac_mcp_panel_state(models_for=<provider>)` for on-demand, bounded model catalogs (≤600 models, ids ≤120 chars, ≤12 reasoning values) loaded only when the editor opens.
- New tools `open_mac_mcp_panel`, `mac_mcp_panel_state` and `mac_mcp_panel_setting` are classified in the central risk registry (read/read/local-write); the setting tool is unavailable in `read_only` profiles and in delegated scopes that exclude it.
- ChatGPT-only discovery gate: tools and the UI resource are listed, readable and callable only for recognized ChatGPT `clientInfo` labels, including `openai-mcp` and runtime-suffixed `openai-mcp (codex)` when the client advertises MCP Apps UI support. Claude, Codex, OpenCode, `chatgpt-web-cli` and unknown clients never see them. The extension can be disabled live with `{"chatgpt_extensions": {"enabled": false}}`; malformed settings fail closed. clientInfo is treated as a UX routing hint, not identity; normal MCP authentication and approvals still apply.
- Earlier panel resource URIs (`panel-v2.html`, `control-center`) remain readable but unlisted so hosts that cached an older `resourceUri` keep working.
- The UI honors host `safeAreaInsets`/display mode via `ui/initialize` and `ui/notifications/host-context-changed`, and keeps room for the desktop composer in fullscreen.
- `mac_mcp_ui_read` log lines record the client label, decision and URI for each UI resource read.

### Agent visibility and approvals
- Added tool activity intent bubbles in the native app, with concurrent activity lanes and a stabilized multi-call bubble.
- Added opt-in native notifications when delegated agents complete.
- Added an optional Server Approval overlay (`Off`/`Critical`/`High Risk`) with Allow Once/Block confirmation for raw execution, update control, and destructive process/external/browser/native actions, and hardened live approval settings.

### Browser and Computer Use
- Made browser interactions batch-first and kept the batch guidance in the compact tool catalog.
- `browser_act` key actions no longer require foreground focus: DOM keyboard events go to the focused/resolved element, Enter emulates implicit form submission via `requestSubmit()`, and the effect is verified (`ACTION_NO_EFFECT` when a page ignores untrusted events). After an earlier action changed the page, missing targets are awaited briefly instead of failing the batch.
- Safari tab handles now survive Mac MCP's own cross-site navigations (WebContent process swaps) under strict same-window/same-position/same-site rebinding rules; ambiguous Safari handles still fail closed.
- Tabs are re-resolved by native identity (Safari pid, Chrome id) inside every AppleScript step, so tab index shifts cannot fail a call; a closed target returns structured `tab_target_closed` with completed actions, `outcome_unknown` and `automatic_retry: false`.
- Click verification counts a DOM mutation elsewhere on the page as the effect when the page was quiet for at least 250 ms beforehand; Visual Companion mutations are ignored everywhere.
- `browser_observe` visual capture works on Trusted Types pages such as Google Sheets through a narrowly scoped Trusted Types policy, with per-color fallback for CSS Color 4 functions and `TRUSTED_TYPES_BLOCKED` when a page forbids the policy.
- `browser_find` demotes containers that only echo a descendant's match, inner text nodes of actionable controls, and labels whose control also matches, removing most ambiguous ties.
- Added an opt-in Decision Acceleration layer (OpenAI Decisions, Keychain-stored key, off by default) that only picks among already-ranked candidates for ambiguous `browser_act` targets and `computer_plan` rebinds; policy, lease, takeover and readiness checks still run afterwards. `browser_act` actions accept an optional `intent` hint (redacted, ≤120 chars) used only for ambiguous decisions.
- Revalidated browser ownership before mutations and hardened browser input and security metadata.
- Read-only snapshots no longer launch Safari or Chrome just to enumerate tabs.
- Visual Companion accessibility: a single polite live region for session-level transitions, synchronized `aria-expanded`/`aria-controls`, Escape to close with focus return, focus-visible rings, and decorative parts hidden from assistive technology.

### Delegated agents
- Added conflict-aware agent integration fan-in, made agent terminalization atomic, and clarified agent team admission budgets.

### Setup, API and dashboard
- Added `mac-mcp connect-config --client chatgpt|codex|opencode` to generate connection snippets from the actually configured endpoint and authentication.
- Added a safe transaction history view to the operations dashboard.
- Exposed read-only memory and Agent Skills REST APIs and made the published OpenAPI surface machine-accurate.

### Updates, restarts and reliability
- CLI-driven updates run detached and survive Mac MCP's own restart; updater server ownership can be recovered from exactly one verified listener; updater-spawned server environments are sanitized; a corrupt updater journal fails closed; updater wording matches the verified stable-checkpoint model; release signer rotation is hardened.
- The runtime-overlay merge passes the updater identity with `git -c` so it no longer writes "Mac MCP Updater" into the source checkout's `.git/config`.
- `mac-mcp restart` keeps waiting for an exiting server whose identity is briefly unreadable instead of aborting and leaving Mac MCP stopped; it still never signals an unverified process.
- Background jobs stay running while children of their shell survive; stop/timeout signal the surviving process group and escalate to SIGKILL, never signalling a reused pid's group.

## [2.1.8] - 2026-10-05

- Kept read-only snapshots side-effect free by skipping browser tab enumeration for Safari or Chrome when that browser is not already running; closed browsers are no longer launched merely to inspect tabs.
- Sanitized updater-spawned server environments so PYTHONPATH/PYTHONHOME overrides cannot redirect a restarted runtime back to a source checkout; the restarted server is explicitly bound to the deployed runtime root.
- Hardened updater restart ownership recovery so a missing/stale server PID record can be rebuilt only from exactly one verified Mac MCP listener; foreign or ambiguous listeners still fail closed.
- Hardened CLI-driven updates so the updater runs in a detached process session before restarting Mac MCP; updates launched from Mac MCP shell tools now survive their own server restart instead of being cancelled with the hosting tool call.
- Added a unified low-context perception ladder across snapshot, semantic observation, conditional reuse, targeted visuals, and OCR-last fallback, with bounded context/visual telemetry and deterministic conformance coverage.
- Added background native window capture, zero-focus semantic input paths, and human/agent workspace arbitration so scoped Computer Use can coexist with the person using the Mac.
- Added MCP payload Usage metering plus separate provider-native Codex/OpenCode delegated-agent token rollups with local privacy-minimized persistence, 30/90/365-day views, model attribution only when attested, and fail-closed handling for unknown or unavailable accounting.
- Restored Codex read_only and scoped workspace_write delegation using a Mac MCP-enforced macOS Seatbelt boundary with private provider state; workspace-external reads and disallowed writes fail closed while full access remains explicit.
- Fixed the Operations dashboard right rail scrolling/card clipping and made the native Usage heatmap adapt to the available Settings width so the sidebar and detail pane no longer get pushed into the window edges.
- Added public contribution/issue templates and refined Settings/default-agent presentation without changing provider security guarantees.


## [2.1.7] - 2026-10-01

- Fixed the Operations dashboard right rail so What Changed on My Mac, Hot tools, and Delegated agents remain vertically scrollable when their combined content exceeds the fixed desktop viewport; agent cards now size to their full content instead of being clipped into the remaining grid row.
- Added secure paired /mobile access with persistent device sessions, manual pairing, lifecycle-aware Sessions grouping, compact tool-aware icons, and a collapsible idle Agents section.
- Added durable native update progress/recovery UI backed by the updater journal, including explicit prepare/backup/update/sync/dependency/restart/health phases and distinct recovered/recovery-failed states.
- Hardened updater/restart process identity, foreign-listener handling, cancellation-surviving restart handoff, transactional dependency activation, and post-update product-health verification.
- Unified dynamic tool discovery/invocation semantics so hidden-tool dispatch preserves normal result shapes and policy enforcement.
- Added bounded reversible filesystem capture for shell commands and background jobs. `run_command` and `start_background_job` can opt into scoped Git-aware/non-Git preimage capture, return full/partial coverage explicitly, join earlier file-tool transactions into one compound undo receipt, preserve created/deleted/renamed files and empty directories, fail closed on newer conflicting edits, recover durable job captures after bridge restart, and never use global `git reset --hard`/`git clean`. Generated/ignored paths, symlink escapes, Git control metadata changes, capture limits, and interrupted captures are reported as unsupported/partial/unknown instead of being silently claimed reversible.
- Added provider-independent versioned delegated-agent result envelopes and deterministic fan-in. New agent handoffs persist a canonical `result.envelope.json` with summary/claims/evidence/artifacts/warnings/confidence/errors/provenance; marked malformed contracts fail closed while plain legacy provider text is wrapped as an explicit `legacy_fallback`. Team dependency/reviewer prompts consume typed envelopes instead of raw provider logs, fan-in deduplicates evidence/artifacts while preserving task provenance and flags conflicting keyed claims, typed partial failures remain visible without changing DAG task-state semantics, and bounded `get_agent`/`wait_agents` output reports explicit truncation/omission metadata. Reviewer `QUALITY_GATE` markers must agree with typed quality-gate outcomes.
- Reworked Computer Use observe/wait into an event-driven, delta-aware pipeline: browser selector/text/semantic/url/DOM/network waits now wake from in-page mutation/navigation/network signals with bounded polling fallback; Chrome can complete waits in one debugger round-trip while Safari uses compact waiter-status probes. `browser_find(wait_timeout_s=...)` and `computer_plan wait_until` reuse the same primitive. Native `mac_observe(previous_observation_id=...)` now returns compact `not_modified` results without a full AX traversal for short validated windows, returns small deltas for non-structural changes, forces full refresh for structural/expired state, and exposes rolling latency/payload/JS-call/AX-traversal telemetry. MCP and REST screenshot defaults are now consistently opt-in, and Computer Use conformance advances to baseline v5.
- [SEC-FOCUS-001] Added an explicit Chrome Background Companion trusted-pointer path for `browser_act` click/double-click via debugger `Input.dispatchMouseEvent`; inactive Chrome tabs now use bounded CDP focus emulation around trusted pointer dispatch without `Page.bringToFront`/tab activation, Safari trusted-input requests fail closed without foreground/coordinate escalation, and synthetic clicks never auto-upgrade or replay after an uncertain/no-effect outcome.
- [SEC-COMP-001] Tightened browser action verification so unrelated DOM mutation revisions no longer count as click success; bounded verification now accepts target/control/modal/navigation changes or action-correlated fetch/XHR initiation, and activation results expose synthetic/trusted mode plus sanitized same/cross-origin request paths without query/body data.
- [SEC-FOCUS-001] Closed the remaining Chrome focus-escalation fallback: when debugger/background transport is unavailable, the legacy `javascript:` URL bridge now requires an internal foreground capability instead of silently falling back to a potentially focus-stealing transport.
- Hardened browser targeting inside blocking dialogs: `observe`/`find` now scope candidates to the topmost modal, recognize semantic `data-state=open` on real dialog candidates even when background-tab animation throttling leaves wrapper opacity at zero, exclude `data-state=closed` stale descendants, avoid treating unrelated `data-state=open` regions as blocking modals, associate styled radio/checkbox controls with their labels, and allow verified pointer-events hit targets only when they belong to the same control/label relationship instead of bypassing real occlusion.

## [2.1.6] - 2026-09-21

- [SEC-FOCUS-001] Hardened browser focus isolation so model-visible `allow_foreground`/`background` flags can no longer self-authorize Safari/Chrome activation or active-tab changes; only the explicit local **Show Tab** UI receives a scoped foreground capability, and Safari native upload now fails closed instead of temporarily stealing focus.
- Rolled the signed 2.1.5 r2-r13 checkpoints into a new public release boundary while preserving the verified stable-update channel; 2.1.6 starts again at signed revision r1.
- Hardened delegated provider boundaries and lineage-scoped agent control so restricted workers cannot silently widen their filesystem/process/control-plane authority.
- Added bounded agent-team DAG scheduling, reviewer quality gates, failure-aware quorum semantics, shared team budgets, and adaptive retry classification/admission.
- Added per-agent Git worktree isolation with dependency/reviewer fan-in, conflict-aware explicit safe apply, crash/resume preservation, and fail-closed handling of unapplied work.
- Added a persisted global cross-team admission scheduler with provider capacity, FIFO-aware queueing, scoped browser/native/process/clipboard ownership, heartbeat/TTL recovery, and pre-start revision checks.
- Propagated client cancellation through synchronous workers and owned subprocess/browser/native work; uncertain mutating outcomes are recorded as non-retryable `outcome_unknown` rather than guessed or replayed.
- Added task/session-scoped **What Changed on My Mac** receipts with privacy-minimized telemetry for files, apps, browser actions, commands, system changes, delegated agents, and external sends.
- Upgraded `computer_plan` to closed-loop v2 with `wait_until`, conditional branches, bounded retry/fallback, fresh semantic stale-target rebind, AXIdentifier-backed native identity, resource preflight, and hard recovery/action/time budgets.
- Expanded the public regression-backed Security Assurance Matrix to nine control classes and Computer Use conformance to baseline v2 with 16 deterministic contracts.

## [2.1.5] - 2026-09-18

- [SEC-COMP-001] Signed 2.1.5 stable revisions add bounded fail-closed closed-loop Computer Use recovery with semantic stale-target rebind and no duplicate mutation after uncertain effects.
- [SEC-SCHED-001] Signed 2.1.5 stable revisions coordinate delegated work across teams with persisted provider/resource admission, fair queueing, crash-safe leases, and pre-start file revision checks.
- [SEC-GIT-001] Signed 2.1.5 stable revisions isolate delegated Git write agents in per-agent worktrees and require conflict-aware, local/root-only application before changes can reach the user's source checkout.
- [SEC-AGENT-001] Signed 2.1.5 stable revisions add persistent delegated-agent control-plane lineage isolation so scoped agents can inspect/manage only themselves and descendants while local root administration remains available.
- Added a cryptographically verified stable release channel using a pinned Ed25519 signer, detached signed manifest, complete tracked-file SHA-256/mode/size inventory, aggregate payload digest, and parent-commit binding to prevent manifest replay.
- The updater now selects only the newest verified stable-release commit reachable from `origin/main`; ordinary development commits are ignored, while incomplete, tampered, wrong-hash, or invalidly signed release markers fail closed before repository/runtime mutation.
- The installer verifies a pinned standalone bootstrap verifier and the signed stable payload before persistent source/runtime creation, so failed release verification leaves install targets untouched.
- Added offline release-signing and verification tools, two-phase signing-key rotation guidance, CI verification of the newest stable release, and a Developer ID + notarization strategy for public native-app artifacts.
- Preserved the existing updater runtime-overlay merge, backup, restart-health validation, and guarded rollback behavior after cryptographic verification succeeds.

## [2.1.4] - 2026-09-16

- Added switchable public endpoint modes (`Local only`, managed `ngrok`, managed `Cloudflare Tunnel`, or externally managed `Custom HTTPS`) across CLI, native Settings, installer migration, status, and doctor; existing `ngrok_on_start` installations remain backward compatible.
- Cloudflare Tunnel mode now accepts a tunnel token once from native Settings, stores it atomically in an owner-only `0600` credential file, and launches `cloudflared` with `--token-file` so the secret never enters settings, `.env`, or process argv; named-tunnel credentials remain an advanced option. The tunnel connects Cloudflare directly to the Mac without a VPS/public inbound port, while doctor verifies credential safety and the public `/health` route without sending connector credentials. Cloudflare is supervised by a user `launchd` job with `KeepAlive`; Start enables/bootstraps it and Stop boots it out/disables it, so no Terminal session is required.
- The interactive installer now includes public-endpoint onboarding: it asks for Local/Cloudflare/ngrok/Custom mode, offers Homebrew installation of the selected tunnel provider, guides Cloudflare Published application setup, accepts the tunnel token through hidden stdin, and safely falls back to Local only when public setup is deferred.
- [SEC-NET-001] Hardened outbound HTTP against SSRF by revalidating every redirect hop, rejecting non-global DNS answers, disabling inherited proxy routing, and re-resolving/pinning the validated IP at TCP connect time so DNS rebinding cannot pivot a public hostname into loopback/private/metadata space.
- Browser navigation now applies the same public/private destination policy before navigation and to the browser-observed destination afterward; unsafe redirected/rebound tabs are blocked with best-effort close/restore containment, while explicit local development exceptions require separate `HTTP_PRIVATE_ALLOWLIST` / `BROWSER_PRIVATE_ALLOWLIST` host entries.
- [SEC-FS-001] Hardened delegated file scopes against symlink/TOCTOU escapes with operation-time `dir_fd`/`O_NOFOLLOW` traversal for scoped read/write/edit/move/copy/delete/search and transaction snapshot/undo paths; adversarial post-validation swaps now fail closed without touching outside-workspace sentinels.
- Scoped recursive search/find/tree traversal no longer follows symlink directories, while local/unscoped file behavior remains unchanged; scoped cross-device moves fail explicitly rather than weakening the filesystem boundary.
- Added owner-only bounded filesystem transaction journaling for write/edit/move/delete operations, including reversible preimage snapshots, transaction receipts, post-state conflict detection, and `file_transaction_undo`.
- [SEC-TXN-001] Made `write_files_batch(..., atomic=true)` genuinely all-or-nothing and added mixed `file_transaction_batch` write/move/delete transactions; injected mid-batch failures restore every preimage byte-for-byte, while oversized or unavailable snapshots are explicitly marked irreversible or refused before atomic mutation.
- Added durable delegated-workflow checkpoints with task input hashes, sanitized provider cursors, provider/session lineage, resume generations, integrity-checked owner-only state, and two-phase hashed side-effect intents/receipts that survive daemon/worker restarts without storing raw tool payloads.
- Added fail-closed `agent_action(action="resume")`: verified interrupted work continues the same provider session without replaying completed side effects, while corrupt/mismatched checkpoints and unverifiable provider-native mutations return outcome-unknown conflicts; fresh `retry` is blocked after a side-effect boundary.
- Added `mac-mcp doctor` with stable diagnostic reason codes, human/JSON output, read-only Accessibility/runtime/dependency/companion checks, and an owner-only redacted support bundle that excludes credential values, raw configuration, logs, prompts, and chat content.
- Added a deterministic Computer Use conformance lab (`mac-mcp conformance`) with 14 CI-safe contracts covering focus-safe browser defaults, stable tab identity, stale-state handling, render/element readiness, bounded action batches, and no-effect action verification; optional `--live` mode adds read-only Mac/companion health checks.
- Added role-scoped learning for delegated `coder`, `reviewer`, and `orchestrator` agents: approved relevant lessons are injected with bounded top-k context, while run-generated lessons remain reviewable candidates until explicitly approved.
- Added structured lesson confidence/outcome tracking, duplicate merging, conflict reporting, decay/disable controls, and sticky-provenance isolation so untrusted web-derived context cannot enter or poison the trusted lesson pool.
- Added ChatGPT delegated-agent turn budgeting: 15-minute soft checkpoints continue the same live job/session, wait for active tools, and enforce a 20-minute hard tool ceiling without replaying completed side effects.
- Added ChatGPT web-throttle resilience with expanded “requesting too fast” detection, bounded exponential cooldown, post-throttle worker staggering, session-aware retry continuation, and checkpoint/throttle telemetry in the dashboard and menu app. ChatGPT subagents now default to High reasoning while extra-high remains opt-in.
- Added steering daemon generation/epoch safety: ambiguous retries are now bound to the daemon lifetime and old-generation retries fail as `stale_generation`/`outcome=unknown` instead of being replayed after a restart.
- Added bounded idempotency tombstones so late same-generation retries whose canonical dedupe entry aged out fail as `idempotency_expired` rather than silently enqueueing a duplicate instruction.
- Extended the menu-app pending steering correlation marker with the daemon generation while still persisting only metadata and the prompt SHA-256, never raw steering text.

## [2.1.3] - 2026-09-14

- Preserved ambiguous menu-bar steering submissions across native app relaunches with a short-lived owner-only correlation record, allowing the relaunched app to reconcile daemon state or reuse the original idempotency key without persisting raw prompt text.
- Added the dedicated **Mac MCP Chrome Companion** with a Manifest V3 service worker for focus-safe background tab creation, Chrome debugger-backed DOM/page execution, and background-safe visual capture.
- Added Chrome cold-start handling that launches the first requested tab without foregrounding Chrome, while preserving existing active tabs when Chrome is already running.
- Hardened Safari and Chrome interaction reliability on JS-heavy pages with bounded mutation watching, stable tab leases/handles, improved action verification, and fail-closed foreground-only fallbacks.
- Expanded the shared Browser Visual Companion with claimed-tab scoping and activity history while removing idle animation/backdrop-filter wakeups.
- Reduced idle menu/server overhead with adaptive polling, less frequent ngrok process checks, agent-only pulse wakeups, dashboard/agent caching, telemetry summary caching, and deterministic SQLite connection cleanup.
- Fixed Chrome bridge port generation to honor the actual runtime/custom port and preserve it across helper-process imports; this prevents the unpacked extension from reconnecting to the default `8000` port after a backend restart.
- Updated installer guidance and runtime preparation for the Chrome companion while keeping Safari bundled inside `Mac MCP.app`. Chrome unpacked-extension registration remains a one-time per-profile setup.
- Verified the release with 278 Python regression tests plus real background `browser_do` → `browser_observe` → `browser_act` smoke tests and Chrome cold-start focus monitoring.

## [2.1.1] - 2026-09-13

- Fixed the Safari Activity onboarding for locally built/ad-hoc-signed `Mac MCP.app` bundles. Safari does not register those bundles as normal installed Safari extensions, so `showPreferencesForExtension` could fail with “Could not open Safari extension settings.”
- The menu app now distinguishes a registered Safari extension from an unsigned local build. Registered builds keep **Enable in Safari…**; unregistered/ad-hoc builds show **Developer Setup…**, reveal the packaged extension resources, open Safari, and provide the temporary-extension steps.
- Updated the installer and README to state the actual distribution boundary: persistent Safari registration requires an Apple-signed app (Developer ID is supported outside the Mac App Store), while ad-hoc source installs use Safari's unsigned temporary-extension developer flow.
- Surfaced the real SafariServices error when a registered extension preference request fails instead of replacing it with a generic message.

## [2.1.0] - 2026-09-13

- Added **Safari Visual Companion**, a bundled Safari Web Extension that visualizes active Mac MCP browser work directly in the real page with a subtle frame, activity badge, synthetic cursor, and click ripple.
- Added visual states for high-level browser operations including Inspecting, Finding, Reading, Clicking, Typing, Selecting, Focusing, and Scrolling, without copying typed values, selectors, URLs, titles, page text, or secrets into extension events.
- Added the native **Safari Activity** menu-bar card with extension-state detection and an **Enable in Safari…** onboarding action backed by SafariServices.
- Extended the menu-app build and installer to compile, embed, sign, carry, and verify `Mac MCP Safari Visual Companion.appex`; local source builds remain ad-hoc signed while `MAC_MCP_CODESIGN_IDENTITY` supports a Developer ID release-signing path.
- Documented Safari extension enablement, per-site website-access permission, and the optional **Allow Unsigned Extensions** requirement for local/ad-hoc development builds.
- Prevented the visual event attribute from incrementing Mac MCP's browser DOM revision so the overlay cannot manufacture false page progress or interfere with the no-progress circuit breaker.
- Included the post-2.0.51 security/reliability work in the 2.1 line: source-aware web-to-host approvals, secret-egress protection, sticky untrusted provenance, no-progress browser protection, delegated tab leases, idempotent steering recovery, and dedicated-user hardening guidance.
- Verified the Visual Companion on a real Google Flights Antalya → Prague search, including live Inspecting, Finding, Typing, Clicking, and Scrolling feedback on the actual Safari page.

## [2.0.51] - 2026-09-10

- Added semantic `browser_do(extract=[...])` reads for compact natural targets such as `price`, `cancellation`, `parking`, `rating`, `breakfast`, and `payment`, while keeping existing selector-based `actions[].type="extract"` fully compatible.
- Bounded normal `browser_do` responses to an 8 KiB JSON budget; oversized full state and extracted text are compacted progressively, while `debug=true` keeps the existing raw diagnostic response path.
- Kept `return_state="none"` as the normal research default and tightened the core tool description so agents prefer semantic extraction instead of pulling large DOM state.
- Cached semantic DOM candidates once per extract action so several requested fields reuse the same page scan.
- Added regression coverage for semantic extraction, output budgeting, and wildcard delegated-browser scope.
- Real Safari/Etstur validation returned six hotel facts in about 0.9 KiB, versus about 10.8 KiB for a comparable full-state browser response in the same loaded page.

## [2.0.5] - 2026-09-10

- Added a compact 19-tool default MCP surface while preserving the previous 81-tool capability set through dynamic `tool_discover` / `tool_invoke` fallback; the full registered catalog is now 84 tools.
- Added `browser_do` for one-call open/wait/interact/extract/verify/close browser transactions and added targeted `extract` actions to avoid large DOM/HTML round trips.
- Reduced default browser observation size from 120 to 40 elements and made `mac_observe` screenshots opt-in by default.
- Hardened browser `network_idle` against transient `about:blank` loads and normalized hidden-tool results so fallback invocation preserves legacy result shapes.
- Preserved risk/profile/scope enforcement when invoking hidden tools dynamically; `tool_invoke` inherits the target tool's effective risk instead of bypassing policy.
- End-to-end compatibility-tested every previous tool: 80/81 executed successfully on the test Mac, while `set_brightness` remained a pre-existing local backend limitation on both 2.0.4 and 2.0.5. Real Safari, OpenCode agent, memory, skills, update-check, native dialog, and voice TTS/microphone/Whisper paths were exercised.
- Local schema measurement reduced advertised tool context from about 16.7k to 4.5k tokens (~73%) under the default core profile.

## [2.0.4] - 2026-09-10

- Fixed MCP-triggered detached self-updates by snapshotting both `update_helper.py` and `update_state.py`, so the standalone updater no longer fails on package-relative imports before the update starts.
- Moved detached updater state and logs under the external update-state directory instead of the runtime checkout, preventing single-checkout installations from dirtying their own Git worktree before the child updater runs.
- [SEC-UPD-001] Added guarded source-repository rollback to the exact pre-update local HEAD after post-merge failures; rollback uses `git reset --keep` only when branch, HEAD, and worktree state still match the updater's transaction, preserving concurrent user edits, commits, and untracked files.
- Expanded updater regression coverage for split and single-checkout rollback, deployed-marker/source divergence, new/deleted runtime files, concurrent user changes, and isolated staged-helper bootstrapping.
- Added guarded cleanup for detached updater staging directories on both handled success and failure paths, without allowing source/runtime directories to be removed.

## [2.0.3] - 2026-09-09

- [SEC-AUTHZ-001] Added central risk classification for all 81 MCP tools plus `trusted`, `standard`, and `read_only` permission profiles with fail-closed policy enforcement across MCP and REST dispatch.
- Added scoped delegated-agent ownership for path roots, browser tabs, job/terminal IDs, tool families, and access modes; child scopes can only narrow their parent scope.
- Added short-lived hashed scoped credentials for Codex/OpenCode agents, automatic revocation on completion/cancel/despawn, and policy-aware telemetry with agent/profile/scope context.
- Hardened delegated providers: Codex reapplies sandbox/approval policy on resumed sessions; OpenCode refuses fake read-only guarantees, carries explicit scope instructions for native tools, and supports scoped OpenRouter models without writing raw API keys to config.
- Hardened browser concurrency with per-tab leases, stable identity revalidation, request-specific visual capture state, and fail-closed behavior when a tab moves during an action.
- Fixed `MCP_ALLOW_SHELL=false` so terminal/background execution is blocked before process creation, and made delegated-agent metadata updates atomic across concurrent workers.
- Fixed scope/profile denials so MCP clients receive clear `scope_denied` / `profile_denied` tool errors instead of structured-output validation errors.
- Improved ngrok discovery for Apple Silicon/minimal-PATH launches by checking Homebrew binary locations explicitly.
- Added a dedicated README section for background browser isolation and no-focus-stealing automation.

## [2.0.2] - 2026-09-09

- Reworked delegated-agent rows around native SF Symbols: live reasoning/tool/finalizing/retry/terminal states now use semantic icons instead of raw phase strings, while completed/failed/cancelled/timeout/stalled states have distinct visual status indicators.
- Added compact provider/model formatting, reasoning-effort badges, tool-category icons, retry counts, and automatic active-agent scroll positioning without disrupting manual scrolling during normal polling.
- Normalized Codex JSON events (`turn.*`, `item.*`, command/tool events) into the same live phase and tool-call metadata used by OpenCode, including tool counts, last-tool tracking, session IDs, and usage metadata.
- Replaced raw CLI/terminal output in the menu bar with short native action notices that auto-dismiss after 5 seconds for success/info and 8 seconds for errors.
- Improved the active-agent robot layout to avoid clipping and expanded regression coverage for Codex event normalization.

## [2.0.1] - 2026-09-09

- Fixed single-checkout installations (`repo == runtime`) so update metadata and backups live under `~/.mac-mcp/update/` instead of dirtying the Git working tree after the first update.
- Added migration of successful legacy updater artifacts from the checkout into the external update-state directory.
- Added a pre-v2 upgrade bootstrap so users updating an older checkout receive and install the native `Mac MCP.app` automatically on the first v2 startup.
- Added regression coverage for repeated single-checkout updates and legacy state migration.

## [2.0.0] - 2026-09-09

- Added the native SwiftUI `Mac MCP.app` menu bar controller with no Dock icon and independent server lifecycle.
- Added menu bar Start/Stop/Restart/Update/Dashboard controls plus live server, ngrok, tool-call, success-rate, and delegated-agent status.
- Added compact internal scrolling for delegated-agent history and tool usage (five visible tool rows), plus an active-agent robot animation and pulsing status icon.
- Added a collapsed-by-default Voice disclosure panel with live `ask_user_voice` enable/disable behavior and explicit `ask_user` fallback when disabled.
- Added macOS Keychain Groq credential storage and CoreAudio input/output device selection.
- Added live runtime settings under `~/.mac-mcp/settings.json` without requiring server restart for voice changes.
- Extended the safe updater to carry `menu_app/` alongside `mcp_server/` and refresh an installed menu app on successful updates.
- Updated runtime/package versions to 2.0.0 and simplified the README around the current 2.0 architecture.

# Changelog

## [1.8.0] - 2026-09-08

- Added MCP-native `ask_user_voice` for hands-free human-in-the-loop interaction: the Mac speaks a short question using free neural Turkish TTS, records the local spoken answer, transcribes it with Groq Whisper, and returns the transcript to the calling agent.
- Added a lazily compiled native Swift microphone helper with macOS permission handling, silence-based end-of-speech detection, automatic fallback from a silent default input (for example AirPods) to the built-in Mac microphone, and automatic temporary-audio cleanup.
- Added temporary built-in-speaker routing with automatic restoration so voice prompts remain audible even when another output device is connected.
- Added voice configuration for Groq key sourcing, language, input/output device, and TTS rate without hard-coding secrets; `ask_user_voice` shares the existing interactive lock so text and voice prompts cannot stack.
- Added `edge-tts` for high-quality no-key speech synthesis, voice regression tests, and updated the MCP tool count to 81 while leaving the legacy 59-operation REST/OpenAPI surface unchanged.

## [1.7.0] - 2026-09-08

- Added a local-only live operations dashboard at `/dashboard` on the existing Mac MCP server port, with MCP/REST call telemetry, sanitized request/result inspection, success/error and latency metrics, Server-Sent Events, delegated-agent status, and SQLite history under `~/.mac-mcp/dashboard`.
- Added central FastMCP instrumentation so current and future MCP tools are observed automatically without per-tool dashboard wiring; the existing `audit.log` behavior remains unchanged.
- Added secret/binary redaction, bounded payload previews, 7-day / 20,000-event default retention, resilient SQLite schema recovery, and localhost-only enforcement so the dashboard is not exposed through the ngrok MCP tunnel.
- Added `mac-mcp dashboard` and startup dashboard URL output for local access, plus observability regression coverage for sanitization, persistence, recovery, SSE delivery, and loopback security.

## [1.6.4] - 2026-09-08

- Added stable browser `tab_handle` targeting while keeping the existing 80-tool MCP surface. Chrome uses the browser's native unique tab ID; Safari uses a synthetic registry that follows WebContent PID/URL/title so tab-index shifts no longer confuse long-running browser tasks.
- Made `browser_open_url` background-first: new tabs no longer activate Safari/Chrome or become the current tab unless explicitly requested, and the returned result includes the created `tab_handle`.
- Added `tab_handle` support to high-level browser observe/find/act and the main JS/selector/type/wait/get-html/scroll/snapshot paths; legacy window/tab indexes remain compatible.
- Native keyboard actions now fail closed with `foreground_required` unless `allow_foreground=true`, preventing silent focus theft during background automation. Browser screenshots no longer activate the browser before capture.
- Added regression coverage for Chrome native-ID stability, Safari PID-based stability after index shifts, and background keyboard gating.
- Live Safari validation preserved the user's active fourth tab while hidden test tabs were created, typed into, clicked, and scrolled; after a lower-index test tab was closed, the surviving handle resolved from index 6 to 5 and retained its DOM state.

## [1.6.3] - 2026-09-08

- Added five MCP-native Agent Skills tools: `skill_list`, `skill_search`, `skill_get`, `skill_register`, and `skill_update_index`; MCP tool count is now 80 while the legacy REST/OpenAPI surface remains unchanged.
- Added open `SKILL.md` support with YAML metadata, managed `~/.mac-mcp/skills/<name>/SKILL.md` discovery, optional scripts/references/assets resources, external registration, progressive loading, and a rebuildable SQLite FTS5/vector skill index.
- Extracted semantic inference into one shared embedding manager used by both persistent memory and Agent Skills; both searches reuse the same multilingual MiniLM/FastEmbed worker, cache, and idle timeout instead of holding separate model processes.
- Preserved the Mac-specific fallback chain (FastEmbed multilingual MiniLM → Apple NaturalLanguage → feature hash) and backward-compatible `MAC_MCP_MEMORY_*` embedding settings while adding shared `MAC_MCP_EMBEDDING*` settings.
- Verified the split runtime at `/Users/tarkanbulut/mac-mcp` with the full test suite, real shared-worker PID reuse, idle-process reclamation, Apple fallback, restart/health, and live MCP discovery/calls before syncing the distribution repository.

## [1.6.2] - 2026-09-07

- Moved FastEmbed/ONNX inference out of the main Mac MCP process into a dedicated on-demand worker subprocess, so the server itself never retains the multilingual model's large native memory arenas.
- The worker is started only by query-based semantic `memory_search`, is reused by searches within the warm window, and exits completely after 60 seconds of inactivity by default so macOS can reclaim its RAM deterministically.
- `memory_add`, `memory_update`, delete/index maintenance, and queryless listings do not start the worker; they may use the worker only if a semantic search already has it alive.
- Added `MAC_MCP_MEMORY_MODEL_IDLE_SECONDS` (default `60`, `0` exits the worker immediately after its first request) and regression coverage for lightweight non-query memory work and timeout validation.
- Memory tool APIs and the MCP tool count remain unchanged at 75.

## [1.6.1] - 2026-09-07

- Upgraded `memory_search` to a multilingual semantic backend using FastEmbed and `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions); the MCP tool count remains 75.
- Made the multilingual model lazy: add/update/delete and queryless listings never download it; the first query-based search downloads/caches the model, then subsequent searches reuse the local cache.
- Added automatic vector-backend migration so existing Apple/feature-hash SQLite entries are re-embedded from Markdown when multilingual search becomes available, without changing any memory tool API or Markdown files.
- Kept Apple NaturalLanguage and feature-hash vectors as offline fallbacks, and moved the model cache outside the memory source-of-truth tree to `~/.mac-mcp/cache/fastembed`.
- Added deterministic regression tests for Turkish semantic ranking, cross-language retrieval with zero lexical overlap, and backend migration.
- Real Python 3.14 benchmarks ranked the Earl Grey/bergamot memory first (`semantic_score` ~0.85) and an English brutalist-architecture memory first for a Turkish concrete-architecture query with zero lexical overlap; warm searches completed in about 10-12 ms on the test Mac.

## [1.6.0] - 2026-09-07

- Added five MCP-only persistent memory tools: `memory_add`, `memory_search`, `memory_get`, `memory_update`, and `memory_delete`; MCP tool count is now 75 while the REST/OpenAPI surface remains unchanged.
- Added human-readable Markdown source-of-truth storage at `~/.mac-mcp/memory/YYYY/MM/YYYY-MM-DD.md` with server-generated Europe/Istanbul (UTC+3) timestamps and stable `memory_id` values.
- Added queryless date/date_from/date_to listing plus timestamped selection modes for update/delete; deletion requires an explicit `confirm=true`.
- Added a rebuildable SQLite index with FTS5, cached vectors, automatic re-indexing after manual Markdown edits, tags/importance/source filters, and newest/oldest/relevance sorting.
- Added local semantic search through Apple's on-device NaturalLanguage 512-dimensional English sentence embedding when available, with a dependency-free feature-hash fallback.
- Added regression coverage for add/search/get/update/delete, date-range filtering, UTC+3 storage layout, deletion confirmation, manual Markdown re-indexing, and resource cleanup.

## [1.5.0] - 2026-09-07

- Added the MCP-only `mac_mcp_update` tool for commit-based update checks and detached safe updates from `origin/main`; MCP tool count is now 70 while the existing REST/OpenAPI surface remains unchanged.
- Added `mac-mcp update --check` and `mac-mcp update` terminal commands using the same update engine.
- Added split repo/runtime update support: runtime customizations are preserved as a Git overlay and merged against the incoming commit before any real deployment changes are made.
- Updates block on dirty repositories or runtime merge conflicts, preserve untracked runtime data such as `.env`/agent state/logs, and create managed-file backups before syncing.
- Added automatic service restart and `/health` verification with runtime rollback on failed health checks; deployed commit state is tracked separately from repository HEAD for safe retries.
- Dependency refresh is conditional on `mcp_server/requirements.txt` changes.
- Verified a real temporary GitHub old-commit update (`26071a2` -> `4615ad2`) with three runtime customizations and `.env` preservation, plus dirty-repo and merge-conflict fail-safe tests.

## [1.4.1] - 2026-09-07

- Fixed `browser_find` ranking so exact text/role targets beat prefixes/substrings; role/text constraints are hard filters and short tokens no longer match inside unrelated words.
- Added consistent Turkish/Unicode normalization between Python ranking and in-page JavaScript matching.
- Made custom `select` actions wait for asynchronously injected dropdown options and settle after selection instead of failing immediately.
- Added `content` / `leaf` observation scopes that prune large ancestor wrappers and prioritize controls, headings, meaningful leaf text, cards, rows, and images.
- Made `browser_find` and `browser_act(return_state="full")` adaptively reduce dense payloads instead of surfacing 413 truncation failures.
- `browser_act` can now resolve semantic targets (`query`, `text_match`, `role`) internally, allowing multi-filter flows to run in one MCP call without separate find calls.
- Real Safari/Sahibinden validation completed a full Antalya -> Konyaaltı -> 2+1 -> Search -> navigation/stability flow in about 5 seconds with one parent MCP call. Tool count remains 69.

## [1.4.0] - 2026-09-07

- Added three MCP-only high-level browser tools: `browser_observe`, `browser_find`, and `browser_act`; existing browser tools remain unchanged.
- Added compact visible/actionable DOM observations with stable page-scoped element IDs, robust base64-encoded JSON transport, viewport coordinates and best-effort screen coordinates.
- Added optional optimized JPEG viewport/element visual observations instead of requiring full-window PNG/base64 screenshots.
- Added semantic/fuzzy target ranking across text, ARIA labels, placeholders, roles, names, and titles.
- Added one-call batch browser actions for click, type, select, key, scroll, and bounded wait conditions with compact post-action state.
- Added stale observation/element checks and page-context persistence without modifying page DOM attributes.
- MCP tool count is now 69. The existing 59-operation REST/OpenAPI surface remains unchanged; the new browser-agent layer is MCP-only.

## [1.3.0] - 2026-09-06

- Added `spawn_agents` for one-call parallel teams of up to 10 agents with persistent `team_id` state and enforced shared provider/model/reasoning/access configuration.
- Added `wait_agents` with bounded `all`, `any`, and `majority` completion modes to replace repeated status polling.
- Added progress/timing telemetry including phase, first-event latency, idle time, step/tool counts, and last tool.
- Added `idle_timeout_s` and automatic same-model retries; teams default to one retry with a short backoff, and never implicitly fall back to another model.
- Extended `list_agents` with team filtering and `agent_action` with team-level cancel/retry/despawn and cancellation cascade.
- Kept team wait output compact (2,000 characters per child handoff) to protect parent-chat context.
- MCP tool count is now 66. The existing 59-operation REST/OpenAPI surface remains unchanged; agent orchestration remains MCP-only.

## [1.2.0] - 2026-09-06

- Added five MCP-native agent delegation tools: `agent_catalog`, `spawn_agent`, `list_agents`, `get_agent`, and `agent_action`.
- Added non-blocking OpenCode and Codex execution with provider/model/reasoning selection, working-directory and access controls, bounded timeouts, and persistent on-disk agent state.
- Added concise parent-agent handoffs so delegated work can use its own context without flooding the calling ChatGPT conversation with intermediate logs or reasoning.
- Added resumable provider sessions (`message`), retries, cancellation, despawn, compact listings, and opt-in debug logs.
- Added macOS-aware OpenCode/Codex binary discovery, including Homebrew and the ChatGPT-bundled Codex binary.
- Verified real OpenCode and Codex delegation, session follow-up, retry, three parallel agents, timeout handling, cancellation cleanup, and provider failure handling.
- Added unit coverage for final-result extraction and provider command construction.
- MCP tool count is now 64. The existing 59-operation REST/OpenAPI surface remains unchanged for backwards compatibility; agent delegation is currently MCP-only.

## [1.1.1] - 2026-09-03

- Fixed `ask_choice` so three choices fit macOS's three-button native dialog limit; two-choice dialogs retain a visible Cancel button and four-or-more choices are rejected clearly.
- Added native `ask_choice` and `ask_confirmation` human-in-the-loop tools with bounded, fail-closed dialog handling.
- Prevented concurrent native prompts from stacking by returning `prompt_busy` immediately when a dialog is already active.
- Hardened timeout cleanup for AppleScript, browser, shell, and interactive subprocesses so descendants do not remain stuck in the background.
- Exposed all 59 MCP tools as one-to-one Custom GPT Action operations with matching `operationId` names.
- Added REST aliases for file, macOS, browser, search, `mac_observe`, and `mac_act` tools.
- Kept the legacy grouped REST routes available for backwards compatibility while hiding them from the published OpenAPI schema.
- Updated the bundled OpenAPI schema to version 1.1.1 and verified 59 valid operations.
- Documented the OpenAPI/Custom GPT refresh flow and added the new release notes.
