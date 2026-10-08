"""Optional ChatGPT-only MCP Apps discovery, separate from universal Mac MCP tools.

ClientInfo is a feature-routing hint, NOT an authenticated identity. Access control
continues to be enforced by the existing Mac MCP authentication/policy layer.
"""

from __future__ import annotations

import re
from typing import Any

from .runtime_settings import load_runtime_settings_state

CHATGPT_PANEL_TOOLS = frozenset({
    "open_mac_mcp_panel", "mac_mcp_panel_state", "mac_mcp_panel_setting",
})
CHATGPT_PANEL_RESOURCE_URI = "ui://mac-mcp/panel-v3.html"
# Hosts cache a tool's resourceUri, so earlier URIs stay readable (never listed)
# until every client has re-fetched tools/list.
CHATGPT_PANEL_LEGACY_URIS = frozenset({
    "ui://mac-mcp/panel-v2.html", "ui://mac-mcp/control-center",
})
CHATGPT_PANEL_RESOURCE_URIS = CHATGPT_PANEL_LEGACY_URIS | {CHATGPT_PANEL_RESOURCE_URI}


_OPENAI_MCP_NAME = re.compile(r"openai-mcp(?: \([a-z0-9._-]{1,32}\))?")


def chatgpt_extensions_enabled() -> bool:
    """Enabled for ChatGPT hosts by default; explicit false opts out.

    Bad/malformed settings always fail closed, without silently repairing them.
    """
    state = load_runtime_settings_state()
    if state.status not in {"ok", "missing"}:
        return False
    section = state.data.get("chatgpt_extensions", {})
    if not isinstance(section, dict):
        return False
    return section.get("enabled", True) is True


def is_chatgpt_client(server: Any) -> bool:
    """Use the MCP initialize clientInfo, not User-Agent or arbitrary tool args."""
    if not chatgpt_extensions_enabled():
        return False
    try:
        session = server.get_context().request_context.session
        params = session.client_params
        name = str(params.clientInfo.name).strip().lower() if params else ""
    except (LookupError, AttributeError, ValueError, TypeError):
        return False
    # ChatGPT's published MCP initializer has also used clientInfo=openai-mcp,
    # optionally suffixed with its runtime, e.g. "openai-mcp (codex)".
    # For that less-specific name, require explicit MCP Apps UI support.
    if _OPENAI_MCP_NAME.fullmatch(name):
        capabilities = params.capabilities
        extensions = capabilities.model_extra.get("extensions", {}) or {}
        ui = extensions.get("io.modelcontextprotocol/ui", {}) if isinstance(extensions, dict) else {}
        return (
            isinstance(ui, dict)
            and "text/html;profile=mcp-app" in (ui.get("mimeTypes") or [])
        )
    # Exact product-host labels, not arbitrary names containing "chatgpt".
    # In particular, chatgpt-web-cli is not a ChatGPT sidebar host.
    return name in {
        "chatgpt", "chatgpt desktop", "chatgpt-desktop", "chatgpt work",
        "chatgpt-work", "chatgpt mobile", "chatgpt-mobile", "chatgpt.com",
        "com.openai.chatgpt", "com.openai.chatgpt.desktop",
        "com.openai.chatgpt.ios", "com.openai.chatgpt.android",
    }
