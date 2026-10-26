"""Where an agent's browser tab sits in the tab bar, for the menu app's tab pulse (#79).

The menu app draws a short glow over the tab an agent works in, but only when the owner is not
looking at that tab. It sends the event's browser and tab_handle here; this reads the tab's place
from the tab registry and its button's frame from the browser's Accessibility tree. Read-only: no
tab is selected and the browser never comes forward. When the tab cannot be matched for certain,
there is no frame, so nothing is drawn on the wrong tab.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import ax_native, browser_tabs, privacy_policy

_APP_NAMES = {"Safari": "Safari", "Google Chrome": "Google Chrome"}
# Safari's tab buttons are two levels under the window; Chrome nests its tab strip deeper. Going no
# deeper keeps the page itself out of the walk, so following a tab stays quick.
_TAB_DEPTH = {"Safari": 3, "Google Chrome": 8}
# Tab buttons sit in the window's top strip; anything lower is page content.
_TAB_STRIP_DEPTH_PT = 140
# How many buttons had the tab's title when it was last found by its handle. While the pulse follows the tab
# (by title, without the handle), one fewer means the agent's tab closed and its twin is the one left.
_TWINS: "OrderedDict[str, int]" = OrderedDict()
_TWINS_LOCK = threading.Lock()
_TWINS_KEEP = 64


def _norm_title(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _same_title(button: str, tab: str) -> bool:
    # Tab buttons may show a shortened title; the tab registry has the full one.
    a, b = _norm_title(button), _norm_title(tab)
    if not a or not b:
        return False
    # Both sides long enough, so a short title ("Mac") never claims a longer, different tab.
    return a == b or (len(a) >= 8 and b.startswith(a.rstrip("…."))) or (len(b) >= 8 and a.startswith(b))


def tab_buttons(nodes: List[Dict[str, Any]], window_top: float) -> List[Dict[str, Any]]:
    """The window's tab buttons, left to right."""
    found = []
    for node in nodes:
        pos = node.get("position") or {}
        y, width, height = pos.get("y"), pos.get("width"), pos.get("height")
        if y is None or not width or not height:
            continue
        is_tab = node.get("subrole") == "AXTabButton" or (
            node.get("role") == "AXRadioButton" and float(y) - window_top < _TAB_STRIP_DEPTH_PT
        )
        if is_tab and str(node.get("title") or node.get("description") or "").strip():
            found.append(node)
    return sorted(found, key=lambda node: float((node.get("position") or {}).get("x") or 0))


def pick_tab_button(buttons: List[Dict[str, Any]], tab_index: int, title: str) -> Optional[Dict[str, Any]]:
    """The button for this tab: the one at its position when its title agrees, else the only one with its title."""
    if 1 <= tab_index <= len(buttons) and _same_title(_label(buttons[tab_index - 1]), title):
        return buttons[tab_index - 1]
    matches = [node for node in buttons if _same_title(_label(node), title)]
    return matches[0] if len(matches) == 1 else None


def _label(node: Dict[str, Any]) -> str:
    return str(node.get("title") or node.get("description") or "")


def _is_selected(node: Dict[str, Any]) -> bool:
    return node.get("selected") is True or str(node.get("value") or "").strip().lower() in {"true", "1"}


def _remember_twins(tab_handle: str, count: int) -> None:
    with _TWINS_LOCK:
        _TWINS[tab_handle] = count
        _TWINS.move_to_end(tab_handle)
        while len(_TWINS) > _TWINS_KEEP:
            _TWINS.popitem(last=False)


def tab_frame(browser: str, tab_handle: str, hint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The tab button's frame. With hint (window_index, tab_index, title from an earlier answer), only
    the tab bar is read again, cheap enough to follow the tab while its pulse plays (tabs opening or
    closing resize it)."""
    from .tools_ui import _parse_observation

    # Tab titles and the browser's Accessibility tree are not read while the owner paused observation.
    privacy_policy.check_tool("browser_list_tabs")
    app = browser_tabs._browser_key(browser)
    if app not in _APP_NAMES:
        return {"ok": False, "error": "unsupported_browser"}
    hint = hint if isinstance(hint, dict) else {}
    following = int(hint.get("window_index") or 0) > 0 and bool(str(hint.get("title") or "").strip())
    if following:
        window_index, tab_index = int(hint["window_index"]), int(hint.get("tab_index") or 0)
        row: Dict[str, Any] = {"title": str(hint["title"])}
    else:
        try:
            window_index, tab_index, row = browser_tabs.resolve_tab(app, tab_handle)
        except (KeyError, browser_tabs.AmbiguousTabHandleError):
            return {"ok": False, "error": "tab_not_found"}
        if row.get("active"):
            # The tab shows in its window: the owner sees its page, so there is nothing to point at.
            return {"ok": True, "active": True}
    observed = ax_native.observe(
        app=_APP_NAMES[app], app_pid=None, window_index=window_index,
        max_depth=_TAB_DEPTH[app], max_children=80, max_nodes=600, timeout_s=2.0,
    )
    if not observed or not observed[0]:
        return {"ok": False, "error": "tab_bar_unavailable"}
    metadata, nodes = _parse_observation(observed[1])
    window = next((item for item in metadata.get("windows") or [] if int(item.get("index") or 0) == window_index), None)
    if not window or window.get("on_shown_desktop") is False:
        return {"ok": False, "error": "window_not_shown"}
    top = float((window.get("position") or {}).get("y") or 0)
    buttons = tab_buttons(nodes, top)
    title = str(row.get("title") or "")
    twins = sum(1 for node in buttons if _same_title(_label(node), title))
    if following:
        with _TWINS_LOCK:
            known = _TWINS.get(str(tab_handle))
        if known is not None and twins < known:
            return {"ok": False, "error": "tab_not_found"}
    else:
        _remember_twins(str(tab_handle), twins)
    button = pick_tab_button(buttons, tab_index, title)
    if button is None:
        return {"ok": False, "error": "tab_button_not_matched"}
    if _is_selected(button):
        # The owner switched to this tab meanwhile.
        return {"ok": True, "active": True}
    pos = button["position"]
    return {
        "ok": True, "active": False,
        "frame": {key: float(pos[key]) for key in ("x", "y", "width", "height")},
        "window_id": window.get("window_id"),
        "app_pid": metadata.get("pid"),
        "hint": {"window_index": window_index, "tab_index": buttons.index(button) + 1, "title": title},
    }
