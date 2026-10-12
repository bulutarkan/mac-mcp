"""Ask macOS for a permission from the server process (the onboarding's "Allow" buttons).

Kept apart from permission_probe, which only reads states and must never prompt. Asked
from the server, the prompt names the process that holds the grant: "Mac MCP" when the
server runs under Mac MCP Server.
"""
from __future__ import annotations

import ctypes
from typing import Any, Dict

from .permission_probe import GRANTED, _library, accessibility_state, screen_recording_state

REQUESTABLE = ("accessibility", "screen_recording")


def _prompt_accessibility() -> None:
    """AXIsProcessTrustedWithOptions with the prompt option: macOS asks once, naming the holder."""
    services = _library("ApplicationServices")
    foundation = _library("CoreFoundation")
    if services is None or foundation is None:
        return
    try:
        key = ctypes.c_void_p.in_dll(services, "kAXTrustedCheckOptionPrompt")
        true = ctypes.c_void_p.in_dll(foundation, "kCFBooleanTrue")
        create = foundation.CFDictionaryCreate
        release = foundation.CFRelease
        trusted = services.AXIsProcessTrustedWithOptions
    except (AttributeError, ValueError):
        return
    create.restype = ctypes.c_void_p
    create.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                       ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    trusted.restype = ctypes.c_bool
    trusted.argtypes = [ctypes.c_void_p]
    release.argtypes = [ctypes.c_void_p]
    keys = (ctypes.c_void_p * 1)(key.value)
    values = (ctypes.c_void_p * 1)(true.value)
    options = create(None, keys, values, 1, None, None)
    try:
        trusted(options)
    finally:
        if options:
            release(options)


def _request_screen_capture() -> bool:
    graphics = _library("CoreGraphics")
    if graphics is None:
        return False
    try:
        request = graphics.CGRequestScreenCaptureAccess
    except AttributeError:
        return False
    request.restype = ctypes.c_bool
    request.argtypes = []
    return bool(request())


def request_permission(permission: str) -> Dict[str, Any]:
    """Ask macOS for a permission from the server process, so the prompt names its holder."""
    if permission == "accessibility":
        _prompt_accessibility()
        state = accessibility_state()
        return {"permission": permission, "state": state, "restart_needed": False}
    if permission == "screen_recording":
        _request_screen_capture()
        state = screen_recording_state()
        # The grant takes effect for this process only after a restart.
        return {"permission": permission, "state": state, "restart_needed": state != GRANTED}
    raise ValueError(f"unknown permission: {permission}")
