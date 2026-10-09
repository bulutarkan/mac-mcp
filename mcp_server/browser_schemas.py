"""Typed input schemas for the browser tools.

Mode strings are advertised as enums and browser actions as one object per
action type, so a schema-guided client sees the allowed values and required
fields, and a malformed call is rejected by argument validation before the
tool runs. The schemas are inline (no $ref) for clients that do not resolve
references. Accepted aliases stay accepted: modes are matched case-insensitively
(scope also takes page/full/all), and an action may name its kind with
action/kind/op instead of type.
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, List, Literal, Mapping, Optional

from pydantic import AfterValidator, BeforeValidator, WithJsonSchema

from .tools_browser_agent import _ACT_TYPE_ALIASES, _ACT_TYPES, _MAX_ACTIONS

_TEXT_TYPES = ("type", "type_text", "paste")
_KEY_TYPES = ("key", "keyboard", "shortcut")


def _mode(default: str, aliases: Optional[Mapping[str, str]] = None) -> Callable[[Any], Any]:
    def normalize(value: Any) -> Any:
        if value is None:
            return default
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return (aliases or {}).get(key, key)
    return normalize


ObserveScope = Annotated[
    Literal["interactive", "visible", "content", "leaf"],
    BeforeValidator(_mode("interactive", {"page": "content", "full": "content", "all": "content"})),
]
VisualMode = Annotated[Literal["none", "viewport", "element", "full_page"], BeforeValidator(_mode("none"))]
ActReturnState = Annotated[Literal["none", "compact", "full"], BeforeValidator(_mode("compact"))]
DoReturnState = Annotated[Literal["none", "compact", "full"], BeforeValidator(_mode("none"))]


def action_type(action: Mapping[str, Any]) -> str:
    raw = action.get("type")
    if not raw:
        raw = next((action.get(key) for key in _ACT_TYPE_ALIASES if action.get(key)), None)
    return str(raw or "").strip().lower().replace("-", "_")


def validate_action(index: int, action: Any) -> Dict[str, Any]:
    if not isinstance(action, dict):
        raise ValueError(f"actions[{index}] must be an object")
    typ = action_type(action)
    if typ not in _ACT_TYPES:
        raise ValueError(f"actions[{index}] has unsupported type {action.get('type')!r}; use one of: {', '.join(_ACT_TYPES)}")
    if typ in _TEXT_TYPES and action.get("text") is None and not action.get("handoff_id"):
        raise ValueError(f"actions[{index}] ({typ}) needs text (or handoff_id from context_handoff)")
    if typ in _KEY_TYPES and not action.get("key"):
        raise ValueError(f"actions[{index}] ({typ}) needs key, e.g. 'Enter', 'Escape', 'a'")
    if typ == "dialog" and str(action.get("decision") or "").lower() not in {"accept", "dismiss"}:
        raise ValueError(f"actions[{index}] (dialog) needs decision='accept' or 'dismiss'")
    if typ == "drag" and not (action.get("to_element_id") or action.get("to_query") or action.get("to_role")
                              or action.get("dx") is not None or action.get("dy") is not None):
        raise ValueError(f"actions[{index}] (drag) needs to_element_id, to_query/to_role or dx/dy")
    return action


def _validate_actions(value: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not value:
        raise ValueError("actions must be a non-empty list")
    if len(value) > _MAX_ACTIONS:
        raise ValueError(f"actions may contain at most {_MAX_ACTIONS} items")
    return [validate_action(index, action) for index, action in enumerate(value)]


def _validate_optional_actions(value: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    return None if value is None else [validate_action(index, action) for index, action in enumerate(value)]


def _str(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


# One compact object schema: shared fields once, per-type required fields as
# if/then rules. A oneOf with a full variant per type was ~14 KB per tool.
ACTION_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(_ACT_TYPES)},
        "element_id": _str("From observe/find; or use query/role/text_match."),
        "query": _str("Target words, e.g. 'Continue'."),
        "role": _str("ARIA role, e.g. button, link, textbox, combobox."),
        "text_match": _str("Visible text the target contains."),
        "within": _str("Unique phrase of one item."),
        "within_element_id": {"type": "string"},
        "intent": _str("Only breaks ties."),
        "text": _str("type/type_text/paste: text to enter. wait for=text: text to wait for."),
        "handoff_id": _str("context_handoff id instead of text."),
        "option": _str("select: option label or value."),
        "key": _str("key/keyboard/shortcut: Enter, Escape, Tab, ArrowDown or one character."),
        "modifiers": {"type": "array", "items": {"type": "string"}},
        "input_mode": {"type": "string", "enum": ["auto", "dom", "trusted"]},
        "x": {"type": "number", "description": "Canvas: viewport px, with y."},
        "dx": {"type": "number"},
        "dy": {"type": "number"},
        "y": {"type": "number"},
        "for": _str("wait: selector (default), text, element_removed, url_change, network_idle, dom_stable, new_tab."),
        "url_contains": _str("wait for=new_tab: URL filter."),
        "selector": {"type": "string"},
        "timeout_s": {"type": "number", "minimum": 0},
        "wait_s": {"type": "number", "minimum": 0},
        "required": {"type": "boolean"},
        "fields": {"type": "array", "items": {"type": "string"}},
        "item_selector": {"type": "string"},
        "key_field": {"type": "string", "enum": ["text", "href"]},
        "max_items": {"type": "integer", "minimum": 1, "maximum": 500},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 50},
        "decision": {"type": "string", "enum": ["accept", "dismiss"]},
        "prompt_text": {"type": "string"},
        "to_element_id": {"type": "string"},
        "to_query": {"type": "string"},
        "to_role": {"type": "string"},
        "hold_ms": {"type": "integer", "minimum": 0, "maximum": 3000},
    },
    "required": ["type"],
    "additionalProperties": True,
    "allOf": [
        {"if": {"properties": {"type": {"enum": list(_TEXT_TYPES)}}},
         "then": {"anyOf": [{"required": ["text"]}, {"required": ["handoff_id"]}]}},
        {"if": {"properties": {"type": {"enum": list(_KEY_TYPES)}}}, "then": {"required": ["key"]}},
        {"if": {"properties": {"type": {"enum": ["dialog"]}}}, "then": {"required": ["decision"]}},
    ],
}

BrowserActions = Annotated[
    List[Dict[str, Any]],
    AfterValidator(_validate_actions),
    WithJsonSchema({"type": "array", "minItems": 1, "maxItems": _MAX_ACTIONS, "items": ACTION_ITEM_SCHEMA}),
]
OptionalBrowserActions = Annotated[
    Optional[List[Dict[str, Any]]],
    AfterValidator(_validate_optional_actions),
    # Same action objects as browser_act; only the type list is repeated to keep the catalog small.
    WithJsonSchema({"anyOf": [{"type": "array", "maxItems": _MAX_ACTIONS, "items": {
        "type": "object", "description": "Same action object as browser_act.",
        "properties": {"type": {"type": "string", "enum": list(_ACT_TYPES)}},
        "required": ["type"], "additionalProperties": True,
    }}, {"type": "null"}], "default": None}),
]
