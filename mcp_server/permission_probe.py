"""Read macOS privacy permissions for the current process without prompting.

macOS records Accessibility, Screen Recording and Automation consent for the
process that asks, so this must run inside the Mac MCP server to describe the
server. Every probe here is a read-only query that never shows a TCC prompt:
AXIsProcessTrusted, CGPreflightScreenCaptureAccess and
AEDeterminePermissionToAutomateTarget with askUserIfNeeded=false. Microphone
consent belongs to the separate voice helper app and cannot be read from here.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Optional

GRANTED = "granted"
DENIED = "denied"
NOT_DETERMINED = "not_determined"
NOT_RUNNING = "not_running"
UNKNOWN = "unknown"

_SETTINGS = "System Settings > Privacy & Security > "
_PANE = "x-apple.systempreferences:com.apple.preference.security?"

PERMISSIONS: Dict[str, Dict[str, Any]] = {
    "accessibility": {
        "title": "Accessibility",
        "features": ["Mac app control (mac_observe, mac_act)", "System Events and menu actions",
                     "native app adapters that open or select items"],
        "settings_path": _SETTINGS + "Accessibility",
        "settings_url": _PANE + "Privacy_Accessibility",
    },
    "screen_recording": {
        "title": "Screen Recording",
        "features": ["screenshots and visual observation", "browser and window captures", "OCR"],
        "settings_path": _SETTINGS + "Screen & System Audio Recording",
        "settings_url": _PANE + "Privacy_ScreenCapture",
    },
    "automation": {
        "title": "Automation",
        "features": ["Safari and Chrome browser tools", "Calendar, Reminders, Notes and Mail actions",
                     "System Events scripting"],
        "settings_path": _SETTINGS + "Automation",
        "settings_url": _PANE + "Privacy_Automation",
    },
    "microphone": {
        "title": "Microphone",
        "features": ["ask_user_voice spoken answers"],
        "settings_path": _SETTINGS + "Microphone",
        "settings_url": _PANE + "Privacy_Microphone",
    },
}

AUTOMATION_TARGETS = (
    ("System Events", "com.apple.systemevents"),
    ("Safari", "com.apple.Safari"),
    ("Google Chrome", "com.google.Chrome"),
    ("Calendar", "com.apple.iCal"),
    ("Reminders", "com.apple.reminders"),
    ("Notes", "com.apple.Notes"),
    ("Mail", "com.apple.mail"),
)

VOICE_HELPER = {"name": "Mac MCP Voice Helper", "bundle_id": "dev.macmcp.voicehelper"}

_FRAMEWORKS = "/System/Library/Frameworks/"


def _four_cc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


def _library(name: str) -> Optional[ctypes.CDLL]:
    if platform.system() != "Darwin":
        return None
    try:
        return ctypes.cdll.LoadLibrary(f"{_FRAMEWORKS}{name}.framework/{name}")
    except OSError:
        return None


def _bool_call(framework: str, symbol: str) -> str:
    lib = _library(framework)
    if lib is None:
        return UNKNOWN
    try:
        function = getattr(lib, symbol)
    except AttributeError:
        return UNKNOWN
    function.restype = ctypes.c_bool
    function.argtypes = []
    return GRANTED if function() else DENIED


def accessibility_state() -> str:
    return _bool_call("ApplicationServices", "AXIsProcessTrusted")


def screen_recording_state() -> str:
    return _bool_call("CoreGraphics", "CGPreflightScreenCaptureAccess")


# AEDeterminePermissionToAutomateTarget results.
_AUTOMATION_RESULTS = {0: GRANTED, -1743: DENIED, -1744: NOT_DETERMINED, -600: NOT_RUNNING}


def automation_state(bundle_id: str) -> str:
    lib = _library("CoreServices")
    if lib is None:
        return UNKNOWN
    try:
        create = lib.AECreateDesc
        determine = lib.AEDeterminePermissionToAutomateTarget
        dispose = lib.AEDisposeDesc
    except AttributeError:
        return UNKNOWN
    create.restype = ctypes.c_int16
    create.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
    determine.restype = ctypes.c_int32
    determine.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
    dispose.restype = ctypes.c_int16
    dispose.argtypes = [ctypes.POINTER(_AEDesc)]
    raw = bundle_id.encode("utf-8")
    target = _AEDesc()
    if create(_four_cc("bund"), raw, len(raw), ctypes.byref(target)) != 0:
        return UNKNOWN
    try:
        # askUserIfNeeded=False: report the stored decision, never prompt.
        status = determine(ctypes.byref(target), _four_cc("****"), _four_cc("****"), False)
    finally:
        dispose(ctypes.byref(target))
    return _AUTOMATION_RESULTS.get(int(status), UNKNOWN)


def _running_executable() -> Path:
    # Homebrew's python3 re-executes inside Python.app; macOS attributes consent
    # to the binary that is actually running, so ask the kernel for it.
    if platform.system() == "Darwin":
        try:
            libproc = ctypes.cdll.LoadLibrary("/usr/lib/libproc.dylib")
            buffer = ctypes.create_string_buffer(4096)
            if libproc.proc_pidpath(os.getpid(), buffer, ctypes.sizeof(buffer)) > 0:
                return Path(buffer.value.decode("utf-8", "replace"))
        except (OSError, AttributeError):
            pass
    return Path(sys.executable).resolve()


def process_identity() -> Dict[str, Any]:
    executable = _running_executable()
    app = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
    return {
        "pid": os.getpid(),
        "executable": str(executable),
        # System Settings lists the app bundle when there is one, else the binary.
        "listed_as": app.stem if app is not None else executable.name,
    }


def _entry(key: str, state: str, **extra: Any) -> Dict[str, Any]:
    return {"key": key, "state": state, **PERMISSIONS[key], **extra}


def probe_permissions() -> Dict[str, Any]:
    targets = [
        {"app": name, "bundle_id": bundle_id, "state": automation_state(bundle_id)}
        for name, bundle_id in AUTOMATION_TARGETS
    ]
    known = [row["state"] for row in targets if row["state"] not in {NOT_RUNNING, UNKNOWN}]
    if DENIED in known:
        automation = DENIED
    elif NOT_DETERMINED in known:
        automation = NOT_DETERMINED
    elif known:
        automation = GRANTED
    else:
        automation = UNKNOWN
    return {
        "ok": True,
        "context": process_identity(),
        "permissions": {
            "accessibility": _entry("accessibility", accessibility_state()),
            "screen_recording": _entry("screen_recording", screen_recording_state()),
            "automation": _entry("automation", automation, targets=targets),
            "microphone": _entry(
                "microphone", UNKNOWN,
                reason="Recorded by the voice helper app, which macOS tracks separately; it is asked the first time you use voice.",
                identity=VOICE_HELPER,
            ),
        },
    }


# Tool failures (#47): name the permission behind an error so the user is sent
# to the right Privacy & Security pane, and say plainly when it is not one.
REASON_CODES = {
    "accessibility": "ACCESSIBILITY_NOT_ALLOWED",
    "automation": "AUTOMATION_NOT_ALLOWED",
    "screen_recording": "SCREEN_RECORDING_NOT_ALLOWED",
}
_APPLE_EVENTS_DENIED = ("(-1743)", "not authorized to send apple events", "not allowed to send apple events")
_ASSISTIVE_DENIED = ("(-25211)", "assistive access")


def explain_failure(capability: str, error: str, *, check_state: bool = True) -> Dict[str, Any]:
    """Classify a failed Accessibility, Apple Events or screen capture call.

    The error text decides first (Apple Events and assistive-access refusals
    name themselves); otherwise, when check_state is set, the process's own
    read-only permission state for that capability does. Returns reason_code
    plus remediation for a permission, or permission=False when the
    permission is granted (an app error), or "unknown" when it cannot be told.
    """
    text = str(error or "").lower()
    key: Optional[str] = None
    if any(marker in text for marker in _APPLE_EVENTS_DENIED):
        key = "automation"
    elif any(marker in text for marker in _ASSISTIVE_DENIED):
        key = "accessibility"
    state = UNKNOWN
    if key is None and check_state and capability in {"accessibility", "screen_recording"}:
        state = accessibility_state() if capability == "accessibility" else screen_recording_state()
        if state == DENIED:
            key = capability
    if key is None:
        return {"permission": False if state == GRANTED else "unknown", "capability": capability}
    meta = PERMISSIONS[key]
    listed_as = process_identity()["listed_as"]
    if key == "automation":
        target = ""
        marker = "send apple events to "
        if marker in text:
            start = text.index(marker) + len(marker)
            target = str(error)[start:].split(".")[0].split("(")[0].strip()
        remediation = (f"In {meta['settings_path']}, expand “{listed_as}” and turn on "
                       f"{target or 'the app it needs to control'}.")
    else:
        remediation = f"In {meta['settings_path']}, turn on “{listed_as}”."
    return {
        "permission": True, "capability": key, "reason_code": REASON_CODES[key],
        "remediation": remediation, "settings_path": meta["settings_path"], "settings_url": meta["settings_url"],
        "degraded_features": list(meta["features"]),
    }
