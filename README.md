<p align="center">
  <img src="assets/screenshots/mac-mcp.png" alt="Mac MCP" width="760">
</p>

# Mac MCP 2.1.5

Mac MCP is a local macOS control server for AI agents. It exposes your Mac through a native MCP endpoint and a REST/OpenAPI surface, with shell, files, browser automation, macOS UI control, delegated OpenCode/Codex agents, memory, Agent Skills, voice interaction, self-update tooling, and a local operations dashboard.

> **Security:** Mac MCP can execute commands, read/write files, and control desktop apps. Keep MCP authentication enabled whenever the service is reachable outside localhost and expose it only to clients you trust. The operations dashboard is loopback-only **and** requires a separate per-user dashboard Bearer token; localhost is machine-local transport, not a same-user sandbox.

**Secure bootstrap defaults:** missing configuration fails closed. Without explicit settings, MCP authentication is required, shell execution and HTTP/browser host allowlists are disabled, and the global permission profile defaults to `standard` rather than `trusted`. A normal installer run generates the API key and writes the intended settings explicitly. Deliberate `MCP_ALLOW_NO_AUTH=true` is accepted only on loopback with no managed public endpoint; non-loopback or tunneled no-auth startup is refused.

## What's new in 2.1.5

- Added a **cryptographically verified stable release channel**: pinned Ed25519 trust root, detached signed manifest, complete tracked-file SHA-256/mode/size inventory, fail-closed installer/updater verification, and signed-release-only update selection.
- Added first-class **public endpoint modes** across CLI and the native app: Local only, managed ngrok, managed Cloudflare Tunnel, or Custom HTTPS.
- Added secure **Cloudflare Tunnel** lifecycle management with owner-only token storage, `--token-file`, a per-user `launchd` `KeepAlive` job, automatic crash recovery, and Start/Stop control that requires no persistent Terminal session.
- Expanded `install.sh` with public-endpoint onboarding: choose a provider, optionally install `cloudflared`/ngrok through Homebrew, follow Cloudflare Published application guidance, and save the tunnel token through hidden stdin without placing it in settings, `.env`, or process arguments.
- Hardened outbound HTTP and browser navigation against **SSRF, DNS rebinding, and public-to-private redirects**, with explicit private-development allowlists rather than wildcard bypasses.
- Hardened scoped file operations against **symlink/TOCTOU escapes** and added owner-only filesystem transaction journaling with atomic mixed write/move/delete batches and conflict-aware undo.
- Added durable delegated-workflow checkpoints/resume, stronger steering/idempotency recovery, role-scoped lesson controls, ChatGPT turn budgeting, and bounded web-throttle recovery without replaying verified side effects.
- Added `mac-mcp doctor`, redacted support bundles, and a deterministic Computer Use conformance lab; the 2.1.5 release is regression-tested with the full Python suite plus installer and native-app build/signing checks.

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
- ngrok only if you choose the built-in ngrok public endpoint mode
- cloudflared only if you choose the built-in Cloudflare Tunnel mode

```bash
brew install python git
# Optional public providers:
brew install ngrok       # ngrok mode
brew install cloudflared # Cloudflare Tunnel mode
```

Optional helpers:

```bash
brew install cliclick brightness
```

## Install

### Installer

The interactive installer now installs only a cryptographically verified stable release. It clones `main`, finds the newest signed stable-release commit, verifies the pinned bootstrap verifier, Ed25519 manifest signature, complete tracked-file SHA-256/mode/size inventory, aggregate payload digest, and release lineage **before** creating persistent source/runtime paths.

For convenience, the streamed bootstrap is still available:

```bash
curl -fsSL https://raw.githubusercontent.com/bulutarkan/mac-mcp/main/install.sh | bash
```

A streamed script cannot cryptographically authenticate itself before it starts executing. Treat that command as a lower-assurance bootstrap. For higher-assurance installation, obtain `install.sh` from a trusted signed release commit and independently compare the release-signer fingerprint documented in `release/README.md` before running it. Once the trusted installer is running, the cloned source/runtime payload is fail-closed and cryptographically verified.

The installer:

- verifies macOS 13+, Apple Silicon or Intel, Git, Python 3.10+, Xcode Command Line Tools, and `swiftc`;
- verifies the selected stable release cryptographically before moving any source/runtime files into persistent install paths;
- can offer Homebrew when a required dependency is missing, while keeping optional helpers such as `cliclick` and `brightness` optional;
- asks which public endpoint mode you want (`Local only`, `Cloudflare Tunnel`, `ngrok`, or `Custom HTTPS`) and, when Cloudflare/ngrok is selected, offers to install the matching provider with Homebrew if it is missing;
- can finish Cloudflare setup during installation by storing the public hostname in settings and accepting the tunnel token through a hidden terminal prompt; the token is sent to `mac-mcp credential cloudflare save` over stdin and is never placed in shell arguments, settings, or `.env`;
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

