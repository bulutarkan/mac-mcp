<p align="center">
  <img src="assets/screenshots/mac-mcp.png" alt="Mac MCP" width="760">
</p>

# Mac MCP 2.1.3

Mac MCP is a local macOS control server for AI agents. It exposes your Mac through a native MCP endpoint and a REST/OpenAPI surface, with shell, files, browser automation, macOS UI control, delegated OpenCode/Codex agents, memory, Agent Skills, voice interaction, self-update tooling, and a local operations dashboard.

> **Security:** Mac MCP can execute commands, read/write files, and control desktop apps. Keep MCP authentication enabled whenever the service is reachable outside localhost and expose it only to clients you trust. The operations dashboard is loopback-only **and** requires a separate per-user dashboard Bearer token; localhost is machine-local transport, not a same-user sandbox.

## What's new in 2.1.3

- Added a dedicated **Mac MCP Chrome Companion** for true non-focus-stealing background tabs, Chrome DOM/page actions, and background-safe visual capture. Running Chrome uses `tabs.create({active:false})`; cold starts also stay in the background.
- Hardened **Safari + Chrome** browser automation for JS-heavy pages with bounded interaction observers, stable tab ownership, safer action verification, and fail-closed foreground fallbacks.
- Reduced idle energy use in `Mac MCP.app`: adaptive menu polling, less ngrok/process polling, event-driven agent pulse updates, telemetry caching, closed SQLite connections, and no idle Visual Companion animation/blur loops.
- Fixed Chrome Companion bridge configuration so custom/runtime ports survive backend restarts and helper-process imports instead of silently falling back to port `8000`.
- Installer/updater carry the latest Safari and Chrome companion sources. Safari is bundled into `Mac MCP.app`; Chrome's owner-only bridge config is prepared automatically and the unpacked extension is a one-time Chrome profile setup.
- Regression-tested the release with the full Python suite plus real Safari/Chrome background browser smoke tests, including Chrome cold start without stealing application focus.

## Browser automation that doesn't hijack your Mac

Mac MCP can inspect and interact with Safari and Chrome tabs in the background while you keep working in another app or browser tab. Here, **background** means a normal, visible Safari/Chrome tab that Mac MCP controls without bringing the browser or tab to the front; it is not a hidden/headless browser session.

- New browser tabs open in the background by default and return a stable `tab_handle`.
- Stable tab handles survive tab-index changes, so long-running tasks keep targeting the intended Safari or Chrome tab even as other tabs open, close, or move.
- `browser_observe` can return compact DOM context plus viewport, element, or full-page visuals without activating the browser, switching tabs, scrolling the user's page, or leaving screenshot files on disk.
- High-level browser actions can target a specific background tab directly by handle, which makes parallel research and delegated-agent workflows practical without constant focus stealing.
- Foreground-only fallbacks such as native key presses and absolute coordinate clicks fail closed unless foreground access is explicitly requested.
- The native menu bar controller surfaces live browser work in **Sessions** and **Latest Tool Usage** with a privacy-minimized browser/site/action summary. URL paths, query strings, page titles, selectors, and page content are intentionally omitted from this compact view.
- **Show Tab** is an explicit user action: normal automation remains non-focus-stealing, while clicking Show Tab brings that specific real Safari/Chrome tab to the front.

This is designed for workflows where an AI agent keeps working in one or more background browser tabs while the Mac remains usable normally.

### Browser Visual Companion (Safari + Chrome)

`Mac MCP.app` uses one shared WebExtension source for Safari and Chrome to make active Mac MCP browser work visible inside the exact page being automated. The extension is display-only: it renders a subtle pulsing page frame, a small `Mac MCP · …` activity badge, a synthetic cursor, and click feedback for high-level browser actions. Visual events contain only bounded action labels and viewport coordinates; typed text, selectors, URLs, page titles, DOM content, and secrets are not copied into the extension event. The overlay is activity feedback only and must not be treated as a security or trust indicator.

Safari setup depends on how `Mac MCP.app` is signed:

**Developer ID / Apple-signed build (persistent):**

1. Install or update Mac MCP normally.
2. Open **Mac MCP.app → Browser Activity → Enable in Safari…**. You can also use **Safari → Settings → Extensions**.
3. Turn on **Mac MCP Visual Companion** and grant website access for the sites where you want activity feedback.

**Local GitHub/source build (ad-hoc, development mode):**

