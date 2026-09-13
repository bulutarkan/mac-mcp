from __future__ import annotations

import ipaddress
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from .policy import Capability, PolicyContext, RiskAssessment

UNTRUSTED_BROWSER_CONTENT_TOOLS = frozenset({
    "browser_observe", "browser_find", "browser_do", "browser_get_html",
    "browser_get_snapshot", "browser_execute_js", "browser_screenshot",
})
_SAFE_WHILE_WEB_SCOPED_FAMILIES = frozenset({"browser", "interactive", "voice"})
_PRIVILEGED_CAPABILITIES = frozenset({
    Capability.LOCAL_WRITE, Capability.PROCESS_CONTROL, Capability.UI_ACTION,
    Capability.NATIVE_ACCESSIBILITY, Capability.EXTERNAL_SIDE_EFFECT,
    Capability.RAW_EXECUTION, Capability.UPDATE_CONTROL, Capability.AGENT_DELEGATION,
})

@dataclass
class ExecutionSecurityState:
    key: str
    public_session_id: str
    web_scoped: bool = False
    current_origin: Optional[str] = None
    tab_handle: Optional[str] = None
    trust_level: str = "local"
    last_seen_at: float = field(default_factory=time.time)
    last_web_at: Optional[float] = None

@dataclass
class EscalationGrant:
    public_session_id: str
    tool: str
    origin: Optional[str]
    expires_at: float
    uses_remaining: int = 1

@dataclass(frozen=True)
class ContextGateDecision:
    allowed: bool
    code: str
    reason: str
    public_session_id: Optional[str] = None
    origin: Optional[str] = None
    trust_level: str = "local"
    escalated: bool = False

