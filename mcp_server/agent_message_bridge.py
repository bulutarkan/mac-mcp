"""Team messages inside a delegated agent's own CLI (#208): a per-agent hook after each native tool call.

A delegated agent mostly runs its CLI's own tools (Bash, Read, Edit) and may never call Mac MCP,
so for each provider attempt Mac MCP gives the CLI a private hook that, after every tool call,
asks Mac MCP for team messages not yet shown to this agent and adds them as a short note to the
model's next step. The running tool is never interrupted and no new user turn is started.

Proven per provider with a real model (the note was repeated back):
- Claude Code: a PostToolUse hook in a --settings file; hookSpecificOutput.additionalContext.
- Codex: hooks.json in the agent's private CODEX_HOME. Codex runs only trusted hooks, so this one
  hook is marked trusted through Codex's own app-server API (hooks/list, config/batchWrite), never
  by skipping Codex's hook trust. A full-mode agent normally runs on the owner's own ~/.codex,
  which Mac MCP never writes; with messages on it runs on a private copy of the owner's config.
- OpenCode: a plugin named by path in the agent's private opencode.jsonc; tool.execute.after
  appends the note to the tool output the model reads. A restricted OpenCode agent normally runs
  with --pure (no plugins); with messages on it runs without it, inside the same sandbox.
Other providers (Grok, Kimi) are "inbox_only" until their hook is proven: they see messages in
Mac MCP tool results and with team_messages action=inbox.

The hook holds a messaging-only token (it can claim this agent's own messages and nothing else)
in a 0600 file inside the folder the agent's sandbox already reads; it never sees the scoped
Mac MCP credential. Every file is removed and the token revoked when the attempt ends.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import team_messages as tm

LOG = logging.getLogger(__name__)

HOOK_PATH = "/team-messages/hook"
HOOK_HEADER = "X-Mac-MCP-Team-Token"
HOOK_TIMEOUT_S = 3
CODEX_TRUST_TIMEOUT_S = 20.0
# Providers whose CLI can add a note to the model's next step (a hook, plugin, extension or
# context file). Kimi Code's PostToolUse hook is observe-only, so a Kimi agent is told to check its
# inbox instead (tools_agents._team_messaging_instruction).
HOOK_PROVIDERS = ("claude", "codex", "opencode", "grok", "pi", "kilo", "cline", "goose", "antigravity")
PROVEN_PROVIDERS = HOOK_PROVIDERS


def enabled() -> bool:
    return tm.enabled()


@dataclass
class Bridge:
    member_id: str
    room_id: str
    mode: str  # hook | inbox_only
    reason: Optional[str] = None
    args: List[str] = field(default_factory=list)
    drop_flags: List[str] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason, "room": self.room_id}


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _private_file(path: Path, text: str) -> Path:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def hook_url(meta: Dict[str, Any], fmt: str) -> str:
    endpoint = str(meta.get("mcp_endpoint") or "http://127.0.0.1:8765/mcp").rstrip("/")
    base = endpoint[: -len("/mcp")] if endpoint.endswith("/mcp") else endpoint
    return f"{base}{HOOK_PATH}?format={fmt}"


def _curl_command(headers: Path, url: str) -> str:
    # Never fails the tool: no output and exit 0 when Mac MCP does not answer in time.
    return (f"/usr/bin/curl -s -m {HOOK_TIMEOUT_S} -X POST -H @{shlex.quote(str(headers))} "
            f"-H 'Content-Type: application/json' --data-binary @- {shlex.quote(url)} 2>/dev/null || true")


def prepare(agent_id: str, meta: Dict[str, Any], env: Dict[str, str], *, hook_dir: Path,
            codex_home: Optional[Path] = None, opencode_config: Optional[Path] = None,
            harness_home: Optional[Path] = None,
            trust_codex_hook: Optional[Callable[..., Optional[str]]] = None) -> Optional[Bridge]:
    """Join the agent's team room and set up its CLI hook. None when the agent is not in a team."""
    team_id = str(meta.get("team_id") or "").strip()
    if not team_id or not enabled():
        return None
    provider = str(meta.get("provider") or "").lower()
    access_mode = str(meta.get("access_mode") or "workspace_write")
    store = tm.store()
    member_id = f"agent:{agent_id}"
    room_id = tm.team_room(team_id)
    task_id = str(meta.get("team_task_id") or "").strip() or None
    role = str(meta.get("role") or "").strip() or None
    label = " ".join(part for part in (f"task {task_id}" if task_id else agent_id, f"({role})" if role else "") if part)
    store.join(room_id, member_id, label=label, kind="agent", task_id=task_id, role=role, from_start=True)
    bridge = Bridge(member_id=member_id, room_id=room_id, mode="inbox_only")
    if provider not in PROVEN_PROVIDERS:
        bridge.reason = "provider_hook_unverified"
        return bridge

    ttl = float(meta.get("timeout_s") or 3600) + 600
    try:
        token = store.issue_hook_token(member_id, ttl)
        folder = _private_dir(hook_dir)
        headers = _private_file(folder / "team-hook-headers", f"{HOOK_HEADER}: {token}\n")
        bridge.files.append(headers)
        if provider == "claude":
            settings = {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": _curl_command(headers, hook_url(meta, "claude")),
                 "timeout": HOOK_TIMEOUT_S + 2}]}]}}
            path = _private_file(folder / "team-hook-settings.json", json.dumps(settings))
            bridge.files.append(path)
            bridge.args = ["--settings", str(path)]
        elif provider == "codex":
            assert codex_home is not None
            hooks_path = codex_home / "hooks.json"
            hooks = {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": _curl_command(headers, hook_url(meta, "codex")),
                 "timeout": HOOK_TIMEOUT_S + 2}]}]}}
            _private_file(hooks_path, json.dumps(hooks))
            bridge.files.append(hooks_path)
            trust = trust_codex_hook or trust_codex_hooks
            problem = trust(codex_home=codex_home, hooks_path=hooks_path, cwd=str(meta.get("cwd") or "."),
                            env=env, binary=str(meta.get("binary") or "codex"))
            if problem:
                bridge.reason = f"codex_hook_not_trusted:{problem}"
                cleanup(bridge)
                bridge.files.clear()
                return bridge
        elif provider == "opencode":
            assert opencode_config is not None
            plugin = _private_file(folder / "mac-mcp-team-messages.js", _opencode_plugin(headers, hook_url(meta, "text")))
            bridge.files.append(plugin)
            # Named in the agent's own config by path: a plugin/ folder needs a dependency install that the
            # agent's sandbox refuses, and then OpenCode does not load it.
            config_path = _private_dir(opencode_config) / "opencode.jsonc"
            config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
            config["plugin"] = [*(config.get("plugin") or []), plugin.as_uri()]
            _private_file(config_path, json.dumps(config, ensure_ascii=False, indent=2))
            if access_mode != "full":
                # --pure loads no plugin at all; with messages on, the agent runs without it (its sandbox stays).
                bridge.drop_flags = ["--pure"]
        elif provider == "grok":
            # GROK_HOME is the agent's private home; hooks in its hooks/ folder are trusted.
            assert harness_home is not None
            hooks = {"hooks": {"PostToolUse": [{"matcher": "", "hooks": [
                {"type": "command", "command": _curl_command(headers, hook_url(meta, "claude")),
                 "timeout": HOOK_TIMEOUT_S + 2}]}]}}
            bridge.files.append(_private_file(_private_dir(harness_home / "hooks") / "mac-mcp-team-messages.json",
                                              json.dumps(hooks)))
        elif provider == "pi":
            extension = _private_file(folder / "mac-mcp-team-messages.js", _pi_extension(headers, hook_url(meta, "text")))
            bridge.files.append(extension)
            bridge.args = ["-e", str(extension)]
        elif provider == "kilo":
            assert harness_home is not None
            plugin = _private_file(folder / "mac-mcp-team-messages.js", _opencode_plugin(headers, hook_url(meta, "text")))
            bridge.files.append(plugin)
            config_path = _private_dir(harness_home / "config" / "kilo") / "kilo.jsonc"
            config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
            config["plugin"] = [*(config.get("plugin") or []), plugin.as_uri()]
            _private_file(config_path, json.dumps(config, ensure_ascii=False, indent=2))
        elif provider == "cline":
            # Cline runs an executable named after the event from <its config dir>/hooks.
            assert harness_home is not None
            script = _private_dir(harness_home / "hooks") / "PostToolUse"
            _private_file(script, "#!/bin/sh\n" + _curl_command(headers, hook_url(meta, "cline")) + "\n")
            script.chmod(0o700)
            bridge.files.append(script)
        elif provider == "goose":
            # Goose's PostToolUse hook cannot reach the model, but it reads GOOSE_MOIM_MESSAGE_FILE
            # before every model call: Mac MCP writes this agent's new messages there itself.
            note_file = folder / "team-messages.md"
            _private_file(note_file, "")
            bridge.files += [note_file, note_file.with_name(f".{note_file.name}.recent.json")]
            env["GOOSE_MOIM_MESSAGE_FILE"] = str(note_file)
            store.set_push_file(member_id, str(note_file))
            push_pending(member_id)
        elif provider == "antigravity":
            # agy reads .agents/ of every added folder; PreInvocation runs before each model call
            # (so after every batch of tools) and may add steps the model sees.
            assert harness_home is not None
            hooks = {"mac-mcp-team-messages": {"PreInvocation": [
                {"command": _curl_command(headers, hook_url(meta, "antigravity")), "timeout": HOOK_TIMEOUT_S + 2}]}}
            bridge.files.append(_private_file(_private_dir(harness_home / ".agents") / "hooks.json", json.dumps(hooks)))
        bridge.mode = "hook"
        return bridge
    except Exception as exc:  # a broken hook must never stop the agent; it falls back to the inbox
        LOG.warning("team message hook for %s not set up: %s", agent_id, exc)
        cleanup(bridge)
        bridge.files.clear()
        bridge.mode, bridge.reason = "inbox_only", f"hook_setup_failed:{type(exc).__name__}"
        return bridge


