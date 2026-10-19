"""The owner's iPhone, through macOS iPhone Mirroring (Settings > Devices > iPhone).

The work happens in iphone_helper.swift, compiled on first use and cached by source hash: ScreenCaptureKit for the
screen, Apple Vision for its text (Turkish and English), and real mouse and keyboard events for input, sent only
while iPhone Mirroring is in front for the moment it takes, with the pointer and the owner's app put back after.

How far agents may go is the owner's choice, in settings.json -> iphone.access:
- "off": agents cannot use the iPhone.
- "read_only" (the default): see the screen and read its text.
- "full": also tap, scroll, swipe, type and open apps.
This Mac's own permission profile still comes first: on a Read Only Mac, full access reads only.
"""
from __future__ import annotations

import base64
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp.utilities.types import Image

from . import privacy_policy, screen_lock, swift_build
from .policy import IPHONE_INPUT_ACTIONS
from .runtime_settings import load_runtime_settings, update_runtime_setting

ACCESS_LEVELS = ("off", "read_only", "full")
DEFAULT_ACCESS = "read_only"
MIRROR_APP = "iPhone Mirroring"
MIRROR_BUNDLE_ID = "com.apple.ScreenContinuity"

# What each action needs: reading the screen, or acting on the iPhone.
READ_ACTIONS = frozenset({"status", "see", "read", "find"})
INPUT_ACTIONS = IPHONE_INPUT_ACTIONS
# Step types of action=act (one call, one moment in front, the screen after all of them).
STEP_TYPES = frozenset({"tap", "tap_text", "double_tap", "long_press", "swipe", "scroll", "type", "key", "home",
                        "app_switcher", "spotlight", "open_app", "wait", "wait_for_text"})
MAX_STEPS = 40
ACTIONS = READ_ACTIONS | INPUT_ACTIONS

_SOURCE = Path(__file__).resolve().with_name("iphone_helper.swift")
_build_lock = threading.Lock()
_executable: Optional[str] = None


class IPhoneError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def access_setting() -> str:
    section = load_runtime_settings().get("iphone")
    value = section.get("access") if isinstance(section, dict) else None
    return value if value in ACCESS_LEVELS else DEFAULT_ACCESS


def set_access(level: str) -> str:
    if level not in ACCESS_LEVELS:
        raise IPhoneError("IPHONE_ACCESS_INVALID", "access is off, read_only or full.")
    update_runtime_setting("iphone", "access", level)
    return level


def effective_access() -> Dict[str, Any]:
    """The owner's choice, never more than this Mac's own permission profile allows."""
    from .policy import permission_profile_name  # read live: tests and the dashboard change it

    granted = access_setting()
    profile = permission_profile_name()
    effective = "read_only" if granted == "full" and profile == "read_only" else granted
    return {"granted": granted, "effective": effective, "this_mac_profile": profile,
            "limited_by_this_mac": effective != granted}


def _helper() -> str:
    global _executable
    with _build_lock:
        if _executable is None:
            directory = swift_build.cache_dir("MAC_MCP_IPHONE_CACHE", "~/.mac-mcp/cache/iphone-helper")
            _executable = str(swift_build.compile_cached(_SOURCE, directory, "iphone-helper"))
        return _executable


