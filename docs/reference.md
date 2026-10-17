# Developer reference

The MCP and REST surfaces, server commands, Computer Use plans, voice, the operations dashboard and tool coverage.

[← Back to the README](../README.md)

## For developers

Mac MCP exposes your Mac through a native MCP endpoint and a REST/OpenAPI surface, with shell, files, browser automation, macOS UI control, delegated OpenCode/Codex/Claude Code agents, memory, Agent Skills, voice interaction, self-update tooling, and a local operations dashboard. The native MCP endpoint is the full capability surface; REST/OpenAPI intentionally publishes a selected compatibility subset, so some capabilities remain MCP-only.

> **Security:** Mac MCP can execute commands, read/write files, and control desktop apps. Keep MCP authentication enabled whenever the service is reachable outside localhost and expose it only to clients you trust. The operations dashboard is loopback-only **and** requires a separate per-user dashboard Bearer token; localhost is machine-local transport, not a same-user sandbox.

**Secure bootstrap defaults:** missing configuration fails closed. Without explicit settings, MCP authentication is required, shell execution and HTTP/browser host allowlists are disabled, and the global permission profile defaults to `standard` rather than `trusted`. A normal installer run generates the API key and writes the intended settings explicitly. Deliberate `MCP_ALLOW_NO_AUTH=true` is accepted only on loopback with no managed public endpoint; non-loopback or tunneled no-auth startup is refused.

## Server commands

```bash
mac-mcp start
mac-mcp start --public-mode ngrok
# Save the Cloudflare token once in Mac MCP Settings → Advanced.
mac-mcp start --public-mode cloudflare --public-url https://mac.example.com/mcp
mac-mcp start --public-mode custom --public-url https://mac.example.com/mcp
mac-mcp start --public-mode none
mac-mcp status
mac-mcp status --json
mac-mcp restart
mac-mcp stop
mac-mcp dashboard
mac-mcp doctor
mac-mcp conformance
mac-mcp autostart            # show whether Mac MCP starts at login
mac-mcp autostart off        # or: on
mac-mcp repair               # check the parts Mac MCP manages; --yes fixes them
mac-mcp uninstall            # preview; --yes removes (to the Trash)
```

**Start at login** is on by default: the crash supervisor's launchd job lives in `~/Library/LaunchAgents`, so after a restart or a new login a server you left running starts again, together with the menu bar app. A server you stopped with `mac-mcp stop` (or the menu bar app) stays stopped. Turn it off with `mac-mcp autostart off` or **Settings → General → Startup**; the job then moves back to the state folder and only `mac-mcp start` loads it.

**Repair and uninstall.** `mac-mcp repair` checks the Python environment, the `mac-mcp` command, the menu bar app and the start-at-login job, and with `--yes` fixes only what is broken. If the `mac-mcp` command itself is broken, run the runtime's copy directly: `~/mac-mcp/.venv/bin/mac-mcp repair --yes`; if the Python environment is gone, `python3 ~/mac-mcp/mcp_server/venv_repair.py repair` rebuilds it. `mac-mcp uninstall` shows what it would remove and changes nothing; `mac-mcp uninstall --yes` stops Mac MCP and moves the menu bar app, the runtime (with its API key), the `mac-mcp` command and the login jobs into one dated folder in the Trash, so nothing is deleted. Your settings, keys, tokens and memory in `~/.mac-mcp` and a source checkout are kept unless you add `--include-state` or `--include-source`. It does not edit AI app configurations, the PATH line in `~/.zprofile`, saved logins in your Keychain or the Chrome companion.

The legacy `--ngrok` flag remains supported as an alias for `--public-mode ngrok`. When no CLI override is supplied, `start`/`restart` use the public endpoint mode saved by the native Settings window (or the `MAC_MCP_PUBLIC_*` environment overrides).

`mac-mcp doctor` performs read-only checks for the local runtime, Python/version, disk space, state/settings validity, macOS permissions, required/optional helpers, local server health, selected public endpoint health/configuration, dashboard credential file metadata, and Safari/Chrome companion state. Use `--json` for automation; the JSON reports `local_ok`, `public_endpoint` and `health` (`healthy`, `degraded`, `failed`) separately. A selected public endpoint that cannot be reached makes `doctor` exit 1 and `mac-mcp status` exit 2 (1 means the local server is down); pass `--local-only` to either command to judge only the local runtime. `--support-bundle [PATH]` writes an owner-only (`0600`) structured support report; it intentionally excludes raw `.env`, settings values, logs, credentials, cookies, prompts, and chat content.

