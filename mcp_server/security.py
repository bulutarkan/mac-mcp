from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import socket
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import HTTPException, Request, status

BASE_DIR = Path(__file__).resolve().parent
HOME_DIR = Path(os.getenv("MAC_MCP_HOME", str(Path.home()))).expanduser().resolve()


def dashboard_token_path() -> Path:
    configured = os.getenv("MAC_MCP_DASHBOARD_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    state_dir = Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()
    return state_dir / "dashboard-token"


def ensure_dashboard_token(path: Optional[Path] = None) -> str:
    """Return the per-user dashboard credential, creating it securely if needed.

    This token is intentionally separate from MCP_API_KEY so opening the local
    dashboard never exposes the connector credential to the browser/menu app.
    The file boundary is per Unix user, not a sandbox between processes running
    under the same uid.
    """
    token_file = (path or dashboard_token_path()).expanduser()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(token_file.parent, 0o700)
    except OSError:
        pass

    if token_file.is_symlink():
        raise RuntimeError("dashboard token path must not be a symlink")

    if token_file.exists():
        stat = token_file.stat()
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            raise RuntimeError("dashboard token file is not owned by the current user")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            os.chmod(token_file, 0o600)
            return token

    token = secrets.token_urlsafe(48)
    fd, tmp_name = tempfile.mkstemp(prefix=".dashboard-token-", dir=str(token_file.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, token_file)
        os.chmod(token_file, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return token


def dashboard_authorized(expected_token: str, authorization: Optional[str]) -> bool:
    if not expected_token or not authorization or not authorization.lower().startswith("bearer "):
        return False
    supplied = authorization[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected_token)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _strlist(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return parts or default


@dataclass(frozen=True)
class Settings:
    api_key: str
    allow_no_auth: bool
    allow_shell: bool
    rate_limit_per_minute: int
    default_command_timeout_s: int
    max_command_timeout_s: int
    max_output_chars: int
    workdir: Path
    http_allowlist: List[str]
    http_private_allowlist: List[str]
    http_https_only: bool
    http_max_response_bytes: int
    http_timeout_s: int
    # Browser tool settings
    browser_allowlist: List[str]
    browser_private_allowlist: List[str]
    browser_https_only: bool
    download_dir: Path
    max_js_result_chars: int
    max_html_chars: int
    max_wait_s: int


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    workdir_env = os.getenv("WORKDIR", "").strip()
    workdir = Path(workdir_env).expanduser().resolve() if workdir_env else HOME_DIR
    workdir.mkdir(parents=True, exist_ok=True)

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "~/Downloads")).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        api_key=os.getenv("MCP_API_KEY", "").strip(),
        allow_no_auth=_bool("MCP_ALLOW_NO_AUTH", False),
        allow_shell=_bool("MCP_ALLOW_SHELL", False),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 1000),
        default_command_timeout_s=_int("DEFAULT_COMMAND_TIMEOUT_S", 120),
        max_command_timeout_s=_int("MAX_COMMAND_TIMEOUT_S", 600),
        max_output_chars=_int("MAX_OUTPUT_CHARS", 100000),
        workdir=workdir,
        http_allowlist=_strlist("HTTP_ALLOWLIST", []),
        http_private_allowlist=_strlist("HTTP_PRIVATE_ALLOWLIST", []),
        http_https_only=_bool("HTTP_HTTPS_ONLY", False),
        http_max_response_bytes=_int("HTTP_MAX_RESPONSE_BYTES", 5_000_000),
        http_timeout_s=_int("HTTP_TIMEOUT_S", 60),
        # Browser
        browser_allowlist=_strlist("BROWSER_ALLOWLIST", []),
        browser_private_allowlist=_strlist("BROWSER_PRIVATE_ALLOWLIST", []),
        browser_https_only=_bool("BROWSER_HTTPS_ONLY", False),
        download_dir=download_dir,
        max_js_result_chars=_int("MAX_JS_RESULT_CHARS", 20000),
        max_html_chars=_int("MAX_HTML_CHARS", 200000),
        max_wait_s=_int("MAX_WAIT_S", 120),
    )


def _loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    return value in {"127.0.0.1", "::1", "localhost"}


