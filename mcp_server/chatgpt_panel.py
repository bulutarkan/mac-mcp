"""ChatGPT MCP Apps sidebar UI. Reuses Mac MCP's policy-enforced tool transport."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
import base64

from mcp.types import Icon
from typing import Any

from .chatgpt_client_gate import CHATGPT_PANEL_LEGACY_URIS, CHATGPT_PANEL_RESOURCE_URI
from .provider_usage import summary as provider_usage_summary
from .policy import (
    current_policy_context, declared_risk, evaluate_tool_scope, tool_availability,
)
from .runtime_settings import (
    load_runtime_settings_state,
    provider_enabled,
    server_setting,
    update_runtime_setting,
)
from .tools_agents import agent_catalog, list_agents
from .version import __version__

PANEL_URI = CHATGPT_PANEL_RESOURCE_URI
_ICON_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' width='20' height='20' "
             "viewBox='0 0 20 20' fill='none' stroke='currentColor' stroke-width='1.5' "
             "stroke-linecap='round' stroke-linejoin='round'>"
             "<rect x='1.75' y='1.75' width='16.5' height='16.5' rx='5'/>"
             "<path d='M6.6 6.4 10 10l-3.4 3.6'/><path d='M11.4 13.4h2.4'/></svg>")
PANEL_ICON = Icon(
    src="data:image/svg+xml;base64," + base64.b64encode(_ICON_SVG.encode()).decode(),
    mimeType="image/svg+xml", sizes=["20x20"],
)
PANEL_HTML = Path(__file__).resolve().parent / "chatgpt_ui" / "control-center.html"
PANEL_PROVIDERS = ("codex", "opencode", "chatgpt")
_MODEL_LIMIT = 600


def _text(value: Any, limit: int) -> str | None:
    return (value.strip()[:limit] or None) if isinstance(value, str) else None


def _settings_view(state: Any, policy: Any) -> dict[str, Any]:
    """Non-secret settings projection: never URLs with credentials, paths or keys."""
    data = state.data if state.ok else {}
    subagents = data.get("subagents") if isinstance(data.get("subagents"), dict) else {}
    preset = subagents.get("default") if isinstance(subagents.get("default"), dict) else {}
    activity = data.get("tool_activity") if isinstance(data.get("tool_activity"), dict) else {}
    notifications = data.get("notifications") if isinstance(data.get("notifications"), dict) else {}
    public_url = server_setting("public_url", "")
    host = urlsplit(public_url).hostname if isinstance(public_url, str) and public_url else None
    provider = _text(preset.get("provider"), 32)
    return {
        "default_agent": {
            "provider": provider.lower() if provider else None,
            "model": _text(preset.get("model"), 120),
            "reasoning": _text(preset.get("reasoning"), 24),
        },
        "enabled_providers": [name for name in PANEL_PROVIDERS if provider_enabled(name)],
        # Owned by the native app (macOS permission + in-memory state): read-only here.
        "notifications": {
            "agent_completion": notifications.get("agent_completion") is True,
            "activity_bubble": activity.get("show_bubble") is True and activity.get("require_descriptions") is True,
        },
        "connection": {
            "profile": str(policy.profile or "unknown")[:32],
            "endpoint_mode": _text(server_setting("public_endpoint_mode", None), 24) or "local",
            "public_host": host[:120] if host else None,
        },
    }


def panel_model_catalog(settings: Any, provider: str, query: str | None = None) -> dict[str, Any]:
    """Bounded model catalog for one enabled provider, discovered from the provider itself."""
    provider = str(provider or "").strip().lower()
    if provider not in PANEL_PROVIDERS or not provider_enabled(provider):
        raise ValueError("Provider is not enabled")
    data = agent_catalog(settings, provider=provider, model_filter=query, limit=_MODEL_LIMIT).get("providers", {}).get(provider) or {}
    items = data.get("model_items") or [{"id": model} for model in data.get("models") or []]
    models = []
    for item in items[:_MODEL_LIMIT]:
        model_id = _text(item.get("id"), 120)
        if not model_id:
            continue
        entry: dict[str, Any] = {"id": model_id, "reasoning": [str(r)[:24] for r in item.get("reasoning_values") or []][:12]}
        label = _text(item.get("display_name"), 120)
        if label and label != model_id:
            entry["label"] = label
        if item.get("default_reasoning"):
            entry["default_reasoning"] = str(item["default_reasoning"])[:24]
        models.append(entry)
    return {
        "ok": True,
        "provider": provider,
        "available": bool(data.get("available")),
        "models": models,
        "truncated": bool(data.get("models_truncated")),
        "total": int(data.get("model_count") or len(models)),
        "reasoning": [str(r)[:24] for r in data.get("reasoning_values") or []][:12],
    }


def panel_snapshot(telemetry: Any, settings: Any) -> dict[str, Any]:
    """Return a deliberately bounded projection; never expose tokens or agent prompts."""
    stats = telemetry.summary(24)
    agents = list_agents(settings, limit=20).get("agents", [])
    provider_data = provider_usage_summary(days=7).get("providers", {})
    providers = [
        {
            "name": name,
            "turns": int(row.get("turns") or 0),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "total_tokens": row.get("total_tokens"),
        }
        for name in ("codex", "opencode", "chatgpt")
        for row in [provider_data.get(name, {})]
    ]
    agent_items = [
        {
            "id": str(item.get("agent_id") or "")[:16],
            "status": str(item.get("status") or "unknown")[:24],
            "provider": str(item.get("provider") or "unknown")[:32],
            "model": str(item.get("model") or "unspecified")[:80],
            "role": str(item.get("role") or "Agent")[:48],
        }
        for item in agents[:20]
    ]
    state = load_runtime_settings_state()
    policy = current_policy_context()
    setting_allowed = (
        tool_availability(policy.profile, "mac_mcp_panel_setting")["available"]
        and evaluate_tool_scope(
            policy.scope, "mac_mcp_panel_setting", {},
            effective_risk=declared_risk("mac_mcp_panel_setting"),
        ).allowed
    )
    return {
        "ok": True,
        "version": __version__,
        "stats": {
            "calls": int(stats.get("total_calls") or 0),
            "errors": int(stats.get("error_calls") or 0),
            "latency_ms": int(stats.get("avg_duration_ms") or 0),
            "uptime_seconds": int(stats.get("uptime_seconds") or 0),
            "active_calls": len(telemetry.active_calls()),
        },
        "agents": agent_items,
        "active_agents": sum(a["status"] in {"running", "starting"} for a in agent_items),
        "providers": providers,
        "settings": _settings_view(state, policy),
        "settings_writable": (
            state.status in {"ok", "missing"}
            and setting_allowed
        ),
    }


def save_panel_preference(name: str, value: Any, settings: Any = None) -> dict[str, Any]:
    """Only the default delegated agent is writable; security settings stay native-only."""
    if name != "default_agent":
        raise ValueError("Unsupported panel preference")
    if not isinstance(value, dict) or not set(value) <= {"provider", "model", "reasoning"}:
        raise ValueError("default_agent takes only provider, model and reasoning")
    if any(v is not None and not isinstance(v, str) for v in value.values()):
        raise ValueError("default_agent fields must be strings")
    provider = (value.get("provider") or "").strip().lower()
    model = (value.get("model") or "").strip() or None
    reasoning = (value.get("reasoning") or "").strip().lower() or None
    if provider not in PANEL_PROVIDERS or not provider_enabled(provider):
        raise ValueError("Choose an enabled provider")
    if model or reasoning:
        # Filter by the exact id so large catalogs (e.g. OpenCode) are not truncated away.
        catalog = panel_model_catalog(settings, provider, query=model)
        allowed = catalog["reasoning"]
        if model:
            entry = next((m for m in catalog["models"] if m["id"] == model), None)
            if entry is None:
                raise ValueError(f"Model is not in the {provider} catalog")
            allowed = entry["reasoning"]
        if reasoning and reasoning not in allowed:
            raise ValueError("Reasoning level is not supported for this model")
    preset = {"provider": provider, **({"model": model} if model else {}), **({"reasoning": reasoning} if reasoning else {})}
    update_runtime_setting("subagents", "default", preset)
    return {"ok": True, "name": name, "value": preset}


def register_chatgpt_panel(mcp: Any, telemetry: Any, settings: Any) -> None:
    """A regular MCP resource + tool metadata; no SDK 2.x dependency required."""
    def mac_mcp_panel_ui() -> str:
        return PANEL_HTML.read_text(encoding="utf-8")

    for uri in (PANEL_URI, *sorted(CHATGPT_PANEL_LEGACY_URIS)):
        mcp.resource(
            uri,
            name="Mac MCP",
            mime_type="text/html;profile=mcp-app",
            meta={"openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen", "inline"]}},
        )(mac_mcp_panel_ui)

    @mcp.tool(
        name="open_mac_mcp_panel",
        title="Mac MCP",
        icons=[PANEL_ICON],
        description="Open the Mac MCP to view Mac status, agents, usage and preferences.",
        meta={"ui": {"resourceUri": PANEL_URI}, "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}},
    )
    async def open_mac_mcp_panel() -> dict[str, Any]:
        return panel_snapshot(telemetry, settings)

    @mcp.tool(
        name="mac_mcp_panel_state",
        description=(
            "Read bounded Mac MCP status, delegated agents and token usage. "
            "With models_for=<provider>, return only that enabled provider's discovered model catalog."
        ),
    )
    async def mac_mcp_panel_state(models_for: str | None = None) -> dict[str, Any]:
        if models_for:
            return panel_model_catalog(settings, models_for)
        return panel_snapshot(telemetry, settings)

    @mcp.tool(
        name="mac_mcp_panel_setting",
        description=(
            "Change only the Control Center's allowlisted preference: name='default_agent' with "
            "value={provider, model?, reasoning?}, validated against enabled providers and their discovered catalogs."
        ),
    )
    async def mac_mcp_panel_setting(name: str, value: dict[str, Any]) -> dict[str, Any]:
        return save_panel_preference(name, value, settings)
