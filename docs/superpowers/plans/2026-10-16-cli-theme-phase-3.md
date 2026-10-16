# CLI Theme, Phase 3: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In a terminal, `dashboard`, `credential`, `autostart`, `recipe`, `logins`, `catalog`, `conformance`, `repair` and `uninstall` use the Phase 1 theme. Without a terminal, they print exactly what they print today.

**Architecture:** Same as Phases 1 and 2:
- Each command keeps today's plain body.
- Each command gets one branch, `style = Style.for_stream(json_output=...)`.
- When `style` is not `None`, a renderer in `mcp_server/cli_views.py` (or `Steps` from `cli_steps.py`) draws the screen from the data the command already computes.

**Tech Stack:** Python standard library, `unittest`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-15-cli-theme-design.md` (Phase 3). Earlier plans: `docs/superpowers/plans/2026-10-16-cli-theme-phase-1.md`, `docs/superpowers/plans/2026-10-16-cli-theme-phase-2.md`.

## Global Constraints

- All of the Phase 1 and 2 rules hold:
  - the theme turns on only for a TTY and turns off for `NO_COLOR`, `TERM=dumb`, `MAC_MCP_PLAIN` and `--json`;
  - plain output stays byte-for-byte;
  - exit codes are unchanged;
  - text is English and human;
  - every line fits the terminal width;
  - a suggested command never breaks across lines (`Style.summary` glue);
  - themed errors use `render_error` / `summary("error", …)` on stdout (Phase 2 ruling).
- These commands are read without a terminal, and their output must not change:
  - the menu app runs `autostart on|off --json`, `credential cloudflare save` (token on stdin, reads only the exit code), every `logins … --json` and `privacy delete … --json`;
  - the installer runs `printf … | mac-mcp credential cloudflare save >/dev/null`.
- Secrets are never printed:
  - the dashboard token stays in the URL fragment handed to the browser, never on screen;
  - passwords and the Cloudflare token are never echoed.
- Copyable or reviewable data is printed raw: no colour, no indent, no wrapping. That means a held script's content in `catalog enable-script`, and paths in uninstall and repair rows.
- Nothing in the live checks changes the owner's install. These are never run on production:
  - `uninstall --yes`, `repair --yes`;
  - `catalog install`, `catalog enable-script --yes`;
  - `logins add` / `logins remove` / `logins subdomains`, `logins import`;
  - `credential … save` / `remove`;
  - `autostart on` / `off`;
  - `recipe run`, `dashboard` (it opens a browser tab).
- Tests run with: `MCP_API_KEY=test-key-0123456789abcdef0123456789 MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true MAC_MCP_SKIP_MENU_APP_INSTALL=1 ~/mac-mcp/.venv/bin/python -m unittest <module>`.

## Review Focus

1. **`uninstall --yes` moves the runtime this CLI runs from.** Every module the themed screen needs is imported before the move, and the summary still prints after it. Test in Task 7.
2. **`repair --yes` in a terminal runs pip and the Swift app installer.** Their output never lands in the middle of the spinner: it is captured, and shown under the ✗ only when a fix fails. Test in Task 6.
3. **A secret typed into a terminal (`credential cloudflare save`, `logins add`):**
   - it is read hidden (`getpass`) and never printed;
   - piped stdin (installer, menu app) is read exactly as today.

   Test in Tasks 2 and 3.
4. **`catalog enable-script` preview:** the script is shown whole and raw, so what the owner approves is what runs. That means no ANSI codes, no indent and no wrapping. Test in Task 4.
5. **`recipe run`:**
   - a recipe that needs approval is a warning with exit 2;
   - a server that is down is an error that names `mac-mcp start`, with exit 3.

   Test in Task 3.

---

### Task 1: Pin today's plain output of the Phase 3 commands
- New `tests/test_cli_plain_contract_phase3.py`, with mocked inputs and a non-TTY `StringIO`. It holds the exact current text and exit code of:
  - `dashboard`: not running; token missing; opened; browser did not open (`webbrowser.open` patched).
  - `credential cloudflare`:
    - `status`: configured and secure / not configured / unsafe;
    - `save` (stdin patched), ok and error;
    - `remove`: removed / was not configured.
  - `autostart`: status on, status off, and a save error.
  - `recipe list`: items, empty, and server down. `recipe run`: done, needs approval, failed, and a bad `--param`.
  - `logins`:
    - `list`: rows, empty;
    - `add` (stdin patched);
    - `subdomains`, `remove` (found / not found), `import-preview`, `import`;
    - a `VaultError`.
  - `catalog`:
    - `list`, `review`;
    - `install` without `--yes`, `install --yes`;
    - `enable-script` preview and `--yes`;
    - a `CatalogError`.
  - `conformance`: with `run_conformance` patched, pass and fail.
  - `repair`:
    - nothing to repair;
    - preview with a broken row;
    - `--yes` with each fix patched.
  - `uninstall`: preview; preview with `--include-state`; `--yes` in a non-default state dir, with `apply_uninstall` patched.
- These tests run on today's code, pass, and pin the contract.
- Commit: `Tests: pin phase 3 commands' plain output`.

