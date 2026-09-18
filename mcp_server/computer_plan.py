from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

_ALLOWED_TOOLS = frozenset({
    "open_app",
    "mac_snapshot",
    "mac_observe",
    "mac_act",
    "browser_list_tabs",
    "browser_activate_tab",
    "browser_close_tab",
    "browser_observe",
    "browser_find",
    "browser_act",
    "browser_do",
})
_MAX_STEPS = 8
_MAX_ACTION_UNITS = 24
_MAX_SECONDS = 60.0
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")

NestedCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ComputerPlanError(ValueError):
    pass


def allowed_computer_plan_tools() -> tuple[str, ...]:
    return tuple(sorted(_ALLOWED_TOOLS))


def _action_units(tool: str, arguments: Mapping[str, Any]) -> int:
    if tool in {"mac_act", "browser_act", "browser_do"}:
        actions = arguments.get("actions")
        count = len(actions) if isinstance(actions, list) else 0
        if tool == "browser_do" and arguments.get("url"):
            count += 1
        return max(1, count)
    return 1


def _unwrap_tool_result(result: Any) -> Any:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1].get("result", result[1])
    if isinstance(result, dict):
        return result.get("result", result)
    if isinstance(result, (list, tuple)):
        converted: list[Any] = []
        for item in result:
            if hasattr(item, "model_dump"):
                converted.append(item.model_dump(mode="json"))
            elif isinstance(item, dict):
                converted.append(item)
            else:
                converted.append(str(item))
        if len(converted) == 1 and isinstance(converted[0], dict) and converted[0].get("type") == "text":
            text_value = converted[0].get("text")
            if isinstance(text_value, str):
                try:
                    return json.loads(text_value)
                except json.JSONDecodeError:
                    return converted
        return converted
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return result


