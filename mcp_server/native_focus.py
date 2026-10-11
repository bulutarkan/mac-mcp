"""Native focus capture and restore for the layers below tools_ui.

artifact_pipeline (file dialogs) and tools_browser (foreground browser actions) need to
save and restore the frontmost app/window, which tools_ui implements. tools_ui imports
those layers, so they reach it through this leaf module instead of importing tools_ui:
tools_ui registers itself when it loads (the server always loads it). Each call looks
the function up on tools_ui at call time, so patches of tools_ui attributes apply.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any, Optional

_provider: Optional[ModuleType] = None


def provide(module: ModuleType) -> None:
    global _provider
    _provider = module


def _implementation(name: str) -> Any:
    if _provider is None:
        raise RuntimeError("Native focus helpers are not loaded (mcp_server.tools_ui).")
    return getattr(_provider, name)


def capture_focus_context(*args: Any, **kwargs: Any) -> Any:
    return _implementation("_capture_focus_context")(*args, **kwargs)


def restore_focus_context(*args: Any, **kwargs: Any) -> Any:
    return _implementation("_restore_focus_context")(*args, **kwargs)


def post_action_focus_decision(*args: Any, **kwargs: Any) -> Any:
    return _implementation("_post_action_focus_decision")(*args, **kwargs)