def _run(args: List[str], timeout_s: float = 30.0, stdin: Optional[str] = None) -> Dict[str, Any]:
    try:
        executable = _helper()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise IPhoneError("IPHONE_HELPER_UNAVAILABLE", f"The iPhone helper could not be built: {exc}") from exc
    try:
        proc = subprocess.run([executable, *args], input=stdin if stdin is not None else "", capture_output=True,
                              text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise IPhoneError("IPHONE_HELPER_TIMEOUT", "The iPhone helper timed out.") from exc
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise IPhoneError("IPHONE_HELPER_FAILED", (proc.stderr or "no answer").strip()[:300]) from exc
    if not isinstance(payload, dict):
        raise IPhoneError("IPHONE_HELPER_FAILED", "The iPhone helper gave no answer.")
    return payload


def status() -> Dict[str, Any]:
    """Whether iPhone Mirroring is open, without capturing anything (the dashboard polls this)."""
    try:
        mirror = _run(["status"], timeout_s=10.0)
    except IPhoneError as exc:
        mirror = {"ok": False, "reason_code": exc.code, "error": str(exc)}
    return {"ok": True, "mirroring": mirror, "access": effective_access()}


def open_mirroring() -> Dict[str, Any]:
    return _run(["open-mirroring"], timeout_s=15.0)


def _step(action: str, *, x: Any, y: Any, to_x: Any, to_y: Any, text: Optional[str], key: Optional[str],
          app: Optional[str], dx: Any, dy: Any, duration_ms: Any) -> Dict[str, Any]:
    """One input action as a step of action=act."""
    step: Dict[str, Any] = {"type": action}
    for name, value in (("x", x), ("y", y), ("to_x", to_x), ("to_y", to_y), ("dx", dx), ("dy", dy),
                        ("duration_ms", duration_ms)):
        if value is not None:
            step[name] = value
    if text:
        step["text"] = text
    if key:
        step["key"] = key
    if app:
        step["app"] = app
    return step


def _number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return float(value) == float(value) and abs(float(value)) < 1e6
    except (TypeError, ValueError):
        return False


def check_steps(steps: Any) -> List[Dict[str, Any]]:
    """Refuses a batch before anything reaches the iPhone when a step is malformed."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"steps is a list of 1 to {MAX_STEPS} steps.")
    clean: List[Dict[str, Any]] = []
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index} is not an object.")
        step = {str(k): v for k, v in raw.items()}
        kind = str(step.get("type") or "").strip().lower().replace("-", "_")
        if kind not in STEP_TYPES:
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index}: type is one of " + ", ".join(sorted(STEP_TYPES)) + ".")
        step["type"] = kind
        needs = {"tap": ("x", "y"), "double_tap": ("x", "y"), "long_press": ("x", "y"),
                 "swipe": ("x", "y", "to_x", "to_y")}.get(kind, ())
        for name in needs:
            if not _number(step.get(name)):
                raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index} ({kind}) needs {name} in phone points.")
        for name in ("x", "y", "to_x", "to_y", "dx", "dy", "duration_ms", "ms", "timeout_ms", "settle_ms", "index"):
            if name in step and not _number(step[name]):
                raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index}: {name} must be a number.")
        wanted = {"tap_text": "text", "wait_for_text": "text", "type": "text", "key": "key", "open_app": "app"}.get(kind)
        if wanted and not (isinstance(step.get(wanted), str) and step[wanted].strip()):
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index} ({kind}) needs {wanted}.")
        if kind == "type" and "\n" in step["text"]:
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", f"Step {index}: type one line; a key step with return submits.")
        clean.append(step)
    return clean


def _result(payload: Dict[str, Any], access: Dict[str, Any]) -> Any:
    image = payload.pop("image_base64", None)
    payload["access"] = access["effective"]
    payload["coordinates"] = "phone points: origin top left, the screen as returned (width x height)"
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if image:
        return [text, Image(data=base64.b64decode(image), format="jpeg")]
    return text


def run(action: str = "see", *, steps: Any = None, x: Any = None, y: Any = None, to_x: Any = None,
        to_y: Any = None, text: Optional[str] = None, key: Optional[str] = None, app: Optional[str] = None,
        dx: Any = None, dy: Any = None, duration_ms: Any = None, ocr: bool = False, image: bool = True,
        previous_hash: Optional[str] = None) -> Any:
    verb = str(action or "see").strip().lower().replace("-", "_")
    if steps is not None and verb == "see":
        verb = "act"
    try:
        if verb not in ACTIONS:
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", "action is one of: " + ", ".join(sorted(ACTIONS)))
        access = effective_access()
        if verb == "status":
            return status()
        if access["effective"] == "off":
            raise IPhoneError("IPHONE_ACCESS_OFF", "The owner turned off agent use of the iPhone (Settings > Devices "
                              "> iPhone). Do not work around it.")
        if verb in INPUT_ACTIONS and access["effective"] != "full":
            why = ("this Mac is set to Read Only in Permissions & Safety" if access["limited_by_this_mac"]
                   else "the owner allows reading the iPhone only (Settings > Devices > iPhone)")
            raise IPhoneError("IPHONE_READ_ONLY", f"Tapping and typing on the iPhone are off: {why}. Seeing and "
                              "reading the screen still work.")
        batch: Optional[List[Dict[str, Any]]] = None
        if verb == "act":
            batch = check_steps(steps)
        elif verb in INPUT_ACTIONS and verb != "connect":
            batch = check_steps([_step(verb, x=x, y=y, to_x=to_x, to_y=to_y, text=text, key=key, app=app, dx=dx,
                                       dy=dy, duration_ms=duration_ms)])
        if verb == "find" and not (text and text.strip()):
            raise IPhoneError("IPHONE_ARGUMENT_INVALID", "action=find needs text to look for on the screen.")
        privacy_policy.check_app(MIRROR_APP, MIRROR_BUNDLE_ID)
        if screen_lock.is_screen_locked():
            raise IPhoneError("MAC_SCREEN_LOCKED", "This Mac's screen is locked, so iPhone Mirroring cannot be seen "
                              "or used until it is unlocked.")
        if batch is not None:
            payload = _run(["act"], timeout_s=30.0 + 8.0 * len(batch),
                           stdin=json.dumps({"steps": batch, "image": bool(image), "ocr": bool(ocr)}))
        elif verb == "connect":
            payload = _run(["connect", "--image", "1" if image else "0"] + (["--ocr", "1"] if ocr else []), timeout_s=60.0)
        elif verb == "find":
            payload = _run(["find", str(text)])
        else:
            args = [verb]
            if verb == "see" and previous_hash:
                args += ["--previous", str(previous_hash)]
            if verb == "see" and ocr:
                args += ["--ocr", "1"]
            payload = _run(args)
        if not payload.get("ok"):
            message = str(payload.pop("error", "") or "The iPhone helper refused that.")
            if payload.get("image_base64"):  # a batch that stopped part way still shows where it stopped
                payload["message"] = message
                payload["error"] = str(payload.get("reason_code") or "iphone_failed").lower()
                return _result(payload, access)
            return {**payload, "ok": False, "error": str(payload.get("reason_code") or "iphone_failed").lower(),
                    "message": message}
        return _result(payload, access)
    except IPhoneError as exc:
        return {"ok": False, "error": exc.code.lower(), "reason_code": exc.code, "message": str(exc)}
