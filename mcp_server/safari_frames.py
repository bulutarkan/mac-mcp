"""Safari child frames through Accessibility (#384).

Page scripts reach a cross-origin iframe only in Chrome, through the companion's debugger. Safari
has no such door, but it shows each child frame to Accessibility as a web area of its own, with its
URL, inside the page's. browser_observe and browser_act with frame=... in Safari read and use that
web area instead: the frame's labelled controls get element ids, and clicks, text, choices and
check boxes go through Accessibility to the exact element that was observed; a page's custom
combobox is opened and its option pressed. Safari is not brought forward; choosing in an HTML
select opens its menu on screen for a moment.

Accessibility sees only the tab a window shows, so the tab must be the current one in its window.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import ax_native

_SAFARI_BUNDLE = "com.apple.Safari"
# What an agent can work with, named the way page observations name them.
_CONTROL_ROLES = {
    "AXTextField": "textbox", "AXTextArea": "textbox", "AXComboBox": "combobox", "AXPopUpButton": "select",
    "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXButton": "button", "AXLink": "link",
    "AXMenuButton": "button", "AXSlider": "slider", "AXDisclosureTriangle": "button",
}
_TEXT_ROLES = {"AXStaticText": "text", "AXHeading": "heading"}
_MAX_TEXTS = 80
_SUPPORTED = ("click", "fill", "type", "select", "check", "uncheck")
_SETTLE_S = 0.6

_OBSERVATIONS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_LOCK = threading.Lock()
_KEEP = 32


def unavailable(message: str, reason: str = "frame_accessibility_unavailable", **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": reason, "transport": "accessibility", "message": message, **extra}


def _name(node: Dict[str, Any]) -> str:
    for key in ("title", "description", "placeholder", "help"):
        if str(node.get(key) or "").strip():
            return str(node[key]).strip()
    return ""


def _element(node: Dict[str, Any], element_id: str, name: str) -> Dict[str, Any]:
    role = _CONTROL_ROLES[node["role"]]
    out: Dict[str, Any] = {"element_id": element_id, "role": role, "name": name}
    value = str(node.get("value") or "")
    if role in {"checkbox", "radio"}:
        out["checked"] = value == "1"
    elif value and role != "button":
        out["value"] = value[:200]
    if node.get("disabled"):
        out["disabled"] = True
    if node.get("focused"):
        out["focused"] = True
    if "x" in node and "w" in node:
        out["rect"] = {k: round(float(node.get(k) or 0)) for k in ("x", "y", "w", "h")}
    return out


def _remember(record: Dict[str, Any]) -> str:
    observation_id = f"axf_{uuid.uuid4().hex[:12]}"
    with _LOCK:
        _OBSERVATIONS[observation_id] = record
        while len(_OBSERVATIONS) > _KEEP:
            _OBSERVATIONS.popitem(last=False)
    return observation_id


def _recall(observation_id: Optional[str]) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _OBSERVATIONS.get(str(observation_id or ""))


def observe(page_url: str, frame: str, max_elements: int = 80) -> Dict[str, Any]:
    """The frame's controls and text, with element ids for act()."""
    answer = ax_native.observe_frame(bundle_id=_SAFARI_BUNDLE, page_url=page_url, frame=frame)
    if answer is None:
        return unavailable("Mac MCP's Accessibility helper is not ready or Accessibility access is off.")
    if not answer.get("ok"):
        reason = str(answer.get("reason_code") or "")
        hints = {
            "FRAME_PAGE_NOT_SHOWN": "Show this tab in its window (browser_activate_tab), then observe again.",
            "FRAME_NOT_FOUND": "Pass part of one of the listed frame URLs.",
            "FRAME_AMBIGUOUS": "Pass a longer part of the frame URL.",
        }
        return unavailable(str(answer.get("error") or "The frame could not be read."), reason=reason.lower() or "frame_error",
                           frames=answer.get("frames"), hint=hints.get(reason))
    nodes: List[Dict[str, Any]] = [n for n in answer.get("nodes") or [] if isinstance(n, dict)]
    elements: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    targets: Dict[str, Dict[str, Any]] = {}
    text_paths: List[str] = []
    # The last text seen under each parent: a page's custom control often has no name of its own, only a
    # label drawn just before it ("Country *" above a combobox).
    label_before: Dict[str, str] = {}
    for node in nodes:
        role = str(node.get("role") or "")
        path = str(node.get("id") or "")
        parent = path.rsplit("/", 1)[0]
        if role in _TEXT_ROLES:
            label_before[parent] = str(node.get("value") or node.get("title") or "").strip()
        if role in _CONTROL_ROLES and len(elements) < max(1, int(max_elements)):
            element_id = f"a{len(elements) + 1}"
            name = _name(node) or label_before.get(parent, "")
            elements.append(_element(node, element_id, name))
            targets[element_id] = {"path": path, "role": role, "title": str(node.get("title") or ""),
                                   "description": str(node.get("description") or ""), "kind": _CONTROL_ROLES[role],
                                   "name": name, "value": str(node.get("value") or "")}
        elif role in _TEXT_ROLES and len(texts) < _MAX_TEXTS:
            # A static text repeats the text of the one it sits in; keep the outer one.
            if any(path.startswith(outer + "/") for outer in text_paths):
                continue
            value = str(node.get("value") or node.get("title") or "").strip()
            if value:
                text_paths.append(path)
                texts.append({"role": _TEXT_ROLES[role], "text": value[:300]})
    record = {"page_url": page_url, "frame": frame, "frame_url": answer.get("frame_url"), "pid": int(answer.get("pid") or 0),
              "targets": targets}
    observation_id = _remember(record)
    return {
        "ok": True, "observation_id": observation_id, "transport": "accessibility",
        "url": answer.get("frame_url"), "page_url": answer.get("page_url"),
        "element_count": len(elements), "elements": elements, "texts": texts,
        "truncated": bool(answer.get("truncated")) or None,
        "note": "Safari child frame read through Accessibility; act on these element ids with browser_act and the same frame.",
    }


