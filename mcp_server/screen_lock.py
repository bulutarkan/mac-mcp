"""Whether the owner's login session is behind the macOS lock screen.

While the screen is locked, macOS hides every app window from Accessibility and
screen capture returns the lock screen, so native UI tools would fail with a
misleading "window not found" or hand back a picture of the lock screen. Shell,
files, browser page tools and AppleScript to scriptable apps keep working.
"""
from __future__ import annotations

import ctypes
import platform
from typing import Any, Dict, Optional

_FRAMEWORKS = "/System/Library/Frameworks/"
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_LOCKED_KEY = b"CGSSessionScreenIsLocked"


def _library(name: str) -> Optional[ctypes.CDLL]:
    try:
        return ctypes.cdll.LoadLibrary(f"{_FRAMEWORKS}{name}.framework/{name}")
    except OSError:
        return None


def is_screen_locked() -> Optional[bool]:
    """True when locked, False when unlocked, None when it cannot be told (no GUI session, not macOS)."""
    if platform.system() != "Darwin":
        return None
    cg, cf = _library("CoreGraphics"), _library("CoreFoundation")
    if cg is None or cf is None:
        return None
    try:
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
    except AttributeError:
        return None
    session = cg.CGSessionCopyCurrentDictionary()
    if not session:
        return None
    key = cf.CFStringCreateWithCString(None, _LOCKED_KEY, _UTF8)
    try:
        if not key:
            return None
        # The key is only present while the session is locked.
        value = cf.CFDictionaryGetValue(session, key)
        return bool(value) and bool(cf.CFBooleanGetValue(value))
    finally:
        if key:
            cf.CFRelease(key)
        cf.CFRelease(session)


def locked_error(tool: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": f"The Mac's screen is locked, so {tool} cannot see or use app windows until the owner unlocks it.",
        "reason_code": "SCREEN_LOCKED",
        "screen_locked": True,
        "retryable": False,
        "automatic_retry": False,
        "hint": (
            "While the screen is locked, macOS hides app windows from Accessibility and screen capture shows the "
            "lock screen. Shell commands, file tools, browser page tools (browser_observe/browser_act/browser_do) "
            "and AppleScript to scriptable apps through run_command still work."
        ),
    }