class SecurityContextManager:
    """In-memory web→host trust state keyed to a logical MCP/agent session."""

    def __init__(self, *, state_ttl_s: int = 1800, max_states: int = 512) -> None:
        self.state_ttl_s = max(60, int(state_ttl_s))
        self.max_states = max(32, int(max_states))
        self._lock = threading.RLock()
        self._states: dict[str, ExecutionSecurityState] = {}
        self._public_to_key: dict[str, str] = {}
        self._grants: dict[tuple[str, str], EscalationGrant] = {}

    @staticmethod
    def identity_key(policy_context: PolicyContext, steering_key: Optional[str]) -> str:
        if policy_context.agent_id:
            return f"agent:{policy_context.agent_id}"
        if steering_key:
            return f"session:{steering_key}"
        return f"actor:{policy_context.actor}:{policy_context.profile}"

    def _prune_locked(self, now: Optional[float] = None) -> None:
        current = time.time() if now is None else now
        expired = [key for key, state in self._states.items() if current - state.last_seen_at > self.state_ttl_s]
        for key in expired:
            state = self._states.pop(key, None)
            if state is not None:
                self._public_to_key.pop(state.public_session_id, None)
        for grant_key, grant in list(self._grants.items()):
            if grant.expires_at <= current or grant.uses_remaining <= 0:
                self._grants.pop(grant_key, None)
        if len(self._states) > self.max_states:
            ordered = sorted(self._states.values(), key=lambda item: item.last_seen_at)
            for state in ordered[: len(self._states) - self.max_states]:
                self._states.pop(state.key, None)
                self._public_to_key.pop(state.public_session_id, None)

    def touch(self, key: str, public_session_id: str) -> ExecutionSecurityState:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            state = self._states.get(key)
            if state is None:
                state = ExecutionSecurityState(key=key, public_session_id=public_session_id, last_seen_at=now)
                self._states[key] = state
            else:
                if state.public_session_id != public_session_id:
                    self._public_to_key.pop(state.public_session_id, None)
                state.public_session_id = public_session_id
                state.last_seen_at = now
            self._public_to_key[public_session_id] = key
            return state

    @staticmethod
    def _origin(url: Any) -> Optional[str]:
        text = str(url or "").strip()
        if not text:
            return None
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().rstrip(".")
        default_port = 443 if parsed.scheme == "https" else 80
        try:
            port = parsed.port
        except ValueError:
            return None
        suffix = f":{port}" if port and port != default_port else ""
        return f"{parsed.scheme}://{host}{suffix}"

    @staticmethod
    def _trust_for_origin(origin: Optional[str]) -> str:
        if not origin:
            return "unknown"
        host = urlsplit(origin).hostname or ""
        if host == "localhost" or host.endswith(".localhost"):
            return "local_trusted"
        try:
            if ipaddress.ip_address(host).is_loopback:
                return "local_trusted"
        except ValueError:
            pass
        return "untrusted_web"

    @classmethod
    def _find_context_value(cls, value: Any, key: str, *, depth: int = 0) -> Optional[str]:
        if depth > 4:
            return None
        if isinstance(value, Mapping):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
            for child in value.values():
                found = cls._find_context_value(child, key, depth=depth + 1)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value[:12]:
                found = cls._find_context_value(child, key, depth=depth + 1)
                if found:
                    return found
        elif isinstance(value, str):
            text = value.strip()
            if text[:1] in {"{", "["}:
                try:
                    return cls._find_context_value(json.loads(text), key, depth=depth + 1)
                except json.JSONDecodeError:
                    return None
        elif hasattr(value, "model_dump"):
            try:
                return cls._find_context_value(value.model_dump(), key, depth=depth + 1)
            except Exception:
                return None
        return None

    def observe_browser_result(self, *, key: str, public_session_id: str, tool: str,
                               arguments: Mapping[str, Any], result: Any) -> Optional[ExecutionSecurityState]:
        if tool not in UNTRUSTED_BROWSER_CONTENT_TOOLS:
            return None
        url = self._find_context_value(result, "url") or self._find_context_value(arguments, "url")
        origin = self._origin(url)
        trust = self._trust_for_origin(origin)
        tab_handle = self._find_context_value(result, "tab_handle") or self._find_context_value(arguments, "tab_handle")
        state = self.touch(key, public_session_id)
        with self._lock:
            # Once untrusted web content has entered a logical session, merely
            # navigating to localhost (or losing origin metadata) must not launder
            # that trust state. A clean-room/new-session transition is required.
            if origin:
                state.current_origin = origin
            state.tab_handle = tab_handle or state.tab_handle
            if trust == "untrusted_web" or (origin is None and not state.web_scoped):
                state.trust_level = "untrusted_web"
                state.web_scoped = True
                state.last_web_at = time.time()
            elif not state.web_scoped:
                state.trust_level = trust
            state.last_seen_at = time.time()
            return state

    @staticmethod
    def _privileged_host_action(tool: str, risk: RiskAssessment, arguments: Mapping[str, Any]) -> bool:
        if risk.family in _SAFE_WHILE_WEB_SCOPED_FAMILIES:
            return False
        if tool in {"spawn_agent", "spawn_agents"}:
            profile = str(arguments.get("capability_profile") or "").strip().lower()
            if profile in {"browser_only", "read_only"}:
                return False
        return bool(risk.capabilities.intersection(_PRIVILEGED_CAPABILITIES))

    def evaluate(self, *, key: str, public_session_id: str, tool: str,
                 risk: RiskAssessment, arguments: Mapping[str, Any]) -> ContextGateDecision:
        state = self.touch(key, public_session_id)
        with self._lock:
            if not state.web_scoped or state.trust_level != "untrusted_web":
                return ContextGateDecision(True, "context_allowed", "no_untrusted_web_context",
                                           public_session_id, state.current_origin, state.trust_level)
            if not self._privileged_host_action(tool, risk, arguments):
                return ContextGateDecision(True, "context_allowed", "non_privileged_or_browser_action",
                                           public_session_id, state.current_origin, state.trust_level)
            grant_key = (public_session_id, tool)
            grant = self._grants.get(grant_key)
            now = time.time()
            if grant is not None and grant.expires_at > now and grant.uses_remaining > 0 and grant.origin == state.current_origin:
                grant.uses_remaining -= 1
                if grant.uses_remaining <= 0:
                    self._grants.pop(grant_key, None)
                return ContextGateDecision(True, "web_host_escalated", "local_user_one_shot_grant",
                                           public_session_id, state.current_origin, state.trust_level, True)
            return ContextGateDecision(False, "web_host_boundary_denied",
                                       "untrusted_web_context_requires_local_escalation",
                                       public_session_id, state.current_origin, state.trust_level)

    def grant_escalation(self, public_session_id: str, tool: str, *, ttl_s: int = 120) -> dict[str, Any]:
        clean_session = str(public_session_id or "").strip()
        clean_tool = str(tool or "").strip()
        if not clean_session or not clean_tool:
            raise ValueError("session_id_and_tool_required")
        with self._lock:
            self._prune_locked()
            key = self._public_to_key.get(clean_session)
            state = self._states.get(key) if key else None
            if state is None or not state.web_scoped or state.trust_level != "untrusted_web":
                raise KeyError("web_scoped_session_not_found")
            expires = time.time() + max(10, min(int(ttl_s), 300))
            self._grants[(clean_session, clean_tool)] = EscalationGrant(
                public_session_id=clean_session, tool=clean_tool, origin=state.current_origin,
                expires_at=expires, uses_remaining=1,
            )
            return {"session_id": clean_session, "tool": clean_tool, "origin": state.current_origin,
                    "expires_at": expires, "uses_remaining": 1}

    def state_for_public_session(self, public_session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            key = self._public_to_key.get(str(public_session_id))
            state = self._states.get(key) if key else None
            if state is None:
                return None
            return {"session_id": state.public_session_id, "web_scoped": state.web_scoped,
                    "current_origin": state.current_origin, "tab_handle": state.tab_handle,
                    "trust_level": state.trust_level, "last_web_at": state.last_web_at}