def _refresh_paths(record: Dict[str, Any]) -> bool:
    """Read the frame again and move each target to where the same control (kind and name) now is.
    Safari adds and removes small controls as focus moves (an AutoFill button beside the focused
    field), which shifts the paths of the controls after them."""
    fresh = observe(record["page_url"], record["frame"])
    current = _recall(fresh.get("observation_id")) if fresh.get("ok") else None
    if not current:
        return False
    for target in record["targets"].values():
        same = [t for t in current["targets"].values() if t["kind"] == target["kind"] and t["name"] == target["name"]]
        if len(same) == 1:
            target.update({key: same[0][key] for key in ("path", "role", "title", "description", "value")})
    record["pid"] = current["pid"]
    return True


def _act_on(record: Dict[str, Any], element_id: str, op: str, text: Optional[str] = None,
            retried: bool = False) -> Dict[str, Any]:
    target = record["targets"].get(element_id)
    if target is None:
        return {"ok": False, "error": "unknown_element_id", "element_id": element_id,
                "message": "Observe the frame again and use an element id from that observation."}
    expect = {"role": target["role"]}
    if target["title"]:
        expect["title"] = target["title"]
    if target["description"]:
        expect["description"] = target["description"]
    answer = ax_native.act(app_pid=record["pid"], element_id=target["path"], op=op, text=text, replace=True, expect=expect)
    if answer and answer.get("reason_code") == "STALE_ELEMENT_PATH" and not retried and _refresh_paths(record):
        return _act_on(record, element_id, op, text, retried=True)
    if answer is None:
        return {"ok": False, "error": "frame_accessibility_unavailable", "element_id": element_id}
    out = {"element_id": element_id, "name": target["name"], **answer}
    out.setdefault("ok", False)
    return out


