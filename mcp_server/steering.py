from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

from mcp.types import TextContent

from .runtime_settings import steering_setting


STEERING_INSTRUCTION = (
    "The user sent these instructions from the Mac MCP menu bar while this tool was running. "
    "Treat them as new user steering for the current task before choosing the next action."
)

PREEMPT_INSTRUCTION = (
    "This tool was NOT executed. The user queued steering for this agent session while it was idle. "
    "Follow the user's steering before choosing the next action, and do not assume the preempted tool changed anything."
)

DEFAULT_SESSION_TTL_S = 600
STEERING_SCHEMA_VERSION = 1

_LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "ready": frozenset({"queued", "disconnected", "expired"}),
    "queued": frozenset({"delivered", "failed", "disconnected", "expired"}),
    "failed": frozenset({"queued", "delivered", "disconnected", "expired"}),
    "delivered": frozenset({"acknowledged", "queued", "disconnected", "expired"}),
    "acknowledged": frozenset({"queued", "disconnected", "expired"}),
    "disconnected": frozenset(),
    "expired": frozenset(),
}


@dataclass(frozen=True)
class SteeringIdentity:
    """Opaque logical identity for one MCP agent/conversation.

    Stable request metadata wins over transport identity. The raw metadata value is
    hashed before entering steering state, so vendor/account/session IDs are never
    exposed through the dashboard or written to telemetry.
    """

    key: str
    source: str
    transport_session: Any | None = field(default=None, repr=False, compare=False)


def _hashed_identity(source: str, *values: str) -> SteeringIdentity:
    raw = "\0".join((source, *values)).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return SteeringIdentity(key=f"{source}:{digest}", source=source)


def steering_identity_from_context(context: Any) -> SteeringIdentity:
    """Resolve the safest durable steering identity available for an MCP request.

    OpenAI's connector currently creates a fresh Streamable HTTP transport session
    for each tool call, but sends a stable conversation-scoped ``openai/session``
    value in request ``_meta``. Other clients may provide ``_meta.client_id`` or
    reuse a stateful MCP transport. We prefer conversation metadata, then generic
    client_id, then fall back to the transport ServerSession object.
    """

    meta = None
    try:
        meta = context.request_context.meta
    except (AttributeError, ValueError):
        pass

    extras: Dict[str, Any] = {}
    if meta is not None:
        model_extra = getattr(meta, "model_extra", None)
        if isinstance(model_extra, dict):
            extras.update(model_extra)
        try:
            dumped = meta.model_dump(by_alias=True, exclude_none=True)
            if isinstance(dumped, dict):
                extras.update(dumped)
        except (AttributeError, TypeError, ValueError):
            pass

    openai_session = str(extras.get("openai/session") or "").strip()
    if openai_session:
        # Subject scopes the opaque session token to the account when supplied,
        # without storing either raw value in Mac MCP state.
        openai_subject = str(extras.get("openai/subject") or "").strip()
        return _hashed_identity("openai_session", openai_subject, openai_session)

    client_id = ""
    try:
        client_id = str(context.client_id or "").strip()
    except (AttributeError, ValueError):
        pass
    if client_id:
        return _hashed_identity("client_id", client_id)

    try:
        session = context.session
    except (AttributeError, ValueError) as exc:
        raise ValueError("MCP request has no usable steering identity") from exc
    return SteeringIdentity(
        key=f"transport:{id(session)}",
        source="transport",
        transport_session=session,
    )