1. Open **Mac MCP.app → Browser Activity → Developer Setup…**. Mac MCP reveals the runtime `BrowserVisualCompanion` source folder and opens Safari.
2. In Safari, enable web-developer features if the **Develop** menu is hidden.
3. Choose **Develop → Allow Unsigned Extensions**.
4. Choose **Develop → Add Temporary Extension…** and select `~/mac-mcp/menu_app/BrowserVisualCompanion`.
5. Grant website access when Safari asks. Safari treats this as a development/temporary extension; persistent normal installation requires an Apple-signed app bundle.

For Chrome, true background tab creation uses the Chrome-only `~/mac-mcp/menu_app/ChromeVisualCompanion` extension. Open **Mac MCP.app → Browser Activity → Chrome Setup…**, enable **Developer mode** at `chrome://extensions`, choose **Load unpacked**, and select that folder. The companion opens new tabs with Chrome's native `tabs.create({active:false})` API, so background work does not bring Chrome or the new tab to the front. If the companion is unavailable, background tab creation fails closed rather than falling back to a focus-stealing AppleScript open. The same companion also carries DOM/page execution through Chrome's `debugger` API, so normal Mac MCP Chrome reads, clicks, typing and background-safe visual capture do not require the Apple Events JavaScript toggle while the companion is connected. The Chrome companion uses a dedicated owner-only local credential and a loopback WebSocket; the credential is not the global `MCP_API_KEY`.

The project remains fully open source and does **not** need the Mac App Store. For a persistent GitHub release, sign/notarize the distributed `Mac MCP.app` with Developer ID; `menu_app/build_app.sh` accepts `MAC_MCP_CODESIGN_IDENTITY` for that release path.

No separate browser profile, helper daemon, or Xcode project is required. `menu_app/build_app.sh` compiles the `.appex` into `Mac MCP.app/Contents/PlugIns/` with the normal command-line Swift toolchain. Local builds default to ad-hoc signing; release builders can set `MAC_MCP_CODESIGN_IDENTITY` to use a Developer ID identity with hardened runtime/timestamp signing.

## Requirements

- macOS 13+
- Apple Silicon or Intel Mac
- Python 3.10+
- Git
- Xcode Command Line Tools (`swiftc`)
- ngrok only if you want a public HTTPS MCP endpoint

```bash
brew install python git ngrok
```

Optional helpers:

```bash
brew install cliclick brightness
```

## Install

### Recommended: one-line installer

The easiest way to install Mac MCP on a new Mac is the interactive installer:

```bash
curl -fsSL https://raw.githubusercontent.com/bulutarkan/mac-mcp/main/install.sh | bash
```

The installer is designed specifically to work safely through `curl | bash` while still reading interactive answers from the real terminal. It:

- verifies macOS 13+, Apple Silicon or Intel, Git, Python 3.10+, Xcode Command Line Tools, and `swiftc`;
- can offer Homebrew when a required dependency is missing, while keeping optional helpers such as `cliclick` and `brightness` optional;
- creates a Git source checkout at `~/Projects/mac-mcp` and a separate runtime at `~/mac-mcp` without Git metadata;
- creates and verifies the Python virtual environment and dependencies;
- generates a strong MCP API key, enables authenticated access, and stores the runtime `.env` with mode `600`;
- installs the `mac-mcp` CLI at `~/.local/bin/mac-mcp` and records the deployed commit for the built-in updater;
- builds and code-sign verifies the native `Mac MCP.app` menu bar controller in `~/Applications`;
- shows both Bearer-token and `?ApiKey=` connection formats at the end;
- does **not** install OpenCode, Codex, or ChatGPT Web CLI. If you want to use Subagents, install the provider you plan to use separately.

Existing source/runtime/CLI paths are never silently overwritten. If Mac MCP is already installed, use the built-in updater instead of re-running the installer over the same paths.

### Manual installation

If you prefer to manage the checkout and Python environment yourself:

```bash
git clone https://github.com/bulutarkan/mac-mcp.git
cd mac-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp mcp_server/.env.example mcp_server/.env
```

Configure at minimum:

```env
MCP_API_KEY=replace-with-a-long-random-token
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=120

# Optional public tunnel
NGROK_DOMAIN=your-domain.ngrok-free.dev
```

Generate a strong token:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

When authentication is enabled, the preferred client credential is still:

```http
Authorization: Bearer <MCP_API_KEY>
```

