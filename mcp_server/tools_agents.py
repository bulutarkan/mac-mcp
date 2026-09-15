from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal as signal_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi import HTTPException, status

from .policy import PROFILES, narrow_child_profile, profile_contains
from .policy_scope import (
    ResourceScope, access_mode_allows, child_scope, normalize_access_mode, scope_contains,
)
from .scoped_auth import get_scoped_credential_store
from .security import BASE_DIR, Settings, truncate
from .runtime_settings import provider_enabled, provider_setting
from . import browser_tabs

AGENTS_DIR = BASE_DIR / "agents"
TEAMS_DIR = BASE_DIR / "agent_teams"
TERMINAL_STATUSES = {"completed", "failed", "timeout", "stalled", "cancelled"}
DEFAULT_AGENT_TIMEOUT_S = 1800
MAX_AGENT_TIMEOUT_S = 7200
DEFAULT_RESULT_LIMIT = 6000
DETAILED_RESULT_LIMIT = 20000
TEAM_RESULT_LIMIT = 2000
DEFAULT_WAIT_TIMEOUT_S = 30
MAX_WAIT_TIMEOUT_S = 300
MAX_TEAM_SIZE = 10
DEFAULT_CHATGPT_TURN_BUDGET_S = 900
DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S = 1200
DEFAULT_CHATGPT_RATE_LIMIT_BACKOFF_S = 90
MAX_CHATGPT_RATE_LIMIT_BACKOFF_S = 600
DEFAULT_CHATGPT_REDUCED_CONCURRENCY_S = 900
DEFAULT_CHATGPT_START_SPACING_S = 15

_PROVIDER_NAMES = {"opencode", "codex", "chatgpt"}
_ACCESS_MODES = {"read_only", "workspace_write", "full"}
_RESULT_STYLES = {"concise", "detailed"}
_WAIT_MODES = {"all", "any", "majority"}
_AGENT_CAPABILITY_PROFILES: Dict[str, Dict[str, Any]] = {
    "browser_only": {"access_mode": "read_only", "permission_profile": "browser_only", "tool_families": ("browser",)},
    "read_only": {"access_mode": "read_only", "permission_profile": "read_only", "tool_families": None},
    "developer": {
        "access_mode": "workspace_write", "permission_profile": "developer",
        "tool_families": ("terminal", "jobs", "files", "search", "http", "agents", "memory", "skills"),
    },
    "full": {"access_mode": "full", "permission_profile": "trusted", "tool_families": None},
}
_WORKERS: Dict[str, subprocess.Popen] = {}
_WORKERS_LOCK = threading.RLock()
_META_LOCKS: Dict[str, threading.RLock] = {}
_META_LOCKS_GUARD = threading.Lock()


def _now() -> float:
    return time.time()


def _agent_dir(agent_id: str) -> Path:
    if not agent_id or "/" in agent_id or ".." in agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid agent_id.")
    return AGENTS_DIR / agent_id


def _meta_path(agent_id: str) -> Path:
    return _agent_dir(agent_id) / "meta.json"


def _meta_thread_lock(agent_id: str) -> threading.RLock:
    with _META_LOCKS_GUARD:
        return _META_LOCKS.setdefault(agent_id, threading.RLock())