Permissions are read from the running server, because macOS records consent for the process that asks: `doctor` reports Accessibility, Screen Recording and per-app Automation (System Events, Safari, Chrome, Calendar, Reminders, Notes, Mail) as allowed, not allowed, not asked yet or not checked, with the features each one enables and the exact System Settings path and app name to allow. These are read-only queries that never show a permission prompt. Microphone access belongs to the separate Mac MCP Voice Helper, so it is described rather than checked. When the server is not running, `doctor` says so and describes its own process instead. Port conflicts name the program using the port (Mac MCP never stops a program it did not start) and point to **Settings → Advanced → Server Port**; tunnel problems say whether the tunnel binary is missing, the credential is unsafe, the tunnel process stopped, or the public route does not answer.

The same checks are in the app under **Settings → Help & Diagnostics**: run or recheck diagnostics, use the fix button next to each problem (restart, open the right setting or System Settings pane, view the redacted log), export a support report after seeing what it contains, view the server or tunnel log, copy version information, and open the documentation, issue form or private security policy. Nothing is sent anywhere automatically.

**In a terminal** the CLI uses Mac MCP's own look: `mac-mcp` alone shows the commands in groups with a one-line status, and every command (and every `--help`) opens with the same header, line their facts up and end with one sentence saying what happened (with the command to run next when something is wrong). `start`, `stop` and `restart` show each step live with its time; in a terminal `restart` waits for the outcome, as `--wait` does. `doctor` groups its checks and folds the ones that passed; `doctor --verbose` lists every check. A token or password typed into the terminal (`credential cloudflare save`, `logins add`) is read without being shown. The text is plain, exactly as before, whenever the output is not a terminal (pipes, scripts, the menu app, the installer), with `--json`, or with `NO_COLOR` or `MAC_MCP_PLAIN=1` set. On a light terminal background the colours turn deeper by themselves (the CLI asks the terminal for its background); `MAC_MCP_THEME=light` or `dark` chooses by hand.

`mac-mcp status --json` prints one object for scripts: `ok`, `state`, `exit_code`, `server` (`running`, `pid`, `identity`, `port`, `health` from the local `/health` route), `public_endpoint` (`mode`, `url`, `tunnel_running`, `route` — whether the public `/health` answers — and `error`), `stray_processes`, `supervisor` and `remediation`. `state` tells the cases apart: `healthy`, `stopped`, `port_conflict`, `ownership_unverified`, `unresponsive` (process alive but `/health` does not answer), `degraded` (selected tunnel not running, or running while its public route does not answer) and `config_error` (public endpoint settings invalid). `doctor --json` carries the same `exit_code` field.

CLI exit codes are a stable contract:

| Command | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `status` | healthy | server stopped, unverified, port taken or not answering `/health` | public endpoint unavailable or misconfigured (0 with `--local-only`) | — |
| `doctor` | all checks pass | a check failed | — | — |
| `start` | started | could not start (for example port conflict) | invalid configuration or security bootstrap error | ngrok started but its public `/health` does not answer yet |
| `stop` / `restart` | done | a component did not stop or come back healthy | — | — |
| `restart --wait` | restarted and healthy | restart failed, or only the public endpoint failed (degraded) | — | no outcome reported in time |
| `update --check` | checked | check failed | local changes block updating | — |
| `update` | updated or already current | update failed or was blocked | — | — |
| `recipe run` | completed | failed | needs approval | server not running |
| `recipe list` | listed | request failed | — | server not running |

In short: 0 is success, 1 is an operational failure, 2 means a person has to act (configuration, approval, a degraded connector, or invalid command-line usage), and 3 means the server could not be reached.

