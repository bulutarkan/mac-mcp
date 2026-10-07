"""Optional, confidence-gated ambiguity resolver backed by OpenAI Decisions.

Deterministic resolvers stay authoritative. This module is only consulted when a
deterministic resolver already produced a bounded, ambiguous candidate set; it
may pick one of those candidate IDs and nothing else. It never decides policy,
approval, ownership, or human-takeover questions, and every failure degrades to
the caller's existing deterministic choice.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence

import httpx

from .data_guard import redact_sensitive_text
from .runtime_settings import keychain_password, load_runtime_settings_state

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DEFAULT_MODEL = "gpt-6-luna"
_KEY_CACHE_TTL_S = 60.0
_VERIFY_TIMEOUT_S = 10.0
_NONE_CHOICE = "none"
_FIELD_LIMIT = 80

AMBIGUITY_FLOOR = 0.60
AMBIGUITY_MARGIN = 0.10

_SCOPES = {"off", "browser", "native", "both"}

_MODELS_URL = "https://api.openai.com/v1/models"
_WARM_MIN_INTERVAL_S = 15.0


def _new_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(_VERIFY_TIMEOUT_S),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=120.0),
    )


# One pooled client: a fresh TLS handshake per call measured ~2.8 s worst case
# against a 400 ms budget, while a warm pooled connection stayed under ~330 ms.
_client_lock = threading.Lock()
_shared_client: Optional[httpx.Client] = None


def _client() -> httpx.Client:
    global _shared_client
    with _client_lock:
        if _shared_client is None or _shared_client.is_closed:
            _shared_client = _new_client()
        return _shared_client


@dataclass(frozen=True)
class DecisionConfig:
    enabled: bool = False
    scope: str = "off"
    timeout_ms: int = 600
    accept_threshold: float = 0.80
    agree_threshold: float = 0.65
    max_candidates: int = 8
    model: str = DEFAULT_MODEL

    def allows(self, surface: str) -> bool:
        if not self.enabled or self.scope == "off":
            return False
        return self.scope == "both" or self.scope == surface


@dataclass(frozen=True)
class DecisionCandidate:
    candidate_id: str
    label: str = ""
    role: str = ""
    tag: str = ""
    context: str = ""
    risky: bool = False


@dataclass
class DecisionResult:
    outcome: str
    attempted: bool = False
    selected_id: Optional[str] = None
    choice: Optional[str] = None
    confidence: Optional[float] = None
    latency_ms: int = 0
    candidate_count: int = 0

    @property
    def accepted(self) -> bool:
        return self.selected_id is not None

    def metadata(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "attempted": self.attempted,
            "accepted": self.accepted,
            "confidence": round(self.confidence, 3) if self.confidence is not None else None,
            "latency_ms": self.latency_ms,
            "candidate_count": self.candidate_count,
        }


def _bounded_float(value: Any, default: float, low: float, high: float) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if low <= number <= high else None


def _bounded_int(value: Any, default: int, low: int, high: int) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def load_decision_config() -> DecisionConfig:
    """Strict, fail-closed reader for the ``decision_acceleration`` section."""
    disabled = DecisionConfig()
    state = load_runtime_settings_state()
    if not state.ok:
        return disabled
    section = state.data.get("decision_acceleration")
    if not isinstance(section, dict):
        return disabled
    enabled = section.get("enabled", False)
    scope = section.get("scope", "both")
    model = section.get("model", DEFAULT_MODEL)
    if not isinstance(enabled, bool) or not isinstance(scope, str) or not isinstance(model, str):
        return disabled
    scope = scope.strip().lower()
    if scope not in _SCOPES or not model.strip():
        return disabled
    timeout_ms = _bounded_int(section.get("timeout_ms"), 600, 50, 3000)
    accept = _bounded_float(section.get("accept_threshold"), 0.80, 0.5, 1.0)
    agree = _bounded_float(section.get("agree_threshold"), 0.65, 0.0, 1.0)
    max_candidates = _bounded_int(section.get("max_candidates"), 8, 2, 12)
    if timeout_ms is None or accept is None or agree is None or max_candidates is None or agree > accept:
        return disabled
    return DecisionConfig(
        enabled=enabled,
        scope=scope,
        timeout_ms=timeout_ms,
        accept_threshold=accept,
        agree_threshold=agree,
        max_candidates=max_candidates,
        model=model.strip(),
    )


def _keychain_service() -> str:
    return os.getenv("MAC_MCP_DECISIONS_KEYCHAIN_SERVICE", "com.bulutarkan.mac-mcp")


def _keychain_account() -> str:
    return os.getenv("MAC_MCP_DECISIONS_KEYCHAIN_ACCOUNT", "openai-decisions-api-key")


def _read_api_key() -> Optional[str]:
    """Settings (Keychain) key first; the env override exists for isolated tests."""
    value = keychain_password(service=_keychain_service(), account=_keychain_account())
    if value:
        return value
    return os.getenv("MAC_MCP_DECISIONS_OPENAI_API_KEY", "").strip() or None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_key_lock = threading.Lock()
_key_state: Dict[str, Any] = {
    "value": None,
    "loaded_at": None,
    "refreshing": False,
    "invalid_fingerprint": None,
    "verified_fingerprint": None,
}


def _store_key(value: Optional[str]) -> None:
    with _key_lock:
        _key_state["value"] = value
        _key_state["loaded_at"] = time.monotonic()
        _key_state["refreshing"] = False


def _refresh_key_in_background() -> None:
    try:
        value = _read_api_key()
    except Exception:  # pragma: no cover - defensive: never break callers
        value = None
    _store_key(value)
    if value:
        _warm_connection(value)


_warm_state: Dict[str, float] = {"last": 0.0}


def _warm_connection(api_key: str) -> None:
    """Open/refresh the pooled TLS connection off the action path; result is ignored."""
    now = time.monotonic()
    with _key_lock:
        if now - _warm_state["last"] < _WARM_MIN_INTERVAL_S:
            return
        _warm_state["last"] = now
    try:
        _client().get(_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=_VERIFY_TIMEOUT_S)
    except (httpx.HTTPError, ValueError):
        pass


def _schedule_warmup(api_key: str) -> None:
    threading.Thread(target=_warm_connection, args=(api_key,), name="decision-warmup", daemon=True).start()


def _cached_api_key() -> tuple[Optional[str], bool]:
    """Return ``(key, loaded)`` without ever blocking on Keychain.

    A missing or stale cache schedules a background refresh, so an ambiguous
    browser/native action never waits on a Keychain subprocess or prompt.
    """
    now = time.monotonic()
    with _key_lock:
        loaded_at = _key_state["loaded_at"]
        fresh = loaded_at is not None and now - float(loaded_at) < _KEY_CACHE_TTL_S
        if not fresh and not _key_state["refreshing"]:
            _key_state["refreshing"] = True
            threading.Thread(target=_refresh_key_in_background, name="decision-key-refresh", daemon=True).start()
        return _key_state["value"], loaded_at is not None


def prefetch_api_key() -> None:
    _cached_api_key()


def reload_api_key() -> None:
    """Synchronously re-read the key after the user changed it in Settings."""
    _store_key(_read_api_key())


def _key_known_invalid(value: str) -> bool:
    with _key_lock:
        return _key_state["invalid_fingerprint"] == _fingerprint(value)


def _mark_key(value: str, *, valid: bool) -> None:
    fingerprint = _fingerprint(value)
    with _key_lock:
        if valid:
            _key_state["verified_fingerprint"] = fingerprint
            if _key_state["invalid_fingerprint"] == fingerprint:
                _key_state["invalid_fingerprint"] = None
        else:
            _key_state["invalid_fingerprint"] = fingerprint
            if _key_state["verified_fingerprint"] == fingerprint:
                _key_state["verified_fingerprint"] = None


def _key_status(value: Optional[str], loaded: bool) -> str:
    if not loaded:
        return "unknown"
    if not value:
        return "missing"
    fingerprint = _fingerprint(value)
    with _key_lock:
        if _key_state["invalid_fingerprint"] == fingerprint:
            return "invalid"
        if _key_state["verified_fingerprint"] == fingerprint:
            return "valid"
    return "unverified"


def assess_ambiguity(
    scores: Sequence[float],
    *,
    floor: float = AMBIGUITY_FLOOR,
    margin: float = AMBIGUITY_MARGIN,
) -> Dict[str, Any]:
    """Classify a descending score list the same way computer_plan recovery does."""
    values = [float(score or 0.0) for score in scores]
    best = values[0] if values else 0.0
    second = values[1] if len(values) > 1 else None
    gap = (best - second) if second is not None else None
    ambiguous = bool(
        second is not None and best >= floor and second >= floor and gap is not None and gap < margin
    )
    return {
        "ambiguous": ambiguous,
        "candidate_count": len(values),
        "best": round(best, 3),
        "second": round(second, 3) if second is not None else None,
        "margin": round(gap, 3) if gap is not None else None,
    }


_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {"resolutions": 0, "ambiguous": 0, "attempted": 0, "accepted": 0, "outcomes": {}}


def record_resolution(surface: str, *, ambiguous: bool, result: Optional[DecisionResult] = None) -> None:
    with _stats_lock:
        _stats["resolutions"] += 1
        if ambiguous:
            _stats["ambiguous"] += 1
        if result is not None:
            if result.attempted:
                _stats["attempted"] += 1
            if result.accepted:
                _stats["accepted"] += 1
            key = f"{surface}:{result.outcome}"
            _stats["outcomes"][key] = int(_stats["outcomes"].get(key, 0)) + 1


def decision_stats() -> Dict[str, Any]:
    with _stats_lock:
        snapshot = dict(_stats)
        snapshot["outcomes"] = dict(_stats["outcomes"])
    total = snapshot["resolutions"]
    snapshot["ambiguity_rate"] = round(snapshot["ambiguous"] / total, 4) if total else 0.0
    return snapshot


def _clip(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return redact_sensitive_text(text[:_FIELD_LIMIT])


def is_risky_label(*values: Any) -> bool:
    from .tools_ui import _RISKY_WORDS

    searchable = " ".join(str(value or "") for value in values).lower()
    return any(word in searchable for word in _RISKY_WORDS)


def _describe(candidate: DecisionCandidate) -> str:
    parts = [f"label: {_clip(candidate.label) or '(none)'}"]
    if candidate.role:
        parts.append(f"role: {_clip(candidate.role)}")
    if candidate.tag:
        parts.append(f"tag: {_clip(candidate.tag)}")
    if candidate.context:
        parts.append(f"context: {_clip(candidate.context)}")
    return "; ".join(parts)


def build_request(intent: str, candidates: Sequence[DecisionCandidate], model: str) -> Dict[str, Any]:
    choices = [{"value": c.candidate_id, "description": _describe(c)} for c in candidates]
    choices.append({"value": _NONE_CHOICE, "description": "None of the candidates clearly matches the request."})
    return {
        "model": model,
        "input": redact_sensitive_text(" ".join(str(intent or "").split())[:400]),
        "questions": [{
            "type": "choice",
            "name": "target",
            "instructions": (
                "Which UI candidate does the request refer to? Labels can repeat; use each candidate's "
                "context to tell them apart. Choose none only if two or more candidates fit equally well "
                "or none fits."
            ),
            "choices": choices,
        }],
    }


def _parse_answer(payload: Any) -> tuple[Optional[str], Optional[float]]:
    if not isinstance(payload, Mapping):
        return None, None
    answers = payload.get("answers")
    if not isinstance(answers, list):
        return None, None
    answer = next(
        (a for a in answers if isinstance(a, Mapping) and a.get("name") == "target"),
        None,
    )
    if answer is None or answer.get("type") != "choice":
        return None, None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(choice, str) or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None, None
    return choice, float(confidence)


def resolve_ambiguity(
    intent: str,
    candidates: Sequence[DecisionCandidate],
    *,
    surface: str,
    deterministic_id: str,
    config: Optional[DecisionConfig] = None,
) -> DecisionResult:
    """Pick one of ``candidates`` or return a non-accepted result.

    A non-accepted result always means "keep the deterministic behavior".
    """
    config = config or load_decision_config()
    bounded = list(candidates)[: config.max_candidates]
    result = DecisionResult(outcome="disabled", candidate_count=len(bounded))
    if not config.allows(surface):
        return result
    if len(bounded) < 2 or deterministic_id not in {c.candidate_id for c in bounded}:
        result.outcome = "unsupported_input"
        return result
    api_key, loaded = _cached_api_key()
    if not api_key:
        result.outcome = "no_key" if loaded else "key_pending"
        return result
    if _key_known_invalid(api_key):
        result.outcome = "invalid_key"
        return result

    by_id = {c.candidate_id: c for c in bounded}
    body = build_request(intent, bounded, config.model)
    result.attempted = True
    started = time.perf_counter()
    try:
        response = _client().post(
            DECISIONS_URL,
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=config.timeout_ms / 1000.0,
        )
        if response.status_code in {401, 403}:
            _mark_key(api_key, valid=False)
            result.outcome = "invalid_key"
        elif response.status_code == 429:
            result.outcome = "rate_limited"
        elif response.status_code >= 400:
            result.outcome = "http_error"
        else:
            choice, confidence = _parse_answer(response.json())
            result.choice, result.confidence = choice, confidence
            if choice is None:
                result.outcome = "malformed"
            elif choice == _NONE_CHOICE:
                result.outcome = "no_match"
            elif choice not in by_id:
                result.outcome = "malformed"
            else:
                result.outcome = "pending"
    except httpx.TimeoutException:
        result.outcome = "timeout"
        _schedule_warmup(api_key)
    except (httpx.HTTPError, ValueError):
        result.outcome = "http_error"
    finally:
        result.latency_ms = int((time.perf_counter() - started) * 1000)

    if result.outcome != "pending":
        return result
    choice = str(result.choice)
    confidence = float(result.confidence or 0.0)
    if choice != deterministic_id and by_id[choice].risky:
        result.outcome = "skipped_risky"
    elif confidence >= config.accept_threshold:
        result.outcome = "accepted"
        result.selected_id = choice
    elif confidence >= config.agree_threshold and choice == deterministic_id:
        result.outcome = "accepted_agreement"
        result.selected_id = choice
    else:
        result.outcome = "low_confidence"
    return result


def verify_api_key() -> Dict[str, Any]:
    """User-initiated key check from Settings; may block on Keychain and network."""
    config = load_decision_config()
    value = _read_api_key()
    _store_key(value)
    payload: Dict[str, Any] = {"ok": False, "enabled": config.enabled, "scope": config.scope}
    if not value:
        payload["status"] = "missing"
        return payload
    body = build_request(
        "Verification request. Pick the button labelled OK.",
        [DecisionCandidate("c1", label="OK", role="button"), DecisionCandidate("c2", label="Cancel", role="button")],
        config.model,
    )
    started = time.perf_counter()
    try:
        response = _client().post(
            DECISIONS_URL,
            json=body,
            headers={"Authorization": f"Bearer {value}", "Content-Type": "application/json"},
            timeout=_VERIFY_TIMEOUT_S,
        )
        payload["http_status"] = response.status_code
        if response.status_code in {401, 403}:
            _mark_key(value, valid=False)
            payload["status"] = "invalid"
        elif response.status_code == 429:
            payload["status"] = "rate_limited"
        elif response.status_code >= 400:
            payload["status"] = "provider_error"
        elif _parse_answer(response.json())[0] is None:
            payload["status"] = "unexpected_response"
        else:
            _mark_key(value, valid=True)
            payload["status"] = "valid"
            payload["ok"] = True
    except httpx.TimeoutException:
        payload["status"] = "timeout"
    except (httpx.HTTPError, ValueError):
        payload["status"] = "unreachable"
    payload["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return payload


def decision_status() -> Dict[str, Any]:
    config = load_decision_config()
    if config.enabled:
        prefetch_api_key()
    with _key_lock:
        value, loaded = _key_state["value"], _key_state["loaded_at"] is not None
    return {
        "enabled": config.enabled,
        "scope": config.scope,
        "active": config.enabled and config.scope != "off" and _key_status(value, loaded) in {"valid", "unverified"},
        "key_status": _key_status(value, loaded),
        "timeout_ms": config.timeout_ms,
        "stats": decision_stats(),
    }
