# Browser work

How Mac MCP works in Safari and Chrome tabs without taking over your Mac, saved logins, privacy controls, the Visual Companion and Decision Acceleration.

[← Back to the README](../README.md)

## Browser automation that doesn't hijack your Mac

Mac MCP can inspect and interact with Safari and Chrome tabs in the background while you keep working in another app or browser tab. Here, **background** means a normal, visible Safari/Chrome tab that Mac MCP controls without bringing the browser or tab to the front; it is not a hidden/headless browser session.

- New browser tabs open in the background by default and return a stable `tab_handle`.
- Stable tab handles survive tab-index changes, so long-running tasks keep targeting the intended Safari or Chrome tab even as other tabs open, close, or move. Each AppleScript step re-resolves the tab by its native identity, so a shift in the middle of a `browser_act` call does not fail it; if the target tab itself closes, the call returns `tab_target_closed` with the actions that already completed and `automatic_retry: false` instead of an HTTP error.
- `browser_observe` can return compact DOM context plus viewport, element, or full-page visuals without activating the browser, switching tabs, scrolling the user's page, or leaving screenshot files on disk.
- High-level browser actions can target a specific background tab directly by handle, which makes parallel research and delegated-agent workflows practical without constant focus stealing.
- `browser_act` `type` replaces a field's content by default; `clear: false` appends to the end of a text input, textarea or rich-text editor (number, date and other non-text inputs refuse it with `append_unsupported`). Rich-text editors (ProseMirror, Lexical, Draft.js, Slate, Quill and similar) receive the text as a paste through their own input path, and a step is reported `value_applied` only when the editor took it: if a page draws pasted text without telling its framework, the step says `editor_model_unconfirmed` and asks for a fresh look instead of claiming success. A rich editor that draws the text at once is read again 0.2 s later, after it re-renders: if the text is gone (empty, or back to what was there before) the step fails with `editor_rerender_lost` and the batch stops before any submit; if the editor kept it in its own form (a list item, a link) the step says `editor_rerendered_text`. An editor that takes the input but never shows the text gets `editor_render_unconfirmed`: it may hold the text without drawing it, so the step stops without calling the input rejected and without suggesting a second paste that could enter it twice. Plain inputs and textareas get no extra read. Replacing only a selected part of the text is not supported: `type` removes all existing text even when only part of it is selected, so an action that asks for a partial edit (fields such as `selection`, `range`, `find`/`replace` or `selection_start`) is refused instead of replacing everything, and `clear` must be `true` or `false`. The same warning sits in the `clear` argument's description and the server instructions, so agents see it before they edit.
- **What an action actually did (`outcome`, `expect`).** Every `browser_act` action reports `outcome`: `confirmed` (its result was checked: typed text is in the editor, a select took the option, a post appeared, an `expect` was met), `page_reacted` (the page changed after the action, but nothing checked its intended result: a menu closing is not a link copied, a network request is not a submission), `unconfirmed`, `failed` or `blocked` (policy or the person took over). The response lists `unconfirmed_actions` with a hint. Give an action `expect` to check its result: `{text: 'Pending review'}`, `{gone: 'Choose an image'}`, `{selector: 'figure img'}` or `{url_change: true}` (`timeout_s`, default 5). An unmet `expect` stops the batch with `expectation_not_met`, `outcome_unknown: true` and no automatic retry, so a submission is never sent twice. Typed text is compared the way editors rewrite it (smart quotes, dashes, ellipses), so Medium's punctuation no longer reads as `input_not_applied`. Persistence after a reload, clipboard contents and server-side results are not checked automatically: reload or observe, or use `expect` on what the page shows.
- **Saved logins (`browser_sign_in`).** Agents can sign in to sites the owner has saved in **Settings → Permissions & Safety → Saved Logins** (or `mac-mcp logins add --site github.com --username ada@example.com` with the password on stdin). Mac MCP types the username and password straight into the page; the caller only learns which account was used and what was filled, so the password never reaches the model, its provider, Mac MCP telemetry, logs or screenshots. The login is chosen from the page's own site (a login for `github.com` is used on `github.com` only, never on look-alikes; its subdomains only when the owner turns that on for the login, and never on shared hosting such as `github.io` where anyone can own a subdomain; logins saved before this rule keep working on subdomains), only on HTTPS pages, and the page script re-checks the site right before filling. Passwords live in the owner's login keychain, written and read only through `/usr/bin/security` with the secret on stdin, so there is no macOS prompt per use; sites and usernames sit in an owner-only index. Apple does not let other apps read iCloud Passwords, so to reuse them export from the Passwords app (File → Export All Passwords), import the sites you choose in Settings (`mac-mcp logins import-preview` / `import`), and let Mac MCP delete the export file. Saving a login for a site is the authorization: signing in asks nothing further. Password fields are also never reported back by `browser_observe`, effect checks or `extract` (they show `[hidden]`).
- Foreground-only fallbacks such as native key presses, absolute coordinate clicks, foreground URL opens, and Safari's native file-picker path are **capability-gated**. A model cannot grant itself focus by sending `allow_foreground=true` or `background=false`.
- `browser_activate_tab` is treated as user-visible foreground behavior even when it would not raise the browser app, because changing Safari's current tab or Chrome's active tab can interrupt a user already working there. Normal automation should target stable `tab_handle` values directly without activating them.
- The native menu bar controller surfaces live browser work in **Sessions** and **Latest Tool Usage** with a privacy-minimized browser/site/action summary. URL paths, query strings, page titles, selectors, and page content are intentionally omitted from this compact view.
- **Settings → Privacy** is one place for what Mac MCP may look at and keep. **Pause observation** stops every tool that reads the screen, a web page, the clipboard or the microphone, plus new agents, until you turn it off; file and command tools keep working. **Private mode** keeps work going but stores nothing about it: no activity rows, usage, memories, lessons, run timelines or agent token usage (the security audit log still records tool names and outcomes). **Never observe these apps / sites** are never read, captured or acted in: native observation and actions in an excluded app are refused, a whole-screen screenshot is refused while an excluded app is on screen (or when the visible apps cannot be checked), an excluded site's tabs are listed without title or address and cannot be observed, opened or acted in, and `mac_snapshot` hides their titles. The server enforces all of this where each capture happens, so no tool argument bypasses it. The pane also lists every local store (what it holds, how long it is kept, size, owner-only mode) and every place data can leave the Mac (connected AI apps, the public tunnel, agent providers, voice transcription, decision acceleration) with whether it is on. Sensitive categories (password managers, banking and finance, health) are recognized by name and address; you can turn each into a never-observe rule (**Also never observe**), otherwise they only label. Recognition can miss, so your own exclusions are the reliable rule. Every tool result that read a page or an app carries `privacy_source` (the site or app, its category and the rule that allowed it). The More menu has **Pause Observation** and **Private Mode** for a single task, and the menu header shows Paused or Private. Each store can be moved to the Trash from the pane, or all at once (`mac-mcp privacy delete --store KEY | --all`, preview first; `--yes` stops Mac MCP, moves the files into one dated folder in the Trash and starts it again). Starting an agent returns a `data_notice` naming the provider that receives its task. Stored data: Mac MCP keeps every store owner-only (0700 directories, 0600 files and logs), shows whether FileVault encrypts the disk, and can keep its content stores out of Time Machine (**Keep Mac MCP data out of Time Machine**; settings, saved-login index, audit log and logs stay in backups). It adds no encryption layer of its own: a key this account's processes can read would not stop same-account malware, and losing it would lose the data.
- **Settings → Usage → Data & Retention** controls usage history: turn recording off, keep 30, 90 or 365 days (default 365), or clear all stored tool and provider usage. Only daily per-tool aggregates are kept, never prompts, arguments or results; a shorter period removes older days immediately.
- **Show Tab** is the explicit local-user action: only that trusted UI path receives a short lexical foreground capability and can bring that specific real Safari/Chrome tab to the front.

