from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from mcp.types import ToolAnnotations

from .policy_scope import (
    AccessMode, ResourceScope, ScopeDecision, ScopeRequest,
    access_mode_allows, evaluate_scope, normalize_access_mode,
)


class Capability(str, Enum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    PROCESS_CONTROL = "process_control"
    UI_ACTION = "ui_action"
    BROWSER_CONTROL = "browser_control"
    NATIVE_ACCESSIBILITY = "native_accessibility"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    NETWORK_ACCESS = "network_access"
    RAW_EXECUTION = "raw_execution"
    UPDATE_CONTROL = "update_control"
    AGENT_DELEGATION = "agent_delegation"
    HUMAN_INTERACTION = "human_interaction"


@dataclass(frozen=True)
class RiskOverride:
    capabilities: Optional[frozenset[Capability]] = None
    destructive: Optional[bool] = None
    sensitive: Optional[bool] = None
    requested_access_mode: Optional[AccessMode] = None


RiskResolver = Callable[[Mapping[str, Any]], RiskOverride]


@dataclass(frozen=True)
class RiskEntry:
    tool: str
    family: str
    capabilities: frozenset[Capability]
    destructive: bool = False
    sensitive: bool = False
    resolver: Optional[RiskResolver] = None


@dataclass(frozen=True)
class RiskAssessment:
    tool: str
    family: str
    capabilities: frozenset[Capability]
    destructive: bool
    sensitive: bool
    requested_access_mode: Optional[AccessMode] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "family": self.family,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "destructive": self.destructive,
            "sensitive": self.sensitive,
            "requested_access_mode": (
                self.requested_access_mode.value if self.requested_access_mode is not None else None
            ),
        }


class ApprovalSource(str, Enum):
    CLIENT = "client"
    SERVER = "server"
    EXTERNAL = "external"
    NONE = "none"


@dataclass(frozen=True)
class ApprovalBehavior:
    source: ApprovalSource
    automatic_confirmation: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "automatic_confirmation": self.automatic_confirmation,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class PermissionProfile:
    name: str
    allowed_capabilities: Optional[frozenset[Capability]]
    allow_destructive_families: Optional[frozenset[str]]
    access_mode_ceiling: AccessMode
    approval: ApprovalBehavior


@dataclass(frozen=True)
class PolicyContext:
    """Call identity container designed to grow into per-credential policy."""

    profile: str = "trusted"
    actor: str = "global"
    agent_id: Optional[str] = None
    team_id: Optional[str] = None
    resource: Optional[Mapping[str, Any]] = None
    scope: Optional[ResourceScope] = None
    lock: Optional[Mapping[str, Any]] = None

    def telemetry_fields(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "actor": self.actor,
            "agent_id": self.agent_id,
            "team_id": self.team_id,
            "resource": dict(self.resource) if self.resource is not None else None,
            "scope": self.scope.to_dict() if self.scope is not None else None,
            "lock": dict(self.lock) if self.lock is not None else None,
        }


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    profile: str
    code: str
    reason: str
    denied_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "profile": self.profile,
            "code": self.code,
            "reason": self.reason,
            "denied_capabilities": list(self.denied_capabilities),
        }


def _caps(*values: Capability) -> frozenset[Capability]:
    return frozenset(values)


def _http_risk(arguments: Mapping[str, Any]) -> RiskOverride:
    method = str(arguments.get("method") or "GET").strip().upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return RiskOverride(
            capabilities=_caps(Capability.READ, Capability.NETWORK_ACCESS),
            destructive=False,
        )
    return RiskOverride(
        capabilities=_caps(Capability.NETWORK_ACCESS, Capability.EXTERNAL_SIDE_EFFECT),
        destructive=method == "DELETE",
    )


