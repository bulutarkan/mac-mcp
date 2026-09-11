# Mac MCP Menu Bar Controller

Native SwiftUI controller for Mac MCP. It is an `LSUIElement` app, so it lives only in the macOS menu bar and does not appear in the Dock.

Build/install:

```bash
./menu_app/install_app.sh
```

Default install path: `~/Applications/Mac MCP.app`.

The app is independent from the Python server. Quitting the app does not stop MCP; server Start/Stop/Restart actions call the configured `mac-mcp` CLI. It reads the existing localhost-only dashboard APIs for recent tool activity, delegated-agent state, and persistent MCP steering sessions. The **Steer the agent** card keeps stateful MCP sessions visible while they are idle, queues prompts to the selected session only, and shows whether a prompt will ride the current tool result or preempt that session's next tool before execution. Non-secret settings live in `~/.mac-mcp/settings.json`; the Groq API key is stored in macOS Keychain.

`ask_user_voice` can be disabled at runtime without removing the MCP tool. Disabled calls return `experimental_tool_disabled` with `fallback_tool: ask_user`.
