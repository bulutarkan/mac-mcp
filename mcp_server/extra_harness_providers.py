"""More headless coding-agent harnesses: pi, Kilo Code, Antigravity, Cline and Goose.

Keep per-agent model credentials and Mac MCP scoped MCP connections isolated from
the owner's CLI configuration. No provider-specific sandbox guarantee is assumed:
tools_agents.py enforces the macOS process boundary for restricted access.

Antigravity (`agy`) is the exception to a private home: its Google sign-in lives in the
owner's macOS Keychain and ~/.gemini, so it runs with the owner's HOME, and Mac MCP's
scoped server reaches it as an added workspace folder (`--add-dir`) holding
`.agents/mcp_config.json`; the owner's own agy configuration is never changed. It runs
with full access only: agy reads its sign-in with /usr/bin/security, which the restricted
boundary refuses so that no agent can read the owner's other Keychain secrets.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Cline 3.0's --id opens its interactive chat, which --json refuses: no headless resume.
NO_RESUME = frozenset({"cline"})
LABELS = {
    "pi": "pi", "kilo": "Kilo Code", "antigravity": "Antigravity",
    "cline": "Cline", "goose": "Goose",
}
BINARIES = {"pi": "pi", "kilo": "kilo", "antigravity": "agy", "cline": "cline", "goose": "goose"}
_ENV_KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
             "GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "CLINE_API_KEY")


def binary_candidates(provider: str) -> List[Optional[str]]:
    name = BINARIES[provider]
    return [os.getenv(f"MAC_MCP_{provider.upper()}_BINARY"), shutil.which(name),
            f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}",
            str(Path.home() / ".local" / "bin" / name)]


def owner_home(provider: str) -> Path:
    root = Path.home()
    return {
        "pi": root / ".pi" / "agent",
        "kilo": root / ".config" / "kilo",
        "antigravity": root / ".gemini",
        "cline": Path(os.getenv("CLINE_DIR") or str(root / ".cline")),
        "goose": Path(os.getenv("GOOSE_PATH_ROOT") or str(root / ".config" / "goose")),
    }[provider]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_key(keychain_lookup: Callable[[str], Optional[str]]) -> bool:
    return any(os.getenv(k) or keychain_lookup(k) for k in _ENV_KEYS)


def sign_in_state(provider: str, keychain_lookup: Callable[[str], Optional[str]]) -> Tuple[bool, str]:
    home = owner_home(provider)
    key_names = {
        "pi": ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        "kilo": ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        "goose": ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        "antigravity": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "cline": ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
    }[provider]
    if any(os.getenv(k) or keychain_lookup(k) for k in key_names):
        return True, ""
    known = {
        "pi": (home / "auth.json").is_file(),
        "kilo": (home / "auth.json").is_file(),
        "cline": (home / "data" / "settings" / "providers.json").is_file(),  # `cline auth` keeps its keys here
        "goose": (home / "config.yaml").is_file(),
        # Antigravity can use its own local account; presence of a state dir is
        # only a hint. The actual sign-in must be verified by a real CLI run.
        "antigravity": (home / "antigravity-cli" / "settings.json").is_file(),  # Owner sign-in still verified on run.
    }[provider]
    if known:
        return True, ""
    return False, f"{LABELS[provider]} has no detected model credentials; configure its CLI sign-in or a supported API key."


def models(provider: str, binary: Optional[str], env: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
    """Do not invent model names or query billable APIs on every catalog call."""
    if provider == "kilo":
        try:
            obj = json.loads(_read(owner_home(provider) / "kilo.json"))
            model = str(obj.get("model") or "").strip()
            return ([model] if model else []), (model or None)
        except (TypeError, ValueError):
            return [], None
    if provider == "goose":
        model = os.getenv("GOOSE_MODEL", "").strip()
        return ([model] if model else []), (model or None)
    return [], None


def _private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _copy_auth(source: Path, dest: Path) -> None:
    """Only known credential files, never arbitrary plugins/hooks/MCP definitions."""
    if source.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        dest.chmod(0o600)


def _mcp_json(url: str, token: str) -> Dict[str, Any]:
    return {"type": "remote", "url": url,
            "headers": {"Authorization": f"Bearer {token}"}}


def prepare_home(provider: str, state_root: Path, endpoint: str, token: str) -> Path:
    home = state_root / (provider + "-home")
    _private_dir(home)
    url = str(endpoint)
    if provider == "pi":
        agent = home / "agent"
        _private_dir(agent)
        _copy_auth(owner_home("pi") / "auth.json", agent / "auth.json")
        _copy_auth(owner_home("pi") / "models.json", agent / "models.json")
        _private_file(agent / "mcp.json", json.dumps({"mcpServers": {
            # direct: each tool is its own pi tool (mcp__mac_mcp__<tool>), not one lazy proxy tool.
            "mac-mcp": {"url": url, "headers": {"Authorization": f"Bearer {token}"}, "exposure": "direct"}
        }}))
    elif provider == "kilo":
        config = home / "config" / "kilo" / "kilo.jsonc"
        _private_file(config, json.dumps({
            "mcp": {"mac-mcp": _mcp_json(url, token)},
            "permission": {"task": "deny", "external_directory": "deny"},
        }))
    elif provider == "cline":
        data = home / "data"
        settings = data / "settings"
        _private_dir(settings)
        _private_file(settings / "cline_mcp_settings.json", json.dumps({
            "mcpServers": {"mac-mcp": {
                "transport": {"type": "streamableHttp", "url": url,
                              "headers": {"Authorization": f"Bearer {token}"}},
                "timeout": 300,
            }}
        }))
        _private_file(settings / "global-settings.json",
                      json.dumps({"telemetryOptOut": True, "autoUpdateEnabled": False}))
        # The owner's `cline auth` sign-in, for this run only; without it the key comes from the environment.
        _copy_auth(owner_home("cline") / "data" / "settings" / "providers.json", settings / "providers.json")
    elif provider == "goose":
        conf = home / "config"
        _private_dir(conf)
        # YAML without PyYAML and without inheriting owner's arbitrary extensions.
        _private_file(conf / "config.yaml",
                      "GOOSE_MODE: auto\nGOOSE_TELEMETRY_ENABLED: false\nextensions:\n"
                      "  developer:\n    enabled: true\n    type: platform\n    name: developer\n    bundled: true\n"
                      "  mac-mcp:\n    enabled: true\n    type: streamable_http\n"
                      "    name: mac-mcp\n    uri: " + json.dumps(url) +
                      "\n    headers:\n      Authorization: " + json.dumps("Bearer " + token) +
                      "\n    timeout: 300\n")
    else:
        # Antigravity reads `.agents/mcp_config.json` of every workspace folder, including
        # one added with --add-dir; the owner's ~/.gemini/config/mcp_config.json is untouched.
        server: Dict[str, Any] = {"serverUrl": url}
        if token:
            server["headers"] = {"Authorization": f"Bearer {token}"}
        _private_dir(home / ".agents")
        _private_file(home / ".agents" / "mcp_config.json", json.dumps({"mcpServers": {"mac-mcp": server}}))
    return home


def remove_credentials(provider: str, state_root: Path) -> None:
    home = state_root / (provider + "-home")
    targets = {
        "pi": [home / "agent" / "auth.json", home / "agent" / "models.json", home / "agent" / "mcp.json"],
        "kilo": [home / "config" / "kilo" / "kilo.jsonc"],
        # Cline also saves a key it was given through the environment into providers.json.
        "cline": [home / "data" / "settings" / "cline_mcp_settings.json", home / "data" / "settings" / "providers.json"],
        "goose": [home / "config" / "config.yaml"],
        "antigravity": [home / ".agents" / "mcp_config.json"],
    }[provider]
    for path in targets:
        path.unlink(missing_ok=True)


def provider_env(provider: str, home: Path, state_root: Path,
                 keychain_lookup: Callable[[str], Optional[str]],
                 model: str = "") -> Dict[str, str]:
    out = {"HOME": str(state_root / "home"), "TMPDIR": str(state_root / "tmp")}
    for folder in (state_root / "home", state_root / "tmp"):
        _private_dir(folder)
    # Do not forward every available model credential to every delegated CLI.
    model_provider = str(model or "").split("/", 1)[0].lower() if "/" in str(model or "") else ""
    if not model_provider and provider == "goose":
        model_provider = str(os.getenv("GOOSE_PROVIDER") or "").lower()
    selected = {
        "openrouter": ("OPENROUTER_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "xai": ("XAI_API_KEY",),
    }.get(model_provider)
    if provider == "antigravity":
        # Preserve the owner's Google sign-in: do not silently switch to API billing.
        selected = ()
    elif selected is None:
        selected = ("OPENROUTER_API_KEY",)  # Explicit fallback; never leak unrelated keys.
    for key in selected:
        val = os.getenv(key) or keychain_lookup(key)
        if val:
            out[key] = val
    if provider == "pi":
        out["PI_CODING_AGENT_DIR"] = str(home / "agent")
        out["PI_CODING_AGENT_SESSION_DIR"] = str(home / "sessions")
        _private_dir(home / "sessions")
    elif provider == "kilo":
        for envname, sub in (("XDG_CONFIG_HOME", "config"),
                             ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache"),
                             ("XDG_STATE_HOME", "state")):
            path = home / sub
            _private_dir(path)
            out[envname] = str(path)
        out["KILO_DISABLE_AUTOUPDATE"] = "1"
    elif provider == "cline":
        out["CLINE_DIR"] = str(home)
        out["CLINE_SESSION_BACKEND_MODE"] = "local"
    elif provider == "goose":
        out["GOOSE_PATH_ROOT"] = str(home)
        out["GOOSE_DISABLE_KEYRING"] = "1"
        out["GOOSE_TELEMETRY_OFF"] = "1"
        if "OPENROUTER_API_KEY" in out and not os.getenv("GOOSE_PROVIDER"):
            out["GOOSE_PROVIDER"] = "openrouter"
        for key in ("GOOSE_PROVIDER", "GOOSE_MODEL"):
            if os.getenv(key):
                out[key] = str(os.environ[key])
    else:
        # agy's sign-in is the owner's (Keychain and ~/.gemini): a private HOME is signed out.
        out["HOME"] = str(Path.home())
        out["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return out


def command(meta: Dict[str, Any], prompt: str) -> List[str]:
    provider = str(meta["provider"])
    binary = str(meta["binary"])
    model = str(meta.get("model") or "").strip()
    reasoning = str(meta.get("reasoning") or "").strip()
    resume = str(meta.get("resume_session_id") or "").strip()
    cwd = str(meta.get("cwd") or "")
    if provider == "pi":
        # Not --no-extensions: pi's MCP support is a built-in extension. Its private agent dir has
        # no extensions of the owner's, and --no-approve ignores the project's own.
        cmd = [binary, "--print", "--mode", "json", "--no-skills",
               "--no-prompt-templates", "--no-context-files", "--no-approve"]
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--session", resume]
        # pi prints a JSONL stream even with --mode=json.
        return [*cmd, prompt]
    if provider == "kilo":
        cmd = [binary, "run", "--format", "json", "--auto", "--dir", cwd]
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--variant", reasoning]
        if resume:
            cmd += ["--session", resume]
        return [*cmd, prompt]
    if provider == "cline":
        cmd = [binary, "--json", "--auto-approve", "true", "--cwd", cwd, "--config",
               str(meta["private_home"]), "--data-dir", str(Path(meta["private_home"]) / "data")]
        if model:
            # Like the other agents, a model is `provider/model` (openrouter/vendor/name).
            vendor, _, name = model.partition("/")
            cmd += ["--provider", vendor, "--model", name] if name else ["--model", model]
        if reasoning:
            cmd += ["--thinking", reasoning]
        # No resume: Cline's --id switches to its interactive chat, which --json refuses.
        return [*cmd, prompt]
    if provider == "goose":
        cmd = [binary, "run", "--text", prompt, "--output-format", "stream-json",
               "--max-tool-repetitions", "8", "--max-turns", "40"]
        if model:
            vendor, _, name = model.partition("/")
            cmd += ["--provider", vendor, "--model", name] if name else ["--model", model]
        if resume:
            cmd += ["--resume", "--name", resume]
        else:
            cmd += ["--name", str(meta.get("agent_id") or "mac-mcp-agent")]
        return cmd
    # Headless runs cannot answer permission prompts; agy runs only with full access (its
    # Keychain sign-in needs /usr/bin/security, which the restricted boundary refuses).
    if meta.get("private_home"):
        # agy resolves relative paths against an added folder too; name the real one.
        prompt = (f"Working directory: {cwd}. Relative paths mean this folder. The folder "
                  f"{meta['private_home']} only holds Mac MCP's connection settings: never read or write there.\n\n"
                  + prompt)
    cmd = [binary, "--print", prompt, "--output-format", "stream-json",
           "--dangerously-skip-permissions", "--disable-slash-commands"]
    if model:
        cmd += ["--model", model]
    if reasoning:
        cmd += ["--effort", reasoning]
    if resume:
        cmd += ["--conversation", resume]
    for root in meta.get("extra_roots") or ():
        if root != cwd:
            cmd += ["--add-dir", str(root)]
    if meta.get("private_home"):
        cmd += ["--add-dir", str(meta["private_home"])]
    return cmd


def agy_tool_name(info: Any, fallback: Any = "") -> str:
    """agy calls every MCP tool through call_mcp_tool; name it as the other agents do."""
    info = info if isinstance(info, dict) else {}
    params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
    name = str(info.get("name") or fallback or "")
    if name == "call_mcp_tool" and params.get("ToolName"):
        return f"mcp__{params.get('ServerName') or 'mcp'}__{params['ToolName']}"
    return name


def _normalized_usage(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, dict) or not usage:
        return None
    def num(*keys: str) -> int:
        for key in keys:
            v = usage.get(key)
            if isinstance(v, (int, float)):
                return max(0, int(v))
        return 0
    return {"input": num("input", "input_tokens", "inputTokens"),
            "output": num("output", "output_tokens", "outputTokens"),
            "reasoning": num("reasoning", "reasoning_tokens", "thinking_tokens"),
            "cache": {"read": num("cacheRead", "cache_read_input_tokens", "cacheReadTokens", "cache_read_tokens"),
                      "write": num("cacheWrite", "cache_write_input_tokens", "cacheWriteTokens")}}


def _kilo_usage(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """Kilo (like OpenCode) reports {input, output, reasoning, cache: {read, write}} per step."""
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return {"input": int(tokens.get("input") or 0), "output": int(tokens.get("output") or 0),
            "reasoning": int(tokens.get("reasoning") or 0),
            "cache": {"read": int(cache.get("read") or 0), "write": int(cache.get("write") or 0)}}


def _added_usage(total: Any, step: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not step:
        return total if isinstance(total, dict) else None
    if not isinstance(total, dict):
        return step
    return {key: ({"read": total.get("cache", {}).get("read", 0) + step["cache"]["read"],
                   "write": total.get("cache", {}).get("write", 0) + step["cache"]["write"]}
                  if key == "cache" else int(total.get(key) or 0) + int(step.get(key) or 0))
            for key in ("input", "output", "reasoning", "cache")}


def _message_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if not isinstance(msg, list):
        return ""
    chunks: List[str] = []
    for entry in msg:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("text"), str):
            chunks.append(entry["text"])
    return "".join(chunks)


def apply_event(meta: Dict[str, Any], event: Dict[str, Any], now: float,
                note_unknown: Callable[[Dict[str, Any], str], None]) -> None:
    provider = str(meta.get("provider") or "")
    kind = str(event.get("type") or "output")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if provider == "pi":
        if kind == "tool_execution_start":
            _tool(meta, event.get("toolName"), now)
        elif kind == "tool_execution_end":
            if event.get("isError"):
                meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
            meta["phase"] = "reasoning"
        elif kind == "turn_end":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            if message.get("usage"):
                # Each turn reports its own tokens.
                meta["usage"] = _added_usage(meta.get("usage"), _normalized_usage(message["usage"]))
            if message.get("stopReason") in ("error", "aborted"):
                meta["stop_reason"] = str(message["stopReason"])
        elif kind == "session" and event.get("id"):
            meta["session_id"] = str(event["id"])
        elif kind == "agent_end":
            meta["phase"] = "finalizing"
        elif kind not in ("agent_start", "turn_start", "message_start",
                          "message_end", "message_update",
                          "tool_execution_update", "agent_settled"):
            note_unknown(meta, kind)
    elif provider == "kilo":
        if kind == "tool_use" and part.get("type") == "tool":
            _tool(meta, part.get("tool"), now)
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if state.get("status") == "error":
                meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
        elif kind == "step_finish":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            if isinstance(part.get("tokens"), dict):
                meta["usage"] = _added_usage(meta.get("usage"), _kilo_usage(part["tokens"]))
        elif kind == "text":
            meta["phase"] = "finalizing"
        elif kind not in ("step_start",):
            note_unknown(meta, kind)
    elif provider == "cline":
        if kind == "agent_event":
            nested = event.get("event") if isinstance(event.get("event"), dict) else {}
            if nested.get("contentType") == "tool" and nested.get("type") == "content_start":
                _tool(meta, nested.get("toolName"), now)
            elif nested.get("contentType") == "tool" and nested.get("type") == "content_end":
                output = nested.get("output")
                failed = bool(nested.get("error")) or (isinstance(output, dict) and bool(output.get("isError") or output.get("error"))) or (
                    isinstance(output, list) and any(isinstance(item, dict) and item.get("success") is False for item in output))
                if failed:
                    meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
                meta["phase"] = "reasoning"
            elif nested.get("type") == "iteration_end":
                meta["step_count"] = int(meta.get("step_count") or 0) + 1
            elif nested.get("contentType") == "text":
                meta["phase"] = "finalizing"
        elif kind == "run_result":
            meta["phase"] = "finalizing"
            if isinstance(event.get("usage"), dict):
                meta["usage"] = _normalized_usage(event["usage"])
            meta["stop_reason"] = str(event.get("finishReason") or "")
        elif kind != "hook_event":
            note_unknown(meta, kind)
    elif provider == "goose":
        if kind == "message":
            for block in message.get("content") or ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("toolRequest", "tool_request") and message.get("role") == "assistant":
                    call = block.get("toolCall") if isinstance(block.get("toolCall"), dict) else {}
                    value = call.get("value") if isinstance(call.get("value"), dict) else call
                    _tool(meta, value.get("name") or value.get("tool") or "tool", now)
                elif block.get("type") == "toolResponse":
                    result = block.get("toolResult") if isinstance(block.get("toolResult"), dict) else {}
                    value = result.get("value") if isinstance(result.get("value"), dict) else {}
                    if value.get("isError") or result.get("status") == "error":
                        meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
                    meta["phase"] = "reasoning"
                elif block.get("type") == "text" and message.get("role") == "assistant":
                    meta["phase"] = "finalizing"
        elif kind == "complete":
            meta["phase"] = "finalizing"
            meta["usage"] = _normalized_usage(event)
        else:
            note_unknown(meta, kind)
    else:
        # agy --output-format stream-json: the event kind is under "event",
        # step details under "step_update", and the final result is a dict.
        event_kind = str(event.get("event") or kind)
        update = event.get("step_update") if isinstance(event.get("step_update"), dict) else {}
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        session_id = event.get("conversation_id") or update.get("conversation_id") or result.get("conversation_id")
        if session_id:
            meta["session_id"] = str(session_id)
        if event_kind == "step_update":
            state = str(update.get("state") or "").upper()
            step_type = str(update.get("step_type") or "")
            if step_type == "tool" and state == "ACTIVE":
                _tool(meta, agy_tool_name(update.get("tool_info"), update.get("tool_name")), now)
            elif step_type == "tool" and state in ("DONE", "ERROR"):
                if state == "ERROR":
                    meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
                meta["phase"] = "reasoning"
            elif step_type == "agent_response" and state == "DONE":
                meta["step_count"] = int(meta.get("step_count") or 0) + 1
                meta["phase"] = "reasoning"
        elif event_kind == "result":
            meta["phase"] = "finalizing"
            if isinstance(result.get("usage"), dict):
                meta["usage"] = _normalized_usage(result["usage"])
            if str(result.get("status") or "").upper() != "SUCCESS":
                meta["stop_reason"] = str(result.get("status") or "missing_status")
        elif event_kind not in ("init",):
            note_unknown(meta, event_kind)


def _tool(meta: Dict[str, Any], name: Any, now: float) -> None:
    meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
    if name:
        meta["last_tool"] = str(name)
    if not meta.get("first_tool_at"):
        meta["first_tool_at"] = now
    meta["phase"] = "tool"


def extract(provider: str, stdout_path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    final = ""
    session = None
    usage = None
    try:
        source = stdout_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return "", None, None
    with source:
        for raw in source:
            if not raw.lstrip().startswith("{"):
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            if provider == "pi":
                if kind == "session":
                    session = str(ev.get("id") or "") or session
                if kind == "turn_end" and msg.get("role") == "assistant":
                    text = _message_text(msg.get("content")).strip()
                    if text and msg.get("stopReason") not in ("error", "aborted"):
                        final = text
                    if isinstance(msg.get("usage"), dict):
                        usage = _added_usage(usage, _normalized_usage(msg["usage"]))
            elif provider == "kilo":
                session = str(ev.get("sessionID") or "") or session
                if kind == "text" and part.get("type") == "text":
                    final += str(part.get("text") or "")
                if kind == "step_finish" and isinstance(part.get("tokens"), dict):
                    usage = _added_usage(usage, _kilo_usage(part["tokens"]))
            elif provider == "cline":
                # No session id: Cline cannot resume one headless (see command()).
                if kind == "run_result":
                    final = str(ev.get("text") or "").strip()
                    if isinstance(ev.get("usage"), dict):
                        usage = _normalized_usage(ev["usage"])
            elif provider == "goose":
                if kind == "message" and msg.get("role") == "assistant":
                    for block in msg.get("content") or ():
                        if isinstance(block, dict) and block.get("type") in ("toolRequest", "tool_request"):
                            final = ""
                        elif isinstance(block, dict) and block.get("type") == "text":
                            final += str(block.get("text") or "")
                elif kind == "complete":
                    usage = _normalized_usage(ev)
                session = session or None
            else:
                update = ev.get("step_update") if isinstance(ev.get("step_update"), dict) else {}
                result = ev.get("result") if isinstance(ev.get("result"), dict) else {}
                session = str(result.get("conversation_id") or update.get("conversation_id") or ev.get("conversation_id") or "") or session
                if ev.get("event") == "result" and result.get("status") == "SUCCESS":
                    final = str(result.get("response") or "")
                    if isinstance(result.get("usage"), dict):
                        usage = _normalized_usage(result["usage"])
                elif ev.get("status") == "SUCCESS" and isinstance(ev.get("response"), str):
                    # agy --output-format json (single result) compatibility.
                    final = ev["response"]
                    if isinstance(ev.get("usage"), dict):
                        usage = _normalized_usage(ev["usage"])
    return final.strip(), session, usage


def stop_reason_failure(provider: str, stdout_path: Path) -> Optional[str]:
    """Successful process exit is not enough for an unsuccessful agent turn."""
    try:
        source = stdout_path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    reason = None
    finished = False
    with source:
        for raw in source:
            if raw.startswith("--- provider attempt"):
                reason = None
                finished = False
                continue
            if not raw.lstrip().startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            if provider == "cline" and kind == "run_result":
                finished = True
                finish = str(event.get("finishReason") or "")
                reason = None if finish == "completed" else (finish or "unknown_finish")
            elif provider == "pi":
                if kind == "agent_end":
                    finished = True
                elif kind == "turn_end":
                    msg = event.get("message") or {}
                    if msg.get("stopReason") in ("error", "aborted"):
                        reason = str(msg["stopReason"])
            elif provider == "kilo" and kind == "step_finish":
                part = event.get("part") or {}
                if part.get("reason") == "stop":
                    finished = True
                elif part.get("reason") in ("error", "aborted"):
                    reason = str(part["reason"])
            elif provider == "goose" and kind == "complete":
                finished = True
            elif provider == "antigravity":
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                status = str(result.get("status") or event.get("status") or "").upper()
                if status == "SUCCESS":
                    finished = True
                elif status in ("ERROR", "FAILED", "CANCELLED", "TIMEOUT"):
                    reason = status
    if reason:
        return reason
    return None if finished else "missing_terminal_event"


# ---------------------------------------------------------------------------
# Tool calls, for workflow checkpoints: a native tool that is not known to only read may have
# changed something, so a run that used one is never replayed automatically.

READ_ONLY_TOOLS = {
    "antigravity": frozenset({"view_file", "list_dir", "find_by_name", "grep_search", "search_web",
                              "read_url_content", "command_status"}),
    "pi": frozenset({"read", "ls", "grep", "find"}),
    "kilo": frozenset({"read", "grep", "glob", "list", "webfetch", "websearch", "todoread", "todowrite"}),
    "cline": frozenset({"read_files", "list_files", "search_codebase", "fetch_web_content"}),
    "goose": frozenset(),
}


def tool_calls(provider: str, event: Dict[str, Any]) -> List[Tuple[str, bool]]:
    """The tools an event starts, each with whether it is one of Mac MCP's own (scoped) tools."""
    kind = str(event.get("type") or "")
    if provider == "antigravity":
        update = event.get("step_update") if isinstance(event.get("step_update"), dict) else {}
        if event.get("event") == "step_update" and update.get("step_type") == "tool" \
                and str(update.get("state") or "").upper() == "ACTIVE":
            name = agy_tool_name(update.get("tool_info"), update.get("tool_name"))
            return [(name, name.startswith("mcp__mac-mcp__"))]
    elif provider == "pi" and kind == "tool_execution_start":
        name = str(event.get("toolName") or "")
        return [(name, name.startswith("mcp__mac_mcp__"))]
    elif provider == "kilo" and kind == "tool_use":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        name = str(part.get("tool") or "")
        return [(name, name.startswith("mac-mcp_"))]
    elif provider == "cline" and kind == "agent_event":
        nested = event.get("event") if isinstance(event.get("event"), dict) else {}
        if nested.get("contentType") == "tool" and nested.get("type") == "content_start":
            name = str(nested.get("toolName") or "")
            return [(name, name.startswith("mac-mcp__"))]
    elif provider == "goose" and kind == "message":
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        calls = []
        for block in message.get("content") or ():
            if isinstance(block, dict) and block.get("type") in ("toolRequest", "tool_request"):
                call = block.get("toolCall") if isinstance(block.get("toolCall"), dict) else {}
                value = call.get("value") if isinstance(call.get("value"), dict) else call
                name = str(value.get("name") or "")
                calls.append((name, name.startswith("mac-mcp__")))
        return calls
    return []


