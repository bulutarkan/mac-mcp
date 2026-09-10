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
- Added guarded source-repository rollback to the exact pre-update local HEAD after post-merge failures; rollback uses `git reset --keep` only when branch, HEAD, and worktree state still match the updater's transaction, preserving concurrent user edits, commits, and untracked files.
- Expanded updater regression coverage for split and single-checkout rollback, deployed-marker/source divergence, new/deleted runtime files, concurrent user changes, and isolated staged-helper bootstrapping.
- Added guarded cleanup for detached updater staging directories on both handled success and failure paths, without allowing source/runtime directories to be removed.

## [2.0.3] - 2026-09-09

- Added central risk classification for all 81 MCP tools plus `trusted`, `standard`, and `read_only` permission profiles with fail-closed policy enforcement across MCP and REST dispatch.
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
