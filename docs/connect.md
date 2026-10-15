# Connect your AI apps

How Claude, ChatGPT, Codex, OpenCode and other MCP apps reach Mac MCP: one-step setup, ready-made connection settings, and a public address for ChatGPT.

[← Back to the README](../README.md)

## Workflows

### Talk to ChatGPT. Let it work on your Mac.

ChatGPT is one of the AI apps you can use with Mac MCP, and its voice mode makes it an especially natural one. ChatGPT reaches your Mac through a public endpoint you turn on, such as a Cloudflare Tunnel (see [Public endpoint modes](#public-endpoint-modes)).

ChatGPT Live can use plugins during voice conversations. With Mac MCP connected, the same ChatGPT conversation you already use can become a control surface for your actual Mac. You can talk naturally while Mac MCP handles the execution: browsing in Safari or Chrome, working with files, opening apps, running commands, and coordinating agents.

Instead of moving every task into a separate coding-agent session, you can keep the interaction in ChatGPT and simply talk. From your phone, you can ask ChatGPT to work on a reachable Mac somewhere else. From your desktop, you can keep talking while Mac MCP works in the background without taking over the computer.

For example:

- "Check my Reddit replies, answer the important ones, and close the browser when you are done."
- "Go through the repo, run the tests, and tell me what is broken."
- "Find three hotels for next weekend, compare the reviews, and save the shortlist on my Mac."

**ChatGPT is the conversation. Mac MCP is the execution layer.**

#### The Mac MCP panel in ChatGPT

When Mac MCP is connected as a ChatGPT plugin, ChatGPT also shows Mac MCP as an app you can open next to your conversations: from the ChatGPT sidebar in full screen, or as a side panel inside any chat. The panel shows tool activity, delegated agents and token usage at a glance, and lets you choose the default agent ChatGPT delegates to. It refreshes only when you press refresh.

The panel is offered only to ChatGPT; other MCP clients keep their normal tool list. Everything it does goes through the same permission profiles and approvals as any other Mac MCP call, and it never receives API keys, tokens or credentials. To turn it off, set `{"chatgpt_extensions": {"enabled": false}}` in `~/.mac-mcp/settings.json`. See [docs/chatgpt-control-center.md](chatgpt-control-center.md) for details.

This is a powerful combination, not a Mac MCP-only voice feature. ChatGPT provides the natural voice interface and reasoning experience; Mac MCP gives it a local execution layer on macOS. Existing ChatGPT plugin permissions, approvals, and usage limits still apply, and Mac MCP keeps its normal permission, background-control, outcome-safety, and undo boundaries.

## Set up a connection

### Connect an AI app in one step

The tour's **Connect** step lists the AI apps on this Mac, each with a **Connect** button. The same setup is available from the CLI:

```bash
mac-mcp clients                          # which apps are on this Mac, and which already have Mac MCP
mac-mcp connect --client claude-desktop  # or claude-code, codex, opencode
mac-mcp connect --client chatgpt         # copies ChatGPT's connector address to the clipboard
```

`mac-mcp connect` adds Mac MCP to the app as a command-based server that runs `mac-mcp stdio`, replacing an earlier `mac-mcp` entry. The app's config holds no address and no key: `mac-mcp stdio` forwards MCP messages to the running server and adds the API key from Mac MCP's own `.env`. If the server restarts, the bridge sets up the session again on the new server. If the server is not running, a request waits up to 15 seconds and then returns a clear error. A request cut off mid-reply is never resent. Claude Code, Codex, Grok Build (`grok`), Antigravity (`agy`) and pi are set up through their own `mcp add` commands. For Claude Desktop, OpenCode, Kimi Code (`~/.kimi-code/mcp.json`), Cline (`~/.cline/data/settings/cline_mcp_settings.json`), Kilo Code (`~/.config/kilo/kilo.json`) and Goose (`~/.config/goose/config.yaml`, as a stdio extension), Mac MCP edits their config and leaves everything else in it unchanged, keeping the old file next to it as `*.before-mac-mcp`. If an OpenCode config contains comments, Mac MCP does not change it and points you to the manual setup. For ChatGPT, the address including its key goes straight to the clipboard and is never printed; ChatGPT still needs a public HTTPS endpoint.

### Generate a client connection config

For manual setup, use the CLI to generate a connection snippet from the Mac MCP endpoint and authentication state that are actually configured on this Mac:

```bash
mac-mcp connect-config --client chatgpt
mac-mcp connect-config --client codex
mac-mcp connect-config --client opencode
mac-mcp connect-config --client claude-code
```

The command never prints the configured API-key value. Codex, OpenCode and Claude Code snippets reference a client-side environment variable (`MAC_MCP_API_KEY` by default); set that variable in the client process to the same secret value configured as Mac MCP's `MCP_API_KEY`. ChatGPT uses the existing header-limited query-key compatibility form and prints only `?ApiKey=<API_KEY>` as a placeholder.

ChatGPT connects from OpenAI's servers, so it needs a public HTTPS endpoint; the installer's safe default, Local only, works only for AI apps on this Mac (Claude Code, Codex, OpenCode). On Local only, `--client chatgpt` explains this and lists the next steps instead of printing a URL ChatGPT cannot reach: choose Cloudflare, ngrok or Custom HTTPS in **Settings → Connections → Public Endpoint**, check it with `mac-mcp status`, then run the command again. Whenever a snippet points at the public endpoint, the command also checks that it answers and says so (`Public endpoint check: reachable` or `NOT reachable`). Public access is never switched on by these commands.

For Claude Code the output is a ready-to-run `claude mcp add-json --scope user …` command. Its JSON is single-quoted so the shell stores `${MAC_MCP_API_KEY}` literally; Claude Code expands it from its own environment each time it connects, so the key is never written to `~/.claude.json`. The output ends with a verify step: `claude mcp get mac-mcp` should report `Status: Connected` (an HTTP 401 means the variable is missing or wrong in Claude Code's environment). Claude Desktop and claude.ai are not guided yet: Mac MCP has not verified their connection format, so `--client claude-desktop` explains this instead of printing a snippet.

`--endpoint auto` is the default: it uses the configured public HTTPS endpoint for ChatGPT and loopback for local Codex, OpenCode and Claude Code. Use `--endpoint public` when Codex, OpenCode or Claude Code run elsewhere, or `--endpoint local` to force loopback where supported. `--name` changes the client-side server ID, and `--auth-env` changes the client environment-variable name without exposing its value.

Client config formats evolve. The generated output labels the target format it assumes; prefer regenerating the snippet with your installed Mac MCP instead of copying an old README fragment.

### Public endpoint modes

Mac MCP treats the local server and its public transport as separate layers. **Local only** exposes no managed public endpoint. **ngrok** runs a managed ngrok process and uses `NGROK_DOMAIN`. **Cloudflare Tunnel** runs `cloudflared` directly on the Mac and connects the selected named tunnel (or an owner-controlled token file) to `127.0.0.1:<port>`; no VPS, reverse proxy, inbound port-forward, or public Mac IP is required. **Custom HTTPS** records an externally managed HTTPS MCP endpoint and does not start a tunnel provider.

The native Settings window provides a four-way `Local only / ngrok / Cloudflare / Custom HTTPS` switch. The selection is persisted under `server.public_endpoint_mode` and `server.public_url` in `~/.mac-mcp/settings.json`; optional named-tunnel metadata may use the non-secret `server.cloudflare_tunnel` field. Existing installations that only have `ngrok_on_start=true` continue to behave as ngrok mode until the new setting is saved. Environment variables `MAC_MCP_PUBLIC_ENDPOINT_MODE` and `MAC_MCP_PUBLIC_URL` can override persisted values for managed/headless deployments.

### Cloudflare Tunnel: 5-minute setup

The interactive installer can do the Mac-side setup for you. Choose **Cloudflare Tunnel** when `install.sh` asks for a public endpoint. If `cloudflared` is missing, the installer offers `brew install cloudflared`; declining it does not break the core install and leaves Mac MCP in **Local only** mode.

For the Cloudflare-side setup:

1. Your domain must be active in Cloudflare. In the Cloudflare dashboard, go to **Networking → Tunnels**, choose **Create tunnel**, and give it any name that identifies this Mac.
2. Open the tunnel's **Routes** tab, choose **Add route → Published application**, select the hostname you want (for example `mac.example.com`), and point the service to `http://localhost:8000` for a default install. If you changed Mac MCP's server port, use that port instead.
3. Cloudflare shows a `cloudflared` setup/install command for the connector. **Do not run the service-install command when Mac MCP is managing the tunnel.** Copy only the tunnel token from that command. For an existing tunnel, **Add a replica** also exposes a connector command containing the token.
4. Back in the Mac MCP installer, enter the public hostname such as `https://mac.example.com` and paste the token into the hidden prompt. If you skip either value, the installer safely leaves the public mode as **Local only**; finish later in **Mac MCP.app → Settings → Advanced → Cloudflare**.
5. Start Mac MCP. `mac-mcp start` creates/enables a per-user `launchd` job with `KeepAlive`; no Terminal window has to remain open. Verify with `mac-mcp status` and `mac-mcp doctor`.

Cloudflare's current documentation calls this a remotely-managed tunnel and a **Published application** route. See [Set up Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/get-started/), [Add routes](https://developers.cloudflare.com/cloudflare-one/networks/routes/add-routes/), and [Tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/). A tunnel token is a credential: anyone who has it can run a connector for that tunnel, so rotate it in Cloudflare if it is ever exposed.

For the simplest Cloudflare setup, create the tunnel and hostname in Cloudflare, then paste the tunnel token once into **Settings → Advanced → Cloudflare**. Mac MCP writes it atomically to `~/.mac-mcp/cloudflare-tunnel-token` as an owner-only `0600` file, never writes the token into `settings.json` or `.env`, never re-displays it, and starts `cloudflared` with `--token-file` so the secret is not exposed in process arguments. `CLOUDFLARE_TUNNEL_TOKEN_FILE` may override the credential-file path without putting the token value in the environment. Named-tunnel credentials remain available as an advanced alternative. The tunnel connects Cloudflare directly to `http://127.0.0.1:<port>` on the Mac: no VPS, public IP, router port forwarding, or inbound firewall opening is required. Custom mode is for operators who already provide their own external HTTPS routing. All public URLs must be HTTPS and may not contain query strings, fragments, or URL userinfo. `mac-mcp status` shows the selected connector URL and `mac-mcp doctor` checks the selected provider, credential-file safety, and public `/health` route without sending the MCP API key. Switching providers stops any managed ngrok/cloudflared process that is no longer selected. In Cloudflare mode, `mac-mcp start` (and the native app Start action) installs/enables a user LaunchAgent with `KeepAlive`, so the tunnel stays independent of Terminal and is automatically restarted if `cloudflared` exits; `mac-mcp stop` boots out and disables that job.

`Authorization` remains authoritative when both forms are supplied. Empty, duplicate, or invalid `ApiKey` query credentials are rejected while authentication is enabled. The server removes `ApiKey` from its local access-log URL before logging, but upstream proxies/tunnels can still observe query strings, so Bearer headers should be preferred whenever the client supports them.
