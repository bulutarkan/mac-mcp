# Product site, README front door and demo video (#57, #101) — design

Agreed with the owner on 2026-10-13. Hosting: GitHub Pages at `https://bulutarkan.github.io/mac-mcp/`. English only. A 40-second Remotion video. The site goes live with 2.2.0, the first release that contains Scheduled Tasks and the live island.

## Promise

**Let your AI apps use your Mac.** Mac MCP is a local server and menu bar app that lets Claude, ChatGPT, Codex and other MCP apps work in your Mac's apps, files, terminal and browser, with approvals you control.

Three proof points, the same on the site, in the README and in the video:

1. **It works inside your apps.** Native apps through macOS Accessibility, plus files and the terminal.
2. **It browses where you can see it.** Visible, non-focus-stealing browser automation in Safari and Chrome: it works in a tab while you keep using the Mac.
3. **It hands off work and reports back.** Codex, Claude Code and OpenCode agents with a report of what they did; Scheduled Tasks (experimental) run AI tasks and scripts on a schedule.

## Page sections

1. Header: icon, name, How it works · Safety · Install, GitHub.
2. Hero: promise, subhead, Download Mac MCP {version} (exact asset URL), View on GitHub, "Apple Silicon · macOS 13+ · MIT · {release_id}", the demo video (poster, no autoplay, captions, transcript), a small CSS notch-island motif.
3. Works with: Claude Desktop, Claude Code, ChatGPT, Codex, OpenCode and other MCP clients (text, no vendor logos).
4. Three proof points.
5. Workflows: ChatGPT from your phone (needs a public endpoint such as Cloudflare Tunnel); a Claude Code dev loop; a Codex delegated run with its report; a weekday scheduled summary (experimental; tasks an AI app suggests wait for your approval).
6. You stay in charge: Server Approval and permission profiles (an allowed capability is not an approval prompt); Pause observation, private mode, never-observe apps and sites; the live island is off by default; updates install only signed stable checkpoints. "What Mac MCP is not": not a sandbox, it acts with your macOS user's permissions; localhost is machine-local transport, not same-user isolation; what your AI app reads is sent to that app's provider; browser work is visible; the package is not notarized yet, so macOS asks you to choose Open Anyway. Links: SECURITY.md, docs/TERMINOLOGY.md, docs/LOCAL_API_SECURITY.md.
7. Install: package (version, size, exact URL, SHA-256 published with the GitHub release and a `shasum -a 256` check, Open Anyway steps); terminal installer (`curl … install.sh | bash`, installs the newest cryptographically verified stable release checkpoint, Apple Silicon or Intel); from source (README). Requirements. On a phone: "Open this page on your Mac to install."
8. Footer: GitHub, Releases, Changelog, Security, MIT license, "Built from {release_id}", not affiliated with Apple, Anthropic or OpenAI.

Never on the page: "never leaves your Mac", sandbox claims, hidden or invisible browsing, pricing, Pro, competitor comparisons, or a signed claim for the package hash (the hash is published with the release; the source checkpoint is what is signed).

## Release data

`scripts/build_site.py` renders `site/` into `_site/` at deploy time from the latest GitHub release and its `.sha256` asset, after checking the tag, the release ID, the signed manifest at that tag, the asset name and that GitHub's digest equals the published hash. Releases older than 2.2.0 are not deployed, so the page never shows features that are not in the downloadable release. `--offline` builds a local preview that cannot be deployed.

## Video storyboard (MacMcpDemo, 1920×1080, 30 fps, 40 s, captions, no voice)

| Time | Scene | Caption |
|---|---|---|
| 0–4 s | A chat asks for a Mac task; the AI can't do it | Your AI can think. It can't use your Mac. |
| 4–9 s | One-click Connect list | Connect the AI apps you already use. |
| 9–16 s | The island beside the notch; the slot shows Finder, Terminal, Safari in a spinning ring, then a tick; "optional, off by default" | See what it's doing. |
| 16–23 s | Safari working in a background tab with the companion frame while the user types in a front window | Visible, non-focus-stealing browser work. |
| 23–30 s | Scheduled task "Every weekday at 08:30" → run report (Result, Steps); "Experimental" | Hand off work. Get a report back. |
| 30–36 s | Server Approval sheet, then Pause observation and Private mode | You stay in charge. |
| 36–40 s | Icon and URL | Let your AI apps use your Mac. Open source, MIT. |

The UI is redrawn from the app's own designs with neutral glyphs; every on-screen UI string exists in the app.

## README front door

`# Mac MCP` (no version), the poster linked to the demo, the promise and who it is for, the three proof points, website and demo links, quick install, a short "You stay in charge" paragraph, then What's new, a Workflows section (the ChatGPT section moves here) and the existing manual.

## Budgets

HTML ≤ 30 KB, CSS ≤ 25 KB, JS ≤ 3 KB, first load without video ≤ 350 KB, video ≤ 4 MB, whole `site/` ≤ 6 MB (every tracked file ships in every install).
