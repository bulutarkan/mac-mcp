# Chrome Web Store: plan for the Mac MCP Chrome Companion (#188)

Today the Chrome companion is loaded by hand (Developer mode → Load unpacked). A Web Store listing would make it one click and keep it updated. This page records what that takes; nothing here is published yet, and the store account and review are the owner's.

## What blocks a straight upload

The extension reads its connection details (the server's port and its owner-only token) from `bridge_config.js`, a file Mac MCP writes into the extension's own folder on install (`mcp_server/chrome_background_bridge.py`, `ensure_chrome_companion_config`). An extension installed from the Web Store lives in Chrome's profile, where Mac MCP cannot write. A store build therefore needs another way to get the port and token:

1. **Pairing on localhost (recommended).** The extension asks `http://127.0.0.1:<port>` for a pairing request; the Mac MCP menu app shows "Chrome wants to connect" with Allow, and only then the server hands the extension a token, stored with `chrome.storage.local`. The port can be the default with a short list of fallbacks. Nothing new is installed outside Chrome, and the owner sees every pairing.
2. **Native messaging.** Mac MCP installs a native-messaging host manifest in `~/Library/Application Support/Google/Chrome/NativeMessagingHosts`, and the extension asks the host for the token. This works without a prompt, but adds the `nativeMessaging` permission (deliberately absent today, and pinned absent by `tests/test_chrome_background_transport.py`) and a host program to keep signed.

Both keep the current WebSocket bridge and its loopback/origin/token checks unchanged.

## What the review will look at

- **Permissions:** `debugger` (page actions and capture in background tabs) and host access to all `http`/`https` pages. Both are broad; the listing needs a clear single purpose ("lets your local Mac MCP work in Chrome tabs in the background") and a justification for each. Expect a longer, manual review.
- **Data use:** the companion sends page content only to the local Mac MCP server on 127.0.0.1 and nowhere else; the listing's privacy disclosures and a privacy policy URL say so.
- **Account:** a Chrome Web Store developer account (one-time registration fee) under the owner's name.

## Steps when the owner decides to go ahead

1. Implement pairing (option 1) behind the existing bridge, with tests for refusal, expiry and replay.
2. Build a store package from `menu_app/ChromeVisualCompanion` without `bridge_config.js`.
3. Write the listing: purpose, permission justifications, privacy disclosures and policy, screenshots.
4. Submit; keep Load unpacked documented until the listing is live.
5. Point Settings → Browser → Chrome Setup… at the listing once it is live.

Until then, the setup is as easy as it can be by hand: the folder path is on the clipboard, and Settings and the tour show the companion as Connected on their own.
