"""Who works in which browser tab, for the menu's browser rows and the page overlay (#124).

The menu shows one row per tab an agent is using, named after the agent or team and its task,
with Show Tab, Take over and Stop Agent. The page overlay shows the same short name. Names are
the agent's own task titles, cut short and stripped of links, paths and addresses, because the
overlay's text is written into the page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .data_guard import redact_sensitive_text

_LABEL_TTL_S = 30.0
_LABEL_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Optional[str]]]] = {}
_LABEL_LOCK = threading.Lock()
# How long a tab keeps its row after its last call when no delegated lease holds it (a chat's tab).
ROW_LINGER_S = 20.0
MAX_ROWS = 8

_PROVIDER_NAMES = {
    "codex": "Codex",
    "opencode": "OpenCode",
    "claude": "Claude",
    "claude_code": "Claude Code",
    "chatgpt": "ChatGPT",
    "chatgpt_web": "ChatGPT",
    "chatgpt-web": "ChatGPT",
    "gemini": "Gemini",
}


def _short(value: Any, limit: int) -> Optional[str]:
    text = str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = redact_sensitive_text(text)
    text = re.sub(r"(?i)\b(?:https?|file)://\S+", "", text)
    text = re.sub(r"(?:^|\s)(?:~?/|/(?:Users|home|private|tmp|Volumes)/)\S+", " ", text)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip(" -·:")
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _provider_name(provider: Any) -> str:
    key = str(provider or "").strip().lower()
    return _PROVIDER_NAMES.get(key) or (key.replace("_", " ").title() if key else "")


def agent_label(agent_id: Optional[str], team_id: Optional[str] = None) -> Dict[str, Optional[str]]:
    """{"who": the team's name or "<Provider> agent", "task": the agent's own task title}."""
    agent = str(agent_id or "").strip()
    team = str(team_id or "").strip()
    if not agent and not team:
        return {"who": None, "task": None}
    key = (agent, team)
    now = time.monotonic()
    with _LABEL_LOCK:
        cached = _LABEL_CACHE.get(key)
        if cached and now - cached[0] < _LABEL_TTL_S:
            return dict(cached[1])
    meta: Mapping[str, Any] = {}
    team_meta: Mapping[str, Any] = {}
    try:
        from . import tools_agents  # lazily: tools_agents is heavy and imports browser modules
        if agent:
            meta = tools_agents._read_meta(agent)
        team = team or str(meta.get("team_id") or "").strip()
        if team:
            team_meta = tools_agents._read_team(team)
    except Exception:
        pass
    who = _short(team_meta.get("title"), 32) if team else None
    if not who:
        provider = _provider_name(meta.get("provider"))
        who = f"{provider} agent" if provider else "Agent"
    label = {"who": who, "task": _short(meta.get("title"), 48)}
    with _LABEL_LOCK:
        _LABEL_CACHE[key] = (now, label)
        if len(_LABEL_CACHE) > 256:
            for stale in sorted(_LABEL_CACHE, key=lambda k: _LABEL_CACHE[k][0])[:64]:
                _LABEL_CACHE.pop(stale, None)
    return dict(label)


def visual_label() -> Optional[str]:
    """The overlay's name for the caller acting now: "Research team · Compare prices"; None for a chat."""
    try:
        from .policy import current_policy_context
        context = current_policy_context()
    except Exception:
        return None
    agent = str(getattr(context, "agent_id", "") or "").strip()
    if not agent:
        return None
    label = agent_label(agent, str(getattr(context, "team_id", "") or "").strip() or None)
    text = " · ".join(part for part in (label.get("who"), label.get("task")) if part)
    return text[:80] or None


_LABELLED_DOCUMENTS: Dict[Any, bool] = {}
_LABELLED_LOCK = threading.Lock()


def visual_who_prefix(document_key: Any) -> str:
    """A statement put before a page action script that names its agent for the overlay.

    Empty when the caller is a chat and the document never had a name, so those scripts are unchanged;
    a document named before is cleared, so a later chat's actions do not show the old agent's name.
    """
    label = visual_label()
    with _LABELLED_LOCK:
        if label:
            _LABELLED_DOCUMENTS[document_key] = True
            if len(_LABELLED_DOCUMENTS) > 512:
                _LABELLED_DOCUMENTS.pop(next(iter(_LABELLED_DOCUMENTS)), None)
        elif not _LABELLED_DOCUMENTS.pop(document_key, False):
            return ""
    return f"window.__mcpVisualWho={json.dumps(label)};\n"