# Optional built-in ngrok provider
NGROK_DOMAIN=your-domain.ngrok-free.dev

# Optional environment overrides for public endpoint selection.
# Normally these are managed from Mac MCP Settings instead.
# MAC_MCP_PUBLIC_ENDPOINT_MODE=cloudflare
# MAC_MCP_PUBLIC_URL=https://mac.example.com/mcp
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

### Public endpoint modes

Mac MCP treats the local server and its public transport as separate layers. **Local only** exposes no managed public endpoint. **ngrok** runs a managed ngrok process and uses `NGROK_DOMAIN`. **Cloudflare Tunnel** runs `cloudflared` directly on the Mac and connects the selected named tunnel (or an owner-controlled token file) to `127.0.0.1:<port>`; no VPS, reverse proxy, inbound port-forward, or public Mac IP is required. **Custom HTTPS** records an externally managed HTTPS MCP endpoint and does not start a tunnel provider.

The native Settings window provides a four-way `Local only / ngrok / Cloudflare / Custom HTTPS` switch. The selection is persisted under `server.public_endpoint_mode` and `server.public_url` in `~/.mac-mcp/settings.json`; optional named-tunnel metadata may use the non-secret `server.cloudflare_tunnel` field. Existing installations that only have `ngrok_on_start=true` continue to behave as ngrok mode until the new setting is saved. Environment variables `MAC_MCP_PUBLIC_ENDPOINT_MODE` and `MAC_MCP_PUBLIC_URL` can override persisted values for managed/headless deployments.

### Cloudflare Tunnel: 5-minute setup

The interactive installer can do the Mac-side setup for you. Choose **Cloudflare Tunnel** when `install.sh` asks for a public endpoint. If `cloudflared` is missing, the installer offers `brew install cloudflared`; declining it does not break the core install and leaves Mac MCP in **Local only** mode.

For the Cloudflare-side setup:

1. Your domain must be active in Cloudflare. In the Cloudflare dashboard, go to **Networking → Tunnels**, choose **Create tunnel**, and give it any name that identifies this Mac.
2. Open the tunnel's **Routes** tab, choose **Add route → Published application**, select the hostname you want (for example `mac.example.com`), and point the service to `http://localhost:8000` for a default install. If you changed Mac MCP's server port, use that port instead.
3. Cloudflare shows a `cloudflared` setup/install command for the connector. **Do not run the service-install command when Mac MCP is managing the tunnel.** Copy only the tunnel token from that command. For an existing tunnel, **Add a replica** also exposes a connector command containing the token.
4. Back in the Mac MCP installer, enter the public hostname such as `https://mac.example.com` and paste the token into the hidden prompt. If you skip either value, the installer safely leaves the public mode as **Local only**; finish later in **Mac MCP.app → Settings → Advanced → Cloudflare**.
5. Start Mac MCP. `mac-mcp start` creates/enables a per-user `launchd` job with `KeepAlive`; no Terminal window has to remain open. Verify with `mac-mcp status` and `mac-mcp doctor`.

Cloudflare's current documentation calls this a remotely-managed tunnel and a **Published application** route. See [Set up Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/get-started/), [Add routes](https://developers.cloudflare.com/cloudflare-one/networks/routes/add-routes/), and [Tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/). A tunnel token is a credential: anyone who has it can run a connector for that tunnel, so rotate it in Cloudflare if it is ever exposed.

For the simplest Cloudflare setup, create the tunnel and hostname in Cloudflare, then paste the tunnel token once into **Settings → Advanced → Cloudflare**. Mac MCP writes it atomically to `~/.mac-mcp/cloudflare-tunnel-token` as an owner-only `0600` file, never writes the token into `settings.json` or `.env`, never re-displays it, and starts `cloudflared` with `--token-file` so the secret is not exposed in process arguments. `CLOUDFLARE_TUNNEL_TOKEN_FILE` may override the credential-file path without putting the token value in the environment. Named-tunnel credentials remain available as an advanced alternative. The tunnel connects Cloudflare directly to `http://127.0.0.1:<port>` on the Mac: no VPS, public IP, router port forwarding, or inbound firewall opening is required. Custom mode is for operators who already provide their own external HTTPS routing. All public URLs must be HTTPS and may not contain query strings, fragments, or URL userinfo. `mac-mcp status` shows the selected connector URL and `mac-mcp doctor` checks the selected provider, credential-file safety, and public `/health` route without sending the MCP API key. Switching providers stops any managed ngrok/cloudflared process that is no longer selected. In Cloudflare mode, `mac-mcp start` (and the native app Start action) installs/enables a user LaunchAgent with `KeepAlive`, so the tunnel stays independent of Terminal and is automatically restarted if `cloudflared` exits; `mac-mcp stop` boots out and disables that job.

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

