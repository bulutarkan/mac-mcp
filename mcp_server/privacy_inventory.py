"""What Mac MCP keeps on this Mac and where its data can go (#19 Privacy dashboard).

Read-only: sizes and modes are measured, nothing is created or changed. Each store
names how long it is kept; "until deleted" means the owner removes it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import paths
from .runtime_settings import (
    load_runtime_settings,
    memory_privacy,
    provider_enabled,
    run_evidence_privacy,
    server_setting,
    tool_enabled,
    usage_privacy,
)

_SIZE_FILE_LIMIT = 20_000


def _measure(targets: Iterable[Path]) -> Dict[str, Any]:
    total = files = 0
    exists = False
    modes: List[str] = []
    for root in targets:
        if not root.exists() or root.is_symlink():
            continue
        exists = True
        try:
            modes.append(oct(stat.S_IMODE(root.stat().st_mode)))
        except OSError:
            pass
        if root.is_file():
            files += 1
            total += root.stat().st_size
            continue
        for current, _dirs, names in os.walk(root):
            for name in names:
                if files >= _SIZE_FILE_LIMIT:
                    break
                try:
                    total += (Path(current) / name).lstat().st_size
                    files += 1
                except OSError:
                    continue
    private = bool(modes) and all(mode in {"0o700", "0o600"} for mode in modes)
    return {"exists": exists, "bytes": total, "files": files, "modes": sorted(set(modes)), "owner_only": private}


def stores() -> List[Dict[str, Any]]:
    usage = usage_privacy()
    memory = memory_privacy()
    evidence = run_evidence_privacy()
    telemetry = paths.telemetry_dir()
    rows = [
        ("tool_activity", "Tool activity", "Which tools ran, when, how long and whether they worked (no prompts or results).",
         [telemetry], f"{usage['retention_days']} days" if usage["enabled"] else "not recorded"),
        ("memory", "Memories", "What you or your agents chose to remember.", [paths.memory_dir()],
         "until deleted" if not memory["retention_days"] else f"{memory['retention_days']} days"),
        ("lessons", "Lessons", "Short lessons agents learned from past tasks.", [paths.lessons_dir()], "set in Settings › Usage"),
        ("run_evidence", "Run timelines", "Steps of plans and recipes, with screenshots only if you turned them on.",
         [paths.runs_dir()], f"{evidence['retention_days']} days" + ("" if evidence["capture"] else ", screenshots off")),
        ("agents", "Delegated agents", "Prompts, output and results of agents Mac MCP started.",
         [paths.agents_dir(), paths.agent_teams_dir()], "until deleted"),
        ("jobs", "Background jobs", "Output of long shell commands.", [paths.jobs_dir()], "until deleted"),
        ("file_history", "File change history", "Copies kept so file edits can be undone.", [paths.transactions_dir()],
         "until deleted"),
        ("provider_usage", "Agent token usage", "Tokens and cost reported by agent providers.", [paths.provider_usage_db()],
         f"{usage['retention_days']} days" if usage["enabled"] else "not recorded"),
        ("saved_logins", "Saved logins", "Sites and usernames; passwords are in your login Keychain.", [paths.vault_dir()],
         "until deleted"),
        ("audit_log", "Audit log", "Tool names, outcomes and timing, for security review.", [paths.audit_log()],
         "until deleted"),
        ("logs", "Server logs", "Mac MCP's own log files.", sorted(paths.state_dir().glob("*.log")), "rotated by size"),
    ]
    out = []
    for key, name, holds, targets, kept in rows:
        out.append({"key": key, "name": name, "holds": holds, "kept": kept,
                    "paths": [str(path) for path in targets], **_measure(targets)})
    return out


def recipients() -> List[Dict[str, Any]]:
    """Every place data can leave this Mac, and whether it is on now."""
    settings = load_runtime_settings()
    decisions = settings.get("decision_acceleration") if isinstance(settings.get("decision_acceleration"), dict) else {}
    mode = str(server_setting("public_endpoint_mode", "none") or "none")
    return [
        {"key": "ai_apps", "name": "AI apps you connect", "active": True,
         "sends": "The results of the tools each app calls: page text, file contents, screenshots it asks for."},
        {"key": "public_endpoint", "name": {"cloudflare": "Cloudflare Tunnel", "ngrok": "ngrok"}.get(mode, "Public endpoint"),
         "active": mode in {"cloudflare", "ngrok", "custom"},
         "sends": "Requests from remote AI apps such as ChatGPT pass through it, encrypted."},
        {"key": "codex", "name": "OpenAI (Codex agents)", "active": provider_enabled("codex", False),
         "sends": "Task prompts and the files and tool results those agents read."},
        {"key": "claude", "name": "Anthropic (Claude Code agents)", "active": provider_enabled("claude", False),
         "sends": "Task prompts and the files and tool results those agents read."},
        {"key": "opencode", "name": "OpenCode's model provider", "active": provider_enabled("opencode", False),
         "sends": "Task prompts and the files and tool results those agents read."},
        {"key": "voice", "name": "Groq (voice transcription)", "active": tool_enabled("ask_user_voice", default=False),
         "sends": "Audio of voice questions, when you answer by voice."},
        {"key": "decisions", "name": "OpenAI (decision acceleration)", "active": decisions.get("enabled") is True,
         "sends": "Short descriptions of ambiguous on-screen targets to pick the right one."},
    ]
