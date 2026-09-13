from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from .data_guard import (
    action_fingerprint,
    contains_direct_secret,
    safe_target_summary,
    scan_sensitive_egress,
    scan_sensitive_source,
    secret_fingerprints,
)
from .policy import Capability, PolicyContext, RiskAssessment

UNTRUSTED_BROWSER_CONTENT_TOOLS = frozenset({
    "browser_observe", "browser_find", "browser_do", "browser_get_html",
    "browser_get_snapshot", "browser_execute_js", "browser_screenshot",
})
_BROWSER_NAVIGATION_TOOLS = frozenset({"browser_open_url"})
_BROWSER_PROGRESS_TOOLS = frozenset({
    "browser_act", "browser_do", "browser_click_selector", "browser_type_selector",
    "browser_coordinate_click",
})
_PROGRESS_EXEMPT_ACTIONS = frozenset({"wait", "scroll", "extract", "focus"})
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
    tab_title: Optional[str] = None
    trust_level: str = "local"
    provenance_origin: Optional[str] = None
    provenance_tab_handle: Optional[str] = None
    provenance_tab_title: Optional[str] = None
    provenance_class: str = "local"
    tainted_at: Optional[float] = None
    taint_reasons: set[str] = field(default_factory=set)
    inherited_from_session: Optional[str] = None
    inheritance_hops: int = 0
    sensitive_fingerprints: set[str] = field(default_factory=set)
    sensitive_source_classes: set[str] = field(default_factory=set)
    clipboard_sensitive: bool = False
    last_sensitive_at: Optional[float] = None
    last_seen_at: float = field(default_factory=time.time)
    last_web_at: Optional[float] = None
    progress_state: Optional[str] = None
    no_progress_signature: Optional[str] = None
    no_progress_count: int = 0
    breaker_signature: Optional[str] = None
    breaker_since: Optional[float] = None


@dataclass
class EscalationGrant:
    public_session_id: str
    tool: str
    action_fingerprint: str
    origin: Optional[str]
    expires_at: float
    uses_remaining: int = 1
    request_id: Optional[str] = None


@dataclass
class PendingEscalation:
    request_id: str
    public_session_id: str
    tool: str
    action_fingerprint: str
    origin: Optional[str]
    tab_handle: Optional[str]
    tab_title: Optional[str]
    reason_code: str
    target_summary: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ContextGateDecision:
    allowed: bool
    code: str
    reason: str
    public_session_id: Optional[str] = None
    origin: Optional[str] = None
    trust_level: str = "local"
    escalated: bool = False
    approval_required: bool = False
    request_id: Optional[str] = None
    target_summary: Optional[str] = None
    tab_handle: Optional[str] = None
    tab_title: Optional[str] = None


