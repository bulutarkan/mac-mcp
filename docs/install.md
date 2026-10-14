# Install, update and permissions

Requirements, the package and the installer, a manual install, how updates work, and the macOS permissions Mac MCP asks for.

[← Back to the README](../README.md)

## Requirements

- macOS 13+
- Apple Silicon or Intel Mac
- Python 3.10+
- Git
- Xcode Command Line Tools (`swiftc`)
- ngrok only if you choose the built-in ngrok public endpoint mode
- cloudflared only if you choose the built-in Cloudflare Tunnel mode

```bash
brew install python git
# Optional public providers:
brew install ngrok       # ngrok mode
brew install cloudflared # Cloudflare Tunnel mode
```

Optional helpers:

```bash
brew install cliclick brightness
```

## Install

### Package (Apple Silicon, macOS 14+)

Download `Mac-MCP-…-arm64.pkg` from the [latest GitHub Release](https://github.com/bulutarkan/mac-mcp/releases/latest) and open it. It installs the menu bar app and the server for your account (no administrator password) and starts them; no Terminal, Xcode tools, Git, Homebrew or Python are needed. It ships its own Python, so it is about 90 MB.

The package is **not signed with an Apple Developer ID and not notarized yet**, so macOS blocks the first open: go to System Settings → Privacy & Security and choose **Open Anyway**. Compare the download with the `.sha256` file on the release page if you want to check it. The code inside is the signed stable checkpoint named in the file name.

To update a package install, install the newer package the same way; your settings, API key and tokens are kept. The package does not replace an existing `install.sh`/source installation; keep updating that one with `mac-mcp update`. Intel Macs use the installer below.

### Installer

The interactive installer now installs only a cryptographically verified stable release. It clones `main`, finds the newest signed stable-release commit, verifies the pinned bootstrap verifier, Ed25519 manifest signature, complete tracked-file SHA-256/mode/size inventory, aggregate payload digest, and release lineage **before** creating persistent source/runtime paths.

For convenience, the streamed bootstrap is still available:

```bash
curl -fsSL https://raw.githubusercontent.com/bulutarkan/mac-mcp/main/install.sh | bash
```

A streamed script cannot cryptographically authenticate itself before it starts executing. Treat that command as a lower-assurance bootstrap. For higher-assurance installation, obtain `install.sh` from a trusted signed release commit and independently compare the release-signer fingerprint documented in `release/README.md` before running it. Once the trusted installer is running, the cloned source/runtime payload is fail-closed and cryptographically verified.

The installer:

- verifies macOS 13+, Apple Silicon or Intel, Git, Python 3.10+, Xcode Command Line Tools, and `swiftc`;
- verifies the selected stable release cryptographically before moving any source/runtime files into persistent install paths;
- can offer Homebrew when a required dependency is missing, while keeping optional helpers such as `cliclick` and `brightness` optional;
- asks which public endpoint mode you want (`Local only`, `Cloudflare Tunnel`, `ngrok`, or `Custom HTTPS`) and, when Cloudflare/ngrok is selected, offers to install the matching provider with Homebrew if it is missing;
- can finish Cloudflare setup during installation by storing the public hostname in settings and accepting the tunnel token through a hidden terminal prompt; the token is sent to `mac-mcp credential cloudflare save` over stdin and is never placed in shell arguments, settings, or `.env`;
- creates a Git source checkout at `~/Projects/mac-mcp` and a separate runtime at `~/mac-mcp` without Git metadata;
- creates and verifies the Python virtual environment and dependencies;
- generates a strong MCP API key, enables authenticated access, and stores the runtime `.env` with mode `600`;
- installs the `mac-mcp` CLI at `~/.local/bin/mac-mcp` and records the deployed commit for the built-in updater;
- builds and code-sign verifies the native `Mac MCP.app` menu bar controller in `~/Applications`;
- shows both Bearer-token and `?ApiKey=` connection formats at the end;
- does **not** install OpenCode, Codex, or Claude Code. If you want to use Subagents, install the provider you plan to use separately.

Existing source/runtime/CLI paths are never silently overwritten. If Mac MCP is already installed, use the built-in updater instead of re-running the installer over the same paths.

### Manual installation

If you prefer to manage the checkout and Python environment yourself:

```bash
git clone https://github.com/bulutarkan/mac-mcp.git
cd mac-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --no-deps --no-build-isolation -e .
cp mcp_server/.env.example mcp_server/.env
```

`requirements.lock` pins every dependency, transitive packages and build tools included, by version and SHA-256 hash; the installer, the updater and CI all install from it, and pip refuses a package that does not match. After changing dependencies in `pyproject.toml`, regenerate it with the command at the top of the file (`uv pip compile ... --generate-hashes`).

Configure at minimum:

```env
MCP_API_KEY=replace-with-a-long-random-token
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=120

# Optional built-in ngrok provider
NGROK_DOMAIN=your-domain.ngrok-free.dev

# Optional environment overrides for public endpoint selection.
# Normally these are managed from Mac MCP Settings instead.
# MAC_MCP_PUBLIC_ENDPOINT_MODE=cloudflare
# MAC_MCP_PUBLIC_URL=https://mac.example.com/mcp
```

Generate a strong token:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

When authentication is enabled, the preferred client credential is still:

```http
Authorization: Bearer <MCP_API_KEY>
```

For MCP clients/connectors that cannot set an `Authorization` header, Mac MCP also accepts the configured global key in the endpoint URL:

```text
https://your-domain.example/mcp?ApiKey=<MCP_API_KEY>
```

## Updating

**From the app:** click the Mac MCP menu bar icon, open **Settings** (gear icon) → **General** → **Updates**. The card checks the verified release channel when you open it and shows your current version next to the newest verified release. **Check Update** checks again, and **Update Now** installs it while the card lists each step as it runs, from Prepare and Backup through Restart and the final Health check. If an update is interrupted, the same button changes to **Resume Recovery** or **Retry Recovery**. "Blocked by local changes" means the source checkout has uncommitted or untracked files (`git status` in the source checkout lists them): commit, stash or move them, then check again. If anything still looks wrong, `mac-mcp doctor` reports update and recovery state.

**From Terminal**, for automation or if you prefer the CLI:

```bash
mac-mcp update --check
mac-mcp update
```

Both paths run the same updater with the same signature, lineage and dirty-repository checks.

The updater scans the first-parent history of `origin/main` and installs the newest cryptographically verified stable release checkpoint, not arbitrary repository HEAD. Development or otherwise unverified commits ahead of that checkpoint are not offered as normal updates. It also blocks on dirty repositories, preserves runtime overlays and private files, creates a runtime backup, restarts the managed service, performs a health check, and rolls back managed runtime files if verification fails.

In 2.0, `menu_app/` is part of the managed runtime. If `Mac MCP.app` is already installed, a successful update rebuilds and refreshes it automatically.

## macOS permissions

Grant only the permissions required by the tools you use:

- **Accessibility** for `mac_observe`, `mac_act`, System Events, and desktop automation;
- **Screen Recording** for protected screen capture;
- **Automation** when macOS asks permission to control Safari, Chrome, System Events, Reminders, or other apps;
- **Microphone** for `ask_user_voice`.
