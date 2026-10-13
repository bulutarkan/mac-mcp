"""What Mac MCP keeps on this Mac and where its data can go (#19 Privacy dashboard).

Read-only: sizes and modes are measured, nothing is created or changed. Each store
names how long it is kept; "until deleted" means the owner removes it.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
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



def _directory_bytes(root: Path) -> int:
    """Apparent size of everything under root, as du -A reports it (fast even for 60k+ files).
    Falls back to adding file sizes when du is unavailable."""
    try:
        out = subprocess.run(["/usr/bin/du", "-skA", str(root)], capture_output=True, text=True, timeout=30)
        return int(out.stdout.split()[0]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        total = 0
        for current, _dirs, names in os.walk(root):
            for name in names:
                try:
                    total += (Path(current) / name).lstat().st_size
                except OSError:
                    continue
        return total


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
        files += sum(len(names) for _current, _dirs, names in os.walk(root))
        total += _directory_bytes(root)
    private = bool(modes) and all(mode in {"0o700", "0o600"} for mode in modes)
    return {"exists": exists, "bytes": total, "files": files, "modes": sorted(set(modes)), "owner_only": private}


def filevault_on() -> Any:
    """True/False from fdesetup (no admin needed), None when it cannot tell."""
    try:
        out = subprocess.run(["/usr/bin/fdesetup", "isactive"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = (out.stdout or "").strip().lower()
    return True if answer == "true" else False if answer == "false" else None


def backup_excluded(path: Path) -> Any:
    """Whether Time Machine skips this path (tmutil isexcluded), None when unknown."""
    try:
        out = subprocess.run(["/usr/bin/tmutil", "isexcluded", str(path)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout or ""
    return True if "[Excluded]" in text else False if "[Included]" in text else None


def set_backup_exclusion(exclude: bool) -> Dict[str, Any]:
    """Keep every content store out of Time Machine (sticky, per item), or put it back."""
    done: List[str] = []
    failed: List[str] = []
    for store in stores(measure=False):
        if store["key"] in _BACKED_UP_ALWAYS:
            continue
        for raw in store["paths"]:
            path = Path(raw)
            if not path.exists():
                continue
            # The sticky per-item exclusion tmutil addexclusion writes, set directly: tmutil
            # takes ~11 s per path, the attribute takes milliseconds and Time Machine reads it.
            args = (["/usr/bin/xattr", "-wx", _EXCLUDE_XATTR, _EXCLUDE_VALUE, str(path)] if exclude
                    else ["/usr/bin/xattr", "-d", _EXCLUDE_XATTR, str(path)])
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                failed.append(str(path))
                continue
            if out.returncode == 0 or (not exclude and "No such xattr" in (out.stderr or "")):
                done.append(str(path))
            else:
                failed.append(str(path))
    return {"changed": done, "failed": failed}


_EXCLUDE_XATTR = "com.apple.metadata:com_apple_backup_excludeItem"
_EXCLUDE_VALUE = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY).hex()


# Kept in backups whatever the choice: small, and needed to restore Mac MCP itself.
_BACKED_UP_ALWAYS = frozenset({"saved_logins", "audit_log", "logs"})


def stores(*, measure: bool = True) -> List[Dict[str, Any]]:
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
        ("backups", "Backups", "Copies of files kept before fixes and updates, for rolling back.",
         [paths.state_dir() / "backups"], "until deleted"),
    ]
    out = []
    for key, name, holds, targets, kept in rows:
        row = {"key": key, "name": name, "holds": holds, "kept": kept, "paths": [str(path) for path in targets],
               "deletable": key in DELETABLE}
        if measure:
            row.update(_measure(targets))
            existing = [path for path in targets if path.exists()]
            row["in_backups"] = (None if not existing else
                                 not all(backup_excluded(path) is True for path in existing))
        out.append(row)
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


# Stores the owner can move to the Trash from Settings > Privacy (or mac-mcp privacy delete).
# Settings, the saved-login index, the audit log and logs are not: they run Mac MCP itself.
DELETABLE = ("tool_activity", "memory", "lessons", "run_evidence", "agents", "jobs", "file_history", "provider_usage",
             "backups")
# The package installer keeps its own rollback copy (backups/pkg-*) and prunes it itself.
_KEPT_BACKUPS = "pkg-*"


def delete_plan(keys: Iterable[str]) -> List[Dict[str, Any]]:
    wanted = set(keys)
    return [store for store in stores() if store["key"] in wanted and store["key"] in DELETABLE]


def trash_stores(keys: Iterable[str], *, trash_root: Path, stamp: str) -> Dict[str, Any]:
    """Move the chosen stores into one dated folder in the Trash; nothing is erased."""
    folder = trash_root / f"Mac MCP data {stamp}"
    moved: List[Dict[str, str]] = []
    failed: List[Dict[str, str]] = []
    for store in delete_plan(keys):
        sources = [Path(raw) for raw in store["paths"]]
        if store["key"] == "backups":
            sources = [child for root in sources if root.is_dir() for child in sorted(root.iterdir())
                       if not child.match(_KEPT_BACKUPS)]
        for path in sources:
            raw = str(path)
            if not path.exists():
                continue
            target = folder / store["key"] / path.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                moved.append({"store": store["key"], "path": raw, "to": str(target)})
            except OSError as exc:
                failed.append({"store": store["key"], "path": raw, "error": type(exc).__name__})
    return {"folder": str(folder), "moved": moved, "failed": failed}


_PROVIDER_VENDORS = {"codex": "OpenAI (Codex)", "claude": "Anthropic (Claude Code)",
                     "opencode": "the model provider OpenCode is set up with"}


def with_provider_notice(result: Any) -> Any:
    """Name where a started agent's data goes, at the moment it starts (#19)."""
    if not isinstance(result, dict):
        return result
    providers = {str(result.get("provider") or "")} | {
        str(agent.get("provider") or "") for agent in result.get("agents") or [] if isinstance(agent, dict)}
    vendors = [_PROVIDER_VENDORS[key] for key in sorted(providers) if key in _PROVIDER_VENDORS]
    if vendors:
        result["data_notice"] = (f"This task's prompt, and the files and tool results the agent reads, go to "
                                 f"{' and '.join(vendors)} (Settings > Privacy lists every recipient).")
    return result