def _tool_invoke_risk(arguments: Mapping[str, Any]) -> RiskOverride:
    target = str(arguments.get("tool_name") or "").strip()
    nested = arguments.get("arguments") if isinstance(arguments.get("arguments"), Mapping) else {}
    if not target or target in {"tool_invoke", "tool_discover"}:
        return RiskOverride(
            capabilities=_caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.UI_ACTION,
                               Capability.BROWSER_CONTROL, Capability.NATIVE_ACCESSIBILITY, Capability.EXTERNAL_SIDE_EFFECT,
                               Capability.NETWORK_ACCESS, Capability.RAW_EXECUTION, Capability.UPDATE_CONTROL,
                               Capability.AGENT_DELEGATION, Capability.HUMAN_INTERACTION),
            destructive=True, sensitive=True,
        )
    try:
        _, effective = resolve_risk(target, nested)
    except KeyError:
        return RiskOverride(capabilities=_caps(Capability.RAW_EXECUTION), destructive=True, sensitive=True)
    return RiskOverride(
        capabilities=effective.capabilities, destructive=effective.destructive, sensitive=effective.sensitive,
        requested_access_mode=effective.requested_access_mode,
    )


def _update_risk(arguments: Mapping[str, Any]) -> RiskOverride:
    check_only = arguments.get("check_only", True)
    if check_only is not False:
        return RiskOverride(
            capabilities=_caps(Capability.READ, Capability.LOCAL_WRITE, Capability.NETWORK_ACCESS),
            destructive=False,
            sensitive=False,
        )
    return RiskOverride(
        capabilities=_caps(
            Capability.READ,
            Capability.LOCAL_WRITE,
            Capability.PROCESS_CONTROL,
            Capability.NETWORK_ACCESS,
            Capability.UPDATE_CONTROL,
        ),
        destructive=True,
        sensitive=True,
    )


def _agent_spawn_risk(arguments: Mapping[str, Any]) -> RiskOverride:
    raw_mode = arguments.get("access_mode", AccessMode.WORKSPACE_WRITE.value)
    try:
        mode = normalize_access_mode(str(raw_mode))
    except ValueError:
        mode = AccessMode.FULL
    return RiskOverride(requested_access_mode=mode)


def _agent_action_risk(arguments: Mapping[str, Any]) -> RiskOverride:
    action = str(arguments.get("action") or "").strip().lower()
    if action in {"cancel", "despawn"}:
        return RiskOverride(
            capabilities=_caps(Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL),
            destructive=True,
        )
    return RiskOverride(
        capabilities=_caps(
            Capability.LOCAL_WRITE,
            Capability.PROCESS_CONTROL,
            Capability.AGENT_DELEGATION,
        ),
        destructive=False,
        requested_access_mode=AccessMode.FULL,
    )


def _r(
    tool: str,
    family: str,
    capabilities: frozenset[Capability],
    *,
    destructive: bool = False,
    sensitive: bool = False,
    resolver: Optional[RiskResolver] = None,
) -> RiskEntry:
    return RiskEntry(tool, family, capabilities, destructive, sensitive, resolver)