@contextmanager
def _locked_meta(agent_id: str, *, create_parent: bool = False) -> Iterator[None]:
    """Serialize one agent's metadata across both threads and worker processes."""
    path = _meta_path(agent_id)
    thread_lock = _meta_thread_lock(agent_id)
    with thread_lock:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.exists():
            yield
            return
        lock_path = path.parent / ".meta.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_meta_unlocked(agent_id: str) -> Dict[str, Any]:
    path = _meta_path(agent_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt agent metadata: {agent_id}") from exc


def _write_meta_unlocked(agent_id: str, meta: Dict[str, Any]) -> None:
    path = _meta_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".meta.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_meta(agent_id: str) -> Dict[str, Any]:
    with _locked_meta(agent_id):
        return _read_meta_unlocked(agent_id)


def _write_meta(agent_id: str, meta: Dict[str, Any]) -> None:
    with _locked_meta(agent_id, create_parent=True):
        _write_meta_unlocked(agent_id, meta)


def _update_meta(
    agent_id: str,
    update: Callable[[Dict[str, Any]], Optional[bool]],
) -> Dict[str, Any]:
    """Atomically update one agent; return False from update to skip the write."""
    with _locked_meta(agent_id):
        meta = _read_meta_unlocked(agent_id)
        if update(meta) is not False:
            _write_meta_unlocked(agent_id, meta)
        return meta


def _team_dir(team_id: str) -> Path:
    if not team_id or "/" in team_id or ".." in team_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid team_id.")
    return TEAMS_DIR / team_id


def _team_meta_path(team_id: str) -> Path:
    return _team_dir(team_id) / "meta.json"


def _read_team(team_id: str) -> Dict[str, Any]:
    path = _team_meta_path(team_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent team not found: {team_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Corrupt team metadata: {team_id}") from exc


def _write_team(team_id: str, meta: Dict[str, Any]) -> None:
    path = _team_meta_path(team_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _team_summary(team_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    team = dict(meta or _read_team(team_id))
    agent_ids = list(team.get("agent_ids") or [])
    provider = str(team.get("provider") or "opencode").lower()
    access_mode = str(team.get("access_mode") or "workspace_write")
    access_info = _access_mode_info(provider, access_mode)
    counts: Dict[str, int] = {}
    for agent_id in agent_ids:
        try:
            agent_meta = _normalize(agent_id, _read_meta(agent_id))
        except HTTPException:
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        public = _public_meta(agent_id, agent_meta)
        state = str(public.get("status") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    terminal_count = sum(counts.get(state, 0) for state in TERMINAL_STATUSES)
    if agent_ids and counts.get("completed", 0) == len(agent_ids):
        team_status = "completed"
    elif agent_ids and terminal_count >= len(agent_ids):
        team_status = "completed_with_failures"
    else:
        team_status = "running"
    return {
        "team_id": team_id,
        "status": team_status,
        "title": team.get("title"),
        "provider": team.get("provider"),
        "model": team.get("model"),
        "reasoning": team.get("reasoning"),
        "project": team.get("project"),
        "access_mode": team.get("access_mode"),
        "permission_profile": team.get("permission_profile"),
        "scope": team.get("scope"),
        "access_mode_enforced": access_info["enforced"],
        "access_mode_note": access_info["note"],
        "created_at": team.get("created_at"),
        "agent_ids": agent_ids,
        "count": len(agent_ids),
        "status_counts": counts,
        "terminal_count": terminal_count,
        "parent_team_id": team.get("parent_team_id"),
    }


def _tail_text(path: Path, max_lines: int = 40, max_chars: int = 6000) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max(1, max_lines):])[-max_chars:]


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_group(pid: Optional[int], sig: int) -> None:
    if not pid:
        return
    try:
        os.killpg(int(pid), sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _reap_worker(agent_id: str, proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    finally:
        browser_tabs.release_agent_leases(agent_id)
        with _WORKERS_LOCK:
            _WORKERS.pop(agent_id, None)


def _keychain_secret(service: str, account: str) -> Optional[str]:
    if not service or not account:
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _base_env() -> Dict[str, str]:
    env = os.environ.copy()
    home = Path.home()
    user = os.getenv("USER") or home.name
    path_parts = [
        str(home / ".opencode" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    inherited = env.get("PATH", "")
    if inherited:
        path_parts.insert(0, inherited)
    env.update({
        "HOME": str(home),
        "USER": user,
        "LOGNAME": os.getenv("LOGNAME") or user,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CI": "1",
        "NO_COLOR": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "PATH": ":".join(path_parts),
    })
    root = str(BASE_DIR.parent)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (":" + existing_pythonpath if existing_pythonpath else "")

    if not env.get("OPENROUTER_API_KEY"):
        service = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_SERVICE", "openrouter-api-key").strip()
        account = os.getenv("MAC_MCP_OPENROUTER_KEYCHAIN_ACCOUNT", user).strip()
        key = _keychain_secret(service, account)
        if key:
            env["OPENROUTER_API_KEY"] = key
    return env


def _chatgpt_env() -> Dict[str, str]:
    env = _base_env()
    # This provider drives an authenticated interactive web browser. CI=1 changes
    # browser/automation behavior and can prevent ChatGPT UI controls from hydrating.
    env.pop("CI", None)
    return env


def _provider_env(agent_id: str, meta: Dict[str, Any], scoped_token: str) -> Tuple[Dict[str, str], Optional[Path]]:
    env = _chatgpt_env() if str(meta.get("provider") or "").lower() == "chatgpt" else _base_env()
    if scoped_token:
        env["MAC_MCP_AGENT_TOKEN"] = scoped_token
    else:
        env.pop("MAC_MCP_AGENT_TOKEN", None)
    cleanup_root: Optional[Path] = None
    if str(meta.get("provider") or "").lower() == "opencode":
        cleanup_root = _agent_dir(agent_id) / "provider_config"
        config_dir = cleanup_root / "opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        try:
            cleanup_root.chmod(0o700)
            config_dir.chmod(0o700)
        except OSError:
            pass
        config_path = config_dir / "opencode.jsonc"
        payload: Dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "mac-mcp": {
                    "type": "remote",
                    "url": str(meta.get("mcp_endpoint") or "http://127.0.0.1:8765/mcp"),
                    "headers": {"Authorization": f"Bearer {scoped_token}"},
                }
            },
        }
        selected_model = str(meta.get("model") or "").strip()
        if selected_model.startswith("openrouter/"):
            model_id = selected_model.removeprefix("openrouter/")
            small_model = selected_model
            models: Dict[str, Any] = {model_id: {}}
            if model_id == "nex-agi/nex-n2.5-pro:free":
                small_model = "openrouter/nex-agi/nex-n2.5-mini:free"
                models["nex-agi/nex-n2.5-mini:free"] = {}
            payload["provider"] = {
                "openrouter": {
                    "options": {"apiKey": "{env:OPENROUTER_API_KEY}"},
                    "models": models,
                }
            }
            payload["small_model"] = small_model
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path.chmod(0o600)
        env["XDG_CONFIG_HOME"] = str(cleanup_root)
    return env, cleanup_root


def _cleanup_provider_config(path: Optional[Path]) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _codex_scoped_mcp_args(meta: Dict[str, Any]) -> List[str]:
    if not meta.get("scoped_mcp"):
        return []
    endpoint = str(meta.get("mcp_endpoint") or "http://127.0.0.1:8765/mcp")
    return [
        "--config", f"mcp_servers.mac-mcp.url={json.dumps(endpoint)}",
        "--config", 'mcp_servers.mac-mcp.bearer_token_env_var="MAC_MCP_AGENT_TOKEN"',
    ]


def _find_binary(provider: str) -> Optional[str]:
    provider = provider.lower()
    home = Path.home()
    if provider == "opencode":
        candidates = [
            provider_setting("opencode", "binary_path", ""),
            os.getenv("OPENCODE_BINARY"),
            shutil.which("opencode"),
            "/opt/homebrew/bin/opencode",
            str(home / ".opencode" / "bin" / "opencode"),
            "/usr/local/bin/opencode",
        ]
    elif provider == "codex":
        candidates = [
            provider_setting("codex", "binary_path", ""),
            os.getenv("CODEX_BINARY"),
            shutil.which("codex"),
            "/opt/homebrew/bin/codex",
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/usr/local/bin/codex",
            str(home / ".npm-global" / "bin" / "codex"),
        ]
    elif provider == "chatgpt":
        candidates = [
            provider_setting("chatgpt", "binary_path", ""),
            os.getenv("CHATGPT_WEB_CLI_BINARY"),
            os.getenv("CHATGPT_CLI_BINARY"),
            shutil.which("chatgpt-web"),
            shutil.which("chatgpt"),
            str(home / "Projects" / "chatgpt-web-cli" / "bin" / "chatgpt"),
        ]
    else:
        return None
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate))
    return None


def _version(binary: Optional[str]) -> Optional[str]:
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10, env=_base_env())
        text = (proc.stdout or proc.stderr or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def _opencode_models(binary: str) -> List[str]:
    try:
        proc = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, env=_base_env())
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip() and "/" in line]
    except Exception:
        return []


def _codex_known_models() -> Tuple[List[str], Optional[str], Optional[str]]:
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return [], None, None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None, None
    default_model_match = re.search(r'^model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    default_reasoning_match = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"', text, re.MULTILINE)
    section_match = re.search(r'\[tui\.model_availability_nux\](.*?)(?:\n\[|\Z)', text, re.DOTALL)
    models: List[str] = []
    if section_match:
        models.extend(re.findall(r'^"([^"]+)"\s*=', section_match.group(1), re.MULTILINE))
    default_model = default_model_match.group(1) if default_model_match else None
    if default_model and default_model not in models:
        models.insert(0, default_model)
    return models, default_model, default_reasoning_match.group(1) if default_reasoning_match else None


def _chatgpt_cached_models(binary: str) -> Tuple[List[str], Optional[str]]:
    try:
        root = Path(binary).resolve().parent.parent
        payload = json.loads((root / "state.json").read_text(encoding="utf-8"))
        rows = payload.get("modelCache") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return [], None
        models: List[str] = []
        selected: Optional[str] = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            models.append(name)
            if row.get("selected"):
                selected = name
        return models, selected
    except Exception:
        return [], None


def _chatgpt_models(binary: str) -> Tuple[List[str], Optional[str]]:
    # Catalog/model discovery should not generate browser traffic on every MCP call.
    # A successful `chatgpt models` run persists this cache; use it first and only
    # touch the live web UI when no cache exists yet.
    cached_models, cached_selected = _chatgpt_cached_models(binary)
    if cached_models:
        return cached_models, cached_selected
    try:
        proc = subprocess.run([binary, "models", "--json"], capture_output=True, text=True, timeout=40, env=_chatgpt_env())
        if proc.returncode == 0:
            rows = json.loads(proc.stdout or "[]")
            if isinstance(rows, list):
                models: List[str] = []
                selected: Optional[str] = None
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if not name:
                        continue
                    models.append(name)
                    if row.get("selected"):
                        selected = name
                if models:
                    return models, selected
    except Exception:
        pass
    return [], None


def _chatgpt_effort(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not raw or raw == "none":
        return None
    aliases = {
        "low": "low", "medium": "medium", "high": "high",
        "xhigh": "extra-high", "extra-high": "extra-high", "extrahigh": "extra-high", "max": "extra-high",
    }
    return aliases.get(raw)


def _chatgpt_default_project() -> Optional[str]:
    configured = provider_setting("chatgpt", "default_project", "")
    value = str(configured or os.getenv("CHATGPT_SUBAGENT_PROJECT", "") or "").strip()
    return value or None


def _chatgpt_int_setting(name: str, env_name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(env_name)
    if raw is None or not str(raw).strip():
        raw = provider_setting("chatgpt", name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _chatgpt_budget_config() -> Tuple[int, int]:
    soft = _chatgpt_int_setting(
        "turn_budget_s", "CHATGPT_PROVIDER_TURN_BUDGET_S",
        DEFAULT_CHATGPT_TURN_BUDGET_S, 60, 3600,
    )
    hard = _chatgpt_int_setting(
        "hard_tool_budget_s", "CHATGPT_PROVIDER_HARD_TOOL_BUDGET_S",
        DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S, 120, 7200,
    )
    return soft, max(soft, hard)


def _chatgpt_rate_limit_reason_text(text: str) -> Optional[str]:
    normalized = str(text or "").lower().replace("’", "'")
    patterns = (
        ("requesting_too_fast", r"you(?:'re| are) (?:making )?requests too (?:quickly|fast)"),
        ("temporarily_limited", r"temporarily limited access to your conversations"),
        ("provider_rate_limited", r"chatgpt(?:_web)?_rate_limited|temporarily rate-limited"),
        ("too_many_requests", r"too many requests|http\s*429|\b429\b"),
        ("rate_limit", r"rate[- ]?limit(?:ed|ing)?"),
    )
    for reason, pattern in patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            return reason
    return None


def _log_text_since(path: Path, offset: int = 0, max_chars: int = 8000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, int(offset)))
            data = handle.read(max_chars * 4)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-max_chars:]


def _chatgpt_rate_limit_reason(
    stdout_path: Path,
    stderr_path: Path,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
) -> Optional[str]:
    text = _log_text_since(stdout_path, stdout_offset) + "\n" + _log_text_since(stderr_path, stderr_offset)
    return _chatgpt_rate_limit_reason_text(text)


def _chatgpt_cooldown_seconds(throttle_count: int) -> int:
    base = _chatgpt_int_setting(
        "rate_limit_backoff_s", "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_S",
        DEFAULT_CHATGPT_RATE_LIMIT_BACKOFF_S, 5, 600,
    )
    cap = _chatgpt_int_setting(
        "rate_limit_backoff_cap_s", "CHATGPT_PROVIDER_RATE_LIMIT_BACKOFF_CAP_S",
        MAX_CHATGPT_RATE_LIMIT_BACKOFF_S, base, 1800,
    )
    exponent = max(0, min(6, int(throttle_count) - 1))
    return min(cap, base * (2 ** exponent))


def _chatgpt_provider_state_path() -> Path:
    return AGENTS_DIR / ".chatgpt-provider-state.json"


def _update_chatgpt_provider_state(update: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = AGENTS_DIR / ".chatgpt-provider-state.lock"
    state_path = _chatgpt_provider_state_path()
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                state = {}
            update(state)
            fd, tmp_name = tempfile.mkstemp(prefix=".chatgpt-provider-state.", suffix=".tmp", dir=AGENTS_DIR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(state, ensure_ascii=False, sort_keys=True))
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(tmp_name, state_path)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            return state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _record_chatgpt_shared_throttle(cooldown_until: float, reason: str, now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else _now())
    reduced_for = _chatgpt_int_setting(
        "reduced_concurrency_s", "CHATGPT_PROVIDER_REDUCED_CONCURRENCY_S",
        DEFAULT_CHATGPT_REDUCED_CONCURRENCY_S, 60, 3600,
    )
    def mutate(state: Dict[str, Any]) -> None:
        state["throttle_count"] = int(state.get("throttle_count") or 0) + 1
        state["last_throttled_at"] = current
        state["last_throttle_reason"] = reason
        state["cooldown_until"] = max(float(state.get("cooldown_until") or 0), float(cooldown_until))
        state["reduced_until"] = max(float(state.get("reduced_until") or 0), current + reduced_for)
        state["next_allowed_at"] = max(float(state.get("next_allowed_at") or 0), float(cooldown_until))
    return _update_chatgpt_provider_state(mutate)


def _reserve_chatgpt_provider_start(now: Optional[float] = None) -> float:
    current = float(now if now is not None else _now())
    spacing = _chatgpt_int_setting(
        "post_throttle_start_spacing_s", "CHATGPT_PROVIDER_POST_THROTTLE_SPACING_S",
        DEFAULT_CHATGPT_START_SPACING_S, 1, 120,
    )
    delay = 0.0
    def mutate(state: Dict[str, Any]) -> None:
        nonlocal delay
        cooldown_until = float(state.get("cooldown_until") or 0)
        reduced_until = float(state.get("reduced_until") or 0)
        if current >= reduced_until and current >= cooldown_until:
            state["next_allowed_at"] = current
            delay = 0.0
            return
        reserved = max(current, cooldown_until, float(state.get("next_allowed_at") or 0))
        state["next_allowed_at"] = reserved + spacing
        delay = max(0.0, reserved - current)
    _update_chatgpt_provider_state(mutate)
    return delay


def _wait_chatgpt_provider_gate(agent_id: str) -> bool:
    delay = _reserve_chatgpt_provider_start()
    if delay <= 0:
        return True
    def mark_waiting(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current["phase"] = "throttled"
        current["note"] = f"ChatGPT provider cooldown active; next safe start in about {int(delay)}s."
        current["updated_at"] = _now()
        return True
    _update_meta(agent_id, mark_waiting)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        try:
            if _read_meta(agent_id).get("status") == "cancelled":
                return False
        except HTTPException:
            return False
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
    def mark_ready(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current["phase"] = "retrying" if int(current.get("retry_count") or 0) > 0 else "provider_starting"
        current["updated_at"] = _now()
        return True
    _update_meta(agent_id, mark_ready)
    return True


def _chatgpt_turn_budget_action(meta: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
    if str(meta.get("provider") or "").lower() != "chatgpt" or meta.get("checkpoint_pending"):
        return None
    current = float(now if now is not None else _now())
    turn_started = float(meta.get("turn_started_at") or meta.get("provider_started_at") or current)
    soft = int(meta.get("turn_budget_s") or DEFAULT_CHATGPT_TURN_BUDGET_S)
    hard = int(meta.get("hard_tool_budget_s") or DEFAULT_CHATGPT_HARD_TOOL_BUDGET_S)
    active_tool_started = meta.get("active_tool_started_at")
    if active_tool_started:
        if current - float(active_tool_started) >= hard:
            return "hard_tool_budget"
        if current - turn_started >= soft:
            return "wait_for_tool"
        return None
    if current - turn_started >= soft:
        return "turn_budget"
    return None


def _chatgpt_checkpoint_prompt(reason: str) -> str:
    detail = "the current tool exceeded the hard safety budget" if reason == "hard_tool_budget" else "the current turn reached its budget"
    return (
        "Continue the same delegated task from the current conversation state. This is an automatic checkpoint because "
        f"{detail}. Do not repeat completed work or duplicate external side effects. Reconcile the latest visible tool/results "
        "first, then continue toward the original goal and finish with the requested handoff when complete."
    )


def _chatgpt_checkpoint_command(meta: Dict[str, Any], reason: str) -> Optional[List[str]]:
    target = str(meta.get("provider_job_id") or meta.get("session_id") or "").strip()
    binary = str(meta.get("binary") or "").strip()
    if not target or not binary:
        return None
    cmd = [binary, "interrupt", target]
    model = str(meta.get("model") or "").strip()
    if model:
        cmd += ["--model", model]
    effort = _chatgpt_effort(meta.get("reasoning"))
    if effort:
        cmd += ["--effort", effort]
    cmd.append(_chatgpt_checkpoint_prompt(reason))
    return cmd


def _request_chatgpt_checkpoint(agent_id: str, meta: Dict[str, Any], reason: str) -> bool:
    cmd = _chatgpt_checkpoint_command(meta, reason)
    if not cmd:
        return False
    requested_at = _now()
    def mark_pending(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled" or current.get("checkpoint_pending"):
            return False
        current.update({
            "checkpoint_pending": True, "phase": "checkpointing", "last_checkpoint_reason": reason,
            "last_checkpoint_requested_at": requested_at,
            "note": "ChatGPT turn budget reached; checkpointing into a fresh turn.", "updated_at": requested_at,
        })
        return True
    latest = _update_meta(agent_id, mark_pending)
    if not latest.get("checkpoint_pending"):
        return False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=_chatgpt_env())
    except (OSError, subprocess.SubprocessError) as exc:
        proc = None
        error = str(exc)
    else:
        error = (proc.stderr or proc.stdout or "").strip()
    if proc is None or proc.returncode != 0:
        def mark_failed(current: Dict[str, Any]) -> None:
            current["checkpoint_pending"] = False
            current["checkpoint_failures"] = int(current.get("checkpoint_failures") or 0) + 1
            current["phase"] = "tool" if current.get("active_tool_started_at") else "reasoning"
            current["note"] = "Automatic ChatGPT checkpoint failed; continuing current turn without duplicate retry."
            current["last_checkpoint_error"] = str(error or "checkpoint request failed")[-500:]
            current["updated_at"] = _now()
        _update_meta(agent_id, mark_failed)
        return False
    def mark_requested(current: Dict[str, Any]) -> None:
        current["checkpoint_count"] = int(current.get("checkpoint_count") or 0) + 1
        current["last_checkpoint_at"] = _now()
        current["updated_at"] = _now()
    _update_meta(agent_id, mark_requested)
    return True


def _chatgpt_session_for_job(meta: Dict[str, Any]) -> Optional[str]:
    existing = str(meta.get("session_id") or "").strip()
    if existing:
        return existing
    job_id = str(meta.get("provider_job_id") or "").strip()
    binary = str(meta.get("binary") or "").strip()
    if not job_id or not binary:
        return None
    try:
        proc = subprocess.run([binary, "jobs", "--all", "--json"], capture_output=True, text=True, timeout=10, env=_chatgpt_env())
        rows = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("id") or "") == job_id:
            session_id = str(row.get("sessionId") or row.get("session_id") or "").strip()
            return session_id or None
    return None


def _access_mode_info(provider: str, access_mode: str) -> Dict[str, Any]:
    if provider == "codex":
        return {
            "enforced": True,
            "note": "Codex sandbox and approval policy are explicitly applied on initial and resumed runs.",
        }
    if provider == "chatgpt":
        return {
            "enforced": False,
            "note": (
                "ChatGPT Web CLI runs in the authenticated ChatGPT web account. Mac MCP access_mode is a "
                "behavioral delegation boundary for this provider, not an OS sandbox; the spawning Mac MCP "
                "scoped credential is not attached automatically."
            ),
        }
    if access_mode == "read_only":
        return {
            "enforced": False,
            "note": "OpenCode CLI has no enforceable read-only sandbox; this mode is refused.",
        }
    return {
        "enforced": False,
        "note": (
            "OpenCode access_mode is not a hard filesystem sandbox; --auto uses OpenCode's permission model "
            "and may allow access beyond cwd."
        ),
    }


def _validate_provider_access_mode(provider: str, access_mode: str) -> None:
    if provider == "opencode" and access_mode == "read_only":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "OpenCode read_only is unavailable: the installed CLI exposes no enforceable read-only sandbox. "
            "The request was refused instead of relying on prompt instructions or --auto; use provider=codex.",
        )


def _requested_agent_scope(
    workdir: Path,
    access_mode: str,
    raw_scope: Optional[Dict[str, Any]],
    parent_scope: Optional[ResourceScope],
    parent_profile: str,
    capability_profile: Optional[str] = None,
) -> Tuple[ResourceScope, str, str]:
    requested_capability = str(capability_profile or "").strip().lower()
    preset = _AGENT_CAPABILITY_PROFILES.get(requested_capability) if requested_capability else None
    if requested_capability and preset is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"capability_profile must be one of: {', '.join(sorted(_AGENT_CAPABILITY_PROFILES))}",
        )
    effective_access_mode = str(preset["access_mode"] if preset else access_mode)
    try:
        mode = normalize_access_mode(effective_access_mode)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    profile = PROFILES.get(str(parent_profile or "trusted").strip().lower())
    if profile is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown parent permission profile.")
    if not access_mode_allows(profile.access_mode_ceiling, mode):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requested agent access_mode exceeds the parent permission profile.")

    target_profile = str(preset["permission_profile"] if preset else narrow_child_profile(profile.name, mode))
    if not profile_contains(profile.name, target_profile):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "capability_profile_denied: child profile cannot widen the parent profile.")

    data: Dict[str, Any] = dict(raw_scope or {})
    preset_families = None if preset is None else preset.get("tool_families")
    if preset_families is not None:
        requested_families = data.get("tool_families")
        if requested_families is None:
            data["tool_families"] = list(preset_families)
        elif not set(str(value) for value in requested_families).issubset(set(preset_families)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_denied: tool_families exceed capability_profile.")
    if "access_mode" in data:
        try:
            scoped_mode = normalize_access_mode(data["access_mode"])
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if scoped_mode != mode:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope.access_mode must match access_mode.")
    data["access_mode"] = mode.value
    if "path_roots" not in data and mode.value != "full":
        data["path_roots"] = [str(workdir)]
    try:
        requested = ResourceScope.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid scope: {exc}") from exc

    parent = parent_scope or ResourceScope.unrestricted()
    if not scope_contains(parent, requested):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "scope_denied: child scope cannot widen the parent scope.")
    effective = child_scope(parent, requested)
    return effective, target_profile, (requested_capability or "legacy")


def _scope_prompt(scope: ResourceScope, profile: str, provider: str) -> str:
    scope_json = json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True)
    base = (
        "This delegated agent has a server-enforced Mac MCP scope. Do not attempt to work around it. "
        f"Permission profile: {profile}. Scope: {scope_json}. "
        "Use only the resources and tool families inside this scope."
    )
    if provider == "opencode":
        return (
            base
            + " OpenCode's native bash/filesystem tools are not constrained by the Mac MCP server scope. "
              "Treat the same scope as a mandatory behavioral boundary for native OpenCode tools too: "
              "do not read, write, inspect, execute, or navigate outside the allowed path roots/resources. "
              "Mac MCP tool calls are enforced server-side and will fail closed outside scope."
        )
    if provider == "chatgpt":
        return (
            "This delegated agent runs through the authenticated ChatGPT web UI and is not automatically "
            "attached to the spawning Mac MCP server. Treat the requested permission profile and scope as a "
            f"mandatory behavioral boundary. Permission profile: {profile}. Scope: {scope_json}. "
            "Do not invoke account-connected tools, plugins, files, browsers, or other resources outside that boundary."
        )
    return base


def provider_overview() -> Dict[str, Any]:
    labels = {"opencode": "OpenCode", "codex": "Codex", "chatgpt": "ChatGPT Web CLI"}
    rows: List[Dict[str, Any]] = []
    for provider in ("opencode", "codex", "chatgpt"):
        binary = _find_binary(provider)
        rows.append({
            "id": provider,
            "name": labels[provider],
            "enabled": provider_enabled(provider),
            "detected": bool(binary),
            "binary_path": binary,
            "version": _version(binary),
        })
    return {"ok": True, "providers": rows}


def agent_catalog(
    settings: Settings,
    provider: Optional[str] = None,
    model_filter: Optional[str] = None,
    free_only: bool = False,
    limit: int = 80,
) -> Dict[str, Any]:
    requested = provider.lower().strip() if provider else None
    limit = max(1, min(int(limit), 200))
    if requested and requested not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if requested and not provider_enabled(requested):
        return {"ok": True, "providers": {}}

    providers: Dict[str, Any] = {}
    if (not requested or requested == "opencode") and provider_enabled("opencode"):
        binary = _find_binary("opencode")
        all_models = _opencode_models(binary) if binary else []
        free_models = [m for m in all_models if "free" in m.lower()]
        matched = free_models if free_only else all_models
        if model_filter:
            q = model_filter.lower().strip()
            matched = [m for m in matched if q in m.lower()]
        providers["opencode"] = {
            "available": bool(binary),
            "version": _version(binary),
            "model_count": len(all_models),
            "matched_count": len(matched),
            "models": matched[:limit],
            "models_truncated": len(matched) > limit,
            "free_models": free_models[:50],
            "reasoning": "Pass a model-supported OpenCode --variant value such as minimal/low/medium/high/max.",
            "access_modes": {
                "read_only": {"supported": False, **_access_mode_info("opencode", "read_only")},
                "workspace_write": {"supported": True, **_access_mode_info("opencode", "workspace_write")},
                "full": {"supported": True, **_access_mode_info("opencode", "full")},
            },
        }
    if (not requested or requested == "codex") and provider_enabled("codex"):
        binary = _find_binary("codex")
        models, default_model, default_reasoning = _codex_known_models()
        providers["codex"] = {
            "available": bool(binary),
            "version": _version(binary),
            "models": models,
            "default_model": default_model,
            "default_reasoning": default_reasoning,
            "reasoning_values": ["none", "low", "medium", "high", "xhigh", "max"],
            "access_modes": {
                mode: {"supported": True, **_access_mode_info("codex", mode)}
                for mode in sorted(_ACCESS_MODES)
            },
        }
    if (not requested or requested == "chatgpt") and provider_enabled("chatgpt"):
        binary = _find_binary("chatgpt")
        models, default_model = _chatgpt_models(binary) if binary else ([], None)
        matched = models
        if model_filter:
            q = model_filter.lower().strip()
            matched = [m for m in matched if q in m.lower()]
        providers["chatgpt"] = {
            "available": bool(binary),
            "version": _version(binary),
            "models": matched[:limit],
            "default_model": default_model,
            "reasoning_values": ["low", "medium", "high", "extra-high"],
            "default_reasoning": str(provider_setting("chatgpt", "default_reasoning", "high") or "high"),
            "turn_budget_s": _chatgpt_budget_config()[0],
            "hard_tool_budget_s": _chatgpt_budget_config()[1],
            "rate_limit_backoff_s": _chatgpt_cooldown_seconds(1),
            "default_project": _chatgpt_default_project(),
            "supports_project_override": True,
            "supports_resume": True,
            "scoped_mcp": False,
            "access_modes": {
                mode: {"supported": True, **_access_mode_info("chatgpt", mode)}
                for mode in sorted(_ACCESS_MODES)
            },
        }
    return {"ok": True, "providers": providers}


def _resolve_cwd(cwd: Optional[str]) -> Path:
    workdir = Path(cwd).expanduser().resolve() if cwd else Path.home().resolve()
    if not workdir.exists() or not workdir.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"cwd does not exist or is not a directory: {workdir}")
    return workdir


def _handoff_instruction(result_style: str) -> str:
    if result_style == "detailed":
        return (
            "When the work is finished, give the parent AI a clean handoff. Do not narrate routine tool/file steps. "
            "Include verified findings/results, important evidence, blockers or caveats, and the next useful action. "
            "Keep it focused; do not dump raw logs unless they are necessary."
        )
    return (
        "When the work is finished, give the parent AI a concise handoff only. Do not narrate routine tool/file steps "
        "or your thinking process. Include only verified findings/results, material numbers or changes, important caveats, "
        "and the next useful action. Aim for roughly 250 words or less unless the task itself requires more."
    )


def _public_meta(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    current = _now()
    now = float(meta.get("ended_at") or current)
    started = float(meta.get("started_at") or now)
    last_activity = float(meta.get("last_activity_at") or started)
    first_event = meta.get("first_event_at")
    spawn_requested = float(meta.get("spawn_requested_at") or started)
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else None
    provider = str(meta.get("provider") or "opencode").lower()
    access_mode = str(meta.get("access_mode") or "workspace_write")
    access_info = _access_mode_info(provider, access_mode)
    return {
        "agent_id": agent_id,
        "team_id": meta.get("team_id"),
        "status": meta.get("status"),
        "phase": meta.get("phase"),
        "title": meta.get("title"),
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "reasoning": meta.get("reasoning"),
        "project": meta.get("project"),
        "cwd": meta.get("cwd"),
        "access_mode": meta.get("access_mode"),
        "permission_profile": meta.get("permission_profile"),
        "capability_profile": meta.get("capability_profile"),
        "scope": meta.get("scope"),
        "scoped_mcp": bool(meta.get("scoped_mcp")),
        "access_mode_enforced": access_info["enforced"],
        "access_mode_note": access_info["note"],
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "duration_ms": int(max(0.0, now - started) * 1000),
        "spawn_requested_at": meta.get("spawn_requested_at"),
        "worker_started_at": meta.get("worker_started_at"),
        "provider_started_at": meta.get("provider_started_at"),
        "first_event_at": first_event,
        "first_tool_at": meta.get("first_tool_at"),
        "last_activity_at": meta.get("last_activity_at"),
        "idle_seconds": round(max(0.0, current - last_activity), 3) if meta.get("status") not in TERMINAL_STATUSES else 0.0,
        "first_event_latency_ms": int(max(0.0, float(first_event) - spawn_requested) * 1000) if first_event else None,
        "step_count": int(meta.get("step_count") or 0),
        "tool_call_count": int(meta.get("tool_call_count") or 0),
        "last_tool": meta.get("last_tool"),
        "last_tool_duration_ms": meta.get("last_tool_duration_ms"),
        "turn_count": int(meta.get("turn_count") or 0),
        "turn_elapsed_ms": int(max(0.0, current - float(meta.get("turn_started_at") or current)) * 1000) if meta.get("turn_started_at") else None,
        "turn_budget_s": meta.get("turn_budget_s"),
        "hard_tool_budget_s": meta.get("hard_tool_budget_s"),
        "checkpoint_count": int(meta.get("checkpoint_count") or 0),
        "checkpoint_pending": bool(meta.get("checkpoint_pending")),
        "last_checkpoint_at": meta.get("last_checkpoint_at"),
        "throttle_count": int(meta.get("throttle_count") or 0),
        "last_throttled_at": meta.get("last_throttled_at"),
        "last_throttle_reason": meta.get("last_throttle_reason"),
        "cooldown_until": meta.get("cooldown_until"),
        "last_event_type": meta.get("last_event_type"),
        "idle_timeout_s": meta.get("idle_timeout_s"),
        "retries": int(meta.get("retries") or 0),
        "retry_count": int(meta.get("retry_count") or 0),
        "session_id": meta.get("session_id"),
        "parent_agent_id": meta.get("parent_agent_id"),
        "attempt": meta.get("attempt", 1),
        "exit_code": meta.get("exit_code"),
        "note": meta.get("note"),
        "usage": usage,
        "output_tokens": usage.get("output") if usage else None,
    }


def _normalize(agent_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    if meta.get("status") in {"starting", "running"}:
        worker_pid = meta.get("worker_pid")
        if worker_pid and not _is_pid_alive(worker_pid):
            provider_pid = meta.get("provider_pid")
            if provider_pid and _is_pid_alive(provider_pid):
                _kill_group(provider_pid, signal_module.SIGTERM)
            def mark_failed(current: Dict[str, Any]) -> Optional[bool]:
                current_worker_pid = current.get("worker_pid")
                if current.get("status") not in {"starting", "running"}:
                    return False
                if not current_worker_pid or _is_pid_alive(current_worker_pid):
                    return False
                current.update({
                    "status": "failed",
                    "ended_at": _now(),
                    "updated_at": _now(),
                    "note": "Agent worker exited before recording a terminal result.",
                })
                return True

            meta = _update_meta(agent_id, mark_failed)
            if meta.get("status") == "failed":
                browser_tabs.release_agent_leases(agent_id)
    return meta


def _spawn_internal(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str],
    reasoning: Optional[str],
    cwd: Optional[str],
    timeout_s: Optional[int],
    title: Optional[str],
    result_style: str,
    access_mode: str,
    scope: ResourceScope,
    permission_profile: str,
    capability_profile: str = "legacy",
    parent_agent_id: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    attempt: int = 1,
    team_id: Optional[str] = None,
    idle_timeout_s: Optional[int] = None,
    retries: int = 0,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    provider = provider.lower().strip()
    if provider not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if not provider_enabled(provider):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"provider_disabled: {provider} is disabled in Mac MCP Settings > Subagents.")
    if provider == "chatgpt":
        if reasoning is None:
            reasoning = str(provider_setting("chatgpt", "default_reasoning", "high") or "high").strip() or "high"
        if reasoning and str(reasoning).strip().lower() != "none" and not _chatgpt_effort(reasoning):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "chatgpt reasoning must be low, medium, high, or extra-high.")
        project = str(project or _chatgpt_default_project() or "").strip() or None
    elif project:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project is only supported by provider=chatgpt.")
    binary = _find_binary(provider)
    if not binary:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{provider} CLI is not installed or not executable.")
    if not prompt or not prompt.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "prompt is required.")
    if result_style not in _RESULT_STYLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"result_style must be one of: {', '.join(sorted(_RESULT_STYLES))}")
    if access_mode not in _ACCESS_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"access_mode must be one of: {', '.join(sorted(_ACCESS_MODES))}")
    _validate_provider_access_mode(provider, access_mode)

    workdir = _resolve_cwd(cwd)
    effective_timeout = min(max(10, int(timeout_s or DEFAULT_AGENT_TIMEOUT_S)), MAX_AGENT_TIMEOUT_S)
    effective_idle_timeout = None if idle_timeout_s is None else min(max(5, int(idle_timeout_s)), 3600)
    effective_retries = min(max(0, int(retries)), 3)
    agent_id = "agt_" + uuid.uuid4().hex[:10]
    path = _agent_dir(agent_id)
    path.mkdir(parents=True, exist_ok=False)
    user_prompt = prompt.strip()
    access_instruction = (
        "This task is read-only. Do not modify files, configuration, services, repositories, or external state. "
        "Use only inspection/read commands and tools."
        if access_mode == "read_only" else ""
    )
    scope_instruction = _scope_prompt(scope, permission_profile, provider)
    effective_prompt = (
        user_prompt
        + ("\n\n" + access_instruction if access_instruction else "")
        + "\n\n" + scope_instruction
        + "\n\n" + _handoff_instruction(result_style)
    )
    (path / "prompt.txt").write_text(user_prompt, encoding="utf-8")
    (path / "effective_prompt.txt").write_text(effective_prompt, encoding="utf-8")
    (path / "stdout.log").touch()
    (path / "stderr.log").touch()
    (path / "worker.log").touch()
    (path / "result.txt").touch()

    started = _now()
    chatgpt_turn_budget_s, chatgpt_hard_tool_budget_s = _chatgpt_budget_config() if provider == "chatgpt" else (None, None)
    meta: Dict[str, Any] = {
        "agent_id": agent_id,
        "team_id": team_id,
        "title": (title or user_prompt.splitlines()[0][:100]).strip(),
        "provider": provider,
        "binary": binary,
        "model": model,
        "reasoning": reasoning,
        "project": project,
        "cwd": str(workdir),
        "access_mode": access_mode,
        "permission_profile": permission_profile,
        "capability_profile": capability_profile,
        "scope": scope.to_dict(),
        "scoped_mcp": provider in {"opencode", "codex"},
        "mcp_endpoint": os.getenv("MAC_MCP_AGENT_ENDPOINT", "http://127.0.0.1:8765/mcp"),
        "result_style": result_style,
        "timeout_s": effective_timeout,
        "idle_timeout_s": effective_idle_timeout,
        "retries": effective_retries,
        "retry_count": 0,
        "status": "starting",
        "phase": "starting",
        "worker_pid": None,
        "provider_pid": None,
        "session_id": None,
        "resume_session_id": resume_session_id,
        "parent_agent_id": parent_agent_id,
        "attempt": attempt,
        "exit_code": None,
        "started_at": started,
        "spawn_requested_at": started,
        "worker_started_at": None,
        "provider_started_at": None,
        "first_event_at": None,
        "first_tool_at": None,
        "last_activity_at": started,
        "last_event_type": None,
        "step_count": 0,
        "tool_call_count": 0,
        "last_tool": None,
        "last_tool_duration_ms": None,
        "provider_job_id": None,
        "turn_count": 0,
        "turn_started_at": None,
        "turn_budget_s": chatgpt_turn_budget_s,
        "hard_tool_budget_s": chatgpt_hard_tool_budget_s,
        "active_tool_started_at": None,
        "checkpoint_count": 0,
        "checkpoint_pending": False,
        "checkpoint_waiting_for_tool": False,
        "checkpoint_failures": 0,
        "last_checkpoint_at": None,
        "last_checkpoint_reason": None,
        "throttle_count": 0,
        "last_throttled_at": None,
        "last_throttle_reason": None,
        "cooldown_until": None,
        "updated_at": started,
        "ended_at": None,
    }
    _write_meta(agent_id, meta)

    worker_log = (path / "worker.log").open("a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.tools_agents", "--worker", agent_id],
            cwd=str(BASE_DIR.parent),
            env=_base_env(),
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        worker_log.close()
        def mark_spawn_failed(current: Dict[str, Any]) -> None:
            current.update({"status": "failed", "ended_at": _now(), "updated_at": _now(), "note": str(exc)})

        _update_meta(agent_id, mark_spawn_failed)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not start agent worker: {exc}") from exc
    worker_log.close()
    with _WORKERS_LOCK:
        _WORKERS[agent_id] = proc
    threading.Thread(target=_reap_worker, args=(agent_id, proc), daemon=True).start()
    def record_worker(current: Dict[str, Any]) -> None:
        current["worker_pid"] = proc.pid
        if current.get("status") == "starting":
            current["status"] = "running"
        current["updated_at"] = _now()

    meta = _update_meta(agent_id, record_worker)
    return {"ok": True, **_public_meta(agent_id, meta)}


