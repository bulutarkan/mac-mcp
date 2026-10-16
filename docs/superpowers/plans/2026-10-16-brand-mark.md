# Brand Mark Rollout: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every Mac MCP logo with the approved monogram. A thin line draws the M, and an upright modern pointer carries on its right leg.

**Architecture:**
- One set of hand-authored SVG sources lives in `assets/brand/`.
- `scripts/build_brand.py` renders every PNG the surfaces need into their existing locations; `--check` keeps them in sync.
- The native app gains three image resources (header mark in two tones, menu bar template), and its menu bar label shows state through alpha and a dot.
- Web surfaces inline the mark, or reference it.
- The demo video is re-rendered.

**Tech Stack:**
- SVG;
- Python standard library, plus `rsvg-convert` and `magick` (already on the build Mac) for rendering;
- SwiftUI/AppKit;
- the Remotion project in `video/`.

**Spec:** `docs/superpowers/specs/2026-10-16-brand-mark-design.md`.

## Global Constraints

- **Mark geometry (64-unit grid):**
  - M: `M14 49 V15 L32 33 L50 15 V29`;
  - pointer: `M3.4 3.2 23 11.2 14.6 14.4 11.3 23.2Z`, with `translate(50 33.2) rotate(22) scale(0.8) translate(-3.4 -3.2)`;
  - round caps and joins.
- **Line weight:**
  - 4.2 at ≥ 128 px;
  - 4.6 at 64 px;
  - 5.0 at 32 px;
  - 5.6 at ≤ 20 px and in the menu bar.
- **Colours:**
  - champagne `#f1e7d2 → #b9a98a` on dark backgrounds;
  - bronze `#9c8a66 → #6f5f42` on light backgrounds;
  - obsidian tile `#34363d → #111216` for the app icon.
- **SVG sources:** no `<script>`, no external `href`, no `<image>`, no fonts.
- **Generated PNGs** are committed. `build_brand.py --check` must pass in the test suite.
- **Menu bar states:** stopped is 40 % alpha; running is full; working adds a dot that the existing `pulse` turns from filled to hollow. The accessibility label is unchanged.
- **Installation:** the native app is installed **from the source repo** only, then checked for running the new binary (memory rule).
- **Out of scope:**
  - tarkan.cloud;
  - a release;
  - new typography;
  - layout changes.
- **Tests run with:** `MCP_API_KEY=test-key-0123456789abcdef0123456789 MCP_ALLOW_NO_AUTH=false MCP_ALLOW_SHELL=true MAC_MCP_SKIP_MENU_APP_INSTALL=1 ~/mac-mcp/.venv/bin/python -m unittest <module>`.

## Review Focus

1. **Menu bar in Light and Dark Mode, and with the menu open (highlighted):** a template image must flip colour by itself. A coloured or non-template PNG would vanish on one of them. Test in Task 2 (the PNG is black and alpha only; the Swift code sets `isTemplate`).
2. **Menu header in Light Mode:** the champagne mark is near-invisible on a light popover, so the bronze mark is used there. Test in Task 2 (source pins the `colorScheme` switch).
3. **App icon on the Dock and in Finder at 16 and 32 px:** the tile has alpha outside the rounded square. `iconutil` builds every size, so no white corners appear. Test in Task 1 (the 1024 PNG has alpha, and its corner pixel is transparent).
4. **Cached old favicon or OG image on the live site:** browsers and social sites cache them. New file names (or a version query) make the change visible. Test in Task 4 (the site references the new names).
5. **A video re-render on a machine without the right fonts:** a font fallback would change the captions. The export script checks the budgets, and I compare the poster frame with the old one. Done in Task 6.

---

### Task 1: Brand sources and the renderer

**Create:**
- `assets/brand/mark.svg`;
- `assets/brand/mark-bronze.svg`;
- `assets/brand/mark-mono.svg`;
- `assets/brand/app-icon.svg`;
- `scripts/build_brand.py`;
- `tests/test_brand_assets.py`.

**`build_brand.py` outputs, rendered with `rsvg-convert`:**

| File | What it is |
|---|---|
| `menu_app/Assets/AppIcon.png` | 1024, from `app-icon.svg` |
| `menu_app/Assets/BrandMark.png` | 64 px, champagne, weight 5.0 |
| `menu_app/Assets/BrandMarkLight.png` | 64 px, bronze, weight 5.0 |
| `menu_app/Assets/MenuBarMark.png` | 18 px, mono black, weight 5.6 |
| `menu_app/Assets/MenuBarMark@2x.png` | 36 px, mono black, weight 5.6 |
| `site/assets/img/app-icon-256.png` | app icon at 256 |
| `site/assets/img/apple-touch-icon.png` | 180, app icon |
| `site/assets/img/mark.svg` | copy of `mark.svg` |
| `video/public/app-icon.png` | app icon at 512 |
| `mcp_server/mobile/icon-192.png`, `mcp_server/mobile/icon-512.png` | app icon |
| `assets/brand/social-preview.png` | 1280×640 |
| `site/assets/img/og-image.jpg` | 1200×630; headline "Let your AI apps use your Mac." and the line "Mac MCP · Open source, MIT · bulutarkan.github.io/mac-mcp" rendered from an SVG `<text>` with a system serif |

`--check` renders each output to a temporary directory and compares the pixels with `magick compare -metric AE`, which must be 0. The OG image is compared with a small tolerance, since it is a JPEG.

**Tests (`test_brand_assets.py`):**
- **SVG hygiene:** they parse; no script, external `href`, `<image>` or `font`.
- **Sizes and alpha:** each PNG has its stated size and alpha. The app icon's corner pixel (0,0) is transparent and its centre is opaque.
- **Template:** the menu bar PNGs contain only black (RGB 0) with varying alpha.
- **In sync:** `build_brand.py --check` exits 0.

