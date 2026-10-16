# CLI Theme, Phase 2: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a terminal, `clients`, `connect`, `connect-config`, `update`, `logs` and `privacy delete` use the Phase 1 theme. Without a terminal, they print exactly what they print today.

**Architecture:** Same as Phase 1:
- Each command keeps today's plain body.
- Each command gets one branch, `style = Style.for_stream(json_output=...)`.
- When `style` is not `None`, a renderer in `mcp_server/cli_views.py` (or `Steps` from `cli_steps.py`) draws the screen from the data the command already computes.

**Tech Stack:** Python standard library, `unittest`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-15-cli-theme-design.md` (Phase 2 section). Phase 1 plan: `docs/superpowers/plans/2026-10-16-cli-theme-phase-1.md`.

## Global Constraints

- All of Phase 1's rules hold:
  - the theme turns on only for a TTY and turns off for `NO_COLOR`, `TERM=dumb`, `MAC_MCP_PLAIN` and `--json`;
  - plain output stays byte-for-byte;
  - exit codes are unchanged;
  - text is English and human;
  - every line fits the terminal width;
  - a suggested command never breaks across lines.
- The menu app reads these, without a terminal:
  - `clients --json` and `connect --client X --json` (JSON, untouched);
  - `connect-config --client X` (plain text, OnboardingWindowController);
  - `update` and `update --check` (`applyUpdateCheckOutput`).
- Copyable data stays copyable. The `connect-config` snippet and `logs` lines are printed raw: no colour, no indent, no wrapping.
- Secrets are never printed. `connect --client chatgpt` keeps copying the address straight to the clipboard.
- Tests run with: `MCP_API_KEY=test-key-0123456789abcdef0123456789 MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true MAC_MCP_SKIP_MENU_APP_INSTALL=1 ~/mac-mcp/.venv/bin/python -m unittest <module>`.

## Review Focus

1. **A `connect-config` snippet copied from the themed screen:** it must paste into the app's config unchanged, with no ANSI codes, no indent and no wrapped lines. Test in Task 3.
2. **A very long log line:** shown whole (the terminal wraps it), never cut, never coloured. Test in Task 5.
3. **`update` failing after the updater started:** the end of the update log is shown under the ✗, and the exit code stays 1. Test in Task 4.
4. **`clients` when no app is on this Mac:** says so and names no command to run. Test in Task 2.
5. **`privacy delete --yes` when Mac MCP was not running:** it neither stops nor starts the server, and the summary says the stores moved. Test in Task 6.

---

### Task 1: Pin today's plain output of the Phase 2 commands
- Add to `tests/test_cli_plain_contract.py`, with mocked inputs and a non-TTY `StringIO`, the exact current text of:
  - `clients` (with `client_setup.apps_status` patched);
  - `connect` success and failure (failure goes to stderr);
  - `connect-config` (with `render_connection_config` and the endpoint patched);
  - `update --check` (`format_check` output: up to date, update available, dirty, unverified ahead);
  - `update` for the pkg install kind;
  - `logs` and `logs --list` (log paths patched to temp files);
  - `privacy delete` preview and `--yes` (with `trash_stores`/`delete_plan` patched).
- Run them on today's code; they pass and pin the contract.
- Commit: `Tests: pin phase 2 commands' plain output`.

### Task 2: Themed `clients` and `connect`
- `cli_views.render_clients(style, apps)` uses the status pattern:
  - `●` ok "added";
  - `●` warn "added the old way", with detail `key in its settings — mac-mcp connect --client <id>`;
  - a dim dot "on this Mac · not added";
  - the apps not on this Mac fold into one dim line "Not on this Mac: A · B".
  - Summary when apps are on this Mac: `✓ Mac MCP is added to N of M apps on this Mac`. If an app is installed but not added, `next_command` is `mac-mcp connect --client <first such id>`.
  - Summary when no app is on this Mac: `ℹ No AI app Mac MCP knows is on this Mac.`, with no command.
- `connect` in a terminal:
  - header `connect`;
  - one step, "Added Mac MCP to {name}" / "Add Mac MCP to {name}" (ChatGPT: "Copied ChatGPT's address" / "Copy ChatGPT's address");
  - the `SetupError` message printed inside the step, so it shows under the ✗;
  - summary ok: `result['next']`;
  - summary on error: "Mac MCP was not added", next `mac-mcp clients`;
  - exit codes unchanged.
