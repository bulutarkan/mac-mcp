"""Mac MCP server package.

``mcp_server.app`` is built on first access, so importing a submodule (the
CLI, the crash supervisor) never constructs the whole server as a side effect.
"""
from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from .main import app

        return app
    raise AttributeError(f"module 'mcp_server' has no attribute {name!r}")
