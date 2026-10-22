"""Plan quota windows (5-hour and weekly) of the subscriptions agents run on.

Read the way the providers' own CLIs read them: the Codex ChatGPT login
(``wham/usage``), the Claude Code login (``/api/oauth/usage``) and OpenCode's
stored logins. Credentials are only read, never refreshed or written: a refresh
here would race the CLI that owns the login and can revoke it. Nothing is read
from the Keychain, so no macOS prompt appears; an expired token is reported and
the CLI renews it the next time it runs.

The dashboard asks only for the sources of running teams. ``snapshot`` answers
from the cache at once and refreshes stale sources on a background thread, so a
slow provider never delays the menu's poll.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

FIVE_HOUR = "5h"
WEEKLY = "1w"
# A day separates a 5-hour window (18000s) from a weekly one (604800s).
_WEEKLY_MIN_SECONDS = 86400
REQUEST_TIMEOUT_S = 10.0

# Codex's endpoint is cheap; Claude's limiter is per account and sticky once
# tripped (429 until the account goes quiet), so it is read rarely and left
# alone for a while after a 429.
SOURCE_TTL_S = {"codex": 60.0, "opencode-openai": 60.0, "opencode-go": 120.0, "claude": 300.0}
ERROR_TTL_S = 120.0
CLAUDE_RATE_LIMIT_COOLDOWN_S = 900.0

SOURCE_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "opencode-openai": "OpenCode · ChatGPT",
    "opencode-go": "OpenCode Go",
}

AUTH_ERRORS = frozenset({"login expired", "not signed in", "key expired"})


class QuotaError(Exception):
    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.rate_limited = rate_limited


def quota_source(provider: Any, model: Any) -> Optional[str]:
    """The plan whose limits an agent of this provider and model spends."""
    name = str(provider or "").strip().lower()
    if name in ("codex", "claude"):
        return name
    if name == "opencode":
        prefix = str(model or "").strip().lower().split("/", 1)[0]
        if prefix == "openai":
            return "opencode-openai"
        if prefix == "opencode-go":
            return "opencode-go"
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: Any) -> Optional[float]:
    number = _number(value)
    if number is not None:
        return number / 1000.0 if number > 1e12 else number
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _window(kind: str, used_percent: Any, resets_at: Any) -> Optional[Dict[str, Any]]:
    used = _number(used_percent)
    if used is None:
        return None
    return {"kind": kind, "used_percent": round(max(0.0, min(100.0, used)), 1), "resets_at": _timestamp(resets_at)}


def _ordered(windows: Mapping[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [windows[kind] for kind in (FIVE_HOUR, WEEKLY) if kind in windows]


def parse_codex(body: Any) -> Dict[str, Any]:
    """``wham/usage``: windows are routed by their span, not their slot; a plan
    with only a weekly limit reports it as ``primary_window``."""
    if not isinstance(body, Mapping) or not isinstance(body.get("rate_limit"), Mapping):
        raise QuotaError("unexpected response")
    limits = body["rate_limit"]
    windows: Dict[str, Dict[str, Any]] = {}
    for key, fallback in (("primary_window", FIVE_HOUR), ("secondary_window", WEEKLY)):
        item = limits.get(key)
        if not isinstance(item, Mapping):
            continue
        span = _number(item.get("limit_window_seconds"))
        kind = fallback if span is None else (WEEKLY if span >= _WEEKLY_MIN_SECONDS else FIVE_HOUR)
        resets_at = item.get("reset_at")
        if resets_at is None and _number(item.get("reset_after_seconds")) is not None:
            resets_at = time.time() + float(item["reset_after_seconds"])
        parsed = _window(kind, item.get("used_percent"), resets_at)
        if parsed and kind not in windows:
            windows[kind] = parsed
    plan = body.get("plan_type")
    return {"plan": plan if isinstance(plan, str) else None, "windows": _ordered(windows)}


def parse_claude(body: Any) -> Dict[str, Any]:
    """``/api/oauth/usage``: ``utilization`` is already a 0-100 percentage. Team
    and Enterprise plans may report no weekly window at all."""
    if not isinstance(body, Mapping):
        raise QuotaError("unexpected response")
    error = body.get("error")
    if isinstance(error, Mapping) and error.get("type") == "rate_limit_error":
        raise QuotaError("rate limited", rate_limited=True)
    windows: Dict[str, Dict[str, Any]] = {}
    for key, kind in (("five_hour", FIVE_HOUR), ("seven_day", WEEKLY)):
        item = body.get(key)
        if isinstance(item, Mapping):
            parsed = _window(kind, item.get("utilization"), item.get("resets_at"))
            if parsed:
                windows[kind] = parsed
    for item in body.get("limits") or []:
        if not isinstance(item, Mapping):
            continue
        kind = {"session": FIVE_HOUR, "weekly": WEEKLY}.get(str(item.get("kind") or ""))
        if kind and kind not in windows:
            parsed = _window(kind, item.get("percent"), item.get("resets_at"))
            if parsed:
                windows[kind] = parsed
    return {"plan": None, "windows": _ordered(windows)}


def parse_opencode_go(body: Any) -> Dict[str, Any]:
    """``zen/go/v1/usage``: ``usage.rolling`` is the 5-hour window."""
    usage = body.get("usage") if isinstance(body, Mapping) else None
    if not isinstance(usage, Mapping) or not isinstance(usage.get("rolling"), Mapping):
        raise QuotaError("unexpected response")
    windows: Dict[str, Dict[str, Any]] = {}
    for key, kind in (("rolling", FIVE_HOUR), ("weekly", WEEKLY)):
        item = usage.get(key)
        if not isinstance(item, Mapping):
            continue
        percent = next((item[name] for name in ("percent", "usagePercent", "usedPercent") if name in item), None)
        resets_at = next((item[name] for name in ("resetsAt", "resetAt", "reset_at") if name in item), None)
        parsed = _window(kind, percent, resets_at)
        if parsed:
            windows[kind] = parsed
    return {"plan": None, "windows": _ordered(windows)}


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise QuotaError("not signed in")
    if not isinstance(data, Mapping):
        raise QuotaError("not signed in")
    return data


def _codex_auth_path() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"


def _claude_credentials_path() -> Path:
    home = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".claude") / ".credentials.json"


def _opencode_auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


_CLAUDE_UA_LOCK = threading.Lock()
_claude_user_agent: Optional[str] = None


def _claude_code_user_agent() -> str:
    """The endpoint answers only a Claude Code User-Agent; use the installed
    version so the header stays current."""
    global _claude_user_agent
    with _CLAUDE_UA_LOCK:
        if _claude_user_agent:
            return _claude_user_agent
        version = "2.1.121"
        binary = shutil.which("claude") or next(
            (str(path) for path in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude")) if path.exists()),
            None,
        )
        if binary:
            try:
                output = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5).stdout
                match = re.search(r"\d+\.\d+\.\d+", output or "")
                if match:
                    version = match.group(0)
            except (OSError, subprocess.SubprocessError):
                pass
        _claude_user_agent = f"claude-code/{version}"
        return _claude_user_agent


def _get_json(url: str, headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        body = b""
        try:
            body = exc.read(2048)
        except OSError:
            pass
        if code == 429:
            raise QuotaError("rate limited", rate_limited=True)
        if code in (401, 403):
            raise QuotaError("key expired" if b"api_key_expired" in body else "login expired")
        raise QuotaError(f"HTTP {code}")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QuotaError("offline")
    try:
        return json.loads(raw or b"null")
    except ValueError:
        raise QuotaError("unexpected response")


def _wham_usage(access_token: Any, account_id: Any) -> Dict[str, Any]:
    if not isinstance(access_token, str) or not access_token.strip():
        raise QuotaError("not signed in")
    headers = {"Authorization": f"Bearer {access_token.strip()}", "Accept": "application/json", "User-Agent": "codex_cli_rs"}
    # One login can cover several workspaces; without the account the backend
    # may answer for another one.
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-Id"] = account_id.strip()
    return parse_codex(_get_json("https://chatgpt.com/backend-api/wham/usage", headers))


def _expired(expires_ms: Any) -> bool:
    expires = _timestamp(expires_ms)
    return expires is not None and expires <= time.time()


def fetch_codex() -> Dict[str, Any]:
    tokens = _read_json(_codex_auth_path()).get("tokens")
    if not isinstance(tokens, Mapping):
        raise QuotaError("not signed in")
    return _wham_usage(tokens.get("access_token"), tokens.get("account_id"))


def fetch_claude() -> Dict[str, Any]:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    plan = None
    if not token:
        oauth = _read_json(_claude_credentials_path()).get("claudeAiOauth")
        if not isinstance(oauth, Mapping) or not isinstance(oauth.get("accessToken"), str):
            raise QuotaError("not signed in")
        if _expired(oauth.get("expiresAt")):
            raise QuotaError("login expired")
        token = oauth["accessToken"].strip()
        plan = oauth.get("subscriptionType") if isinstance(oauth.get("subscriptionType"), str) else None
    result = parse_claude(_get_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": _claude_code_user_agent(),
    }))
    result["plan"] = plan
    return result


def fetch_opencode_openai() -> Dict[str, Any]:
    entry = _read_json(_opencode_auth_path()).get("openai")
    if not isinstance(entry, Mapping) or entry.get("type") != "oauth":
        raise QuotaError("not signed in")
    if _expired(entry.get("expires")):
        raise QuotaError("login expired")
    return _wham_usage(entry.get("access"), entry.get("accountId"))


def fetch_opencode_go() -> Dict[str, Any]:
    entry = _read_json(_opencode_auth_path()).get("opencode-go")
    key = entry.get("key") if isinstance(entry, Mapping) else None
    if not isinstance(key, str) or not key.strip():
        raise QuotaError("not signed in")
    return parse_opencode_go(_get_json("https://opencode.ai/zen/go/v1/usage", {
        "Authorization": f"Bearer {key.strip()}", "Accept": "application/json", "User-Agent": "mac-mcp",
    }))


FETCHERS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "codex": fetch_codex,
    "claude": fetch_claude,
    "opencode-openai": fetch_opencode_openai,
    "opencode-go": fetch_opencode_go,
}


class QuotaCache:
    def __init__(
        self,
        fetchers: Optional[Mapping[str, Callable[[], Dict[str, Any]]]] = None,
        clock: Callable[[], float] = time.time,
        run_in_background: bool = True,
    ) -> None:
        self._fetchers = dict(fetchers or FETCHERS)
        self._clock = clock
        self._background = run_in_background
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._next_fetch: Dict[str, float] = {}
        self._inflight: set[str] = set()

    def _refresh(self, source: str) -> None:
        now = self._clock()
        previous = self._entries.get(source) or {}
        try:
            result = self._fetchers[source]()
            entry = {"windows": result.get("windows") or [], "plan": result.get("plan"), "error": None, "fetched_at": now}
            delay = SOURCE_TTL_S.get(source, 120.0)
        except QuotaError as exc:
            # A transient failure keeps the last reading; a dead login does not.
            keep = exc.message not in AUTH_ERRORS
            entry = {
                "windows": (previous.get("windows") or []) if keep else [],
                "plan": previous.get("plan"),
                "error": exc.message,
                "fetched_at": previous.get("fetched_at") if keep else None,
            }
            delay = CLAUDE_RATE_LIMIT_COOLDOWN_S if exc.rate_limited and source == "claude" else max(
                ERROR_TTL_S, SOURCE_TTL_S.get(source, 120.0))
        except Exception:
            entry = {"windows": previous.get("windows") or [], "plan": previous.get("plan"),
                     "error": "unavailable", "fetched_at": previous.get("fetched_at")}
            delay = ERROR_TTL_S
        with self._lock:
            self._entries[source] = entry
            self._next_fetch[source] = now + delay
            self._inflight.discard(source)

    def snapshot(self, sources: Iterable[str]) -> List[Dict[str, Any]]:
        wanted = [source for source in dict.fromkeys(sources) if source in self._fetchers]
        now = self._clock()
        due: List[str] = []
        with self._lock:
            for source in wanted:
                if source not in self._inflight and now >= self._next_fetch.get(source, 0.0):
                    self._inflight.add(source)
                    due.append(source)
        for source in due:
            if self._background:
                threading.Thread(target=self._refresh, args=(source,), name=f"quota-{source}", daemon=True).start()
            else:
                self._refresh(source)
        rows: List[Dict[str, Any]] = []
        with self._lock:
            for source in wanted:
                entry = self._entries.get(source)
                if entry is None:
                    continue
                rows.append({"source": source, "label": SOURCE_LABELS.get(source, source), **entry})
        return rows


_CACHE: Optional[QuotaCache] = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> QuotaCache:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = QuotaCache()
        return _CACHE


def team_quota_sources(agents: Iterable[Mapping[str, Any]], team: Mapping[str, Any]) -> List[str]:
    """Quota sources of a team's running agents, else of the team's provider."""
    team_id = team.get("team_id")
    sources: List[str] = []
    for agent in agents:
        if agent.get("team_id") != team_id or agent.get("status") not in ("starting", "running"):
            continue
        source = quota_source(agent.get("provider"), agent.get("model"))
        if source and source not in sources:
            sources.append(source)
    if not sources:
        source = quota_source(team.get("provider"), team.get("model"))
        if source:
            sources.append(source)
    return sources


def teams_quota(agents: List[Mapping[str, Any]], teams: List[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Per running team, the cached quota of each plan its agents use."""
    if os.environ.get("MAC_MCP_PROVIDER_QUOTA", "").strip().lower() in ("0", "off", "false"):
        return {}
    per_team: Dict[str, List[str]] = {}
    for team in teams:
        team_id = str(team.get("team_id") or "")
        if team_id and int(team.get("pending_count") or 0) > 0:
            per_team[team_id] = team_quota_sources(agents, team)
    rows = {row["source"]: row for row in get_cache().snapshot(s for sources in per_team.values() for s in sources)}
    return {team_id: [rows[s] for s in sources if s in rows] for team_id, sources in per_team.items()}
