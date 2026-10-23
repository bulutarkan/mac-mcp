"""What a delegated agent did, as steps a person can read (#175 run reports).

Each provider streams its own events into the agent's stdout.log: Codex `exec --json` items,
Claude Code stream-json messages, OpenCode `run --format json` parts, Kimi Code stream-json
messages and Grok Build streaming-json updates. This module turns them into one list of steps:

    {"kind": "thinking" | "message" | "tool" | "command" | "files" | "search" | "todo" | "error",
     "title": "plain sentence", "detail": "secondary line", "text": "longer text",
     "status": "done" | "failed" | "running", "input": {name: short value}, "output": "text"}

Long values are cut; nothing here is sent anywhere, it only shapes what the owner sees.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    "knowledge_search": "Searched your folders",
    "team_messages": "Messaged teammates",
    "fleet": "Used another Mac",
    "iphone": "Used the iPhone",
    "screenshot": "Took a screenshot",
}
_CLAUDE_TOOLS = {
    "Bash": "Ran a command", "Read": "Read a file", "Write": "Wrote a file", "Edit": "Edited a file",
    "MultiEdit": "Edited a file", "Grep": "Searched files", "Glob": "Found files", "WebSearch": "Searched the web",
    "WebFetch": "Read a web page", "TodoWrite": "Updated its plan", "Task": "Started a helper",
    "NotebookEdit": "Edited a notebook",
}
_GROK_TOOLS = {"read_file": "Read a file", "list_dir": "Listed a folder", "grep": "Searched files",
               "search_tool": "Looked for a tool", "todo_write": "Updated its plan", "web_search": "Searched the web",
               "web_fetch": "Read a web page"}
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
        path = str(arguments.get("file_path") or arguments.get("notebook_path") or arguments.get("path") or "")
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


# ── Kimi Code and Grok Build ─────────────────────────────────────────────────

def _kimi_call(call: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Kimi names its tools like Claude Code (Bash, Read, Write, mcp__mac-mcp__...); arguments are a JSON string."""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = function.get("arguments")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        arguments = {}
    return _claude_tool(str(function.get("name") or ""), arguments if isinstance(arguments, dict) else {})


def _grok_call(event: Dict[str, Any]) -> Dict[str, Any]:
    name = str(event.get("toolName") or event.get("title") or "")
    raw = event.get("rawInput") if isinstance(event.get("rawInput"), dict) else {}
    inner = str(raw.get("tool_name") or "")
    if name == "use_tool" and "__" in inner:
        server, tool = inner.split("__", 1)
        return _mcp_step(server, tool, raw.get("tool_input"), "", False, True)
    if name == "run_terminal_command":
        return {"kind": "command", "title": str(raw.get("description") or "Ran a command"),
                "detail": _cut(raw.get("command") or "", 400), "output": "", "status": "running"}
    if name in {"write", "search_replace"}:
        path = str(raw.get("file_path") or raw.get("target_file") or "")
        return {"kind": "files", "title": "Wrote a file" if name == "write" else "Edited a file",
                "files": [{"path": path, "change": "add" if name == "write" else "update"}], "status": "running"}
    return {"kind": "tool", "title": _GROK_TOOLS.get(name) or _humanize(name), "detail": name,
            "input": _inputs(raw), "output": "", "status": "running"}


# ── Antigravity, pi and Goose ────────────────────────────────────────────────

_AGY_TOOLS = {"view_file": "Read a file", "list_dir": "Listed a folder", "find_by_name": "Found files",
              "grep_search": "Searched files", "search_web": "Searched the web", "read_url_content": "Read a web page",
              "command_status": "Checked a command"}
_PI_TOOLS = {"read": "Read a file", "ls": "Listed a folder", "grep": "Searched files", "find": "Found files"}


def _file_step(name: str, path: Any, wrote: bool) -> Dict[str, Any]:
    return {"kind": "files", "title": "Wrote a file" if wrote else "Edited a file",
            "files": [{"path": str(path or ""), "change": "add" if wrote else "update"}], "status": "running"}