This is designed for workflows where an AI agent keeps working in one or more background browser tabs while the Mac remains usable normally.

### Reading a whole page

To read a page (an article, a product page with its reviews, docs, a thread), one call returns all of its text:

```json
{"browser":"Safari","url":"https://example.com/page","actions":[{"type":"read"}]}
```

`browser_do` (or `browser_act` on an open tab) first scrolls through the page so content that loads on scroll is there, then puts the page back where it was and reads the text the browser shows: no scripts, styles, templates or hidden content. While anything on the page is still loading (a skeleton or `aria-busy` placeholder) it moves one screen at a time and waits on a placeholder until it is replaced; across a page where nothing loads it takes bigger steps. Loading stops at the end of the page or after `timeout_s` (default 12). `loaded.still_loading` above zero means placeholders were left, so part of the page may be missing from the text. The answer is under `read`: `text`, `total_chars`, `headings`, `complete` and `loaded` (how far it scrolled, why it stopped and whether anything was still loading). A page longer than `max_chars` (default 40,000, at most 100,000) comes in parts: pass `next_offset` back as `offset`; the parts join into the whole text and never split a word. `load_more:false` reads the page as it is. Text inside cross-origin frames and closed shadow roots is not included.

### Bounded extraction and long-page scans

Read static rendered records with the existing `browser_act`, or the same action inside `browser_do`:

```json
{"type":"extract","fields":[{"name":"reviews","selector":"div.review","attr":"text","all":true,"max_items":100}],"max_chars":6000}
```