def turn_finished(provider: str, event: Dict[str, Any]) -> bool:
    kind = str(event.get("type") or "")
    return {"antigravity": event.get("event") == "result", "pi": kind == "agent_end",
            "kilo": kind == "step_finish" and (event.get("part") or {}).get("reason") == "stop",
            "cline": kind == "run_result", "goose": kind == "complete"}.get(provider, False)


# ---------------------------------------------------------------------------
# Usage for Settings › Usage: (event id, token counts) once per reporting event. agy, Cline and
# Goose report the whole run at its end; pi each turn; Kilo (like OpenCode) each step.

def _count(value: Any) -> Optional[int]:
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_report(provider: str, event: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Optional[int]]]]:
    kind = str(event.get("type") or "")
    if provider == "antigravity" and event.get("event") == "result" and isinstance(event.get("result"), dict):
        raw = event["result"].get("usage")
        if isinstance(raw, dict):
            return "end", {"input": _count(raw.get("input_tokens")), "output": _count(raw.get("output_tokens")),
                           "reasoning": _count(raw.get("thinking_tokens")), "cache_read": _count(raw.get("cache_read_tokens")),
                           "cache_write": None, "total": _count(raw.get("total_tokens"))}
    elif provider == "pi" and kind == "turn_end" and isinstance(event.get("message"), dict):
        message = event["message"]
        raw = message.get("usage")
        if isinstance(raw, dict):
            event_id = str(message.get("responseId") or message.get("timestamp") or "")
            if event_id:
                return event_id, {"input": _count(raw.get("input")), "output": _count(raw.get("output")),
                                  "reasoning": _count(raw.get("reasoning")), "cache_read": _count(raw.get("cacheRead")),
                                  "cache_write": _count(raw.get("cacheWrite")), "total": _count(raw.get("totalTokens"))}
    elif provider == "kilo" and kind == "step_finish" and isinstance(event.get("part"), dict):
        part = event["part"]
        tokens = part.get("tokens")
        if isinstance(tokens, dict) and part.get("id"):
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            return str(part["id"]), {"input": _count(tokens.get("input")), "output": _count(tokens.get("output")),
                                     "reasoning": _count(tokens.get("reasoning")), "cache_read": _count(cache.get("read")),
                                     "cache_write": _count(cache.get("write")), "total": None}
    elif provider == "cline" and kind == "run_result" and isinstance(event.get("usage"), dict):
        raw = event["usage"]
        return "end", {"input": _count(raw.get("inputTokens")), "output": _count(raw.get("outputTokens")),
                       "reasoning": None, "cache_read": _count(raw.get("cacheReadTokens")),
                       "cache_write": _count(raw.get("cacheWriteTokens")), "total": None}
    elif provider == "goose" and kind == "complete":
        return "end", {"input": _count(event.get("input_tokens")), "output": _count(event.get("output_tokens")),
                       "reasoning": None, "cache_read": _count(event.get("cache_read_input_tokens")),
                       "cache_write": _count(event.get("cache_write_input_tokens")), "total": _count(event.get("total_tokens"))}
    return None