### Outbound URL / SSRF boundary

`http_request` and `browser_open_url` treat public hostname allowlists and private-network access as separate permissions. A wildcard `HTTP_ALLOWLIST=*` or `BROWSER_ALLOWLIST=*` permits public hostnames; it does **not** permit loopback, RFC1918/ULA, link-local/metadata, carrier-grade NAT, multicast, unspecified, reserved, or other non-global address space. Hostnames that resolve to any blocked address fail closed. URL userinfo and non-HTTP(S) schemes are also rejected.

For `http_request`, every redirect hop is revalidated before the next request. The HTTP transport additionally resolves again at TCP-connect time, rejects a DNS answer that has changed into blocked address space, and connects to the already-validated IP while retaining the original hostname for HTTP/TLS identity. Environment proxy variables are deliberately not inherited by this host-access path. Redirect output is bounded to destination origins rather than copying full redirect paths or query strings.

Browser engines own their own network stack, so Mac MCP does not claim transport-level IP pinning for Safari or Chrome. Instead, browser navigation validates DNS before navigation and then revalidates the browser-observed destination URL after navigation; a same-host DNS change is resolved again at that boundary. If a newly created tab is observed on a blocked destination it is closed best-effort and the call fails as `browser_redirect_blocked`; for an existing tab Mac MCP best-effort restores the previously observed safe URL.