Each field requires a unique `name` and a CSS `selector` (or a `semantic` name). Missing fields and invalid CSS are errors; they never default to the page body. Text projection excludes scripts, styles, templates and hidden content, including hidden composed ancestors. Attribute projection still supports the existing `attr` contract; explicit `html` reads raw markup. `browser_do(extract=["price","rating"])` remains the semantic shortcut.

`matched_counts` counts selector matches; `fields[name]` reports `matched_count`, `eligible_count` (rendered/visible), `returned_count`, `has_more` and `continuation`. When partial, merge that field's `{offset,text_offset}` continuation into its next extraction spec **on the same unchanged document**. A long record is returned in exact text chunks; append chunks in order. `text_offset` is a browser UTF-16 offset, not a Python character count. `browser_do` preserves these counts and positions even when its 8 KB response budget cuts content. Extraction covers currently rendered matches, not the site's total review badge or unloaded pages.

For long or virtualized lists, one `browser_act` scan performs multiple bounded scroll/read steps internally:

```json
{"type":"scan","item_selector":".review","max_items":100,"max_chars":12000,"max_steps":50}
```

A partial scan returns `next_cursor`; pass it as `cursor` with the same selector, key field and tab. Inside `browser_do` the scan comes back under `scan` (with `items`, `next_cursor`, `complete` and `stopped`); it reads at most half of `browser_do`'s answer budget, so call it again with the cursor, or use `browser_act` for bigger pages. It drains every pending rendered row before scrolling, retains distinct identical rows, and splits long text into chunks carrying `record_id`, `text_offset` and `text_complete`. Group/append chunks by `record_id`. `key_field="href"` explicitly deduplicates by link; the default uses stable item IDs/data keys when available, otherwise DOM identity plus content. Remounts without stable identity may appear twice; this favors coverage over silently dropping distinct rows.

Cursor state is document-local, expires after five idle minutes, and is bounded to four sessions and 10,000 tracked records. Navigation, expiry, changed selector/container or intervening scroll rejects a cursor; observe before starting another scan. Item, character, byte, step, time and state limits return an explicit partial result. `no_usable_candidates` is not completion. `complete=true` means the observed list reached its end and produced no new rows across settled reads; it does not prove that a site's delayed loader, other tab or pagination has no more content. Check the reported scope/counts against any declared total. Three calls is a target for bounded static extraction, never a reason to omit the rest of a long page.

### Faster decisions on look-alike targets with the OpenAI Decisions API

Real pages are full of near-duplicates: a **Continue** button under both Shipping and Billing, **Reply** next to **Reply all**, two **25** cells in a two-month date picker. A typical agent stops there, observes the page again and spends another model round trip choosing. Mac MCP can settle it inside the same `browser_act` call instead.

When Decision Acceleration is on, Mac MCP sends the already-ranked candidates to the [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions), OpenAI's typed-answer endpoint for fast classification and routing (OpenAI describes it as about 10x faster than the Responses API). Mac MCP gives each call a 600 ms budget; a warm call measured under ~330 ms. The agent can add a one-line `intent` hint, such as "the Billing step", and that hint decided our calibration cases:

| Ambiguous target | Without hint | With `intent` |
|---|---|---|
| Shipping vs Billing **Continue** | 0.16, kept the first match | Billing, 0.83 |
| **Reply** vs **Reply all** | Reply | Reply all, 0.93 |
| Two **25** calendar cells | no answer, 0.28 | November 25, 0.82 |