def _pi_extension(headers: Path, url: str) -> str:
    return (
        "// Mac MCP team messages (#208): after each tool, add messages from teammates to its result.\n"
        'const { readFileSync } = require("node:fs");\n'
        f"const HEADER = readFileSync({json.dumps(str(headers))}, \"utf8\").trim();\n"
        "const NAME = HEADER.split(\":\")[0];\n"
        "const TOKEN = HEADER.split(\":\").slice(1).join(\":\").trim();\n"
        "module.exports = function (pi) {\n"
        '  pi.on("tool_result", async (event) => {\n'
        "    try {\n"
        f"      const response = await fetch({json.dumps(url)}, {{\n"
        "        method: \"POST\", headers: { [NAME]: TOKEN, \"Content-Type\": \"application/json\" }, body: \"{}\",\n"
        f"        signal: AbortSignal.timeout({HOOK_TIMEOUT_S * 1000}),\n"
        "      });\n"
        "      const note = response.ok ? (await response.text()).trim() : \"\";\n"
        "      if (!note) return undefined;\n"
        "      return { content: [...(event.content || []), { type: \"text\", text: note }],\n"
        "               structuredContent: event.structuredContent, details: event.details };\n"
        "    } catch (_error) { return undefined; }\n"
        "  });\n"
        "};\n"
    )