### Task 2: Themed `dashboard`, `credential`, `autostart`
- **`dashboard`** in a terminal:
  - header `dashboard`;
  - row `Address  <url without the token>`;
  - summary ok "Opened the dashboard in your browser";
  - if the browser did not open: summary warn "The browser did not open", next `mac-mcp dashboard`;
  - not running: `render_error` "Mac MCP is not running", next `mac-mcp start`, exit 1;
  - token missing: `render_error` "The dashboard key is missing", next `mac-mcp restart`, exit 1.
  - The token never appears on screen. The test asserts it is absent.
- **`credential cloudflare`** in a terminal:
  - `status`: row `Cloudflare Tunnel` with one of
    - ok "saved" · "owner-only file" → summary ok, exit 0;
    - info "not saved" → summary warn "No Cloudflare Tunnel credential is saved", next `mac-mcp credential cloudflare save`, exit 1;
    - error "saved but unsafe" · reason → summary error, exit 2.
  - `save`:
    - when stdin is also a TTY, read the token with `getpass.getpass("  Cloudflare Tunnel token: ")` (hidden, one line);
    - otherwise read stdin exactly as today;
    - summary ok "Saved the Cloudflare Tunnel credential", note "owner-only file";
    - error → `render_error`, exit 2.
  - `remove`: summary ok "Removed the Cloudflare Tunnel credential" or info "No Cloudflare Tunnel credential was saved".
- **`autostart`** in a terminal (without `--json`):
  - row `Start at login` with ok "on" · "a server you left running starts again when you log in", or info "off" · "start it with mac-mcp start or the menu bar app";
  - summary ok "Start at login is on" / "Start at login is off". When off, next is `mac-mcp autostart on`.
  - a save error → `render_error`, exit 1.
- Tests (`tests/test_cli_views.py`):
  - `DashboardScreen`: opened (no token on screen), not opened, not running, token missing.
  - `CredentialScreen`: three status states with their codes; save from a TTY uses getpass and never prints the token; save from a pipe reads stdin; remove.
  - `AutostartScreen`: on, off, save error, and `--json` stays JSON.

### Task 3: Themed `recipe` and `logins`
- **`recipe list`** in a terminal:
  - rows: dot ok for active, warn for paused; label = name; value = recipe id; detail = parameters (`name:type`, `?` when optional);
  - summary ok `N recipes`;
  - empty: summary info "No active recipes", note "save one with computer_plan(save_as_recipe=…)";
  - server down (code 3) or token missing: `render_error` with the message, next `mac-mcp start`, exit 3;
  - other errors: `render_error`, the same code.
- **`recipe run`** in a terminal:
  - a step "Ran <recipe_id>" / "Run <recipe_id>" with a spinner;
  - done → summary ok "<name> completed", note "N steps";
  - `approval_required` → summary warn "<name> needs approval first", exit 2;
  - failed → ✗ with `status: message` as the reason, summary error "The recipe did not complete", exit 1;
  - server down → `render_error` next `mac-mcp start`, exit 3;
  - a bad `--param` → `render_error` "--param must be name=value: …", exit 1.
