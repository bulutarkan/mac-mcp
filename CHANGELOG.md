# Changelog

## [1.1.1] - 2026-09-03

- Added native `ask_choice` and `ask_confirmation` human-in-the-loop tools with bounded, fail-closed dialog handling.
- Prevented concurrent native prompts from stacking by returning `prompt_busy` immediately when a dialog is already active.
- Hardened timeout cleanup for AppleScript, browser, shell, and interactive subprocesses so descendants do not remain stuck in the background.
- Exposed all 59 MCP tools as one-to-one Custom GPT Action operations with matching `operationId` names.
- Added REST aliases for file, macOS, browser, search, `mac_observe`, and `mac_act` tools.
- Kept the legacy grouped REST routes available for backwards compatibility while hiding them from the published OpenAPI schema.
- Updated the bundled OpenAPI schema to version 1.1.1 and verified 59 valid operations.
- Documented the OpenAPI/Custom GPT refresh flow and added the new release notes.
