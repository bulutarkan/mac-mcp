from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

from mcp.types import TextContent


STEERING_INSTRUCTION = (
    "The user sent these instructions from the Mac MCP menu bar while this tool was running. "
    "Treat them as new user steering for the current task before choosing the next action."
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
    """Return a human-friendly label/detail without exposing internal event IDs."""
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

    if name in {"read_file", "write_file", "edit_file", "move_file", "copy_file", "delete_path", "list_directory", "directory_tree", "search_files", "find_files", "get_file_info", "create_directory"}:
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
    """In-memory, localhost-fed steering inbox keyed to active top-level MCP calls."""

    def __init__(self, *, max_messages_per_target: int = 10, max_text_chars: int = 4_000) -> None:
        self.max_messages_per_target = max(1, int(max_messages_per_target))
        self.max_text_chars = max(64, int(max_text_chars))
        self._lock = threading.RLock()
        self._targets: Dict[str, Dict[str, Any]] = {}
        self._messages: Dict[str, list[Dict[str, Any]]] = {}
        self._recent: deque[Dict[str, Any]] = deque(maxlen=100)
        self._used_flow_numbers: set[int] = set()

    def _allocate_flow_number(self) -> int:
        for number in range(1, 1000):
            if number not in self._used_flow_numbers:
                self._used_flow_numbers.add(number)
                return number
        return max(self._used_flow_numbers, default=0) + 1

    def open_target(self, event_id: str, *, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = time.time()
        label, detail = describe_target(tool, arguments or {})
        with self._lock:
            flow_number = self._allocate_flow_number()
            target = {
                "event_id": event_id,
                "flow_number": flow_number,
                "label": label,
                "detail": detail,
                "tool": str(tool),
                "started_at": now,
            }
            self._targets[event_id] = target
            self._messages[event_id] = []
            return dict(target)

    def active_targets(self) -> list[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows = []
            for event_id, target in self._targets.items():
                row = dict(target)
                row["duration_ms"] = max(0, int((now - float(row["started_at"])) * 1000))
                row["queued"] = len(self._messages.get(event_id, []))
                rows.append(row)
        return sorted(rows, key=lambda item: (item["flow_number"], item["started_at"]))

    def enqueue(self, event_id: str, text: str) -> Dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("empty_message")
        if len(clean) > self.max_text_chars:
            raise ValueError("message_too_long")
        with self._lock:
            if event_id not in self._targets:
                raise KeyError("target_closed")
            queue = self._messages.setdefault(event_id, [])
            if len(queue) >= self.max_messages_per_target:
                raise OverflowError("queue_full")
            message = {
                "id": "st_" + uuid.uuid4().hex[:12],
                "event_id": event_id,
                "text": clean,
                "created_at": time.time(),
                "status": "queued",
            }
            queue.append(message)
            return dict(message)

    def close_target(self, event_id: str, *, delivered: bool) -> list[Dict[str, Any]]:
        """Atomically close first, then consume; late enqueue therefore returns target_closed."""
        now = time.time()
        with self._lock:
            target = self._targets.pop(event_id, None)
            if target is not None:
                self._used_flow_numbers.discard(int(target.get("flow_number") or 0))
            messages = self._messages.pop(event_id, [])
            status = "delivered" if delivered else "tool_failed"
            for message in messages:
                message["status"] = status
                message["delivered_at"] = now if delivered else None
                message["closed_at"] = now
                self._recent.append(dict(message))
            return [dict(message) for message in messages]

    def recent(self, limit: int = 30) -> list[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[-max(1, min(int(limit), 100)) :][::-1]


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
        text=__import__("json").dumps(payload, ensure_ascii=False, separators=(",", ":")),
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
        # Do not add arbitrary top-level keys because stricter output schemas may
        # reject them.
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
