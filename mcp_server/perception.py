from __future__ import annotations

import json
import math
from typing import Any, Dict, Mapping, Optional

PERCEPTION_LADDER = (
    "snapshot",
    "semantic",
    "conditional",
    "targeted_visual",
    "ocr_full_visual",
)

DEFAULT_CONTEXT_BUDGET_BYTES = 64_000


def json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def estimate_payload_tokens(payload_bytes: int) -> int:
    # Deliberately labeled as an estimate: JSON tokenization varies by model.
    return max(0, int(math.ceil(max(0, int(payload_bytes or 0)) / 4.0)))


def finalize_perception_telemetry(
    payload: Dict[str, Any],
    *,
    stage: str,
    state_mode: str,
    node_count: int = 0,
    duration_ms: Optional[int] = None,
    visual_bytes: int = 0,
    visual_width: Optional[int] = None,
    visual_height: Optional[int] = None,
    ocr_used: bool = False,
    context_budget_bytes: int = DEFAULT_CONTEXT_BUDGET_BYTES,
    context_truncated: bool = False,
    expand_hint: Optional[str] = None,
) -> Dict[str, Any]:
    telemetry = payload.setdefault("telemetry", {})
    telemetry["perception_stage"] = str(stage)
    telemetry["state_mode"] = str(state_mode)
    telemetry["node_count"] = max(0, int(node_count or 0))
    telemetry["visual_bytes"] = max(0, int(visual_bytes or 0))
    telemetry["ocr_used"] = bool(ocr_used)
    if duration_ms is not None:
        telemetry["duration_ms"] = max(0, int(duration_ms))
    if visual_width is not None and visual_height is not None:
        telemetry["visual_dimensions"] = {
            "width": max(0, int(visual_width)),
            "height": max(0, int(visual_height)),
        }

    budget = max(4_096, min(int(context_budget_bytes or DEFAULT_CONTEXT_BUDGET_BYTES), 256_000))
    context = {
        "limit_bytes": budget,
        "truncated": bool(context_truncated),
    }
    if expand_hint:
        context["expand_hint"] = str(expand_hint)
    telemetry["context_budget"] = context

    refresh_perception_size(payload)
    return payload


def refresh_perception_size(payload: Dict[str, Any]) -> Dict[str, Any]:
    telemetry = payload.setdefault("telemetry", {})
    context = telemetry.get("context_budget")
    if not isinstance(context, dict):
        context = {"limit_bytes": DEFAULT_CONTEXT_BUDGET_BYTES, "truncated": False}
        telemetry["context_budget"] = context
    budget = max(
        4_096,
        min(int(context.get("limit_bytes") or DEFAULT_CONTEXT_BUDGET_BYTES), 256_000),
    )
    for _ in range(6):
        measured = json_bytes(payload)
        telemetry["payload_bytes"] = measured
        telemetry["payload_tokens_estimate"] = estimate_payload_tokens(measured)
        within_budget = measured <= budget
        context["within_budget"] = within_budget
        if within_budget:
            context.pop("budget_exceeded", None)
        else:
            context["budget_exceeded"] = True
            context.setdefault(
                "expand_hint",
                "Narrow the requested scope/sections or reduce element/tree limits before escalating to visual context.",
            )
        next_measured = json_bytes(payload)
        if next_measured == measured:
            break
    return payload


def safe_perception_metrics(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract only numeric/mode metadata; never copy observed text/content."""
    telemetry = payload.get("telemetry") if isinstance(payload, Mapping) else None
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    context = telemetry.get("context_budget")
    context = context if isinstance(context, Mapping) else {}
    return {
        "payload_bytes": int(telemetry.get("payload_bytes") or 0),
        "payload_tokens_estimate": int(telemetry.get("payload_tokens_estimate") or 0),
        "node_count": int(telemetry.get("node_count") or 0),
        "state_mode": str(telemetry.get("state_mode") or ""),
        "perception_stage": str(telemetry.get("perception_stage") or ""),
        "visual_bytes": int(telemetry.get("visual_bytes") or 0),
        "duration_ms": int(telemetry.get("duration_ms") or 0),
        "capture_duration_ms": int(telemetry.get("capture_duration_ms") or 0),
        "remote_js_calls": int(telemetry.get("remote_js_calls") or 0),
        "ax_traversals": int(telemetry.get("ax_traversals") or 0),
        "context_truncated": bool(context.get("truncated", False)),
        "context_within_budget": bool(context.get("within_budget", True)),
        "context_budget_exceeded": bool(context.get("budget_exceeded", False)),
    }
