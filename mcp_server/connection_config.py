from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


SUPPORTED_CLIENTS = ("chatgpt", "codex", "opencode")
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
) -> RenderedConnectionConfig:
    selected = str(client or "").strip().lower()
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
