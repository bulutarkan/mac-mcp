# Server permission identity ("Mac MCP", not "Python") — design

Date: 2026-10-12 · Status: draft · Part 1 of the premium onboarding work
(part 2: `2026-10-12-premium-onboarding-design.md`)

## 1. Problem

macOS records Accessibility, Screen Recording and Automation consent for the
*responsible process* of whatever asks. The Mac MCP server is
`python -m uvicorn …`, so today:

- started by launchd (restart hand-off, supervisor), the responsible process is
  Homebrew's `Python.app` → System Settings and every prompt say **"Python"**;
  owners read that as something foreign and unsafe.
- started by `mac-mcp start` in Terminal, the responsible process is
  **Terminal**, so permissions silently depend on how the server was started.
- a Homebrew Python upgrade (3.14.7 → 3.14.8) changes the binary, so granted
  permissions can disappear.

## 2. Evidence (spike, 2026-10-12, throwaway code)

A 20-line C launcher inside an ad-hoc-signed bundle
(`CFBundleName` "Mac MCP"), started by launchd, `posix_spawn`s the venv
Python. Read with `responsibility_get_pid_responsible_for_pid` (no prompt):

| | responsible process | AX / screen preflight |
|---|---|---|
| python started by launchd directly | Homebrew `Python.app` | granted (Python's grant) |
| python started by the launcher via launchd | **the launcher app** | not granted (new identity) |

Building the launcher twice from the same source gave the same CDHash. For an
ad-hoc signature macOS ties the grant to the CDHash, so the grant survives
rebuilds as long as the launcher's bytes do not change.

## 3. Decisions

- **A dedicated helper app owns the permissions:** `Mac MCP Server.app`
  (bundle id `com.bulutarkan.mac-mcp.server`, display name **"Mac MCP"**,
  `LSUIElement`, the Mac MCP icon). Its only code is a tiny C launcher that
  spawns the server as a child, forwards SIGTERM/SIGINT/SIGHUP and exits with
  the child's status. It never `exec`s (after `exec` the responsible process
  would run Python's code and macOS would see "Python" again).
- **The launcher is frozen.** Source `menu_app/ServerHelper/launcher.c`, built
  with `clang -O2` and ad-hoc signed. A build writes the SHA-256 of the source
  next to the bundle; install, update and package steps rebuild the helper
  only when that hash differs, so the CDHash, and the owner's grants, stay
  stable. A toolchain update alone never triggers a rebuild.
- **Location:** `~/mac-mcp/.helper/Mac MCP Server.app` (in the runtime, not
  inside `Mac MCP.app`, so menu-app updates never touch it). The package
  installer ships it prebuilt in its staging and lays it out like the Python
  tree.
- **launchd starts it, for the default install only.** `mac-mcp start`
  bootstraps a transient LaunchAgent `com.macmcp.server` from the state folder
  (no `RunAtLoad` at login, no `KeepAlive`; the supervisor and start-at-login
  already handle restarts) whose program is the helper with the existing
  `python -m uvicorn mcp_server.main:app …` arguments, environment and log
  file. Isolated installs (non-default state, see `DEFAULT_STATE_DIR`) keep
  today's direct `Popen`, so tests never touch a shared label.
- **The recorded server pid is the Python child**, found as the listener on
  the port whose parent is the helper. Role matching, listener ownership,
  `stop`, `status`, `doctor` and the supervisor keep working on the same
  `python -m uvicorn` process they know today. Stopping the child ends the
  helper; `stop` also boots out `com.macmcp.server`.
- **Fallback:** if the helper is missing or fails to start, `start` falls back
  to the direct spawn and says so in `status`/`doctor`
  ("permissions are attributed to Python").
- **Migration:** the first start through the helper is a new identity, so
  Accessibility and Screen Recording must be granted once more, now listed as
  "Mac MCP". Doctor, Get Started and the onboarding (part 2) detect
  "not granted" and walk the owner through it; the release notes say so.
  The owner's production install is switched only when the owner is ready
  to click the prompts.
- **Asking for permission comes from the server.** New authenticated
  `POST /dashboard/api/permissions/request {"permission": "accessibility" |
  "screen_recording"}` calls `AXIsProcessTrustedWithOptions(prompt)` /
  `CGRequestScreenCaptureAccess()` in the server process, so the prompt names
  "Mac MCP". It returns the state afterwards and whether a restart is needed
  (Screen Recording takes effect after a restart).

## 4. Components

| Unit | Responsibility |
|---|---|
| `menu_app/ServerHelper/launcher.c`, `Info.plist` | the frozen launcher and its bundle metadata |
| `menu_app/ServerHelper/build_helper.sh` | build + ad-hoc sign + write `.source-sha256`; no-op when the hash matches |
| `mcp_server/server_helper.py` | locate/ensure the helper, build the LaunchAgent payload, find the Python child |
| `mcp_server/cli.py` (`_start_server_locked`, `stop`) | start through the helper for the default install, record the child, boot out on stop |
| `mcp_server/permission_probe.py` + `dashboard_routes.py` | `request_permission()` and the new route; `process_identity()` reports the helper |
| `install.sh`, `mcp_server/update_helper.py`, `packaging/pkg/pkg_install.py`, `scripts/build_pkg.sh` | build or lay out the helper (hash-gated) |
| `mcp_server/diagnostics.py` | doctor shows who holds permissions ("Mac MCP" vs "Python — fallback") |

## 5. Testing

- Unit: payload contents, hash-gated rebuild (no rebuild when the hash
  matches, rebuild when it differs), child-pid discovery, fallback path,
  isolated installs never bootstrap `com.macmcp.server`.
- Integration (opt-in, no prompts): start a throwaway server through the
  helper on port 8878 with a temporary label and assert the responsible
  process of the listener is the helper (the spike's probe as a test).
- Live (owner present): switch production, accept the "Mac MCP" prompts,
  confirm AX/screen granted, restart, confirm grants persist across a
  `mac-mcp restart` and a helper rebuild from the same source.

## 6. Out of scope

Developer ID signing (would make grants survive any rebuild); renaming the
product; Automation prompts (they follow the same responsible process, so they
also show "Mac MCP", but are requested when first used).