The layer can only pick from candidates Mac MCP already found, never chooses a risky action such as delete, send or pay over the deterministic match, and falls back to the normal path on any timeout, error or low confidence. It is off by default, uses your own OpenAI key from Keychain, and sends only short redacted labels: no typed values, URLs or page content. Setup and thresholds are under [Optional Decision Acceleration](#optional-decision-acceleration-experimental).

### Browser Visual Companion (Safari + Chrome)

`Mac MCP.app` uses one shared WebExtension source for Safari and Chrome to make active Mac MCP browser work visible inside the exact page being automated. The extension is display-only: it renders a subtle pulsing page frame, a small `Mac MCP · …` activity badge, a synthetic cursor, and click feedback for high-level browser actions. Visual events contain only bounded action labels and viewport coordinates; typed text, selectors, URLs, page titles, DOM content, and secrets are not copied into the extension event. The overlay is activity feedback only and must not be treated as a security or trust indicator.

Safari setup depends on how `Mac MCP.app` is signed:

**Developer ID / Apple-signed build (persistent):**

1. Install or update Mac MCP normally.
2. Open **Mac MCP.app → Browser Activity → Enable in Safari…**. You can also use **Safari → Settings → Extensions**.
3. Turn on **Mac MCP Visual Companion** and grant website access for the sites where you want activity feedback.

**Local GitHub/source build (ad-hoc, development mode):**

1. Open **Mac MCP.app → Browser Activity → Developer Setup…**. Mac MCP reveals the runtime `BrowserVisualCompanion` source folder and opens Safari.
2. In Safari, enable web-developer features if the **Develop** menu is hidden.
3. Choose **Develop → Allow Unsigned Extensions**.
4. Choose **Develop → Add Temporary Extension…** and select `~/mac-mcp/menu_app/BrowserVisualCompanion`.
5. Grant website access when Safari asks. Safari treats this as a development/temporary extension; persistent normal installation requires an Apple-signed app bundle.

For Chrome, true background tab creation uses the Chrome-only `~/mac-mcp/menu_app/ChromeVisualCompanion` extension. Open **Settings → Connections → Browser → Chrome Setup…** (or **Set up** next to Chrome in the tour's Permissions step): Mac MCP opens `chrome://extensions`, shows the folder in Finder and copies its path. Turn on **Developer mode**, choose **Load unpacked**, press ⌘⇧G and ⌘V, then **Select**. The card turns to **Connected** with the companion's version within a few seconds; there is nothing to check by hand. When the loaded companion is older than the server (a feature it does not report yet), the card says so and **Reload in Chrome…** points at the reload arrow on `chrome://extensions`. `GET /dashboard/api/diagnostics/runtime` reports the same as `chrome_companion` (connected, version, connected_at, last_seen_at, missing_features, needs_reload). A Chrome Web Store listing would remove Developer mode; what it needs is in [chrome-web-store.md](chrome-web-store.md). The companion opens new tabs with Chrome's native `tabs.create({active:false})` API, so background work does not bring Chrome or the new tab to the front. If the companion is unavailable, background tab creation fails closed rather than falling back to a focus-stealing AppleScript open. The same companion also carries DOM/page execution through Chrome's `debugger` API, so normal Mac MCP Chrome reads, clicks, typing and background-safe visual capture do not require the Apple Events JavaScript toggle while the companion is connected. The Chrome companion uses a dedicated owner-only local credential and a loopback WebSocket; the credential is not the global `MCP_API_KEY`.

The project remains fully open source and does **not** need the Mac App Store. For a persistent GitHub release, sign/notarize the distributed `Mac MCP.app` with Developer ID; `menu_app/build_app.sh` accepts `MAC_MCP_CODESIGN_IDENTITY` for that release path.

No separate browser profile, helper daemon, or Xcode project is required. `menu_app/build_app.sh` compiles the `.appex` into `Mac MCP.app/Contents/PlugIns/` with the normal command-line Swift toolchain. Local builds default to ad-hoc signing; release builders can set `MAC_MCP_CODESIGN_IDENTITY` to use a Developer ID identity with hardened runtime/timestamp signing.

### Optional Decision Acceleration (experimental)

**Settings → Advanced → Decision Acceleration** has an opt-in OpenAI Decisions resolver. It is off by default. The OpenAI API key is stored in Keychain (`com.bulutarkan.mac-mcp` / `openai-decisions-api-key`) and never in `settings.json`; **Test** sends one tiny verification request.

When the switch is off, the key is missing, or OpenAI rejected the key, behavior is unchanged and no outbound call is made. When enabled with a working key:

- `browser_act` asks Decisions only when the deterministic ranking finds two or more near-equal targets (top scores ≥ 0.60 and within 0.10). Unique targets never trigger a call.
- `computer_plan` recovery asks Decisions only for an ambiguous browser/native rebind that would otherwise fail closed with `RECOVERY_AMBIGUOUS_TARGET`.
- `browser_act` actions accept an optional `intent` string (for example "the Billing step") that is added to the Decisions input only when the ranking is ambiguous. It is capped at 120 characters and secrets are redacted; without it the request is unchanged.
- Decisions may only pick one of the already-ranked candidate IDs. A choice is accepted at confidence ≥ 0.80, or at ≥ 0.65 when it agrees with the deterministic top match. A `none` answer, for example on a true tie, keeps the deterministic choice.
- A risky alternative (delete, send, pay, …) is never chosen over the deterministic match.
- Timeouts (600 ms default), errors, rate limits, invalid keys and low confidence all fall back to the existing deterministic path.
- Policy, approvals, leases, takeover checks and readiness still run after the choice and remain authoritative.
- Only short, redacted candidate labels, roles and the requested target text are sent. Typed values, URLs and page content are not sent.

`settings.json` → `decision_acceleration` also accepts `scope` (`browser`, `native`, `both`, `off`), `timeout_ms`, `accept_threshold`, `agree_threshold` and `max_candidates`; an invalid value disables the feature.

Default local endpoint:

```text
http://127.0.0.1:8000/mcp
```

A custom port can be supplied through `MAC_MCP_PORT` or CLI flags.
