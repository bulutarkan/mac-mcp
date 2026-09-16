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

from .runtime_settings import load_runtime_settings, settings_path
from .public_endpoint import (
    PublicEndpointError,
    inspect_cloudflare_credential,
    public_health_url,
    resolve_public_endpoint,
)
from .security import dashboard_token_path
from .version import __version__

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


def _check_settings() -> CheckResult:
    started = time.perf_counter()
    path = settings_path()
    if not path.exists():
        return result(
            "settings.json", "config", INFO, "SETTINGS_NOT_CREATED",
            "Runtime settings file has not been created; built-in defaults are in use.",
            started=started, details={"path": _safe_path(path)},
        )
    if path.is_symlink():
        return result(
            "settings.json", "config", WARN, "SETTINGS_SYMLINK",
            "Runtime settings file is a symbolic link.", started=started,
            remediation="Prefer a regular owner-controlled settings file.",
            details={"path": _safe_path(path)},
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return result(
            "settings.json", "config", FAIL, "SETTINGS_INVALID_JSON",
            "Runtime settings file cannot be parsed.", started=started,
            remediation="Fix ~/.mac-mcp/settings.json JSON syntax.",
            details={"path": _safe_path(path), "error_type": type(exc).__name__},
        )
    if not isinstance(payload, dict):
        return result(
            "settings.json", "config", FAIL, "SETTINGS_ROOT_NOT_OBJECT",
            "Runtime settings root must be a JSON object.", started=started,
            remediation="Replace the settings root with a JSON object.",
            details={"path": _safe_path(path)},
        )
    return result(
        "settings.json", "config", PASS, "SETTINGS_VALID",
        "Runtime settings JSON is valid.", started=started,
        details={"path": _safe_path(path), "mode": _mode(path), "top_level_keys": sorted(str(k) for k in payload)[:24]},
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


def _check_accessibility() -> CheckResult:
    started = time.perf_counter()
    osa = shutil.which("osascript") or "/usr/bin/osascript"
    if not Path(osa).exists():
        return result(
            "permissions.accessibility", "permissions", FAIL, "OSASCRIPT_MISSING",
            "AppleScript is unavailable, so Accessibility cannot be checked.", started=started,
        )
    try:
        proc = subprocess.run(
            [osa, "-e", 'tell application "System Events" to return UI elements enabled'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        return result(
            "permissions.accessibility", "permissions", WARN, "ACCESSIBILITY_CHECK_TIMEOUT",
            "Accessibility status check timed out.", started=started,
            remediation="Open System Settings > Privacy & Security > Accessibility and verify the app/terminal running Mac MCP is allowed.",
        )
    except OSError:
        return result(
            "permissions.accessibility", "permissions", WARN, "ACCESSIBILITY_CHECK_UNAVAILABLE",
            "Accessibility status could not be queried.", started=started,
        )
    text = (proc.stdout or "").strip().lower()
    enabled = proc.returncode == 0 and text == "true"
    if enabled:
        return result(
            "permissions.accessibility", "permissions", PASS, "ACCESSIBILITY_ENABLED",
            "macOS Accessibility UI scripting is enabled for this execution context.", started=started,
        )
    return result(
        "permissions.accessibility", "permissions", FAIL, "ACCESSIBILITY_DISABLED",
        "macOS Accessibility UI scripting is not enabled for this execution context.", started=started,
        remediation="Grant Accessibility permission in System Settings > Privacy & Security > Accessibility, then rerun doctor.",
        details={"probe_exit_code": int(proc.returncode)},
    )


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
        pid = _read_pid_file(path)
        if pid is None:
            continue
        if _pid_alive(pid):
            return result(
                f"process.{name}", "process", PASS, f"{name.upper()}_RUNNING",
                f"{name} process is running.", started=started,
                details={"pid": pid, "managed_by": "pid_file", "pid_file": _safe_path(path)},
            )
        stale.append(_safe_path(path))

    pid = _launchctl_pid(label)
    if pid is not None:
        return result(
            f"process.{name}", "process", PASS, f"{name.upper()}_RUNNING",
            f"{name} process is running under launchctl.", started=started,
            details={"pid": pid, "managed_by": "launchctl", "label": label, **({"stale_pid_files": stale} if stale else {})},
        )

    if name == "server":
        _, port = _local_host_port()
        pid = _listener_pid(port)
        if pid is not None:
            return result(
                "process.server", "process", PASS, "SERVER_RUNNING",
                "server process is listening on the configured port.", started=started,
                details={"pid": pid, "managed_by": "listener", "port": port, **({"stale_pid_files": stale} if stale else {})},
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
        f"No managed {name} process was detected.", started=started,
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


def _check_cloudflared_dependency() -> CheckResult:
    started = time.perf_counter()
    candidates = [
        shutil.which("cloudflared"),
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
    ]
    found = next((item for item in candidates if item and Path(item).is_file() and os.access(item, os.X_OK)), None)
    if found:
        return result(
            "dependency.cloudflared", "dependencies", PASS, "CLOUDFLARED_AVAILABLE",
            "cloudflared is available (Cloudflare Tunnel public endpoint).", started=started,
            details={"path": _safe_path(found)},
        )
    return result(
        "dependency.cloudflared", "dependencies", WARN, "CLOUDFLARED_MISSING",
        "cloudflared is not installed (Cloudflare Tunnel public endpoint).", started=started,
        remediation="Install cloudflared only if you plan to use Cloudflare Tunnel mode.",
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
        return result(
            "public.endpoint", "network", WARN, "PUBLIC_ENDPOINT_HTTP_ERROR",
            "Configured public endpoint is not healthy from this Mac.", started=started,
            remediation="Check the selected public endpoint provider/reverse proxy and rerun doctor.",
            details={**details, "http_status": int(exc.code)},
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return result(
            "public.endpoint", "network", WARN, "PUBLIC_ENDPOINT_UNREACHABLE",
            "Configured public endpoint could not be reached from this Mac.", started=started,
            remediation="Check DNS/TLS/routing for the configured public endpoint and rerun doctor.",
            details={**details, "error_type": type(exc).__name__},
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
    return _check_managed_process("ngrok")


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
    return _check_managed_process("cloudflared")


def doctor_checks() -> list[CheckResult]:
    checks: list[Callable[[], CheckResult]] = [
        _check_runtime,
        _check_state_dir,
        _check_settings,
        _check_disk,
        lambda: _binary_result("dependency.osascript", "osascript", required=True, purpose="macOS automation"),
        lambda: _binary_result("dependency.cliclick", "cliclick", required=False, purpose="coordinate/input fallback"),
        lambda: _binary_result("dependency.ngrok", "ngrok", required=False, purpose="ngrok public tunnel"),
        _check_cloudflared_dependency,
        _check_accessibility,
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
            rows.append(check())
        except Exception as exc:  # a doctor must report broken checks, not crash the whole report
            rows.append(result(
                f"internal.{getattr(check, '__name__', 'check')}", "internal", WARN,
                "DIAGNOSTIC_CHECK_ERROR", "A diagnostic check could not complete.",
                details={"error_type": type(exc).__name__},
            ))
    return rows


def build_report(checks: Iterable[CheckResult], *, kind: str = "doctor", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = [c.to_dict() if isinstance(c, CheckResult) else dict(c) for c in checks]
    counts = {status: sum(1 for row in rows if row.get("status") == status) for status in (PASS, WARN, FAIL, INFO)}
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "ok": counts[FAIL] == 0,
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
    lines.append(
        f"Result: {'OK' if report.get('ok') else 'ISSUES FOUND'} | "
        f"pass={counts.get(PASS, 0)} warn={counts.get(WARN, 0)} fail={counts.get(FAIL, 0)} info={counts.get(INFO, 0)}"
    )
    return "\n".join(lines)