def _agy_call(info: Dict[str, Any], fallback: Any = "") -> Dict[str, Any]:
    """agy reaches MCP servers through call_mcp_tool and names its own tools in snake_case."""
    name = str(info.get("name") or fallback or "")
    params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
    if name == "call_mcp_tool":
        return _mcp_step(str(params.get("ServerName") or ""), str(params.get("ToolName") or ""),
                         params.get("Arguments"), "", False, True)
    if name == "run_command":
        return {"kind": "command", "title": "Ran a command", "detail": _cut(params.get("CommandLine") or "", 400),
                "output": "", "status": "running"}
    if name in {"write_to_file", "replace_file_content", "multi_replace_file_content"}:
        return _file_step(name, params.get("TargetFile"), name == "write_to_file")
    return {"kind": "tool", "title": _AGY_TOOLS.get(name) or _humanize(name), "detail": name,
            "input": _inputs(params), "output": "", "status": "running"}


def _pi_call(name: str, arguments: Any) -> Dict[str, Any]:
    """pi names MCP tools mcp__<server>__<tool> (mac_mcp for mac-mcp) and its own in lower case."""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name.startswith("mcp__"):
        return _claude_tool(name, arguments)
    if name == "bash":
        return {"kind": "command", "title": "Ran a command", "detail": _cut(arguments.get("command") or "", 400),
                "output": "", "status": "running"}
    if name in {"write", "edit"}:
        return _file_step(name, arguments.get("path"), name == "write")
    return {"kind": "tool", "title": _PI_TOOLS.get(name) or _humanize(name), "detail": name,
            "input": _inputs(arguments), "output": "", "status": "running"}


def _goose_call(name: str, arguments: Any) -> Dict[str, Any]:
    """Goose names extension tools <extension>__<tool>; its developer tools are shell, write, edit, …"""
    arguments = arguments if isinstance(arguments, dict) else {}
    tool = name.split("__", 1)[1] if name.startswith("developer__") else name
    if "__" in tool:
        server, inner = tool.split("__", 1)
        return _mcp_step(server, inner, arguments, "", False, True)
    if tool == "shell":
        return {"kind": "command", "title": "Ran a command", "detail": _cut(arguments.get("command") or "", 400),
                "output": "", "status": "running"}
    if tool in {"write", "edit", "text_editor"}:
        return _file_step(tool, arguments.get("path"), tool == "write")
    return {"kind": "tool", "title": _humanize(tool), "detail": tool, "input": _inputs(arguments),
            "output": "", "status": "running"}


_COPILOT_TOOLS = {"view": "Read a file", "glob": "Found files", "grep": "Searched files",
                  "web_fetch": "Read a web page", "read_bash": "Read a command's output",
                  "semantic_code_search": "Searched the code"}


def _copilot_call(data: Dict[str, Any]) -> Dict[str, Any]:
    """Copilot names MCP tools <server>-<tool> and says which server; its own are bash, view, create, edit, …"""
    name = str(data.get("toolName") or "")
    arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    if data.get("mcpServerName"):
        return _mcp_step(str(data["mcpServerName"]), str(data.get("mcpToolName") or name), arguments, "", False, True)
    if name == "bash":
        return {"kind": "command", "title": "Ran a command", "detail": _cut(arguments.get("command") or "", 400),
                "output": "", "status": "running"}
    if name in {"create", "edit"}:
        return _file_step(name, arguments.get("path"), name == "create")
    return {"kind": "tool", "title": _COPILOT_TOOLS.get(name) or _humanize(name), "detail": name,
            "input": _inputs(arguments), "output": "", "status": "running"}


def _cline_call(name: str, arguments: Any) -> Dict[str, Any]:
    """Cline names MCP tools <server>__<tool>; its own are run_commands, editor, read_files, …"""
    arguments = arguments if isinstance(arguments, dict) else {}
    if "__" in name:
        server, tool = name.split("__", 1)
        return _mcp_step(server, tool, arguments, "", False, True)
    if name == "run_commands":
        commands = arguments.get("commands") if isinstance(arguments.get("commands"), list) else []
        return {"kind": "command", "title": "Ran a command" if len(commands) < 2 else "Ran commands",
                "detail": _cut("; ".join(str(command) for command in commands), 400), "output": "", "status": "running"}
    if name == "editor":
        return _file_step(name, arguments.get("path"), "old_text" not in arguments)
    return {"kind": "tool", "title": _humanize(name), "detail": name, "input": _inputs(arguments),
            "output": "", "status": "running"}