**Steps:**
1. Write the tests, run them, and see them fail.
2. Write the SVGs and the script; run it.
3. Run the tests and see them pass.
4. Commit: `Brand: monogram sources and renderer`.

### Task 2: Native app (menu bar, header, Live Island, app icon)

**Modify:**
- `menu_app/build_app.sh`: copy `BrandMark.png`, `BrandMarkLight.png`, `MenuBarMark.png` and `MenuBarMark@2x.png` into `Resources/Assets`.
- `menu_app/Sources/MacMCPMenuApp.swift`: the menu bar label becomes `Image(nsImage: MenuBarMark.image(for: state))`.
  - A small `MenuBarMark` enum loads the template PNG pair into one `NSImage` with `isTemplate = true`, and draws the variants once.
  - Stopped: drawn at 40 % alpha.
  - Running: the mark.
  - Working: the mark plus a dot at the top right, filled or hollow by `state.pulse`.
- `menu_app/Sources/MenuBarView.swift` header:
  - the bare mark at 30 pt;
  - `BrandMark` in Dark Mode and `BrandMarkLight` in Light Mode, through `@Environment(\.colorScheme)`;
  - no clip shape.
- `menu_app/Sources/LiveIsland.swift` `IslandMark`: load `BrandMark.png`. The paused and private states are unchanged.

**Tests:**
- Update `tests/test_menu_status_surfaces.py` and `tests/test_native_reduce_motion.py`: the old `server.rack` / `cpu` pins become pins on `MenuBarMark.image(for:` and on the `pulse` driving the dot.
- Add pins:
  - `isTemplate = true`;
  - `colorScheme == .dark ? "BrandMark" : "BrandMarkLight"`;
  - `BrandMark` in `LiveIsland.swift`;
  - `build_app.sh` copies the four files.
- `tests/test_app_branding.py` keeps passing (icon path unchanged).

**Build:** `menu_app/build_app.sh /tmp/mac-mcp-brand-test`, then `codesign --verify --deep --strict`.

**Commit:** `Brand: menu bar mark with states, header and island mark, new app icon`.

### Task 3: Tour

**Modify:** `menu_app/Onboarding/index.html`.
- The welcome and closing hero images (`hero-icon`) use `../Assets/BrandMark.png`; the tour is dark.
- The mock macOS rows (Settings list, notification) keep `../Assets/AppIcon.png`.
- Update `onboarding.css` only if the bare mark needs a size or shadow change; `.hero-icon` now carries no tile.

**Tests:**
- The onboarding source pins the hero to `BrandMark.png`.
- `node --test menu_app/Onboarding/tests/` passes.

**Commit:** `Brand: the tour shows the monogram`.

### Task 4: Site and social images

**Modify `site/index.html`:**
- `<link rel="icon" type="image/svg+xml" href="assets/img/mark.svg">`, plus the PNG fallback and the `apple-touch-icon`;
- the brand and the island use `assets/img/mark.svg`.

Commit the rendered `og-image.jpg` and `social-preview.png`.

**Tests:**
- `tests/test_site.py` asserts the favicon, `apple-touch-icon` and brand references.
- `build_site.py --offline --out <tmp>` and `--check` pass, after updating the first-load size budget if needed.

**Commit:** `Brand: site favicon, brand and social images`.

### Task 5: Mobile, dashboard and ChatGPT panel

**Modify:**
- `mcp_server/mobile/index.html`: the hexagon SVG becomes the inline mark (`currentColor`, sized by the existing CSS).
- `mcp_server/mobile/manifest.json`: an `icons` entry for 192 and 512, plus an `apple-touch-icon` link in `index.html`.
- `mcp_server/dashboard/index.html`: the hexagon becomes the mark.
- `mcp_server/chatgpt_ui/control-center.html` (and its source under `chatgpt_ui/`, if it is built): the terminal logo becomes the mark.
- Make sure the server serves the new mobile icons (route or static mount), with a test.

**Tests:**
- Source pins.
- The mobile route serves `icon-192.png` with `image/png`.

**Commit:** `Brand: monogram on the mobile page, dashboard and ChatGPT panel`.

### Task 6: Video and README demo

**Modify:** `video/src/components.tsx`: `AppIcon` drops the extra `borderRadius`, since the new icon has its own tile and alpha.

**Render:**
- `cd video && npm ci && npm run render`;
- then the export script, which writes `site/assets/media/mac-mcp-demo.mp4` and `demo-poster.jpg` within their budgets.

**Check:**
- Look at the poster and at 3 frames (the scenes with the icon).
- `video/README.md` stays accurate.

**Commit:** `Brand: re-rendered demo video and poster`.

### Task 7: Verify, install, publish, review

**Verify:**
- full suite;
- `git diff --check`;
- tour tests;
- native build and `codesign`.

**Install:** from the source repo, then confirm the running binary is the new one by inode.

**Live captures:**
- menu bar: light and dark, stopped not forced in production, running, and working (a short read-only agent);
- the menu header in dark and light;
- the Live Island;
- the Dock and the Finder icon.

**Publish:**
- **Runtime:** promote the changed `mcp_server` static files (mobile, dashboard, chatgpt_ui) after checking that runtime equals the previous commit, plus `menu_app` so the runtime build matches.
- **Pages:** push (deploys Pages for `site/**`), then check that the live site serves the new favicon, mark and OG image.

**Docs:**
- CHANGELOG entry;
- roadmap #187 and #204 notes;
- tell the owner to upload `social-preview.png` in repo settings.

**Review:** a fresh whole-branch review on the most capable model, then one fix pass.