RISK_REGISTRY: dict[str, RiskEntry] = {
    # Terminal and background processes
    "run_command": _r("run_command", "terminal", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.UI_ACTION, Capability.EXTERNAL_SIDE_EFFECT, Capability.NETWORK_ACCESS, Capability.RAW_EXECUTION), destructive=True, sensitive=True),
    "process_list": _r("process_list", "terminal", _caps(Capability.READ), sensitive=True),
    "kill_process": _r("kill_process", "terminal", _caps(Capability.PROCESS_CONTROL), destructive=True),
    "get_system_info": _r("get_system_info", "terminal", _caps(Capability.READ), sensitive=True),
    "start_background_job": _r("start_background_job", "jobs", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.EXTERNAL_SIDE_EFFECT, Capability.NETWORK_ACCESS, Capability.RAW_EXECUTION), destructive=True, sensitive=True),
    "get_job_status": _r("get_job_status", "jobs", _caps(Capability.READ), sensitive=True),
    "get_job_output": _r("get_job_output", "jobs", _caps(Capability.READ), sensitive=True),
    "stop_job": _r("stop_job", "jobs", _caps(Capability.PROCESS_CONTROL), destructive=True),
    "list_jobs": _r("list_jobs", "jobs", _caps(Capability.READ), sensitive=True),
    "wait_jobs": _r("wait_jobs", "jobs", _caps(Capability.READ), sensitive=True),
    "run_commands_parallel": _r("run_commands_parallel", "jobs", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.EXTERNAL_SIDE_EFFECT, Capability.NETWORK_ACCESS, Capability.RAW_EXECUTION), destructive=True, sensitive=True),
    # Delegated agents
    "agent_catalog": _r("agent_catalog", "agents", _caps(Capability.READ, Capability.PROCESS_CONTROL), sensitive=True),
    "spawn_agent": _r("spawn_agent", "agents", _caps(Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.AGENT_DELEGATION), sensitive=True, resolver=_agent_spawn_risk),
    "spawn_agents": _r("spawn_agents", "agents", _caps(Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.AGENT_DELEGATION), sensitive=True, resolver=_agent_spawn_risk),
    "wait_agents": _r("wait_agents", "agents", _caps(Capability.READ), sensitive=True),
    "list_agents": _r("list_agents", "agents", _caps(Capability.READ), sensitive=True),
    "get_agent": _r("get_agent", "agents", _caps(Capability.READ), sensitive=True),
    "agent_action": _r("agent_action", "agents", _caps(Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.AGENT_DELEGATION), destructive=True, sensitive=True, resolver=_agent_action_risk),
    # Files
    "write_file": _r("write_file", "files", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "write_files_batch": _r("write_files_batch", "files", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "read_file": _r("read_file", "files", _caps(Capability.READ), sensitive=True),
    "read_multiple_files": _r("read_multiple_files", "files", _caps(Capability.READ), sensitive=True),
    "edit_file": _r("edit_file", "files", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "move_file": _r("move_file", "files", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "copy_file": _r("copy_file", "files", _caps(Capability.READ, Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "delete_path": _r("delete_path", "files", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "list_directory": _r("list_directory", "files", _caps(Capability.READ), sensitive=True),
    "directory_tree": _r("directory_tree", "files", _caps(Capability.READ), sensitive=True),
    "create_directory": _r("create_directory", "files", _caps(Capability.LOCAL_WRITE), sensitive=True),
    "get_file_info": _r("get_file_info", "files", _caps(Capability.READ), sensitive=True),
    "find_files": _r("find_files", "files", _caps(Capability.READ), sensitive=True),
    # Native macOS
    "run_applescript": _r("run_applescript", "macos", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.UI_ACTION, Capability.NATIVE_ACCESSIBILITY, Capability.EXTERNAL_SIDE_EFFECT, Capability.NETWORK_ACCESS, Capability.RAW_EXECUTION), destructive=True, sensitive=True),
    "send_notification": _r("send_notification", "macos", _caps(Capability.UI_ACTION, Capability.EXTERNAL_SIDE_EFFECT)),
    "clipboard_get": _r("clipboard_get", "macos", _caps(Capability.READ), sensitive=True),
    "clipboard_set": _r("clipboard_set", "macos", _caps(Capability.LOCAL_WRITE, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "open_app": _r("open_app", "macos", _caps(Capability.PROCESS_CONTROL, Capability.UI_ACTION)),
    "open_url": _r("open_url", "macos", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT)),
    "set_volume": _r("set_volume", "macos", _caps(Capability.UI_ACTION, Capability.EXTERNAL_SIDE_EFFECT)),
    "get_volume": _r("get_volume", "macos", _caps(Capability.READ)),
    "set_brightness": _r("set_brightness", "macos", _caps(Capability.UI_ACTION, Capability.EXTERNAL_SIDE_EFFECT)),
    "screenshot": _r("screenshot", "macos", _caps(Capability.READ, Capability.LOCAL_WRITE), sensitive=True),
    "set_reminder": _r("set_reminder", "macos", _caps(Capability.LOCAL_WRITE, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "get_running_apps": _r("get_running_apps", "macos", _caps(Capability.READ), sensitive=True),
    "mac_observe": _r("mac_observe", "accessibility", _caps(Capability.READ, Capability.NATIVE_ACCESSIBILITY), sensitive=True),
    "mac_act": _r("mac_act", "accessibility", _caps(Capability.UI_ACTION, Capability.NATIVE_ACCESSIBILITY, Capability.EXTERNAL_SIDE_EFFECT), destructive=True, sensitive=True),
    # Local search and HTTP
    "search_files": _r("search_files", "search", _caps(Capability.READ), sensitive=True),
    "spotlight_search": _r("spotlight_search", "search", _caps(Capability.READ), sensitive=True),
    "http_request": _r("http_request", "http", _caps(Capability.READ, Capability.NETWORK_ACCESS, Capability.EXTERNAL_SIDE_EFFECT), destructive=True, sensitive=True, resolver=_http_risk),
    # Browser control
    "browser_open_url": _r("browser_open_url", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT)),
    "browser_list_tabs": _r("browser_list_tabs", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_activate_tab": _r("browser_activate_tab", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL)),
    "browser_close_tab": _r("browser_close_tab", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL), destructive=True),
    "browser_observe": _r("browser_observe", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_find": _r("browser_find", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_act": _r("browser_act", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True, sensitive=True),
    "browser_do": _r("browser_do", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True, sensitive=True),
    "browser_execute_js": _r("browser_execute_js", "browser", _caps(Capability.READ, Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT, Capability.RAW_EXECUTION), destructive=True, sensitive=True),
    "browser_click_selector": _r("browser_click_selector", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True),
    "browser_type_selector": _r("browser_type_selector", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True, sensitive=True),
    "browser_wait_for_selector": _r("browser_wait_for_selector", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_get_html": _r("browser_get_html", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_wait_for_download": _r("browser_wait_for_download", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL, Capability.LOCAL_WRITE), sensitive=True),
    "browser_screenshot": _r("browser_screenshot", "browser", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.BROWSER_CONTROL), sensitive=True),
    "browser_scroll": _r("browser_scroll", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL)),
    "browser_press_key": _r("browser_press_key", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True),
    "browser_coordinate_click": _r("browser_coordinate_click", "browser", _caps(Capability.UI_ACTION, Capability.BROWSER_CONTROL, Capability.EXTERNAL_SIDE_EFFECT), destructive=True),
    "browser_get_snapshot": _r("browser_get_snapshot", "browser", _caps(Capability.READ, Capability.BROWSER_CONTROL), sensitive=True),
    # Update, memory, skills, and interaction
    "mac_mcp_update": _r("mac_mcp_update", "update", _caps(Capability.READ, Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.NETWORK_ACCESS, Capability.UPDATE_CONTROL), destructive=True, sensitive=True, resolver=_update_risk),
    "memory_add": _r("memory_add", "memory", _caps(Capability.LOCAL_WRITE), sensitive=True),
    "memory_search": _r("memory_search", "memory", _caps(Capability.READ), sensitive=True),
    "memory_get": _r("memory_get", "memory", _caps(Capability.READ), sensitive=True),
    "memory_update": _r("memory_update", "memory", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "memory_delete": _r("memory_delete", "memory", _caps(Capability.LOCAL_WRITE), destructive=True, sensitive=True),
    "skill_list": _r("skill_list", "skills", _caps(Capability.READ), sensitive=True),
    "skill_search": _r("skill_search", "skills", _caps(Capability.READ), sensitive=True),
    "skill_get": _r("skill_get", "skills", _caps(Capability.READ), sensitive=True),
    "skill_register": _r("skill_register", "skills", _caps(Capability.READ, Capability.LOCAL_WRITE), sensitive=True),
    "skill_update_index": _r("skill_update_index", "skills", _caps(Capability.READ, Capability.LOCAL_WRITE), sensitive=True),
    "ask_user": _r("ask_user", "interactive", _caps(Capability.UI_ACTION, Capability.HUMAN_INTERACTION, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "ask_user_voice": _r("ask_user_voice", "voice", _caps(Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.UI_ACTION, Capability.NETWORK_ACCESS, Capability.HUMAN_INTERACTION, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "ask_choice": _r("ask_choice", "interactive", _caps(Capability.UI_ACTION, Capability.HUMAN_INTERACTION, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "ask_confirmation": _r("ask_confirmation", "interactive", _caps(Capability.UI_ACTION, Capability.HUMAN_INTERACTION, Capability.EXTERNAL_SIDE_EFFECT), sensitive=True),
    "tool_discover": _r("tool_discover", "meta", _caps(Capability.READ)),
    "tool_invoke": _r("tool_invoke", "meta", _caps(Capability.RAW_EXECUTION), destructive=True, sensitive=True, resolver=_tool_invoke_risk),
}


PROFILES: dict[str, PermissionProfile] = {
    "trusted": PermissionProfile(
        name="trusted",
        allowed_capabilities=None,
        allow_destructive_families=None,
        access_mode_ceiling=AccessMode.FULL,
        approval=ApprovalBehavior(
            source=ApprovalSource.NONE,
            automatic_confirmation=False,
            summary="Allowed calls execute without an automatic Mac MCP confirmation prompt; the MCP client may still apply its own approval flow.",
        ),
    ),
    "standard": PermissionProfile(
        name="standard",
        allowed_capabilities=frozenset(
            capability for capability in Capability
            if capability not in {Capability.RAW_EXECUTION, Capability.UPDATE_CONTROL}
        ),
        allow_destructive_families=frozenset({"browser", "accessibility"}),
        access_mode_ceiling=AccessMode.READ_ONLY,
        approval=ApprovalBehavior(
            source=ApprovalSource.NONE,
            automatic_confirmation=False,
            summary="Allowed calls are capability-gated but do not trigger a second Mac MCP confirmation prompt; client-side approval is separate.",
        ),
    ),
    "read_only": PermissionProfile(
        name="read_only",
        allowed_capabilities=_caps(
            Capability.READ,
            Capability.NETWORK_ACCESS,
            Capability.BROWSER_CONTROL,
            Capability.NATIVE_ACCESSIBILITY,
        ),
        allow_destructive_families=frozenset(),
        access_mode_ceiling=AccessMode.READ_ONLY,
        approval=ApprovalBehavior(
            source=ApprovalSource.NONE,
            automatic_confirmation=False,
            summary="Mutating capabilities are denied by policy. Allowed read-only calls do not require a Mac MCP confirmation prompt.",
        ),
    ),
}


def declared_risk(tool: str) -> RiskAssessment:
    try:
        entry = RISK_REGISTRY[tool]
    except KeyError as exc:
        raise KeyError(f"MCP tool has no central risk entry: {tool}") from exc
    return RiskAssessment(
        tool=entry.tool,
        family=entry.family,
        capabilities=entry.capabilities,
        destructive=entry.destructive,
        sensitive=entry.sensitive,
    )


def resolve_risk(tool: str, arguments: Optional[Mapping[str, Any]] = None) -> tuple[RiskAssessment, RiskAssessment]:
    declared = declared_risk(tool)
    entry = RISK_REGISTRY[tool]
    if entry.resolver is None:
        return declared, declared
    override = entry.resolver(arguments or {})
    effective = replace(
        declared,
        capabilities=override.capabilities if override.capabilities is not None else declared.capabilities,
        destructive=override.destructive if override.destructive is not None else declared.destructive,
        sensitive=override.sensitive if override.sensitive is not None else declared.sensitive,
        requested_access_mode=override.requested_access_mode,
    )
    return declared, effective


def evaluate_profile(profile_name: str, risk: RiskAssessment) -> PolicyDecision:
    profile = PROFILES.get(profile_name)
    if profile is None:
        return PolicyDecision(False, profile_name, "profile_denied", "unknown_profile")
    denied_capabilities: tuple[str, ...] = ()
    if profile.allowed_capabilities is not None:
        denied_capabilities = tuple(sorted(
            capability.value for capability in risk.capabilities
            if capability not in profile.allowed_capabilities
        ))
    if denied_capabilities:
        return PolicyDecision(False, profile.name, "profile_denied", "capability_not_allowed", denied_capabilities)
    if risk.destructive and profile.allow_destructive_families is not None:
        if risk.family not in profile.allow_destructive_families:
            return PolicyDecision(False, profile.name, "profile_denied", "destructive_not_allowed")
    if risk.requested_access_mode is not None:
        if not access_mode_allows(profile.access_mode_ceiling, risk.requested_access_mode):
            return PolicyDecision(False, profile.name, "profile_denied", "access_mode_exceeds_profile")
    return PolicyDecision(True, profile.name, "profile_allowed", "allowed")


def permission_profile_name() -> str:
    return (os.getenv("MAC_MCP_PERMISSION_PROFILE", "trusted").strip().lower() or "trusted")


def permission_semantics(profile_name: Optional[str] = None) -> dict[str, Any]:
    """Describe capability enforcement and human-approval behavior separately.

    This is intentionally descriptive: it must never imply that an allowed tool
    call will produce a confirmation prompt. Approval sources are modeled now so
    a future client/server/external guard can be represented without changing the
    capability contract.
    """
    active_name = str(profile_name or permission_profile_name()).strip().lower() or "trusted"
    all_capabilities = frozenset(Capability)
    profiles: list[dict[str, Any]] = []
    for name, profile in PROFILES.items():
        allowed = all_capabilities if profile.allowed_capabilities is None else profile.allowed_capabilities
        denied = all_capabilities.difference(allowed)
        profiles.append({
            "name": name,
            "active": name == active_name,
            "capability_enforcement": "server",
            "allowed_capabilities": sorted(capability.value for capability in allowed),
            "denied_capabilities": sorted(capability.value for capability in denied),
            "destructive_families": (
                ["*"] if profile.allow_destructive_families is None
                else sorted(profile.allow_destructive_families)
            ),
            "access_mode_ceiling": profile.access_mode_ceiling.value,
            "approval_behavior": profile.approval.to_dict(),
        })
    return {
        "active_profile": active_name,
        "known_profile": active_name in PROFILES,
        "capability_enforcement": "Mac MCP server policy",
        "approval_contract": "separate_from_capability_enforcement",
        "ask_confirmation_is_automatic_gate": False,
        "supported_approval_sources": [source.value for source in ApprovalSource],
        "profiles": profiles,
    }


def environment_policy_context(*, actor: str = "global") -> PolicyContext:
    return PolicyContext(profile=permission_profile_name(), actor=actor)


_CURRENT_CONTEXT: contextvars.ContextVar[Optional[PolicyContext]] = contextvars.ContextVar(
    "mac_mcp_policy_context", default=None
)


def current_policy_context() -> PolicyContext:
    return _CURRENT_CONTEXT.get() or environment_policy_context()


def set_policy_context(context: PolicyContext) -> contextvars.Token[Optional[PolicyContext]]:
    return _CURRENT_CONTEXT.set(context)


def reset_policy_context(token: contextvars.Token[Optional[PolicyContext]]) -> None:
    _CURRENT_CONTEXT.reset(token)


def policy_metadata(
    context: PolicyContext,
    declared: RiskAssessment,
    effective: RiskAssessment,
    decision: PolicyDecision,
) -> dict[str, Any]:
    return {
        "declared_risk": declared.to_dict(),
        "effective_risk": effective.to_dict(),
        "policy_decision": decision.code,
        **context.telemetry_fields(),
    }


def profile_denied_result(
    tool: str,
    decision: PolicyDecision,
    declared: RiskAssessment,
    effective: RiskAssessment,
) -> dict[str, Any]:
    return {
        "ok": False,
        "denied": True,
        "error": "profile_denied",
        "tool": tool,
        "profile": decision.profile,
        "policy_decision": decision.code,
        "reason": decision.reason,
        "denied_capabilities": list(decision.denied_capabilities),
        "declared_risk": declared.to_dict(),
        "effective_risk": effective.to_dict(),
    }


def annotations_for_tool(tool: str) -> ToolAnnotations:
    risk = declared_risk(tool)
    mutating = {
        Capability.LOCAL_WRITE,
        Capability.PROCESS_CONTROL,
        Capability.UI_ACTION,
        Capability.EXTERNAL_SIDE_EFFECT,
        Capability.RAW_EXECUTION,
        Capability.UPDATE_CONTROL,
        Capability.AGENT_DELEGATION,
        Capability.HUMAN_INTERACTION,
    }
    read_only = not bool(risk.capabilities.intersection(mutating))
    open_world = bool(risk.capabilities.intersection({
        Capability.NETWORK_ACCESS,
        Capability.BROWSER_CONTROL,
        Capability.EXTERNAL_SIDE_EFFECT,
    }))
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=risk.destructive,
        idempotentHint=True if read_only else (False if risk.destructive else None),
        openWorldHint=open_world,
    )


def narrow_child_profile(parent_profile: str, access_mode: AccessMode | str) -> str:
    """Map child execution mode to a profile without ever elevating the parent."""

    mode = normalize_access_mode(access_mode)
    parent = str(parent_profile or "trusted").strip().lower()
    if parent == "read_only":
        return "read_only"
    if parent == "standard":
        return "read_only" if mode == AccessMode.READ_ONLY else "standard"
    if parent != "trusted":
        return "read_only"
    return {
        AccessMode.READ_ONLY: "read_only",
        AccessMode.WORKSPACE_WRITE: "standard",
        AccessMode.FULL: "trusted",
    }[mode]


def _scope_paths(tool: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key in ("path", "source", "destination", "cwd"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value)
    raw_paths = arguments.get("paths")
    if isinstance(raw_paths, (list, tuple)):
        paths.extend(str(value) for value in raw_paths if isinstance(value, str) and value.strip())
    raw_files = arguments.get("files")
    if isinstance(raw_files, (list, tuple)):
        for item in raw_files:
            if isinstance(item, Mapping):
                value = item.get("path")
                if isinstance(value, str) and value.strip():
                    paths.append(value)
    # browser_screenshot.path is intentionally included by the generic path key.
    return tuple(dict.fromkeys(paths))


def evaluate_tool_scope(
    scope: Optional[ResourceScope],
    tool: str,
    arguments: Mapping[str, Any],
    effective_risk: Optional[RiskAssessment] = None,
) -> ScopeDecision:
    if scope is None:
        return ScopeDecision(True)
    risk = effective_risk or resolve_risk(tool, arguments)[1]
    reasons: list[str] = []
    base = evaluate_scope(
        scope,
        ScopeRequest(tool_family=risk.family, access_mode=risk.requested_access_mode),
    )
    reasons.extend(base.reasons)

    for path in _scope_paths(tool, arguments):
        reasons.extend(evaluate_scope(scope, ScopeRequest(path=path)).reasons)

    if risk.family == "browser" and scope.browser_tabs is not None and "*" not in scope.browser_tabs:
        handles: list[str] = []
        handle = str(arguments.get("tab_handle") or "").strip()
        if handle:
            handles.append(handle)
        tab_handles = arguments.get("tab_handles")
        if isinstance(tab_handles, (list, tuple)):
            for value in tab_handles:
                candidate = str(value or "").strip()
                if candidate:
                    handles.append(candidate)
        handles = list(dict.fromkeys(handles))
        if tool == "browser_list_tabs":
            pass  # result is filtered after execution
        elif not handles:
            reasons.append("browser_tab_required")
        else:
            for selected_handle in handles:
                reasons.extend(evaluate_scope(scope, ScopeRequest(browser_tab=selected_handle)).reasons)

    if risk.family == "jobs" and scope.job_ids is not None and "*" not in scope.job_ids:
        job_id = str(arguments.get("job_id") or "").strip()
        job_ids = arguments.get("job_ids")
        if tool == "list_jobs":
            pass  # result is filtered after execution
        elif tool in {"start_background_job", "run_commands_parallel"} and not job_id:
            reasons.append("job_creation_not_allowed")
        elif job_id:
            reasons.extend(evaluate_scope(scope, ScopeRequest(job_id=job_id)).reasons)
        if isinstance(job_ids, (list, tuple)):
            for value in job_ids:
                if isinstance(value, str) and value.strip():
                    reasons.extend(evaluate_scope(scope, ScopeRequest(job_id=value.strip())).reasons)

    terminal_id = str(arguments.get("terminal_id") or "").strip()
    if terminal_id:
        reasons.extend(evaluate_scope(scope, ScopeRequest(terminal_id=terminal_id)).reasons)

    return ScopeDecision(not reasons, tuple(dict.fromkeys(reasons)))


def scope_denied_result(tool: str, decision: ScopeDecision, scope: ResourceScope) -> dict[str, Any]:
    return {
        "ok": False,
        "denied": True,
        "error": "scope_denied",
        "tool": tool,
        "policy_decision": "scope_denied",
        "reasons": list(decision.reasons),
        "scope": scope.to_dict(),
    }


def filter_scoped_result(
    scope: Optional[ResourceScope],
    tool: str,
    result: Any,
) -> Any:
    if scope is None or not isinstance(result, dict):
        return result
    if tool == "browser_list_tabs" and scope.browser_tabs is not None and "*" not in scope.browser_tabs:
        allowed = set(scope.browser_tabs)
        copied = dict(result)
        tabs = copied.get("tabs")
        if isinstance(tabs, list):
            copied["tabs"] = [item for item in tabs if isinstance(item, dict) and str(item.get("tab_handle")) in allowed]
            copied["count"] = len(copied["tabs"])
        return copied
    if tool == "list_jobs" and scope.job_ids is not None and "*" not in scope.job_ids:
        allowed = set(scope.job_ids)
        copied = dict(result)
        jobs = copied.get("jobs")
        if isinstance(jobs, list):
            copied["jobs"] = [item for item in jobs if isinstance(item, dict) and str(item.get("job_id")) in allowed]
            copied["count"] = len(copied["jobs"])
        return copied
    return result