For MCP clients/connectors that cannot set an `Authorization` header, Mac MCP also accepts the configured global key in the endpoint URL:

```text
https://your-domain.example/mcp?ApiKey=<MCP_API_KEY>
```

`Authorization` remains authoritative when both forms are supplied. Empty, duplicate, or invalid `ApiKey` query credentials are rejected while authentication is enabled. The server removes `ApiKey` from its local access-log URL before logging, but upstream proxies/tunnels can still observe query strings, so Bearer headers should be preferred whenever the client supports them.

## Product and security terminology

Mac MCP uses a small canonical glossary so UI and documentation do not imply stronger isolation or invisibility than the product actually provides. See [`docs/TERMINOLOGY.md`](docs/TERMINOLOGY.md) for the full definitions.

- **Background browser automation:** visible, non-focus-stealing Safari/Chrome automation; not hidden or headless.
- **Capability:** whether the Mac MCP server policy permits a tool/risk class.
- **Approval:** a human-confirmation mechanism and its source; separate from capability enforcement.
- **Localhost / loopback:** machine-local transport, not same-user isolation or a sandbox.
- **Mac MCP logical session:** a local hashed steering identity that keeps one agent/conversation flow coherent; not the raw provider identity.
- **Dedicated user:** containment/hardening that reduces blast radius; not a complete sandbox.

For an advanced two-account deployment, see **[Hardened deployment with a dedicated non-admin macOS user](docs/HARDENED_DEDICATED_USER.md)**. It covers loopback authentication, an explicit ACL-shared directory, TCC/GUI-session limits, tool behavior, rollback, and a two-user validation matrix.

## Permission profiles and approval semantics

Mac MCP treats **capability enforcement** and **human approval** as separate security concepts. A capability being allowed means only that the Mac MCP server policy permits that tool/risk class. It does **not** mean a second confirmation prompt will appear before the action runs.

| Profile | Server-enforced capability behavior | Approval source | Automatic Mac MCP prompt |
| --- | --- | --- | --- |
| `trusted` | All registered capabilities; destructive families are not additionally restricted by the profile. | `none` | No |
| `standard` | Blocks `raw_execution` and `update_control`; destructive operations are limited to browser/accessibility families; access-mode ceiling is read-only. | `none` | No |
| `read_only` | Allows read/network/browser/native-accessibility capabilities only and denies destructive calls. | `none` | No |

`ask_confirmation` remains an explicit interaction tool and is not a blanket confirmation wrapper around normal tool calls. Separately, Mac MCP has narrow server-side security gates for risky trust-boundary crossings. Under `standard` and scoped delegated profiles, an untrusted web context → privileged host action can require a source-aware **Allow Once / Block** decision. The global `trusted` profile intentionally skips that routine web→host confirmation so trusted interactive workflows are not interrupted on every shell/file/UI hop. Detected credential/secret egress to an untrusted origin remains source-aware and approval-gated even under `trusted`. Exact-action grants remain origin-bound and single-use.

Delegated Codex workers currently run with Codex `approval_policy="never"`; their sandbox/access mode is separate from human approval. OpenCode permission behavior is also provider-side and must not be treated as a Mac MCP server confirmation guarantee.

Set the server capability profile with `MAC_MCP_PERMISSION_PROFILE=trusted|standard|read_only`. The native menu bar app reads `/dashboard/api/security/semantics` and shows **Allowed Capabilities** and **Approval Behavior** separately for the active profile. The three preset rows are clickable: choosing one persists the value in `mcp_server/.env` and applies it to new global requests immediately without restarting the server or ngrok. Existing delegated agents keep the scoped profile issued when they were started; new agents inherit the newly selected parent profile.

Browser resilience guards are configurable with `MAC_MCP_NO_PROGRESS_THRESHOLD` (default `4`, range `2–10`) and `MAC_MCP_TAB_LEASE_TTL_S` (default `300` seconds, range `30–3600`). The no-progress breaker stops only repeated meaningful browser actions that fail to change DOM revision, URL, or title; wait/scroll/extract flows do not consume that budget. Delegated-agent tab ownership is logical and time-bounded: completing/cancelling/crashing an agent releases ownership without closing the user's tab, and the next agent must make a fresh `browser_observe` before acting on a previously owned handle.