def _find_label(record: Dict[str, Any], label: str) -> Optional[str]:
    wanted = " ".join(str(label).split()).casefold()
    named = [(eid, " ".join(t["name"].split()).casefold()) for eid, t in record["targets"].items()]
    exact = [eid for eid, name in named if name == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = [eid for eid, name in named if wanted and wanted in name]
    return partial[0] if len(partial) == 1 else None


def _set(record: Dict[str, Any], element_id: str, value: Any) -> Dict[str, Any]:
    target = record["targets"].get(element_id)
    if target is None:
        return _act_on(record, element_id, "press")
    kind = target["kind"]
    if kind == "select":
        return _act_on(record, element_id, "select_option", str(value))
    if kind == "combobox":
        # A page's combobox usually opens a list to choose from; one that opens none takes typed text.
        chosen = _act_on(record, element_id, "select_option", str(value))
        if chosen.get("reason_code") != "MENU_NOT_FOUND":
            return chosen
        return _act_on(record, element_id, "set_text", str(value))
    if kind in {"checkbox", "radio"}:
        wanted = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on", "checked"}
        if (target["value"] == "1") == wanted:
            return {"ok": True, "element_id": element_id, "name": target["name"], "effect_observed": False, "already": True}
        return _act_on(record, element_id, "press")
    return _act_on(record, element_id, "set_text", str(value))


def act(page_url: str, frame: str, actions: List[Dict[str, Any]], observation_id: Optional[str] = None,
        return_state: str = "compact") -> Dict[str, Any]:
    record = _recall(observation_id)
    if record is None or record.get("frame") != frame or record.get("page_url") != page_url:
        if observation_id:
            return unavailable("That observation is not of this Safari frame (or has expired); observe the frame again.",
                               reason="stale_observation")
        observed = observe(page_url, frame)
        if not observed.get("ok"):
            return observed
        record = _recall(observed["observation_id"]) or {}
    results: List[Dict[str, Any]] = []
    for action in actions:
        typ = str((action or {}).get("type") or "").strip().lower()
        element_id = str(action.get("element_id") or "")
        if typ == "click":
            result = _act_on(record, element_id, "press")
        elif typ in {"fill", "type"} and isinstance(action.get("values"), dict):
            result = {"ok": True, "type": typ, "fields": []}
            for label, value in action["values"].items():
                found = _find_label(record, str(label))
                field = (_set(record, found, value) if found else
                         {"ok": False, "error": "field_not_found", "label": label,
                          "available": [t["name"] for t in record["targets"].values() if t["name"]][:40]})
                result["fields"].append({"label": label, **field})
                if not field.get("ok"):
                    result["ok"] = False
                    break
        elif typ in {"fill", "type"}:
            result = _set(record, element_id, action.get("text", action.get("value", "")))
        elif typ == "select":
            option = action.get("option") if action.get("option") is not None else action.get("value")
            result = _act_on(record, element_id, "select_option", str(option or ""))
        elif typ in {"check", "uncheck"}:
            result = _set(record, element_id, typ == "check")
        else:
            result = {"ok": False, "error": "unsupported_in_accessibility_frame",
                      "message": f"In a Safari child frame browser_act can {', '.join(_SUPPORTED)}.", "supported": list(_SUPPORTED)}
        results.append({"type": typ, **result})
        if not result.get("ok"):
            break
    ok = bool(results) and all(r.get("ok") for r in results) and len(results) == len(actions)
    response: Dict[str, Any] = {"ok": ok, "transport": "accessibility", "results": results, "url": record.get("frame_url")}
    if return_state != "none":
        # Pages validate a moment after input (a submit button turns on); read the frame once that settled.
        time.sleep(_SETTLE_S if results else 0)
        state = observe(page_url, frame)
        response["state"] = state if not state.get("ok") else {
            key: state.get(key) for key in ("observation_id", "url", "element_count", "elements", "texts")
        }
    return response