def _cline_output(event: Dict[str, Any]) -> Tuple[str, bool]:
    """A tool's output and whether it failed: run_commands lists {result, success} per command."""
    output = event.get("output")
    failed = bool(event.get("error"))
    if isinstance(output, list):
        failed = failed or any(isinstance(item, dict) and item.get("success") is False for item in output)
        text = "\n".join(str(item.get("result") or item.get("error") or "") if isinstance(item, dict) else str(item)
                         for item in output)
    elif isinstance(output, dict):
        failed = failed or bool(output.get("isError")) or bool(output.get("error"))
        if output.get("error"):
            return str(output["error"]), True
        text = _content_text(output.get("content")) if "content" in output else str(output.get("result") or "")
    else:
        text = str(output or "")
    if event.get("error"):
        text = str(event["error"].get("message") if isinstance(event["error"], dict) else event["error"])
    return text, failed


def _grok_output(event: Dict[str, Any]) -> str:
    parts = []
    for item in event.get("content") or []:
        if isinstance(item, dict) and isinstance(item.get("content"), dict):
            parts.append(str(item["content"].get("text") or ""))
    return "".join(parts)


def _error_message(event: Dict[str, Any]) -> str:
    """Codex {message} / {error: {message}}, OpenCode {error: {name, data: {message}}}."""
    error = event.get("error")
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        return str(data.get("message") or error.get("message") or error.get("name") or "")
    return str(error or event.get("message") or "")


