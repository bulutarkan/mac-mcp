# Mac MCP

**Let your AI apps use your Mac.** Mac MCP is a local server and menu bar app that lets Claude, ChatGPT, Codex, OpenCode and other MCP apps work in your Mac's apps, files, terminal and browser, with approvals you control.

```bash
npx @bulutarkan/mac-mcp install
```

This runs the same installer as the [GitHub repository](https://github.com/bulutarkan/mac-mcp): it verifies the signed stable release before installing, never overwrites an existing install, settings or keys, and asks before optional extras. Installing the npm package itself changes nothing on your Mac; only `install` does.

After that, use the `mac-mcp` command (or the same commands through npx):

```bash
npx @bulutarkan/mac-mcp status
npx @bulutarkan/mac-mcp update
```

Requires macOS 13 or later (Apple Silicon or Intel). Prefer a package? Download the `.pkg` from the [latest release](https://github.com/bulutarkan/mac-mcp/releases/latest). Documentation, security notes and the menu bar app: [github.com/bulutarkan/mac-mcp](https://github.com/bulutarkan/mac-mcp).
