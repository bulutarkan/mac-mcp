# ChatGPT Control Center (MCP Apps / OpenAI Extensions)

The experimental Mac MCP Control Center ships in Mac MCP 2.1.9. It gives a connected ChatGPT plugin a global sidebar entrypoint, a thread panel entrypoint, and a standalone MCP Apps UI. These features are host-dependent and do not automatically appear in every MCP client. **Other clients retain the existing tool list without any ChatGPT UI tools.**

## Implementation

- `mcp_server/chatgpt_client_gate.py`: opt-in/out and fail-closed client-aware discovery. Reads initialized MCP `clientInfo`, not User-Agent strings or tool arguments; also recognizes ChatGPT's `openai-mcp` client, including runtime-suffixed labels such as `openai-mcp (codex)`, only when it advertises the MCP Apps HTML MIME type. Unrecognized clients cannot list/read/call the ChatGPT-only UI.
- `mcp_server/chatgpt_panel.py`: UI resource `ui://mac-mcp/panel-v3.html` and three centrally policy-classified tools: `open_mac_mcp_panel`, `mac_mcp_panel_state`, and `mac_mcp_panel_setting`.
- `chatgpt_ui/`: lightweight UI source using `@modelcontextprotocol/ext-apps` (MCP Apps postMessage bridge) and esbuild; `npm ci && npm run build` generates the single distributable HTML file.
- `mcp_server/chatgpt_ui/control-center.html`: distributable bundled UI resource registered through `pyproject.toml` package data.
- `tests/test_chatgpt_panel.py`: entrypoint metadata, resource MIME, bounded data projection, allowlist and read-only policy tests.

The sidebar is a compact monochrome UI that follows the mac-mcp-site dark design language and `>_` logo. Data loads from the host's initial tool result and is refreshed **only manually** via the icon-only refresh button (no polling; a single fallback read runs only if the host opens the panel without delivering a result). It currently displays 24-hour MCP telemetry, a bounded recent delegated-agent list, 7-day provider token accounting and a compact Settings tab. The only writable setting is the **default agent** (`subagents.default`: provider, optional model and reasoning). The server validates it against enabled providers and their live-discovered catalogs (`mac_mcp_panel_state(models_for=<provider>)`, fetched only when the editor opens). Notifications and the activity bubble are shown read-only because the native app owns them (macOS permission, in-memory state). Connection shows version, permission profile, endpoint mode and public host only (never the URL query or key). Note: the native app writes its in-memory settings on save; its Settings window reloads the file when opened, but saves from the menu popup can still overwrite a panel change made meanwhile. Tool calls from the iframe go through the MCP Apps host and the same Mac MCP server policy gates as calls from ChatGPT. No bearer token, session cookie, `.env`, raw agent prompt or Keychain value is sent to the UI.

The new setting tool is **not available in read-only** permission profiles and is not available if a delegated scope excludes the tool family. The entire extension can be disabled live via `{ "chatgpt_extensions": { "enabled": false } }` in the existing `~/.mac-mcp/settings.json`; unset defaults to enabled **only when the MCP client identifies as ChatGPT**. Invalid settings fail closed. ClientInfo is self-declared protocol metadata, so this is a UX/client compatibility gate, **not proof of ChatGPT identity**; preserve normal authenticated MCP credentials and approval policy. Changing access profiles, providing secrets and controlling processes remain native-only in this MVP.

## Development

```bash
npm --prefix chatgpt_ui ci
npm --prefix chatgpt_ui run build
node chatgpt_ui/test_bridge.mjs
python -m unittest tests.test_chatgpt_panel -v
```

Check actual rendering with an **isolated** HTTP preview, not by restarting production on 8765. A standalone browser preview renders the layout without live values because the UI's official MCP Apps iframe bridge requires a compatible host. Test actual tool invocation and UI interactions through a connected ChatGPT development plugin, not an unauthenticated browser route.

## ChatGPT host behavior

- ChatGPT re-reads the UI resource when a panel opens, so keep `ui://mac-mcp/panel-v3.html` stable. If the URI ever changes, keep the previous URIs readable through `CHATGPT_PANEL_LEGACY_URIS` (never listed): hosts cache a tool's `resourceUri`, and an unknown URI shows "App unavailable".
- ChatGPT connects as `openai-mcp` and, in its newer runtime, as `openai-mcp (codex)`. The newer runtime first probes MCP `2026-07-28` with `server/discover`, receives `400` from the current SDK and falls back to `initialize`; this is expected.
- A chat that once failed to load the panel keeps showing "App unavailable … after retrying". Verify UI changes in a **new** chat.
- The desktop global entrypoint is a persistent tab; it keeps running an already-loaded UI until ChatGPT is restarted.
- The panel reads `safeAreaInsets` from the host context and keeps room for the desktop composer in fullscreen.
- `mac_mcp_ui_read` server log lines record the client label, decision and URI for each UI read.

## Validation checklist

1. Verify that `tools/list` advertises `open_mac_mcp_panel` with both `global` and `thread` entrypoints, its data-URI SVG icon, and the `ui://` resource only to ChatGPT clients.
2. Open the panel in a new chat and from the global entrypoint; confirm the initial render uses the host tool result without additional tool calls.
3. Confirm default-agent writes are rejected for read-only sessions and accepted for authorized sessions, and appear in native Settings.
4. Confirm Claude/Codex/OpenCode see neither the tools nor the resource.

Official references: https://developers.openai.com/plugins/build/extensions and https://developers.openai.com/plugins/build/chatgpt-ui and https://github.com/modelcontextprotocol/ext-apps.
