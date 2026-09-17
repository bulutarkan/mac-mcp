from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

READINESS_TIMEOUT_S = 1.2
READINESS_POLL_S = 0.06
READINESS_STABLE_MS = 120
ACTION_VERIFY_TIMEOUT_S = 1.2
ACTION_VERIFY_POLL_S = 0.07

_EFFECT_ACTIONS = {
    "click", "double_click", "type", "type_text", "paste",
    "action", "accessibility_action", "menu",
}


def _rect(state: Dict[str, Any], key: str = "position") -> Optional[Tuple[int, int, int, int]]:
    raw = state.get(key)
    if not isinstance(raw, dict):
        return None
    try:
        x = int(raw.get("x"))
        y = int(raw.get("y"))
        width = int(raw.get("width"))
        height = int(raw.get("height"))
    except (TypeError, ValueError):
        return None
    return x, y, width, height


def geometry_signature(state: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        _rect(state),
        _rect(state, "window_position"),
        state.get("window_title"),
        state.get("role"),
        state.get("subrole"),
    )



def observed_geometry_matches(state: Dict[str, Any], observed_node: Optional[Dict[str, Any]]) -> bool:
    if not observed_node:
        return False
    current = _rect(state)
    observed = _rect(observed_node)
    return current is not None and observed is not None and current == observed

def observed_identity_matches(state: Dict[str, Any], observed_node: Optional[Dict[str, Any]]) -> bool:
    if not observed_node:
        return True
    for key in ("role", "subrole"):
        before = str(observed_node.get(key) or "")
        current = str(state.get(key) or "")
        if before and current and before != current:
            return False
    # Actionable labels are part of the conservative identity check. Empty/dynamic
    # labels are intentionally ignored to avoid rejecting legitimate unlabeled controls.
    before_title = str(observed_node.get("title") or "")
    current_title = str(state.get("title") or "")
    if before_title and current_title and before_title != current_title:
        return False
    return True


def readiness_reason(
    state: Dict[str, Any],
    *,
    observed_node: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    if not state.get("connected"):
        return "ELEMENT_DETACHED"
    if not observed_identity_matches(state, observed_node):
        return "STALE_ELEMENT_PATH"
    if state.get("window_minimized") is True:
        return "WINDOW_MINIMIZED"
    if state.get("enabled") is False:
        return "ELEMENT_DISABLED"
    if state.get("hidden") is True or state.get("visible") is False or state.get("offscreen") is True:
        return "ELEMENT_NOT_VISIBLE"
    if state.get("busy") is True:
        return "ELEMENT_BUSY"

    rect = _rect(state)
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return "ELEMENT_ZERO_BOUNDS"

    window_rect = _rect(state, "window_position")
    if window_rect is not None and window_rect[2] > 0 and window_rect[3] > 0:
        x, y, width, height = rect
        wx, wy, ww, wh = window_rect
        overlaps = x < wx + ww and x + width > wx and y < wy + wh and y + height > wy
        if not overlaps:
            return "ELEMENT_OUTSIDE_WINDOW"

    if state.get("modal_sheet_blocks_target") is True or state.get("popover_covers_target") is True:
        return "ELEMENT_OCCLUDED"
    return None


def verification_required(action_type: str, element_id: Optional[str]) -> bool:
    return bool(element_id) and str(action_type or "").lower().replace("-", "_") in _EFFECT_ACTIONS


def _different(before: Dict[str, Any], after: Dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(
        key in before and key in after and before.get(key) != after.get(key)
        for key in keys
    )


def effect_changed(before: Dict[str, Any], after: Dict[str, Any], action: Dict[str, Any]) -> tuple[bool, str]:
    if not before or not after:
        return False, "insufficient_state"
    if before.get("connected", True) and not after.get("connected", True):
        return True, "target_disconnected"

    typ = str(action.get("type") or "").lower().replace("-", "_")
    if typ in {"type", "type_text", "paste"}:
        if _different(before, after, ("value", "character_count")):
            return True, "text_state_changed"
        return False, "text_state_unchanged"

    target_keys = (
        "value", "character_count", "selected", "enabled", "title", "child_count",
    )
    if _different(before, after, target_keys):
        return True, "target_state_changed"

    window_keys = (
        "window_title", "window_count", "window_child_count",
        "sheet_count", "popover_count", "menu_count",
    )
    if _different(before, after, window_keys):
        return True, "window_state_changed"
    return False, "state_unchanged"