`mac-mcp logs [server|cloudflared|ngrok|audit|update] [-n LINES]` prints the last lines of one log with tokens, keys and credential values redacted; `mac-mcp logs --list` shows every log's size and the bounds. Server, `cloudflared` and `ngrok` logs rotate at 10 MB and keep three older files (`MAC_MCP_LOG_MAX_BYTES`, `MAC_MCP_LOG_BACKUPS`); rotation copies and truncates, so the running process keeps writing. The audit log (tool, outcome and duration only, `0600`) rotates at 5 MB with three older files, and only the newest 20 update logs are kept (`MAC_MCP_UPDATE_LOGS_KEPT`).

**Crash supervision.** `mac-mcp start` records that the server should run and loads a small launchd job (`com.macmcp.supervisor`, every 30 seconds; its plist stays in `~/.mac-mcp`, so nothing new starts at login). If the server process is gone, or stops answering `/health` for four checks in a row, it is started again through the normal `mac-mcp start`; an exited ngrok tunnel is restarted the same way while the server keeps running. `mac-mcp stop` records the stop first, so the supervisor never undoes it, and the supervisor steps aside during updates and restarts. After three failed recoveries in 15 minutes it backs off. `mac-mcp status` and `doctor` (also **Settings → Help & Diagnostics**) show the last recovery and why; `MAC_MCP_SUPERVISOR=0` turns supervision off.

**Restarts.** `mac-mcp restart` checks the configuration, the tunnel binary and the runtime Python before it stops anything, and leaves a working server alone if a check fails. If the new server does not come up, it retries once; the final state (`succeeded`, `degraded` when only the public endpoint failed, or `failed` with a repair command) is written to `~/.mac-mcp/restart-status.json`. `restart --wait` waits for that outcome, which is what the app's Restart button reports.

**Updates.** Only one update runs at a time; a second request reports the running update's ID and status. The newest 10 runtime backups are kept (`MAC_MCP_UPDATE_BACKUPS_KEPT`), plus the one recovery needs, and each update removes scratch folders and worktrees left by an updater that was killed. Finished agents' worktrees with nothing left to review are removed after 7 days (`MAC_MCP_AGENT_WORKTREE_RETENTION_DAYS`, `0` keeps them); `doctor` shows backup and worktree disk use.

**If the runtime Python disappears.** When a Homebrew or macOS upgrade removes the Python the runtime venv was built from, `mac-mcp` says so and prints the repair command instead of failing with "bad interpreter":

```bash
python3 ~/mac-mcp/mcp_server/venv_repair.py check
python3 ~/mac-mcp/mcp_server/venv_repair.py repair
```

`repair` builds and verifies a new venv beside the old one with the newest supported Homebrew Python, swaps it in, keeps the old one as `.venv.previous-<timestamp>` and restores it if the new one fails its checks. `doctor` warns ahead of time when the venv points at a versioned Homebrew path that the next upgrade will remove.

`mac-mcp conformance` runs the deterministic Computer Use regression lab. Its default suite is CI-safe and verifies contracts such as background browser behavior, explicit foreground fallbacks, stable tab identity, stale-handle rejection, render/element readiness, bounded action batches, and no-effect click handling. `--live` adds read-only checks against this Mac without clicking or typing in the user's applications.

### Closed-loop Computer Use plans

`computer_plan` defaults to plan schema v2 for bounded multi-step browser/native workflows. In addition to ordinary tool steps and `$ref` result reuse, a plan can use `wait_until`, conditional `branch`, bounded `retry`, and a single safe `fallback`. Recoverable **pre-mutation** stale/readiness failures can trigger a fresh observation and semantic target rebind inside the same model tool call: browser targets use semantic `browser_find`, while native targets prefer `AXIdentifier` and otherwise require a unique role/title/description fingerprint. Ambiguous matches fail closed rather than guessing.

Recovery has independent count/time budgets plus the existing total plan/action-unit limits. Mutating work is never automatically replayed after `ACTION_NO_EFFECT`, verification uncertainty, policy denial, `outcome_unknown`, an exception crossing a mutating tool boundary, or a partially successful action batch. Optional plan `resources` use the same global ownership state as delegated-agent admission, so a conflicting browser tab/native/workspace resource can fail at preflight before any plan step runs. Every nested action still goes back through normal Mac MCP policy, scope, browser/native leases, telemetry and effect verification.

## Voice

### ChatGPT Voice as the interface

