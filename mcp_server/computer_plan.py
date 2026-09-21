from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Optional, Sequence

from .agent_admission import AdmissionError, release as admission_release, request_resource_lease

_ALLOWED_TOOLS = frozenset({
    "open_app",
    "mac_snapshot",
    "mac_observe",
    "mac_act",
    "mac_app",
    "browser_list_tabs",
    "browser_close_tab",
    "browser_observe",
    "browser_find",
    "browser_act",
    "browser_do",
})
_READ_ONLY_RECOVERY_TOOLS = frozenset({
    "mac_snapshot", "mac_observe", "browser_list_tabs", "browser_observe", "browser_find",
})
_MUTATING_TOOLS = frozenset({
    "open_app", "mac_act", "mac_app", "browser_close_tab", "browser_act", "browser_do",
})
_MAX_STEPS = 8
_MAX_EXPANDED_STEPS = 16
_MAX_ACTION_UNITS = 24
_MAX_SECONDS = 60.0
_MAX_RECOVERIES = 8
_MAX_RECOVERY_SECONDS = 20.0
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")

# These failures happen before a mutation is emitted by the current browser/native action layers.
_RECOVERABLE_PREMUTATION_CODES = frozenset({
    "STALE_OBSERVATION",
    "STALE_ELEMENT_PATH",
    "STALE_WINDOW_HANDLE",
    "STALE_APP_HANDLE",
    "WINDOW_HANDLE_UNKNOWN",
    "APP_HANDLE_UNKNOWN",
    "WINDOW_IDENTITY_UNAVAILABLE",
    "ELEMENT_DETACHED",
    "ELEMENT_NOT_READY",
    "ELEMENT_DISABLED",
    "ELEMENT_NOT_VISIBLE",
    "ELEMENT_HIDDEN",
    "ELEMENT_OFFSCREEN",
    "ELEMENT_ZERO_BOUNDS",
    "ELEMENT_OUTSIDE_WINDOW",
    "ELEMENT_OCCLUDED",
    "ELEMENT_BUSY",
    "ELEMENT_UNSTABLE",
    "ELEMENT_POINTER_EVENTS_NONE",
    "ELEMENT_HIT_TEST_FAILED",
    "ELEMENT_READONLY",
    "ELEMENT_NOT_EDITABLE",
    "ELEMENT_NOT_ACTIONABLE",
    "READINESS_TIMEOUT",
    "RENDER_NOT_READY",
    "ZERO_CONTENT_BOUNDS",
})
_RECOVERABLE_PREMUTATION_ERRORS = frozenset({
    "stale_observation", "stale_element", "element_not_ready", "element_not_available",
})
_TERMINAL_UNCERTAIN_CODES = frozenset({
    "ACTION_NO_EFFECT",
    "ACTION_VERIFICATION_UNAVAILABLE",
    "FOCUS_RESTORE_FAILED",
    "OUTCOME_UNKNOWN",
    "outcome_unknown",
    "client_cancelled_outcome_unknown",
})

NestedCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ComputerPlanError(ValueError):
    pass


@dataclass
class _Budget:
    max_seconds: float
    max_action_units: int
    max_recoveries: int
    max_recovery_seconds: float
    started: float = field(default_factory=time.monotonic)
    action_units: int = 0
    recoveries: int = 0
    recovery_seconds: float = 0.0
    nested_calls: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def check_time(self, step_id: str) -> None:
        if self.elapsed() >= self.max_seconds:
            raise _PlanStop(
                "PLAN_TIME_BUDGET_EXCEEDED",
                f"computer_plan exceeded its {self.max_seconds:g}s budget before step {step_id}",
                step_id,
            )

    def consume_call(self, tool: str, arguments: Mapping[str, Any]) -> None:
        units = _action_units(tool, arguments)
        if self.action_units + units > self.max_action_units:
            raise _PlanStop(
                "PLAN_ACTION_BUDGET_EXCEEDED",
                f"computer_plan exceeds its {self.max_action_units}-unit runtime action budget",
                None,
            )
        self.action_units += units
        self.nested_calls += 1

    def begin_recovery(self, step_id: Optional[str]) -> float:
        if self.recoveries >= self.max_recoveries:
            raise _PlanStop(
                "RECOVERY_BUDGET_EXCEEDED",
                f"computer_plan exhausted its {self.max_recoveries} recovery budget",
                step_id,
            )
        if self.recovery_seconds >= self.max_recovery_seconds:
            raise _PlanStop(
                "RECOVERY_TIME_BUDGET_EXCEEDED",
                f"computer_plan exhausted its {self.max_recovery_seconds:g}s recovery budget",
                step_id,
            )
        self.recoveries += 1
        return time.monotonic()

    def finish_recovery(self, started: float, step_id: Optional[str]) -> None:
        self.recovery_seconds += max(0.0, time.monotonic() - started)
        if self.recovery_seconds > self.max_recovery_seconds:
            raise _PlanStop(
                "RECOVERY_TIME_BUDGET_EXCEEDED",
                f"computer_plan exceeded its {self.max_recovery_seconds:g}s recovery budget",
                step_id,
            )


class _PlanStop(Exception):
    def __init__(self, reason_code: str, error: str, step_id: Optional[str], *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(error)
        self.reason_code = str(reason_code)
        self.error = str(error)
        self.step_id = step_id
        self.details = dict(details or {})


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
        # FastMCP can return one text content block or [text,image]. Parse the text
        # payload whenever it is JSON so recovery can inspect observation metadata.
        for item in converted:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    return json.loads(str(item["text"]))
                except json.JSONDecodeError:
                    continue
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list)):
                    return parsed
        return converted
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
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
    if "not_equals" in condition and value == condition.get("not_equals"):
        return False, f"condition inequality failed for {label}"
    if bool(condition.get("truthy", False)) and not bool(value):
        return False, f"condition truthy check failed for {label}"
    if bool(condition.get("falsy", False)) and bool(value):
        return False, f"condition falsy check failed for {label}"
    if "contains" in condition:
        needle = condition.get("contains")
        try:
            matched = needle in value
        except TypeError:
            matched = False
        if not matched:
            return False, f"condition contains check failed for {label}"
    return True, "condition_met"


