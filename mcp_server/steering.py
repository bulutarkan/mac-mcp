from __future__ import annotations

import json
import threading
import time
import uuid
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

from mcp.types import TextContent


STEERING_INSTRUCTION = (
    "The user sent these instructions from the Mac MCP menu bar while this tool was running. "
    "Treat them as new user steering for the current task before choosing the next action."
)

PREEMPT_INSTRUCTION = (
    "This tool was NOT executed. The user queued steering for this agent session while it was idle. "
    "Follow the user's steering before choosing the next action, and do not assume the preempted tool changed anything."
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
    """In-memory steering inbox keyed to persistent stateful MCP sessions.

    The Python ServerSession object is stable for the lifetime of a stateful MCP
    transport. Mac MCP assigns it a short local session ID for menu-bar routing.
    Raw user steering text never enters persistent telemetry.
    """

    def __init__(self, *, max_messages_per_session: int = 10, max_text_chars: int = 4_000) -> None:
        self.max_messages_per_session = max(1, int(max_messages_per_session))
        self.max_text_chars = max(64, int(max_text_chars))
        self._lock = threading.RLock()
        self._sessions: Dict[int, Dict[str, Any]] = {}
        self._session_refs: Dict[int, weakref.ReferenceType[Any]] = {}
        self._public_to_key: Dict[str, int] = {}
        self._recent: deque[Dict[str, Any]] = deque(maxlen=100)
        self._used_flow_numbers: set[int] = set()

    def _allocate_flow_number(self) -> int:
        for number in range(1, 1000):
            if number not in self._used_flow_numbers:
                self._used_flow_numbers.add(number)
                return number
        number = max(self._used_flow_numbers, default=0) + 1
        self._used_flow_numbers.add(number)
        return number

    def _recent_record(self, message: Dict[str, Any], status: str, *, tool: Optional[str] = None) -> None:
        now = time.time()
        self._recent.append({
            "id": message.get("id"),
            "session_id": message.get("session_id"),
            "status": status,
            "created_at": message.get("created_at"),
            "delivered_at": now if status in {"delivered", "preempted"} else None,
            "tool": tool,
        })

    def _drop_session(self, key: int, expected_public_id: str) -> None:
        with self._lock:
            state = self._sessions.get(key)
            if state is None or state.get("session_id") != expected_public_id:
                return
            self._sessions.pop(key, None)
            self._session_refs.pop(key, None)
            self._public_to_key.pop(expected_public_id, None)
            self._used_flow_numbers.discard(int(state.get("flow_number") or 0))
            for message in state.get("pending", []):
                self._recent_record(message, "session_ended")

    def _ensure_session_locked(
        self,
        session: Any,
        *,
        tool: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = id(session)
        ref = self._session_refs.get(key)
        state = self._sessions.get(key)
        if ref is None or ref() is not session or state is None:
            public_id = "sess_" + uuid.uuid4().hex[:12]
            flow_number = self._allocate_flow_number()
            now = time.time()
            state = {
                "session_id": public_id,
                "flow_number": flow_number,
                "created_at": now,
                "last_activity_at": now,
                "last_tool": "",
                "label": "Agent session",
                "detail": "Idle",
                "pending": [],
                "active": {},
            }
            self._sessions[key] = state
            self._public_to_key[public_id] = key
            self_ref = weakref.ref(self)

            def gone(_ref: weakref.ReferenceType[Any], *, object_key: int = key, sid: str = public_id) -> None:
                manager = self_ref()
                if manager is not None:
                    manager._drop_session(object_key, sid)

            self._session_refs[key] = weakref.ref(session, gone)

        if tool:
            label, detail = describe_target(tool, arguments or {})
            state["last_tool"] = str(tool)
            state["label"] = label
            state["detail"] = detail
            state["last_activity_at"] = time.time()
        return state

    def session_id_for(self, session: Any) -> str:
        with self._lock:
            return str(self._ensure_session_locked(session)["session_id"])

    def prepare_call(
        self,
        session: Any,
        *,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> list[Dict[str, Any]]:
        """Register/touch a session and consume idle steering before tool execution."""
        with self._lock:
            state = self._ensure_session_locked(session, tool=tool, arguments=arguments)
            pending = list(state["pending"])
            if not pending:
                return []
            state["pending"].clear()
            state["last_activity_at"] = time.time()
            for message in pending:
                self._recent_record(message, "preempted", tool=tool)
            return [dict(message) for message in pending]

    def begin_call(
        self,
        session: Any,
        event_id: str,
        *,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        label, detail = describe_target(tool, arguments or {})
        with self._lock:
            state = self._ensure_session_locked(session, tool=tool, arguments=arguments)
            state["active"][event_id] = {
                "event_id": event_id,
                "tool": str(tool),
                "label": label,
                "detail": detail,
                "started_at": now,
            }
            state["last_activity_at"] = now
            return self._public_state_locked(state, now=now)

    def finish_call(self, session: Any, event_id: str, *, delivered: bool) -> list[Dict[str, Any]]:
        """Finish one active call. Failed tools leave pending steering queued for the next call."""
        now = time.time()
        with self._lock:
            key = id(session)
            ref = self._session_refs.get(key)
            state = self._sessions.get(key)
            if ref is None or ref() is not session or state is None:
                return []
            state["active"].pop(event_id, None)
            state["last_activity_at"] = now
            if not delivered:
                return []
            messages = list(state["pending"])
            state["pending"].clear()
            for message in messages:
                self._recent_record(message, "delivered", tool=state.get("last_tool"))
            return [dict(message) for message in messages]

    def enqueue(self, session_id: str, text: str) -> Dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("empty_message")
        if len(clean) > self.max_text_chars:
            raise ValueError("message_too_long")
        with self._lock:
            key = self._public_to_key.get(str(session_id))
            state = self._sessions.get(key) if key is not None else None
            ref = self._session_refs.get(key) if key is not None else None
            if key is None or state is None or ref is None or ref() is None:
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
            return {
                **message,
                "session_state": "working" if state["active"] else "idle",
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
            "session_id": state["session_id"],
            "flow_number": state["flow_number"],
            "label": label,
            "detail": detail,
            "tool": tool,
            "state": status,
            "created_at": state["created_at"],
            "last_activity_at": state["last_activity_at"],
            "activity_ms": activity_ms,
            "queued": len(state["pending"]),
            "active_calls": len(active_calls),
        }

    def sessions(self) -> list[Dict[str, Any]]:
        now = time.time()
        with self._lock:
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

    # FastMCP structured-output tools return (content_blocks, structured_content).
    # Preserve that tuple exactly and append steering only to the unstructured
    # content side so MCP outputSchema validation keeps receiving its dict.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        content, structured = result
        if isinstance(content, (list, tuple)):
            content = [*content, block]
        else:
            content = [content, block]

        # FastMCP wraps Dict[str, Any] returns as {"result": {...}}. Mirror the
        # steering payload into that inner result so clients/connectors that surface
        # only structuredContent still deliver the user's steering to the model.
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
