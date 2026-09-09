<p align="center">
  <img src="assets/screenshots/mac-mcp.png" alt="Mac MCP" width="760">
</p>

# Mac MCP 2.0

Mac MCP is a local macOS control server for AI agents. It exposes your Mac through a native MCP endpoint and a REST/OpenAPI surface, with shell, files, browser automation, macOS UI control, delegated OpenCode/Codex agents, memory, Agent Skills, voice interaction, self-update tooling, and a local operations dashboard.

> **Security:** Mac MCP can execute commands, read/write files, and control desktop apps. Keep authentication enabled whenever the service is reachable outside localhost and expose it only to clients you trust. The operations dashboard is loopback-only.

## What's new in 2.0

- Native **Mac MCP.app** menu bar controller written in SwiftUI. It runs without a Dock icon and remains independent from the Python server.
- Start, Stop, Restart, Update, Dashboard, server status, ngrok status, success rate, recent tool usage, and delegated-agent status are available from the menu bar.
- **Latest Tool Usage** shows up to five rows at once and scrolls internally for older calls.
- **Delegated Agents** keeps a compact fixed-height list and scrolls internally when multiple active/recent agents exist. Active work also triggers a lightweight animated robot and a pulsing menu bar status icon.
- **Voice** is a collapsed disclosure section by default. `ask_user_voice` can be enabled/disabled live without removing the MCP tool from discovery.
- When voice is disabled, calls return `experimental_tool_disabled` and instruct the agent to fall back to `ask_user`.
- Groq API keys can be stored in **macOS Keychain** instead of plaintext configuration.
- Voice input/output pickers enumerate connected CoreAudio devices such as AirPods, built-in microphone, and speakers.
- Runtime settings are read live from `~/.mac-mcp/settings.json`; voice changes do not require an MCP restart.
- The updater now carries the native `menu_app/` runtime alongside `mcp_server/` and refreshes an already-installed menu app after updates.

## Browser automation that doesn't hijack your Mac

Mac MCP can inspect and interact with Safari and Chrome tabs in the background while you keep working in another app or browser tab.

- New browser tabs open in the background by default and return a stable `tab_handle`.
- Stable tab handles survive tab-index changes, so long-running tasks keep targeting the intended Safari or Chrome tab even as other tabs open, close, or move.
- `browser_observe` can return compact DOM context plus viewport, element, or full-page visuals without activating the browser, switching tabs, scrolling the user's page, or leaving screenshot files on disk.
- High-level browser actions can target a specific background tab directly by handle, which makes parallel research and delegated-agent workflows practical without constant focus stealing.
- Foreground-only fallbacks such as native key presses and absolute coordinate clicks fail closed unless foreground access is explicitly requested.

This is designed for workflows where an AI agent keeps working in one or more background browser tabs while the Mac remains usable normally.

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

The app uses the existing localhost dashboard APIs:

```text
/dashboard/api/summary
/dashboard/api/events
/dashboard/api/agents
```

## Server commands

```bash
mac-mcp start
mac-mcp start --ngrok
mac-mcp status
mac-mcp restart --ngrok
mac-mcp stop
mac-mcp dashboard
```

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

## Operations dashboard

Open:

```text
http://127.0.0.1:<port>/dashboard
```

The dashboard records sanitized MCP/REST tool activity, status, latency, recent delegated-agent state, active calls, and tool frequency. Telemetry persists locally under:

```text
~/.mac-mcp/dashboard/telemetry.sqlite3
```

The dashboard is restricted to loopback access even when `/mcp` is exposed through ngrok.

## Tool coverage

Mac MCP 2.0 exposes **81 MCP tools** across:

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