def _conditions_ok(conditions: Any, *, outputs: Mapping[str, Any], current: Any = None) -> tuple[bool, Optional[str]]:
    if conditions is None:
        return True, None
    if isinstance(conditions, Mapping):
        conditions = [conditions]
    if not isinstance(conditions, list):
        raise ComputerPlanError("conditions must be an array or object")
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
    if payload.get("ok") is False or payload.get("denied") is True or payload.get("blocked") is True:
        return True
    actions = payload.get("actions")
    if isinstance(actions, list):
        return any(isinstance(item, Mapping) and item.get("ok") is False for item in actions)
    return False


def _summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"type": type(payload).__name__}
    keys = (
        "ok", "error", "reason_code", "denied", "blocked", "observation_id", "previous_observation_id",
        "active_app", "app_handle", "window_handle", "tab_handle", "url", "title", "best_match",
        "state_mode", "post_state_ok", "observe_again", "retryable", "automatic_retry", "outcome_unknown",
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


def _validate_conditions_shape(name: str, conditions: Any, *, prior_ids: set[str], allow_current: bool = False) -> None:
    if conditions is None:
        return
    if isinstance(conditions, Mapping):
        conditions = [conditions]
    if not isinstance(conditions, list):
        raise ComputerPlanError(f"{name} must be an array or object")
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
        if not any(key in condition for key in ("exists", "equals", "not_equals", "truthy", "falsy", "contains")):
            raise ComputerPlanError(f"each {name} condition needs an operator")


def _step_kind(raw: Mapping[str, Any]) -> str:
    kind = str(raw.get("type") or raw.get("kind") or "tool").strip().lower().replace("-", "_")
    return "tool" if kind in {"", "tool", "action"} else kind


def _validate_step_list(
    steps: Any,
    *,
    version: int,
    seen: set[str],
    prior: set[str],
    expanded_counter: list[int],
) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ComputerPlanError("steps must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise ComputerPlanError(f"step {index + 1} must be an object")
        expanded_counter[0] += 1
        if expanded_counter[0] > (_MAX_STEPS if version == 1 else _MAX_EXPANDED_STEPS):
            limit = _MAX_STEPS if version == 1 else _MAX_EXPANDED_STEPS
            raise ComputerPlanError(f"computer_plan supports at most {limit} expanded steps")
        step_id = str(raw.get("id") or f"step_{expanded_counter[0]}").strip()
        if not _STEP_ID_RE.fullmatch(step_id):
            raise ComputerPlanError(f"invalid step id: {step_id}")
        if step_id in seen:
            raise ComputerPlanError(f"duplicate step id: {step_id}")
        seen.add(step_id)
        kind = _step_kind(raw)
        if version == 1 and kind != "tool":
            raise ComputerPlanError("wait_until/branch steps require plan_version=2")

        if kind == "branch":
            conditions = raw.get("conditions") if raw.get("conditions") is not None else raw.get("condition")
            _validate_conditions_shape("branch conditions", conditions, prior_ids=set(prior))
            then_steps = raw.get("then") or raw.get("then_steps")
            else_steps = raw.get("else") or raw.get("else_steps") or []
            if not isinstance(then_steps, list) or not then_steps:
                raise ComputerPlanError(f"branch step {step_id} requires non-empty then steps")
            if not isinstance(else_steps, list):
                raise ComputerPlanError(f"branch step {step_id} else must be an array")
            # Validate each branch against the same prior outputs. IDs remain globally unique.
            then_normalized = _validate_step_list(
                then_steps, version=version, seen=seen, prior=set(prior) | {step_id}, expanded_counter=expanded_counter,
            )
            else_normalized = []
            if else_steps:
                else_normalized = _validate_step_list(
                    else_steps, version=version, seen=seen, prior=set(prior) | {step_id}, expanded_counter=expanded_counter,
                )
            normalized.append({
                "id": step_id, "kind": "branch", "conditions": conditions,
                "then": then_normalized, "else": else_normalized,
            })
            # Branch child outputs are intentionally branch-local. A later top-level
            # step may reference the branch decision itself but not an output that only
            # exists on one conditional path.
            prior.add(step_id)
            continue

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
            if not head or head not in prior:
                raise ComputerPlanError(f"step {step_id} references unknown or future step: {ref}")
        _validate_conditions_shape("preconditions", raw.get("preconditions"), prior_ids=set(prior))
        _validate_conditions_shape("postconditions", raw.get("postconditions"), prior_ids=set(prior) | {step_id})

        if kind == "wait_until":
            if tool not in _READ_ONLY_RECOVERY_TOOLS:
                raise ComputerPlanError(f"wait_until step {step_id} must use a read-only observe/find tool")
            until = raw.get("until") or raw.get("postconditions")
            target = raw.get("target")
            if until is None and not isinstance(target, Mapping):
                raise ComputerPlanError(f"wait_until step {step_id} requires until conditions or target")
            _validate_conditions_shape("wait_until conditions", until, prior_ids=set(prior) | {step_id})
            timeout_s = float(raw.get("timeout_s", 5.0))
            poll_ms = int(raw.get("poll_ms", 150))
            if timeout_s <= 0 or timeout_s > 20:
                raise ComputerPlanError("wait_until timeout_s must be > 0 and <= 20")
            if poll_ms < 20 or poll_ms > 2000:
                raise ComputerPlanError("wait_until poll_ms must be between 20 and 2000")
            normalized.append({
                "id": step_id, "kind": "wait_until", "tool": tool, "arguments": dict(arguments),
                "until": until, "target": dict(target) if isinstance(target, Mapping) else None,
                "timeout_s": timeout_s, "poll_ms": poll_ms,
                "preconditions": raw.get("preconditions"), "postconditions": raw.get("postconditions"),
            })
            prior.add(step_id)
            continue

        retry_raw = raw.get("retry")
        retry: dict[str, Any] = {}
        if isinstance(retry_raw, int):
            retry = {"max_attempts": max(1, min(int(retry_raw), 4))}
        elif isinstance(retry_raw, Mapping):
            retry = dict(retry_raw)
        elif retry_raw not in (None, False):
            raise ComputerPlanError(f"step {step_id} retry must be an integer or object")
        if retry:
            max_attempts = int(retry.get("max_attempts", 2))
            if max_attempts < 1 or max_attempts > 4:
                raise ComputerPlanError("retry.max_attempts must be between 1 and 4")
            retry["max_attempts"] = max_attempts
            retry["delay_ms"] = max(0, min(int(retry.get("delay_ms", 80)), 1000))

        fallback = raw.get("fallback")
        fallback_normalized = None
        if fallback is not None:
            if version < 2:
                raise ComputerPlanError("fallback requires plan_version=2")
            if not isinstance(fallback, Mapping):
                raise ComputerPlanError(f"step {step_id} fallback must be an object")
            fallback_id = str(fallback.get("id") or f"{step_id}_fallback")
            fb_raw = dict(fallback)
            fb_raw["id"] = fallback_id
            fb_list = _validate_step_list(
                [fb_raw], version=version, seen=seen, prior=set(prior), expanded_counter=expanded_counter,
            )
            fallback_normalized = fb_list[0]

        normalized.append({
            "id": step_id,
            "kind": "tool",
            "tool": tool,
            "arguments": dict(arguments),
            "preconditions": raw.get("preconditions"),
            "postconditions": raw.get("postconditions"),
            "retry": retry,
            "fallback": fallback_normalized,
            "recovery": dict(raw.get("recovery") or {}) if isinstance(raw.get("recovery"), Mapping) else {},
        })
        prior.add(step_id)
    return normalized


def _all_ids(steps: Sequence[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for step in steps:
        sid = str(step.get("id") or "")
        if sid:
            result.add(sid)
        if step.get("kind") == "branch":
            result.update(_all_ids(step.get("then") or []))
            result.update(_all_ids(step.get("else") or []))
        fallback = step.get("fallback")
        if isinstance(fallback, Mapping):
            result.add(str(fallback.get("id") or ""))
    return result


def _validate_steps(steps: Any, *, version: int = 1) -> list[dict[str, Any]]:
    if version not in {1, 2}:
        raise ComputerPlanError("plan_version must be 1 or 2")
    normalized = _validate_step_list(
        steps, version=version, seen=set(), prior=set(), expanded_counter=[0],
    )
    # v1 keeps the historical admission-time unit budget. v2 charges actual runtime
    # calls so bounded retries/waits/fallbacks cannot escape the budget.
    if version == 1:
        total_units = 0
        for step in normalized:
            if step.get("kind") == "tool":
                total_units += _action_units(str(step["tool"]), step.get("arguments") or {})
        if total_units > _MAX_ACTION_UNITS:
            raise ComputerPlanError(f"computer_plan exceeds the {_MAX_ACTION_UNITS}-unit action budget")
    return normalized


def _payload_reason(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        return "STEP_RESULT_FAILED", "nested tool returned a failed result"
    reason = str(payload.get("reason_code") or payload.get("error") or "STEP_RESULT_FAILED")
    error = str(payload.get("error") or reason or "nested tool returned a failed result")
    actions = payload.get("actions")
    if isinstance(actions, list):
        failed = next((item for item in actions if isinstance(item, Mapping) and item.get("ok") is False), None)
        if failed:
            reason = str(failed.get("reason_code") or failed.get("error") or reason)
            error = str(failed.get("error") or reason)
    return reason, error


def _has_prior_successful_mutation(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return False
    for item in actions:
        if not isinstance(item, Mapping):
            continue
        if item.get("ok") is False:
            return False
        if item.get("ok") is True:
            return True
    return False


def _uncertain_or_terminal(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if payload.get("denied") is True or payload.get("blocked") is True:
        return True
    if payload.get("outcome_unknown") is True:
        return True
    if payload.get("automatic_retry") is False:
        return True
    reason, _ = _payload_reason(payload)
    if reason in _TERMINAL_UNCERTAIN_CODES or reason.lower() in {str(x).lower() for x in _TERMINAL_UNCERTAIN_CODES}:
        return True
    return False


def _safe_recovery_candidate(tool: str, payload: Any) -> bool:
    if _uncertain_or_terminal(payload):
        return False
    if tool in _READ_ONLY_RECOVERY_TOOLS:
        return isinstance(payload, Mapping) and bool(payload.get("retryable") or payload.get("observe_again"))
    if tool not in _MUTATING_TOOLS:
        return False
    if _has_prior_successful_mutation(payload):
        return False
    reason, _ = _payload_reason(payload)
    error = str(payload.get("error") or "").lower() if isinstance(payload, Mapping) else ""
    return (
        reason in _RECOVERABLE_PREMUTATION_CODES
        or reason.upper() in _RECOVERABLE_PREMUTATION_CODES
        or error in _RECOVERABLE_PREMUTATION_ERRORS
    )


def _observation_by_id(outputs: Mapping[str, Any], observation_id: Any) -> Optional[Mapping[str, Any]]:
    wanted = str(observation_id or "")
    if not wanted:
        return None
    for value in outputs.values():
        if isinstance(value, Mapping) and str(value.get("observation_id") or "") == wanted:
            return value
    return None


def _text_token(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _semantic_target_from_browser_node(node: Mapping[str, Any]) -> dict[str, Any]:
    query = (
        str(node.get("aria_label") or "").strip()
        or str(node.get("text") or "").strip()
        or str(node.get("placeholder") or "").strip()
        or str(node.get("name") or "").strip()
        or str(node.get("title") or "").strip()
        or str(node.get("value") or "").strip()
    )
    return {
        "query": query,
        "role": str(node.get("role") or "").strip() or None,
        "text": str(node.get("text") or "").strip() or None,
    }


def _browser_old_node(arguments: Mapping[str, Any], outputs: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    obs = _observation_by_id(outputs, arguments.get("observation_id"))
    actions = arguments.get("actions")
    if not isinstance(obs, Mapping) or not isinstance(actions, list) or not actions:
        return None
    action = next((item for item in actions if isinstance(item, Mapping) and item.get("element_id")), None)
    if not action:
        return None
    element_id = str(action.get("element_id") or "")
    for node in obs.get("elements") or []:
        if isinstance(node, Mapping) and str(node.get("element_id") or "") == element_id:
            return node
    return None


def _native_fingerprint(node: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]] | None = None) -> dict[str, str]:
    parent: Optional[Mapping[str, Any]] = None
    parent_id = str(node.get("parent_id") or "")
    if parent_id and nodes:
        parent = next((item for item in nodes if str(item.get("element_id") or "") == parent_id), None)
    return {
        "identifier": _text_token(node.get("identifier")),
        "role": _text_token(node.get("role")),
        "subrole": _text_token(node.get("subrole")),
        "title": _text_token(node.get("title")),
        "description": _text_token(node.get("description")),
        "value": _text_token(node.get("value"))[:160],
        "parent_role": _text_token((parent or {}).get("role")),
        "parent_title": _text_token((parent or {}).get("title")),
    }


def _native_semantic_score(fingerprint: Mapping[str, str], node: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> float:
    candidate = _native_fingerprint(node, nodes)
    identifier = fingerprint.get("identifier") or ""
    if identifier:
        return 1.0 if candidate.get("identifier") == identifier else 0.0
    required_role = fingerprint.get("role") or ""
    if required_role and candidate.get("role") != required_role:
        return 0.0
    score = 0.25 if required_role else 0.0
    weights = {
        "subrole": 0.10,
        "title": 0.28,
        "description": 0.16,
        "value": 0.10,
        "parent_role": 0.05,
        "parent_title": 0.06,
    }
    evidence = 0
    for key, weight in weights.items():
        expected = fingerprint.get(key) or ""
        if not expected:
            continue
        evidence += 1
        actual = candidate.get(key) or ""
        if actual == expected:
            score += weight
    if evidence == 0 and required_role:
        return 0.0
    return min(1.0, score)


def _native_old_node(arguments: Mapping[str, Any], outputs: Mapping[str, Any]) -> tuple[Optional[Mapping[str, Any]], list[Mapping[str, Any]]]:
    obs = _observation_by_id(outputs, arguments.get("observation_id"))
    actions = arguments.get("actions")
    if not isinstance(obs, Mapping) or not isinstance(actions, list) or not actions:
        return None, []
    action = next((item for item in actions if isinstance(item, Mapping) and item.get("element_id")), None)
    if not action:
        return None, []
    nodes = [item for item in (obs.get("nodes") or []) if isinstance(item, Mapping)]
    element_id = str(action.get("element_id") or "")
    return next((node for node in nodes if str(node.get("element_id") or "") == element_id), None), nodes


def _unique_browser_match(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    matches = [item for item in (payload.get("matches") or []) if isinstance(item, Mapping)]
    if not matches:
        raise _PlanStop("RECOVERY_TARGET_NOT_FOUND", "fresh browser scan could not find the stale target", None)
    best = matches[0]
    score = float(best.get("confidence") or 0.0)
    if score < 0.60:
        raise _PlanStop("RECOVERY_TARGET_NOT_FOUND", "fresh browser target confidence was too low", None)
    if len(matches) > 1:
        second = float(matches[1].get("confidence") or 0.0)
        if second >= 0.60 and score - second < 0.10:
            raise _PlanStop(
                "RECOVERY_AMBIGUOUS_TARGET",
                "fresh browser scan found multiple equally plausible targets; refusing to guess",
                None,
                details={"candidate_count": len(matches), "best_confidence": score, "second_confidence": second},
            )
    return best


def _unique_native_match(fingerprint: Mapping[str, str], payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = [item for item in (payload.get("nodes") or []) if isinstance(item, Mapping)]
    scored = sorted(
        ((_native_semantic_score(fingerprint, node, nodes), node) for node in nodes),
        key=lambda item: item[0], reverse=True,
    )
    scored = [item for item in scored if item[0] >= (0.99 if fingerprint.get("identifier") else 0.65)]
    if not scored:
        raise _PlanStop("RECOVERY_TARGET_NOT_FOUND", "fresh native observation could not uniquely rebind the target", None)
    best_score, best = scored[0]
    if len(scored) > 1:
        second_score = scored[1][0]
        if fingerprint.get("identifier") or best_score - second_score < 0.12:
            raise _PlanStop(
                "RECOVERY_AMBIGUOUS_TARGET",
                "fresh native observation contains ambiguous semantic target identity; refusing to guess",
                None,
                details={"candidate_count": len(scored), "best_score": round(best_score, 3), "second_score": round(second_score, 3)},
            )
    return best


async def _nested_call(
    call_tool: NestedCaller,
    tool: str,
    arguments: dict[str, Any],
    budget: _Budget,
) -> Any:
    budget.check_time(tool)
    budget.consume_call(tool, arguments)
    result = await call_tool(tool, arguments)
    return _unwrap_tool_result(result)


async def _recover_rebind(
    call_tool: NestedCaller,
    *,
    tool: str,
    arguments: dict[str, Any],
    outputs: Mapping[str, Any],
    budget: _Budget,
    recovery_config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if tool in {"browser_act", "browser_do"}:
        old_node = _browser_old_node(arguments, outputs)
        actions = arguments.get("actions")
        action = next((item for item in actions or [] if isinstance(item, Mapping) and item.get("element_id")), None)
        semantic: dict[str, Any] = {}
        if isinstance(action, Mapping):
            semantic_query = (
                action.get("query") or action.get("target") or action.get("text_match") or action.get("target_text")
            )
            if semantic_query:
                semantic["query"] = str(semantic_query)
                semantic["role"] = action.get("role")
                semantic["text"] = action.get("text_match") or action.get("target_text")
        if not semantic.get("query") and old_node is not None:
            semantic.update(_semantic_target_from_browser_node(old_node))
        if not semantic.get("query"):
            raise _PlanStop("RECOVERY_REBIND_UNAVAILABLE", "browser stale target has no semantic identity to rebind", None)
        find_args = {
            "browser": arguments.get("browser", "Safari"),
            "query": semantic.get("query"),
            "actionable_only": True,
            "max_results": 5,
        }
        for key in ("window_index", "tab_index", "tab_handle"):
            if arguments.get(key) is not None:
                find_args[key] = arguments.get(key)
        if semantic.get("role"):
            find_args["role"] = semantic.get("role")
        if semantic.get("text"):
            find_args["text"] = semantic.get("text")
        fresh = await _nested_call(call_tool, "browser_find", find_args, budget)
        if _result_failed(fresh) or not isinstance(fresh, Mapping):
            raise _PlanStop("RECOVERY_OBSERVE_FAILED", "fresh browser semantic scan failed", None)
        best = _unique_browser_match(fresh)
        rebound = json.loads(json.dumps(arguments))
        rebound["observation_id"] = fresh.get("observation_id")
        old_id = str((action or {}).get("element_id") or "")
        for item in rebound.get("actions") or []:
            if isinstance(item, MutableMapping) and str(item.get("element_id") or "") == old_id:
                item["element_id"] = best.get("element_id")
        return rebound, {
            "kind": "browser_semantic_rebind",
            "old_element_id": old_id,
            "new_element_id": best.get("element_id"),
            "observation_id": fresh.get("observation_id"),
            "confidence": best.get("confidence"),
        }

    if tool == "mac_act":
        old_node, old_nodes = _native_old_node(arguments, outputs)
        old_observation = _observation_by_id(outputs, arguments.get("observation_id"))
        if old_node is None:
            raise _PlanStop("RECOVERY_REBIND_UNAVAILABLE", "native stale target has no prior semantic node identity", None)
        fingerprint = _native_fingerprint(old_node, old_nodes)
        observe_args: dict[str, Any] = {
            "include_screenshot": False,
            "ocr": False,
        }
        app = arguments.get("app") or ((old_observation or {}).get("active_app") if isinstance(old_observation, Mapping) else None)
        window_index = arguments.get("window_index") or ((old_observation or {}).get("window_index") if isinstance(old_observation, Mapping) else None)
        if app is not None:
            observe_args["app"] = app
        if window_index is not None:
            observe_args["window_index"] = window_index
        fresh = await _nested_call(call_tool, "mac_observe", observe_args, budget)
        if _result_failed(fresh) or not isinstance(fresh, Mapping):
            raise _PlanStop("RECOVERY_OBSERVE_FAILED", "fresh native observation failed", None)
        best = _unique_native_match(fingerprint, fresh)
        rebound = json.loads(json.dumps(arguments))
        rebound["observation_id"] = fresh.get("observation_id")
        if fresh.get("app_handle"):
            rebound["app_handle"] = fresh.get("app_handle")
        if fresh.get("window_handle"):
            rebound["window_handle"] = fresh.get("window_handle")
        actions = rebound.get("actions") or []
        old_id = str(next((item.get("element_id") for item in actions if isinstance(item, Mapping) and item.get("element_id")), ""))
        for item in actions:
            if isinstance(item, MutableMapping) and str(item.get("element_id") or "") == old_id:
                item["element_id"] = best.get("element_id")
        return rebound, {
            "kind": "native_semantic_rebind",
            "old_element_id": old_id,
            "new_element_id": best.get("element_id"),
            "observation_id": fresh.get("observation_id"),
            "identifier": best.get("identifier") or None,
        }

    # Read-only/precondition failures can be retried without target rebinding.
    return dict(arguments), {"kind": "bounded_retry"}


def _semantic_target_match(target: Mapping[str, Any], payload: Any) -> tuple[bool, Optional[dict[str, Any]], str]:
    if not isinstance(payload, Mapping):
        return False, None, "payload_not_object"
    nodes_key = "nodes" if isinstance(payload.get("nodes"), list) else "elements"
    nodes = [item for item in (payload.get(nodes_key) or []) if isinstance(item, Mapping)]
    if not nodes:
        best = payload.get("best_match")
        if isinstance(best, Mapping):
            nodes = [best]
    identifier = _text_token(target.get("identifier"))
    role = _text_token(target.get("role"))
    title = _text_token(target.get("title") or target.get("text") or target.get("aria_label"))
    description = _text_token(target.get("description") or target.get("placeholder"))
    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for node in nodes:
        if identifier:
            score = 1.0 if _text_token(node.get("identifier")) == identifier else 0.0
        else:
            node_role = _text_token(node.get("role"))
            if role and node_role != role:
                continue
            score = 0.30 if role else 0.0
            texts = {
                _text_token(node.get("title")), _text_token(node.get("text")), _text_token(node.get("aria_label")),
                _text_token(node.get("description")), _text_token(node.get("placeholder")), _text_token(node.get("value")),
            }
            if title and title in texts:
                score += 0.50
            if description and description in texts:
                score += 0.20
        if score >= (0.99 if identifier else 0.65):
            candidates.append((score, node))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return False, None, "target_not_found"
    if len(candidates) > 1:
        if identifier or candidates[0][0] - candidates[1][0] < 0.12:
            return False, None, "target_ambiguous"
    return True, dict(candidates[0][1]), "target_found"


async def _execute_wait_until(
    call_tool: NestedCaller,
    *,
    step: Mapping[str, Any],
    outputs: MutableMapping[str, Any],
    budget: _Budget,
) -> tuple[Any, dict[str, Any]]:
    deadline = min(budget.started + budget.max_seconds, time.monotonic() + float(step.get("timeout_s") or 5.0))
    poll_s = max(0.02, min(float(step.get("poll_ms") or 150) / 1000.0, 2.0))
    attempts = 0
    last_reason = "wait_condition_not_met"
    last_payload: Any = None
    while time.monotonic() < deadline:
        budget.check_time(str(step.get("id")))
        arguments = _resolve_refs(step.get("arguments") or {}, outputs)
        try:
            payload = await _nested_call(call_tool, str(step["tool"]), arguments, budget)
        except _PlanStop:
            raise
        except Exception as exc:
            raise _PlanStop(
                "WAIT_UNTIL_OBSERVE_FAILED",
                f"wait_until observation failed: {exc}",
                str(step.get("id")),
                details={"attempts": attempts},
            ) from exc
        attempts += 1
        last_payload = payload
        if not _result_failed(payload):
            cond_ok, cond_reason = _conditions_ok(step.get("until"), outputs=outputs, current=payload)
            target = step.get("target")
            target_ok, best_match, target_reason = (True, None, "no_target")
            if isinstance(target, Mapping):
                target_ok, best_match, target_reason = _semantic_target_match(target, payload)
            if cond_ok and target_ok:
                if isinstance(payload, Mapping) and best_match is not None:
                    payload = dict(payload)
                    payload["best_match"] = best_match
                return payload, {"attempts": attempts, "status": "condition_met"}
            last_reason = cond_reason or target_reason
        else:
            reason, _ = _payload_reason(payload)
            last_reason = reason
            if _uncertain_or_terminal(payload):
                raise _PlanStop(reason, "wait_until nested read failed terminally", str(step.get("id")))
        await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
    raise _PlanStop(
        "WAIT_UNTIL_TIMEOUT",
        f"wait_until timed out after {float(step.get('timeout_s') or 5.0):g}s: {last_reason}",
        str(step.get("id")),
        details={"attempts": attempts, "last_result": _summary(last_payload)},
    )


async def _execute_tool_step(
    call_tool: NestedCaller,
    *,
    step: Mapping[str, Any],
    outputs: MutableMapping[str, Any],
    budget: _Budget,
    version: int,
) -> tuple[Any, dict[str, Any]]:
    step_id = str(step["id"])
    tool = str(step["tool"])
    arguments = _resolve_refs(step.get("arguments") or {}, outputs)
    retry_cfg = dict(step.get("retry") or {})
    max_attempts = int(retry_cfg.get("max_attempts") or (2 if version >= 2 else 1))
    if version < 2:
        max_attempts = 1
    delay_s = max(0.0, min(float(retry_cfg.get("delay_ms") or 80) / 1000.0, 1.0))
    recovery_cfg = dict(step.get("recovery") or {})
    allow_rebind = recovery_cfg.get("rebind", True) is not False
    attempts: list[dict[str, Any]] = []
    current_args = dict(arguments)

    for attempt in range(1, max_attempts + 1):
        budget.check_time(step_id)
        started = time.monotonic()
        try:
            payload = await _nested_call(call_tool, tool, current_args, budget)
        except _PlanStop:
            raise
        except Exception as exc:
            # An exception crossing a mutating nested tool boundary is ambiguous; never replay it.
            raise _PlanStop(
                "STEP_CALL_FAILED",
                str(exc),
                step_id,
                details={"attempt": attempt, "automatic_retry": False, "outcome_unknown": tool in _MUTATING_TOOLS},
            ) from exc
        failed = _result_failed(payload)
        post_ok, post_reason = _conditions_ok(step.get("postconditions"), outputs={**outputs, step_id: payload}, current=payload)
        attempt_record = {
            "attempt": attempt,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "result": _summary(payload),
        }
        attempts.append(attempt_record)
        if not failed and post_ok:
            return payload, {"attempts": attempts, "recovered": attempt > 1}

        reason, error = _payload_reason(payload) if failed else ("POSTCONDITION_FAILED", str(post_reason))
        attempt_record["reason_code"] = reason
        attempt_record["error"] = error
        if version < 2 or attempt >= max_attempts:
            break
        if not _safe_recovery_candidate(tool, payload):
            break
        recovery_started = budget.begin_recovery(step_id)
        try:
            try:
                if allow_rebind:
                    current_args, recovery_meta = await _recover_rebind(
                        call_tool,
                        tool=tool,
                        arguments=current_args,
                        outputs=outputs,
                        budget=budget,
                        recovery_config=recovery_cfg,
                    )
                else:
                    recovery_meta = {"kind": "bounded_retry_without_rebind"}
            except _PlanStop as stop:
                if stop.step_id is None:
                    stop.step_id = step_id
                raise
            except Exception as exc:
                raise _PlanStop(
                    "RECOVERY_OBSERVE_FAILED",
                    f"closed-loop recovery observation failed: {exc}",
                    step_id,
                ) from exc
            if delay_s:
                await asyncio.sleep(min(delay_s, budget.remaining()))
        finally:
            budget.finish_recovery(recovery_started, step_id)
        attempt_record["recovery"] = recovery_meta

    fallback = step.get("fallback")
    last_payload = payload if 'payload' in locals() else None
    last_reason, last_error = _payload_reason(last_payload) if last_payload is not None else ("STEP_RESULT_FAILED", "step failed")
    if isinstance(fallback, Mapping) and version >= 2 and _safe_recovery_candidate(tool, last_payload):
        fallback_started = budget.begin_recovery(step_id)
        try:
            fb_payload, fb_meta = await _execute_tool_step(
                call_tool, step=fallback, outputs=outputs, budget=budget, version=version,
            )
        finally:
            budget.finish_recovery(fallback_started, step_id)
        return fb_payload, {
            "attempts": attempts,
            "recovered": True,
            "fallback_used": True,
            "fallback_step_id": fallback.get("id"),
            "fallback": fb_meta,
        }
    raise _PlanStop(last_reason, last_error, step_id, details={"attempts": attempts})


async def _execute_steps(
    call_tool: NestedCaller,
    *,
    steps: Sequence[Mapping[str, Any]],
    outputs: MutableMapping[str, Any],
    records: list[dict[str, Any]],
    budget: _Budget,
    version: int,
) -> None:
    for step in steps:
        step_id = str(step["id"])
        budget.check_time(step_id)
        kind = str(step.get("kind") or "tool")
        index = len(records)
        pre_ok, pre_reason = _conditions_ok(step.get("preconditions"), outputs=outputs)
        if not pre_ok:
            raise _PlanStop("PRECONDITION_FAILED", str(pre_reason), step_id)
        started = time.monotonic()
        if kind == "branch":
            selected_ok, reason = _conditions_ok(step.get("conditions"), outputs=outputs)
            selected = "then" if selected_ok else "else"
            payload = {"ok": True, "branch": selected, "condition_reason": reason}
            outputs[step_id] = payload
            records.append({
                "index": index, "id": step_id, "type": "branch", "tool": None,
                "ok": True, "status": "completed", "branch": selected,
                "duration_ms": int((time.monotonic() - started) * 1000), "result": payload,
            })
            selected_steps = step.get(selected) or []
            if selected_steps:
                await _execute_steps(
                    call_tool, steps=selected_steps, outputs=outputs, records=records, budget=budget, version=version,
                )
            continue

        if kind == "wait_until":
            try:
                payload, meta = await _execute_wait_until(
                    call_tool, step=step, outputs=outputs, budget=budget,
                )
            except _PlanStop as stop:
                records.append({
                    "index": index, "id": step_id, "type": "wait_until", "tool": step.get("tool"),
                    "ok": False, "status": "failed", "duration_ms": int((time.monotonic() - started) * 1000),
                    "reason_code": stop.reason_code, "error": stop.error, **stop.details,
                })
                raise
            outputs[step_id] = payload
            records.append({
                "index": index, "id": step_id, "type": "wait_until", "tool": step.get("tool"),
                "ok": True, "status": "completed", "duration_ms": int((time.monotonic() - started) * 1000),
                "result": _summary(payload), **meta,
            })
            continue

        try:
            payload, meta = await _execute_tool_step(
                call_tool, step=step, outputs=outputs, budget=budget, version=version,
            )
        except _PlanStop as stop:
            records.append({
                "index": index, "id": step_id, "type": "tool", "tool": step.get("tool"),
                "ok": False, "status": "failed", "duration_ms": int((time.monotonic() - started) * 1000),
                "reason_code": stop.reason_code, "error": stop.error, **stop.details,
            })
            raise
        outputs[step_id] = payload
        records.append({
            "index": index, "id": step_id, "type": "tool", "tool": step.get("tool"),
            "ok": True, "status": "completed", "duration_ms": int((time.monotonic() - started) * 1000),
            "result": _summary(payload), **meta,
        })


def derive_computer_plan_resources(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Derive conservative static resource claims before any plan step executes."""
    claims: list[dict[str, str]] = []

    def add(kind: str, identifier: Any, mode: str) -> None:
        if not isinstance(identifier, str) or not identifier.strip() or "$ref" in identifier:
            return
        item = {"kind": kind, "id": identifier.strip(), "mode": mode}
        if item not in claims:
            claims.append(item)

    def walk(items: Sequence[Mapping[str, Any]]) -> None:
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            kind = _step_kind(raw)
            if kind == "branch":
                walk(raw.get("then") or raw.get("then_steps") or [])
                walk(raw.get("else") or raw.get("else_steps") or [])
                continue
            tool = str(raw.get("tool") or "")
            args = raw.get("arguments") if isinstance(raw.get("arguments"), Mapping) else {}
            browser_mode = "read" if tool in {"browser_list_tabs", "browser_observe", "browser_find"} else "write"
            if tool.startswith("browser_"):
                tab = args.get("tab_handle")
                if isinstance(tab, str):
                    add("browser_tab", tab, browser_mode)
            native_mode = "read" if tool in {"mac_snapshot", "mac_observe"} else "write"
            if tool.startswith("mac_") or tool == "open_app":
                window = args.get("window_handle")
                app_handle = args.get("app_handle")
                app = args.get("app")
                if isinstance(window, str):
                    add("native_window", window, native_mode)
                elif isinstance(app_handle, str):
                    add("native_app", app_handle, native_mode)
                elif isinstance(app, str):
                    add("native_app", app, native_mode)
            fallback = raw.get("fallback")
            if isinstance(fallback, Mapping):
                walk([fallback])

    walk(list(steps))
    return claims


async def execute_computer_plan(
    call_tool: NestedCaller,
    *,
    steps: Sequence[Mapping[str, Any]] | list[dict[str, Any]],
    max_seconds: float = 45.0,
    plan_version: int = 1,
    max_recoveries: int = 4,
    max_recovery_seconds: float = 12.0,
    max_action_units: int = _MAX_ACTION_UNITS,
    resources: Optional[Sequence[Mapping[str, Any]]] = None,
    admission_root: Optional[Path] = None,
) -> dict[str, Any]:
    version = int(plan_version)
    normalized = _validate_steps(list(steps), version=version)
    try:
        max_seconds_value = float(max_seconds)
        recovery_seconds_value = float(max_recovery_seconds)
        recoveries_value = int(max_recoveries)
        action_units_value = int(max_action_units)
    except (TypeError, ValueError) as exc:
        raise ComputerPlanError("plan budgets must be numeric") from exc
    if max_seconds_value <= 0 or max_seconds_value > _MAX_SECONDS:
        raise ComputerPlanError(f"max_seconds must be > 0 and <= {_MAX_SECONDS:g}")
    if recoveries_value < 0 or recoveries_value > _MAX_RECOVERIES:
        raise ComputerPlanError(f"max_recoveries must be between 0 and {_MAX_RECOVERIES}")
    if recovery_seconds_value <= 0 or recovery_seconds_value > _MAX_RECOVERY_SECONDS:
        raise ComputerPlanError(f"max_recovery_seconds must be > 0 and <= {_MAX_RECOVERY_SECONDS:g}")
    if action_units_value <= 0 or action_units_value > 64:
        raise ComputerPlanError("max_action_units must be between 1 and 64")

    budget = _Budget(
        max_seconds=max_seconds_value,
        max_action_units=action_units_value,
        max_recoveries=recoveries_value,
        max_recovery_seconds=recovery_seconds_value,
    )
    outputs: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    failure: Optional[dict[str, Any]] = None
    resource_lease_id: Optional[str] = None
    resource_preflight: dict[str, Any] = {"requested": bool(resources), "admitted": True}

    if resources:
        if admission_root is None:
            raise ComputerPlanError("resource preflight requires an admission_root")
        try:
            lease = request_resource_lease(
                Path(admission_root),
                owner_id="plan_" + uuid.uuid4().hex[:12],
                resources=resources,
                ttl_s=max(30, int(max_seconds_value) + 15),
            )
        except AdmissionError as exc:
            return {
                "ok": False,
                "reason_code": "RESOURCE_PREFLIGHT_FAILED",
                "error": str(exc),
                "resource_preflight": {"requested": True, "admitted": False, "error": exc.code, **dict(exc.details or {})},
                "steps": [],
                "final_result": None,
                "plan_stats": {
                    "plan_version": version, "steps_requested": len(_all_ids(normalized)), "steps_executed": 0,
                    "model_tool_calls": 1, "nested_tool_calls": 0, "recoveries_used": 0,
                    "action_units_used": 0, "duration_ms": 0,
                },
            }
        if not lease.get("admitted"):
            return {
                "ok": False,
                "reason_code": "RESOURCE_BUSY",
                "error": "computer_plan resource preflight found a conflicting active owner",
                "resource_preflight": {"requested": True, "admitted": False, "blockers": lease.get("blockers") or []},
                "steps": [], "final_result": None,
                "plan_stats": {
                    "plan_version": version, "steps_requested": len(_all_ids(normalized)), "steps_executed": 0,
                    "model_tool_calls": 1, "nested_tool_calls": 0, "recoveries_used": 0,
                    "action_units_used": 0, "duration_ms": 0,
                },
            }
        resource_lease_id = str(lease.get("lease_id") or "") or None
        resource_preflight = {"requested": True, "admitted": True, "resource_count": len(lease.get("resources") or [])}

    try:
        await _execute_steps(
            call_tool, steps=normalized, outputs=outputs, records=records, budget=budget, version=version,
        )
    except _PlanStop as stop:
        failure = {
            "reason_code": stop.reason_code,
            "error": stop.error,
            "step_id": stop.step_id,
            **stop.details,
        }
    finally:
        if resource_lease_id and admission_root is not None:
            try:
                admission_release(Path(admission_root), lease_id=resource_lease_id)
            except Exception:
                pass

    executed = sum(1 for item in records if item.get("status") != "skipped")
    requested = len(_all_ids(normalized))
    elapsed_ms = int(budget.elapsed() * 1000)
    reduction = 0.0 if requested <= 1 else round((1.0 - (1.0 / requested)) * 100.0, 1)
    final_result = None
    if records:
        for record in reversed(records):
            sid = str(record.get("id") or "")
            if sid in outputs:
                final_result = outputs[sid]
                break

    response: dict[str, Any] = {
        "ok": failure is None,
        "steps": records,
        "final_result": final_result,
        "resource_preflight": resource_preflight,
        "plan_stats": {
            "plan_version": version,
            "steps_requested": requested,
            "steps_executed": executed,
            "model_tool_calls": 1,
            "nested_tool_calls": budget.nested_calls,
            "standalone_equivalent_tool_calls": requested,
            "model_round_trip_reduction_percent": reduction,
            "duration_ms": elapsed_ms,
            "max_steps": _MAX_STEPS if version == 1 else _MAX_EXPANDED_STEPS,
            "max_action_units": budget.max_action_units,
            "action_units_used": budget.action_units,
            "max_seconds": budget.max_seconds,
            "max_recoveries": budget.max_recoveries,
            "recoveries_used": budget.recoveries,
            "max_recovery_seconds": budget.max_recovery_seconds,
            "recovery_duration_ms": int(budget.recovery_seconds * 1000),
        },
    }
    if failure is not None:
        response.update(failure)
    return response