Untrusted web provenance is **sticky at the logical-session level**. Once a session consumes third-party browser content, writing that content to a local scratch file, reading it back, closing the tab, or passing through unrelated read-only tools does not erase that provenance. Scoped/non-trusted workflows continue through the web→host security gate until the work moves to an independent clean-room session. The global `trusted` profile keeps the sticky provenance for audit and secret-egress checks but does not show a routine Allow Once dialog for each normal privileged host hop. Delegated children spawned from a tainted session inherit the taint metadata (origin, reason, and credential fingerprints) without copying raw DOM or secrets into the security log; nested delegation preserves the inheritance chain. A genuinely independent MCP session with no transferred payload starts clean under the normal policy.

## Install the menu bar app

```bash
./menu_app/install_app.sh
```

Default location:

```text
~/Applications/Mac MCP.app
```

The app is independent from the server:

- quitting the app does **not** stop MCP;
- stopping MCP does **not** quit the app;
- `mac-mcp start` opens the app automatically when it is installed;
- server controls remain available even while the Voice section is collapsed.

The app uses the localhost dashboard APIs with a separate dashboard Bearer credential stored in `~/.mac-mcp/dashboard-token` (mode `0600`). The menu app reads that owner-only file locally and never needs the global `MCP_API_KEY`:

```text
/dashboard/api/summary
/dashboard/api/security/semantics
/dashboard/api/events
/dashboard/api/agents
/dashboard/api/steering
```

### Live menu-bar steering

