"""Kimi Code and Grok Build as delegated-agent providers.

Both are agent harnesses with their own tools, run headless like the other providers:
Kimi Code `kimi -p ... --output-format stream-json` and Grok Build
`grok -p ... --output-format streaming-json` print one JSON event per line, which this
module turns into the agent's progress, result, session id and usage.

Each agent gets a private home for the CLI (KIMI_CODE_HOME / GROK_HOME) under its agent
directory: the owner's model settings and sign-in are copied in for the run (the sign-in is
removed again when the run ends), the owner's own MCP servers are left out and Mac MCP's
scoped server is added. The access mode is enforced by tools_agents' macOS Seatbelt
boundary, not by the CLI: Kimi's prompt mode runs every tool without asking.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import extra_harness_providers as extra

LABELS: Dict[str, str] = {"kimi": "Kimi Code", "grok": "Grok Build", **extra.LABELS}
PROVIDERS = frozenset(LABELS)

# Grok tools that outlive or escape one delegated run (Mac MCP schedules and fans out work itself),
# and its write tools, removed in read_only (the Seatbelt boundary refuses the writes anyway).
_GROK_LIFECYCLE_TOOLS = ("scheduler_create", "scheduler_delete", "scheduler_list", "spawn_subagent")
_GROK_WRITE_TOOLS = ("write", "search_replace")
# Grok streams these between the events that matter.
_GROK_PASSIVE_EVENTS = {"available_commands", "usage", "max_turns_reached", "thinking", "plan"}

_ENV_REFERENCE_RE = re.compile(r'^\s*(?:api_key_env|env_key)\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*$', re.M)
_MODEL_TABLE_RE = re.compile(r'^\s*\[models\.(?:"([^"]+)"|([A-Za-z0-9_.\-]+))\]\s*$', re.M)
_DEFAULT_MODEL_RE = re.compile(r'^\s*default_model\s*=\s*"([^"]+)"\s*$', re.M)


# ---------------------------------------------------------------------------
# Installation and the owner's configuration

def binary_candidates(provider: str) -> List[Optional[str]]:
    if provider in extra.LABELS:
        return extra.binary_candidates(provider)
    home = Path.home()
    name = "kimi" if provider == "kimi" else "grok"
    return [
        os.getenv("KIMI_CODE_BINARY" if provider == "kimi" else "GROK_BINARY"),
        shutil.which(name),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        str(home / ".npm-global" / "bin" / name),
        str(home / ".local" / "bin" / name),
    ]


def owner_home(provider: str) -> Path:
    """Where the owner's own CLI keeps its settings and sign-in."""
    if provider == "kimi":
        return Path(os.getenv("KIMI_CODE_HOME") or (Path.home() / ".kimi-code")).expanduser()
    return Path(os.getenv("GROK_HOME") or (Path.home() / ".grok")).expanduser()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def owner_config_text(provider: str) -> str:
    return _read_text(owner_home(provider) / "config.toml")


def referenced_env_names(config_text: str) -> List[str]:
    """API key variables the owner's model settings read (`api_key_env` in Kimi, `env_key` in Grok)."""
    return sorted(set(_ENV_REFERENCE_RE.findall(config_text or "")))


def kimi_models(config_text: str) -> Tuple[List[str], Optional[str]]:
    """Model aliases of the owner's Kimi config and its default (no tomllib: Python 3.10)."""
    aliases: List[str] = []
    for quoted, bare in _MODEL_TABLE_RE.findall(config_text or ""):
        alias = quoted or bare
        if alias and alias not in aliases:
            aliases.append(alias)
    match = _DEFAULT_MODEL_RE.search(config_text or "")
    return aliases, (match.group(1) if match else None)


def parse_grok_models(output: str) -> Tuple[List[str], Optional[str]]:
    """`grok models` lists `  - name` lines and marks the default `  * name (default)`."""
    models: List[str] = []
    default: Optional[str] = None
    for line in (output or "").splitlines():
        match = re.match(r"^\s*([-*])\s+(\S+)(\s+\(default\))?\s*$", line)
        if not match:
            continue
        models.append(match.group(2))
        if match.group(1) == "*" or match.group(3):
            default = match.group(2)
    return models, default


