"""What a delegated agent did, as steps a person can read (#175 run reports).

Each provider streams its own events into the agent's stdout.log: Codex `exec --json` items,
Claude Code stream-json messages, OpenCode `run --format json` parts. This module turns them
into one list of steps:

    {"kind": "thinking" | "message" | "tool" | "command" | "files" | "search" | "todo" | "error",
     "title": "plain sentence", "detail": "secondary line", "text": "longer text",
     "status": "done" | "failed" | "running", "input": {name: short value}, "output": "text"}

Long values are cut; nothing here is sent anywhere, it only shapes what the owner sees.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_STEPS = 200
MAX_TEXT = 4000
MAX_OUTPUT = 2500
MAX_VALUE = 300
_MARKER = "TASK_RESULT_ENVELOPE_V1"

# Mac MCP's own tools, in words. Anything else is shown by its name made readable.
_TOOL_WORDS = {
    "tool_discover": "Looked for the right tool",
    "tool_invoke": "Used a Mac MCP tool",
    "read_file": "Read a file",
    "write_file": "Wrote a file",
    "edit_file": "Edited a file",
    "search_files": "Searched files",
    "run_command": "Ran a command",
    "browser_observe": "Looked at a web page",
    "browser_act": "Worked in a web page",
    "browser_do": "Visited a web page",
    "browser_find": "Searched a web page",
    "browser_list_tabs": "Listed browser tabs",
    "mac_snapshot": "Looked at the screen",
    "mac_observe": "Looked at an app",
    "mac_act": "Worked in an app",
    "mac_app": "Used an app",
    "http_request": "Made a web request",
    "get_system_info": "Read system information",
    "system_info": "Read system information",
    "memory_search": "Searched memories",
    "screenshot": "Took a screenshot",
}
_CLAUDE_TOOLS = {
    "Bash": "Ran a command", "Read": "Read a file", "Write": "Wrote a file", "Edit": "Edited a file",
    "MultiEdit": "Edited a file", "Grep": "Searched files", "Glob": "Found files", "WebSearch": "Searched the web",
    "WebFetch": "Read a web page", "TodoWrite": "Updated its plan", "Task": "Started a helper",
    "NotebookEdit": "Edited a notebook",
}
_OPENCODE_TOOLS = {"bash": "Ran a command", "read": "Read a file", "write": "Wrote a file", "edit": "Edited a file",
                   "grep": "Searched files", "glob": "Found files", "list": "Listed a folder", "webfetch": "Read a web page",
                   "todowrite": "Updated its plan", "patch": "Edited files"}


def _cut(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _humanize(name: str) -> str:
    words = re.sub(r"[_\-]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)).strip()
    return words[:1].upper() + words[1:].lower() if words else "Used a tool"


def _inputs(arguments: Any) -> Dict[str, str]:
    """Arguments as short name/value pairs; the intent sentence is shown as the step's title instead."""
    if not isinstance(arguments, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in list(arguments.items())[:12]:
        if key == "description" or value in (None, "", [], {}):
            continue
        out[str(key)] = _cut(value, MAX_VALUE)
    return out


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "content" in content:
            return _content_text(content["content"])
        if "text" in content:
            return str(content.get("text") or "")
        return json.dumps(content, ensure_ascii=False, default=str)
    if isinstance(content, list):
        return "\n".join(_content_text(item) for item in content if item)
    return "" if content is None else str(content)


def _readable_output(text: str) -> str:
    """Tool results are often JSON; keep them, the app shows JSON objects as rows."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1)[:MAX_OUTPUT * 2]
        except ValueError:
            pass
    return _cut(text, MAX_OUTPUT)


def _mcp_step(server: str, tool: str, arguments: Any, output: str, failed: bool, running: bool) -> Dict[str, Any]:
    arguments = arguments if isinstance(arguments, dict) else {}
    inner = str(arguments.get("tool_name") or "") if tool == "tool_invoke" else ""
    shown = inner or tool
    intent = str(arguments.get("description") or "").strip()
    title = intent or _TOOL_WORDS.get(shown) or _humanize(shown)
    inputs = _inputs(arguments.get("arguments") if inner else arguments)
    detail = ("Mac MCP" if server.startswith("mac-mcp") or server.startswith("mac_mcp") else server or "Tool") + f" · {shown}"
    return {"kind": "tool", "title": _cut(title, 160), "detail": detail, "input": inputs,
            "output": _readable_output(output) if output else "",
            "status": "running" if running else "failed" if failed else "done"}


# ── Codex ────────────────────────────────────────────────────────────────────

def _codex_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = str(item.get("type") or "")
    status = str(item.get("status") or "completed")
    running = status == "in_progress"
    failed = status in {"failed", "declined"} or bool(item.get("error"))
    if kind == "reasoning":
        text = str(item.get("text") or "").strip()
        heading = re.match(r"^\*\*(.+?)\*\*\s*", text)
        title = heading.group(1) if heading else "Thinking"
        body = text[heading.end():] if heading else text
        return {"kind": "thinking", "title": _cut(title, 120), "text": _cut(body, MAX_TEXT), "status": "done"}
    if kind == "agent_message":
        text = str(item.get("text") or "").split(_MARKER, 1)[0].strip()
        return {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"} if text else None
    if kind == "mcp_tool_call":
        error = item.get("error")
        output = _content_text(item.get("result")) or (_content_text(error) if error else "")
        return _mcp_step(str(item.get("server") or ""), str(item.get("tool") or ""), item.get("arguments"),
                         output, failed, running)
    if kind == "command_execution":
        code = item.get("exit_code")
        command = str(item.get("command") or "")
        command = re.sub(r"^(/bin/)?(ba|z)?sh -l?c ", "", command).strip("'\"")
        return {"kind": "command", "title": "Ran a command", "detail": _cut(command, 400),
                "output": _cut(str(item.get("aggregated_output") or ""), MAX_OUTPUT),
                "exit_code": code, "status": "running" if running else "failed" if (code not in (None, 0) or failed) else "done"}
    if kind == "file_change":
        changes = [{"path": str(change.get("path") or ""), "change": str(change.get("kind") or "update")}
                   for change in (item.get("changes") or []) if isinstance(change, dict)][:50]
        count = len(changes)
        return {"kind": "files", "title": f"Changed {count} file{'s' if count != 1 else ''}", "files": changes,
                "status": "failed" if failed else "done"}
    if kind == "web_search":
        return {"kind": "search", "title": "Searched the web", "detail": _cut(item.get("query") or "", 200), "status": "done"}
    if kind == "todo_list":
        items = [{"text": _cut(entry.get("text") or "", 200), "done": bool(entry.get("completed"))}
                 for entry in (item.get("items") or []) if isinstance(entry, dict)][:30]
        return {"kind": "todo", "title": "Planned the work", "items": items, "status": "done"}
    if kind == "error":
        return {"kind": "error", "title": "Ran into a problem", "text": _cut(item.get("message") or "", MAX_TEXT),
                "status": "failed"}
    return None


# ── Claude Code ──────────────────────────────────────────────────────────────

def _claude_tool(name: str, arguments: Any) -> Dict[str, Any]:
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return _mcp_step(server, tool, arguments, "", False, True)
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "Bash":
        return {"kind": "command", "title": str(arguments.get("description") or "Ran a command"),
                "detail": _cut(arguments.get("command") or "", 400), "output": "", "status": "running"}
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        path = str(arguments.get("file_path") or arguments.get("notebook_path") or "")
        return {"kind": "files", "title": _CLAUDE_TOOLS.get(name, "Edited a file"),
                "files": [{"path": path, "change": "add" if name == "Write" else "update"}], "status": "running"}
    if name == "WebSearch":
        return {"kind": "search", "title": "Searched the web", "detail": _cut(arguments.get("query") or "", 200),
                "status": "running"}
    if name == "TodoWrite":
        items = [{"text": _cut(todo.get("content") or "", 200), "done": todo.get("status") == "completed"}
                 for todo in (arguments.get("todos") or []) if isinstance(todo, dict)][:30]
        return {"kind": "todo", "title": "Planned the work", "items": items, "status": "done"}
    return {"kind": "tool", "title": _CLAUDE_TOOLS.get(name) or _humanize(name), "detail": name,
            "input": _inputs(arguments), "output": "", "status": "running"}


# ── Building the list ────────────────────────────────────────────────────────

def from_events(lines: List[str]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    by_id: Dict[str, int] = {}
    usage: Dict[str, Any] = {}

    def put(key: Optional[str], step: Optional[Dict[str, Any]]) -> None:
        if not step:
            return
        if key and key in by_id:
            previous = steps[by_id[key]]
            for field in ("title", "detail", "input"):
                if previous.get(field) and not step.get(field):
                    step[field] = previous[field]
            steps[by_id[key]] = step
            return
        if len(steps) >= MAX_STEPS:
            return
        if key:
            by_id[key] = len(steps)
        steps.append(step)

    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        # Codex
        if kind in {"item.started", "item.updated", "item.completed"} and isinstance(event.get("item"), dict):
            item = event["item"]
            put(str(item.get("id") or "") or None, _codex_item(item))
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            raw = event["usage"]
            usage = {"input_tokens": int(raw.get("input_tokens") or 0), "output_tokens": int(raw.get("output_tokens") or 0),
                     "reasoning_tokens": int(raw.get("reasoning_output_tokens") or 0)}
        elif kind in {"turn.failed", "error"}:
            message = (event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else event.get("message")
            if message:
                put(None, {"kind": "error", "title": "Ran into a problem", "text": _cut(message, MAX_TEXT), "status": "failed"})
        # Claude Code
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            for block in event["message"].get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thinking" and str(block.get("thinking") or "").strip():
                    put(None, {"kind": "thinking", "title": "Thinking", "text": _cut(block["thinking"], MAX_TEXT), "status": "done"})
                elif block.get("type") == "text":
                    text = str(block.get("text") or "").split(_MARKER, 1)[0].strip()
                    if text:
                        put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
                elif block.get("type") == "tool_use":
                    put(str(block.get("id") or "") or None, _claude_tool(str(block.get("name") or ""), block.get("input")))
        elif kind == "user" and isinstance(event.get("message"), dict):
            for block in event["message"].get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    index = by_id.get(str(block.get("tool_use_id") or ""))
                    if index is not None:
                        step = steps[index]
                        output = _content_text(block.get("content"))
                        step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                        step["status"] = "failed" if block.get("is_error") else "done"
        elif kind == "result" and isinstance(event.get("usage"), dict):
            raw = event["usage"]
            usage = {"input_tokens": int(raw.get("input_tokens") or 0) + int(raw.get("cache_read_input_tokens") or 0),
                     "output_tokens": int(raw.get("output_tokens") or 0)}
            if event.get("total_cost_usd") is not None:
                usage["cost_usd"] = float(event["total_cost_usd"])
        # OpenCode
        elif kind in {"tool_use", "text", "reasoning"} and isinstance(event.get("part"), dict):
            part = event["part"]
            if kind == "text":
                text = str(part.get("text") or "").split(_MARKER, 1)[0].strip()
                if text:
                    put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
            elif kind == "reasoning":
                if str(part.get("text") or "").strip():
                    put(None, {"kind": "thinking", "title": "Thinking", "text": _cut(part["text"], MAX_TEXT), "status": "done"})
            else:
                tool = str(part.get("tool") or "")
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                status = str(state.get("status") or "completed")
                failed = status == "error"
                output = _content_text(state.get("output") or state.get("error") or "")
                if "_" in tool and tool.split("_", 1)[0] in {"mac-mcp", "mac", "macmcp"}:
                    step = _mcp_step("mac-mcp", tool.split("_", 1)[1], state.get("input"), output, failed, False)
                elif tool == "bash":
                    step = {"kind": "command", "title": str(state.get("title") or "Ran a command"),
                            "detail": _cut((state.get("input") or {}).get("command") or "", 400),
                            "output": _cut(output, MAX_OUTPUT), "status": "failed" if failed else "done"}
                else:
                    step = {"kind": "tool", "title": str(state.get("title") or _OPENCODE_TOOLS.get(tool) or _humanize(tool)),
                            "detail": tool, "input": _inputs(state.get("input")), "output": _readable_output(output),
                            "status": "failed" if failed else "done"}
                put(str(part.get("callID") or part.get("id") or "") or None, step)
    return {"steps": steps, "usage": usage}


def for_agent(agent_dir: Path) -> Dict[str, Any]:
    """Steps from an agent's saved event stream; empty when there is none."""
    path = agent_dir / "stdout.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"steps": [], "usage": {}}
    return from_events(lines)
