from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .runtime_settings import load_runtime_settings, load_runtime_settings_state, settings_path
from .managed_process import (
    listener_owner,
    listener_pids,
    matches_role,
    port_conflict_advice,
    port_is_listening,
    process_snapshot,
    validate_process_record,
)
from .permission_probe import DENIED, GRANTED, NOT_DETERMINED, probe_permissions
from .cli_bootstrap import default_cli_path, launcher_kind, runtime_entrypoint
from .runtime_resolver import resolve_cloudflared_binary, resolve_ngrok_binary
from .public_endpoint import (
    PublicEndpointError,
    inspect_cloudflare_credential,
    public_health_url,
    resolve_public_endpoint,
)
from .policy import (
    configured_permission_profile_name,
    evaluate_profile,
    permission_profile_name,
    permission_profile_scope,
    resolve_risk,
)
from .security import dashboard_token_path, load_settings
from .version import __version__
from .update_state import UpdateStateError, read_deployed_commit, read_update_state, update_root, update_state_path

SCHEMA_VERSION = 1
PASS = "pass"
WARN = "warn"
FAIL = "fail"
INFO = "info"
_VALID_STATUSES = {PASS, WARN, FAIL, INFO}


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    category: str
    status: str
    reason_code: str
    summary: str
    duration_ms: int = 0
    remediation: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        item = asdict(self)
        if item["details"] is None:
            item.pop("details")
        if item["remediation"] is None:
            item.pop("remediation")
        return item


