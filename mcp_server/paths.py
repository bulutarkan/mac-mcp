"""Where Mac MCP keeps things (#143): one resolver per location.

Precedence is the same everywhere and unchanged from before: an explicit MAC_MCP_*
variable wins, otherwise the default. Resolvers are read on every call, so a test or
an isolated install that sets the variable later is honored. They never create or
chmod anything; code that writes a store makes its directory private itself.

Not every store sits under the state root: telemetry, memory, lessons and settings
default to ~/.mac-mcp even when MAC_MCP_STATE_DIR points elsewhere, and job/agent
records and the audit log live next to the server code. That is how they shipped;
moving them would strand existing data, so this module names them rather than moves
them. Secrets (the API key, provider tokens) are not paths here: they stay in
mcp_server/.env and the Keychain.
"""
from __future__ import annotations

import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent


def _home_state() -> Path:
    return Path.home() / ".mac-mcp"


def _env_path(name: str, default: Path) -> Path:
    configured = os.getenv(name, "").strip()
    return Path(configured).expanduser() if configured else default


def state_dir() -> Path:
    """The state root: MAC_MCP_STATE_DIR, else ~/.mac-mcp."""
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(_home_state()))).expanduser()


def settings_path() -> Path:
    return _env_path("MAC_MCP_SETTINGS_PATH", _home_state() / "settings.json")


def telemetry_dir() -> Path:
    return _env_path("MAC_MCP_TELEMETRY_DIR", _home_state() / "dashboard")


def memory_dir() -> Path:
    return _env_path("MAC_MCP_MEMORY_DIR", _home_state() / "memory")


def lessons_dir() -> Path:
    return _env_path("MAC_MCP_LESSON_DIR", _home_state() / "role-learning")


def transactions_dir() -> Path:
    return _env_path("MAC_MCP_FILE_JOURNAL_DIR", state_dir() / "transactions")


def runs_dir() -> Path:
    return state_dir() / "runs"


def vault_dir() -> Path:
    return state_dir() / "vault"


def provider_usage_db() -> Path:
    return state_dir() / "state" / "provider_usage.sqlite3"


def scheduled_tasks_db() -> Path:
    return state_dir() / "state" / "scheduled_tasks.sqlite3"


def jobs_dir() -> Path:
    return SERVER_DIR / "jobs"


def agents_dir() -> Path:
    return SERVER_DIR / "agents"


def agent_teams_dir() -> Path:
    return SERVER_DIR / "agent_teams"


def audit_log() -> Path:
    return SERVER_DIR / "audit.log"