def _short(value: Any, limit: int = 76) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _domain(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlparse(text if "://" in text else "https://" + text)
        host = (parsed.hostname or "").lower()
        return host[4:] if host.startswith("www.") else (host or None)
    except ValueError:
        return None


def _path_hint(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    name = path.name or str(path)
    parent = path.parent.name
    if parent and parent not in {"/", "."}:
        return _short(f"{parent}/{name}", 58)
    return _short(name, 58)


def describe_target(tool: str, arguments: Dict[str, Any]) -> tuple[str, str]:
    """Return a human-friendly label/detail without exposing internal IDs."""
    name = str(tool or "tool")
    args = arguments or {}

    if name.startswith("browser_"):
        browser = str(args.get("browser") or "Browser")
        browser = "Chrome" if browser.lower() in {"chrome", "google chrome", "chromium"} else browser.title()
        host = _domain(args.get("url"))
        label = f"{browser} · {host}" if host else f"{browser} · {name.removeprefix('browser_').replace('_', ' ')}"
        detail = name
        if args.get("extract"):
            detail += " · " + _short(", ".join(map(str, args.get("extract") or [])), 48)
        return _short(label), _short(detail)

    if name in {"run_command", "start_background_job"}:
        command = _short(args.get("command"), 66) or name
        return f"Terminal · {command}", name
    if name == "run_commands_parallel":
        commands = args.get("commands") or []
        cwd = _path_hint(args.get("cwd"))
        label = f"Terminal · {len(commands)} parallel commands"
        if cwd:
            label += f" · {cwd}"
        return _short(label), name

    if name in {
        "read_file", "write_file", "edit_file", "move_file", "copy_file", "delete_path",
        "list_directory", "directory_tree", "search_files", "find_files", "get_file_info", "create_directory",
    }:
        hint = _path_hint(args.get("path") or args.get("source") or args.get("destination"))
        family = "Files" if name != "search_files" else "Search"
        return _short(f"{family} · {hint or name.replace('_', ' ')}"), name

    if name in {"mac_observe", "mac_act"}:
        app = _short(args.get("app"), 52)
        return _short(f"macOS · {app or name.replace('_', ' ')}"), name

    if name in {"spawn_agent", "spawn_agents", "wait_agents", "agent_action"}:
        title = _short(args.get("title") or args.get("provider") or args.get("team_id"), 58)
        return _short(f"Agents · {title or name.replace('_', ' ')}"), name

    if name == "http_request":
        host = _domain(args.get("url"))
        return _short(f"HTTP · {host or 'request'}"), name

    return _short(name.replace("_", " ").title()), name


class SteeringManager:
    """In-memory steering inbox keyed to logical MCP agent sessions."""

    def __init__(
        self,
        *,
        max_messages_per_session: int = 10,
        max_text_chars: int = 4_000,
        session_ttl_s: Optional[int] = None,
    ) -> None:
        self.max_messages_per_session = max(1, int(max_messages_per_session))
        self.max_text_chars = max(64, int(max_text_chars))
        configured_ttl = session_ttl_s
        if configured_ttl is None:
            env_ttl = os.getenv("MAC_MCP_STEERING_SESSION_TTL_S", "").strip()
            if env_ttl:
                try:
                    configured_ttl = int(env_ttl)
                except ValueError:
                    configured_ttl = DEFAULT_SESSION_TTL_S
            else:
                try:
                    configured_minutes = int(steering_setting("session_ttl_minutes", DEFAULT_SESSION_TTL_S // 60))
                except (TypeError, ValueError):
                    configured_minutes = DEFAULT_SESSION_TTL_S // 60
                configured_ttl = configured_minutes * 60
        self.session_ttl_s = max(60, int(configured_ttl))
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._transport_refs: Dict[str, weakref.ReferenceType[Any]] = {}
        self._public_to_key: Dict[str, str] = {}
        self._recent: deque[Dict[str, Any]] = deque(maxlen=100)
        self._used_flow_numbers: set[int] = set()

    @property
    def session_ttl_minutes(self) -> int:
        with self._lock:
            return max(1, int(self.session_ttl_s // 60))

    def set_session_ttl_minutes(self, minutes: int) -> int:
        value = int(minutes)
        if value <= 0:
            raise ValueError("session_ttl_must_be_positive")
        with self._lock:
            self.session_ttl_s = value * 60
            self._prune_locked()
            return max(1, int(self.session_ttl_s // 60))

    def _allocate_flow_number(self) -> int:
        for number in range(1, 1000):
            if number not in self._used_flow_numbers:
                self._used_flow_numbers.add(number)
                return number
        number = max(self._used_flow_numbers, default=0) + 1
        self._used_flow_numbers.add(number)
        return number

    def _transition_locked(
        self,
        state: Dict[str, Any],
        lifecycle_state: str,
        *,
        last_error: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        current = str(state.get("lifecycle_state") or "ready")
        target = str(lifecycle_state)
        if target == current:
            if last_error is not None:
                state["last_error"] = last_error
            return
        allowed = _LIFECYCLE_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise RuntimeError(f"illegal_steering_transition:{current}->{target}")
        state["lifecycle_state"] = target
        state["last_transition_at"] = time.time() if now is None else now
        state["last_error"] = last_error

    @staticmethod
    def _recent_lifecycle_state(status: str) -> str:
        return {
            "queued": "queued",
            "delivered": "delivered",
            "preempted": "delivered",
            "acknowledged": "acknowledged",
            "delivery_failed": "failed",
            "session_ended": "disconnected",
            "session_expired": "expired",
        }.get(status, status)

    def _recent_record(
        self,
        message: Dict[str, Any],
        status: str,
        *,
        tool: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        now = time.time()
        lifecycle_state = self._recent_lifecycle_state(status)
        self._recent.append({
            "schema_version": STEERING_SCHEMA_VERSION,
            "kind": "instruction",
            "id": message.get("id"),
            "session_id": message.get("session_id"),
            "status": status,
            "lifecycle_state": lifecycle_state,
            "created_at": message.get("created_at"),
            "transitioned_at": now,
            "delivered_at": now if lifecycle_state == "delivered" else None,
            "tool": tool,
            "delivery_mode": delivery_mode,
            "last_error": last_error,
        })

    def _recent_session_record(self, state: Dict[str, Any], status: str) -> None:
        now = time.time()
        self._recent.append({
            "schema_version": STEERING_SCHEMA_VERSION,
            "kind": "session",
            "id": "se_" + uuid.uuid4().hex[:12],
            "session_id": state.get("session_id"),
            "status": status,
            "lifecycle_state": self._recent_lifecycle_state(status),
            "created_at": state.get("created_at"),
            "transitioned_at": now,
            "delivered_at": None,
            "tool": state.get("last_tool"),
            "delivery_mode": None,
            "last_error": state.get("last_error"),
        })

    def _acknowledge_locked(self, state: Dict[str, Any], *, tool: Optional[str] = None) -> None:
        awaiting = list(state.get("awaiting_ack") or [])
        if not awaiting:
            return
        state["awaiting_ack"].clear()
        for message in awaiting:
            self._recent_record(message, "acknowledged", tool=tool)
        if not state.get("pending"):
            self._transition_locked(state, "acknowledged")

    def _drop_key_locked(self, key: str, *, status: str = "session_ended") -> None:
        state = self._sessions.pop(key, None)
        self._transport_refs.pop(key, None)
        if state is None:
            return
        terminal_state = self._recent_lifecycle_state(status)
        if terminal_state in {"disconnected", "expired"}:
            self._transition_locked(state, terminal_state)
        public_id = str(state.get("session_id") or "")
        self._public_to_key.pop(public_id, None)
        self._used_flow_numbers.discard(int(state.get("flow_number") or 0))
        for message in state.get("pending", []):
            self._recent_record(message, status)
        self._recent_session_record(state, status)

    def _drop_transport_identity(self, key: str, expected_public_id: str) -> None:
        with self._lock:
            state = self._sessions.get(key)
            if state is None or state.get("session_id") != expected_public_id:
                return
            self._drop_key_locked(key)

    def _prune_locked(self, now: Optional[float] = None) -> None:
        current = time.time() if now is None else now
        for key, state in list(self._sessions.items()):
            if state.get("active"):
                continue
            if state.get("identity_source") == "transport":
                ref = self._transport_refs.get(key)
                if ref is not None and ref() is None:
                    self._drop_key_locked(key)
                    continue
            last_activity = float(state.get("last_activity_at") or state.get("created_at") or current)
            if current - last_activity > self.session_ttl_s:
                self._drop_key_locked(key, status="session_expired")

    def _ensure_identity_locked(
        self,
        identity: SteeringIdentity,
        *,
        tool: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        self._prune_locked(now)
        state = self._sessions.get(identity.key)

        if identity.source == "transport":
            session = identity.transport_session
            if session is None:
                raise ValueError("transport steering identity requires a session object")
            ref = self._transport_refs.get(identity.key)
            if state is not None and (ref is None or ref() is not session):
                # Python object ids can be reused after GC. Never merge a new
                # transport into an old state merely because the id matches.
                self._drop_key_locked(identity.key)
                state = None

        if state is None:
            public_id = "sess_" + uuid.uuid4().hex[:12]
            state = {
                "session_id": public_id,
                "flow_number": self._allocate_flow_number(),
                "identity_source": identity.source,
                "created_at": now,
                "last_activity_at": now,
                "last_tool": "",
                "label": "Agent session",
                "detail": "Idle",
                "pending": [],
                "awaiting_ack": [],
                "active": {},
                "lifecycle_state": "ready",
                "last_transition_at": now,
                "last_error": None,
            }
            self._sessions[identity.key] = state
            self._public_to_key[public_id] = identity.key

            if identity.source == "transport":
                session = identity.transport_session
                self_ref = weakref.ref(self)

                def gone(
                    _ref: weakref.ReferenceType[Any],
                    *,
                    key: str = identity.key,
                    sid: str = public_id,
                ) -> None:
                    manager = self_ref()
                    if manager is not None:
                        manager._drop_transport_identity(key, sid)

                self._transport_refs[identity.key] = weakref.ref(session, gone)

        if tool:
            label, detail = describe_target(tool, arguments or {})
            state["last_tool"] = str(tool)
            state["label"] = label
            state["detail"] = detail
            state["last_activity_at"] = now
        return state

    def session_id_for(self, identity: SteeringIdentity) -> str:
        with self._lock:
            return str(self._ensure_identity_locked(identity)["session_id"])

    def prepare_call(
        self,
        identity: SteeringIdentity,
        *,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> list[Dict[str, Any]]:
        """Register/touch identity and consume queued idle steering before execution."""
        with self._lock:
            state = self._ensure_identity_locked(identity, tool=tool, arguments=arguments)
            self._acknowledge_locked(state, tool=tool)
            pending = list(state["pending"])
            if not pending:
                return []
            state["pending"].clear()
            state["awaiting_ack"].extend(dict(message) for message in pending)
            now = time.time()
            state["last_activity_at"] = now
            for message in pending:
                self._recent_record(message, "preempted", tool=tool, delivery_mode="preempted")
            self._transition_locked(state, "delivered", now=now)
            return [dict(message) for message in pending]

    def begin_call(
        self,
        identity: SteeringIdentity,
        event_id: str,
        *,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        label, detail = describe_target(tool, arguments or {})
        with self._lock:
            state = self._ensure_identity_locked(identity, tool=tool, arguments=arguments)
            state["active"][event_id] = {
                "event_id": event_id,
                "tool": str(tool),
                "label": label,
                "detail": detail,
                "started_at": now,
            }
            state["last_activity_at"] = now
            return self._public_state_locked(state, now=now)

    def finish_call(self, identity: SteeringIdentity, event_id: str, *, delivered: bool) -> list[Dict[str, Any]]:
        """Finish one call; failed calls leave pending steering for the next preemption."""
        now = time.time()
        with self._lock:
            state = self._sessions.get(identity.key)
            if state is None:
                return []
            state["active"].pop(event_id, None)
            state["last_activity_at"] = now
            if not delivered:
                if state.get("pending"):
                    self._transition_locked(
                        state,
                        "failed",
                        last_error="tool_failed_before_steering_delivery",
                        now=now,
                    )
                    for message in state["pending"]:
                        self._recent_record(
                            message,
                            "delivery_failed",
                            tool=state.get("last_tool"),
                            last_error="tool_failed_before_steering_delivery",
                        )
                return []
            messages = list(state["pending"])
            state["pending"].clear()
            if messages:
                state["awaiting_ack"].extend(dict(message) for message in messages)
                for message in messages:
                    self._recent_record(
                        message,
                        "delivered",
                        tool=state.get("last_tool"),
                        delivery_mode="running_result",
                    )
                self._transition_locked(state, "delivered", now=now)
            return [dict(message) for message in messages]

    def enqueue(self, session_id: str, text: str) -> Dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("empty_message")
        if len(clean) > self.max_text_chars:
            raise ValueError("message_too_long")
        with self._lock:
            self._prune_locked()
            key = self._public_to_key.get(str(session_id))
            state = self._sessions.get(key) if key is not None else None
            if key is None or state is None:
                raise KeyError("session_closed")
            queue = state["pending"]
            if len(queue) >= self.max_messages_per_session:
                raise OverflowError("queue_full")
            message = {
                "id": "st_" + uuid.uuid4().hex[:12],
                "session_id": str(session_id),
                "text": clean,
                "created_at": time.time(),
                "status": "queued",
            }
            queue.append(message)
            self._recent_record(message, "queued", tool=state.get("last_tool"))
            self._transition_locked(state, "queued")
            return {
                **message,
                "session_state": "working" if state["active"] else "idle",
                "activity_state": "working" if state["active"] else "idle",
                "lifecycle_state": state["lifecycle_state"],
            }

    def _public_state_locked(self, state: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
        current = time.time() if now is None else now
        active_calls = list(state["active"].values())
        if active_calls:
            current_call = max(active_calls, key=lambda item: float(item.get("started_at") or 0.0))
            status = "working"
            label = current_call["label"]
            detail = current_call["detail"]
            tool = current_call["tool"]
            activity_ms = max(0, int((current - float(current_call["started_at"])) * 1000))
        else:
            status = "idle"
            label = state["label"]
            detail = state["detail"]
            tool = state["last_tool"]
            activity_ms = max(0, int((current - float(state["last_activity_at"])) * 1000))
        return {
            "schema_version": STEERING_SCHEMA_VERSION,
            "session_id": state["session_id"],
            "flow_number": state["flow_number"],
            "label": label,
            "detail": detail,
            "tool": tool,
            # Backward-compatible activity fields.
            "state": status,
            "queued": len(state["pending"]),
            # Versioned lifecycle contract.
            "activity_state": status,
            "lifecycle_state": state.get("lifecycle_state", "ready"),
            "last_transition_at": state.get("last_transition_at", state["created_at"]),
            "last_error": state.get("last_error"),
            "pending_instruction_count": len(state["pending"]),
            "awaiting_acknowledgement_count": len(state.get("awaiting_ack") or []),
            "created_at": state["created_at"],
            "last_activity_at": state["last_activity_at"],
            "activity_ms": activity_ms,
            "active_calls": len(active_calls),
        }

    def sessions(self) -> list[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            rows = [self._public_state_locked(state, now=now) for state in self._sessions.values()]
        return sorted(rows, key=lambda item: (item["flow_number"], item["created_at"]))

    def recent(self, limit: int = 30) -> list[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[-max(1, min(int(limit), 100)) :][::-1]


def preemption_error(tool: str, messages: Iterable[Dict[str, Any]]) -> str:
    public_messages = [
        {
            "id": str(message.get("id") or ""),
            "text": str(message.get("text") or ""),
            "created_at": message.get("created_at"),
        }
        for message in messages
    ]
    payload = {
        "_mac_mcp_steering": {
            "preempted_tool": str(tool),
            "instruction": PREEMPT_INSTRUCTION,
            "messages": public_messages,
        }
    }
    return "mac_mcp_steering_preempted: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def attach_steering(result: Any, messages: Iterable[Dict[str, Any]]) -> Any:
    public_messages = [
        {
            "id": str(message.get("id") or ""),
            "text": str(message.get("text") or ""),
            "created_at": message.get("created_at"),
        }
        for message in messages
    ]
    if not public_messages:
        return result

    payload = {
        "_mac_mcp_steering": {
            "instruction": STEERING_INSTRUCTION,
            "messages": public_messages,
        }
    }
    block = TextContent(
        type="text",
        text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )

    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        content, structured = result
        if isinstance(content, (list, tuple)):
            content = [*content, block]
        else:
            content = [content, block]

        if isinstance(structured.get("result"), dict):
            structured = dict(structured)
            inner = dict(structured["result"])
            inner["_mac_mcp_steering"] = payload["_mac_mcp_steering"]
            structured["result"] = inner
        return (content, structured)

    if isinstance(result, dict):
        enriched = dict(result)
        enriched["_mac_mcp_steering"] = payload["_mac_mcp_steering"]
        return enriched
    if isinstance(result, list):
        return [*result, block]
    if isinstance(result, tuple):
        return [*result, block]
    return [result, block]