class SecurityContextManager:
    """In-memory provenance, web→host boundary, and secret-egress state."""

    def __init__(
        self, *, state_ttl_s: int = 1800, max_states: int = 512,
        pending_ttl_s: int = 120, rejection_cooldown_s: int = 120,
        max_secret_fingerprints: int = 256,
        no_progress_threshold: Optional[int] = None,
    ) -> None:
        self.state_ttl_s = max(60, int(state_ttl_s))
        self.max_states = max(32, int(max_states))
        self.pending_ttl_s = max(15, min(int(pending_ttl_s), 300))
        self.rejection_cooldown_s = max(15, min(int(rejection_cooldown_s), 600))
        self.max_secret_fingerprints = max(32, min(int(max_secret_fingerprints), 2048))
        if no_progress_threshold is None:
            try:
                no_progress_threshold = int(os.getenv("MAC_MCP_NO_PROGRESS_THRESHOLD", "4"))
            except ValueError:
                no_progress_threshold = 4
        self.no_progress_threshold = max(2, min(int(no_progress_threshold), 10))
        self._lock = threading.RLock()
        self._states: dict[str, ExecutionSecurityState] = {}
        self._public_to_key: dict[str, str] = {}
        self._grants: dict[tuple[str, str], EscalationGrant] = {}
        self._pending: dict[str, PendingEscalation] = {}
        self._rejections: dict[tuple[str, str], float] = {}

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
        for request_id, pending in list(self._pending.items()):
            if pending.expires_at <= current:
                self._pending.pop(request_id, None)
        for rejection_key, expires_at in list(self._rejections.items()):
            if expires_at <= current:
                self._rejections.pop(rejection_key, None)
        if len(self._states) > self.max_states:
            ordered = sorted(self._states.values(), key=lambda item: item.last_seen_at)
            for state in ordered[: len(self._states) - self.max_states]:
                self._states.pop(state.key, None)
                self._public_to_key.pop(state.public_session_id, None)

    def _invalidate_session_grants_locked(self, public_session_id: str) -> None:
        for grant_key, grant in list(self._grants.items()):
            if grant.public_session_id == public_session_id:
                self._grants.pop(grant_key, None)
        for request_id, pending in list(self._pending.items()):
            if pending.public_session_id == public_session_id:
                self._pending.pop(request_id, None)

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


    @staticmethod
    def _browser_action_signature(tool: str, arguments: Mapping[str, Any]) -> Optional[str]:
        if tool not in _BROWSER_PROGRESS_TOOLS:
            return None
        if tool in {"browser_act", "browser_do"}:
            actions = arguments.get("actions") or []
            if not isinstance(actions, list):
                return None
            meaningful = []
            for action in actions:
                if not isinstance(action, Mapping):
                    continue
                typ = str(action.get("type") or "").lower().replace("-", "_")
                if typ in _PROGRESS_EXEMPT_ACTIONS:
                    continue
                meaningful.append(dict(action))
            if not meaningful:
                return None
            payload = {
                "browser": arguments.get("browser"),
                "tab_handle": arguments.get("tab_handle"),
                "window_index": arguments.get("window_index"),
                "tab_index": arguments.get("tab_index"),
                "actions": meaningful,
            }
            return action_fingerprint(tool, payload)
        return action_fingerprint(tool, arguments)

    @classmethod
    def _browser_progress_state(cls, result: Any) -> Optional[str]:
        url = cls._find_context_value(result, "url")
        title = cls._find_context_value(result, "title")
        revision = cls._find_context_value(result, "dom_revision")
        observation = cls._find_context_value(result, "observation_id")
        if not any(value is not None for value in (url, title, revision, observation)):
            return None
        # Observation ids are deliberately excluded: a fresh observation over an
        # unchanged DOM is not itself page progress.
        return json.dumps({
            "url": url or "",
            "title": title or "",
            "dom_revision": revision or "",
        }, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _result_failed(cls, result: Any) -> bool:
        if isinstance(result, Mapping):
            if result.get("ok") is False:
                return True
            actions = result.get("actions")
            if isinstance(actions, list) and any(isinstance(item, Mapping) and item.get("ok") is False for item in actions):
                return True
        return False

    def _observe_browser_progress_locked(
        self, state: ExecutionSecurityState, tool: str, arguments: Mapping[str, Any], result: Any,
    ) -> None:
        current_state = self._browser_progress_state(result)
        signature = self._browser_action_signature(tool, arguments)

        if signature is None:
            # Pure wait/scroll/extract/observe flows never consume the retry budget,
            # but genuine page progress should clear a prior breaker.
            if current_state is not None and current_state != state.progress_state:
                state.progress_state = current_state
                state.no_progress_signature = None
                state.no_progress_count = 0
                state.breaker_signature = None
                state.breaker_since = None
            elif current_state is not None and state.progress_state is None:
                state.progress_state = current_state
            return

        progressed = current_state is not None and state.progress_state is not None and current_state != state.progress_state
        if progressed:
            state.progress_state = current_state
            state.no_progress_signature = None
            state.no_progress_count = 0
            state.breaker_signature = None
            state.breaker_since = None
            return

        comparable = current_state is not None or self._result_failed(result)
        if not comparable:
            return
        if current_state is not None and state.progress_state is None:
            state.progress_state = current_state
        if state.no_progress_signature == signature:
            state.no_progress_count += 1
        else:
            state.no_progress_signature = signature
            state.no_progress_count = 1
        if state.no_progress_count >= self.no_progress_threshold:
            state.breaker_signature = signature
            state.breaker_since = time.time()


    @staticmethod
    def _collect_agent_ids(value: Any, *, depth: int = 0) -> list[str]:
        if depth > 5:
            return []
        found: list[str] = []
        if isinstance(value, Mapping):
            candidate = value.get("agent_id")
            if candidate is not None:
                text = str(candidate).strip()
                if text.startswith("agt_"):
                    found.append(text)
            for child in value.values():
                found.extend(SecurityContextManager._collect_agent_ids(child, depth=depth + 1))
        elif isinstance(value, (list, tuple)):
            for child in value[:32]:
                found.extend(SecurityContextManager._collect_agent_ids(child, depth=depth + 1))
        elif isinstance(value, str):
            text = value.strip()
            if text[:1] in {"{", "["}:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if parsed is not None:
                    found.extend(SecurityContextManager._collect_agent_ids(parsed, depth=depth + 1))
        elif hasattr(value, "model_dump"):
            try:
                found.extend(SecurityContextManager._collect_agent_ids(value.model_dump(), depth=depth + 1))
            except Exception:
                pass
        unique: list[str] = []
        seen: set[str] = set()
        for agent_id in found:
            if agent_id not in seen:
                seen.add(agent_id)
                unique.append(agent_id)
        return unique

    def _mark_untrusted_provenance_locked(
        self, state: ExecutionSecurityState, *, origin: Optional[str],
        tab_handle: Optional[str], tab_title: Optional[str], reason: str,
        inherited_from_session: Optional[str] = None, inheritance_hops: Optional[int] = None,
    ) -> None:
        now = time.time()
        state.web_scoped = True
        state.trust_level = "untrusted_web"
        state.provenance_class = "tainted_untrusted_web"
        state.last_web_at = state.last_web_at or now
        state.tainted_at = state.tainted_at or now
        state.taint_reasons.add(str(reason))
        if origin:
            state.provenance_origin = origin
        if tab_handle:
            state.provenance_tab_handle = tab_handle
        if tab_title:
            state.provenance_tab_title = tab_title
        if inherited_from_session:
            state.inherited_from_session = inherited_from_session
        if inheritance_hops is not None:
            state.inheritance_hops = max(state.inheritance_hops, int(inheritance_hops))
        state.last_seen_at = now

    def inherit_delegated_provenance(
        self, *, parent_key: str, parent_public_session_id: str, tool: str,
        arguments: Mapping[str, Any], result: Any,
    ) -> list[dict[str, Any]]:
        if tool not in {"spawn_agent", "spawn_agents", "agent_action"}:
            return []
        if tool == "agent_action":
            action = str(arguments.get("action") or "").strip().lower()
            if action not in {"retry", "message"}:
                return []
        child_ids = self._collect_agent_ids(result)
        if not child_ids:
            return []
        with self._lock:
            self._prune_locked()
            parent = self._states.get(parent_key)
            if (
                parent is None or not parent.web_scoped
                or parent.provenance_class != "tainted_untrusted_web"
            ):
                return []
            inherited: list[dict[str, Any]] = []
            for child_id in child_ids:
                if child_id == parent_public_session_id:
                    continue
                child_key = f"agent:{child_id}"
                child = self._states.get(child_key)
                if child is None:
                    child = ExecutionSecurityState(
                        key=child_key, public_session_id=child_id, last_seen_at=time.time()
                    )
                    self._states[child_key] = child
                else:
                    child.public_session_id = child_id
                self._public_to_key[child_id] = child_key
                self._mark_untrusted_provenance_locked(
                    child, origin=parent.provenance_origin or parent.current_origin,
                    tab_handle=parent.provenance_tab_handle or parent.tab_handle,
                    tab_title=parent.provenance_tab_title or parent.tab_title,
                    reason="delegated_context_transfer",
                    inherited_from_session=parent_public_session_id,
                    inheritance_hops=parent.inheritance_hops + 1,
                )
                child.current_origin = parent.current_origin
                child.tab_handle = parent.tab_handle
                child.tab_title = parent.tab_title
                child.sensitive_fingerprints.update(parent.sensitive_fingerprints)
                child.sensitive_source_classes.update(parent.sensitive_source_classes)
                child.clipboard_sensitive = parent.clipboard_sensitive
                child.last_sensitive_at = parent.last_sensitive_at
                inherited.append({
                    "agent_id": child_id,
                    "origin": child.provenance_origin,
                    "reason_code": "delegated_context_transfer",
                    "inheritance_hops": child.inheritance_hops,
                })
            return inherited

    def observe_browser_result(
        self, *, key: str, public_session_id: str, tool: str,
        arguments: Mapping[str, Any], result: Any,
    ) -> Optional[ExecutionSecurityState]:
        if (
            tool not in UNTRUSTED_BROWSER_CONTENT_TOOLS
            and tool not in _BROWSER_NAVIGATION_TOOLS
            and tool not in _BROWSER_PROGRESS_TOOLS
        ):
            return None
        url = self._find_context_value(result, "url") or self._find_context_value(arguments, "url")
        origin = self._origin(url)
        tab_handle = self._find_context_value(result, "tab_handle") or self._find_context_value(arguments, "tab_handle")
        tab_title = self._find_context_value(result, "title")
        state = self.touch(key, public_session_id)
        with self._lock:
            self._observe_browser_progress_locked(state, tool, arguments, result)
            if origin is None and state.current_origin:
                origin = state.current_origin
            trust = self._trust_for_origin(origin)
            if origin and state.current_origin and origin != state.current_origin:
                # Approvals are source-origin-bound. Navigation invalidates every
                # pending/granted crossing for this logical session.
                self._invalidate_session_grants_locked(public_session_id)
            if origin:
                state.current_origin = origin
            state.tab_handle = tab_handle or state.tab_handle
            state.tab_title = tab_title or state.tab_title
            if tool in _BROWSER_NAVIGATION_TOOLS:
                if not state.web_scoped:
                    state.trust_level = trust
                state.last_seen_at = time.time()
                return state
            if trust == "untrusted_web" or (origin is None and not state.web_scoped):
                self._mark_untrusted_provenance_locked(
                    state,
                    origin=origin or state.provenance_origin,
                    tab_handle=tab_handle or state.provenance_tab_handle,
                    tab_title=tab_title or state.provenance_tab_title,
                    reason="untrusted_browser_content",
                )
            elif not state.web_scoped:
                state.trust_level = trust
            state.last_seen_at = time.time()
            return state

    def observe_host_result(
        self, *, key: str, public_session_id: str, tool: str,
        arguments: Mapping[str, Any], result: Any,
    ) -> Optional[ExecutionSecurityState]:
        state = self.touch(key, public_session_id)
        scan = scan_sensitive_source(tool, arguments, result)
        with self._lock:
            if tool == "clipboard_set":
                content = str(arguments.get("content") or "")
                outgoing = secret_fingerprints(content, include_entropy=True, include_whole=True)
                state.clipboard_sensitive = contains_direct_secret(content) or bool(state.sensitive_fingerprints.intersection(outgoing))
                state.last_seen_at = time.time()
                return state
            if not scan.sensitive:
                return None
            for fingerprint in scan.fingerprints:
                if len(state.sensitive_fingerprints) >= self.max_secret_fingerprints:
                    break
                state.sensitive_fingerprints.add(fingerprint)
            if scan.source_class:
                state.sensitive_source_classes.add(scan.source_class)
            state.last_sensitive_at = time.time()
            state.last_seen_at = state.last_sensitive_at
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

    def _consume_grant_locked(
        self, state: ExecutionSecurityState, tool: str, fingerprint: str,
        *, origin: Optional[str] = None,
    ) -> Optional[EscalationGrant]:
        grant_key = (state.public_session_id, fingerprint)
        grant = self._grants.get(grant_key)
        now = time.time()
        source_origin = origin or state.provenance_origin or state.current_origin
        if (
            grant is not None and grant.expires_at > now and grant.uses_remaining > 0
            and grant.tool == tool and grant.origin == source_origin
        ):
            grant.uses_remaining -= 1
            if grant.uses_remaining <= 0:
                self._grants.pop(grant_key, None)
            return grant
        return None

    def _pending_for_locked(
        self, state: ExecutionSecurityState, tool: str, fingerprint: str,
        reason_code: str, target_summary: str,
    ) -> PendingEscalation:
        for pending in self._pending.values():
            if (
                pending.public_session_id == state.public_session_id
                and pending.tool == tool
                and pending.action_fingerprint == fingerprint
                and pending.expires_at > time.time()
            ):
                return pending
        now = time.time()
        pending = PendingEscalation(
            request_id="apr_" + uuid.uuid4().hex[:14],
            public_session_id=state.public_session_id,
            tool=tool,
            action_fingerprint=fingerprint,
            origin=state.provenance_origin or state.current_origin,
            tab_handle=state.provenance_tab_handle or state.tab_handle,
            tab_title=state.provenance_tab_title or state.tab_title,
            reason_code=reason_code,
            target_summary=target_summary,
            created_at=now,
            expires_at=now + self.pending_ttl_s,
        )
        self._pending[pending.request_id] = pending
        return pending

    def evaluate(
        self, *, key: str, public_session_id: str, tool: str,
        risk: RiskAssessment, arguments: Mapping[str, Any],
    ) -> ContextGateDecision:
        state = self.touch(key, public_session_id)
        with self._lock:
            progress_signature = self._browser_action_signature(tool, arguments)
            if progress_signature and state.breaker_signature == progress_signature:
                return ContextGateDecision(
                    False, "browser_no_progress",
                    f"same browser action produced no DOM/URL/title progress {state.no_progress_count} times",
                    public_session_id, state.current_origin, state.trust_level,
                    target_summary=f"browser action stalled after {state.no_progress_count} attempts",
                    tab_handle=state.tab_handle, tab_title=state.tab_title,
                )
            fingerprint = action_fingerprint(tool, arguments)
            argument_origin = self._origin(arguments.get("url"))
            current_target_origin = argument_origin or state.current_origin
            source_origin = state.provenance_origin or current_target_origin
            current_origin_is_untrusted = self._trust_for_origin(current_target_origin) == "untrusted_web"

            if current_origin_is_untrusted:
                egress = scan_sensitive_egress(
                    tool, arguments, state.sensitive_fingerprints,
                    clipboard_sensitive=state.clipboard_sensitive,
                )
                if egress.sensitive:
                    grant = self._consume_grant_locked(state, tool, fingerprint, origin=current_target_origin)
                    if grant is not None:
                        return ContextGateDecision(
                            True, "secret_egress_escalated", "local_user_one_shot_grant",
                            public_session_id, source_origin, state.trust_level, True,
                            request_id=grant.request_id,
                            target_summary=safe_target_summary(tool, arguments, secret_egress=True),
                            tab_handle=state.provenance_tab_handle or state.tab_handle,
                            tab_title=state.provenance_tab_title or state.tab_title,
                        )
                    rejection_key = (public_session_id, fingerprint)
                    if self._rejections.get(rejection_key, 0) > time.time():
                        return ContextGateDecision(
                            False, "security_approval_rejected", "recent_user_rejection",
                            public_session_id, source_origin, state.trust_level,
                            target_summary=safe_target_summary(tool, arguments, secret_egress=True),
                            tab_handle=state.provenance_tab_handle or state.tab_handle,
                            tab_title=state.provenance_tab_title or state.tab_title,
                        )
                    pending = self._pending_for_locked(
                        state, tool, fingerprint, "secret_egress",
                        safe_target_summary(tool, arguments, secret_egress=True),
                    )
                    if pending.origin != current_target_origin:
                        pending.origin = current_target_origin
                    return ContextGateDecision(
                        False, "secret_egress_approval_required", egress.reason or "sensitive_data_to_untrusted_origin",
                        public_session_id, source_origin, state.trust_level,
                        approval_required=True, request_id=pending.request_id,
                        target_summary=pending.target_summary, tab_handle=pending.tab_handle, tab_title=pending.tab_title,
                    )

            if not state.web_scoped or state.trust_level != "untrusted_web":
                return ContextGateDecision(
                    True, "context_allowed", "no_untrusted_web_context",
                    public_session_id, state.current_origin, state.trust_level,
                )
            if not self._privileged_host_action(tool, risk, arguments):
                return ContextGateDecision(
                    True, "context_allowed", "non_privileged_or_browser_action",
                    public_session_id, source_origin, state.trust_level,
                )

            grant = self._consume_grant_locked(state, tool, fingerprint)
            if grant is not None:
                return ContextGateDecision(
                    True, "web_host_escalated", "local_user_one_shot_grant",
                    public_session_id, source_origin, state.trust_level, True,
                    request_id=grant.request_id,
                    target_summary=safe_target_summary(tool, arguments),
                    tab_handle=state.provenance_tab_handle or state.tab_handle,
                    tab_title=state.provenance_tab_title or state.tab_title,
                )
            rejection_key = (public_session_id, fingerprint)
            if self._rejections.get(rejection_key, 0) > time.time():
                return ContextGateDecision(
                    False, "security_approval_rejected", "recent_user_rejection",
                    public_session_id, source_origin, state.trust_level,
                    target_summary=safe_target_summary(tool, arguments),
                    tab_handle=state.provenance_tab_handle or state.tab_handle,
                    tab_title=state.provenance_tab_title or state.tab_title,
                )
            pending = self._pending_for_locked(
                state, tool, fingerprint, "web_host_boundary",
                safe_target_summary(tool, arguments),
            )
            return ContextGateDecision(
                False, "web_host_boundary_approval_required",
                "untrusted_web_context_requires_local_escalation",
                public_session_id, source_origin, state.trust_level,
                approval_required=True, request_id=pending.request_id,
                target_summary=pending.target_summary, tab_handle=pending.tab_handle, tab_title=pending.tab_title,
            )

    def pending_request(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            pending = self._pending.get(str(request_id or ""))
            if pending is None:
                return None
            return {
                "request_id": pending.request_id,
                "session_id": pending.public_session_id,
                "tool": pending.tool,
                "origin": pending.origin,
                "tab_handle": pending.tab_handle,
                "tab_title": pending.tab_title,
                "reason_code": pending.reason_code,
                "target_summary": pending.target_summary,
                "expires_at": pending.expires_at,
            }

    def grant_escalation(
        self, public_session_id: str, tool: str, *, ttl_s: int = 120,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_session = str(public_session_id or "").strip()
        clean_tool = str(tool or "").strip()
        if not clean_session or not clean_tool:
            raise ValueError("session_id_and_tool_required")
        with self._lock:
            self._prune_locked()
            key = self._public_to_key.get(clean_session)
            state = self._states.get(key) if key else None
            if state is None:
                raise KeyError("security_session_not_found")
            candidates = [
                pending for pending in self._pending.values()
                if pending.public_session_id == clean_session and pending.tool == clean_tool
            ]
            if request_id:
                candidates = [item for item in candidates if item.request_id == request_id]
            if not candidates:
                raise KeyError("pending_escalation_not_found")
            pending = max(candidates, key=lambda item: item.created_at)
            expires = time.time() + max(10, min(int(ttl_s), 300))
            self._grants[(clean_session, pending.action_fingerprint)] = EscalationGrant(
                public_session_id=clean_session,
                tool=clean_tool,
                action_fingerprint=pending.action_fingerprint,
                origin=pending.origin,
                expires_at=expires,
                uses_remaining=1,
                request_id=pending.request_id,
            )
            self._pending.pop(pending.request_id, None)
            self._rejections.pop((clean_session, pending.action_fingerprint), None)
            return {
                "request_id": pending.request_id,
                "session_id": clean_session,
                "tool": clean_tool,
                "origin": pending.origin,
                "target_summary": pending.target_summary,
                "reason_code": pending.reason_code,
                "expires_at": expires,
                "uses_remaining": 1,
            }

    def reject_escalation(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            pending = self._pending.pop(str(request_id or ""), None)
            if pending is None:
                return None
            self._rejections[(pending.public_session_id, pending.action_fingerprint)] = time.time() + self.rejection_cooldown_s
            return {
                "request_id": pending.request_id,
                "session_id": pending.public_session_id,
                "tool": pending.tool,
                "origin": pending.origin,
                "target_summary": pending.target_summary,
                "reason_code": pending.reason_code,
                "rejected_until": self._rejections[(pending.public_session_id, pending.action_fingerprint)],
            }

    def state_for_public_session(self, public_session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            key = self._public_to_key.get(str(public_session_id))
            state = self._states.get(key) if key else None
            if state is None:
                return None
            pending = [
                self.pending_request(item.request_id)
                for item in self._pending.values()
                if item.public_session_id == state.public_session_id
            ]
            return {
                "session_id": state.public_session_id,
                "web_scoped": state.web_scoped,
                "current_origin": state.current_origin,
                "tab_handle": state.tab_handle,
                "tab_title": state.tab_title,
                "trust_level": state.trust_level,
                "provenance_origin": state.provenance_origin,
                "provenance_tab_handle": state.provenance_tab_handle,
                "provenance_tab_title": state.provenance_tab_title,
                "provenance_class": state.provenance_class,
                "tainted_at": state.tainted_at,
                "taint_reasons": sorted(state.taint_reasons),
                "inherited_from_session": state.inherited_from_session,
                "inheritance_hops": state.inheritance_hops,
                "last_web_at": state.last_web_at,
                "last_sensitive_at": state.last_sensitive_at,
                "sensitive_source_classes": sorted(state.sensitive_source_classes),
                "sensitive_fingerprint_count": len(state.sensitive_fingerprints),
                "clipboard_sensitive": state.clipboard_sensitive,
                "pending_escalations": [item for item in pending if item is not None],
            }