def grok_models(binary: str, env: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
    try:
        proc = subprocess.run(
            [binary, "--no-auto-update", "models"], capture_output=True, text=True, timeout=30,
            env=env, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return [], None
    if proc.returncode != 0:
        return [], None
    return parse_grok_models(proc.stdout)


def models(provider: str, binary: Optional[str], env: Dict[str, str]) -> Tuple[List[str], Optional[str]]:
    if provider in extra.LABELS:
        return extra.models(provider, binary, env)
    if provider == "kimi":
        return kimi_models(owner_config_text("kimi"))
    if binary:
        return grok_models(binary, env)
    return [], None


def install_roots(binary: str) -> List[Path]:
    """What the CLI must be able to read to start: its launcher, its package and, for a Node
    script, the Node installation that runs it."""
    roots: List[Path] = []

    def add(path: Path) -> None:
        if path not in roots:
            roots.append(path)

    launcher = Path(binary).expanduser()
    resolved = launcher.resolve(strict=False)
    add(launcher.parent)
    add(resolved.parent)
    for parent in list(resolved.parents)[:5]:
        if (parent / "package.json").is_file():
            add(parent)
            break
    try:
        with resolved.open("rb") as handle:
            first_line = handle.readline(200)
    except OSError:
        first_line = b""
    if first_line.startswith(b"#!") and b"node" in first_line:
        node = shutil.which("node", path=os.getenv("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")
        if node:
            add(Path(node).resolve(strict=False).parent.parent)
    return roots


def sign_in_state(provider: str, keychain_lookup: Callable[[str], Optional[str]]) -> Tuple[bool, str]:
    """Whether the CLI can reach a model: a sign-in, an API key, or a configured model provider."""
    if provider in extra.LABELS:
        return extra.sign_in_state(provider, keychain_lookup)
    home = owner_home(provider)
    config = owner_config_text(provider)
    keys = referenced_env_names(config)
    if any(os.getenv(name) or keychain_lookup(name) for name in keys):
        return True, ""
    if re.search(r'^\s*api_key\s*=\s*"[^"]+"', config, re.M):
        return True, ""
    if provider == "kimi":
        credentials = home / "credentials"
        if os.getenv("KIMI_API_KEY") or (credentials.is_dir() and any(credentials.iterdir())):
            return True, ""
        return False, (
            "Kimi Code is not signed in for Mac MCP: run `kimi login`, set KIMI_API_KEY for the server, "
            f"or add a model provider to {home / 'config.toml'}."
        )
    if os.getenv("XAI_API_KEY") or (home / "auth.json").is_file():
        return True, ""
    return False, (
        "Grok Build is not signed in for Mac MCP: run `grok login`, set XAI_API_KEY for the server, "
        f"or add a model with an env_key to {home / 'config.toml'}."
    )


# ---------------------------------------------------------------------------
# The agent's private CLI home

def private_home(state_root: Path, provider: str) -> Path:
    return state_root / ("kimi-home" if provider == "kimi" else "grok-home")


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _private_file(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def grok_config_without_mcp(config_text: str) -> str:
    """The owner's Grok config with its MCP servers removed: the agent gets only Mac MCP's."""
    kept: List[str] = []
    skipping = False
    for line in (config_text or "").splitlines():
        header = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if header:
            name = header.group(1).strip()
            skipping = name == "mcp_servers" or name.startswith("mcp_servers.")
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def credential_paths(provider: str, home: Path) -> List[Path]:
    """The sign-in copied into a private home for one run."""
    if provider == "kimi":
        return [home / "credentials"]
    return [home / "auth.json"]


def prepare_home(
    provider: str, state_root: Path, mcp_endpoint: str, scoped_token: str,
) -> Path:
    """Seed the agent's private CLI home from the owner's and add Mac MCP's scoped server."""
    if provider in extra.LABELS:
        return extra.prepare_home(provider, state_root, mcp_endpoint, scoped_token)
    home = private_home(state_root, provider)
    _private_dir(home)
    source = owner_home(provider)
    config = owner_config_text(provider)
    for path in credential_paths(provider, source):
        target = home / path.name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
        try:
            if path.is_dir():
                shutil.copytree(path, target)
                for item in [target, *target.rglob("*")]:
                    item.chmod(0o700 if item.is_dir() else 0o600)
            elif path.is_file():
                shutil.copyfile(path, target)
                target.chmod(0o600)
        except OSError:
            pass
    authorization = f"Bearer {scoped_token}" if scoped_token else ""
    if provider == "kimi":
        _private_file(home / "config.toml", config)
        server: Dict[str, Any] = {"transport": "http", "url": mcp_endpoint}
        if authorization:
            server["headers"] = {"Authorization": authorization}
        _private_file(home / "mcp.json", json.dumps({"mcpServers": {"mac-mcp": server}}, indent=2) + "\n")
    else:
        section = f"\n[mcp_servers.mac-mcp]\nurl = {json.dumps(mcp_endpoint)}\nenabled = true\n"
        if authorization:
            section += f"\n[mcp_servers.mac-mcp.headers]\nAuthorization = {json.dumps(authorization)}\n"
        _private_file(home / "config.toml", grok_config_without_mcp(config) + section)
    return home


def remove_credentials(provider: str, state_root: Path) -> None:
    """The sign-in copy does not outlive the run; the next run copies a fresh one."""
    if provider in extra.LABELS:
        extra.remove_credentials(provider, state_root)
        return
    home = private_home(state_root, provider)
    for path in [*credential_paths(provider, home), home / "mcp.json"]:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    if provider == "grok":
        # The scoped bearer token sits in the config's Mac MCP header.
        config = home / "config.toml"
        text = _read_text(config)
        if text:
            _private_file(config, grok_config_without_mcp(text))


def provider_env(
    provider: str, home: Path, state_root: Path, keychain_lookup: Callable[[str], Optional[str]],
    selected_model: str = "",
) -> Dict[str, str]:
    """Variables the CLI needs: its private home and the API keys its model settings name."""
    if provider in extra.LABELS:
        return extra.provider_env(provider, home, state_root, keychain_lookup, model=selected_model)
    env = {
        "HOME": str(state_root / "home"),
        "TMPDIR": str(state_root / "tmp"),
    }
    names = set(referenced_env_names(owner_config_text(provider)))
    if provider == "kimi":
        env["KIMI_CODE_HOME"] = str(home)
        env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
        names.add("KIMI_API_KEY")
    else:
        env["GROK_HOME"] = str(home)
        names.add("XAI_API_KEY")
    for name in sorted(names):
        value = os.getenv(name) or keychain_lookup(name)
        if value:
            env[name] = value
    for path in (state_root / "home", state_root / "tmp"):
        _private_dir(path)
    return env


# ---------------------------------------------------------------------------
# Command line

def command(meta: Dict[str, Any], prompt: str) -> List[str]:
    provider = str(meta.get("provider") or "")
    if provider in extra.LABELS:
        return extra.command(meta, prompt)
    binary = str(meta["binary"])
    access_mode = str(meta.get("access_mode") or "workspace_write")
    model = str(meta.get("model") or "").strip()
    resume = str(meta.get("resume_session_id") or "").strip()
    if provider == "kimi":
        cmd = [binary, "--prompt", prompt, "--output-format", "stream-json"]
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--session", resume]
        cwd = str(meta.get("cwd") or "")
        for root in meta.get("extra_roots") or ():
            if root != cwd:
                cmd += ["--add-dir", str(root)]
        return cmd
    cmd = [
        binary, "--no-auto-update", "--single", prompt, "--output-format", "streaming-json",
        # Headless runs cannot answer permission prompts; restricted modes are bounded by the
        # Seatbelt profile and full mode is unrestricted by design (as for Claude Code).
        "--permission-mode", "bypassPermissions",
        "--cwd", str(meta["cwd"]),
    ]
    removed = list(_GROK_LIFECYCLE_TOOLS)
    if access_mode == "read_only":
        removed += list(_GROK_WRITE_TOOLS)
    cmd += ["--disallowed-tools", ",".join(removed)]
    if model:
        cmd += ["--model", model]
    reasoning = str(meta.get("reasoning") or "").strip().lower()
    if reasoning and reasoning != "none":
        cmd += ["--reasoning-effort", reasoning]
    if resume:
        cmd += ["--resume", resume]
    return cmd


# ---------------------------------------------------------------------------
# Events

def _note_tool(meta: Dict[str, Any], name: Any, now: float) -> None:
    meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
    if name:
        meta["last_tool"] = str(name)
    if not meta.get("first_tool_at"):
        meta["first_tool_at"] = now
    meta["phase"] = "tool"


def _kimi_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return ""


def apply_event(meta: Dict[str, Any], event: Dict[str, Any], now: float, note_unknown: Callable[[Dict[str, Any], str], None]) -> None:
    provider = str(meta.get("provider") or "")
    if provider in extra.LABELS:
        extra.apply_event(meta, event, now, note_unknown)
        return
    if provider == "kimi":
        role = str(event.get("role") or "")
        if role == "assistant":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            calls = event.get("tool_calls") if isinstance(event.get("tool_calls"), list) else []
            for call in calls:
                function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
                _note_tool(meta, function.get("name"), now)
            if not calls:
                meta["phase"] = "finalizing"
        elif role == "tool":
            meta["phase"] = "reasoning"
        elif role == "meta":
            kind = str(event.get("type") or "")
            if kind == "session.resume_hint" and event.get("session_id"):
                meta["session_id"] = str(event["session_id"])
            elif kind not in {"system.version"}:
                note_unknown(meta, f"meta:{kind}")
        else:
            note_unknown(meta, role or "output")
        return

    kind = str(event.get("type") or "output")
    if kind == "tool_call":
        _note_tool(meta, event.get("toolName") or event.get("title"), now)
    elif kind == "tool_call_update":
        state = str(event.get("status") or "")
        if state == "failed":
            meta["failed_tool_calls"] = int(meta.get("failed_tool_calls") or 0) + 1
        if state in {"completed", "failed"}:
            meta["phase"] = "reasoning"
    elif kind == "text":
        meta["phase"] = "finalizing"
    elif kind == "end":
        stop = str(event.get("stopReason") or "")
        meta["stop_reason"] = stop or None
        usage = grok_usage(event.get("usage"))
        if usage:
            meta["usage"] = usage
        meta["phase"] = "finalizing"
    elif kind == "usage":
        meta["step_count"] = int(meta.get("step_count") or 0) + 1
    elif kind not in _GROK_PASSIVE_EVENTS:
        note_unknown(meta, kind)


def grok_usage(usage: Any) -> Optional[Dict[str, Any]]:
    """Grok's usage in the shape the other providers report. Missing usage stays missing, never zero."""
    if not isinstance(usage, dict) or not usage:
        return None
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": int(usage.get("reasoning_tokens") or 0),
        "cache": {
            "read": int(usage.get("cache_read_input_tokens") or 0),
            "write": int(usage.get("cache_creation_input_tokens") or 0),
        },
    }


_KIMI_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def kimi_usage_records(home: Path, session_id: Optional[str], since: float) -> List[Dict[str, Any]]:
    """Kimi Code's tokens for one run, one entry per model call, from its session journal.

    Its stream-json prints no usage; every call is a durable `usage.record` line in
    `sessions/<workspace>/<session>/agents/<agent>/wire.jsonl` under the private home. A resumed
    agent shares the home with the runs before it, so only records from `since` on count.
    The line position makes each record's id stable, so reading again never counts twice.
    """
    session = str(session_id or "").strip()
    if session and (not _KIMI_SESSION_ID_RE.match(session) or ".." in session):
        return []
    pattern = f"*/{session or '*'}/agents/*/wire.jsonl"
    since_ms = float(since or 0) * 1000.0
    records: List[Dict[str, Any]] = []
    for wire in sorted((home / "sessions").glob(pattern)):
        session_name = wire.parents[2].name
        agent_name = wire.parent.name
        try:
            lines = wire.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if '"usage.record"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = record.get("usage") if isinstance(record, dict) else None
            if record.get("type") != "usage.record" or not isinstance(usage, dict):
                continue
            stamp = record.get("time")
            if not isinstance(stamp, (int, float)) or stamp < since_ms:
                continue
            counts = {
                name: max(0, int(usage.get(key) or 0))
                for name, key in (
                    ("input", "inputOther"), ("output", "output"),
                    ("cache_read", "inputCacheRead"), ("cache_write", "inputCacheCreation"),
                )
            }
            records.append({
                "event_id": f"{session_name}:{agent_name}:{number}",
                "session_id": session_name,
                "model": str(record.get("model") or "").strip() or None,
                "timestamp": float(stamp) / 1000.0,
                **counts,
                # Kimi's input leaves cached tokens out, so the four parts add up to the call.
                "total": sum(counts.values()),
            })
    return records


def _events(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line.startswith("--- provider attempt"):
            out.append({"type": "__attempt__"})
            continue
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def extract(provider: str, stdout_path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """The final answer, the session id to resume and the usage of the last attempt."""
    if provider in extra.LABELS:
        return extra.extract(provider, stdout_path)
    events = _events(stdout_path)
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    if provider == "kimi":
        result = ""
        for event in events:
            if event.get("role") == "assistant" and not event.get("tool_calls"):
                text = _kimi_text(event.get("content")).strip()
                if text:
                    result = text
            elif event.get("role") == "meta" and event.get("type") == "session.resume_hint":
                session_id = str(event.get("session_id") or "") or session_id
        return result, session_id, None
    # Grok streams the answer in pieces; the answer is what it wrote after its last tool call.
    pieces: List[str] = []
    for event in events:
        kind = event.get("type")
        if kind in {"tool_call", "__attempt__"}:
            pieces = []
        elif kind == "text":
            pieces.append(str(event.get("data") or ""))
        elif kind == "end":
            session_id = str(event.get("sessionId") or "") or session_id
            usage = grok_usage(event.get("usage")) or usage
    return "".join(pieces).strip(), session_id, usage


def stop_reason_failure(provider: str, stdout_path: Path) -> Optional[str]:
    """A Grok run that ended for another reason than finishing its turn did not finish its task."""
    if provider in extra.LABELS:
        return extra.stop_reason_failure(provider, stdout_path)
    if provider != "grok":
        return None
    reason = None
    for event in _events(stdout_path):
        if event.get("type") == "__attempt__":
            reason = None
        elif event.get("type") == "end":
            reason = str(event.get("stopReason") or "")
    if reason and reason != "end_turn":
        return reason
    return None