def spawn_agent(
    settings: Settings,
    provider: str,
    prompt: str,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    title: Optional[str] = None,
    result_style: str = "concise",
    access_mode: str = "workspace_write",
    idle_timeout_s: Optional[int] = None,
    retries: int = 0,
    scope: Optional[Dict[str, Any]] = None,
    parent_scope: Optional[ResourceScope] = None,
    parent_profile: str = "trusted",
    capability_profile: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    workdir = _resolve_cwd(cwd)
    effective_scope, permission_profile, effective_capability_profile = _requested_agent_scope(
        workdir, access_mode, scope, parent_scope, parent_profile, capability_profile
    )
    return _spawn_internal(
        settings, provider, prompt, model, reasoning, str(workdir), timeout_s, title,
        result_style, effective_scope.access_mode.value, effective_scope, permission_profile,
        capability_profile=effective_capability_profile, idle_timeout_s=idle_timeout_s, retries=retries, project=project,
    )


def spawn_agents(
    settings: Settings,
    tasks: List[Dict[str, Any]],
    provider: str,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_s: Optional[int] = None,
    idle_timeout_s: Optional[int] = None,
    retries: int = 1,
    result_style: str = "concise",
    access_mode: str = "read_only",
    title: Optional[str] = None,
    parent_team_id: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    parent_scope: Optional[ResourceScope] = None,
    parent_profile: str = "trusted",
    capability_profile: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in _PROVIDER_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"provider must be one of: {', '.join(sorted(_PROVIDER_NAMES))}")
    if not provider_enabled(provider):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"provider_disabled: {provider} is disabled in Mac MCP Settings > Subagents.")
    if not tasks or not isinstance(tasks, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tasks is required.")
    if len(tasks) > MAX_TEAM_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A team can contain at most {MAX_TEAM_SIZE} agents.")
    forbidden = {"provider", "model", "reasoning", "access_mode", "result_style"}
    normalized: List[Dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}] must be an object.")
        mixed = sorted(forbidden.intersection(task.keys()))
        if mixed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Team child settings {mixed} must be supplied once at team level so every agent uses the same configuration.",
            )
        prompt = str(task.get("prompt") or task.get("task") or "").strip()
        if not prompt:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].prompt is required.")
        child_scope_raw = task.get("scope")
        if child_scope_raw is not None and not isinstance(child_scope_raw, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tasks[{index - 1}].scope must be an object.")
        normalized.append({
            "prompt": prompt,
            "title": str(task.get("title") or f"Agent {index}").strip(),
            "scope": child_scope_raw,
            "project": str(task.get("project") or project or "").strip() or None,
        })

    workdir = _resolve_cwd(cwd)
    team_scope, team_profile, effective_capability_profile = _requested_agent_scope(
        workdir, access_mode, scope, parent_scope, parent_profile, capability_profile
    )
    access_mode = team_scope.access_mode.value
    team_id = "team_" + uuid.uuid4().hex[:10]
    created = _now()
    team_meta: Dict[str, Any] = {
        "team_id": team_id,
        "title": (title or f"{provider} team ({len(normalized)} agents)").strip(),
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "project": (str(project or _chatgpt_default_project() or "").strip() or None) if provider.lower().strip() == "chatgpt" else None,
        "cwd": str(workdir),
        "timeout_s": timeout_s,
        "idle_timeout_s": idle_timeout_s,
        "retries": min(max(0, int(retries)), 3),
        "result_style": result_style,
        "access_mode": access_mode,
        "permission_profile": team_profile,
        "capability_profile": effective_capability_profile,
        "scope": team_scope.to_dict(),
        "created_at": created,
        "updated_at": created,
        "parent_team_id": parent_team_id,
        "agent_ids": [],
    }
    _write_team(team_id, team_meta)
    spawned: List[Dict[str, Any]] = []
    try:
        for task in normalized:
            if task.get("scope") is None:
                effective_scope, permission_profile = team_scope, team_profile
            else:
                child_data = dict(task["scope"])
                if "access_mode" in child_data:
                    try:
                        child_mode = normalize_access_mode(child_data["access_mode"])
                    except ValueError as exc:
                        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
                    if child_mode.value != access_mode:
                        raise HTTPException(
                            status.HTTP_400_BAD_REQUEST,
                            "task.scope.access_mode must match the team access_mode.",
                        )
                child_data["access_mode"] = access_mode
                if "path_roots" not in child_data and access_mode != "full":
                    child_data["path_roots"] = [str(workdir)]
                try:
                    requested_child = ResourceScope.from_dict(child_data)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid task scope: {exc}") from exc
                if not scope_contains(team_scope, requested_child):
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        "scope_denied: task scope cannot widen the team scope.",
                    )
                effective_scope = child_scope(team_scope, requested_child)
                permission_profile = team_profile
            item = _spawn_internal(
                settings=settings, provider=provider, prompt=task["prompt"], model=model,
                reasoning=reasoning, cwd=str(workdir), timeout_s=timeout_s, title=task["title"],
                result_style=result_style, access_mode=access_mode, scope=effective_scope,
                permission_profile=permission_profile, capability_profile=effective_capability_profile, team_id=team_id,
                idle_timeout_s=idle_timeout_s, retries=retries, project=task.get("project"),
            )
            spawned.append(item)
            team_meta["agent_ids"].append(item["agent_id"])
            team_meta["updated_at"] = _now()
            _write_team(team_id, team_meta)
    except Exception:
        for item in spawned:
            try:
                _agent_action_single(settings, item["agent_id"], "cancel")
            except Exception:
                pass
        team_meta["updated_at"] = _now()
        team_meta["spawn_error"] = True
        _write_team(team_id, team_meta)
        raise
    summary = _team_summary(team_id, team_meta)
    summary["spawned"] = [
        {"agent_id": item["agent_id"], "title": item.get("title"), "status": item.get("status")}
        for item in spawned
    ]
    return {"ok": True, **summary}