Mac MCP keeps a **Mac MCP logical session** visible between tool calls instead of showing it only for the few milliseconds while a tool is running. It prefers stable conversation metadata supplied by the MCP client (for example OpenAI's conversation-scoped `openai/session` metadata), then generic `_meta.client_id`, and finally a reused stateful Streamable HTTP transport as a fallback. Raw identity values are hashed before entering steering state and are never exposed in the dashboard. This matters for hosts that create a fresh transport session for every tool call: repeated calls from the same conversation still collapse into one **Working / Idle** agent card.

Steering messages are kept in memory only and are bound to the selected logical agent, never to a global "next caller" queue. If the selected agent currently has a tool running, the prompt is appended to that tool's live response as structured `_mac_mcp_steering` content. If the agent is idle, the prompt remains queued for that logical session and its next requested tool is **preempted before execution** with a `mac_mcp_steering_preempted` tool error, so the agent sees the user's new direction before doing more work. A different conversation/session cannot consume that prompt. Logical steering sessions expire after 10 minutes of inactivity by default. The menu-bar **Sessions** disclosure lets you enter any positive number of minutes and persists that value in `~/.mac-mcp/settings.json`; stateful protocol transports are separately bounded so short-lived client transports do not accumulate indefinitely. Raw steering text is not written into telemetry SQLite, and nested fallback calls such as `tool_invoke` do not create duplicate visible sessions.

Steering POST acceptance is idempotent for clients that send `client_instruction_id`. The menu app generates a UUID for each Send action and reuses that same UUID for a bounded retry when the HTTP result is ambiguous. Replaying the same session + client ID + text returns the original canonical `st_*` message instead of enqueuing a duplicate; reusing a client ID with different text or another live session returns `409 idempotency_conflict`. Recent lifecycle rows include the client correlation ID so the menu app can recover an accepted message after a lost response. The active dedupe index is bounded; evicted keys move into a bounded tombstone window so a late same-generation retry returns `409 idempotency_expired` instead of silently creating a second instruction.

Each daemon lifetime also publishes a random `generation_id`. The native menu app binds every new steering submission and ambiguous retry to that generation. If the daemon restarts before an uncertain response can be recovered, an old-generation retry is rejected as `409 stale_generation` with `outcome=unknown`; it is never automatically replayed into the new daemon. The user can then intentionally resend, which creates a fresh client ID against the current generation. The generation marker prevents duplicate replay across restart boundaries; it does not pretend to recover the old daemon's lost in-memory result. Legacy clients that omit `generation_id` remain compatible, but do not receive this stronger restart-boundary guarantee.

If the native menu app itself is relaunched while the daemon keeps running, the daemon-owned logical sessions remain available and the new app instance rediscovers them from `/dashboard/api/steering`. A steering submission whose HTTP outcome was still ambiguous at app exit keeps only a short-lived owner-only correlation record in `~/.mac-mcp/pending-steering.json` (client ID, logical session ID, prompt SHA-256, daemon generation ID, timestamp; never the raw prompt). On relaunch the app reconciles that record against daemon `recent` state; re-entering the same prompt reuses the original idempotency key only while the daemon generation still matches.

For example, if an agent is researching with visible, non-focus-stealing browser automation in a Safari tab and you type `stop using Airbnb and check Booking.com instead` into that agent's Session, Mac MCP routes the instruction only to that logical agent. A running tool can return the steering immediately; an idle agent is interrupted before its next tool call so it can change course first.

### SwiftUI state update efficiency

The native controller remains `@MainActor` and keeps the same polling cadence, but it publishes decoded dashboard values only when they actually change. Session snapshots are diffed by stable `session_id` before replacing the observable array, preserving SwiftUI row identity and avoiding hierarchy invalidation for identical polls. Volatile duration updates are coalesced only while their rendered label would remain unchanged. This is a rendering/state-efficiency optimization, not a slower-refresh mode.

### Sessions information architecture

The menu bar derives three deterministic sections from the versioned lifecycle snapshot: **Needs Attention** (`failed`, unresolved/unknown, or recent disconnected/expired session events), **Active** (working, queued, delivered, pending, or awaiting acknowledgement), and **Recent** (retained idle `ready`/`acknowledged` sessions). Historical terminal rows are informational rather than steerable. The menu-bar status icon carries only an aggregate attention/active signal. Mac MCP does not show session Retry/Cancel controls because no such backend actions exist; connection **Retry** remains a separate dashboard-reachability action.

### Versioned session lifecycle

The steering API exposes `schema_version: 1` and separates **activity** from **instruction lifecycle**. Legacy `state=working|idle` and `queued` fields remain for compatibility; new clients should prefer `activity_state`, `lifecycle_state`, `pending_instruction_count`, `last_transition_at`, and `last_error`.

The instruction lifecycle is intentionally small: `ready → queued → delivered → acknowledged`. If the underlying tool fails before queued steering can be delivered, the session enters `failed` while keeping the instruction pending for the next tool call. Transport-backed sessions emit `disconnected` when their MCP transport disappears, and idle sessions emit `expired` when their retention TTL elapses. Illegal lifecycle transitions are rejected internally instead of silently producing ambiguous state.

`acknowledged` is inferred when the same logical agent makes its next top-level tool request after receiving steering; it means the agent continued after the delivery boundary, not that the model sent a separate acknowledgement packet. Daemon/API reachability is a different connection concern and is handled separately by the menu app's connection UX.

### Menu bar connection resilience

The native controller does not treat a failed dashboard request as valid empty data. A successful empty `/dashboard/api/steering` response clears the session list normally; an HTTP error, timeout, or connection refusal preserves the last successful snapshot and marks it stale. If the app has never received a valid session snapshot, it shows **Session data unavailable** rather than **No agent sessions yet**.

A transport failure such as timeout or connection refusal enters `disconnected`; an HTTP error or invalid response from a reachable server enters `degraded`, including failures from the primary summary endpoint. HTTP status failures, timeouts, and connection-refused errors are surfaced separately. Automatic polling backs off from 1 second to a 30-second cap and returns to the normal 2.5-second cadence after the next complete successful refresh.

## Server commands

```bash
mac-mcp start
mac-mcp start --ngrok
mac-mcp status
mac-mcp restart --ngrok
mac-mcp stop
mac-mcp dashboard
mac-mcp doctor
mac-mcp conformance
```

`mac-mcp doctor` performs read-only checks for the local runtime, Python/version, disk space, state/settings validity, Accessibility, required/optional helpers, server health, dashboard credential file metadata, and Safari/Chrome companion state. Use `--json` for automation. `--support-bundle [PATH]` writes an owner-only (`0600`) structured support report; it intentionally excludes raw `.env`, settings values, logs, credentials, cookies, prompts, and chat content.

`mac-mcp conformance` runs the deterministic Computer Use regression lab. Its default suite is CI-safe and verifies contracts such as background browser behavior, explicit foreground fallbacks, stable tab identity, stale-handle rejection, render/element readiness, bounded action batches, and no-effect click handling. `--live` adds read-only checks against this Mac without clicking or typing in the user's applications.

Default local endpoint:

```text
http://127.0.0.1:8000/mcp
```

A custom port can be supplied through `MAC_MCP_PORT` or CLI flags.

## Voice interaction

`ask_user_voice` speaks a short prompt, records the local answer, transcribes it with Groq Whisper, and returns the transcript to the calling agent.

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

## Role learning for delegated agents

Mac MCP keeps **workflow lessons** separate from generic factual memory. Delegated agents can opt into a role with `role="coder"`, `role="reviewer"`, or `role="orchestrator"`. At spawn time, only a small top-k set of **approved, relevant** lessons for that role is injected; current task instructions always take priority. Unrelated tasks receive no lesson context.

A worker may emit a compact structured lesson candidate at the end of a run, but candidates are quarantined and **never auto-activate**. `lesson_search` lets the parent review candidates and `lesson_feedback` records `approve`, `success`, `failure`, `disable`, or `enable` outcomes. Repeated failures lower confidence and can disable a lesson; stale low-confidence lessons can be disabled during `lesson_consolidate`. Exact duplicates merge within the same trust domain, while contradictory preferred actions are reported for review instead of silently choosing a winner. Manual candidate creation and consolidation remain available through tool discovery so the compact core tool surface stays small.

Role lessons are stored as structured fields and bounded evidence references in `~/.mac-mcp/role-learning/role-lessons.sqlite3`; raw agent transcripts are not stored in the lesson database. Sticky provenance is enforced: a web-tainted session cannot write or approve trusted lessons, tainted children do not receive trusted lesson context, and untrusted candidates are kept in a separate quarantine namespace so they cannot poison an existing trusted lesson.

## ChatGPT subagent resilience

When ChatGPT Web CLI is used as a delegated-agent provider, Mac MCP keeps long web turns bounded without treating the budget as a hard task timeout. The default soft turn budget is 15 minutes; if a tool is still active Mac MCP waits for it, with a 20-minute hard tool ceiling, then requests a controlled ChatGPT `interrupt` that continues the same task in a fresh turn. The continuation explicitly avoids repeating completed work or external side effects. ChatGPT subagents default to **High** reasoning; `extra-high` remains opt-in.

If ChatGPT reports request throttling, Mac MCP records the reason/time, applies bounded exponential cooldown, recovers the existing ChatGPT session for retry when possible, and staggers other ChatGPT worker starts during the recovery window instead of launching a retry storm. Dashboard and menu-bar agent rows expose turn elapsed time plus checkpoint/throttle counts. These values can be tuned with `CHATGPT_PROVIDER_TURN_BUDGET_S`, `CHATGPT_PROVIDER_HARD_TOOL_BUDGET_S`, `CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_S`, and `CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_CAP_S`.

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

Mac MCP 2.1 advertises a compact **21-tool core surface by default**, backed by **84 registered MCP capabilities**. The 63 less-common tools remain available through `tool_discover` and `tool_invoke`, including every tool from the previous 81-tool surface.

Set `MAC_MCP_TOOL_PROFILE=full` to advertise all registered tools directly to the client. You can also add selected tools to the compact surface with `MAC_MCP_CORE_EXTRA_TOOLS=name1,name2`.

The capability set covers:

- terminal/system and background jobs;
- delegated OpenCode/Codex agents;
- file management;
- macOS automation and Accessibility UI control;
- Safari/Chrome browser automation with stable tab handles and background visual observation;
- HTTP and search;
- text/choice/confirmation/voice human input;
- persistent memory;
- Agent Skills;
- safe self-update.

Use MCP tool discovery for the authoritative live schema.

## Updating

```bash
mac-mcp update --check
mac-mcp update
```

The updater follows `origin/main`, blocks on dirty repositories, preserves runtime overlays and private files, creates a runtime backup, restarts the managed service, performs a health check, and rolls back managed runtime files if verification fails.

In 2.0, `menu_app/` is part of the managed runtime. If `Mac MCP.app` is already installed, a successful update rebuilds and refreshes it automatically.

## macOS permissions

Grant only the permissions required by the tools you use:

- **Accessibility** for `mac_observe`, `mac_act`, System Events, and desktop automation;
- **Screen Recording** for protected screen capture;
- **Automation** when macOS asks permission to control Safari, Chrome, System Events, Reminders, or other apps;
- **Microphone** for `ask_user_voice`.

## Development

Run tests:

```bash
python -m unittest discover -s tests -v
```

Build the native menu app without installing it:

```bash
./menu_app/build_app.sh /tmp/mac-mcp-build
```

Project layout:

```text
mcp_server/   Python MCP server and dashboard
menu_app/     Native SwiftUI menu bar controller
tests/        Regression tests
openapi/      REST/OpenAPI schema assets
```

## License

MIT
