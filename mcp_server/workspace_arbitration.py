from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import c_double, c_uint32
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .agent_admission import AdmissionError, claim_agent_resources
from .policy import current_policy_context

_DEFAULT_RECENT_INPUT_S = 2.0
_COREGRAPHICS_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CG_HID_SYSTEM_STATE = 1
_KNOWN_APP_BUNDLES = {
    "finder": "com.apple.finder",
    "notes": "com.apple.notes",
    "mail": "com.apple.mail",
    "calendar": "com.apple.ical",
    "preview": "com.apple.preview",
    "settings": "com.apple.systempreferences",
    "system settings": "com.apple.systempreferences",
    "system preferences": "com.apple.systempreferences",
}

_RESOURCE_DISPLAY_NAMES = {
    "com.apple.finder": "Finder",
    "com.apple.notes": "Notes",
    "com.apple.mail": "Mail",
    "com.apple.ical": "Calendar",
    "com.apple.preview": "Preview",
    "com.apple.systempreferences": "System Settings",
}

_CG_EVENT_TYPES = (
    1,   # leftMouseDown
    3,   # rightMouseDown
    5,   # mouseMoved
    6,   # leftMouseDragged
    7,   # rightMouseDragged
    10,  # keyDown
    22,  # scrollWheel
    25,  # otherMouseDown
    27,  # otherMouseDragged
)


def delegated_agent_identity() -> Optional[Dict[str, str]]:
    context = current_policy_context()
    agent_id = str(context.agent_id or "").strip()
    if not agent_id:
        return None
    return {
        "agent_id": agent_id,
        "team_id": str(context.team_id or "").strip(),
        "actor": str(context.actor or "").strip(),
    }


def seconds_since_user_input(timeout_s: float = 2.0) -> tuple[Optional[float], Optional[str]]:
    # CoreGraphics exposes the same physical-event age signal used by the
    # previous Swift PoC. ctypes avoids PyObjC and any runtime compiler step.
    _ = timeout_s  # kept for API compatibility with the earlier bounded probe.
    try:
        core_graphics = ctypes.CDLL(_COREGRAPHICS_PATH)
        probe = core_graphics.CGEventSourceSecondsSinceLastEventType
        probe.argtypes = [c_uint32, c_uint32]
        probe.restype = c_double
        ages = [
            float(probe(_CG_HID_SYSTEM_STATE, event_type))
            for event_type in _CG_EVENT_TYPES
        ]
    except (OSError, AttributeError, TypeError, ValueError):
        return None, "HUMAN_INPUT_PROBE_FAILED"
    finite = [age for age in ages if age >= 0.0]
    if not finite:
        return None, "HUMAN_INPUT_PROBE_INVALID"
    return min(finite), None


def recent_user_input(max_age_s: float = _DEFAULT_RECENT_INPUT_S) -> tuple[Optional[bool], Optional[str], Optional[float]]:
    age, error = seconds_since_user_input()
    if age is None:
        return None, error, None
    return age <= max(0.1, float(max_age_s)), None, age


def frontmost_application_name(timeout_s: float = 2.0) -> tuple[Optional[str], Optional[str]]:
    script = (
        'tell application "System Events" to get name of first application process '
        'whose frontmost is true'
    )
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.2, min(float(timeout_s), 5.0)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "FRONTMOST_APP_PROBE_TIMEOUT"
    except (OSError, subprocess.SubprocessError):
        return None, "FRONTMOST_APP_PROBE_FAILED"
    if proc.returncode != 0:
        return None, "FRONTMOST_APP_PROBE_FAILED"
    value = (proc.stdout or "").strip()
    return (value or None), None if value else "FRONTMOST_APP_UNAVAILABLE"


def native_app_human_takeover(app: str) -> Optional[Dict[str, Any]]:
    identity = delegated_agent_identity()
    if identity is None:
        return None

    target = str(app or "").strip()
    if not target:
        return {
            "reason_code": "HUMAN_OWNERSHIP_UNKNOWN",
            "retryable": True,
            "human_priority": True,
            "yielded": True,
            "resource_kind": "native_app",
            "probe_error": "target_app_missing",
            **identity,
        }

    frontmost, error = frontmost_application_name()
    if frontmost is None:
        return {
            "reason_code": "HUMAN_OWNERSHIP_UNKNOWN",
            "retryable": True,
            "human_priority": True,
            "yielded": True,
            "resource_kind": "native_app",
            "probe_error": error,
            **identity,
        }
    if frontmost != target:
        return None

    return {
        "reason_code": "HUMAN_ACTIVE_RESOURCE",
        "retryable": True,
        "human_priority": True,
        "yielded": True,
        "resource_kind": "native_app",
        **identity,
    }


