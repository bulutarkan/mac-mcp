from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    configured = os.getenv("MAC_MCP_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".mac-mcp" / "settings.json"


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

def keychain_password() -> str | None:
    service = os.getenv("MAC_MCP_VOICE_GROQ_KEYCHAIN_SERVICE", "com.bulutarkan.mac-mcp").strip()
    account = os.getenv("MAC_MCP_VOICE_GROQ_KEYCHAIN_ACCOUNT", "groq-api-key").strip()
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