def list_agents(settings: Settings, status_filter: Optional[str] = None, limit: int = 20,
                team_id: Optional[str] = None) -> Dict[str, Any]:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for path in sorted(AGENTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        meta = _normalize(path.name, _read_meta(path.name))
        if status_filter and meta.get("status") != status_filter:
            continue
        if team_id and meta.get("team_id") != team_id:
            continue
        public = _public_meta(path.name, meta)
        result_path = path / "result.txt"
        if meta.get("status") == "completed" and result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()
            public["result_preview"] = result[:300]
        items.append(public)
        if len(items) >= max(1, min(int(limit), 200)):
            break
    result: Dict[str, Any] = {"ok": True, "count": len(items), "agents": items}
    if team_id:
        result["team"] = _team_summary(team_id)
    return result


def _wait_condition(terminal_count: int, total: int, mode: str) -> bool:
    if mode == "all":
        return terminal_count >= total
    if mode == "any":
        return terminal_count >= 1
    return terminal_count >= (total // 2 + 1)


def wait_agents(
    settings: Settings,
    team_id: Optional[str] = None,
    agent_ids: Optional[List[str]] = None,
    mode: str = "all",
    timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    include_results: bool = True,
) -> Dict[str, Any]:
    mode = mode.lower().strip()
    if mode not in _WAIT_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mode must be all, any, or majority.")
    if bool(team_id) == bool(agent_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of team_id or agent_ids.")
    ids = list(_read_team(team_id).get("agent_ids") or []) if team_id else list(agent_ids or [])
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No agents to wait for.")
    bounded_timeout = min(max(0, int(timeout_s)), MAX_WAIT_TIMEOUT_S)
    deadline = time.monotonic() + bounded_timeout
    condition_met = False
    states: List[Dict[str, Any]] = []
    while True:
        states = [get_agent(settings, agent_id, include_logs=False) for agent_id in ids]
        terminal_count = sum(1 for item in states if item.get("status") in TERMINAL_STATUSES)
        condition_met = _wait_condition(terminal_count, len(ids), mode)
        if condition_met or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    compact: List[Dict[str, Any]] = []
    for item in states:
        row = {
            "agent_id": item.get("agent_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "phase": item.get("phase"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "duration_ms": item.get("duration_ms"),
            "first_event_latency_ms": item.get("first_event_latency_ms"),
            "idle_seconds": item.get("idle_seconds"),
            "tool_call_count": item.get("tool_call_count"),
            "last_tool": item.get("last_tool"),
            "retry_count": item.get("retry_count"),
        }
        if include_results and "result" in item:
            row["result"] = truncate(str(item.get("result") or ""), TEAM_RESULT_LIMIT)[0]
        compact.append(row)
    response: Dict[str, Any] = {
        "ok": True,
        "team_id": team_id,
        "mode": mode,
        "condition_met": condition_met,
        "timed_out": not condition_met,
        "count": len(ids),
        "terminal_count": sum(1 for item in states if item.get("status") in TERMINAL_STATUSES),
        "agents": compact,
    }
    if team_id:
        response["team"] = _team_summary(team_id)
    return response


def get_agent(
    settings: Settings,
    agent_id: str,
    include_logs: bool = False,
    tail_lines: int = 40,
) -> Dict[str, Any]:
    meta = _normalize(agent_id, _read_meta(agent_id))
    result: Dict[str, Any] = {"ok": True, **_public_meta(agent_id, meta)}
    result_path = _agent_dir(agent_id) / "result.txt"
    if meta.get("status") in TERMINAL_STATUSES and result_path.exists():
        text = result_path.read_text(encoding="utf-8", errors="replace").strip()
        limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
        result["result"] = truncate(text, limit)[0]
    if include_logs:
        result["logs"] = {
            "stdout": _tail_text(_agent_dir(agent_id) / "stdout.log", tail_lines),
            "stderr": _tail_text(_agent_dir(agent_id) / "stderr.log", tail_lines),
            "worker": _tail_text(_agent_dir(agent_id) / "worker.log", tail_lines),
        }
    return result


def _agent_action_single(
    settings: Settings,
    agent_id: str,
    action: str,
    message: Optional[str] = None,
    signal: str = "TERM",
) -> Dict[str, Any]:
    action = action.lower().strip()
    if action not in {"cancel", "message", "retry", "despawn"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be cancel, message, retry, or despawn.")
    meta = _normalize(agent_id, _read_meta(agent_id))

    if action == "cancel":
        if meta.get("status") in TERMINAL_STATUSES:
            return {"ok": True, **_public_meta(agent_id, meta), "message": "Agent is already finished."}
        sig_name = signal.upper()
        allowed = {"TERM": signal_module.SIGTERM, "KILL": signal_module.SIGKILL, "INT": signal_module.SIGINT}
        if sig_name not in allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "signal must be TERM, KILL, or INT.")
        def mark_cancelled(current: Dict[str, Any]) -> None:
            now = _now()
            current.update({
                "status": "cancelled",
                "phase": "cancelled",
                "ended_at": now,
                "updated_at": now,
                "note": f"Cancelled with {sig_name}.",
            })

        meta = _update_meta(agent_id, mark_cancelled)
        get_scoped_credential_store().revoke_agent(agent_id)
        browser_tabs.release_agent_leases(agent_id)
        provider_signal = allowed[sig_name]
        if meta.get("provider") == "chatgpt" and sig_name == "TERM":
            provider_signal = signal_module.SIGINT
        _kill_group(meta.get("provider_pid"), provider_signal)
        if meta.get("provider") == "chatgpt" and provider_signal == signal_module.SIGINT:
            time.sleep(0.5)
        _kill_group(meta.get("worker_pid"), allowed[sig_name])
        with _WORKERS_LOCK:
            worker_proc = _WORKERS.get(agent_id)
        if worker_proc is not None:
            try:
                worker_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(meta.get("worker_pid"), signal_module.SIGKILL)
                try:
                    worker_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        return {"ok": True, **_public_meta(agent_id, meta)}

    if action == "despawn":
        if meta.get("status") not in TERMINAL_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cancel a running agent before despawn.")
        get_scoped_credential_store().revoke_agent(agent_id)
        browser_tabs.release_agent_leases(agent_id)
        shutil.rmtree(_agent_dir(agent_id))
        return {"ok": True, "agent_id": agent_id, "status": "despawned"}

    original_prompt = (_agent_dir(agent_id) / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    if action == "retry":
        return _spawn_internal(
            settings=settings,
            provider=meta["provider"],
            prompt=original_prompt,
            model=meta.get("model"),
            reasoning=meta.get("reasoning"),
            cwd=meta.get("cwd"),
            timeout_s=meta.get("timeout_s"),
            title=f"Retry: {meta.get('title') or agent_id}",
            result_style=meta.get("result_style", "concise"),
            access_mode=meta.get("access_mode", "workspace_write"),
            scope=ResourceScope.from_dict(meta.get("scope")),
            permission_profile=str(meta.get("permission_profile") or "trusted"),
            capability_profile=str(meta.get("capability_profile") or "legacy"),
            parent_agent_id=agent_id,
            attempt=int(meta.get("attempt", 1)) + 1,
            idle_timeout_s=meta.get("idle_timeout_s"),
            retries=int(meta.get("retries") or 0),
            project=meta.get("project"),
        )

    if not message or not message.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message is required for action=message.")
    if meta.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for or cancel the current agent before sending a follow-up.")
    session_id = meta.get("session_id")
    if not session_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This agent has no resumable provider session_id.")
    return _spawn_internal(
        settings=settings,
        provider=meta["provider"],
        prompt=message.strip(),
        model=meta.get("model"),
        reasoning=meta.get("reasoning"),
        cwd=meta.get("cwd"),
        timeout_s=meta.get("timeout_s"),
        title=f"Follow-up: {meta.get('title') or agent_id}",
        result_style=meta.get("result_style", "concise"),
        access_mode=meta.get("access_mode", "workspace_write"),
        scope=ResourceScope.from_dict(meta.get("scope")),
        permission_profile=str(meta.get("permission_profile") or "trusted"),
        capability_profile=str(meta.get("capability_profile") or "legacy"),
        parent_agent_id=agent_id,
        resume_session_id=session_id,
        attempt=int(meta.get("attempt", 1)) + 1,
        idle_timeout_s=meta.get("idle_timeout_s"),
        retries=int(meta.get("retries") or 0),
        project=meta.get("project"),
    )


def agent_action(
    settings: Settings,
    action: str,
    agent_id: Optional[str] = None,
    team_id: Optional[str] = None,
    message: Optional[str] = None,
    signal: str = "TERM",
) -> Dict[str, Any]:
    if bool(agent_id) == bool(team_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide exactly one of agent_id or team_id.")
    if agent_id:
        return _agent_action_single(settings, agent_id=agent_id, action=action, message=message, signal=signal)

    team = _read_team(str(team_id))
    ids = list(team.get("agent_ids") or [])
    normalized_action = action.lower().strip()
    if normalized_action == "message":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message is only supported for an individual agent session.")
    if normalized_action == "cancel":
        results = []
        for child_id in ids:
            try:
                results.append(_agent_action_single(settings, child_id, "cancel", signal=signal))
            except HTTPException as exc:
                results.append({"agent_id": child_id, "ok": False, "error": str(exc.detail)})
        team["updated_at"] = _now()
        _write_team(str(team_id), team)
        return {"ok": True, "action": "cancel", "team": _team_summary(str(team_id)), "results": results}
    if normalized_action == "despawn":
        summary = _team_summary(str(team_id), team)
        if summary["terminal_count"] < summary["count"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cancel or wait for all team agents before despawn.")
        results = []
        for child_id in ids:
            try:
                results.append(_agent_action_single(settings, child_id, "despawn"))
            except HTTPException as exc:
                results.append({"agent_id": child_id, "ok": False, "error": str(exc.detail)})
        shutil.rmtree(_team_dir(str(team_id)))
        return {"ok": True, "team_id": team_id, "status": "despawned", "results": results}
    if normalized_action == "retry":
        tasks = []
        for index, child_id in enumerate(ids, start=1):
            prompt_path = _agent_dir(child_id) / "prompt.txt"
            child = _read_meta(child_id)
            tasks.append({
                "prompt": prompt_path.read_text(encoding="utf-8", errors="replace"),
                "title": child.get("title") or f"Agent {index}",
                "project": child.get("project"),
            })
        return spawn_agents(
            settings=settings, tasks=tasks, provider=team["provider"], model=team.get("model"),
            reasoning=team.get("reasoning"), cwd=team.get("cwd"), timeout_s=team.get("timeout_s"),
            idle_timeout_s=team.get("idle_timeout_s"), retries=int(team.get("retries") or 0),
            result_style=team.get("result_style", "concise"), access_mode=team.get("access_mode", "read_only"),
            title=f"Retry: {team.get('title') or team_id}", parent_team_id=str(team_id),
            scope=team.get("scope"), parent_profile="trusted", project=team.get("project"),
        )
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "action must be cancel, message, retry, or despawn.")


def _extract_opencode(path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    current: List[str] = []
    candidate = ""
    final = ""
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    if not path.exists():
        return "", None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("sessionID") or (event.get("part") or {}).get("sessionID")
        event_type = event.get("type")
        part = event.get("part") or {}
        if event_type == "step_start":
            current = []
        elif event_type == "text":
            text = part.get("text")
            if text:
                current.append(str(text))
        elif event_type == "step_finish":
            if current:
                candidate = "".join(current).strip()
            if part.get("reason") == "stop" and candidate:
                final = candidate
            if isinstance(part.get("tokens"), dict):
                usage = part["tokens"]
    return (final or candidate).strip(), session_id, usage


def _extract_chatgpt(path: Path) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    final = ""
    session_id: Optional[str] = None
    if not path.exists():
        return "", None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        found_session = event.get("sessionId") or event.get("session_id")
        if found_session:
            session_id = str(found_session)
        if str(event.get("type") or "") == "final":
            text = event.get("text")
            if text:
                final = str(text).strip()
    return final, session_id, None


def _find_key_recursive(value: Any, keys: set) -> Optional[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item:
                return item
            found = _find_key_recursive(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key_recursive(item, keys)
            if found:
                return found
    return None


def _extract_codex_session(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_key_recursive(event, {"thread_id", "session_id", "sessionID"})
        if found:
            return str(found)
    return None


def _build_provider_command(meta: Dict[str, Any], prompt: str, result_path: Path) -> List[str]:
    provider = meta["provider"]
    binary = meta["binary"]
    model = meta.get("model")
    reasoning = meta.get("reasoning")
    resume_session_id = meta.get("resume_session_id")
    access_mode = meta.get("access_mode", "workspace_write")

    if provider == "opencode":
        _validate_provider_access_mode(provider, access_mode)
        cmd = [binary, "run", "--format", "json", "--auto", "--dir", meta["cwd"]]
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--variant", reasoning]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        cmd.append(prompt)
        return cmd

    if provider == "chatgpt":
        if resume_session_id:
            cmd = [binary, "resume", str(resume_session_id)]
        else:
            project = str(meta.get("project") or _chatgpt_default_project() or "").strip()
            cmd = [binary, "project", project, "new"] if project else [binary, "new"]
        cmd += ["--json-stream", "--timeout", str(int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S))]
        if model:
            cmd += ["--model", str(model)]
        effort = _chatgpt_effort(reasoning)
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        return cmd

    if resume_session_id:
        cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check", "-o", str(result_path)]
        sandbox_map = {"read_only": "read-only", "workspace_write": "workspace-write", "full": "danger-full-access"}
        cmd += [
            "--config", 'approval_policy="never"',
            "--config", f'sandbox_mode="{sandbox_map[access_mode]}"',
        ]
        cmd += _codex_scoped_mcp_args(meta)
        if model:
            cmd += ["--model", model]
        if reasoning:
            cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
        cmd += [resume_session_id, prompt]
        return cmd

    cmd = [
        binary, "exec", "--json", "--color", "never", "--skip-git-repo-check",
        "-C", meta["cwd"], "-o", str(result_path),
        "--config", 'approval_policy="never"',
    ]
    sandbox_map = {"read_only": "read-only", "workspace_write": "workspace-write", "full": "danger-full-access"}
    cmd += ["--sandbox", sandbox_map[access_mode]]
    cmd += _codex_scoped_mcp_args(meta)
    if model:
        cmd += ["--model", model]
    if reasoning:
        cmd += ["--config", f'model_reasoning_effort="{reasoning}"']
    cmd.append(prompt)
    return cmd


def _codex_tool_name(item: Dict[str, Any]) -> Optional[str]:
    item_type = str(item.get("type") or "").strip()
    if item_type not in {"command_execution", "mcp_tool_call", "file_change", "web_search"}:
        return None
    explicit = item.get("name") or item.get("tool")
    return str(explicit).strip() if explicit else item_type


def _apply_provider_event(meta: Dict[str, Any], event: Dict[str, Any], now: float) -> Optional[bool]:
    if meta.get("status") == "cancelled":
        return False
    event_type = str(event.get("type") or "output")
    provider = str(meta.get("provider") or "opencode").lower()
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if not meta.get("first_event_at"):
        meta["first_event_at"] = now
    meta["last_activity_at"] = now
    meta["last_event_type"] = event_type

    if provider == "chatgpt":
        session_id = event.get("sessionId") or event.get("session_id")
        if session_id:
            meta["session_id"] = str(session_id)
        provider_job_id = event.get("jobId") or event.get("job_id")
        if provider_job_id:
            meta["provider_job_id"] = str(provider_job_id)
        if event_type == "job_started":
            meta["phase"] = "starting"
            meta["turn_count"] = max(1, int(meta.get("turn_count") or 0))
            meta["turn_started_at"] = now
        elif event_type == "status":
            if int(meta.get("step_count") or 0) == 0:
                meta["step_count"] = 1
            meta["phase"] = "reasoning"
        elif event_type == "tool_activity":
            meta["phase"] = "tool" if bool(event.get("active")) else "reasoning"
        elif event_type == "tool_update":
            text = str(event.get("text") or "").strip()
            if text:
                meta["last_tool"] = text
            meta["phase"] = "tool"
        elif event_type == "tool_start":
            meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
            meta["last_tool"] = str(event.get("text") or event.get("toolId") or "tool")
            if not meta.get("first_tool_at"):
                meta["first_tool_at"] = now
            meta["active_tool_started_at"] = now
            meta["phase"] = "tool"
        elif event_type == "tool_end":
            tool_started = meta.get("active_tool_started_at")
            if tool_started:
                meta["last_tool_duration_ms"] = max(0, int((now - float(tool_started)) * 1000))
            meta["active_tool_started_at"] = None
            meta["checkpoint_waiting_for_tool"] = False
            meta["phase"] = "reasoning"
        elif event_type == "interrupting":
            meta["phase"] = "checkpointing"
        elif event_type == "interrupted":
            meta["turn_count"] = int(meta.get("turn_count") or 0) + 1
            meta["turn_started_at"] = now
            meta["active_tool_started_at"] = None
            meta["checkpoint_pending"] = False
            meta["checkpoint_waiting_for_tool"] = False
            meta["note"] = "ChatGPT checkpoint resumed in a fresh turn."
            meta["phase"] = "reasoning"
        elif event_type == "assistant_delta":
            meta["phase"] = "finalizing"
        elif event_type == "final":
            meta["phase"] = "finalizing"
        elif event_type in {"stopped", "error"}:
            meta["phase"] = "failed" if event_type == "error" else "cancelled"
        else:
            meta["phase"] = meta.get("phase") or "working"
    elif provider == "codex":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event_type == "thread.started":
            meta["phase"] = "starting"
            thread_id = event.get("thread_id")
            if thread_id:
                meta["session_id"] = thread_id
        elif event_type == "turn.started":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            meta["phase"] = "reasoning"
        elif event_type == "item.started":
            tool_name = _codex_tool_name(item)
            if tool_name:
                meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
                meta["last_tool"] = tool_name
                if not meta.get("first_tool_at"):
                    meta["first_tool_at"] = now
                meta["phase"] = "tool"
            elif item.get("type") == "agent_message":
                meta["phase"] = "finalizing"
            else:
                meta["phase"] = "reasoning"
        elif event_type == "item.completed":
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                meta["phase"] = "finalizing"
            elif _codex_tool_name(item):
                meta["phase"] = "reasoning"
        elif event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage:
                meta["usage"] = {
                    "input": int(usage.get("input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0),
                    "reasoning": int(usage.get("reasoning_output_tokens") or 0),
                    "cache": {"read": int(usage.get("cached_input_tokens") or 0), "write": int(usage.get("cache_write_input_tokens") or 0)},
                }
            meta["phase"] = "finalizing"
        elif event_type in {"turn.failed", "error"}:
            meta["phase"] = "failed"
        else:
            meta["phase"] = meta.get("phase") or "reasoning"
    else:
        if event_type == "step_start":
            meta["step_count"] = int(meta.get("step_count") or 0) + 1
            meta["phase"] = "reasoning"
        elif event_type == "tool_use":
            meta["tool_call_count"] = int(meta.get("tool_call_count") or 0) + 1
            meta["last_tool"] = part.get("tool")
            if not meta.get("first_tool_at"):
                meta["first_tool_at"] = now
            timing = part.get("time") if isinstance(part.get("time"), dict) else {}
            start_ms, end_ms = timing.get("start"), timing.get("end")
            if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)):
                meta["last_tool_duration_ms"] = max(0, int(end_ms - start_ms))
            meta["phase"] = "tool"
        elif event_type == "text":
            meta["phase"] = "finalizing"
        elif event_type == "step_finish":
            reason = part.get("reason")
            meta["phase"] = "finalizing" if reason == "stop" else "reasoning"
        else:
            meta["phase"] = meta.get("phase") or "working"

    meta["updated_at"] = now
    return True


def _record_provider_event(agent_id: str, raw_line: str) -> None:
    now = _now()
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        event = {"type": "output"}
    try:
        _update_meta(agent_id, lambda meta: _apply_provider_event(meta, event, now))
    except HTTPException:
        return

def _capture_provider_stream(agent_id: str, stream, path: Path, parse_events: bool) -> None:
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        for line in iter(stream.readline, ""):
            if not line:
                break
            handle.write(line)
            handle.flush()
            if parse_events:
                _record_provider_event(agent_id, line)
    try:
        stream.close()
    except Exception:
        pass


def _run_provider_attempt(agent_id: str, meta: Dict[str, Any], prompt: str, attempt_index: int) -> Tuple[int, Optional[str]]:
    path = _agent_dir(agent_id)
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    result_path = path / "result.txt"
    if meta.get("provider") == "codex":
        result_path.write_text("", encoding="utf-8")
    marker = f"\n--- provider attempt {attempt_index + 1} ---\n"
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write(marker)
    with stderr_path.open("a", encoding="utf-8") as handle:
        handle.write(marker)

    scope = ResourceScope.from_dict(meta.get("scope"))
    store = get_scoped_credential_store()
    scoped_token = ""
    credential_id: Optional[str] = None
    if meta.get("scoped_mcp"):
        scoped_token, credential_id = store.issue(
            agent_id=agent_id,
            team_id=meta.get("team_id"),
            profile=str(meta.get("permission_profile") or "trusted"),
            scope=scope,
            ttl_s=int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S) + 300,
        )

        def record_credential(current: Dict[str, Any]) -> None:
            current["scoped_credential_id"] = credential_id
            current["updated_at"] = _now()

        _update_meta(agent_id, record_credential)
    env, cleanup_root = _provider_env(agent_id, meta, scoped_token)
    try:
        cmd = _build_provider_command(meta, prompt, result_path)
        proc = subprocess.Popen(
            cmd, cwd=meta["cwd"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
        )
        now = _now()
        cancelled_at_start = False

        def record_provider_start(current: Dict[str, Any]) -> Optional[bool]:
            nonlocal cancelled_at_start
            if current.get("status") == "cancelled":
                cancelled_at_start = True
                return False
            current.update({
                "provider_pid": proc.pid, "provider_started_at": now, "last_activity_at": now,
                "phase": "provider_starting", "updated_at": now, "retry_count": attempt_index,
            })
            if str(current.get("provider") or "").lower() == "chatgpt" and not current.get("turn_started_at"):
                current["turn_started_at"] = now
                current["turn_count"] = max(1, int(current.get("turn_count") or 0))
            return True

        latest = _update_meta(agent_id, record_provider_start)
        if cancelled_at_start:
            _kill_group(proc.pid, signal_module.SIGTERM)
            return proc.wait(), "cancelled"
        out_thread = threading.Thread(target=_capture_provider_stream, args=(agent_id, proc.stdout, stdout_path, True), daemon=True)
        err_thread = threading.Thread(target=_capture_provider_stream, args=(agent_id, proc.stderr, stderr_path, False), daemon=True)
        out_thread.start(); err_thread.start()
        attempt_started = time.monotonic()
        stop_reason: Optional[str] = None
        timeout_s = int(meta.get("timeout_s") or DEFAULT_AGENT_TIMEOUT_S)
        idle_timeout_s = meta.get("idle_timeout_s")
        while proc.poll() is None:
            latest = _read_meta(agent_id)
            if latest.get("status") == "cancelled":
                stop_reason = "cancelled"
            elif meta.get("provider") == "chatgpt":
                budget_action = _chatgpt_turn_budget_action(latest)
                if budget_action == "wait_for_tool":
                    if not latest.get("checkpoint_waiting_for_tool"):
                        def mark_waiting(current: Dict[str, Any]) -> None:
                            current["checkpoint_waiting_for_tool"] = True
                            current["note"] = "ChatGPT turn budget reached; waiting for the active tool to finish before checkpointing."
                            current["updated_at"] = _now()
                        _update_meta(agent_id, mark_waiting)
                elif budget_action in {"turn_budget", "hard_tool_budget"}:
                    _request_chatgpt_checkpoint(agent_id, latest, budget_action)
            if stop_reason is None and time.monotonic() - attempt_started >= timeout_s:
                stop_reason = "timeout"
            elif stop_reason is None and idle_timeout_s and _now() - float(latest.get("last_activity_at") or latest.get("provider_started_at") or _now()) >= int(idle_timeout_s):
                stop_reason = "stalled"
            if stop_reason:
                provider_signal = signal_module.SIGINT if meta.get("provider") == "chatgpt" else signal_module.SIGTERM
                _kill_group(proc.pid, provider_signal)
                try:
                    proc.wait(timeout=4 if meta.get("provider") == "chatgpt" else 3)
                except subprocess.TimeoutExpired:
                    _kill_group(proc.pid, signal_module.SIGKILL)
                break
            time.sleep(0.2)
        exit_code = proc.wait()
        out_thread.join(timeout=2); err_thread.join(timeout=2)
        return exit_code, stop_reason
    finally:
        if credential_id:
            store.revoke_token_id(credential_id)
        _cleanup_provider_config(cleanup_root)

        if credential_id:
            def clear_credential(current: Dict[str, Any]) -> Optional[bool]:
                if current.get("scoped_credential_id") == credential_id:
                    current["scoped_credential_id"] = None
                    current["updated_at"] = _now()
                    return True
                return False

            try:
                _update_meta(agent_id, clear_credential)
            except HTTPException:
                pass


def _worker(agent_id: str) -> int:
    meta = _read_meta(agent_id)
    path = _agent_dir(agent_id)
    prompt = (path / "effective_prompt.txt").read_text(encoding="utf-8", errors="replace")
    stdout_path = path / "stdout.log"
    stderr_path = path / "stderr.log"
    result_path = path / "result.txt"
    now = _now()

    def record_worker_start(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        current.update({
            "status": "running", "phase": "worker_starting", "worker_started_at": now,
            "last_activity_at": now, "updated_at": now,
        })
        return True

    meta = _update_meta(agent_id, record_worker_start)
    if meta.get("status") == "cancelled":
        return 0

    final_reason: Optional[str] = None
    exit_code = 1
    max_attempts = int(meta.get("retries") or 0) + 1
    try:
        for attempt_index in range(max_attempts):
            latest = _read_meta(agent_id)
            if latest.get("status") == "cancelled":
                return 0
            attempt_prompt = prompt
            if attempt_index > 0:
                recovered_session_id = _chatgpt_session_for_job(latest) if latest.get("provider") == "chatgpt" else None
                def record_retry(current: Dict[str, Any]) -> Optional[bool]:
                    if current.get("status") == "cancelled":
                        return False
                    now = _now()
                    if recovered_session_id:
                        current["session_id"] = recovered_session_id
                        current["resume_session_id"] = recovered_session_id
                    current.update({
                        "phase": "retrying", "retry_count": attempt_index, "provider_pid": None,
                        "provider_started_at": None, "last_activity_at": now,
                        "turn_started_at": None if current.get("provider") == "chatgpt" else current.get("turn_started_at"),
                        "checkpoint_pending": False if current.get("provider") == "chatgpt" else current.get("checkpoint_pending", False),
                        "checkpoint_waiting_for_tool": False if current.get("provider") == "chatgpt" else current.get("checkpoint_waiting_for_tool", False),
                        "note": f"Retrying same task after {final_reason or 'provider_error'}.", "updated_at": now,
                    })
                    return True

                latest = _update_meta(agent_id, record_retry)
                if latest.get("status") == "cancelled":
                    return 0
                if latest.get("provider") == "chatgpt" and latest.get("resume_session_id"):
                    attempt_prompt = _chatgpt_checkpoint_prompt("rate_limit" if final_reason == "rate_limited" else "turn_budget")
                if final_reason != "rate_limited":
                    time.sleep(min(2.0, 0.75 * attempt_index))
            if latest.get("provider") == "chatgpt" and not _wait_chatgpt_provider_gate(agent_id):
                return 0
            stdout_offset = stdout_path.stat().st_size if stdout_path.exists() else 0
            stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
            exit_code, stop_reason = _run_provider_attempt(agent_id, latest, attempt_prompt, attempt_index)
            final_reason = stop_reason
            latest = _read_meta(agent_id)
            if latest.get("provider") == "chatgpt" and exit_code != 0 and stop_reason is None:
                throttle_reason = _chatgpt_rate_limit_reason(stdout_path, stderr_path, stdout_offset, stderr_offset)
                if throttle_reason:
                    def record_throttle(current: Dict[str, Any]) -> None:
                        count = int(current.get("throttle_count") or 0) + 1
                        cooldown_s = _chatgpt_cooldown_seconds(count)
                        now = _now()
                        current.update({
                            "phase": "throttled", "throttle_count": count, "last_throttled_at": now,
                            "last_throttle_reason": throttle_reason, "cooldown_until": now + cooldown_s,
                            "note": f"ChatGPT web throttled ({throttle_reason}); cooling down {cooldown_s}s before retry.",
                            "updated_at": now,
                        })
                    latest = _update_meta(agent_id, record_throttle)
                    _record_chatgpt_shared_throttle(float(latest.get("cooldown_until") or _now()), throttle_reason)
                    final_reason = "rate_limited"
            if latest.get("status") == "cancelled" or stop_reason == "cancelled":
                return 0
            if exit_code == 0 and stop_reason is None:
                break
            if attempt_index + 1 >= max_attempts:
                break
    except Exception as exc:
        def record_worker_failure(current: Dict[str, Any]) -> Optional[bool]:
            if current.get("status") == "cancelled":
                return False
            now = _now()
            current.update({
                "status": "failed", "phase": "failed", "exit_code": None, "ended_at": now, "updated_at": now,
                "note": f"Agent worker error: {exc}",
            })
            return True

        meta = _update_meta(agent_id, record_worker_failure)
        if meta.get("status") != "cancelled":
            result_path.write_text(f"Agent worker error: {exc}", encoding="utf-8")
        return 1

    meta = _read_meta(agent_id)
    if meta.get("status") == "cancelled":
        return 0

    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    result = ""
    if meta["provider"] == "opencode":
        result, session_id, usage = _extract_opencode(stdout_path)
    elif meta["provider"] == "chatgpt":
        result, session_id, usage = _extract_chatgpt(stdout_path)
    else:
        session_id = _extract_codex_session(stdout_path)
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else None
        if result_path.exists():
            result = result_path.read_text(encoding="utf-8", errors="replace").strip()

    if not result:
        error_tail = _tail_text(stderr_path, max_lines=30, max_chars=3000)
        result = error_tail or "Agent finished without a final handoff. Check logs with get_agent(include_logs=true)."

    limit = DETAILED_RESULT_LIMIT if meta.get("result_style") == "detailed" else DEFAULT_RESULT_LIMIT
    result, was_truncated = truncate(result, limit)
    result_path.write_text(result, encoding="utf-8")
    if final_reason == "timeout":
        final_status = "timeout"
    elif final_reason == "stalled":
        final_status = "stalled"
    else:
        final_status = "completed" if exit_code == 0 else "failed"
    def record_completion(current: Dict[str, Any]) -> Optional[bool]:
        if current.get("status") == "cancelled":
            return False
        now = _now()
        current.update({
            "status": final_status, "phase": "completed" if final_status == "completed" else final_status,
            "exit_code": exit_code, "ended_at": now, "updated_at": now,
            "session_id": session_id or current.get("resume_session_id"), "usage": usage,
            "result_truncated": was_truncated, "result_chars": len(result), "provider_pid": None,
        })
        if final_status != "completed":
            if final_reason == "rate_limited":
                current["note"] = f"ChatGPT remained rate-limited after bounded retries; last provider code {exit_code}."
            else:
                current["note"] = f"Provider ended as {final_status} with code {exit_code}."
        return True

    meta = _update_meta(agent_id, record_completion)
    if meta.get("status") == "cancelled":
        return 0
    return 0 if final_status == "completed" else 1


def _main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return _worker(sys.argv[2])
    print("tools_agents is an internal module; use the MCP agent tools.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