- Tests (`tests/test_cli_views.py`):
  - `ClientsScreen`: added, old way, installed, not on this Mac, none at all.
  - `ConnectScreen`: success, failure, ChatGPT never prints the address.

### Task 3: Themed `connect-config`
- `cli_views.render_connect_config(style, rendered, endpoint_kind, endpoint_url, public_check, auth_required)`:
  - rows Client, Target, Endpoint (`<kind> · <url>`), Public check (ok / warn "not reachable right now"), Authentication;
  - then a dim line "Copy into <target>:", then `rendered.snippet` exactly as is, unindented and uncoloured;
  - then notes for "Secret setup", "Verify" and the ChatGPT query-key warning;
  - summary ok "Snippet ready — no secret is printed.", or warn when the public check failed.
- Errors (unguided client, config error) in a terminal: header plus `✗ <message>`, exit 1.
- Tests:
  - the snippet appears byte-for-byte in the output, and the lines between "Copy into" and the next blank line carry no `\x1b`;
  - error case;
  - the plain path still writes the old text.

### Task 4: Themed `update`
- `update --check` in a terminal, via `cli_views.render_update_check(style, info)`:
  - rows Installed (short commit), Verified stable (short commit · release id/version), Branch;
  - summary:
    - update available → ok "A verified update is ready", next `mac-mcp update`;
    - up to date → ok "Mac MCP is up to date.";
    - unverified commits ahead → ok "No newer verified stable release", with detail "N development commits ahead" as a row;
    - dirty → error "Update blocked: the repository has local changes".
  - Exit codes unchanged (dirty is 2).
- pkg install kind in a terminal: summary info `<message>` and the download URL as a row.
- `update` (apply) in a terminal:
  - step "Checked for a verified release";
  - already up to date → summary ok;
  - step "Installed the update" / "Install the update" around `launch_detached_update` and `proc.wait()`, spinner meanwhile;
  - success → summary ok "Updated to <release_id or short commit>", with the log path as a dim note;
  - failure → ✗ with the last 15 lines of the update log as the reason, summary error next `mac-mcp logs update`, exit 1.
  - `UpdateInProgress` and `UpdateError` → `✗` summary with the message (and the log path when known), exit 1.
- Tests: each check state; pkg; apply success; apply failure with the log tail; in progress.

### Task 5: Themed `logs`
- `logs <component>` in a terminal:
  - header `logs`;
  - one dim info line `<component> · <home_path(path)> · last N lines · secrets redacted`, then a blank line;
  - then the `tail_log` text written raw: no colour, no indent, no wrapping;
  - summary ok `N lines`.
  - A missing log → summary error "No <component> log yet", exit 1.
- `logs --list` in a terminal: rows `name · size · path`, a dim note with the bounds, summary ok `M logs · X MB in all`.
- Tests:
  - raw lines are unchanged, including a 300-character line and one holding `\x1b`-free text;
  - missing log;
  - list.

### Task 6: Themed `privacy delete`
- Preview in a terminal: rows per store, `name` with `size or empty` and the paths dim; summary warn "Nothing was changed", next = the exact `--yes` command.
- Missing or unknown `--store` in a terminal: summary error `Choose --all or --store from: …`, exit 2.
- `--yes` in a terminal:
  - step "Stopped Mac MCP" (only when it was running; uses the existing plain stop with `keep_intent=True`);
  - step "Moved N stores to the Trash" / "Move the stores to the Trash", with each failure as the reason;
  - step "Started Mac MCP again" (only when it was running);
  - summary ok "Moved to the Trash: <names>", or error with the failures, exit as today.
- Tests: preview; `--yes` while running; `--yes` while not running (no stop or start); a failure.

### Task 7: Verify, docs, promote, live check, review
- Full suite.
- `git diff --check`.
- CHANGELOG entry and `docs/reference.md` sentence.
- Commit and push.
- Promote the changed runtime files: check that each runtime copy equals the previous commit first, then back it up.
- Live renders with the installed CLI in a real pty (80 and 40 columns):
  - `clients`;
  - `connect-config --client codex`;
  - `update --check`;
  - `logs -n 5`;
  - `logs --list`;
  - `privacy delete --all` (preview only).
- Never run `connect` against the owner's own app configs, nor `update` (apply) or `privacy delete --yes` on production. Those are covered by mocked pty demos.
- Confirm the plain output through a pipe.
- Confirm that the menu app's Help & Diagnostics and Connections still work.
- Run a fresh whole-branch review on the most capable model, then do one fix pass with TDD.
