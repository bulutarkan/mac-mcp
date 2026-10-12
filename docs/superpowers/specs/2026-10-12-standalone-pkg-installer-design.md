# Standalone `.pkg` installer — design

Date: 2026-10-12 · Status: draft for review · Roadmap: related to #58, #59, #132, #173

## 1. Goal

A person who has never used Terminal can install Mac MCP by double-clicking one
`.pkg` file. The package installs the menu bar app and the MCP server, lays out
the runtime and state, and starts everything in the background. No Xcode
Command Line Tools, Git, Homebrew or system Python are needed.

`install.sh` stays the developer/advanced path and is not changed by this work.

### Decisions already made

- **Unsigned for now.** No Developer ID exists on the build Mac. The first open
  goes through System Settings → Privacy & Security → "Open Anyway". Developer
  ID signing/notarization (and #173 MacUpdate) is revisited only if a Developer
  ID is obtained.
- **Python is bundled** inside the package.
- **Updates for pkg installs, v1:** install the newer `.pkg` over the old one.
  An in-app, Git-free updater is a later, separate project.
- **Build cadence:** packages are built only at signed stable checkpoints,
  never per commit, by a local script first. Moving the build to GitHub Actions
  happens after the script is proven.
- **Apple Silicon only (arm64) in v1.** onnxruntime no longer publishes
  macOS x86_64 wheels, and the build Mac is arm64.

## 2. Non-goals (v1)

- Developer ID signing, notarization, stapling.
- An in-app updater for pkg installs (the existing Git updater keeps serving
  `install.sh` installs).
- Intel Macs.
- Public endpoint (Cloudflare/ngrok) setup and subagent provider setup: these
  default to off and move to onboarding (#59) / Settings.
- An uninstaller (#132).
- Binding the pkg hash into the signed release manifest. The pkg contains
  `release/stable-manifest.json`, so it cannot be listed inside that same
  manifest. v1 publishes a `.sha256` file next to the pkg.

## 3. Install layout

The pkg installs into the current user's home (`enable_currentUserHome`
domain: "Install for me only"). No administrator password, and the install
scripts run as the user, never as root.

Final layout matches an `install.sh` install so the rest of the product
(CLI, menu app, doctor, settings) behaves identically:

| Path | Content |
|---|---|
| `~/Applications/Mac MCP.app` | Prebuilt, ad-hoc-signed menu bar app + Safari Visual Companion appex |
| `~/mac-mcp/` | Runtime: source tree of the checkpoint commit (no `.git`) |
| `~/mac-mcp/.python/` | Bundled CPython (python-build-standalone, pinned) |
| `~/mac-mcp/.venv/` | Virtualenv created from `.python`, deps installed offline |
| `~/.mac-mcp/` | State (settings, tokens, update state) |
| `~/.mac-mcp/install-kind` | `pkg` (new marker; absent for `install.sh` installs) |
| `~/.local/bin/mac-mcp` | CLI launcher (same `cli_bootstrap.ensure_cli_launcher`) |

There is no source checkout (`~/Projects/mac-mcp`) for pkg installs.

## 4. Package contents

Payload (relative to the user's home):

- `Applications/Mac MCP.app`
- `.mac-mcp/pkg/staging/` containing:
  - `release.json` — `release_id`, `version`, `commit`, `payload_sha256`,
    `python_version`, `arch`, build time.
  - `source.tar` — `git archive <checkpoint commit>`, the same tree
    `install.sh` puts in the runtime.
  - `python.tar.gz` — pinned python-build-standalone `install_only_stripped`
    for `aarch64-apple-darwin` (version and SHA-256 pinned in the build script).
  - `wheelhouse/` — every wheel from `requirements.lock`, arm64/py3.12, with
    hashes still enforced at install time.

Scripts: `preinstall`, `postinstall` (bash, `/usr/bin` tools only for
preinstall; postinstall hands off to the bundled Python).

Expected size: roughly 100–150 MB compressed (onnxruntime, numpy and the
Python runtime dominate).

## 5. Components

### 5.1 `scripts/build_pkg.sh` (build machine)

1. Refuse unless the tree is clean and `HEAD` (or `--commit`) is a verified
   stable checkpoint: `scripts/verify_release_manifest.py --commit <c>
   --branch main` must pass.
2. Build the app from that commit's sources into a temp dir
   (`menu_app/build_app.sh`), `codesign --verify --deep --strict`.
3. Download the pinned Python tarball, verify SHA-256.
4. `pip download --only-binary=:all: --require-hashes` from
   `requirements.lock` for macOS arm64 / CPython 3.12 into the wheelhouse;
   the setuptools/wheel build tools from the lock are included so the
   editable install of the runtime works offline.
5. Write `release.json`, assemble the payload root, `pkgbuild` +
   `productbuild` with a distribution that enables only the
   current-user-home domain and requires macOS ≥ 13 on arm64.
6. Output `dist/Mac-MCP-<version>-<release_id>-arm64.pkg` and a `.sha256`.
   `dist/` is git-ignored.

### 5.2 `preinstall`

Runs before any file is placed. Aborts the install (non-zero exit, clear
message in the Installer log) without touching anything when:

- `~/mac-mcp` exists and `~/.mac-mcp/install-kind` is not `pkg`
  (an `install.sh` / developer install — never overwritten by a pkg), or
- the Mac is not arm64 / macOS < 13 (also enforced by the distribution).

### 5.3 `packaging/pkg/pkg_install.py` (postinstall core)

Run by the bundled Python straight from staging, so it is unit-testable with a
temporary `HOME` and alternate port. Steps:

1. Read `release.json`; validate staging contents and the wheel hashes against
   the lock.
2. **Upgrade only:** stop the running server (`mac-mcp stop`) and menu app,
   move the current runtime to `~/.mac-mcp/backups/pkg-<timestamp>/runtime`
   (only the newest backup is kept).
3. Extract `source.tar` into `~/mac-mcp`, `python.tar.gz` into
   `~/mac-mcp/.python`, create `~/mac-mcp/.venv` from it **in its final
   location** (venvs are not relocatable), then
   `pip install --no-index --find-links wheelhouse --require-hashes -r
   requirements.lock` and `pip install --no-index --no-deps
   --no-build-isolation -e ~/mac-mcp`, `pip check`, import smoke test —
   the same steps as `install.sh`'s `install_python_environment`, offline.
4. **Fresh install:** write `mcp_server/.env` from `.env.example` with a newly
   generated API key and the same secure defaults as `install.sh`
   (`MCP_ALLOW_NO_AUTH=false`, `MCP_ALLOW_SHELL=true`, profile `standard`),
   mode 600. **Upgrade:** copy the previous `.env` across unchanged.
   `~/.mac-mcp` (settings, tokens, keys) is never rewritten, only added to.
5. Chrome companion config (`ensure_chrome_companion_config`), CLI launcher
   (`ensure_cli_launcher`), `~/.local/bin` PATH line in `~/.zprofile` (same
   as `install.sh`), `~/.mac-mcp/update/deployed-commit`, `install-kind`.
6. Start the server (`mac-mcp start`) and open the app in the background
   (`open -g`). Verify `/health` on the configured port.
7. On any failure in steps 3–6: remove the partial runtime, restore the backup
   runtime (upgrade) and restart it, exit non-zero so Installer reports
   failure. A failed fresh install leaves no runtime behind.
8. Delete `~/.mac-mcp/pkg/staging`. Write a sanitized log to
   `~/.mac-mcp/logs/pkg-install.log` (no API key or tokens).

### 5.4 Small product changes

- **Updater for pkg installs:** `mac-mcp update` / `update --check` and the
  app's Updates section detect `install-kind = pkg` and report "this install
  was made from a package; download the newer `.pkg` from GitHub Releases"
  with the link, instead of the current "Repository not found" error.
- **Doctor:** shows the install kind.
- `.gitignore`: `dist/`.

## 6. Verification

### 6.1 `install.sh` still works (done first, independent of the pkg)

Isolated fresh installs with a temporary `HOME`, temp state/runtime/app/CLI
paths, no public endpoint, `MAC_MCP_START_NOW=no` and `MAC_MCP_SKIP_MENU_APP=1`
(launchd labels are shared with production, so the isolated install never uses
`mac-mcp start`):

1. **Published channel:** run `install.sh` as users get it (clones GitHub,
   picks the newest signed checkpoint).
2. **Current `HEAD`:** a copy of `install.sh` in temp with a throwaway trust
   root, against a temp clone of `HEAD` with a manifest signed by that
   throwaway key — proves today's tree (catalog, run evidence, new files)
   installs, before it is released.

For both: app builds and verifies, venv passes `pip check`, then the server is
started directly from the temp runtime's venv on port 8878 with the temp state
dir; `/health` returns 200 and an authenticated `tools/list` succeeds.
Production (`~/mac-mcp`, `~/.mac-mcp`, port 8765, PID) is checked unchanged
before and after.

### 6.2 pkg

- Unit tests for `pkg_install.py` (temp `HOME`): fresh install; upgrade keeps
  `.env`, settings and tokens byte-identical; refuses an `install.sh` install;
  failed pip step rolls back to the previous runtime; staging removed; no
  secrets in the log.
- Tests for the updater/doctor install-kind branch.
- Build check: `pkgutil --check-signature` (expected unsigned),
  `pkgutil --payload-files`, distribution XML inspection, app inside verifies
  with `codesign --verify --deep --strict`.
- Postinstall dry run on this Mac against a temp `HOME` with port 8878.
- **Real end-to-end** (manual gate, needs Tarkan): a separate macOS user
  account on this Mac (or a VM). Download the pkg through a browser so it is
  quarantined, confirm the "Open Anyway" flow, install, see the menu bar app
  and a healthy server, connect a client, then install the same pkg again as
  an upgrade. A real install on Tarkan's own account would replace the
  production runtime, so it is not used for this test.

## 7. Risks

- **Home-domain installs:** `enable_currentUserHome` is older Installer
  functionality; if it misbehaves on macOS 26, fall back to a system-domain
  pkg whose postinstall runs the per-user steps as the console user. Verified
  in the real end-to-end test.
- **Offline wheel resolution:** if any locked package has no arm64/cp312
  wheel, the build fails loudly; no sdist builds on the user's Mac.
- **Size:** ~100–150 MB download; acceptable for v1.
- **Two update paths:** pkg installs update by reinstalling until the in-app
  updater exists; the updater message makes this explicit.