When Mac MCP is connected as a ChatGPT plugin, supported ChatGPT Voice experiences can use it during a live conversation. That lets you speak to ChatGPT naturally while Mac MCP carries out supported work on the Mac.

Mac MCP does not replace or modify ChatGPT Voice. It provides the execution layer behind the conversation.

### Local voice interaction

`ask_user_voice` is a separate Mac MCP capability. It lets the agent speak a short prompt through the Mac, record the local answer, transcribe it with Groq Whisper, and continue the task with the returned transcript.

Voice is **off until you turn it on**, because it sends data to third parties: the question text goes to Microsoft's online text-to-speech (`edge_tts`) and the recorded answer goes to Groq for transcription, where Groq's own retention applies. Before every recording Mac MCP shows a dialog to **Record**, **Always Allow** or decline; nothing is recorded or sent before that choice, and a declined or unanswered dialog returns `skipped` with `fallback_tool: ask_user`. **Always Allow** can be revoked in **Settings → Voice → Ask before every recording**. The recording is deleted from the Mac afterwards, the transcript is replaced with `[voice transcript not stored]` in activity history, and each decision is logged as a `voice_egress` security event with only provider, model, consent and outcome.

The menu app manages:

- experimental enable/disable toggle;
- Groq API key in macOS Keychain;
- input microphone;
- output device;
- language;
- timeout;
- TTS voice and rate.

Non-secret settings are stored in:

```text
~/.mac-mcp/settings.json
```

Environment variables remain supported as fallbacks, including `MAC_MCP_VOICE_GROQ_API_KEY`, `GROQ_API_KEY`, `MAC_MCP_VOICE_LANGUAGE`, `MAC_MCP_VOICE_INPUT_DEVICE`, `MAC_MCP_VOICE_OUTPUT_DEVICE`, and `MAC_MCP_VOICE_TTS_RATE`.

## Operations dashboard

Open it with the authenticated local launcher:

```bash
mac-mcp dashboard
```

The static dashboard shell is loopback-only. Every sensitive `/dashboard/api/*` request and the live `/dashboard/events` stream additionally require a separate dashboard Bearer token stored at `~/.mac-mcp/dashboard-token` with mode `0600`; the state directory is kept owner-only (`0700`). The CLI/menu app passes the browser credential in a URL **fragment**, which is not sent in the HTTP request, and dashboard JavaScript immediately moves it to `sessionStorage`, removes it from the address bar, and uses an `Authorization` header for API/SSE requests. The global connector `MCP_API_KEY` is not exposed to the browser.

The dashboard records sanitized MCP/REST tool activity, status, latency, recent delegated-agent state, active calls, and tool frequency. Provider identity fields such as OpenAI session/subject/organization/location are dropped from telemetry; the security migration also scrubs legacy persisted rows on first startup. Telemetry persists locally under:

```text
~/.mac-mcp/dashboard/telemetry.sqlite3
```

Loopback means **machine-local**, not **user-private**. The dashboard token prevents unrelated unauthenticated local processes and browser-origin requests from using sensitive endpoints, but a malicious process already running as the same macOS user can generally read that user's files and is inside this trust boundary. Unix-domain sockets were evaluated for menu-app ↔ daemon traffic; they can provide filesystem-owner permissions but do not solve same-UID isolation and cannot be consumed directly by the browser dashboard, so authenticated loopback HTTP remains the single transport. See `docs/LOCAL_API_SECURITY.md` for the threat model and decision.

## Tool coverage

Mac MCP 2.1 advertises a compact core surface by default, backed by a larger centrally registered MCP capability catalog. The exact visible catalog is permission-profile and delegated-scope aware: `list_tools` and `tool_discover` use the same availability policy, while `tool_invoke` preserves the nested tool's success/failure status.

Set `MAC_MCP_TOOL_PROFILE=full` to advertise every tool allowed by the active permission profile directly to the client. You can also add selected tools to the compact surface with `MAC_MCP_CORE_EXTRA_TOOLS=name1,name2`. Exact catalog counts are intentionally runtime-derived rather than hard-coded here so documentation cannot drift as tools are added or removed.

The capability set covers:

