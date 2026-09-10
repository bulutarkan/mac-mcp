from __future__ import annotations

import ipaddress
import logging
import os
import socket
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
    http_https_only: bool
    http_max_response_bytes: int
    http_timeout_s: int
    # Browser tool settings
    browser_allowlist: List[str]
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
        allow_no_auth=_bool("MCP_ALLOW_NO_AUTH", True),
        allow_shell=_bool("MCP_ALLOW_SHELL", True),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 1000),
        default_command_timeout_s=_int("DEFAULT_COMMAND_TIMEOUT_S", 120),
        max_command_timeout_s=_int("MAX_COMMAND_TIMEOUT_S", 600),
        max_output_chars=_int("MAX_OUTPUT_CHARS", 100000),
        workdir=workdir,
        http_allowlist=_strlist("HTTP_ALLOWLIST", ["*"]),
        http_https_only=_bool("HTTP_HTTPS_ONLY", False),
        http_max_response_bytes=_int("HTTP_MAX_RESPONSE_BYTES", 5_000_000),
        http_timeout_s=_int("HTTP_TIMEOUT_S", 60),
        # Browser
        browser_allowlist=_strlist("BROWSER_ALLOWLIST", ["*"]),
        browser_https_only=_bool("BROWSER_HTTPS_ONLY", False),
        download_dir=download_dir,
        max_js_result_chars=_int("MAX_JS_RESULT_CHARS", 20000),
        max_html_chars=_int("MAX_HTML_CHARS", 200000),
        max_wait_s=_int("MAX_WAIT_S", 120),
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


# ── HTTP URL validation ──────────────────────────────────────────────────────
_PRIVATE_NETWORKS = [
    ipaddress.ip_network(n) for n in [
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "::1/128", "fc00::/7", "fe80::/10",
    ]
]


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def validate_url(settings: Settings, url: str) -> None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL must include scheme and host.")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    if settings.http_https_only and scheme != "https":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only HTTPS URLs are allowed.")

    allowlist = settings.http_allowlist
    if "*" not in allowlist:
        host = hostname.rstrip(".")
        if not any(host == a.rstrip(".") or host.endswith("." + a.rstrip(".")) for a in allowlist):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hostname not in HTTP allowlist.")

    try:
        ipaddress.ip_address(hostname)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Direct IP URLs are not allowed.")
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot resolve hostname: {e}") from e
    for _, _, _, _, sockaddr in infos:
        if _is_private(sockaddr[0]):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Resolved IP is private; blocked.")
