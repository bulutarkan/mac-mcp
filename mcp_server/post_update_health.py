from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from . import release_trust
from .policy import permission_profile_name, tool_availability
from .public_endpoint import PublicEndpointError, public_health_url, resolve_public_endpoint
from .chatgpt_client_gate import CHATGPT_PANEL_TOOLS
from .runtime_resolver import resolve_cloudflared_binary, resolve_ngrok_binary
from .security import Settings
from .tool_summaries import CORE_TOOL_NAMES
from .update_state import read_deployed_commit, update_root, update_state_path
from .version import __version__

SCHEMA_VERSION = 1
_GATE_REPORT_NAME = "health-gate.json"
_PENDING_CACHE: tuple[str | None, str | None, dict[str, Any] | None] | None = None
_GATE_TARGET: str | None = None
_GATE_TASK: asyncio.Task[dict[str, Any]] | None = None
_GATE_RESULT: dict[str, Any] | None = None


@dataclass(frozen=True)
class HealthCheck:
    check_id: str
    status: str
    critical: bool
    summary: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload["details"] is None:
            payload.pop("details")
        return payload


def _runtime_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _repo_root() -> Path:
    configured = os.getenv("MAC_MCP_REPO", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Projects" / "mac-mcp"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _read_update_state() -> dict[str, Any]:
    path = update_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def health_gate_report_path() -> Path:
    return update_root() / _GATE_REPORT_NAME


def read_health_gate_report() -> dict[str, Any] | None:
    path = health_gate_report_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_health_gate_report(payload: dict[str, Any]) -> None:
    path = health_gate_report_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".health-gate.", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _runtime_port() -> int:
    raw = os.getenv("MAC_MCP_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    argv = list(sys.argv[1:])
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                break
        if item.startswith("--port="):
            try:
                return int(item.split("=", 1)[1])
            except ValueError:
                break
    return 8765


def _local_base_url() -> str:
    return f"http://127.0.0.1:{_runtime_port()}"


def _signed_repo_mismatch_context(runtime: Path) -> dict[str, Any] | None:
    global _PENDING_CACHE
    repo = _repo_root()
    if not (repo / ".git").exists():
        return None
    deployed = read_deployed_commit(runtime)
    head = _git(repo, "rev-parse", "HEAD").lower()
    if not deployed or not head or deployed.lower() == head:
        return None

    cache_key = (deployed.lower(), head)
    if _PENDING_CACHE is not None and _PENDING_CACHE[:2] == cache_key:
        return _PENDING_CACHE[2]
    try:
        verified = release_trust.verify_release_commit(repo, head)
    except release_trust.ReleaseVerificationError:
        context = None
    else:
        context = {
            "active": True,
            "source": "signed_repo_mismatch",
            "from_commit": deployed.lower(),
            "target_commit": head,
            "release_id": verified.release_id,
            "release_version": verified.version,
            "repo": str(repo),
        }
    _PENDING_CACHE = (cache_key[0], cache_key[1], context)
    return context


def pending_update_context(runtime: Path | None = None) -> dict[str, Any] | None:
    runtime = runtime or _runtime_root()
    deployed = read_deployed_commit(runtime)
    state = _read_update_state()
    status = str(state.get("status") or "").strip().lower()
    target = str(state.get("to_commit") or "").strip().lower()
    if status in {"starting", "running", "health_gate"} and target and target != str(deployed or "").lower():
        return {
            "active": True,
            "source": f"update_state:{status}",
            "from_commit": str(state.get("from_commit") or deployed or "").strip().lower() or None,
            "target_commit": target,
            "release_id": state.get("release_id"),
            "release_version": state.get("release_version"),
            "repo": str(state.get("repo") or _repo_root()),
        }
    if status == "rolling_back":
        return None
    return _signed_repo_mismatch_context(runtime)


def _check(
    check_id: str,
    ok: bool,
    *,
    critical: bool,
    pass_summary: str,
    fail_summary: str,
    details: dict[str, Any] | None = None,
) -> HealthCheck:
    return HealthCheck(
        check_id=check_id,
        status="pass" if ok else ("fail" if critical else "warn"),
        critical=critical,
        summary=pass_summary if ok else fail_summary,
        details=details,
    )


def _menu_app_path() -> Path:
    configured = os.getenv("MAC_MCP_APP_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Applications" / "Mac MCP.app"


def _menu_process_pids(app: Path) -> list[int]:
    executable = app / "Contents" / "MacOS" / "MacMCPMenu"
    if not executable.exists():
        return []
    try:
        target = str(executable.resolve())
    except OSError:
        target = str(executable)
    proc = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[1] != target:
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            pass
    return pids


def _codesign_ok(app: Path) -> bool:
    if not app.exists():
        return False
    proc = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    return proc.returncode == 0


def _public_basic_health_url() -> tuple[str | None, str, str]:
    try:
        public = resolve_public_endpoint()
    except PublicEndpointError as exc:
        return None, "invalid", type(exc).__name__
    if public.mode == "none":
        return None, "none", public.source
    health = public_health_url(public)
    if not health:
        return None, public.mode, public.source
    parsed = urlsplit(health)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "probe=basic", "")), public.mode, public.source


async def _auth_smoke(settings: Settings, local_base: str) -> tuple[bool, int | None, str | None]:
    if settings.allow_no_auth:
        return True, None, None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.head(f"{local_base}/mcp")
        return response.status_code in {401, 403}, response.status_code, None
    except Exception as exc:
        return False, None, type(exc).__name__


async def _public_endpoint_smoke(public_url: str) -> tuple[bool, int | None, str | None]:
    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
                response = await client.get(
                    public_url,
                    headers={"User-Agent": f"Mac-MCP-Update-Gate/{__version__}"},
                )
            last_status = response.status_code
            payload = response.json()
            if response.status_code == 200 and payload.get("ok") is True:
                return True, response.status_code, None
            last_error = "unexpected_response"
        except Exception as exc:
            last_error = type(exc).__name__
        if attempt < 3:
            await asyncio.sleep(0.4)
    return False, last_status, last_error


def expected_core_tools(profile: str) -> set[str]:
    """Core tools this release must list for a non-ChatGPT client under ``profile``.

    Tools the profile denies are intentionally absent, and extra tools from
    MAC_MCP_CORE_EXTRA_TOOLS are optional, so neither is required.
    """
    return {
        name for name in CORE_TOOL_NAMES - CHATGPT_PANEL_TOOLS
        if tool_availability(profile, name).get("available") is True
    }


async def _mcp_smoke(settings: Settings, local_base: str) -> tuple[bool, dict[str, Any], str | None]:
    headers: dict[str, str] = {}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    last_error: str | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(headers=headers, timeout=3.0) as client:
                async with streamable_http_client(
                    f"{local_base}/mcp",
                    http_client=client,
                ) as (read_stream, write_stream, _session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
            names = sorted({tool.name for tool in tools.tools})
            return True, {
                "protocol_version": initialized.protocolVersion,
                "tool_count": len(names),
                "tool_names": names,
            }, None
        except Exception as exc:
            last_error = type(exc).__name__
        if attempt < 2:
            await asyncio.sleep(0.35)
    return False, {}, last_error


async def run_post_update_health_gate(
    settings: Settings,
    context: dict[str, Any],
    *,
    local_base_url: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    local_base = (local_base_url or _local_base_url()).rstrip("/")
    target = str(context.get("target_commit") or "").strip().lower()
    repo = Path(str(context.get("repo") or _repo_root())).expanduser()
    checks: list[HealthCheck] = []

    # Target release / runtime version.
    verified = None
    try:
        head = _git(repo, "rev-parse", "HEAD").lower()
        verified = release_trust.verify_release_commit(repo, target)
        release_ok = bool(target and head == target and verified.target_commit == target)
    except (release_trust.ReleaseVerificationError, OSError):
        head = _git(repo, "rev-parse", "HEAD").lower()
        release_ok = False
    checks.append(_check(
        "release.target",
        release_ok,
        critical=True,
        pass_summary="Repository is on the verified target release.",
        fail_summary="Repository is not on the verified target release.",
        details={"target": target[:12], "head": head[:12] if head else None},
    ))

    expected_version = verified.version if verified is not None else str(context.get("release_version") or "")
    checks.append(_check(
        "runtime.version",
        not expected_version or __version__ == expected_version,
        critical=True,
        pass_summary="Runtime version matches the verified release.",
        fail_summary="Runtime version does not match the verified release.",
        details={"runtime_version": __version__, "release_version": expected_version or None},
    ))

    # Auth must remain fail-closed when configured.
    auth_ok, auth_status, auth_error = await _auth_smoke(settings, local_base)
    checks.append(_check(
        "mcp.auth_required",
        auth_ok,
        critical=True,
        pass_summary="MCP authentication behavior matches configuration.",
        fail_summary="MCP authentication behavior is unsafe or unreachable.",
        details={"allow_no_auth": settings.allow_no_auth, "unauthenticated_status": auth_status, "error_type": auth_error},
    ))

    mcp_ok, mcp_details, mcp_error = await _mcp_smoke(settings, local_base)
    checks.append(_check(
        "mcp.initialize_tools",
        mcp_ok and int(mcp_details.get("tool_count") or 0) > 0,
        critical=True,
        pass_summary="MCP initialize and tools/list succeeded.",
        fail_summary="MCP initialize or tools/list failed.",
        details=(
            {
                "protocol_version": mcp_details.get("protocol_version"),
                "tool_count": mcp_details.get("tool_count", 0),
                "error_type": mcp_error,
            }
        ),
    ))

    profile = permission_profile_name()
    expected_tools = expected_core_tools(profile)
    missing_tools = sorted(expected_tools - set(mcp_details.get("tool_names") or []))
    checks.append(_check(
        "mcp.core_tools",
        mcp_ok and not missing_tools,
        critical=True,
        pass_summary="Every core tool the permission profile allows is registered.",
        fail_summary="Core tools the permission profile allows are missing.",
        details={
            "permission_profile": profile,
            "expected_count": len(expected_tools),
            "missing": missing_tools[:40],
        },
    ))
    run_command_expected = bool(settings.allow_shell and tool_availability(profile, "run_command").get("available"))
    actual_tools = set(mcp_details.get("tool_names") or [])
    run_command_present = "run_command" in actual_tools
    surface_ok = (not run_command_expected) or run_command_present
    checks.append(_check(
        "permissions.tool_surface",
        surface_ok,
        critical=True,
        pass_summary="Critical tool availability matches the active permission profile.",
        fail_summary="Critical tool availability does not match the active permission profile.",
        details={
            "permission_profile": profile,
            "shell_enabled": settings.allow_shell,
            "run_command_expected": run_command_expected,
            "run_command_present": run_command_present,
        },
    ))

    # Menu app is part of the supported product surface.
    app = _menu_app_path()
    app_exists = app.is_dir()
    checks.append(_check(
        "menu_app.installed",
        app_exists,
        critical=True,
        pass_summary="Mac MCP menu app is installed.",
        fail_summary="Mac MCP menu app is missing after update.",
        details={"path": str(app)},
    ))
    checks.append(_check(
        "menu_app.codesign",
        _codesign_ok(app),
        critical=True,
        pass_summary="Mac MCP menu app code signature is valid.",
        fail_summary="Mac MCP menu app code signature verification failed.",
    ))
    menu_pids = _menu_process_pids(app)
    checks.append(_check(
        "menu_app.running",
        bool(menu_pids),
        critical=True,
        pass_summary="Mac MCP menu app process is running.",
        fail_summary="Mac MCP menu app is installed but not running.",
        details={"process_count": len(menu_pids)},
    ))

    safari_plugins = list((app / "Contents" / "PlugIns").glob("*.appex")) if app_exists else []
    checks.append(_check(
        "companion.safari_bundle",
        bool(safari_plugins),
        critical=False,
        pass_summary="Safari Visual Companion is bundled.",
        fail_summary="Safari Visual Companion bundle was not found.",
        details={"bundle_count": len(safari_plugins)},
    ))
    chrome_dir = _runtime_root() / "menu_app" / "ChromeVisualCompanion"
    chrome_required = ["manifest.json", "background.js", "bridge_config.js"]
    chrome_missing = [name for name in chrome_required if not (chrome_dir / name).exists()]
    checks.append(_check(
        "companion.chrome_files",
        not chrome_missing,
        critical=False,
        pass_summary="Chrome Visual Companion files are present.",
        fail_summary="Chrome Visual Companion files are incomplete.",
        details={"missing": chrome_missing},
    ))

    public_url, public_mode, public_source = _public_basic_health_url()
    provider_ok = True
    provider_path: str | None = None
    provider_source: str | None = None
    if public_mode == "ngrok":
        resolved = resolve_ngrok_binary()
        provider_ok, provider_path, provider_source = bool(resolved.path), resolved.path, resolved.source
    elif public_mode == "cloudflare":
        resolved = resolve_cloudflared_binary()
        provider_ok, provider_path, provider_source = bool(resolved.path), resolved.path, resolved.source
    elif public_mode == "invalid":
        provider_ok = False
    checks.append(_check(
        "public.provider",
        provider_ok,
        critical=public_mode not in {"none"},
        pass_summary="Configured public endpoint provider is available.",
        fail_summary="Configured public endpoint provider is unavailable or invalid.",
        details={"mode": public_mode, "source": public_source, "binary_present": bool(provider_path), "binary_source": provider_source},
    ))

    public_ok = True
    public_status: int | None = None
    public_error: str | None = None
    if public_mode not in {"none", "invalid"}:
        if not public_url:
            public_ok = False
        else:
            public_ok, public_status, public_error = await _public_endpoint_smoke(public_url)
    elif public_mode == "invalid":
        public_ok = False
    checks.append(_check(
        "public.health",
        public_ok,
        critical=public_mode != "none",
        pass_summary="Selected public endpoint is healthy.",
        fail_summary="Selected public endpoint is not healthy.",
        details={"mode": public_mode, "http_status": public_status, "error_type": public_error},
    ))

    critical_failures = [row.check_id for row in checks if row.critical and row.status == "fail"]
    warnings = [row.check_id for row in checks if row.status == "warn"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": not critical_failures,
        "status": "passed" if not critical_failures else "failed",
        "target_commit": target,
        "release_id": verified.release_id if verified is not None else context.get("release_id"),
        "release_version": verified.version if verified is not None else expected_version or None,
        "source": context.get("source"),
        "duration_ms": max(0, int((time.time() - started) * 1000)),
        "critical_failures": critical_failures,
        "warnings": warnings,
        "checks": [row.to_dict() for row in checks],
    }
    _write_health_gate_report(report)
    return report


async def get_or_start_post_update_health_gate(
    settings: Settings,
    context: dict[str, Any],
    *,
    local_base_url: str | None = None,
) -> dict[str, Any] | None:
    """Return a completed gate report, or None while the gate runs in background."""
    global _GATE_TARGET, _GATE_TASK, _GATE_RESULT
    target = str(context.get("target_commit") or "").strip().lower()
    if target != _GATE_TARGET:
        _GATE_TARGET = target
        _GATE_TASK = None
        _GATE_RESULT = None
    if _GATE_RESULT is not None:
        return _GATE_RESULT
    if _GATE_TASK is None:
        _GATE_TASK = asyncio.create_task(
            run_post_update_health_gate(settings, context, local_base_url=local_base_url),
            name="mac-mcp-post-update-health-gate",
        )
        return None
    if not _GATE_TASK.done():
        return None
    try:
        _GATE_RESULT = _GATE_TASK.result()
    except Exception as exc:
        _GATE_RESULT = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "target_commit": target,
            "release_id": context.get("release_id"),
            "release_version": context.get("release_version"),
            "source": context.get("source"),
            "duration_ms": 0,
            "critical_failures": ["gate.internal"],
            "warnings": [],
            "checks": [HealthCheck(
                check_id="gate.internal", status="fail", critical=True,
                summary="Post-update health gate raised an internal error.",
                details={"error_type": type(exc).__name__},
            ).to_dict()],
        }
        _write_health_gate_report(_GATE_RESULT)
    return _GATE_RESULT
