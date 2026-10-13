# Configuration: where each setting comes from

Mac MCP reads configuration from three places. Each has one job:

| Kind | Where | Read | Examples |
|---|---|---|---|
| Deployment environment | `MAC_MCP_*` / `MCP_*` environment variables, `mcp_server/.env` | Mostly at startup | port, host, state root, isolated test installs |
| Owner preferences | `~/.mac-mcp/settings.json` (`MAC_MCP_SETTINGS_PATH`) | Live, on use, with short caches | public endpoint mode, providers, privacy rules, retention |
| Secrets | `mcp_server/.env` (`MCP_API_KEY`) and the macOS Keychain | When needed, never copied into settings or logs | API key, provider credentials, saved logins, Groq key |

Precedence for a setting that both sources can set is: **environment variable, then settings.json, then the built-in default.** An environment value is a deployment decision (an isolated install, a test, a managed Mac), so it wins over a preference the owner set in the app.

## Paths

`mcp_server/paths.py` is the only place that resolves where Mac MCP keeps things. Every resolver reads its environment variable on each call and never creates anything.

| Location | Resolver | Override | Default |
|---|---|---|---|
| State root | `paths.state_dir()` | `MAC_MCP_STATE_DIR` | `~/.mac-mcp` |
| Settings | `paths.settings_path()` | `MAC_MCP_SETTINGS_PATH` | `~/.mac-mcp/settings.json` |
| Tool activity | `paths.telemetry_dir()` | `MAC_MCP_TELEMETRY_DIR` | `~/.mac-mcp/dashboard` |
| Memory | `paths.memory_dir()` | `MAC_MCP_MEMORY_DIR` | `~/.mac-mcp/memory` |
| Lessons | `paths.lessons_dir()` | `MAC_MCP_LESSON_DIR` | `~/.mac-mcp/role-learning` |
| File change history | `paths.transactions_dir()` | `MAC_MCP_FILE_JOURNAL_DIR` | state root `/transactions` |
| Run timelines | `paths.runs_dir()` | (state root) | state root `/runs` |
| Saved login index | `paths.vault_dir()` | (state root) | state root `/vault` |
| Agent token usage | `paths.provider_usage_db()` | (state root) | state root `/state/provider_usage.sqlite3` |
| Jobs, agents, audit log | `paths.jobs_dir()`, `paths.agents_dir()`, `paths.audit_log()` | none | next to the server code (`~/mac-mcp/mcp_server/…`) |

Settings, telemetry, memory and lessons default to `~/.mac-mcp` even when `MAC_MCP_STATE_DIR` points elsewhere, and job/agent records sit next to the server code. That is how they shipped. Moving them would strand existing data, so the table records it instead. To isolate a test install completely, set each override.

## Adding a setting

1. A path: add a resolver to `paths.py` and use it; do not rebuild `Path(os.getenv(...))` in a module (a test enforces this for the state root).
2. An owner preference: read it through a named function in `runtime_settings.py` (for example `privacy_setting`, `usage_privacy()`), so its default and validation live in one place. Keep live-read behavior: the server must not need a restart.
3. A secret: keep it in `.env` or the Keychain and pass references, never values, through settings or logs.