def _site_from_origin(origin: Any) -> Optional[str]:
    text = str(origin or "")
    host = text.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host.removeprefix("www.")[:120] or None


def _browser_from_handle(handle: str) -> Optional[str]:
    if handle.startswith("btab_chrome_"):
        return "Google Chrome"
    if handle.startswith("btab_safari_"):
        return "Safari"
    return None


def active_tab_rows(
    *,
    events: Iterable[Mapping[str, Any]],
    leases: Mapping[str, Mapping[str, Any]],
    takeovers: Mapping[str, Mapping[str, Any]],
    registry: Mapping[str, Mapping[str, Any]],
    context_for: Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]],
    now: Optional[float] = None,
    label_for: Callable[[Optional[str], Optional[str]], Dict[str, Optional[str]]] = agent_label,
) -> List[Dict[str, Any]]:
    """One row per browser tab in use: from live and just-finished calls, delegated leases and takeovers."""
    current = time.time() if now is None else float(now)
    rows: Dict[str, Dict[str, Any]] = {}

    def row_for(handle: str) -> Dict[str, Any]:
        row = rows.get(handle)
        if row is None:
            known = registry.get(handle) or {}
            row = rows[handle] = {
                "tab_handle": handle,
                "browser": known.get("browser") or _browser_from_handle(handle),
                "site": None,
                "action": None,
                "agent_id": None,
                "team_id": None,
                "session_id": None,
                "client": None,
                "running": False,
                "last_active_at": 0.0,
                "taken_over": False,
            }
        return row

    for event in events:
        status = str(event.get("status") or "")
        running = status == "running"
        stamp = float(event.get("timestamp") or event.get("started_at") or 0.0)
        if not running and current - stamp > ROW_LINGER_S:
            continue
        context = context_for(event) or {}
        handle = str(context.get("tab_handle") or "").strip()
        if not handle or str(event.get("tool") or "") == "browser_close_tab":
            continue
        row = row_for(handle)
        if stamp >= row["last_active_at"]:
            row["last_active_at"] = stamp
            row["action"] = context.get("action") or row["action"]
            for key in ("agent_id", "team_id", "session_id", "client"):
                if event.get(key):
                    row[key] = str(event.get(key))
        row["browser"] = row["browser"] or context.get("browser")
        row["site"] = row["site"] or context.get("site")
        row["running"] = row["running"] or running

    for handle, lease in leases.items():
        if not lease.get("agent_id"):
            continue
        row = row_for(str(handle))
        row["agent_id"] = row["agent_id"] or str(lease.get("agent_id"))
        row["team_id"] = row["team_id"] or (str(lease.get("team_id")) if lease.get("team_id") else None)
        row["site"] = row["site"] or _site_from_origin(lease.get("origin"))
        row["last_active_at"] = max(row["last_active_at"], float(lease.get("last_seen_at") or 0.0))

    for handle, record in takeovers.items():
        row = row_for(str(handle))
        row["taken_over"] = True
        row["running"] = False
        row["browser"] = row["browser"] or record.get("browser")
        for key in ("agent_id", "team_id", "session_id", "who", "task"):
            if record.get(key) and not row.get(key):
                row[key] = record.get(key)
        row["last_active_at"] = max(row["last_active_at"], float(record.get("taken_at") or 0.0))

    for row in rows.values():
        known = registry.get(row["tab_handle"]) or {}
        if not row["site"] and known.get("url"):
            row["site"] = _site_from_origin(known.get("url"))
        if row["agent_id"] or row["team_id"]:
            label = label_for(row["agent_id"], row["team_id"])
            row["who"] = row.get("who") or label.get("who")
            row["task"] = row.get("task") or label.get("task")
        else:
            row.setdefault("who", None)
            row.setdefault("task", None)
        row["can_stop"] = bool(row["agent_id"]) and not row["taken_over"]
        row["last_active_at"] = round(row["last_active_at"], 3)

    ordered = sorted(rows.values(), key=lambda item: (not item["running"], -item["last_active_at"]))
    return ordered[:MAX_ROWS]