PUSH_RECENT = 20  # messages a push file keeps, newest first
_PUSH_LOCK = threading.Lock()


def push_pending(member_id: str) -> None:
    """Add a member's unseen messages to its push file (Goose reads it before each model call).

    Goose shows the file on every turn and never empties it, so it is a view of the recent messages:
    new ones are added to it, never put in place of it. Two batches that arrive between two of the
    agent's turns are both shown (roadmap #46); the oldest leave once PUSH_RECENT newer ones are in.
    """
    path = tm.store().push_file(member_id)
    if not path:
        return
    with _PUSH_LOCK:
        messages = tm.store().claim(member_id, channel="hook")
        if not messages:
            return
        target = Path(path)
        recent_file = target.with_name(f".{target.name}.recent.json")
        try:
            earlier = json.loads(recent_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            earlier = []
        recent = (list(reversed(messages)) + [m for m in earlier if isinstance(m, dict)])[:PUSH_RECENT]
        temporary = target.with_name(f".{target.name}.{os.getpid()}")
        try:
            _private_file(temporary, json.dumps(recent, ensure_ascii=False))
            os.replace(temporary, recent_file)
            _private_file(temporary, tm.note(recent) + "\n")
            os.replace(temporary, target)
        except OSError as exc:
            # Not shown after all: the next push or the inbox brings them again.
            LOG.warning("team message push file for %s not written: %s", member_id, exc)
            tm.store().unclaim(member_id, [int(message["seq"]) for message in messages], channel="hook")


def _opencode_plugin(headers: Path, url: str) -> str:
    return (
        "// Mac MCP team messages (#208): after each tool, add messages from teammates to the tool output.\n"
        'import { readFileSync } from "node:fs";\n'
        f"const HEADER = readFileSync({json.dumps(str(headers))}, \"utf8\").trim();\n"
        "const [NAME, ...REST] = HEADER.split(\":\");\n"
        "const TOKEN = REST.join(\":\").trim();\n"
        "export const MacMcpTeamMessages = async () => ({\n"
        '  "tool.execute.after": async (_input, output) => {\n'
        "    try {\n"
        f"      const response = await fetch({json.dumps(url)}, {{\n"
        f"        method: \"POST\", headers: {{ [NAME]: TOKEN, \"Content-Type\": \"application/json\" }}, body: \"{{}}\",\n"
        f"        signal: AbortSignal.timeout({HOOK_TIMEOUT_S * 1000}),\n"
        "      });\n"
        "      const note = response.ok ? (await response.text()).trim() : \"\";\n"
        '      if (note) output.output = String(output.output ?? "") + "\\n\\n" + note;\n'
        "    } catch (_error) {}\n"
        "  },\n"
        "});\n"
    )


def trust_codex_hooks(*, codex_home: Path, hooks_path: Path, cwd: str, env: Dict[str, str], binary: str) -> Optional[str]:
    """Mark the hooks in hooks_path trusted in this private CODEX_HOME, as Codex's /hooks screen does.

    Returns None on success, else a short reason. Only hooks from hooks_path are trusted.
    """
    codex = shutil.which(binary) or binary
    proc_env = {**env, "CODEX_HOME": str(codex_home)}
    try:
        proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=proc_env, cwd=cwd, text=True)
    except OSError as exc:
        return f"app_server_unavailable:{type(exc).__name__}"
    deadline = time.monotonic() + CODEX_TRUST_TIMEOUT_S
    # readline() blocks while the app-server stays silent; ending it at the deadline makes it return.
    watchdog = threading.Timer(CODEX_TRUST_TIMEOUT_S, proc.kill)
    watchdog.daemon = True
    watchdog.start()

    def call(request_id: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"])[:200])
                return message.get("result") or {}
        raise TimeoutError(method)

    try:
        call(1, "initialize", {"clientInfo": {"name": "mac-mcp", "version": "1"}})
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        listed = call(2, "hooks/list", {"cwds": [cwd]})
        ours = [hook for entry in listed.get("data") or [] for hook in entry.get("hooks") or []
                if Path(str(hook.get("sourcePath") or "")).resolve(strict=False) == hooks_path.resolve(strict=False)]
        if not ours:
            return "hook_not_listed"
        edits = [{"keyPath": f'hooks.state."{hook["key"]}".trusted_hash', "value": hook["currentHash"],
                  "mergeStrategy": "upsert"} for hook in ours]
        call(3, "config/batchWrite", {"edits": edits, "filePath": str(codex_home / "config.toml")})
        return None
    except Exception as exc:
        return f"{type(exc).__name__}"
    finally:
        watchdog.cancel()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass


def apply_args(provider: str, cmd: List[str], args: List[str], drop_flags: Optional[List[str]] = None) -> List[str]:
    """Put the hook arguments where the CLI reads options: before Claude's '--', else before the prompt."""
    if drop_flags:
        cmd = [part for part in cmd if part not in drop_flags]
    if not args:
        return cmd
    if provider == "claude" and "--" in cmd:
        index = cmd.index("--")
        return [*cmd[:index], *args, *cmd[index:]]
    return [*cmd[:-1], *args, cmd[-1]]


def cleanup(bridge: Optional[Bridge]) -> None:
    if bridge is None:
        return
    for path in bridge.files:  # the token file goes first, whatever the database says
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    tm.store().revoke_hook_tokens(bridge.member_id)
    tm.store().set_push_file(bridge.member_id, None)


def finish(bridge: Optional[Bridge], *, terminal: bool) -> None:
    """After an attempt: revoke the hook; a finished agent also leaves the room.

    Messaging never fails an agent: if the store cannot be written, the hook token still
    expires on its own and the agent's own result stands."""
    try:
        cleanup(bridge)
        if bridge is not None and terminal:
            tm.store().leave(bridge.room_id, bridge.member_id)
    except Exception as exc:
        LOG.warning("team messages cleanup for %s failed: %s", bridge.member_id if bridge else None, exc)


# ---------------------------------------------------------------------------
# The hook's endpoint

def hook_response(member_id: Optional[str], fmt: str) -> Response:
    if member_id is None:
        return Response(status_code=401)
    messages = tm.store().claim(member_id, channel="hook")
    if not messages:
        return Response(status_code=204)
    note = tm.note(messages)
    if fmt in {"claude", "codex"}:
        return JSONResponse({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}})
    if fmt == "cline":
        return JSONResponse({"contextModification": note})
    if fmt == "antigravity":
        return JSONResponse({"injectSteps": [{"ephemeralMessage": note}]})
    return PlainTextResponse(note)


def routes() -> List[Route]:
    async def hook(request: Request) -> Response:
        # Reached only by the agent's own hook with its messaging-only token, never the scoped credential.
        import asyncio

        token = request.headers.get(HOOK_HEADER, "")
        fmt = request.query_params.get("format", "text")
        member = await asyncio.to_thread(tm.store().hook_member, token)
        return await asyncio.to_thread(hook_response, member, fmt)

    return [Route(HOOK_PATH, hook, methods=["POST"])]