- **`logins`** in a terminal (every subcommand without `--json`):
  - `list`: rows with no dot: label = site, value = username, detail = id (and "also subdomains"). Summary ok `N saved logins`. Empty: summary info "No saved logins", note "add one in Settings › Saved Logins".
  - `add`:
    - when stdin is a TTY, the password is read with `getpass.getpass("  Password: ")`; otherwise from stdin exactly as today;
    - summary ok "Saved the login for <site>".
  - `subdomains`: summary ok "<site>: also on its subdomains" / "<site>: this site only".
  - `remove`: ok "Removed the saved login", or error "No such saved login" (exit 1).
  - `import-preview`: rows index / site / username, summary info `N logins can be imported`, next `mac-mcp logins import <file>`; empty: info "No usable logins".
  - `import`: summary ok "Imported N logins · skipped M", note "export file deleted" when it was.
  - `VaultError` / `ValueError`: `render_error` with the message, exit 1.
- Tests:
  - `RecipeScreen`: list, empty, server down (code 3, names `mac-mcp start`), run done, needs approval (2), failed (1), bad param.
  - `LoginsScreen`: list (no password field ever printed), empty, add from a TTY uses getpass, add from a pipe, remove missing (1), import-preview, import, error.

### Task 4: Themed `catalog`
- **`list`**:
  - rows: dot ok "installed <v>", warn "update available", info "not installed"; label = pack name; value = `id · version · kind`;
  - a dim line "Signed by <signer>";
  - summary ok `N packs`, or warn `N updates available`, next `mac-mcp catalog review <first id with an update>`.
- **`review`**, via `render_catalog_review(style, report)`:
  - rows Pack (name · id version kind), Publisher, Signed by (signer · catalog digest 16), Action (+ installed), Capabilities, Recipe tools (when any), Permission changes (when any);
  - files as dim rows `path · size B · sha256 16`;
  - held scripts as a warn row with the note;
  - `note` as a dim line;
  - `blocked` → summary error "Blocked: …", else summary ok "Reviewed <id> <version>".
- **`install`**:
  - without `--yes`: the review screen, then summary warn "Nothing installed", next `mac-mcp catalog install <id> --yes`, exit 1;
  - with `--yes`: step "Installed <id> <version>" / "Install <id>", summary ok, note "the recipe is a draft: activate it after review" when there is a recipe.
- **`enable-script`**:
  - without `--yes`: rows Script (path) and SHA-256 (full); a dim line "Script:"; then the content written raw (no ANSI, no indent, no wrap); then summary warn "Nothing enabled — it can run anything", next `mac-mcp catalog enable-script <id> <path> --yes`, exit 1;
  - with `--yes`: summary ok "Enabled <path> for <id>".
- `CatalogError` → `render_error`, exit 1.
- Tests: `CatalogScreen`: list with an update, review (with blocked, held scripts, files), install preview (1) and done, enable-script preview (raw block byte-for-byte), error.

### Task 5: Themed `conformance`
- Factor the grouped checklist body of `render_doctor` into `_render_checklist(style, groups, verbose)`. The existing doctor tests must stay green.
- `render_conformance(style, report)`:
  - header `conformance`;
  - groups by `check_id` prefix: Browser, Native app, Observation, Workspace, Planning, and Live for checks whose `category` is not `computer_use`, with Other as the fallback;
  - passed checks fold per group into `✓ N checks passed`; warn and fail are written out with Fix lines;
  - summary:
    - ok "All N Computer Use checks passed", note `<pass rate>% · <focus regressions> focus regressions · <duration>`;
    - error `N failed` — fix them, then run `mac-mcp conformance`;
    - warn when there are only warnings.
- The run itself goes inside a step "Ran the Computer Use checks" / "Run the Computer Use checks" (spinner; `--live` can take seconds).
- Exit code unchanged.
- Tests: `ConformanceScreen`: all pass, a failure with its Fix line, live group, and doctor screens unchanged (the existing tests).

