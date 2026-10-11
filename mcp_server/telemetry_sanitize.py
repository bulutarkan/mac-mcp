"""Secret-aware, bounded sanitizing for telemetry values (shared by observability and change summaries).

It depends only on data_guard (itself a leaf), so lower layers can use it without import cycles.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from .data_guard import redact_sensitive_text


DEFAULT_PREVIEW_CHARS = 4_000


_SECRET_KEYS = {
    "authorization", "proxy_authorization", "api_key", "apikey", "access_key",
    "client_secret", "secret", "password", "passwd", "passphrase", "token",
    "access_token", "refresh_token", "id_token", "session_token", "session_id",
    "cookie", "set_cookie", "private_key", "credentials", "credential",
    "openai_session", "openai_subject", "openai_organization", "openai_org", "openai_location",
}


_SECRET_KEY_SUFFIXES = ("_password", "_secret", "_token", "_api_key", "_apikey")


_PROVIDER_IDENTITY_KEYS = {
    "openai_session", "openai_subject", "openai_organization", "openai_org", "openai_location",
}


_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")


_QUERY_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)\s*[=:]\s*)([^\s&;,]+)"
)


_PROVIDER_IDENTITY_TEXT_RE = re.compile(
    r"(?i)[\"']?openai[/_-](?:session|subject|organization|org|location)[\"']?\s*[=:]\s*[\"']?[^\"'\s,;}]+[\"']?"
)


_ENV_SECRET_RE = re.compile(
    r"(?im)^(\s*(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*=\s*)(.+)$"
)


_SK_RE = re.compile(r"\b(?:sk|pk|rk|ghp|github_pat)-?[A-Za-z0-9_-]{16,}\b")


_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def _normalized_key(key: Optional[str]) -> str:
    if not key:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def _is_provider_identity_key(key: Optional[str]) -> bool:
    return _normalized_key(key) in _PROVIDER_IDENTITY_KEYS


def _is_secret_key(key: Optional[str]) -> bool:
    normalized = _normalized_key(key)
    return bool(normalized) and (
        normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)
    )


def _redact_text(text: str) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _PROVIDER_IDENTITY_TEXT_RE.sub("[provider identity redacted]", text)
    text = _ENV_SECRET_RE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _SK_RE.sub("[REDACTED]", text)
    return redact_sensitive_text(text)


def _looks_encoded_blob(text: str) -> bool:
    if text.startswith("data:image/") or text.startswith("data:application/octet-stream"):
        return True
    compact = text.replace("\n", "").replace("\r", "")
    return len(compact) >= 768 and bool(_BASE64_RE.fullmatch(compact))


def sanitize_value(value: Any, *, key: Optional[str] = None, preview_chars: int = DEFAULT_PREVIEW_CHARS,
                   depth: int = 0) -> Any:
    """Return a JSON-safe, bounded, secret-aware representation for dashboard telemetry."""
    if _is_secret_key(key):
        return "[REDACTED]"
    if depth > 12:
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[binary · {len(value):,} bytes]"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        safe = _redact_text(value)
        if _looks_encoded_blob(safe):
            label = "image data" if safe.startswith("data:image/") else "encoded/binary value"
            return f"[{label} · {len(value):,} chars]"
        if len(safe) > preview_chars:
            omitted = len(safe) - preview_chars
            return safe[:preview_chars] + f"\n… [{omitted:,} chars omitted]"
        return safe
    if hasattr(value, "model_dump"):
        try:
            return sanitize_value(value.model_dump(), key=key, preview_chars=preview_chars, depth=depth + 1)
        except Exception:
            pass
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for child_key, child_value in value.items():
            text_key = str(child_key)
            if _is_provider_identity_key(text_key):
                continue
            out[text_key] = sanitize_value(
                child_value, key=text_key, preview_chars=preview_chars, depth=depth + 1
            )
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        cap = 100
        result = [
            sanitize_value(item, preview_chars=preview_chars, depth=depth + 1)
            for item in items[:cap]
        ]
        if len(items) > cap:
            result.append(f"[{len(items) - cap:,} more items omitted]")
        return result
    try:
        return sanitize_value(vars(value), key=key, preview_chars=preview_chars, depth=depth + 1)
    except Exception:
        return _redact_text(str(value))[:preview_chars]
