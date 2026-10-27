from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


def settings_path() -> Path:
    return paths.settings_path()


@dataclass(frozen=True)
class RuntimeSettingsLoad:
    path: Path
    status: str
    data: dict[str, Any]
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def load_runtime_settings_state(path: Path | None = None) -> RuntimeSettingsLoad:
    path = path or settings_path()
    if not path.exists():
        return RuntimeSettingsLoad(path=path, status="missing", data={})
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return RuntimeSettingsLoad(
            path=path,
            status="unreadable",
            data={},
            error_type=type(exc).__name__,
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return RuntimeSettingsLoad(
            path=path,
            status="invalid_json",
            data={},
            error_type=type(exc).__name__,
        )
    if not isinstance(payload, dict):
        return RuntimeSettingsLoad(
            path=path,
            status="root_not_object",
            data={},
        )
    return RuntimeSettingsLoad(path=path, status="ok", data=payload)


def load_runtime_settings() -> dict[str, Any]:
    state = load_runtime_settings_state()
    return state.data if state.ok else {}


def tool_enabled(name: str, default: bool = True) -> bool:
    tools = load_runtime_settings().get("experimental_tools", {})
    if not isinstance(tools, dict):
        return default
    item = tools.get(name)
    if isinstance(item, dict):
        value = item.get("enabled")
        return value if isinstance(value, bool) else default
    if isinstance(item, bool):
        return item
    return default


def server_setting(name: str, default: Any = None) -> Any:
    server = load_runtime_settings().get("server", {})
    if not isinstance(server, dict):
        return default
    return server.get(name, default)


def voice_setting(name: str, default: Any = None) -> Any:
    voice = load_runtime_settings().get("voice", {})
    if not isinstance(voice, dict):
        return default
    return voice.get(name, default)


def steering_setting(name: str, default: Any = None) -> Any:
    steering = load_runtime_settings().get("steering", {})
    if not isinstance(steering, dict):
        return default
    return steering.get(name, default)


def tool_activity_setting(name: str, default: Any = None) -> Any:
    activity = load_runtime_settings().get("tool_activity", {})
    if not isinstance(activity, dict):
        return default
    return activity.get(name, default)


def privacy_setting(name: str, default: Any = None) -> Any:
    privacy = load_runtime_settings().get("privacy", {})
    if not isinstance(privacy, dict):
        return default
    return privacy.get(name, default)


# 0 keeps memories until they are deleted; memory is user-curated, so nothing
# expires unless the person picks a period.
MEMORY_RETENTION_CHOICES = (0, 90, 180, 365, 730)


def memory_privacy() -> dict[str, Any]:
    try:
        days = int(privacy_setting("memory_retention_days", 0) or 0)
    except (TypeError, ValueError):
        days = 0
    if days not in MEMORY_RETENTION_CHOICES:
        days = min(MEMORY_RETENTION_CHOICES, key=lambda choice: abs(choice - days))
    return {
        "retention_days": days,
        "keep_important": privacy_setting("memory_keep_important", True) is not False,
    }


USAGE_RETENTION_CHOICES = (30, 90, 365)
DEFAULT_USAGE_RETENTION_DAYS = 365
_USAGE_PRIVACY_TTL_S = 2.0
_usage_privacy_cache: dict[str, Any] = {"at": -1.0, "value": None}


def usage_privacy(*, fresh: bool = False) -> dict[str, Any]:
    """Usage metering switch and retention, read live from settings with a short cache.

    The collector consults this on every tool call, so the file is read at most
    once every couple of seconds.
    """
    import time as _time

    now = _time.monotonic()
    cached = _usage_privacy_cache.get("value")
    if not fresh and cached is not None and now - float(_usage_privacy_cache["at"]) < _USAGE_PRIVACY_TTL_S:
        return dict(cached)
    enabled = privacy_setting("usage_metering", True)
    try:
        days = int(privacy_setting("usage_retention_days", DEFAULT_USAGE_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_USAGE_RETENTION_DAYS
    if days not in USAGE_RETENTION_CHOICES:
        days = min(USAGE_RETENTION_CHOICES, key=lambda choice: abs(choice - days))
    value = {"enabled": enabled is not False, "retention_days": days}
    _usage_privacy_cache.update({"at": now, "value": value})
    return dict(value)


# Run evidence (#108): checkpoint screenshots are off until the owner turns them on, and run
# records with any kept screenshots expire after a short, chosen period.
RUN_EVIDENCE_RETENTION_CHOICES = (1, 7, 30)
DEFAULT_RUN_EVIDENCE_RETENTION_DAYS = 7


def run_evidence_privacy() -> dict[str, Any]:
    try:
        days = int(privacy_setting("run_evidence_retention_days", DEFAULT_RUN_EVIDENCE_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RUN_EVIDENCE_RETENTION_DAYS
    if days not in RUN_EVIDENCE_RETENTION_CHOICES:
        days = min(RUN_EVIDENCE_RETENTION_CHOICES, key=lambda choice: abs(choice - days))
    return {"capture": privacy_setting("run_evidence_capture", False) is True, "retention_days": days}


# Scheduled Tasks run contents (#199): the result, output and steps' text and inputs. The run's
# skeleton (how it ended, when, usage, fingerprints) stays for the run's own 90 days.
RUN_DETAIL_RETENTION_CHOICES = (7, 30, 90)
DEFAULT_RUN_DETAIL_RETENTION_DAYS = 30


def run_detail_privacy() -> dict[str, Any]:
    try:
        days = int(privacy_setting("run_detail_retention_days", DEFAULT_RUN_DETAIL_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RUN_DETAIL_RETENTION_DAYS
    if days not in RUN_DETAIL_RETENTION_CHOICES:
        days = min(RUN_DETAIL_RETENTION_CHOICES, key=lambda choice: abs(choice - days))
    return {"retention_days": days}


# Mac MCP Chat (#114): a chat nobody has used for this long is removed (a working chat never is).
CHAT_RETENTION_CHOICES = (7, 30, 90, 180, 365)
DEFAULT_CHAT_RETENTION_DAYS = 90


def chat_retention_days() -> int:
    try:
        days = int(privacy_setting("chat_retention_days", DEFAULT_CHAT_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_CHAT_RETENTION_DAYS
    if days not in CHAT_RETENTION_CHOICES:
        days = min(CHAT_RETENTION_CHOICES, key=lambda choice: abs(choice - days))
    return days


def security_setting(name: str, default: Any = None) -> Any:
    security = load_runtime_settings().get("security", {})
    if not isinstance(security, dict):
        return default
    return security.get(name, default)


def server_approval_profile_setting() -> str:
    state = load_runtime_settings_state()
    if state.status == "missing":
        return "off"
    if not state.ok:
        return "__invalid__"
    security = state.data.get("security")
    if security is None:
        return "off"
    if not isinstance(security, dict):
        return "__invalid__"
    value = security.get("server_approval_profile")
    if value is None:
        return "off"
    if not isinstance(value, str):
        return "__invalid__"
    return value.strip().lower() or "off"


def update_runtime_setting(
    section: str,
    name: str,
    value: Any,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    target = path or settings_path()
    if target.is_symlink():
        raise RuntimeError("settings path must not be a symlink")
    state = load_runtime_settings_state(target)
    if state.status not in {"ok", "missing"}:
        raise RuntimeError(f"settings file is {state.status}")
    payload = dict(state.data)
    current = payload.get(section, {})
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise RuntimeError(f"settings section {section!r} must be an object")
    updated = dict(current)
    updated[str(name)] = value
    payload[str(section)] = updated

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".mac-mcp-settings-",
        dir=str(target.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
    return payload


def subagent_default_preset() -> dict[str, Any] | None:
    state = load_runtime_settings_state()
    if not state.ok:
        return None
    subagents = state.data.get("subagents")
    if not isinstance(subagents, dict):
        return None
    preset = subagents.get("default")
    if not isinstance(preset, dict):
        return None
    return dict(preset)


_TEAM_PRESET_ID_RE = re.compile(r"^tp_[0-9a-f]{8}$")
_TEAM_MEMBER_ID_RE = re.compile(r"^m_[0-9a-f]{8}$")


def _preset_text(value: Any, limit: int) -> str | None:
    # len() counts code points (Unicode scalars), as the app's team builder does (TeamPreset.length): an
    # emoji that is one character on screen may be several, and both sides must agree on what fits.
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if 1 <= len(text) <= limit else None


def _preset_member(
    raw: Any, where: str, providers: set[str], roles: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """One member as the server uses it, else what is wrong with it."""
    if not isinstance(raw, dict):
        return None, f"{where} must be an object"
    name = _preset_text(raw.get("name"), 40)
    label = f"{where} ({name!r})" if name else where
    member_id = raw.get("id")
    if not isinstance(member_id, str) or not _TEAM_MEMBER_ID_RE.fullmatch(member_id):
        return None, f"{label}.id must be m_ and 8 lowercase hex digits"
    if name is None:
        return None, f"{where}.name must be 1-40 characters"
    provider = raw.get("provider")
    provider = provider.strip().lower() if isinstance(provider, str) else ""
    if provider not in providers:
        return None, f"{label}.provider {raw.get('provider')!r} is not one of {', '.join(sorted(providers))}"
    fields: dict[str, Any] = {}
    for key, allowed in (("model", None), ("reasoning", None), ("role", roles)):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return None, f"{label}.{key} must be text"
        text = (value or "").strip() or None
        if text is not None and key != "model":
            text = text.lower()
        if text is not None and allowed is not None and text not in allowed:
            return None, f"{label}.{key} {text!r} is not one of {', '.join(sorted(allowed))}"
        fields[key] = text
    return {"id": member_id, "name": name, "provider": provider, **fields}, None


def subagent_team_presets(
    *, providers: Any, roles: Any, max_members: int = 10, path: Path | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Saved agent teams (Settings > Agents) and what was wrong with the ones that were skipped.

    The native app writes them; a hand-edited or newer-format entry must never stop the server, so each
    malformed preset or member is left out and named in the problems instead. A preset that lost a member
    keeps that member's problem in its own "problems", so it is listed but refused when started.
    """
    state = load_runtime_settings_state(path)
    if not state.ok:
        return [], ([] if state.status == "missing" else [f"settings.json is {state.status}; no team presets read."])
    subagents = state.data.get("subagents")
    raw_presets = subagents.get("team_presets") if isinstance(subagents, dict) else None
    if raw_presets is None:
        return [], []
    if not isinstance(raw_presets, list):
        return [], ["subagents.team_presets must be a list; no team presets read."]
    provider_ids, role_ids = set(providers), set(roles)
    presets: list[dict[str, Any]] = []
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_presets):
        where = f"team_presets[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where} must be an object; preset skipped.")
            continue
        name = _preset_text(raw.get("name"), 60)
        preset_id = raw.get("id")
        if name is None:
            problems.append(f"{where}.name must be 1-60 characters; preset skipped.")
            continue
        where = f"{where} ({name!r})"
        if not isinstance(preset_id, str) or not _TEAM_PRESET_ID_RE.fullmatch(preset_id):
            problems.append(f"{where}.id must be tp_ and 8 lowercase hex digits; preset skipped.")
            continue
        if preset_id in seen_ids or name.casefold() in seen_names:
            problems.append(f"{where} repeats another preset's id or name; preset skipped.")
            continue
        raw_members = raw.get("members")
        if not isinstance(raw_members, list):
            problems.append(f"{where}.members must be a list; preset skipped.")
            continue
        members: list[dict[str, Any]] = []
        member_problems: list[str] = []
        member_keys: set[str] = set()
        for member_index, raw_member in enumerate(raw_members):
            member_where = f"{where}.members[{member_index}]"
            if len(members) >= max_members:
                member, problem = None, f"{member_where}: a team has at most {max_members} members"
            else:
                member, problem = _preset_member(raw_member, member_where, provider_ids, role_ids)
            if member is not None and (member["id"] in member_keys or member["name"].casefold() in member_keys):
                member, problem = None, f"{member_where} repeats another member's id or name"
            if member is None:
                problems.append(f"{problem}; member skipped.")
                member_problems.append(str(problem))
                continue
            member_keys.update({member["id"], member["name"].casefold()})
            members.append(member)
        if not members:
            problems.append(f"{where} has no usable members; preset skipped.")
            continue
        seen_ids.add(preset_id)
        seen_names.add(name.casefold())
        updated_at = raw.get("updated_at")
        presets.append({
            "id": preset_id,
            "name": name,
            "members": members,
            "updated_at": updated_at if isinstance(updated_at, str) else None,
            # Members left out above: the preset is listed but never starts with fewer members (fail closed).
            "problems": member_problems,
        })
    return presets, problems


def provider_setting(provider: str, name: str, default: Any = None) -> Any:
    subagents = load_runtime_settings().get("subagents", {})
    if not isinstance(subagents, dict):
        return default
    providers = subagents.get("providers", {})
    if not isinstance(providers, dict):
        return default
    item = providers.get(str(provider or "").strip().lower(), {})
    if not isinstance(item, dict):
        return default
    return item.get(name, default)


def provider_enabled(provider: str, default: bool | None = None) -> bool:
    key = str(provider or "").strip().lower()
    state = load_runtime_settings_state()
    if not state.ok:
        return False
    subagents = state.data.get("subagents")
    if not isinstance(subagents, dict):
        return False
    providers = subagents.get("providers")
    if not isinstance(providers, dict):
        return False
    item = providers.get(key)
    if not isinstance(item, dict):
        return False
    value = item.get("enabled")
    return value if isinstance(value, bool) else False

def keychain_password(service: str | None = None, account: str | None = None) -> str | None:
    if service is None:
        service = os.getenv("MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE", "com.bulutarkan.mac-mcp")
    if account is None:
        account = os.getenv("MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT", "groq-api-key")
    service = service.strip()
    account = account.strip()
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
