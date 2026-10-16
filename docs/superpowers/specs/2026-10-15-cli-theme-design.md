# CLI theme: one calm, modern style for every `mac-mcp` command

Date: 2026-10-15
Status: approved in conversation, awaiting written-spec review

## Goal

`mac-mcp status`, `start`, `stop`, `doctor` and the rest print developer-log text today
("mac-mcp is running (pid 28323; identity verified via pid_record)", `[PASS] runtime.python: …`).
The owner wants every command to share one theme: calm, modern, readable, the same feel as the
menu app and the tour.

Success means:

- In a terminal, every command opens with the same header, lays its body out the same way and
  ends with one summary line that says what happened in plain words.
- Long operations show live, step-by-step progress.
- Nothing that reads the CLI's output breaks: the menu app, the installer, the updater, scripts
  and tests see exactly the text they see today.

## Owner decisions

| Question | Choice |
|---|---|
| Style | Calm and modern: brand header, coloured status dots, aligned label/value rows, one summary line; little colour, plenty of space |
| Long operations | Step by step, live: a spinner per step, fixed with ✓ and its duration when done |
| `mac-mcp` / `--help` | Grouped and short: Everyday, Connect, Troubleshoot, Advanced; bare `mac-mcp` adds a one-line live status |
| Approach | Our own small style module, no new dependency |
| Rollout | Three phases (below) |

## The one rule: plain output never changes

The theme is on only when standard output is a terminal. It is off, and the output is
byte-for-byte today's, when any of these holds:

- standard output is not a TTY (a pipe, a file, the menu app's `Process`, the installer);
- `NO_COLOR` is set (any value);
- `TERM=dumb`;
- `MAC_MCP_PLAIN=1` (new; for anyone who wants the old text in a terminal);
- the command runs with `--json`.

`mac-mcp stdio` never draws anything: it loads only the small style module (through the
argument parser), never the screens or the live steps.

Why: the menu app parses CLI text. `DoctorReport.parse` reads `doctor`'s `[PASS]`/`[WARN]`
lines, `AppState.notice(for:result:)` turns `start`/`restart` output into notices, and
`applyUpdateCheckOutput` reads `update`. All of them run the CLI without a terminal. The
installer runs `mac-mcp --version` without one and `mac-mcp start` with the installer's own
terminal, so an interactive install shows the themed start; it reads only the exit code.

## Theme

### Colours

Taken from the app and tour tokens, and chosen to read on light and dark terminal backgrounds.

| Role | Colour | Used for |
|---|---|---|
| Brand / accent | ember `#ffb45e` | `◆ Mac MCP` header, command names in help |
| Success | green `#5ed3a0` | `●` running, `✓` |
| Warning | amber `#f5a524` | `⚠`, attention rows |
| Error | red `#ff6b6b` | `✗`, failed steps |
| Info | violet `#8b7cf6` | `ℹ`, hints |
| Secondary | terminal dim attribute | pids, paths, durations, details |

Colour depth: truecolor when `COLORTERM` is `truecolor` or `24bit`, else 256 colours when
`TERM` contains `256color`, else the 16 basic ANSI colours.

One palette cannot read on both backgrounds (the bright tones fall to 1.3–2.4:1 on a light
background). In a terminal the CLI asks the terminal for its background colour (OSC 11, at most
0.15 s, once per run; Terminal.app answers) and uses deeper tones on a light one: brand
`#a84d0b`, ok `#16794a`, warn `#9a6200`, error `#c22d2d`, info `#5b45d6` (5:1 or more on
white). No answer means the dark tones above. `MAC_MCP_THEME=light|dark` chooses by hand.

### Symbols

`◆` header · `●` status dot (green / amber / red) · `✓` done · `⚠` warning · `✗` error ·
`ℹ` info · `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` live step · `·` separator.

### Layout rules

- Every command opens with `◆ Mac MCP · <command>` and an empty line.
- Body lines are indented two spaces. Labels are left-aligned; values start in one column per
  block.
- Details (pid, path, duration) are dim. Bold is used only for labels and the summary.
- Every command ends with one summary line: `✓ …`, `⚠ …` or `✗ … — try: <command>`.
- Errors always read `✗ What happened — try: <command>` when there is a next step. No Python
  traceback is shown; `--verbose` adds the detail.
- Language stays English, like the app. Text is human, not log: "Running · pid 28323" rather
  than "identity verified via pid_record".
- Lines wrap to the terminal width (`shutil.get_terminal_size`), never past 100 columns.

## Output patterns

Every command uses one of four bodies between the header and the summary.

### A. Status (`status`, `clients`, `privacy`, …)

A dot, a label, a value, dim details.

```
  ◆ Mac MCP · status

  ● Running          pid 28323 · up 2h 14m
  ● Public address  mac.tarkan.cloud  (Cloudflare)
  ● Crash guard     watching · last recovery Oct 13

  Logs  ~/.mac-mcp/mac-mcp.log

  ✓ All good — AI apps can reach this Mac.
```

Server stopped:

```
  ◆ Mac MCP · status

  ● Stopped          not answering on port 8765
  ● Public address  mac.tarkan.cloud  (Cloudflare · waiting for the server)

  ✗ Mac MCP is not running — try: mac-mcp start
```

Exit codes are unchanged: 0 healthy, 1 server down or not answering, 2 public endpoint
unavailable or misconfigured.

### B. Steps (`start`, `stop`, `restart`, `update`, `connect`)

Each step starts with a spinner and is fixed with `✓` and its duration, or `✗` with the reason
under it and the next command.

```
  ◆ Mac MCP · restart

  ✓ Stopped the server             0.8s
  ✓ Started the server             2.1s
  ✓ Cloudflare tunnel connected    1.4s

  ✓ Restarted — pid 28323 · mac.tarkan.cloud answers
```

`restart` hands its work to a one-shot launchd worker and returns at once today ("handed off
to one-shot launchd worker"). In a terminal it waits for the result and shows the steps, as
`--wait` does now. Without a terminal it behaves exactly as today.

### C. Checklist (`doctor`)

Checks are grouped (Runtime, Server, Permissions, Dependencies, Agents, Updates). Passed checks
fold into one line per group; warnings, failures and notes are written out.

```
  ◆ Mac MCP · doctor

  Runtime        ✓ 5 checks passed
  Server         ✓ 3 checks passed
  Permissions    ✓ Accessibility · Screen Recording · Automation
                 ℹ Microphone is asked the first time you use voice
  Dependencies   ✓ osascript · cliclick · cloudflared · ngrok
  Agents         ℹ 2 agent worktrees hold unreviewed changes (859.7 MiB)

  ✓ Everything Mac MCP needs is in place.   (--verbose: every check)
```

`--verbose` lists every check. The `[PASS] name: message` text stays as it is without a terminal.

### D. Home and help

`mac-mcp` with no command: one live status line above the grouped help.
`mac-mcp --help`: the same screen without the status line (no server call).

```
  ◆ Mac MCP · 2.2.0  ● Running

  Everyday
    status      How Mac MCP is doing
    start       Start the server
    stop        Stop the server
    restart     Restart the server

  Connect
    clients     AI apps on this Mac
    connect     Add Mac MCP to an AI app

  Troubleshoot
    doctor      Check everything
    logs        Recent server log

  Advanced  dashboard · privacy · recipe · logins · catalog · credential · …

  mac-mcp <command> --help for more.
```

`mac-mcp <command> --help` uses the same theme: a description, the options, one short example.

Groups:

| Group | Commands |
|---|---|
| Everyday | status, start, stop, restart, update |
| Connect | clients, connect, connect-config |
| Troubleshoot | doctor, logs, repair |
| Advanced | dashboard, privacy, recipe, logins, catalog, credential, autostart, conformance, uninstall |

`stdio` is not listed in the grouped screen (it is what AI apps run); it stays in
`mac-mcp stdio --help`.

## Architecture

### `mcp_server/cli_style.py` (new, standard library only)

- **`Style`**: decides once per process whether the theme is on (the rule above) and the colour
  depth; holds the palette and the symbols. `Style.for_stream(stream, json=False)`.
- **Writers**: `header(command)`, `row(dot, label, value, detail="")`, `note(text)`,
  `group(label, lines)`, `summary(tone, text, next_command=None)`, `error(text, next_command)`.
  Column alignment is computed per block.
- **`Steps`**: a context manager for live steps. The spinner runs on a daemon thread and redraws
  one line with `\r`. Each `step(label)` is fixed with `✓ label  1.2s` or `✗ label` on exit.
  Ctrl+C or an exception fixes the current line as `✗` and restores the cursor; the exception
  is re-raised. With the theme off `Steps` prints nothing, and the command's own plain prints
  run as before.
- **`GroupedHelpFormatter`**: an `argparse` formatter for the grouped home/help screen and the
  themed per-command help. With the theme off, argparse's default output is used.

### How commands use it

Each command splits gathering from printing, then chooses once:

```python
info = _status_info(args)          # what today's code computes, unchanged
if style.enabled:
    return _render_status(info, style)
return _print_status_plain(info)   # today's print() calls, moved verbatim
```

The plain branch keeps today's lines and their order exactly. The themed branch reads the same
`info`. Exit codes come from `info`, so both branches return the same code.

### Phases

1. **Phase 1**: `cli_style.py`; home (`mac-mcp`) and `--help`; `status`; `start`, `stop`,
   `restart`; `doctor`.
2. **Phase 2**: `clients`, `connect`, `connect-config`, `update`, `logs`, `privacy`.
3. **Phase 3**: `dashboard`, `recipe`, `logins`, `catalog`, `credential`, `autostart`,
   `conformance`, `repair`, `uninstall`.

Each phase is committed, promoted and shown to the owner on its own.

## Testing

- **Plain-output contract (golden tests)**: for every command in the phase, with a non-TTY
  stream, the output equals today's text exactly. Captured before any change, so the
  refactor cannot drift.
- **Style decision**: theme off for non-TTY, `NO_COLOR`, `TERM=dumb`, `MAC_MCP_PLAIN=1` and
  `--json`; colour depth from `COLORTERM`/`TERM`.
- **Themed output (golden tests)**: a fake TTY at a fixed width; header, alignment, dim
  details, summary and error format, with ANSI codes kept in the expected text.
- **`Steps`**: success fixes `✓` with a duration; an exception and Ctrl+C fix `✗`, clear the
  spinner and re-raise; nothing is printed with the theme off.
- **Help**: the grouped screen lists every command except `stdio` exactly once; per-command help
  still shows every option.
- **App compatibility**: `DoctorReport.parse` and the start/restart notice still work on the
  plain output (existing Swift-source and Python tests, plus the golden tests above).
- **Live check per phase**: screenshots of `status`, `restart`, `doctor` and the home screen in
  a real terminal (light and dark); the app's Settings › Help & Diagnostics still shows the
  doctor report.

## Out of scope

- Interactive menus, prompts or a TUI.
- Any change to what a command does, except that `restart` in a terminal waits for the
  result.
- Translation; the CLI stays English.
- A new dependency (`rich` and similar were considered and declined).