def _path_get(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    text = str(path or "").strip()
    if not text:
        return True, current
    for token in text.split("."):
        if isinstance(current, Mapping):
            if token not in current:
                return False, None
            current = current[token]
            continue
        if isinstance(current, (list, tuple)):
            try:
                index = int(token)
            except ValueError:
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _resolve_ref(ref: str, outputs: Mapping[str, Any]) -> Any:
    head, dot, tail = str(ref or "").strip().partition(".")
    if not head or head not in outputs:
        raise ComputerPlanError(f"unknown or not-yet-completed step reference: {ref}")
    found, value = _path_get(outputs[head], tail if dot else "")
    if not found:
        raise ComputerPlanError(f"step reference path not found: {ref}")
    return value


def _resolve_refs(value: Any, outputs: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        if set(value.keys()) == {"$ref"}:
            return _resolve_ref(str(value.get("$ref") or ""), outputs)
        return {str(key): _resolve_refs(child, outputs) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(child, outputs) for child in value]
    if isinstance(value, tuple):
        return tuple(_resolve_refs(child, outputs) for child in value)
    return value


def _check_condition(condition: Mapping[str, Any], *, outputs: Mapping[str, Any], current: Any = None) -> tuple[bool, str]:
    if "ref" in condition:
        ref = str(condition.get("ref") or "").strip()
        try:
            value = _resolve_ref(ref, outputs)
            found = True
        except ComputerPlanError:
            value = None
            found = False
        label = ref
    else:
        path = str(condition.get("path") or "").strip()
        found, value = _path_get(current, path)
        label = path or "<result>"

    if "exists" in condition and bool(condition.get("exists")) != found:
        return False, f"condition exists mismatch for {label}"
    if not found:
        return False, f"condition path missing: {label}"
    if "equals" in condition and value != condition.get("equals"):
        return False, f"condition equality failed for {label}"
    if bool(condition.get("truthy", False)) and not bool(value):
        return False, f"condition truthy check failed for {label}"
    return True, "condition_met"


def _conditions_ok(
    conditions: Any, *, outputs: Mapping[str, Any], current: Any = None,
) -> tuple[bool, Optional[str]]:
    if conditions is None:
        return True, None
    if not isinstance(conditions, list):
        raise ComputerPlanError("preconditions/postconditions must be arrays")
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise ComputerPlanError("each condition must be an object")
        ok, reason = _check_condition(condition, outputs=outputs, current=current)
        if not ok:
            return False, reason
    return True, None


def _result_failed(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if payload.get("ok") is False or payload.get("denied") is True:
        return True
    actions = payload.get("actions")
    if isinstance(actions, list):
        return any(isinstance(item, Mapping) and item.get("ok") is False for item in actions)
    return False


def _summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"type": type(payload).__name__}
    keys = (
        "ok", "error", "reason_code", "denied", "observation_id", "previous_observation_id",
        "active_app", "app_handle", "window_handle", "tab_handle", "url", "title", "best_match",
        "state_mode", "post_state_ok",
    )
    summary = {key: payload.get(key) for key in keys if key in payload}
    actions = payload.get("actions")
    if isinstance(actions, list):
        summary["action_count"] = len(actions)
        summary["failed_action_count"] = sum(
            1 for item in actions if isinstance(item, Mapping) and item.get("ok") is False
        )
    return summary




def _walk_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, Mapping):
        if set(value.keys()) == {"$ref"}:
            refs.append(str(value.get("$ref") or "").strip())
        else:
            for child in value.values():
                refs.extend(_walk_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            refs.extend(_walk_refs(child))
    return refs


def _validate_conditions_shape(name: str, conditions: Any, *, prior_ids: set[str]) -> None:
    if conditions is None:
        return
    if not isinstance(conditions, list):
        raise ComputerPlanError(f"{name} must be an array")
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise ComputerPlanError(f"each {name} condition must be an object")
        has_ref = "ref" in condition
        has_path = "path" in condition
        if has_ref == has_path:
            raise ComputerPlanError(f"each {name} condition must contain exactly one of ref or path")
        if has_ref:
            ref = str(condition.get("ref") or "").strip()
            head = ref.partition(".")[0]
            if not head or head not in prior_ids:
                raise ComputerPlanError(f"{name} references unknown or future step: {ref}")
        if not any(key in condition for key in ("exists", "equals", "truthy")):
            raise ComputerPlanError(f"each {name} condition needs exists, equals, or truthy")


def _validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ComputerPlanError("steps must be a non-empty array")
    if len(steps) > _MAX_STEPS:
        raise ComputerPlanError(f"computer_plan supports at most {_MAX_STEPS} steps")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_units = 0
    for index, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise ComputerPlanError(f"step {index + 1} must be an object")
        step_id = str(raw.get("id") or f"step_{index + 1}").strip()
        if not _STEP_ID_RE.fullmatch(step_id):
            raise ComputerPlanError(f"invalid step id: {step_id}")
        if step_id in seen:
            raise ComputerPlanError(f"duplicate step id: {step_id}")
        seen.add(step_id)

        tool = str(raw.get("tool") or "").strip()
        if tool not in _ALLOWED_TOOLS:
            raise ComputerPlanError(
                f"tool '{tool}' is not allowed in computer_plan; allowed: {', '.join(sorted(_ALLOWED_TOOLS))}"
            )
        arguments = raw.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise ComputerPlanError(f"step {step_id} arguments must be an object")
        for ref in _walk_refs(arguments):
            head = ref.partition(".")[0]
            if not head or head not in seen - {step_id}:
                raise ComputerPlanError(f"step {step_id} references unknown or future step: {ref}")
        _validate_conditions_shape(
            "preconditions", raw.get("preconditions"), prior_ids=seen - {step_id},
        )
        _validate_conditions_shape(
            "postconditions", raw.get("postconditions"), prior_ids=seen,
        )
        total_units += _action_units(tool, arguments)
        if total_units > _MAX_ACTION_UNITS:
            raise ComputerPlanError(
                f"computer_plan exceeds the {_MAX_ACTION_UNITS}-unit action budget"
            )
        normalized.append({
            "id": step_id,
            "tool": tool,
            "arguments": dict(arguments),
            "preconditions": raw.get("preconditions"),
            "postconditions": raw.get("postconditions"),
        })
    return normalized


async def execute_computer_plan(
    call_tool: NestedCaller,
    *,
    steps: Sequence[Mapping[str, Any]] | list[dict[str, Any]],
    max_seconds: float = 45.0,
) -> dict[str, Any]:
    normalized = _validate_steps(list(steps))
    try:
        budget = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise ComputerPlanError("max_seconds must be numeric") from exc
    if budget <= 0 or budget > _MAX_SECONDS:
        raise ComputerPlanError(f"max_seconds must be > 0 and <= {_MAX_SECONDS:g}")

    started = time.monotonic()
    outputs: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    failure: Optional[dict[str, Any]] = None

    for index, step in enumerate(normalized):
        elapsed = time.monotonic() - started
        if elapsed >= budget:
            failure = {
                "reason_code": "PLAN_TIME_BUDGET_EXCEEDED",
                "error": f"computer_plan exceeded its {budget:g}s admission budget before step {step['id']}",
                "step_id": step["id"],
            }
            break

        pre_ok, pre_reason = _conditions_ok(step.get("preconditions"), outputs=outputs)
        if not pre_ok:
            failure = {
                "reason_code": "PRECONDITION_FAILED",
                "error": pre_reason,
                "step_id": step["id"],
            }
            records.append({
                "index": index,
                "id": step["id"],
                "tool": step["tool"],
                "ok": False,
                "status": "skipped",
                **failure,
            })
            break

        try:
            arguments = _resolve_refs(step["arguments"], outputs)
        except ComputerPlanError as exc:
            failure = {
                "reason_code": "REFERENCE_RESOLUTION_FAILED",
                "error": str(exc),
                "step_id": step["id"],
            }
            records.append({
                "index": index, "id": step["id"], "tool": step["tool"],
                "ok": False, "status": "skipped", **failure,
            })
            break

        step_started = time.monotonic()
        try:
            nested = await call_tool(step["tool"], arguments)
            payload = _unwrap_tool_result(nested)
        except Exception as exc:
            failure = {
                "reason_code": "STEP_CALL_FAILED",
                "error": str(exc),
                "step_id": step["id"],
            }
            records.append({
                "index": index,
                "id": step["id"],
                "tool": step["tool"],
                "ok": False,
                "status": "failed",
                "duration_ms": int((time.monotonic() - step_started) * 1000),
                **failure,
            })
            break

        outputs[step["id"]] = payload
        failed = _result_failed(payload)
        post_ok, post_reason = _conditions_ok(
            step.get("postconditions"), outputs=outputs, current=payload,
        )
        step_ok = not failed and post_ok
        record = {
            "index": index,
            "id": step["id"],
            "tool": step["tool"],
            "ok": step_ok,
            "status": "completed" if step_ok else "failed",
            "duration_ms": int((time.monotonic() - step_started) * 1000),
            "result": _summary(payload),
        }
        if failed:
            failure = {
                "reason_code": (
                    str(payload.get("reason_code") or "STEP_RESULT_FAILED")
                    if isinstance(payload, Mapping) else "STEP_RESULT_FAILED"
                ),
                "error": (
                    str(payload.get("error") or "nested tool returned a failed result")
                    if isinstance(payload, Mapping) else "nested tool returned a failed result"
                ),
                "step_id": step["id"],
            }
            record.update(failure)
        elif not post_ok:
            failure = {
                "reason_code": "POSTCONDITION_FAILED",
                "error": post_reason,
                "step_id": step["id"],
            }
            record.update(failure)
        records.append(record)
        if not step_ok:
            break

        if time.monotonic() - started > budget:
            failure = {
                "reason_code": "PLAN_TIME_BUDGET_EXCEEDED",
                "error": f"computer_plan exceeded its {budget:g}s admission budget after step {step['id']}",
                "step_id": step["id"],
            }
            break

    executed = sum(1 for item in records if item.get("status") != "skipped")
    requested = len(normalized)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    reduction = 0.0 if requested <= 1 else round((1.0 - (1.0 / requested)) * 100.0, 1)
    final_result = outputs.get(records[-1]["id"]) if records and records[-1].get("id") in outputs else None

    response: dict[str, Any] = {
        "ok": failure is None and executed == requested,
        "steps": records,
        "final_result": final_result,
        "plan_stats": {
            "steps_requested": requested,
            "steps_executed": executed,
            "model_tool_calls": 1,
            "standalone_equivalent_tool_calls": requested,
            "model_round_trip_reduction_percent": reduction,
            "duration_ms": elapsed_ms,
            "max_steps": _MAX_STEPS,
            "max_action_units": _MAX_ACTION_UNITS,
            "max_seconds": budget,
        },
    }
    if failure is not None:
        response.update(failure)
    return response
