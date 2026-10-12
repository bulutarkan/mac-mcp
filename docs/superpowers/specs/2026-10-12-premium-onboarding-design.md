# Premium onboarding — design

Date: 2026-10-12 · Status: draft · Part 2 (part 1:
`2026-10-12-server-permission-identity-design.md`; part 3, a Remotion product
video, is a later, separate project)

## 1. Goal

A first-run experience that introduces Mac MCP like a premium Mac app and takes
a new owner, especially one who installed the `.pkg` and never opened Terminal,
from "what is this?" to a first AI request that worked: a short animated
product tour, then setup (server, permissions as "Mac MCP", connecting an AI
app, a first request), with color and motion. Owner's words: "premium",
"ürün tanıtımı", "animasyon, renk", "sadece native Swift olmasın", slides with
screenshots, permissions asked as Mac MCP.

Success: a fresh package install opens the tour automatically, every step
shows live state from the server, a first request is detected and celebrated,
and a returning owner (setup already complete) never sees it unless they open
it.

## 2. Approaches considered

1. **Web UI in a native window (chosen).** A `WKWebView` window in the menu
   app renders bundled HTML/CSS/JS. Full freedom for gradients, motion,
   illustrations and video; the native side keeps all privileged actions
   behind a small allowlisted bridge.
2. Pure SwiftUI with custom animation: rejected by the owner ("not only native
   Swift") and slower to iterate on visuals.
3. Served by the MCP server at `/onboarding`: the server is not running on the
   very first screen, and a page served over HTTP widens the attack surface.

## 3. Experience

One window, 960×640, transparent title bar, no resize; arrow keys and
Return/Escape navigate; a progress rail of dots; "Skip tour" always visible.
Dark-first premium look (deep graphite → indigo/teal gradient, soft glass
cards, the app icon's colors as accent), light-mode variant via
`prefers-color-scheme`, every motion disabled by `prefers-reduced-motion`.

**Tour (4 slides, auto-advance pauses on hover)**
1. *Welcome* — the icon assembles from light; headline "Your AI, at the
   controls of your Mac." One-line promise; slot for a product video
   (part 3) that falls back to the animated hero.
2. *Works in your apps* — an animated mock Mac: the Mac MCP graphite cursor
   (the same arrow as the browser companion) glides to Finder, Notes, Mail;
   captions explain native app control.
3. *Browses in the background* — a mock Safari/Chrome tab with the companion
   frame and action label; "fills forms, reads pages, without taking over your
   screen".
4. *Safe by design* — local-only by default, permission profiles, approvals,
   run evidence; real screenshots from `assets/screenshots` in a tilted
   carousel.

**Setup (live, each step completes itself from real state)**
5. *Start Mac MCP* — starts the server if it is not running; an animated
   status ring turns green.
6. *Permissions* — Accessibility and Screen Recording, one card each: why it
   is needed, "Allow" asks through the server so the macOS prompt says
   **Mac MCP** (part 1), then opens the right System Settings pane with a
   short visual hint; the card flips to "Allowed" when the server sees it, and
   offers "Restart Mac MCP" when Screen Recording needs it. "Later" is allowed.
7. *Connect your AI* — cards for Claude Code, Codex, OpenCode, Claude Desktop
   and ChatGPT. Picking one shows the exact setup from `mac-mcp connect-config`
   with a Copy button and numbered steps; ChatGPT explains the public HTTPS
   requirement and links to Connections instead of turning anything on. A
   pulsing "Waiting for your AI app…" turns into "Connected" when the first
   call arrives.
8. *First request* — the read-only prompt with Copy; waits for the first
   successful call and celebrates (confetti, reduced-motion aware).
9. *You're set* — what to try next (three example requests), where Settings
   and the dashboard are, and "Start at login is on".

**When it shows:** automatically once, on the first launch of the menu app
when the Get Started checklist is incomplete and the tour was never finished
or skipped (`settings.onboarding.tour` = `completed` | `skipped`). Always
available from the menu ("Take the tour") and Settings › Get Started ("Open
the tour"). The Get Started checklist (#59) stays as the compact form.

## 4. Architecture

| Unit | Responsibility |
|---|---|
| `menu_app/Onboarding/` (`index.html`, `onboarding.css`, `onboarding.js`, `steps.js`, `assets/`) | the web UI; no network, no third-party code |
| `menu_app/Onboarding/steps.js` | pure state machine: steps, which are done given a status object, next/back; unit-tested with `node --test` |
| `menu_app/Sources/OnboardingWindowController.swift` | the window and `WKWebView` (loads the bundled file URL, CSP `default-src 'self'`, no navigation away) |
| `menu_app/Sources/OnboardingBridge.swift` | Foundation-only parser for messages from the page → a closed set of actions; compiled and tested on its own |
| `AppState` | performs actions and returns status JSON |
| `build_app.sh` | copies `menu_app/Onboarding` into `Contents/Resources/Onboarding`, links WebKit |

**Bridge (page → app), allowlist only:** `status`, `startServer`,
`restartServer`, `requestPermission(accessibility|screen_recording)`,
`openSystemSettings(accessibility|screen_recording)`,
`connectConfig(<known client>)`, `copy(text ≤ 4 KB)`,
`openSettings(<known section>)`, `finish(completed|skipped)`. Anything else is
ignored and logged. The app answers by calling `window.macmcp.receive(json)`.
No secrets cross the bridge (connect-config output already uses placeholders
or environment variables).

**Status object** (from the existing `/dashboard/api/onboarding`, extended
with `permission_holder`: "Mac MCP" | "Python" | "Terminal" from part 1):
server running, first call/success, accessibility, screen recording, public
endpoint mode.

## 5. Error handling

Server not running → setup steps show the start card, the rest wait. Status
fetch fails → cards show "Checking…" then a retry. Permission prompt refused →
card explains how to allow it later in System Settings. Web content fails to
load → the window shows a native fallback with a link to Settings › Get
Started.

## 6. Testing

- `node --test menu_app/Onboarding/tests/` for `steps.js` (run from a Python
  unittest, skipped when Node is absent).
- Swift harness for `OnboardingBridge` (allowlist, size limits, unknown
  actions dropped).
- Source tests: build script bundles the folder and links WebKit; CSP present;
  no remote URLs in the bundle.
- Rendered check: open the window, screenshot every slide and step in dark
  mode, verify text fits at 960×640.

## 7. Out of scope

The Remotion video (part 3); localization (English first, Turkish later);
analytics.
