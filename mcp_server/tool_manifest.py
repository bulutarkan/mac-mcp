"""A pure, inspectable manifest of the MCP tool catalog and its contract checks.

build_manifest() turns registered tool definitions into plain dicts (name,
compact and full description, inputs, enums, side-effect class, REST
exposure); contract_problems() checks semantic rules over that manifest and
the published REST schemas without starting a server or reading user state.
The checks state properties (lengths, enums, required fields, schema parity)
rather than snapshotting whole schemas, so a deliberate change to one tool
does not fail unrelated checks.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .chatgpt_client_gate import CHATGPT_PANEL_TOOLS
from .policy import RISK_REGISTRY, declared_risk
from .tool_summaries import COMPACT_DESCRIPTION_LIMIT, CORE_TOOL_NAMES, CORE_TOOL_SUMMARIES
from .workflow_checkpoints import risk_has_side_effect

# Transport-only fields the server adds to a tool's own inputs.
TRANSPORT_FIELDS = ("description", "idempotency_key")


def _side_effect(name: str) -> Optional[bool]:
    if name not in RISK_REGISTRY:
        return None
    risk = declared_risk(name)
    return risk_has_side_effect([capability.value for capability in risk.capabilities], risk.destructive)


def _enums(properties: Mapping[str, Any]) -> Dict[str, List[Any]]:
    found: Dict[str, List[Any]] = {}
    for name, spec in properties.items():
        for variant in [spec, *(spec.get("anyOf") or [])]:
            if isinstance(variant, dict) and isinstance(variant.get("enum"), list):
                found[name] = list(variant["enum"])
    return found


def compact_description(name: str, description: str) -> str:
    if len(description) <= COMPACT_DESCRIPTION_LIMIT:
        return description
    return CORE_TOOL_SUMMARIES.get(name, "")


def build_manifest(tools: Iterable[Any], *, v1_operations: Iterable[str] = (),
                   v2_operations: Iterable[str] = ()) -> List[Dict[str, Any]]:
    v1, v2 = set(v1_operations), set(v2_operations)
    manifest = []
    for tool in sorted(tools, key=lambda item: item.name):
        schema = dict(getattr(tool, "inputSchema", None) or {})
        properties = dict(schema.get("properties") or {})
        description = str(tool.description or "")
        manifest.append({
            "name": tool.name,
            "core": tool.name in CORE_TOOL_NAMES,
            "description": description,
            "compact_description": compact_description(tool.name, description) if tool.name in CORE_TOOL_NAMES else None,
            "inputs": sorted(name for name in properties if name not in TRANSPORT_FIELDS),
            "required": sorted(name for name in schema.get("required") or [] if name not in TRANSPORT_FIELDS),
            "enums": _enums(properties),
            "defaults": {name: spec.get("default") for name, spec in properties.items() if "default" in spec},
            "side_effect": _side_effect(tool.name),
            # ChatGPT panel tools keep the fixed schema the embedded UI expects.
            "panel_tool": tool.name in CHATGPT_PANEL_TOOLS,
            "advertises_idempotency_key": "idempotency_key" in properties,
            "rest_v1": tool.name in v1,
            "rest_v2": tool.name in v2,
        })
    return manifest


def _body_properties(operation: Mapping[str, Any]) -> Dict[str, Any]:
    body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    return dict(body.get("properties") or {}), list(body.get("required") or [])


def contract_problems(manifest: Sequence[Mapping[str, Any]], *, v2_document: Optional[Mapping[str, Any]] = None,
                      v1_operations: Iterable[str] = (), rest_only_operations: Iterable[str] = ()) -> List[str]:
    """Every broken catalog/schema rule as a readable line; empty when the contract holds."""
    problems: List[str] = []
    by_name = {entry["name"]: entry for entry in manifest}
    for entry in manifest:
        name = entry["name"]
        if entry["side_effect"] is None:
            problems.append(f"{name}: no central risk entry")
        if entry["core"]:
            compact = entry["compact_description"] or ""
            if not compact:
                problems.append(f"{name}: core tool longer than {COMPACT_DESCRIPTION_LIMIT} chars has no summary")
            elif len(compact) > COMPACT_DESCRIPTION_LIMIT or compact.rstrip().endswith("..."):
                problems.append(f"{name}: compact description is clipped or over the limit")
        for field in entry["required"]:
            if field not in entry["inputs"]:
                problems.append(f"{name}: required field {field!r} is not an input")
        for field, values in entry["enums"].items():
            if not values:
                problems.append(f"{name}: enum {field!r} is empty")
            default = entry["defaults"].get(field)
            if default is not None and default not in values:
                problems.append(f"{name}: default {default!r} of {field!r} is not one of its enum values")
        expects_key = bool(entry["side_effect"]) and not entry.get("panel_tool")
        if entry["side_effect"] is not None and entry["advertises_idempotency_key"] != expects_key:
            problems.append(f"{name}: idempotency_key advertised={entry['advertises_idempotency_key']} "
                            f"but side_effect={entry['side_effect']}")
    rest_only = set(rest_only_operations)
    for operation in v1_operations:
        if operation not in by_name and operation not in rest_only:
            problems.append(f"REST v1 {operation}: not a registered MCP tool")
    if v2_document is not None:
        for path, item in (v2_document.get("paths") or {}).items():
            operation = item.get("post") or {}
            name = operation.get("operationId")
            entry = by_name.get(name)
            if path != f"/api/v2/{name}":
                problems.append(f"REST v2 {path}: path does not match operationId {name!r}")
            if entry is None:
                problems.append(f"REST v2 {path}: {name!r} is not a registered MCP tool")
                continue
            properties, required = _body_properties(operation)
            if sorted(p for p in properties if p not in TRANSPORT_FIELDS) != entry["inputs"]:
                problems.append(f"REST v2 {name}: inputs differ from the MCP tool")
            if sorted(r for r in required if r not in TRANSPORT_FIELDS) != entry["required"]:
                problems.append(f"REST v2 {name}: required fields differ from the MCP tool")
            if ("idempotency_key" in properties) != bool(entry["side_effect"]):
                problems.append(f"REST v2 {name}: idempotency_key does not match the tool's side-effect class")
            for field, values in entry["enums"].items():
                published = _enums({field: properties.get(field) or {}}).get(field)
                if published != values:
                    problems.append(f"REST v2 {name}: enum {field!r} differs from the MCP tool")
    return problems
