## Unreleased

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