def _effective_bind_host(host: Optional[str] = None) -> str:
    explicit = str(host or "").strip()
    if explicit:
        return explicit
    env_host = os.getenv("MAC_MCP_HOST", "").strip()
    if env_host:
        return env_host
    argv = list(sys.argv[1:])
    for index, item in enumerate(argv):
        if item == "--host" and index + 1 < len(argv):
            candidate = str(argv[index + 1]).strip()
            if candidate:
                return candidate
        if item.startswith("--host="):
            candidate = item.split("=", 1)[1].strip()
            if candidate:
                return candidate
    return "127.0.0.1"


def _normalize_public_endpoint_mode(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return "none" if raw in {"", "none", "off", "disabled", "local", "local_only"} else raw


def _public_endpoint_mode() -> str:
    raw = os.getenv("MAC_MCP_PUBLIC_ENDPOINT_MODE", "").strip()
    if raw:
        return _normalize_public_endpoint_mode(raw)
    try:
        from .runtime_settings import server_setting
        configured = str(server_setting("public_endpoint_mode", "") or "").strip()
        if configured:
            return _normalize_public_endpoint_mode(configured)
        if bool(server_setting("ngrok_on_start", False)):
            return "ngrok"
    except Exception:
        # Settings parse/read failures must never weaken auth. Treat an unknown
        # public mode as local-only here; the endpoint layer performs its own
        # validation before starting a connector.
        pass
    return "none"


def validate_bootstrap_security(
    settings: Settings, *, host: Optional[str] = None, public_endpoint_mode: Optional[str] = None,
) -> None:
    """Reject unsafe bootstrap states before the MCP app starts serving requests.

    Missing configuration is intentionally fail-closed. Deliberate no-auth mode
    remains available for loopback-only development, but it cannot be combined
    with a non-loopback bind or a managed public endpoint.
    """
    if not settings.allow_no_auth and not settings.api_key:
        raise RuntimeError(
            "secure_bootstrap_missing_api_key: MCP_API_KEY is required when "
            "MCP_ALLOW_NO_AUTH=false. Run the installer or configure a strong API key."
        )

    if not settings.allow_no_auth:
        return

    effective_host = _effective_bind_host(host)
    if not _loopback_host(effective_host):
        raise RuntimeError(
            "secure_bootstrap_no_auth_non_loopback: MCP_ALLOW_NO_AUTH=true is only "
            "permitted on a loopback bind. Enable authentication before using a non-loopback host."
        )

    public_mode = (
        _normalize_public_endpoint_mode(public_endpoint_mode)
        if public_endpoint_mode is not None
        else _public_endpoint_mode()
    )
    if public_mode != "none":
        raise RuntimeError(
            "secure_bootstrap_no_auth_public_endpoint: MCP_ALLOW_NO_AUTH=true cannot be "
            f"combined with public endpoint mode {public_mode!r}. Enable MCP authentication first."
        )

    logging.getLogger("mac_mcp.security").warning(
        "secure_bootstrap_no_auth_loopback: running explicitly without MCP authentication; "
        "keep the server loopback-only and do not start a public tunnel."
    )


def require_shell_enabled(settings: Settings) -> None:
    """Fail closed before any command execution when shell tools are disabled."""
    if not settings.allow_shell:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Shell execution is disabled by MCP_ALLOW_SHELL=false.",
        )


# ── Path resolution (full filesystem access) ───────────────────────────────
def resolve_path(user_path: str) -> Path:
    """Resolve any path. Absolute paths are used as-is if they exist.
    If an absolute path doesn't exist, try it relative to home dir first.
    Relative paths are always resolved relative to home dir."""
    p = Path(user_path).expanduser()
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        # Try stripping leading slash and resolving relative to home
        # e.g. "/mac-mcp" -> "~/mac-mcp"
        relative = Path(str(p).lstrip("/"))
        candidate = (HOME_DIR / relative).resolve()
        if candidate.exists():
            return candidate
        # Fall back to the original absolute path (caller will handle missing)
        return p.resolve()
    return (HOME_DIR / p).resolve()