Local/private development access is opt-in by hostname through `HTTP_PRIVATE_ALLOWLIST` and `BROWSER_PRIVATE_ALLOWLIST` (comma-separated names/suffixes, for example `localhost`). These exceptions are independent from the normal public allowlists; `*` is intentionally ignored in the private allowlists so private-network access cannot be enabled accidentally.

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
mac-mcp start --public-mode ngrok
# Save the Cloudflare token once in Mac MCP Settings → Advanced.
mac-mcp start --public-mode cloudflare --public-url https://mac.example.com/mcp
mac-mcp start --public-mode custom --public-url https://mac.example.com/mcp
mac-mcp start --public-mode none
mac-mcp status
mac-mcp restart
mac-mcp stop
mac-mcp dashboard
mac-mcp doctor
mac-mcp conformance
```

The legacy `--ngrok` flag remains supported as an alias for `--public-mode ngrok`. When no CLI override is supplied, `start`/`restart` use the public endpoint mode saved by the native Settings window (or the `MAC_MCP_PUBLIC_*` environment overrides).

`mac-mcp doctor` performs read-only checks for the local runtime, Python/version, disk space, state/settings validity, Accessibility, required/optional helpers, local server health, selected public endpoint health/configuration, dashboard credential file metadata, and Safari/Chrome companion state. Use `--json` for automation. `--support-bundle [PATH]` writes an owner-only (`0600`) structured support report; it intentionally excludes raw `.env`, settings values, logs, credentials, cookies, prompts, and chat content.

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

## Transactional filesystem changes and undo

Mac MCP journals its primary destructive file operations before changing the filesystem. `write_file`, `edit_file`, `move_file`, and `delete_path` now return a random `transaction_id` plus `undoable`, `undo_expires_at`, and any explicit `irreversible_reason`. `write_files_batch(..., atomic=true)` prepares all preimages before the first write and restores the whole batch if any later write fails. `file_transaction_batch` extends the same all-or-nothing boundary to a mixed sequence of up to 50 `write`, `move`, and `delete` actions. Recent reversible transactions can be restored with `file_transaction_undo`; by default undo refuses to overwrite files changed after the original commit, while `force=true` is an explicit conflict override.

The journal lives under `~/.mac-mcp/transactions/` by default. The directory and transaction folders are owner-only (`0700`); manifests and snapshot archives are `0600`. Manifests contain only the paths/state required for restore, hashes/fingerprints, sizes, timestamps, and transaction metadata — never the new file content or edit text. Reversible preimages necessarily contain the original bytes, so they are kept only in owner-only snapshot archives with bounded retention. Defaults are a 7-day undo/expiry window, 64 transactions, 1 GiB total journal storage, and 256 MiB of preimage data per transaction; expired entries are pruned at daemon startup and on journal activity, while count/byte caps are enforced on every transaction. The corresponding `MAC_MCP_FILE_JOURNAL_*` environment variables can tighten those bounds.

If a single write/move/delete preimage exceeds the snapshot limit, the operation preserves backwards compatibility but returns `undoable=false` with `irreversible_reason=snapshot_limit_exceeded`. Atomic batch operations are stricter: if a complete rollback snapshot cannot be prepared, the batch is refused **before the first mutation**. A process crash after prepare but before commit leaves a `prepared` journal entry; normal undo treats that outcome as unknown, while an explicit `force=true` can restore the recorded pre-state. Delegated-agent undo also re-checks every transaction path against the agent's current resource scope, so a transaction ID cannot bypass workspace confinement. `copy_file` and `create_directory` are not part of this initial transaction-journal boundary.

### Symlink-safe delegated file scopes

Delegated calls with explicit `path_roots` use a second, operation-time filesystem boundary in addition to the normal server policy check. Scoped reads, writes, edits, moves, copies, deletes, directory traversal, filename/content search, transaction snapshots, and undo/rollback walk path components with directory file descriptors plus `O_NOFOLLOW` rather than trusting a path string that was checked earlier. A symlink swap between policy validation and the actual filesystem operation therefore fails closed instead of following the replacement outside the workspace. Recursive find/search/tree operations report or skip symlink entries without traversing through them, and direct access to a symlink that resolves outside the allowed roots is denied.

This stricter traversal is applied only when a delegated resource scope has explicit `path_roots`; ordinary local/unscoped file behavior remains compatible. Scoped cross-device moves are rejected rather than falling back to a copy/delete sequence that would weaken the descriptor boundary. Race failures return structured `scoped_path_unsafe` errors and do not expose outside-workspace file contents.

## Security assurance

Mac MCP publishes a regression-backed [Security Assurance Matrix](docs/security-assurance.md) that maps stable risk classes to their controls, exact automated tests, and release history. `scripts/verify_security_assurance.py` validates those links in CI so renamed tests, missing controls, orphan assurance tags, missing CHANGELOG linkage, and secret-like/private-path material in the public matrix fail the gate instead of silently going stale.

## Durable delegated-workflow resume

Delegated agents now get a provider-independent durable workflow checkpoint under `~/.mac-mcp/workflows/`. The checkpoint stores the original task **input hash**, provider/session lineage, resume generation, a sanitized provider milestone cursor, and a bounded chain of verified Mac MCP side-effect receipts. Receipt rows contain tool/family metadata plus hashes of arguments/results; raw prompts, commands, file contents, typed values, credentials, and tool payloads are not copied into the checkpoint store. Workflow and agent-map files are owner-only (`0600`) inside an owner-only directory (`0700`) and carry an integrity hash so corrupt or mismatched state fails closed.

A normal `agent_action(action="retry")` is only allowed while replaying the original prompt is still safe. Once a verified side effect has happened, a resume generation already exists, or the outcome becomes uncertain, fresh replay returns `409 retry_replay_unsafe`. For an interrupted agent with a verified checkpoint, `agent_action(action="resume")` continues the **same provider session** with an incremented generation and a compact receipt summary that explicitly instructs the provider not to repeat completed side effects. If the provider session cannot be recovered, the input hash/session does not match, the checkpoint is corrupt, or direct provider-native mutation made the commit state unverifiable, Mac MCP returns an outcome-unknown conflict rather than guessing.

Mac MCP can issue strong receipts only for side effects routed through its own tool boundary. Before a mutating Mac MCP tool executes, it durably writes a hashed **pending side-effect intent**; only a successfully returned result can convert that intent into a verified receipt. If the process dies in between, the pending intent survives and the workflow becomes outcome-unknown instead of replayable. Direct Codex/OpenCode shell or file mutations are therefore treated conservatively across a crash boundary; opaque ChatGPT Web CLI tool activity is also marked uncertain. This is intentional: durable resume prefers refusing an ambiguous replay over claiming a destructive action is safe to repeat. Agent/dashboard metadata exposes `workflow_id`, `resume_generation`, checkpoint state/safety, pending/verified side-effect counts, the sanitized cursor, last durable checkpoint time, and whether an interrupted agent is safely resumable.

## Agent team budgets and adaptive retry

`spawn_agents` applies a shared team admission budget on top of each child agent's own timeout. `max_parallel` remains the concurrency ceiling; `team_timeout_s` bounds how long the scheduler may admit new DAG nodes, while optional `max_total_tool_calls` and `max_total_tokens` stop new children once observed team usage reaches the configured limit. If no team timeout is supplied, Mac MCP derives a generous bounded deadline from the child timeout, DAG waves, and revision allowance. Parent responses from `spawn_agents`, `wait_agents`, and team-filtered agent state expose the budget used/remaining values plus an explicit exhaustion reason.

`max_team_retries` is a separate shared retry budget. Automatic retries are classified before replay: rate limits, timeouts/stalls, provider overload and transient transport failures may retry; authentication, permission, quota, invalid-model/request and unknown provider failures fail fast. Durable side-effect/checkpoint safety takes precedence over error classification, and every team retry slot is reserved atomically with bounded start spacing so concurrent failures cannot create a retry storm. `max_total_tokens` is accepted only for providers that expose reliable usage; ChatGPT Web currently rejects that option rather than pretending unreported usage is zero.

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
