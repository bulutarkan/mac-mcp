# The menu bar app

Installing the native menu bar app, live steering from the menu, and how sessions and the connection are shown.

[← Back to the README](../README.md)

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
