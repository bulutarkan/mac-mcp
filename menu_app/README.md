# Mac MCP Menu Bar Controller

Native SwiftUI controller for Mac MCP. It is an `LSUIElement` app, so it lives only in the macOS menu bar and does not appear in the Dock.

Build/install:

```bash
./menu_app/install_app.sh
```

Default install path: `~/Applications/Mac MCP.app`.

The app is independent from the Python server. Quitting the app does not stop MCP; server Start/Stop/Restart actions call the configured `mac-mcp` CLI. It reads the existing localhost-only dashboard APIs for recent tool activity, delegated-agent state, and persistent logical steering sessions. The collapsed **Sessions** card sits below Latest Tool Usage and keeps supported client/conversation identities visible while they are idle—even when the host opens a fresh MCP transport for each tool call. It queues prompts to the selected logical agent only, shows compact working/idle status icons, scrolls after five sessions, and includes a compact positive-minute retention field (10 minutes by default). Non-secret settings live in `~/.mac-mcp/settings.json`; the Groq API key is stored in macOS Keychain.

`ask_user_voice` can be disabled at runtime without removing the MCP tool. Disabled calls return `experimental_tool_disabled` with `fallback_tool: ask_user`.

## Browser activity visibility

For Mac MCP, **background browser automation** means automation inside a normal, visible Safari or Chrome tab without stealing focus. It does not mean a headless or invisible browser.

The menu bar controller shows active browser work in Sessions and Latest Tool Usage. Its compact browser context is deliberately privacy-minimized to browser name, site hostname, and action; URL paths/query strings and page content are not displayed. A **Show Tab** control is available when a stable live tab reference exists, and only that explicit click is allowed to bring the browser to the foreground.

## Permissions and approval

The menu bar app intentionally presents **Allowed Capabilities** separately from **Approval Behavior**. The permission profile is enforced by the Mac MCP server; approval describes whether a human confirmation layer exists and where it comes from. Current built-in profiles report approval source `none`: allowed calls do not automatically trigger a second Mac MCP prompt. Client-side approval, if any, is controlled by the MCP client and is not a server guarantee.

The app reads this information from the localhost-only `/dashboard/api/security/semantics` endpoint. `ask_confirmation` remains an explicit interaction tool rather than a universal approval gate.

The preset overview rows are interactive without changing their visual layout. Selecting Trusted, Standard, or Read Only persists `MAC_MCP_PERMISSION_PROFILE` in the server `.env` and updates the running server immediately; no server/ngrok restart is required. Existing already-issued scoped agent credentials retain their original profile for the lifetime of that agent.

## Session lifecycle

Sessions use the server's versioned lifecycle snapshot when available while remaining compatible with older `working` / `idle` responses. Activity (`working` or `idle`) is separate from steering lifecycle (`ready`, `queued`, `delivered`, `acknowledged`, `failed`, `disconnected`, `expired`). The menu shows queued, delivered, acknowledged, and failed states directly; terminal transport disconnect/expiry events remain visible instead of collapsing immediately into a generic empty-session message.

## Connection resilience

Dashboard reachability is modeled independently from session lifecycle. The app reports `connecting`, `connected`, `degraded`, or `disconnected`; a successful empty response is distinct from a failed fetch. Session fetch failures retain the last valid snapshot with a **stale** marker, while a first-load failure shows **Session data unavailable** instead of pretending there are zero sessions.

Automatic polling uses bounded exponential backoff after failed refreshes: 1, 2, 4, 8, 16, then 30 seconds maximum. A complete successful refresh resets polling to the normal 2.5-second cadence. HTTP 5xx responses, request timeouts, and connection-refused failures are surfaced with distinct status text, and **Retry** triggers an immediate refresh without restarting the daemon or ngrok.
