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
