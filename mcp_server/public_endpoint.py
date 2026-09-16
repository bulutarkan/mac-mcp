from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .runtime_settings import server_setting

PUBLIC_ENDPOINT_MODES = ("none", "ngrok", "cloudflare", "custom")
CLOUDFLARE_TOKEN_FILENAME = "cloudflare-tunnel-token"


class PublicEndpointError(ValueError):
    pass


@dataclass(frozen=True)
class PublicEndpointConfig:
    mode: str
    endpoint_url: str | None
    source: str
    custom_url: str | None = None
    ngrok_domain: str | None = None
    cloudflare_tunnel: str | None = None
    cloudflare_token_file: str | None = None


@dataclass(frozen=True)
class CloudflareCredentialStatus:
    path: Path
    configured: bool
    secure: bool
    reason: str


def _state_dir() -> Path:
    return Path(os.getenv("MAC_MCP_STATE_DIR", str(Path.home() / ".mac-mcp"))).expanduser()


def cloudflare_token_path(override: object = None) -> Path:
    raw = str(override or os.getenv("CLOUDFLARE_TUNNEL_TOKEN_FILE", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _state_dir() / CLOUDFLARE_TOKEN_FILENAME


def inspect_cloudflare_credential(path: Path | str | None = None) -> CloudflareCredentialStatus:
    target = Path(path).expanduser() if path is not None else cloudflare_token_path()
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return CloudflareCredentialStatus(target, False, False, "missing")
    except OSError:
        return CloudflareCredentialStatus(target, False, False, "unreadable_metadata")
    if stat.S_ISLNK(metadata.st_mode):
        return CloudflareCredentialStatus(target, True, False, "symlink_not_allowed")
    if not stat.S_ISREG(metadata.st_mode):
        return CloudflareCredentialStatus(target, True, False, "not_regular_file")
    if metadata.st_uid != os.getuid():
        return CloudflareCredentialStatus(target, True, False, "wrong_owner")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        return CloudflareCredentialStatus(target, True, False, "permissions_not_0600")
    if metadata.st_size <= 0:
        return CloudflareCredentialStatus(target, False, False, "empty")
    return CloudflareCredentialStatus(target, True, True, "ok")


def write_cloudflare_token(value: str, path: Path | str | None = None) -> Path:
    token = str(value or "").strip()
    if not token:
        raise PublicEndpointError("Cloudflare Tunnel token cannot be empty.")
    if any(ch.isspace() for ch in token):
        raise PublicEndpointError("Cloudflare Tunnel token must be a single non-whitespace value.")
    target = Path(path).expanduser() if path is not None else cloudflare_token_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def remove_cloudflare_token(path: Path | str | None = None) -> bool:
    target = Path(path).expanduser() if path is not None else cloudflare_token_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def _normalize_mode(value: object) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": None,
        "off": "none",
        "disabled": "none",
        "local": "none",
        "local_only": "none",
        "none": "none",
        "ngrok": "ngrok",
        "cloudflare": "cloudflare",
        "cloudflare_tunnel": "cloudflare",
        "cf_tunnel": "cloudflare",
        "custom": "custom",
        "custom_url": "custom",
        "external": "custom",
    }
    if raw not in aliases:
        raise PublicEndpointError(
            f"Unsupported public endpoint mode {value!r}; use one of: none, ngrok, cloudflare, custom."
        )
    return aliases[raw]


def normalize_custom_public_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise PublicEndpointError("This public endpoint mode requires a public HTTPS URL.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise PublicEndpointError("Public URL is invalid.") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PublicEndpointError("Public URL must use HTTPS and include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise PublicEndpointError("Public URL must not contain URL userinfo.")
    if parsed.query or parsed.fragment:
        raise PublicEndpointError("Public URL must not contain query parameters or fragments.")
    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/mcp"
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def normalize_ngrok_domain(value: object) -> str:
    raw = str(value or "").strip().lower().rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://") or "/" in raw:
        raise PublicEndpointError(
            "NGROK_DOMAIN should contain only the domain, for example: your-domain.ngrok-free.dev"
        )
    if not raw or "." not in raw:
        raise PublicEndpointError("NGROK_DOMAIN is not set or is invalid.")
    return raw


def normalize_cloudflare_tunnel(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise PublicEndpointError("Cloudflare Tunnel mode requires a tunnel name or UUID.")
    if any(ch in raw for ch in "\r\n\x00"):
        raise PublicEndpointError("Cloudflare tunnel name is invalid.")
    return raw


def _settings_mode() -> tuple[str | None, str]:
    configured = server_setting("public_endpoint_mode", None)
    if configured is not None and str(configured).strip():
        return _normalize_mode(configured), "settings"
    if bool(server_setting("ngrok_on_start", False)):
        return "ngrok", "settings_legacy"
    return None, "default"


def _configured_public_url(override: object = None) -> str:
    if override is not None and str(override).strip():
        return str(override).strip()
    env_value = os.getenv("MAC_MCP_PUBLIC_URL", "").strip()
    if env_value:
        return env_value
    return str(server_setting("public_url", "") or "").strip()


def resolve_public_endpoint(
    *,
    mode_override: object = None,
    public_url_override: object = None,
    force_ngrok: bool = False,
    cloudflare_tunnel_override: object = None,
    cloudflare_token_file_override: object = None,
) -> PublicEndpointConfig:
    explicit_mode = _normalize_mode(mode_override) if mode_override is not None else None
    if force_ngrok and explicit_mode not in {None, "ngrok"}:
        raise PublicEndpointError("--ngrok cannot be combined with a non-ngrok --public-mode.")

    source = "default"
    if force_ngrok:
        mode = "ngrok"
        source = "legacy_cli"
    elif explicit_mode is not None:
        mode = explicit_mode
        source = "cli"
    elif public_url_override is not None and str(public_url_override).strip():
        mode = "custom"
        source = "cli_url"
    else:
        env_mode_raw = os.getenv("MAC_MCP_PUBLIC_ENDPOINT_MODE", "").strip()
        if env_mode_raw:
            mode = _normalize_mode(env_mode_raw) or "none"
            source = "env"
        else:
            settings_mode, settings_source = _settings_mode()
            mode = settings_mode or "none"
            source = settings_source

    if mode == "custom":
        endpoint = normalize_custom_public_url(_configured_public_url(public_url_override))
        return PublicEndpointConfig(
            mode="custom", endpoint_url=endpoint, custom_url=endpoint, source=source
        )

    if mode == "cloudflare":
        endpoint = normalize_custom_public_url(_configured_public_url(public_url_override))
        explicit_token_file = str(
            cloudflare_token_file_override or os.getenv("CLOUDFLARE_TUNNEL_TOKEN_FILE", "") or ""
        ).strip()
        default_token = cloudflare_token_path(explicit_token_file or None)
        token_status = inspect_cloudflare_credential(default_token)
        token_file = str(default_token) if explicit_token_file or token_status.configured else None
        tunnel_raw = (
            cloudflare_tunnel_override
            or os.getenv("CLOUDFLARE_TUNNEL", "")
            or server_setting("cloudflare_tunnel", "")
        )
        tunnel = None
        if tunnel_raw and str(tunnel_raw).strip():
            tunnel = normalize_cloudflare_tunnel(tunnel_raw)
        if not tunnel and not token_file:
            raise PublicEndpointError(
                f"Cloudflare Tunnel credential is not configured. Save the tunnel token in {default_token} or configure a named tunnel."
            )
        return PublicEndpointConfig(
            mode="cloudflare",
            endpoint_url=endpoint,
            source=source,
            custom_url=endpoint,
            cloudflare_tunnel=tunnel,
            cloudflare_token_file=token_file,
        )

    if mode == "ngrok":
        domain = normalize_ngrok_domain(os.getenv("NGROK_DOMAIN", ""))
        return PublicEndpointConfig(
            mode="ngrok",
            endpoint_url=f"https://{domain}/mcp",
            ngrok_domain=domain,
            source=source,
        )

    return PublicEndpointConfig(mode="none", endpoint_url=None, source=source)


def public_health_url(config: PublicEndpointConfig) -> str | None:
    if not config.endpoint_url:
        return None
    parsed = urlsplit(config.endpoint_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