# ── Rate limiter ────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, limit_per_minute)
        self._hits: Dict[str, Deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        q = self._hits.setdefault(key, deque())
        cutoff = now - 60.0
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded.")
        q.append(now)


# ── Auth ────────────────────────────────────────────────────────────────────
def request_authorization(
    settings: Settings,
    authorization: Optional[str],
    query_api_keys: List[str],
) -> Optional[str]:
    """Resolve the request credential without weakening existing auth behavior.

    Bearer headers remain authoritative. When authentication is enabled, clients
    that cannot set headers may pass exactly one non-empty ``?ApiKey=...`` value.
    Query credentials are intentionally limited to the configured global API key;
    scoped agent bearer tokens remain header-only.
    """
    if settings.allow_no_auth or authorization is not None:
        return authorization
    if not query_api_keys:
        return None
    if len(query_api_keys) != 1:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key.")
    token = query_api_keys[0].strip()
    if not token or token != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key.")
    return f"Bearer {token}"


def authenticate(settings: Settings, authorization: Optional[str]) -> str:
    if settings.allow_no_auth:
        return "no-auth"
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Authorization header.")
    lower = authorization.lower()
    if not lower.startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authorization must be a Bearer token.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token is empty.")
    if token != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key.")
    return token


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def rate_limit(limiter: RateLimiter, token: str, ip: str) -> None:
    limiter.check(f"{token}:{ip}")


# ── Audit logger ────────────────────────────────────────────────────────────
def setup_audit_logger() -> logging.Logger:
    logger = logging.getLogger("mcp_audit")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(BASE_DIR / "audit.log")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


# ── Helpers ─────────────────────────────────────────────────────────────────
def truncate(text: str, limit: int) -> Tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    suffix = "\n... [truncated]"
    return text[: max(0, limit - len(suffix))] + suffix, True


# ── URL destination validation ───────────────────────────────────────────────

def _hostname_allowed(hostname: str, allowlist: List[str]) -> bool:
    host = hostname.lower().rstrip(".")
    if "*" in allowlist:
        return True
    return any(host == item.rstrip(".") or host.endswith("." + item.rstrip(".")) for item in allowlist)


def _private_host_allowed(hostname: str, allowlist: List[str]) -> bool:
    # Private-network exceptions must be explicit. A wildcard is intentionally ignored.
    host = hostname.lower().rstrip(".")
    return any(
        item != "*" and (host == item.rstrip(".") or host.endswith("." + item.rstrip(".")))
        for item in allowlist
    )


def _address_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    # is_global excludes loopback, private, link-local, carrier-grade NAT, multicast,
    # unspecified, reserved/documentation ranges, and metadata-style link-local IPs.
    return not addr.is_global


def resolve_host_addresses(hostname: str) -> Tuple[str, ...]:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL hostname is required.")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        return (str(literal),)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot resolve hostname: {e}") from e
    addresses: list[str] = []
    for _, _, _, _, sockaddr in infos:
        value = str(sockaddr[0]).split("%", 1)[0]
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hostname resolved to no usable addresses.")
    return tuple(addresses)


def validate_destination_url(
    url: str, *, allowlist: List[str], private_allowlist: List[str], https_only: bool,
) -> Tuple[str, Tuple[str, ...]]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL must include scheme and host.")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only http/https URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL userinfo is not allowed.")
    if https_only and scheme != "https":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only HTTPS URLs are allowed.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL hostname is required.")
    if not _hostname_allowed(hostname, allowlist):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hostname not in allowlist.")

    private_exception = _private_host_allowed(hostname, private_allowlist)
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not private_exception:
        # Preserve the historical no-direct-IP default; private IPs may only be
        # enabled by an exact/suffix private allowlist entry.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Direct IP URLs are not allowed.")

    addresses = resolve_host_addresses(hostname)
    blocked = [ip for ip in addresses if _address_is_blocked(ip)]
    if blocked and not private_exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Resolved IP is local/private/non-global; blocked.")
    return hostname, addresses


def validate_url(settings: Settings, url: str) -> None:
    validate_destination_url(
        url, allowlist=settings.http_allowlist, private_allowlist=settings.http_private_allowlist,
        https_only=settings.http_https_only,
    )


def validate_browser_url(settings: Settings, url: str) -> None:
    validate_destination_url(
        url, allowlist=settings.browser_allowlist, private_allowlist=settings.browser_private_allowlist,
        https_only=settings.browser_https_only,
    )