def result(
    check_id: str,
    category: str,
    status: str,
    reason_code: str,
    summary: str,
    *,
    started: float | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> CheckResult:
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid diagnostic status: {status}")
    duration_ms = 0 if started is None else max(0, int((time.perf_counter() - started) * 1000))
    return CheckResult(
        check_id=check_id,
        category=category,
        status=status,
        reason_code=reason_code,
        summary=summary,
        duration_ms=duration_ms,
        remediation=remediation,
        details=details,
    )


def state_dir() -> Path:
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()


def runtime_root() -> Path:
    return Path(__file__).resolve().parent.parent


def installed_app_candidates() -> list[Path]:
    configured = os.getenv("MAC_MCP_MENU_APP", "").strip()
    rows = [Path(configured).expanduser()] if configured else []
    rows.extend([Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")])
    return rows


def _safe_path(path: Path | str) -> str:
    value = str(Path(path).expanduser())
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return value


def _mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except OSError:
        return None


def _owner_is_current_user(path: Path) -> bool | None:
    if not hasattr(os, "getuid"):
        return None
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return None


def _check_runtime() -> CheckResult:
    started = time.perf_counter()
    compatible = sys.version_info >= (3, 10)
    return result(
        "runtime.python",
        "runtime",
        PASS if compatible else FAIL,
        "PYTHON_RUNTIME_OK" if compatible else "PYTHON_TOO_OLD",
        f"Python {platform.python_version()} on {platform.machine() or 'unknown architecture'}",
        started=started,
        remediation=None if compatible else "Install Python 3.10 or newer.",
        details={"python": platform.python_version(), "mac_mcp_version": __version__, "architecture": platform.machine()},
    )


def _check_cli_installation() -> CheckResult:
    started = time.perf_counter()
    launcher = default_cli_path()
    entrypoint = runtime_entrypoint(runtime_root())
    kind = launcher_kind(launcher)
    broken_symlink = launcher.is_symlink() and not launcher.exists()
    launcher_executable = launcher.exists() and os.access(launcher, os.X_OK)
    entrypoint_executable = entrypoint.is_file() and os.access(entrypoint, os.X_OK)
    found = shutil.which("mac-mcp")
    on_path = False
    if found:
        try:
            on_path = Path(found).resolve() == launcher.resolve()
        except OSError:
            on_path = Path(found).expanduser() == launcher.expanduser()

    details = {
        "version": __version__,
        "launcher": _safe_path(launcher),
        "launcher_kind": kind,
        "launcher_executable": launcher_executable,
        "runtime_entrypoint": _safe_path(entrypoint),
        "runtime_entrypoint_executable": entrypoint_executable,
        "found_on_path": _safe_path(found) if found else None,
        "on_path": on_path,
        "absolute_invocation": str(launcher.expanduser()),
        "deployed_commit": (read_deployed_commit(runtime_root()) or "")[:12] or None,
    }

    if broken_symlink:
        return result(
            "cli.installation", "cli", FAIL, "CLI_BROKEN_SYMLINK",
            "Mac MCP CLI launcher is a broken symlink.", started=started,
            remediation=f"Reinstall the launcher or run {launcher.expanduser()} after repairing the runtime.",
            details=details,
        )
    if not launcher_executable:
        return result(
            "cli.installation", "cli", FAIL, "CLI_LAUNCHER_MISSING",
            "Mac MCP CLI launcher is missing or not executable.", started=started,
            remediation=f"Re-run the installer or recreate the launcher at {launcher.expanduser()}.",
            details=details,
        )
    if not entrypoint_executable:
        return result(
            "cli.installation", "cli", FAIL, "CLI_RUNTIME_ENTRYPOINT_MISSING",
            "The installed CLI launcher cannot reach the runtime entrypoint.", started=started,
            remediation="Repair the runtime virtual environment or reinstall Mac MCP.",
            details=details,
        )
    if not on_path:
        return result(
            "cli.installation", "cli", WARN, "CLI_NOT_ON_PATH",
            "Mac MCP CLI is healthy but is not visible on the current noninteractive PATH.", started=started,
            remediation=f"Use {launcher.expanduser()} or add {launcher.parent.expanduser()} to PATH for this shell.",
            details=details,
        )
    return result(
        "cli.installation", "cli", PASS, "CLI_AVAILABLE",
        "Mac MCP CLI launcher and runtime entrypoint are healthy.", started=started,
        details=details,
    )


def _check_state_dir() -> CheckResult:
    started = time.perf_counter()
    path = state_dir()
    if not path.exists():
        return result(
            "state.directory", "state", WARN, "STATE_DIR_MISSING",
            "Mac MCP state directory does not exist yet.", started=started,
            remediation="Start Mac MCP once to initialize local state.",
            details={"path": _safe_path(path)},
        )
    if not path.is_dir():
        return result(
            "state.directory", "state", FAIL, "STATE_PATH_NOT_DIRECTORY",
            "Mac MCP state path is not a directory.", started=started,
            remediation="Move the conflicting path and restart Mac MCP.",
            details={"path": _safe_path(path), "mode": _mode(path)},
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    owner = _owner_is_current_user(path)
    safe = owner is not False and (mode & 0o077) == 0
    return result(
        "state.directory", "state", PASS if safe else WARN,
        "STATE_DIR_SECURE" if safe else "STATE_DIR_PERMISSIONS_BROAD",
        "State directory is owner-only." if safe else "State directory permissions are broader than recommended.",
        started=started,
        remediation=None if safe else "Use owner-only permissions (0700) for ~/.mac-mcp.",
        details={"path": _safe_path(path), "mode": oct(mode), "owned_by_current_user": owner},
    )


def _check_update_recovery_state() -> CheckResult:
    started = time.perf_counter()
    path = update_state_path()
    try:
        payload = read_update_state(path)
    except UpdateStateError as exc:
        return result(
            "update.recovery", "update", FAIL, "UPDATE_STATE_CORRUPT",
            "Updater state is corrupt or unreadable; automatic update recovery is blocked.", started=started,
            remediation="Inspect ~/.mac-mcp/update/state.json and updater backups before running another update.",
            details={"state_error": exc.code, "error_type": exc.error_type},
        )
    if payload is None:
        return result(
            "update.recovery", "update", INFO, "UPDATE_STATE_ABSENT",
            "No persisted updater state is present yet.", started=started,
        )
    update_status = str(payload.get("status") or "")
    transaction_version = int(payload.get("transaction_version") or 0)
    incomplete_states = {
        "prepared", "repo_updating", "repo_updated", "runtime_syncing", "runtime_synced",
        "dependency_activating", "dependencies_activated", "restarting", "health_verified",
        "marker_committed", "dependency_commit_started", "dependency_committed", "rolling_back",
    }
    if transaction_version == 1 and update_status in incomplete_states:
        return result(
            "update.recovery", "update", FAIL, "UPDATE_RECOVERY_REQUIRED",
            f"Updater transaction stopped during '{update_status}' and requires crash recovery.",
            started=started,
            remediation="Run `mac-mcp update` again to recover the interrupted transaction before making other runtime changes.",
            details={"update_status": update_status, "transaction_id": payload.get("transaction_id")},
        )
    if update_status == "recovery_failed":
        return result(
            "update.recovery", "update", FAIL, "UPDATE_RECOVERY_FAILED",
            "Automatic updater crash recovery failed and manual inspection is required.",
            started=started,
            remediation="Inspect updater state and backups under ~/.mac-mcp/update before retrying the update.",
            details={"update_status": update_status, "transaction_id": payload.get("transaction_id")},
        )

    runtime_rollback = payload.get("runtime_rollback") if isinstance(payload.get("runtime_rollback"), dict) else {}
    rollback_health = payload.get("rollback_health") if isinstance(payload.get("rollback_health"), dict) else {}
    rollback_status = str(runtime_rollback.get("status") or "")
    health_status = str(rollback_health.get("status") or "")
    details = {
        "update_status": payload.get("status"),
        "runtime_rollback_status": rollback_status or None,
        "rollback_health_status": health_status or None,
    }
    if rollback_status in {"restore_unverified", "failed"} or health_status == "failed":
        return result(
            "update.recovery", "update", FAIL, "UPDATE_ROLLBACK_DEGRADED",
            "The last updater rollback did not restore a verified healthy service.", started=started,
            remediation="Run `mac-mcp status` and `mac-mcp doctor`; if health is not OK, repair or restart the runtime before updating again.",
            details=details,
        )
    if str(payload.get("status") or "") == "failed":
        return result(
            "update.recovery", "update", WARN, "UPDATE_LAST_RUN_FAILED",
            "The last updater run failed, but no degraded rollback state is recorded.", started=started,
            remediation="Review updater state and logs before retrying the update.",
            details=details,
        )
    return result(
        "update.recovery", "update", PASS, "UPDATE_STATE_HEALTHY",
        "Updater state does not report a degraded rollback.", started=started,
        details=details,
    )


def _check_update_storage() -> CheckResult:
    """Updater backups and staging: size, orphans, and the last cleanup failures."""
    started = time.perf_counter()
    root = update_root()
    backups = [
        child for child in (root / "backups").glob("*")
        if child.is_dir() and (child / "manifest.json").is_file()
    ] if (root / "backups").is_dir() else []
    total = 0
    for child in backups:
        for dirpath, _dirs, files in os.walk(child):
            for name in files:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    staging = [child for child in (root / "staging").glob("mac-mcp-update-*")] if (root / "staging").is_dir() else []
    try:
        payload = read_update_state(update_state_path()) or {}
    except UpdateStateError:
        payload = {}
    retention = payload.get("backup_retention") if isinstance(payload.get("backup_retention"), dict) else {}
    details = {
        "backups": len(backups),
        "backups_mib": round(total / (1024 * 1024), 1),
        "staging_dirs": len(staging),
        "last_cleanup_failures": list(retention.get("failures") or [])[:10],
    }
    if details["last_cleanup_failures"]:
        return result(
            "update.storage", "update", WARN, "UPDATE_CLEANUP_FAILED",
            "The last update could not remove some old backups.", started=started,
            remediation="Check permissions under ~/.mac-mcp/update/backups; the next update retries the cleanup.",
            details=details,
        )
    return result(
        "update.storage", "update", PASS, "UPDATE_STORAGE_OK",
        f"{len(backups)} update backups ({details['backups_mib']} MiB); old ones and orphaned staging are cleaned on each update.",
        started=started, details=details,
    )


def _check_agent_worktrees() -> CheckResult:
    """Disk used by agent worktrees still on disk, and how many hold unreviewed work."""
    started = time.perf_counter()
    from .agent_worktrees import retained_worktrees, worktree_retention_s
    from .security import BASE_DIR

    rows = retained_worktrees(BASE_DIR / "agents")
    retention_days = round(worktree_retention_s() / 86400, 1)
    details = {
        "count": len(rows),
        "mib": round(sum(row["bytes"] for row in rows) / (1024 * 1024), 1),
        "oldest_days": round(max((row["age_s"] for row in rows), default=0) / 86400, 1),
        "with_unreviewed_changes": sum(1 for row in rows if row["pending_changes"]),
        "retention_days": retention_days,
    }
    if not rows:
        summary = "No agent worktrees are kept on disk."
    else:
        summary = (
            f"{details['count']} agent worktrees use {details['mib']} MiB; "
            f"{details['with_unreviewed_changes']} hold unreviewed changes."
        )
    return result(
        "agents.worktrees", "agents", INFO, "AGENT_WORKTREES", summary, started=started,
        remediation=(
            "Finished agents' worktrees with nothing left to review are removed after "
            f"{retention_days} days (MAC_MCP_AGENT_WORKTREE_RETENTION_DAYS); review or discard the others."
        ) if rows else None,
        details=details,
    )


def _check_server_supervisor() -> CheckResult:
    """Whether the crash supervisor is loaded, and what its last pass and recovery did."""
    started = time.perf_counter()
    from . import supervisor

    root = state_dir()
    intent = supervisor.read_intent(root)
    state = supervisor.read_state(root)
    try:
        loaded = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/com.macmcp.supervisor"],
            capture_output=True, timeout=3,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        loaded = False
    recovery = {k: v for k, v in (state.get("last_recovery") or {}).items() if k != "log_tail"}
    details = {
        "loaded": loaded,
        "intent": intent.get("desired"),
        "last_result": state.get("last_result"),
        "last_check_age_s": round(time.time() - float(state["last_check_at"]), 1) if state.get("last_check_at") else None,
        "last_recovery": recovery or None,
    }
    if os.getenv("MAC_MCP_SUPERVISOR", "1").strip().lower() in {"0", "false", "no", "off"}:
        return result("server.supervisor", "server", INFO, "SUPERVISOR_DISABLED",
                      "Crash supervision is turned off (MAC_MCP_SUPERVISOR=0).", started=started, details=details)
    if intent.get("desired") != "running":
        return result("server.supervisor", "server", INFO, "SUPERVISOR_IDLE",
                      "The server was stopped on purpose; the supervisor will not start it.", started=started,
                      details=details)
    if not loaded:
        return result("server.supervisor", "server", WARN, "SUPERVISOR_NOT_LOADED",
                      "The server is not supervised: if it crashes, nothing restarts it.", started=started,
                      remediation="Run `mac-mcp start` (or restart) to load the crash supervisor.", details=details)
    details["last_tunnel_recovery"] = state.get("last_tunnel_recovery")
    if state.get("last_result") in {"tunnel_backoff", "tunnel_recovery_failed"}:
        return result("server.supervisor", "server", WARN, "SUPERVISOR_TUNNEL_FAILING",
                      "The server is up, but the supervisor could not bring the ngrok tunnel back.", started=started,
                      remediation="Check `mac-mcp logs ngrok` and the network, then `mac-mcp restart`.",
                      details=details)
    if state.get("last_result") in {"backoff", "recovery_failed"}:
        return result("server.supervisor", "server", WARN, "SUPERVISOR_RECOVERY_FAILING",
                      "The supervisor could not bring the server back; it is backing off.", started=started,
                      remediation="Run `mac-mcp doctor` and `mac-mcp logs server`, fix the cause, then `mac-mcp start`.",
                      details=details)
    summary = "The crash supervisor is watching the server."
    if recovery:
        summary += f" Last recovery: {recovery.get('reason')} -> {recovery.get('result')}."
    return result("server.supervisor", "server", PASS, "SUPERVISOR_ACTIVE", summary, started=started, details=details)


def _check_runtime_python() -> CheckResult:
    """The runtime venv's interpreter: present, supported, right architecture, and not on a fragile path."""
    started = time.perf_counter()
    from .venv_repair import inspect_venv

    runtime = Path(__file__).resolve().parent.parent
    report = inspect_venv(runtime)
    repair = f"python3 {runtime / 'mcp_server' / 'venv_repair.py'} repair"
    if report["status"] == "missing":
        return result("runtime.venv", "runtime", INFO, "RUNTIME_VENV_ABSENT",
                      "This checkout has no runtime virtual environment.", started=started, details=report)
    if report["status"] != "ok":
        return result("runtime.venv", "runtime", FAIL, "RUNTIME_VENV_BROKEN",
                      f"The runtime virtual environment is unusable ({report['status']}).", started=started,
                      remediation=f"Rebuild it safely with: {repair}", details=report)
    if report["pinned_to_versioned_path"]:
        return result("runtime.venv", "runtime", WARN, "RUNTIME_VENV_FRAGILE",
                      "The runtime venv points at a versioned Homebrew Python path, which the next "
                      "Python upgrade removes.", started=started,
                      remediation=f"Rebuild it on Homebrew's stable opt/ path with: {repair} --force",
                      details=report)
    return result("runtime.venv", "runtime", PASS, "RUNTIME_VENV_OK",
                  f"Runtime Python {report.get('version')} ({report.get('machine')}) is present.",
                  started=started, details=report)


def _check_security_chain() -> CheckResult:
    """The retained security events still match their hash chain and latest checkpoint."""
    started = time.perf_counter()
    import sqlite3

    from . import audit_chain

    telemetry_dir = Path(os.getenv("MAC_MCP_TELEMETRY_DIR", str(Path.home() / ".mac-mcp" / "dashboard"))).expanduser()
    db_path = telemetry_dir / "telemetry.sqlite3"
    if not db_path.is_file():
        return result("security.audit_chain", "security", INFO, "SECURITY_CHAIN_ABSENT",
                      "No security events have been recorded yet.", started=started)
    try:
        # Read-only queries on a normal connection: mode=ro cannot open a WAL database.
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(security_events)").fetchall()}
            if "chain_seq" not in columns:
                return result("security.audit_chain", "security", INFO, "SECURITY_CHAIN_PENDING",
                              "Security events are chained once the updated server records its first one.",
                              started=started)
            report = audit_chain.verify(conn, db_path)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return result("security.audit_chain", "security", WARN, "SECURITY_CHAIN_UNREADABLE",
                      "The security record could not be read for verification.", started=started,
                      details={"error_type": type(exc).__name__})
    if not report.get("ok"):
        return result("security.audit_chain", "security", FAIL, "SECURITY_CHAIN_BROKEN",
                      f"The security record no longer verifies ({report.get('problem')} at event "
                      f"#{report.get('seq')}): events were edited or removed outside Mac MCP.",
                      started=started,
                      remediation="Keep a copy of ~/.mac-mcp/dashboard for inspection; new events keep chaining from here.",
                      details=report)
    return result("security.audit_chain", "security", PASS, "SECURITY_CHAIN_OK",
                  f"{report.get('checked')} security events verify against their hash chain.",
                  started=started, details=report)


def _check_settings() -> CheckResult:
    started = time.perf_counter()
    path = settings_path()
    state = load_runtime_settings_state(path)
    common = {
        "path": _safe_path(path),
        "provider_fail_closed": state.status != "ok",
        "load_status": state.status,
    }

    if state.status == "missing":
        return result(
            "settings.json", "config", INFO, "SETTINGS_NOT_CREATED",
            "Runtime settings file has not been created; delegated providers are fail-closed until settings are created.",
            started=started,
            remediation="Run the installer or save Settings once to create an owner-controlled settings.json.",
            details=common,
        )
    if path.is_symlink():
        return result(
            "settings.json", "config", WARN, "SETTINGS_SYMLINK",
            "Runtime settings file is a symbolic link.", started=started,
            remediation="Prefer a regular owner-controlled settings file.",
            details={**common, "mode": _mode(path)},
        )
    if state.status == "unreadable":
        return result(
            "settings.json", "config", FAIL, "SETTINGS_UNREADABLE",
            "Runtime settings file cannot be read; delegated providers are disabled until it is repaired.",
            started=started,
            remediation="Fix ownership/permissions for ~/.mac-mcp/settings.json and rerun doctor.",
            details={**common, "error_type": state.error_type},
        )
    if state.status == "invalid_json":
        return result(
            "settings.json", "config", FAIL, "SETTINGS_INVALID_JSON",
            "Runtime settings JSON is invalid; delegated providers are disabled until it is repaired.",
            started=started,
            remediation="Fix ~/.mac-mcp/settings.json JSON syntax and rerun doctor.",
            details={**common, "error_type": state.error_type},
        )
    if state.status == "root_not_object":
        return result(
            "settings.json", "config", FAIL, "SETTINGS_ROOT_NOT_OBJECT",
            "Runtime settings root must be a JSON object; delegated providers are disabled until it is repaired.",
            started=started,
            remediation="Replace the settings root with a JSON object and rerun doctor.",
            details=common,
        )

    payload = state.data
    return result(
        "settings.json", "config", PASS, "SETTINGS_VALID",
        "Runtime settings JSON is valid.", started=started,
        details={
            **common,
            "mode": _mode(path),
            "top_level_keys": sorted(str(k) for k in payload)[:24],
        },
    )


def _check_permission_profile_scope() -> CheckResult:
    started = time.perf_counter()
    configured = configured_permission_profile_name()
    active = permission_profile_name()
    scope = permission_profile_scope(configured)
    details = {
        "configured_profile": configured,
        "active_profile": active,
        "configured_profile_scope": scope,
        "normalized": configured != active,
        "profile_source": "env" if os.getenv("MAC_MCP_PERMISSION_PROFILE") is not None else "default",
    }
    if configured != active:
        reason = (
            "GLOBAL_PROFILE_DELEGATED_ONLY"
            if scope == "delegated_only"
            else "GLOBAL_PROFILE_INVALID"
        )
        return result(
            "permissions.profile_scope", "config", WARN, reason,
            (
                f"Configured permission profile '{configured}' is not a global server preset; "
                f"Mac MCP is using '{active}' instead."
            ),
            started=started,
            remediation=(
                "Choose trusted, standard, or read_only as the global permission profile. "
                "Use developer/browser_only only through scoped delegated-agent credentials."
            ),
            details=details,
        )
    return result(
        "permissions.profile_scope", "config", PASS, "GLOBAL_PROFILE_VALID",
        f"Global permission profile '{active}' is valid.", started=started,
        details=details,
    )


def _check_permission_coherence() -> CheckResult:
    started = time.perf_counter()
    settings = load_settings()
    profile = permission_profile_name()
    profile_source = "env" if os.getenv("MAC_MCP_PERMISSION_PROFILE") is not None else "default"
    _, shell_risk = resolve_risk("run_command", {"command": "pwd"})
    decision = evaluate_profile(profile, shell_risk)
    if settings.allow_shell and not decision.allowed:
        return result(
            "permissions.shell_profile", "config", WARN, "SHELL_FLAG_PROFILE_DENY",
            "Shell is enabled by MCP_ALLOW_SHELL, but the active permission profile denies raw execution.",
            started=started,
            remediation="Use the trusted permission profile only when shell execution is intentionally required, or disable MCP_ALLOW_SHELL.",
            details={
                "profile": profile,
                "profile_source": profile_source,
                "allow_shell": True,
                "effective_available": False,
                "denied_capabilities": list(decision.denied_capabilities),
            },
        )
    if not settings.allow_shell:
        return result(
            "permissions.shell_profile", "config", PASS, "SHELL_FEATURE_DISABLED",
            "Shell execution is disabled by MCP_ALLOW_SHELL.", started=started,
            details={"profile": profile, "profile_source": profile_source, "allow_shell": False, "effective_available": False},
        )
    return result(
        "permissions.shell_profile", "config", PASS, "SHELL_EFFECTIVELY_AVAILABLE",
        "Shell execution is enabled and allowed by the active permission profile.", started=started,
        details={"profile": profile, "profile_source": profile_source, "allow_shell": True, "effective_available": True},
    )


def _check_disk() -> CheckResult:
    started = time.perf_counter()
    target = runtime_root()
    usage = shutil.disk_usage(target)
    free_gib = usage.free / (1024 ** 3)
    status = PASS if free_gib >= 5 else WARN if free_gib >= 1 else FAIL
    code = "DISK_SPACE_OK" if status == PASS else "DISK_SPACE_LOW" if status == WARN else "DISK_SPACE_CRITICAL"
    return result(
        "system.disk", "system", status, code,
        f"{free_gib:.1f} GiB free on the Mac MCP volume.", started=started,
        remediation=None if status == PASS else "Free disk space before running long downloads, builds, or browser tasks.",
        details={"free_gib": round(free_gib, 2), "total_gib": round(usage.total / (1024 ** 3), 2)},
    )


def _binary_result(check_id: str, name: str, *, required: bool, purpose: str) -> CheckResult:
    started = time.perf_counter()
    found = shutil.which(name)
    if found:
        return result(
            check_id, "dependencies", PASS, f"{name.upper().replace('-', '_')}_AVAILABLE",
            f"{name} is available ({purpose}).", started=started,
            details={"path": _safe_path(found)},
        )
    status = FAIL if required else WARN
    return result(
        check_id, "dependencies", status, f"{name.upper().replace('-', '_')}_MISSING",
        f"{name} is not installed ({purpose}).", started=started,
        remediation=(f"Install {name} before using this capability." if required else f"Install {name} to enable the optional {purpose} capability."),
    )


def _server_permissions() -> dict[str, Any] | None:
    """Ask the running server for its own permissions; consent is recorded per process."""
    host, port = _local_host_port()
    try:
        token = dashboard_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        status_code, payload = _request_json(
            f"http://{host}:{port}/dashboard/api/diagnostics/permissions",
            headers={"Authorization": f"Bearer {token}"}, timeout=8.0,
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, ValueError):
        return None
    if status_code != 200 or not isinstance(payload.get("permissions"), dict):
        return None
    return payload


def _permission_rows() -> list[CheckResult]:
    started = time.perf_counter()
    payload = _server_permissions()
    context = "server"
    if payload is None:
        # Without a server, the doctor can only describe its own process.
        payload = probe_permissions()
        context = "doctor_process"
    identity = payload.get("context") or {}
    listed_as = str(identity.get("listed_as") or "the app running Mac MCP")
    where = "" if context == "server" else " (for this terminal: Mac MCP is not running, so its own permissions could not be read)"
    rows: list[CheckResult] = []

    def row(key: str, status: str, reason: str, summary: str, remediation: str | None = None, **extra: Any) -> None:
        entry = payload["permissions"].get(key) or {}
        details = {
            "state": entry.get("state"),
            "context": context,
            "listed_as": listed_as,
            "executable": _safe_path(identity["executable"]) if identity.get("executable") else None,
            "features": entry.get("features"),
            "settings_path": entry.get("settings_path"),
            "recovery": {"action": "open_system_settings", "url": entry.get("settings_url")},
            **extra,
        }
        rows.append(result(f"permissions.{key}", "permissions", status, reason, summary + where,
                           started=started, remediation=remediation, details=details))

    def allow(key: str) -> str:
        entry = payload["permissions"].get(key) or {}
        features = ", ".join(entry.get("features") or [])
        return f"Allow \u201c{listed_as}\u201d in {entry.get('settings_path')} (used for {features}), then rerun doctor."

    ax = (payload["permissions"].get("accessibility") or {}).get("state")
    if ax == GRANTED:
        row("accessibility", PASS, "ACCESSIBILITY_ENABLED", "Accessibility is allowed.")
    elif ax == DENIED:
        # Only the server's own missing permission breaks Mac MCP.
        row("accessibility", FAIL if context == "server" else WARN, "ACCESSIBILITY_DISABLED",
            "Accessibility is not allowed.", allow("accessibility"))
    else:
        row("accessibility", INFO, "ACCESSIBILITY_UNKNOWN", "Accessibility state could not be determined.",
            allow("accessibility"))

    screen = (payload["permissions"].get("screen_recording") or {}).get("state")
    if screen == GRANTED:
        row("screen_recording", PASS, "SCREEN_RECORDING_ALLOWED", "Screen Recording is allowed.")
    elif screen == DENIED:
        row("screen_recording", WARN, "SCREEN_RECORDING_DENIED",
            "Screen Recording is not allowed; screenshots and visual observation will fail.", allow("screen_recording"))
    else:
        row("screen_recording", INFO, "SCREEN_RECORDING_UNKNOWN", "Screen Recording state could not be determined.",
            allow("screen_recording"))

    automation = payload["permissions"].get("automation") or {}
    targets = [t for t in automation.get("targets") or [] if isinstance(t, dict)]
    denied = [t["app"] for t in targets if t.get("state") == DENIED]
    asked = [t["app"] for t in targets if t.get("state") == NOT_DETERMINED]
    allowed = [t["app"] for t in targets if t.get("state") == GRANTED]
    not_running = [t["app"] for t in targets if t.get("state") == "not_running"]
    extra = {"targets": targets}
    if denied:
        row("automation", WARN, "AUTOMATION_DENIED", "Automation is turned off for " + ", ".join(denied) + ".",
            f"In {automation.get('settings_path')}, expand \u201c{listed_as}\u201d and turn on "
            + ", ".join(denied) + ", then rerun doctor.", **extra)
    elif asked:
        row("automation", INFO, "AUTOMATION_NOT_ASKED_YET",
            "macOS will ask the first time Mac MCP controls " + ", ".join(asked) + ".", **extra)
    elif allowed:
        tail = f" Not running, so not checked: {', '.join(not_running)}." if not_running else ""
        row("automation", PASS, "AUTOMATION_ALLOWED", "Automation is allowed for " + ", ".join(allowed) + "." + tail, **extra)
    else:
        row("automation", INFO, "AUTOMATION_NOT_CHECKED",
            "Automation could not be checked because none of the controlled apps is running.", **extra)

    voice = (payload["permissions"].get("microphone") or {}).get("identity") or {}
    row("microphone", INFO, "MICROPHONE_NOT_CHECKED",
        f"Microphone access belongs to {voice.get('name', 'the voice helper')}, which macOS asks the first time you use voice. "
        f"If voice reports that access was denied, allow it in {(payload['permissions'].get('microphone') or {}).get('settings_path')}.",
        voice_helper=voice)
    return rows


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _launchctl_pid(label: str) -> int | None:
    if platform.system() != "Darwin" or not hasattr(os, "getuid"):
        return None
    launchctl = shutil.which("launchctl") or "/bin/launchctl"
    if not Path(launchctl).exists():
        return None
    try:
        proc = subprocess.run(
            [launchctl, "print", f"gui/{os.getuid()}/{label}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    state_running = False
    pid: int | None = None
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line == "state = running":
            state_running = True
        elif line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
    return pid if state_running and pid and _pid_alive(pid) else None


def _listener_pid(port: int) -> int | None:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(lsof).exists():
        return None
    try:
        proc = subprocess.run(
            [lsof, "-tiTCP:" + str(int(port)), "-sTCP:LISTEN"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode not in {0, 1}:
        return None
    for raw in (proc.stdout or "").splitlines():
        try:
            pid = int(raw.strip())
        except ValueError:
            continue
        if pid > 0 and _pid_alive(pid):
            return pid
    return None


def _check_managed_process(name: str) -> CheckResult:
    started = time.perf_counter()
    if name not in {"server", "ngrok", "cloudflared"}:
        raise ValueError("unsupported managed process")

    _, port = _local_host_port()
    project_root = Path(__file__).resolve().parent.parent
    role = name

    if name == "server":
        label = "mac-mcp-uvicorn"
        candidates = [state_dir() / "mac-mcp.pid", Path("/tmp/mac-mcp-uvicorn.pid")]
    elif name == "ngrok":
        label = "mac-mcp-ngrok"
        candidates = [state_dir() / "ngrok.pid", Path("/tmp/mac-mcp-ngrok.pid")]
    else:
        label = "mac-mcp-cloudflared"
        candidates = [state_dir() / "cloudflared.pid", Path("/tmp/mac-mcp-cloudflared.pid")]

    stale: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        validation = validate_process_record(
            path,
            role,
            port=port,
            project_root=project_root if role == "server" else None,
        )
        details = {
            "pid": validation.pid,
            "managed_by": "pid_file",
            "pid_file": _safe_path(path),
            "identity_status": validation.status,
            "identity_reason": validation.reason,
            "record_format": validation.record_format,
            "port": port,
        }
        if validation.valid:
            if validation.snapshot is not None:
                details.update({
                    "process_start_time": validation.snapshot.start_time,
                    "process_executable": validation.snapshot.executable,
                    "process_cwd": validation.snapshot.cwd,
                })
            return result(
                f"process.{name}", "process", PASS, f"{name.upper()}_RUNNING_VERIFIED",
                f"{name} process is running with a verified fingerprint.", started=started,
                details=details,
            )
        if validation.legacy_match:
            return result(
                f"process.{name}", "process", WARN, f"{name.upper()}_PID_LEGACY_UNFINGERPRINTED",
                f"{name} process matches the expected role, but its PID record has no fingerprint.",
                started=started,
                remediation="Run mac-mcp status or restart Mac MCP once to migrate the legacy PID record.",
                details=details,
            )
        if validation.status in {"dead", "invalid_record"}:
            stale.append(_safe_path(path))
            continue
        if validation.status in {"identity_mismatch", "role_mismatch", "unverifiable"}:
            return result(
                f"process.{name}", "process", FAIL, f"{name.upper()}_PID_IDENTITY_UNVERIFIED",
                f"Recorded {name} PID does not prove ownership of the live process.",
                started=started,
                remediation="Do not kill the PID manually. Run mac-mcp status and inspect the process before repairing stale PID state.",
                details=details,
            )

    pid = _launchctl_pid(label)
    if pid is not None:
        snapshot = process_snapshot(pid)
        if snapshot and matches_role(
            snapshot,
            role,
            port=port,
            project_root=project_root if role == "server" else None,
        ):
            return result(
                f"process.{name}", "process", PASS, f"{name.upper()}_LAUNCHD_VERIFIED",
                f"{name} process is running under the expected launchctl label with verified identity.",
                started=started,
                details={
                    "pid": pid,
                    "managed_by": "launchctl",
                    "label": label,
                    "identity_status": "verified",
                    "process_start_time": snapshot.start_time,
                    "process_executable": snapshot.executable,
                    "process_cwd": snapshot.cwd,
                    **({"stale_pid_files": stale} if stale else {}),
                },
            )
        return result(
            f"process.{name}", "process", FAIL, f"{name.upper()}_LAUNCHD_IDENTITY_UNVERIFIED",
            f"{name} launchctl label is loaded, but its live process identity is not the expected role.",
            started=started,
            remediation="Inspect the launchd job before booting it out; Mac MCP will not treat it as owned.",
            details={
                "pid": pid,
                "managed_by": "launchctl",
                "label": label,
                "identity_status": "role_mismatch" if snapshot else "metadata_unavailable",
            },
        )

    if name == "server":
        owned: list[int] = []
        foreign: list[int] = []
        for listener_pid in listener_pids(port):
            snapshot = process_snapshot(listener_pid)
            if snapshot and matches_role(
                snapshot,
                "server",
                port=port,
                project_root=project_root,
            ):
                owned.append(listener_pid)
            else:
                foreign.append(listener_pid)
        if not owned and not foreign and port_is_listening(port):
            return result(
                "process.server", "process", FAIL, "SERVER_PORT_FOREIGN_LISTENER",
                "Configured Mac MCP port is occupied, but listener ownership could not be resolved.",
                started=started,
                remediation=port_conflict_advice(port, []),
                details={
                    "recovery": {"action": "open_settings", "pane": "advanced"},
                    "port": port,
                    "foreign_listener_pids": [],
                    "verified_listener_pids": [],
                    "listener_pid_resolution": "unavailable",
                    **({"stale_pid_files": stale} if stale else {}),
                },
            )
        if foreign:
            owners = [listener_owner(pid) for pid in foreign]
            named = ", ".join(owner["name"] for owner in owners if owner["name"]) or "another program"
            return result(
                "process.server", "process", FAIL, "SERVER_PORT_FOREIGN_LISTENER",
                f"Configured Mac MCP port {port} is used by {named}.",
                started=started,
                remediation=port_conflict_advice(port, owners),
                details={
                    "port": port,
                    "foreign_listener_pids": foreign,
                    "foreign_listeners": [
                        {**owner, "executable": _safe_path(owner["executable"]) if owner["executable"] else None}
                        for owner in owners
                    ],
                    "recovery": {"action": "open_settings", "pane": "advanced"},
                    "verified_listener_pids": owned,
                    "listener_pid_resolution": "resolved",
                    **({"stale_pid_files": stale} if stale else {}),
                },
            )
        if len(owned) == 1:
            snapshot = process_snapshot(owned[0])
            return result(
                "process.server", "process", PASS, "SERVER_LISTENER_VERIFIED",
                "A Mac MCP server listener was verified by argv, port, and working directory.",
                started=started,
                details={
                    "pid": owned[0],
                    "managed_by": "verified_listener",
                    "port": port,
                    "identity_status": "verified",
                    "process_start_time": snapshot.start_time if snapshot else None,
                    "process_executable": snapshot.executable if snapshot else None,
                    "process_cwd": snapshot.cwd if snapshot else None,
                    **({"stale_pid_files": stale} if stale else {}),
                },
            )
        if len(owned) > 1:
            return result(
                "process.server", "process", FAIL, "SERVER_LISTENER_AMBIGUOUS",
                "Multiple Mac MCP-like listeners were detected for the configured port.",
                started=started,
                remediation="Resolve the duplicate listeners before starting or stopping Mac MCP.",
                details={"port": port, "verified_listener_pids": owned},
            )

    if stale:
        return result(
            f"process.{name}", "process", WARN, f"{name.upper()}_PID_STALE",
            f"Recorded {name} PID state is stale.", started=started,
            remediation=f"Restart Mac MCP to refresh stale {name} process metadata.",
            details={"stale_pid_files": stale},
        )
    return result(
        f"process.{name}", "process", INFO, f"{name.upper()}_PROCESS_NOT_DETECTED",
        f"No verified managed {name} process was detected.", started=started,
    )

def _local_host_port() -> tuple[str, int]:
    host = os.getenv("MAC_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    raw_port = os.getenv("MAC_MCP_PORT", "").strip()
    if not raw_port:
        server = load_runtime_settings().get("server", {})
        if isinstance(server, dict):
            value = server.get("port")
            raw_port = str(value).strip() if isinstance(value, (int, str)) else ""
    try:
        port = int(raw_port or "8000")
    except ValueError:
        port = 8000
    if port < 1 or port > 65535:
        port = 8000
    return host, port


def _request_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 2.0) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(256 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        return int(response.status), payload if isinstance(payload, dict) else {}


def _check_server_health() -> CheckResult:
    started = time.perf_counter()
    host, port = _local_host_port()
    url = f"http://{host}:{port}/health"
    try:
        status_code, payload = _request_json(url, timeout=2.0)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return result(
            "server.health", "server", FAIL, "SERVER_HEALTH_UNREACHABLE",
            "Local Mac MCP health endpoint is not reachable.", started=started,
            remediation="Run `mac-mcp status`, then start or restart the server if needed.",
            details={"url": url, "error_type": type(exc).__name__},
        )
    healthy = status_code == 200 and payload.get("ok") is True
    return result(
        "server.health", "server", PASS if healthy else FAIL,
        "SERVER_HEALTH_OK" if healthy else "SERVER_HEALTH_INVALID",
        "Local Mac MCP health endpoint is healthy." if healthy else "Local health endpoint returned an unexpected response.",
        started=started, details={"url": url, "http_status": status_code, "server": payload.get("server")},
    )


def _check_dashboard_token() -> CheckResult:
    started = time.perf_counter()
    path = dashboard_token_path()
    if not path.exists():
        return result(
            "auth.dashboard_token", "auth", WARN, "DASHBOARD_TOKEN_MISSING",
            "Dashboard credential has not been created.", started=started,
            remediation="Start/restart Mac MCP once so it can create the local dashboard credential.",
            details={"path": _safe_path(path)},
        )
    if path.is_symlink():
        return result(
            "auth.dashboard_token", "auth", FAIL, "DASHBOARD_TOKEN_SYMLINK",
            "Dashboard credential path is a symbolic link.", started=started,
            remediation="Replace it with a regular owner-controlled file.",
            details={"path": _safe_path(path)},
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    owner = _owner_is_current_user(path)
    safe = owner is not False and (mode & 0o077) == 0
    return result(
        "auth.dashboard_token", "auth", PASS if safe else FAIL,
        "DASHBOARD_TOKEN_METADATA_OK" if safe else "DASHBOARD_TOKEN_PERMISSIONS_UNSAFE",
        "Dashboard credential file metadata is owner-only." if safe else "Dashboard credential permissions/ownership are unsafe.",
        started=started,
        remediation=None if safe else "Ensure the credential is owned by your user and mode 0600.",
        details={"path": _safe_path(path), "mode": oct(mode), "owned_by_current_user": owner, "size_bytes": path.stat().st_size},
    )


def _installed_app() -> Path | None:
    return next((p for p in installed_app_candidates() if p.exists()), None)


def _check_menu_app() -> CheckResult:
    started = time.perf_counter()
    app = _installed_app()
    if app is None:
        return result(
            "companion.menu_app", "companion", WARN, "MENU_APP_NOT_INSTALLED",
            "Mac MCP menu app is not installed in a standard location.", started=started,
            remediation="Install the menu app to get native settings, status, and Safari companion support.",
        )
    return result(
        "companion.menu_app", "companion", PASS, "MENU_APP_INSTALLED",
        "Mac MCP menu app is installed.", started=started, details={"path": _safe_path(app)},
    )


def _check_safari_companion() -> CheckResult:
    started = time.perf_counter()
    app = _installed_app()
    if app is None:
        return result(
            "companion.safari", "companion", WARN, "SAFARI_COMPANION_APP_MISSING",
            "Safari Visual Companion cannot be inspected because Mac MCP.app is missing.", started=started,
        )
    plugins = app / "Contents" / "PlugIns"
    matches = list(plugins.glob("*.appex")) if plugins.exists() else []
    present = any((item / "Contents" / "MacOS").is_dir() for item in matches)
    return result(
        "companion.safari", "companion", PASS if present else WARN,
        "SAFARI_COMPANION_BUNDLED" if present else "SAFARI_COMPANION_NOT_BUNDLED",
        "Safari Visual Companion is bundled in Mac MCP.app." if present else "Safari Visual Companion bundle was not found in Mac MCP.app.",
        started=started,
        remediation=None if present else "Reinstall/rebuild Mac MCP.app, then enable the Safari extension.",
        details={"extension_bundle_count": len(matches)},
    )


def _chrome_companion_dir() -> Path:
    configured = os.getenv("MAC_MCP_RUNTIME_DIR", "").strip()
    runtime = Path(configured).expanduser() if configured else runtime_root()
    return runtime / "menu_app" / "ChromeVisualCompanion"


def _check_chrome_companion_files() -> CheckResult:
    started = time.perf_counter()
    directory = _chrome_companion_dir()
    required = [directory / "manifest.json", directory / "background.js", directory / "bridge_config.js"]
    missing = [p.name for p in required if not p.exists()]
    if missing:
        return result(
            "companion.chrome_files", "companion", WARN, "CHROME_COMPANION_INCOMPLETE",
            "Chrome Visual Companion files are incomplete.", started=started,
            remediation="Reinstall/update Mac MCP and reload the unpacked Chrome extension.",
            details={"path": _safe_path(directory), "missing": missing},
        )
    config = directory / "bridge_config.js"
    mode = _mode(config)
    return result(
        "companion.chrome_files", "companion", PASS, "CHROME_COMPANION_FILES_OK",
        "Chrome Visual Companion files and bridge configuration are present.", started=started,
        details={"path": _safe_path(directory), "bridge_config_mode": mode},
    )


def _check_runtime_companion_state() -> CheckResult:
    """Query authenticated localhost runtime diagnostics without exposing the token."""
    started = time.perf_counter()
    token_path = dashboard_token_path()
    if not token_path.exists() or token_path.is_symlink():
        return result(
            "companion.runtime", "companion", INFO, "RUNTIME_DIAGNOSTICS_AUTH_UNAVAILABLE",
            "Active companion connection state could not be queried because the dashboard credential is unavailable.", started=started,
        )
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        return result(
            "companion.runtime", "companion", INFO, "RUNTIME_DIAGNOSTICS_AUTH_UNAVAILABLE",
            "Active companion connection state could not be queried.", started=started,
        )
    host, port = _local_host_port()
    url = f"http://{host}:{port}/dashboard/api/diagnostics/runtime"
    try:
        status_code, payload = _request_json(url, headers={"Authorization": f"Bearer {token}"}, timeout=2.0)
    except urllib.error.HTTPError as exc:
        return result(
            "companion.runtime", "companion", INFO, "RUNTIME_DIAGNOSTICS_ENDPOINT_UNAVAILABLE",
            "Running Mac MCP does not expose the local diagnostics endpoint yet.", started=started,
            details={"http_status": int(exc.code)},
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return result(
            "companion.runtime", "companion", INFO, "RUNTIME_DIAGNOSTICS_UNREACHABLE",
            "Active browser companion connection state could not be queried.", started=started,
        )
    connected = bool(payload.get("chrome_companion_connected"))
    return result(
        "companion.runtime", "companion", PASS if connected else WARN,
        "CHROME_COMPANION_CONNECTED" if connected else "CHROME_COMPANION_DISCONNECTED",
        "Chrome Visual Companion is actively connected." if connected else "Chrome Visual Companion is installed/configured but is not currently connected.",
        started=started,
        remediation=None if connected else "Open Chrome, load/reload the Mac MCP Chrome companion, then rerun doctor.",
        details={"http_status": status_code, "runtime_version": payload.get("version"), "chrome_companion_connected": connected},
    )


def _selected_public_mode() -> str | None:
    try:
        return resolve_public_endpoint().mode
    except PublicEndpointError:
        return None


_INSTALL_HINTS = {"ngrok": "brew install ngrok", "cloudflared": "brew install cloudflared"}


def _selected_dependency_missing(name: str, started: float, source: str) -> CheckResult:
    return result(
        f"dependency.{name}", "dependencies", FAIL, f"{name.upper()}_MISSING_FOR_SELECTED_MODE",
        f"{name} is the selected public endpoint but is not installed.", started=started,
        remediation=f"Install it with: {_INSTALL_HINTS[name]} (or choose another mode in Settings > Connections), then restart Mac MCP.",
        details={"source": source, "recovery": {"action": "open_settings", "pane": "connections"}},
    )


def _check_ngrok_dependency() -> CheckResult:
    started = time.perf_counter()
    resolved = resolve_ngrok_binary()
    if resolved.path:
        return result(
            "dependency.ngrok", "dependencies", PASS, "NGROK_AVAILABLE",
            "ngrok is available (ngrok public tunnel).", started=started,
            details={"path": _safe_path(resolved.path), "source": resolved.source},
        )
    if _selected_public_mode() == "ngrok":
        return _selected_dependency_missing("ngrok", started, resolved.source)
    return result(
        "dependency.ngrok", "dependencies", WARN, "NGROK_MISSING",
        "ngrok is not installed (ngrok public tunnel).", started=started,
        remediation="Install ngrok only if you plan to use ngrok public endpoint mode.",
        details={"source": resolved.source},
    )


def _check_cloudflared_dependency() -> CheckResult:
    started = time.perf_counter()
    resolved = resolve_cloudflared_binary()
    if resolved.path:
        return result(
            "dependency.cloudflared", "dependencies", PASS, "CLOUDFLARED_AVAILABLE",
            "cloudflared is available (Cloudflare Tunnel public endpoint).", started=started,
            details={"path": _safe_path(resolved.path), "source": resolved.source},
        )
    if _selected_public_mode() == "cloudflare":
        return _selected_dependency_missing("cloudflared", started, resolved.source)
    return result(
        "dependency.cloudflared", "dependencies", WARN, "CLOUDFLARED_MISSING",
        "cloudflared is not installed (Cloudflare Tunnel public endpoint).", started=started,
        remediation="Install cloudflared only if you plan to use Cloudflare Tunnel mode.",
        details={"source": resolved.source},
    )


def _check_cloudflare_credential() -> CheckResult:
    started = time.perf_counter()
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError as exc:
        mode = str(load_runtime_settings().get("server", {}).get("public_endpoint_mode", "") or "").strip().lower()
        if mode != "cloudflare":
            return result(
                "credential.cloudflare", "security", INFO, "CLOUDFLARE_NOT_SELECTED",
                "Cloudflare Tunnel is not the selected public endpoint mode.", started=started,
            )
        return result(
            "credential.cloudflare", "security", FAIL, "CLOUDFLARE_CREDENTIAL_MISSING",
            "Cloudflare Tunnel mode is selected but no usable credential is configured.", started=started,
            remediation="Save the tunnel token from Mac MCP Settings > Advanced or configure a named tunnel.",
            details={"error": str(exc)},
        )
    if public.mode != "cloudflare":
        return result(
            "credential.cloudflare", "security", INFO, "CLOUDFLARE_NOT_SELECTED",
            "Cloudflare Tunnel is not the selected public endpoint mode.", started=started,
            details={"mode": public.mode},
        )
    if not public.cloudflare_token_file:
        return result(
            "credential.cloudflare", "security", INFO, "CLOUDFLARE_NAMED_TUNNEL_CREDENTIALS",
            "Cloudflare uses named-tunnel credentials managed outside Mac MCP.", started=started,
        )
    credential = inspect_cloudflare_credential(public.cloudflare_token_file)
    details = {"path": _safe_path(credential.path), "reason": credential.reason}
    if credential.configured and credential.secure:
        return result(
            "credential.cloudflare", "security", PASS, "CLOUDFLARE_CREDENTIAL_SECURE",
            "Cloudflare Tunnel credential is configured as an owner-only 0600 file.", started=started,
            details=details,
        )
    return result(
        "credential.cloudflare", "security", FAIL, "CLOUDFLARE_CREDENTIAL_UNSAFE",
        "Cloudflare Tunnel credential file is missing or has unsafe ownership/permissions.", started=started,
        remediation="Save/replace the token from Mac MCP Settings so it is written atomically as an owner-only 0600 file.",
        details=details,
    )


def _public_route_advice(mode: str, health_url: str) -> tuple[str, dict[str, Any]]:
    """Point at the failing component: the local server, the tunnel process, or the route."""
    _, port = _local_host_port()
    if not port_is_listening(port):
        return ("Mac MCP itself is not running, so nothing can answer publicly. Start Mac MCP, then rerun doctor.",
                {"action": "restart"})
    if mode in {"cloudflare", "ngrok"}:
        name = "cloudflared" if mode == "cloudflare" else "ngrok"
        if _check_managed_process(name).status != PASS:
            return (f"The {name} tunnel is not running. Restart Mac MCP, then rerun doctor.", {"action": "restart"})
        where = ("the Cloudflare dashboard (Zero Trust > Networks > Tunnels > Public hostname)"
                 if mode == "cloudflare" else "your ngrok dashboard (Domains)")
        return (f"{name} is running but {health_url} does not answer. Check in {where} that the hostname routes to "
                f"http://127.0.0.1:{port}, then read mac-mcp logs {name}.",
                {"action": "view_logs", "log": name})
    return (f"Mac MCP is running but {health_url} does not answer. Check that your reverse proxy forwards this "
            f"address to http://127.0.0.1:{port} and that DNS and TLS are valid.", {"action": "open_settings", "pane": "connections"})


def _check_public_endpoint() -> CheckResult:
    started = time.perf_counter()
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError as exc:
        return result(
            "public.endpoint", "network", FAIL, "PUBLIC_ENDPOINT_CONFIG_INVALID",
            "Public endpoint configuration is invalid.", started=started,
            remediation="Fix the public endpoint mode/URL in Settings or the MAC_MCP_PUBLIC_* environment variables.",
            details={"error": str(exc)},
        )
    if public.mode == "none":
        return result(
            "public.endpoint", "network", INFO, "PUBLIC_ENDPOINT_LOCAL_ONLY",
            "Public endpoint mode is Local only; no external endpoint is expected.", started=started,
            details={"mode": public.mode, "source": public.source},
        )

    health_url = public_health_url(public)
    details = {"mode": public.mode, "endpoint": public.endpoint_url, "source": public.source}
    try:
        status_code, payload = _request_json(
            str(health_url), headers={"User-Agent": f"Mac-MCP-Doctor/{__version__}"}, timeout=3.0
        )
    except urllib.error.HTTPError as exc:
        advice, recovery = _public_route_advice(public.mode, str(health_url))
        return result(
            "public.endpoint", "network", WARN, "PUBLIC_ENDPOINT_HTTP_ERROR",
            "Configured public endpoint is not healthy from this Mac.", started=started,
            remediation=advice,
            details={**details, "http_status": int(exc.code), "recovery": recovery},
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        advice, recovery = _public_route_advice(public.mode, str(health_url))
        return result(
            "public.endpoint", "network", WARN, "PUBLIC_ENDPOINT_UNREACHABLE",
            "Configured public endpoint could not be reached from this Mac.", started=started,
            remediation=advice,
            details={**details, "error_type": type(exc).__name__, "recovery": recovery},
        )
    healthy = status_code == 200 and bool(payload.get("ok"))
    return result(
        "public.endpoint", "network", PASS if healthy else WARN,
        "PUBLIC_ENDPOINT_HEALTHY" if healthy else "PUBLIC_ENDPOINT_UNEXPECTED_RESPONSE",
        "Configured public endpoint is healthy." if healthy else "Configured public endpoint returned an unexpected health response.",
        started=started,
        remediation=None if healthy else "Check the reverse proxy/tunnel target and rerun doctor.",
        details={**details, "health_url": health_url, "http_status": status_code},
    )


def _check_ngrok_for_selected_mode() -> CheckResult:
    started = time.perf_counter()
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError:
        return result(
            "process.ngrok", "process", INFO, "NGROK_MODE_UNRESOLVED",
            "ngrok process state was not evaluated because public endpoint configuration is invalid.", started=started,
        )
    if public.mode != "ngrok":
        return result(
            "process.ngrok", "process", INFO, "NGROK_NOT_SELECTED",
            "ngrok is not the selected public endpoint mode.", started=started,
            details={"mode": public.mode},
        )
    return _tunnel_stopped_is_actionable(_check_managed_process("ngrok"), "ngrok")


def _tunnel_stopped_is_actionable(row: CheckResult, name: str) -> CheckResult:
    if row.reason_code != f"{name.upper()}_PROCESS_NOT_DETECTED":
        return row
    return result(
        row.check_id, "process", WARN, f"{name.upper()}_STOPPED",
        f"The selected {name} tunnel is not running, so the public endpoint cannot work.",
        remediation="Restart Mac MCP (menu bar > Restart, or mac-mcp restart); if it stops again, read mac-mcp logs "
        + name + ".",
        details={"recovery": {"action": "restart"}},
    )


def _check_cloudflare_for_selected_mode() -> CheckResult:
    started = time.perf_counter()
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError:
        return result(
            "process.cloudflared", "process", INFO, "CLOUDFLARE_MODE_UNRESOLVED",
            "cloudflared process state was not evaluated because public endpoint configuration is invalid.", started=started,
        )
    if public.mode != "cloudflare":
        return result(
            "process.cloudflared", "process", INFO, "CLOUDFLARE_NOT_SELECTED",
            "Cloudflare Tunnel is not the selected public endpoint mode.", started=started,
            details={"mode": public.mode},
        )
    return _tunnel_stopped_is_actionable(_check_managed_process("cloudflared"), "cloudflared")


def doctor_checks() -> list[CheckResult]:
    checks: list[Callable[[], CheckResult | list[CheckResult]]] = [
        _check_runtime,
        _check_cli_installation,
        _check_state_dir,
        _check_update_recovery_state,
        _check_update_storage,
        _check_agent_worktrees,
        _check_server_supervisor,
        _check_runtime_python,
        _check_security_chain,
        _check_settings,
        _check_permission_profile_scope,
        _check_permission_coherence,
        _check_disk,
        lambda: _binary_result("dependency.osascript", "osascript", required=True, purpose="macOS automation"),
        lambda: _binary_result("dependency.cliclick", "cliclick", required=False, purpose="coordinate/input fallback"),
        _check_ngrok_dependency,
        _check_cloudflared_dependency,
        _permission_rows,
        lambda: _check_managed_process("server"),
        _check_ngrok_for_selected_mode,
        _check_cloudflare_for_selected_mode,
        _check_cloudflare_credential,
        _check_server_health,
        _check_public_endpoint,
        _check_dashboard_token,
        _check_menu_app,
        _check_safari_companion,
        _check_chrome_companion_files,
        _check_runtime_companion_state,
    ]
    rows: list[CheckResult] = []
    for check in checks:
        try:
            produced = check()
            rows.extend(produced if isinstance(produced, list) else [produced])
        except Exception as exc:  # a doctor must report broken checks, not crash the whole report
            rows.append(result(
                f"internal.{getattr(check, '__name__', 'check')}", "internal", WARN,
                "DIAGNOSTIC_CHECK_ERROR", "A diagnostic check could not complete.",
                details={"error_type": type(exc).__name__},
            ))
    return rows


_PUBLIC_ENDPOINT_STATES = {
    "PUBLIC_ENDPOINT_HEALTHY": "healthy",
    "PUBLIC_ENDPOINT_LOCAL_ONLY": "local_only",
    "PUBLIC_ENDPOINT_CONFIG_INVALID": "invalid",
}


def _public_endpoint_state(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        if row.get("check_id") == "public.endpoint":
            return _PUBLIC_ENDPOINT_STATES.get(str(row.get("reason_code") or ""), "unavailable")
    return None


def build_report(checks: Iterable[CheckResult], *, kind: str = "doctor", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = [c.to_dict() if isinstance(c, CheckResult) else dict(c) for c in checks]
    counts = {status: sum(1 for row in rows if row.get("status") == status) for status in (PASS, WARN, FAIL, INFO)}
    local_ok = counts[FAIL] == 0
    public_state = _public_endpoint_state(rows)
    # A selected but unreachable public endpoint is only a WARN row, yet it must
    # not yield an unqualified healthy result; local_ok keeps the local verdict.
    public_down = public_state == "unavailable"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "ok": local_ok and not public_down,
        "local_ok": local_ok,
        "health": "failed" if not local_ok else ("degraded" if public_down else "healthy"),
        "public_endpoint": public_state,
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "checks": rows,
    }
    if extra:
        payload.update(extra)
    return payload


def run_doctor() -> dict[str, Any]:
    return build_report(doctor_checks(), kind="doctor")


def _support_bundle_payload(report: dict[str, Any]) -> dict[str, Any]:
    # Intentionally structured and allowlisted. Never scrape environment values,
    # .env/settings contents, logs, prompts, browser URLs, cookies, or credential files.
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "mac-mcp-support-bundle",
        "generated_at": report.get("generated_at"),
        "version": __version__,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "doctor": report,
        "privacy": {
            "raw_env_included": False,
            "logs_included": False,
            "settings_contents_included": False,
            "credential_values_included": False,
            "prompts_or_chat_included": False,
        },
    }


def write_support_bundle(report: dict[str, Any], destination: str | Path | None = None) -> Path:
    directory = state_dir() / "support"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"doctor-{stamp}.json"
    else:
        path = Path(destination).expanduser()
        if path.exists() and path.is_dir():
            path = path / "mac-mcp-doctor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = _support_bundle_payload(report)
    fd, tmp_name = tempfile.mkstemp(prefix=".mac-mcp-support-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return path


def format_report(report: dict[str, Any], *, title: str = "Mac MCP Doctor") -> str:
    icons = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", INFO: "INFO"}
    lines = [title, "=" * len(title)]
    for row in report.get("checks", []):
        status = str(row.get("status") or INFO)
        lines.append(f"[{icons.get(status, status.upper())}] {row.get('check_id')}: {row.get('summary')}")
        remediation = row.get("remediation")
        if remediation and status in {WARN, FAIL}:
            lines.append(f"       Fix: {remediation}")
    counts = report.get("counts", {})
    lines.append("")
    if report.get("health") == "degraded":
        verdict = "DEGRADED (local runtime OK; selected public endpoint unavailable)"
    else:
        verdict = "OK" if report.get("ok") else "ISSUES FOUND"
    lines.append(
        f"Result: {verdict} | "
        f"pass={counts.get(PASS, 0)} warn={counts.get(WARN, 0)} fail={counts.get(FAIL, 0)} info={counts.get(INFO, 0)}"
    )
    return "\n".join(lines)