- terminal/system and background jobs (each output stream keeps up to 16 MB, `get_job_output` reads only the requested slice, finished jobs expire after 7 days, 200 jobs or 1 GB, and `delete_job` removes one; override with `MAC_MCP_JOB_STREAM_MAX_BYTES`, `MAC_MCP_JOB_RETENTION_DAYS`, `MAC_MCP_JOB_RETENTION_COUNT`, `MAC_MCP_JOB_RETENTION_BYTES`);
- delegated OpenCode/Codex/Claude Code agents;
- file management;
- macOS automation and Accessibility UI control;
- saved recipes: `computer_plan(save_as_recipe="name")` keeps a successful plan as a draft in `~/.mac-mcp/recipes` (owner-only); `recipe(action="update", parameterize=[{"literal": "October", "param": "month"}])` turns fixed values into typed parameters, `recipe(action="activate", confirm=true)` makes it runnable after review (secret-like values are refused), and `recipe(action="run", values={...})` validates the values and runs the steps through `computer_plan` with the usual policy and verification; recipes can be listed, inspected, paused, resumed and deleted;
- Settings › Automation › Recipes (owner-token dashboard routes `/dashboard/api/recipes/manage`, `/detail`, `/action`): every recipe including drafts, its steps and parameters, turn on (the same secret and parameter checks as `activate`), pause, resume, delete, run with values and copy its link;
- recipe launchers outside a chat: `mac-mcp recipe list` and `mac-mcp recipe run rcp_… --param month=November` (exit 0 done, 1 failed, 2 needs approval, 3 server not running) for Raycast script commands or a Shortcuts "Run Shell Script" action, and `macmcp://recipe/run?id=rcp_…&month=November` links for a Shortcuts "Open URL" action. A link always asks for confirmation in Mac MCP before it runs and the result arrives as a notification; launchers can only run activated recipes and pass plain values, never tool names or commands;
- typed `mac_app` adapters for Finder, Notes, Mail, Calendar, Reminders, Preview and System Settings, including Calendar `create_event`/`update_event`, Reminders `list_reminders`/`complete_reminder`, Notes `create_note` and Mail `create_draft` (saved to Drafts only, never sent; with several Mail accounts the sender must be named): each returns the item's stable id with a read-back check, a repeated create returns the existing item instead of a twin, and an uncertain result is reported as `outcome_unknown` rather than retried; Reminders `create_reminder` (title, optional list, due date or time, notes), Notes `append_to_note` (adds text to the end of one note and reads it back), Calendar `find_free_time` (free stretches of a given length within working hours over the next days; all-day events such as holidays do not count as busy; simple repeating events are expanded, and a repeating rule it cannot expand makes the answer `approximate` with the event named), Contacts `find_contacts` (name or organization, or an email address or phone number, with emails and phones), and Shortcuts `list_shortcuts`/`run_shortcut` (and, the other way round, a **Run Mac MCP Recipe** action in the Shortcuts app and Siri that runs a saved recipe after the same confirmation as a `macmcp://recipe/run` link) (by id or exact name, text in and out; a shortcut can do anything its actions do, so running one is allowed only when running commands is, or when the owner turned that shortcut on in Settings › Apps, and a failed run says `outcome_unknown`); Reminders `update_reminder` (rename, move the due date to defer it, replace notes) and `delete_reminder` (a replay finds nothing left and changes nothing), Notes `update_note` (by item_id only; replaces the title and/or text, keeping the title first), Calendar `find_free_time` `buffer_min` (minutes kept free around each event) and `plan_day` (one day read-only: its events, the free time between them, and reminders due by its end, overdue ones marked), Mail `update_message` (mark read or unread, flag or unflag, move to a mailbox of its account or `archive`, each read back), `create_draft` `attachments` (file paths, up to 10 files and 25 MB; the result says how many Mail shows on the draft) and recipients given by name (a contact name becomes that person's address only when it leads to exactly one; otherwise nothing is done and the candidates are listed; `send_mail`'s confirmation shows the addresses);
- Safari/Chrome browser automation with stable tab handles and background visual observation; `browser_checkpoint` hands a sign-in, 2FA, passkey or captcha step to you (a notification, then the agent waits up to 5 minutes or checks back) and resumes once that tab no longer shows the challenge, without reading or storing passwords or codes; with the Chrome companion, agents also work inside cross-origin frames (`frame=`), see native alert/confirm/prompt dialogs and answer them only on an explicit decision, use real hover, drag and key events, and click canvas apps by viewport coordinates;
- HTTP and search;
- text/choice/confirmation/voice human input;
- persistent memory;
- Agent Skills;
- safe self-update.

