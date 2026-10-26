from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlsplit


SUPPORTED_CLIENTS = ("chatgpt", "codex", "opencode", "claude-code", "other")
# Clients set up another way; they get guidance, not a snippet.
UNGUIDED_CLIENTS = {
    "claude-desktop": (
        "Claude Desktop is set up in one step instead: mac-mcp connect --client claude-desktop "
        "(or Mac MCP Settings > Connect). It adds {\"mcpServers\": {\"mac-mcp\": {\"command\": "
        "\"/Users/YOU/.local/bin/mac-mcp\", \"args\": [\"stdio\"]}}} to claude_desktop_config.json: no address and "
        "no key in Claude's config, the key is read from Mac MCP's .env each time Claude starts it, and the "
        "session is set up again after a server restart or an idle timeout. Restart Claude Desktop afterwards."
    ),
}
# Apps `mac-mcp connect` sets up in one step; connect-config points there instead of a snippet.
ONE_STEP_CLIENTS = {
    "kimi": "Kimi Code", "grok": "Grok Build", "antigravity": "Antigravity", "pi": "pi", "cline": "Cline",
    "kilo": "Kilo Code", "goose": "Goose", "copilot": "GitHub Copilot",
}


def guidance(client: str) -> str | None:
    """What connect-config says instead of a snippet for an app set up another way, else None."""
    if client in UNGUIDED_CLIENTS:
        return UNGUIDED_CLIENTS[client]
    if client in ONE_STEP_CLIENTS:
        return (
            f"{ONE_STEP_CLIENTS[client]} is set up in one step instead: mac-mcp connect --client {client}. "
            "It adds Mac MCP as a command, `mac-mcp stdio`, so no address or key goes into its config. "
            "To set it up by hand: mac-mcp connect-config --client other"
        )
    return None


SUPPORTED_ENDPOINTS = ("auto", "local", "public")
DEFAULT_SERVER_NAME = "mac-mcp"
DEFAULT_AUTH_ENV = "MAC_MCP_API_KEY"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConnectionConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedConnectionConfig:
    client: str
    target: str
    snippet: str
    secret_instruction: str | None
    verify_instruction: str | None = None


def validate_server_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or not _NAME_RE.fullmatch(name):
        raise ConnectionConfigError(
            "server name must contain only letters, numbers, underscores, or hyphens"
        )
    return name


def validate_auth_env(value: str) -> str:
    name = str(value or "").strip()
    if not name or not _ENV_RE.fullmatch(name):
        raise ConnectionConfigError("auth env name must be a valid environment variable")
    return name


