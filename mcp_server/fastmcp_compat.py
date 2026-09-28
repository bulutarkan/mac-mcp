from __future__ import annotations

import mcp.server.fastmcp.server as fastmcp_server


def ensure_fastmcp_settings_model_complete() -> bool:
    """Resolve FastMCP Settings forward refs after the module has fully loaded."""
    settings = getattr(fastmcp_server, "Settings", None)
    if settings is None:
        return False
    if getattr(settings, "__pydantic_complete__", False):
        return True
    rebuild = getattr(settings, "model_rebuild", None)
    if rebuild is None:
        return False
    rebuild(_types_namespace=vars(fastmcp_server), raise_errors=True)
    return bool(getattr(settings, "__pydantic_complete__", False))