def failure_reason(steps: List[Dict[str, Any]]) -> Optional[str]:
    """The last problem the agent reported, as the run's reason."""
    for step in reversed(steps):
        if step.get("kind") == "error" and step.get("text"):
            return str(step["text"]).splitlines()[0][:300]
    return None


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

    grok_text: Optional[Dict[str, Any]] = None
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        role = str(event.get("role") or "")
        # Antigravity: init, step_update and result under "event"; the answer streams in text_delta pieces
        if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict):
            update = event["step_update"]
            key = "agy:" + str(update.get("conversation_id") or "") + ":" + str(update.get("step_index") or 0)
            state = str(update.get("state") or "").upper()
            info = update.get("tool_info") if isinstance(update.get("tool_info"), dict) else {}
            if update.get("step_type") == "tool":
                if key not in by_id:
                    put(key, _agy_call(info, update.get("tool_name")))
                if state in {"DONE", "ERROR"} and key in by_id:
                    step = steps[by_id[key]]
                    error = info.get("error") if isinstance(info.get("error"), dict) else {}
                    output = str(error.get("message") or info.get("output") or "")
                    step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                    step["status"] = "failed" if state == "ERROR" else "done"
            elif update.get("step_type") == "agent_response" and update.get("text_delta"):
                if key not in by_id:
                    put(key, {"kind": "message", "title": "Said", "text": "", "status": "done", "_raw": ""})
                if key in by_id:
                    step = steps[by_id[key]]
                    step["_raw"] += str(update["text_delta"])
                    step["text"] = _cut(step["_raw"].split(_MARKER, 1)[0].strip(), MAX_TEXT)
            continue
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            res = event["result"]
            if str(res.get("status") or "") != "SUCCESS":
                put(None, {"kind": "error", "title": "Ran into a problem",
                           "text": _cut(str(res.get("response") or res.get("status") or "Unknown error"), MAX_TEXT),
                           "status": "failed"})
            raw = res.get("usage") if isinstance(res.get("usage"), dict) else {}
            usage = {"input_tokens": int(raw.get("input_tokens") or 0) + int(raw.get("cache_read_tokens") or 0),
                     "output_tokens": int(raw.get("output_tokens") or 0),
                     "reasoning_tokens": int(raw.get("thinking_tokens") or 0)}
            continue
        # pi: tool_execution_* events and assistant message_end events (thinking blocks are not the answer)
        if kind in {"tool_execution_start", "tool_execution_end"}:
            key = str(event.get("toolCallId") or "") or None
            if kind == "tool_execution_start":
                put(key, _pi_call(str(event.get("toolName") or ""), event.get("args")))
            elif key in by_id:
                step = steps[by_id[key]]
                output = _content_text((event.get("result") or {}).get("content") if isinstance(event.get("result"), dict) else event.get("result"))
                step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                step["status"] = "failed" if event.get("isError") else "done"
            continue
        if kind == "message_end" and isinstance(event.get("message"), dict):
            message = event["message"]
            if message.get("role") == "assistant":
                text = "".join(str(block.get("text") or "") for block in message.get("content") or []
                               if isinstance(block, dict) and block.get("type") == "text")
                text = text.split(_MARKER, 1)[0].strip()
                if text:
                    put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
                raw = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                if raw:
                    usage = {"input_tokens": usage.get("input_tokens", 0) + int(raw.get("input") or 0) + int(raw.get("cacheRead") or 0),
                             "output_tokens": usage.get("output_tokens", 0) + int(raw.get("output") or 0)}
            continue
        # GitHub Copilot: tool.execution_start/complete per tool, assistant.message for what it says
        if kind in {"tool.execution_start", "tool.execution_complete"} and isinstance(event.get("data"), dict):
            data = event["data"]
            key = "copilot:" + str(data.get("toolCallId") or "")
            if kind == "tool.execution_start":
                put(key, _copilot_call(data))
            elif key in by_id:
                step = steps[by_id[key]]
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                output = str(result.get("content") or error.get("message") or "")
                step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                step["status"] = "done" if data.get("success") is not False else "failed"
            continue
        if kind == "assistant.message" and isinstance(event.get("data"), dict):
            text = str(event["data"].get("content") or "").split(_MARKER, 1)[0].strip()
            if text:
                put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
            continue
        if kind == "result" and "sessionId" in event and "exitCode" in event:
            if event.get("exitCode") not in (0, None):
                put(None, {"kind": "error", "title": "Ran into a problem",
                           "text": f"GitHub Copilot exited with code {event.get('exitCode')}", "status": "failed"})
            continue
        # Cline: agent_event content_start/content_end per tool; run_result is the final answer
        if kind == "agent_event" and isinstance(event.get("event"), dict):
            inner = event["event"]
            if inner.get("contentType") == "tool" and inner.get("toolCallId"):
                key = "cline:" + str(inner["toolCallId"])
                if inner.get("type") == "content_start":
                    put(key, _cline_call(str(inner.get("toolName") or ""), inner.get("input")))
                elif inner.get("type") == "content_end" and key in by_id:
                    step = steps[by_id[key]]
                    output, failed = _cline_output(inner)
                    step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                    step["status"] = "failed" if failed else "done"
            continue
        if kind == "run_result":
            text = str(event.get("text") or "").split(_MARKER, 1)[0].strip()
            if text:
                put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
            if str(event.get("finishReason") or "completed") != "completed":
                put(None, {"kind": "error", "title": "Ran into a problem",
                           "text": _cut(str(event.get("finishReason")), MAX_TEXT), "status": "failed"})
            raw = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            usage = {"input_tokens": int(raw.get("inputTokens") or 0),
                     "output_tokens": int(raw.get("outputTokens") or 0)}
            continue
        # Goose: messages stream in pieces under one id; tool requests and responses are content blocks
        if kind == "message" and isinstance(event.get("message"), dict):
            message = event["message"]
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ == "toolRequest":
                    call = block.get("toolCall") if isinstance(block.get("toolCall"), dict) else {}
                    value = call.get("value") if isinstance(call.get("value"), dict) else call
                    put("goose:" + str(block.get("id") or ""), _goose_call(str(value.get("name") or ""), value.get("arguments")))
                elif typ == "toolResponse":
                    key = "goose:" + str(block.get("id") or "")
                    if key in by_id:
                        step = steps[by_id[key]]
                        result = block.get("toolResult") if isinstance(block.get("toolResult"), dict) else {}
                        value = result.get("value") if isinstance(result.get("value"), dict) else {}
                        output = _content_text(value.get("content"))
                        step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                        step["status"] = "failed" if value.get("isError") or result.get("status") == "error" else "done"
                elif typ == "text" and message.get("role") == "assistant":
                    key = "goose-text:" + str(message.get("id") or "")
                    if key not in by_id:
                        put(key, {"kind": "message", "title": "Said", "text": "", "status": "done", "_raw": ""})
                    if key in by_id:
                        step = steps[by_id[key]]
                        step["_raw"] += str(block.get("text") or "")
                        step["text"] = _cut(step["_raw"].split(_MARKER, 1)[0].strip(), MAX_TEXT)
            continue
        if kind == "complete" and "input_tokens" in event:
            usage = {"input_tokens": int(event.get("input_tokens") or 0),
                     "output_tokens": int(event.get("output_tokens") or 0)}
            continue
        # Kimi Code: chat messages with a role and no type
        if role in {"assistant", "tool"} and not kind:
            if role == "assistant":
                text = _content_text(event.get("content")).split(_MARKER, 1)[0].strip()
                if text:
                    put(None, {"kind": "message", "title": "Said", "text": _cut(text, MAX_TEXT), "status": "done"})
                for call in event.get("tool_calls") or []:
                    if isinstance(call, dict):
                        put(str(call.get("id") or "") or None, _kimi_call(call))
            else:
                index = by_id.get(str(event.get("tool_call_id") or ""))
                if index is not None:
                    step = steps[index]
                    output = _content_text(event.get("content"))
                    step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                    step["status"] = "done"
            continue
        # Grok Build: ACP session updates, the answer streamed in pieces
        if kind in {"tool_call", "tool_call_update"} and event.get("toolCallId"):
            grok_text = None
            key = str(event["toolCallId"])
            if kind == "tool_call":
                put(key, _grok_call(event))
            elif str(event.get("status") or "") in {"completed", "failed"} and key in by_id:
                step = steps[by_id[key]]
                output = _grok_output(event)
                step["output"] = _readable_output(output) if step["kind"] == "tool" else _cut(output, MAX_OUTPUT)
                step["status"] = "failed" if event.get("status") == "failed" else "done"
            continue
        if kind == "text" and "data" in event:
            if grok_text is None:
                grok_text = {"kind": "message", "title": "Said", "text": "", "status": "done", "_raw": ""}
                put(None, grok_text)
            grok_text["_raw"] += str(event.get("data") or "")
            grok_text["text"] = _cut(grok_text["_raw"].split(_MARKER, 1)[0].strip(), MAX_TEXT)
            continue
        if kind == "end" and isinstance(event.get("usage"), dict):
            raw = event["usage"]
            usage = {"input_tokens": int(raw.get("input_tokens") or 0) + int(raw.get("cache_read_input_tokens") or 0),
                     "output_tokens": int(raw.get("output_tokens") or 0),
                     "reasoning_tokens": int(raw.get("reasoning_tokens") or 0)}
            continue
        # Codex
        if kind in {"item.started", "item.updated", "item.completed"} and isinstance(event.get("item"), dict):
            item = event["item"]
            put(str(item.get("id") or "") or None, _codex_item(item))
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            raw = event["usage"]
            usage = {"input_tokens": int(raw.get("input_tokens") or 0), "output_tokens": int(raw.get("output_tokens") or 0),
                     "reasoning_tokens": int(raw.get("reasoning_output_tokens") or 0)}
        elif kind in {"turn.failed", "error"}:
            message = _error_message(event)
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
        elif kind == "step_finish" and isinstance(event.get("part"), dict):
            tokens = event["part"].get("tokens") if isinstance(event["part"].get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            usage = {"input_tokens": usage.get("input_tokens", 0) + int(tokens.get("input") or 0) + int(cache.get("read") or 0),
                     "output_tokens": usage.get("output_tokens", 0) + int(tokens.get("output") or 0),
                     "reasoning_tokens": usage.get("reasoning_tokens", 0) + int(tokens.get("reasoning") or 0),
                     **({"cost_usd": usage.get("cost_usd", 0.0) + float(event["part"]["cost"])}
                        if event["part"].get("cost") else ({"cost_usd": usage["cost_usd"]} if "cost_usd" in usage else {}))}
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
    for step in steps:
        step.pop("_raw", None)
    steps[:] = [step for step in steps if not (step.get("kind") == "message" and not step.get("text"))]
    return {"steps": steps, "usage": usage}


def for_agent(agent_dir: Path) -> Dict[str, Any]:
    """Steps from an agent's saved event stream; empty when there is none."""
    path = agent_dir / "stdout.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"steps": [], "usage": {}}
    return from_events(lines)
