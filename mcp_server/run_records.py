"""What a Scheduled Tasks run record keeps, and for how long (#199).

A run has two layers:

- The skeleton: how it ended, when, its usage and checks, and per step only its kind, status
  and a fingerprint. It lives as long as the run (50 runs per task, 90 days).
- The contents: the result, the output, and each step's text, inputs and output. They are
  masked for known secrets when written and removed after Settings > Privacy > run details
  (7, 30 or 90 days).

Fingerprints are keyed: HMAC-SHA256 with a key that never leaves this Mac, over a canonical
JSON form (version 1). Two runs with the same steps get the same fingerprints, so "did it do
the same thing as yesterday?" stays answerable after the contents are gone, while a guessable
argument (a common path, a short command) cannot be confirmed from the stored value. A
fingerprint says two inputs were equal; it says nothing about whether the run was right.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Any, Dict, List, Optional

from . import paths
from .data_guard import redact_sensitive_text

FINGERPRINT_VERSION = "v1"
_FINGERPRINT_HEX = 24
# What a step keeps once its contents are gone.
_SKELETON_STEP_KEYS = ("kind", "status", "fp")
# What a run's detail keeps once its contents are gone. Artifacts become a count: their
# names and paths are contents too.
_SKELETON_DETAIL_KEYS = ("agent_id", "provider", "model", "outcome", "usage", "private_mode", "steps_fp")
_key_cache: Dict[str, bytes] = {}
_key_lock = threading.Lock()


def _key_path() -> str:
    return str(paths.state_dir() / "state" / "run-fingerprint.key")


def _read_key(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return b""


def _key() -> bytes:
    path = _key_path()
    with _key_lock:
        cached = _key_cache.get(path)
        if cached:
            return cached
        key = _read_key(path)
        if len(key) < 32:
            # Missing, or left empty or short by a crash mid-write: write a new one in full,
            # then keep whatever ended up on disk so every process agrees on it.
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            temporary = f"{path}.{os.getpid()}.{secrets.token_hex(4)}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
            if len(_read_key(path)) < 32:
                os.replace(temporary, path)
            else:
                os.unlink(temporary)  # another process finished first
            key = _read_key(path)
        try:
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
        _key_cache[path] = key
        return key


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    digest = hmac.new(_key(), canonical(value).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest[:_FINGERPRINT_HEX]}"


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def _step_fingerprint(step: Dict[str, Any]) -> Optional[str]:
    """What the step did with which inputs: its kind, its command line or tool title, and its inputs."""
    basis = {key: step.get(key) for key in ("kind", "input") if step.get(key) is not None}
    if step.get("kind") == "command" and step.get("detail"):
        basis["command"] = step["detail"]
    elif step.get("kind") == "tool" and step.get("title"):
        basis["tool"] = step["title"]
    if "input" not in basis and "command" not in basis:
        return None
    return fingerprint(basis)


def prepare(summary: Optional[str], output: Optional[str],
            detail: Optional[Dict[str, Any]]) -> tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Fingerprint the steps (before masking, so masking never changes them) and mask known secrets."""
    if isinstance(detail, dict) and isinstance(detail.get("steps"), list):
        steps = []
        for step in detail["steps"]:
            if isinstance(step, dict):
                step = dict(step)
                fp = _step_fingerprint(step)
                if fp:
                    step["fp"] = fp
            steps.append(step)
        detail = {**detail, "steps": steps,
                  "steps_fp": fingerprint([step.get("fp") if isinstance(step, dict) else None for step in steps])}
    return (_mask(summary) if summary else summary, _mask(output) if output else output,
            _mask(detail) if detail else detail)


def skeleton(detail: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A run's detail without its contents."""
    if not isinstance(detail, dict):
        return None
    out: Dict[str, Any] = {key: detail[key] for key in _SKELETON_DETAIL_KEYS if key in detail}
    if isinstance(detail.get("artifacts"), list):
        out["artifact_count"] = len(detail["artifacts"])
    if isinstance(detail.get("steps"), list):
        out["steps"] = [{key: step.get(key) for key in _SKELETON_STEP_KEYS if step.get(key) is not None}
                        for step in detail["steps"] if isinstance(step, dict)]
    return out or None