def browser_human_takeover(browser: str, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Browser tabs never yield to the user being on them.

    Delegated agents used to stop whenever their tab was the visible tab of the
    frontmost browser, which also blocked them while the user was only watching
    the work. The owner asked for agents to keep acting on the page they are
    on, so browser mutations are no longer gated on human presence.
    """
    _ = (browser, row)
    return None


def native_app_resource_id(
    *,
    bundle_id: Optional[str] = None,
    app: Optional[str] = None,
    app_handle: Optional[str] = None,
) -> str:
    bundle = str(bundle_id or "").strip().lower()
    if bundle and bundle not in {"missing value", "none", "null"}:
        return bundle
    app_name = str(app or "").strip().lower()
    if app_name:
        return _KNOWN_APP_BUNDLES.get(app_name, app_name)
    return str(app_handle or "").strip().lower()


def native_window_resource_id(
    window_handle: str,
    *,
    bundle_id: Optional[str] = None,
    app_handle: Optional[str] = None,
    app: Optional[str] = None,
) -> str:
    window = str(window_handle or "").strip()
    app_identity = native_app_resource_id(
        bundle_id=bundle_id,
        app=app,
        app_handle=app_handle,
    )
    if not app_identity:
        return window
    return f"{app_identity}:{window}"


def _agent_admission_root() -> Path:
    override = os.getenv("MAC_MCP_AGENT_ADMISSION_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parent / "agents"


def claim_delegated_resource(
    kind: str,
    identifier: str,
    *,
    mode: str = "write",
) -> Optional[Dict[str, Any]]:
    identity = delegated_agent_identity()
    if identity is None:
        return None
    claim = {
        "kind": str(kind or "").strip().lower(),
        "id": str(identifier or "").strip(),
        "mode": str(mode or "write").strip().lower(),
    }
    try:
        result = claim_agent_resources(
            _agent_admission_root(),
            agent_id=identity["agent_id"],
            resources=[claim],
        )
    except AdmissionError as exc:
        return {
            "ok": False,
            "reason_code": "RESOURCE_ARBITRATION_UNAVAILABLE",
            "retryable": True,
            "yielded": True,
            "resource_kind": claim["kind"],
            "arbitration_error": exc.code,
        }
    if not result.get("admitted"):
        return {
            "ok": False,
            "reason_code": "RESOURCE_BUSY",
            "retryable": True,
            "yielded": True,
            "resource_kind": claim["kind"],
            "blocker_count": len(result.get("blockers") or []),
        }
    return {
        "ok": True,
        "lease_id": result.get("lease_id"),
        "generation": result.get("generation"),
        "resource_kind": claim["kind"],
        "mode": claim["mode"],
    }


def sanitize_resource_claims(claims: Iterable[Mapping[str, Any]]) -> list[Dict[str, str]]:
    sanitized: list[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for claim in claims or ():
        kind = str(claim.get("kind") or "").strip().lower()
        mode = str(claim.get("mode") or "write").strip().lower()
        identifier = str(claim.get("id") or "").strip()
        label = {
            "browser_tab": "Browser tab",
            "native_window": "Native window",
            "native_app": "Native app",
            "workspace": "Workspace",
            "path": "Workspace path",
            "file": "File",
            "process": "Process",
            "clipboard": "Clipboard",
        }.get(kind, "Resource")

        if kind == "native_window" and ":" in identifier:
            prefix = identifier.split(":", 1)[0].lower()
            if prefix in _RESOURCE_DISPLAY_NAMES:
                label = f"{_RESOURCE_DISPLAY_NAMES[prefix]} window"
            elif prefix and not prefix.startswith("mapp_") and len(prefix) <= 48:
                display = prefix.replace("-", " ").replace("_", " ").strip().title()
                label = f"{display} window"
        elif kind == "native_app" and identifier:
            normalized = identifier.lower()
            if normalized in _RESOURCE_DISPLAY_NAMES:
                label = f"{_RESOURCE_DISPLAY_NAMES[normalized]} app"
            elif not normalized.startswith("mapp_") and len(identifier) <= 48:
                display = identifier.replace("-", " ").replace("_", " ").strip().title()
                label = f"{display} app"

        key = (kind, mode, label)
        if key in seen:
            continue
        seen.add(key)
        sanitized.append({"kind": kind, "mode": mode, "label": label})
    return sanitized