Use MCP tool discovery for the authoritative live schema. `tool_discover` ranks tools against a plain-words query, says why each matched and pages long results with `next_cursor`.

Listings and multi-file reads report when they stop early: `list_directory`, `find_files` and `list_jobs` return `page.has_more`/`page.next_cursor`, `read_multiple_files` spends one total character budget and lists unread files in `not_read`, and `read_file` gives `next_offset` for the next line.

Failures carry one machine-readable contract on every transport: an MCP error ends with an `error_contract={...}` line and a REST error body has an `error` object next to `detail`, both with `code`, `stage`, `outcome` (`not_executed`, `completed` or `unknown`) and `retry` (`fix_arguments`, `safe_retry`, `observe_again`, `wait_for_user` or `never_retry`). An `unknown` outcome never says `safe_retry`; observe the current state before acting again. A tool that ran and reported failure (for example a non-zero shell exit) is a normal result with `ok: false`, not an error.

State-changing MCP tools accept an optional `idempotency_key` (8-128 characters). Repeating a completed call with the same key and arguments returns the first result marked `idempotent_replay` instead of running it again; the same key with different arguments is `idempotency_key_conflict`; a repeat while the first call is still running or ended with an unknown outcome is refused, never re-run. Keys are scoped to the caller (agent or authenticated actor, so a reconnected client still finds its call) and kept for 24 hours in an owner-only `~/.mac-mcp/idempotency.sqlite3`; results that look like secrets or exceed 256 KB are not stored. This is deduplication, not exactly-once execution.

REST has a versioned surface generated from the MCP tool contract: `POST /api/v2/<tool>` takes the MCP tool's own input schema and runs through the same policy, approvals, telemetry, `idempotency_key` and error contract. A completed call returns the tool's result with HTTP 200 (even when it reports `ok: false`); a failure returns `{"error": {...}}` with the contract's status. The schema is published in `openapi/mac-mcp-v2.json` (API version 2.0.0, product version in `x-product-version`), regenerated with `python -m mcp_server.rest_v2 --write` and checked for drift in tests. The unversioned `/api` routes and `openapi/custom-gpt-actions.json` keep working unchanged.

### Your own MCP servers

Mac MCP is also an MCP client. Servers listed in `~/.mac-mcp/mcp_servers.json` (Settings › MCP Servers, or `mac-mcp mcp`) use the same `mcpServers` shape as Claude Desktop and Cursor:

```json
{"mcpServers": {
  "exa": {"url": "https://mcp.exa.ai/mcp"},
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "…"}}
}}
```

A server with `url` is remote (Streamable HTTP; `"type": "sse"` for SSE) and may set `headers`; one with `command` is started locally over stdio with `args`, `env` and `cwd`. Optional fields: `enabled` (default true), `listed` (default true: its tools appear in the compact tool list; false: reachable through `tool_discover`/`tool_invoke` only) and `timeout_s` (per call, default 120). Server names are lowercase letters, digits, `-` and `_`, at most 24 characters.

Each remote tool is registered as `<server>__<tool>` with the remote input schema and a central risk entry in family `external_mcp`: `readOnlyHint: true` gives read + network access, anything else network + external side effect, and only an explicit `destructiveHint: true` is destructive (allowed by Trusted, not Standard). Every call runs through the normal Mac MCP path (profile, delegated scope, steering, Tool Activity, idempotency, error contract) before it is forwarded; a remote `isError` result becomes a tool error. Connections are held on one background event loop, the config file is re-read when it changes, `notifications/tools/list_changed` refreshes a server's tools, and a dropped server is reconnected before the next call (a call that was already sent is never retried). Local servers' stderr goes to `~/.mac-mcp/external-mcp/<server>.stderr.log`. The dashboard API is `GET /dashboard/api/mcp-servers` and `POST /dashboard/api/mcp-servers/action`; neither returns header or env values.
