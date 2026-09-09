from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    configured = os.getenv("MAC_MCP_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".mac-mcp" / "settings.json"


def load_runtime_settings() -> dict[str, Any]:
    path = settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def voice_setting(name: str, default: Any = None) -> Any:
    voice = load_runtime_settings().get("voice", {})
    if not isinstance(voice, dict):
        return default
    return voice.get(name, default)


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