### Task 6: Themed `repair`
- Preview in a terminal:
  - rows: ok dot `label  detail`; error dot `label  detail · fix: <fix>`;
  - nothing broken → summary ok "Nothing to repair";
  - broken → summary warn `N parts need repair`, next `mac-mcp repair --yes`.
- `--yes` in a terminal: one step per broken part, with these labels:

  | Part | Done | Running |
  |---|---|---|
  | venv | "Rebuilt the Python environment" | "Rebuild the Python environment" |
  | cli | "Recreated the mac-mcp command" | "Recreate the mac-mcp command" |
  | supervisor | "Reinstalled the start-at-login job" | "Reinstall the start-at-login job" |
  | app | "Reinstalled the menu bar app" | "Reinstall the menu bar app" |

  - Subprocess output is captured, never written to the terminal while the spinner runs:
    - `venv_repair.repair(..., quiet=True)` runs its commands with `capture_output` and puts the end of their output in its error message;
    - the app installer runs with `capture_output=True`;
    - stderr inside each step is redirected into the step's buffer.
  - The app step ends ✗ with the reason when the installer exits non-zero, or when it cannot be rebuilt here (the plain text's sentence).
  - Then the rows after repair, and summary ok "Repaired N parts" or error `N parts still need repair`, next `mac-mcp doctor`.
- Exit code unchanged.
- Plain path: `venv_repair.repair` without `quiet` behaves exactly as today.
- Tests: `RepairScreen`: nothing to repair, preview, `--yes` success (and an installer that writes to the fds does not reach the terminal), a failing fix shows its reason.

### Task 7: Themed `uninstall`
- Preview in a terminal:
  - block "Would move to the Trash": rows label · path (dim), with the note as detail;
  - block "Kept": rows label · path, plus "add --include-state to move your settings, keys and tokens too" lines as dim notes;
  - the "Not changed: …" sentence as a wrapped dim note;
  - summary warn "Nothing was changed", next `mac-mcp uninstall --yes[ --include-state][ --include-source]`.
- `--yes` in a terminal:
  - import `cli_steps` and `cli_views` before anything moves;
  - default install: step "Stopped Mac MCP" / "Stop Mac MCP" around the existing `_stop_for_uninstall` and the two bootouts;
  - non-default: a warn note with today's sentence;
  - step "Moved N parts to the Trash" / "Move Mac MCP to the Trash", each failure as the reason;
  - summary:
    - all moved → ok "Mac MCP is removed", note = Trash folder (`home_path`), then the "Not changed" sentence as a dim note;
    - failures → error `N parts could not move — remove them by hand`, exit 1.
- Tests: `UninstallScreen`:
  - preview (and with `--include-state` in the command);
  - `--yes` in a non-default state dir (no stop, the warn note);
  - `--yes` in the default dir (stop step, bootouts patched);
  - a failure (exit 1);
  - the screen needs no import after `apply_uninstall` runs (an `__import__` guard raises for `mcp_server.*` after the move).

### Task 8: Verify, docs, promote, live check, review
- Full suite, `git diff --check`, `node --test menu_app/Onboarding/tests/`.
- CHANGELOG entry and a `docs/reference.md` sentence. Commit and push.
- Promote the changed runtime files: check that each runtime copy equals the previous commit first, then back it up. Restart only if a server module changed (CLI-only files need none). Verify the installed CLI.
- Live renders with the installed CLI in a real pty (80 and 40 columns):
  - `credential cloudflare status`;
  - `autostart`;
  - `recipe list`;
  - `logins list` (output kept in the scratchpad, not quoted);
  - `catalog list`;
  - `catalog review <an id from list>`;
  - `conformance`;
  - `repair` (preview);
  - `uninstall` (preview);
  - `uninstall --include-state` (preview).
- The side-effect paths are covered by mocked pty demos, never production:
  - `dashboard`;
  - `credential save`;
  - `logins add`;
  - `recipe run`;
  - `catalog install`;
  - `repair --yes`;
  - `uninstall --yes`.
- Confirm the plain output through a pipe, and that `--json` and the menu app's Saved Logins, Start at login and Cloudflare credential state still work.
- Run a fresh whole-branch review on the most capable model, then do one fix pass with TDD.