def validate_endpoint_url(value: str) -> str:
    endpoint = str(value or "").strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectionConfigError("endpoint must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ConnectionConfigError("endpoint must not contain query, fragment, or userinfo")
    if not parsed.path.endswith("/mcp"):
        raise ConnectionConfigError("endpoint must end with /mcp")
    return endpoint


def _chatgpt_url(endpoint: str, auth_required: bool) -> str:
    if not endpoint.startswith("https://"):
        raise ConnectionConfigError(
            "ChatGPT requires a remote HTTPS MCP endpoint; configure a public endpoint first"
        )
    if not auth_required:
        return endpoint
    return endpoint + "?ApiKey=<API_KEY>"


def render_connection_config(
    *,
    client: str,
    endpoint_url: str,
    auth_required: bool,
    server_name: str = DEFAULT_SERVER_NAME,
    auth_env: str = DEFAULT_AUTH_ENV,
    bridge_command: list[str] | None = None,
) -> RenderedConnectionConfig:
    selected = str(client or "").strip().lower()
    if guidance(selected):
        raise ConnectionConfigError(guidance(selected))
    if selected not in SUPPORTED_CLIENTS:
        raise ConnectionConfigError(f"unsupported client: {selected or '<empty>'}")

    endpoint = validate_endpoint_url(endpoint_url)
    name = validate_server_name(server_name)
    env_name = validate_auth_env(auth_env)

    if selected == "codex":
        lines = [
            f"[mcp_servers.{name}]",
            f"url = {json.dumps(endpoint)}",
        ]
        secret_instruction = None
        if auth_required:
            lines.append(f"bearer_token_env_var = {json.dumps(env_name)}")
            secret_instruction = (
                f"Set {env_name} in the Codex process environment to the same secret "
                "value configured as Mac MCP's MCP_API_KEY."
            )
        return RenderedConnectionConfig(
            client=selected,
            target="Codex CLI config.toml (mcp_servers.* HTTP format)",
            snippet="\n".join(lines),
            secret_instruction=secret_instruction,
        )

    if selected == "opencode":
        server: dict[str, object] = {
            "type": "remote",
            "url": endpoint,
            "enabled": True,
        }
        secret_instruction = None
        if auth_required:
            server["oauth"] = False
            server["headers"] = {
                "Authorization": f"Bearer {{env:{env_name}}}",
            }
            secret_instruction = (
                f"Set {env_name} in the OpenCode environment to the same secret "
                "value configured as Mac MCP's MCP_API_KEY."
            )
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {name: server},
        }
        return RenderedConnectionConfig(
            client=selected,
            target="OpenCode 1.x opencode.json/opencode.jsonc remote MCP format",
            snippet=json.dumps(payload, indent=2, sort_keys=False),
            secret_instruction=secret_instruction,
        )

    if selected == "claude-code":
        server = {"type": "http", "url": endpoint}
        secret_instruction = None
        if auth_required:
            # Claude Code stores ${VAR} literally and expands it from its own environment
            # when it connects, so the key never lands in ~/.claude.json.
            server["headers"] = {"Authorization": f"Bearer ${{{env_name}}}"}
            secret_instruction = (
                f"Set {env_name} in the environment Claude Code starts from (for example your "
                "shell profile) to the same secret value configured as Mac MCP's MCP_API_KEY. "
                "Keep the single quotes: they stop the shell from expanding the variable."
            )
        payload = json.dumps(server, separators=(",", ":"))
        return RenderedConnectionConfig(
            client=selected,
            target="Claude Code CLI command (claude mcp add-json, user scope, HTTP transport)",
            snippet=f"claude mcp add-json --scope user {name} {shlex.quote(payload)}",
            secret_instruction=secret_instruction,
            verify_instruction=f"Run: claude mcp get {name}  (expect Status: Connected)",
        )

    if selected == "other":
        return _render_other(endpoint, auth_required, name, bridge_command)

    chatgpt_url = _chatgpt_url(endpoint, auth_required)
    secret_instruction = None
    if auth_required:
        secret_instruction = (
            "Replace <API_KEY> in the URL inside ChatGPT with the configured "
            "Mac MCP MCP_API_KEY value. The real key is intentionally not printed here."
        )
    return RenderedConnectionConfig(
        client=selected,
        target="ChatGPT remote MCP app/connector URL field",
        snippet=chatgpt_url,
        secret_instruction=secret_instruction,
    )


def _render_other(endpoint: str, auth_required: bool, name: str, bridge_command: list[str] | None) -> RenderedConnectionConfig:
    """Any other MCP app: the command form most apps take, and the URL form for those that only take a URL."""
    command = list(bridge_command or ["/Users/YOU/.local/bin/mac-mcp", "stdio"])
    payload = {"mcpServers": {name: {"command": command[0], "args": command[1:]}}}
    url_form = f"Apps that only take a URL: use {endpoint} (Streamable HTTP)"
    if auth_required:
        url_form += (
            " with the header Authorization: Bearer <API_KEY>, where <API_KEY> is Mac MCP's MCP_API_KEY. "
            "mac-mcp connect --client other --copy-key puts the key on the clipboard without printing it."
        )
    else:
        url_form += "; this Mac does not ask for a key."
    return RenderedConnectionConfig(
        client="other",
        target="your AI app's MCP settings (mcpServers JSON, or a command with its arguments)",
        snippet=json.dumps(payload, indent=2),
        secret_instruction=(
            "The command form needs no key: `mac-mcp stdio` adds it from Mac MCP's own settings. "
            + url_form
        ),
        verify_instruction="Restart the app, then ask it something about your Mac.",
    )
