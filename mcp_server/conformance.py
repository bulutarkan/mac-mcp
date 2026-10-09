from __future__ import annotations

import inspect
import time
from typing import Callable
from unittest.mock import patch

from . import agent_admission, browser_tabs
from .perception import PERCEPTION_LADDER, finalize_perception_telemetry
from .diagnostics import FAIL, PASS, WARN, CheckResult, build_report, result
from .tools_browser import browser_activate_tab, browser_coordinate_click, browser_open_url, browser_press_key
from . import computer_plan
from .tools_browser_agent import (
    _effect_changed,
    _element_readiness_js,
    _render_readiness_js,
    _wait_for_element_readiness,
    _event_wait_js,
    browser_act,
    browser_observe,
)
from .tools_ui import (
    _delegated_native_human_guard, _native_action_focus_policy, _observation_script,
    _semantic_text_write, act_ui, observe_ui,
)

CONFORMANCE_BASELINE_VERSION = 8


def _contract(
    check_id: str,
    summary: str,
    probe: Callable[[], bool],
    *,
    focus_safe: bool | None = None,
    details: dict | None = None,
) -> CheckResult:
    started = time.perf_counter()
    try:
        ok = bool(probe())
    except Exception as exc:
        data = dict(details or {})
        data["error_type"] = type(exc).__name__
        return result(
            check_id, "computer_use", FAIL, "CONFORMANCE_EXCEPTION",
            f"{summary} (probe raised {type(exc).__name__}).", started=started, details=data,
        )
    data = dict(details or {})
    if focus_safe is not None:
        data["focus_safe"] = focus_safe
    return result(
        check_id, "computer_use", PASS if ok else FAIL,
        "CONFORMANCE_PASS" if ok else "CONFORMANCE_REGRESSION",
        summary, started=started, details=data,
    )


def _parameter_default(function: Callable, name: str):
    return inspect.signature(function).parameters[name].default


def _stable_chrome_handle() -> bool:
    return browser_tabs._chrome_handle("42") == browser_tabs._chrome_handle("42") and browser_tabs._chrome_handle("42") != browser_tabs._chrome_handle("43")


def _origin_normalization() -> bool:
    return (
        browser_tabs._origin("https://Example.com/path?q=1") == "https://example.com"
        and browser_tabs._origin("https://example.com:443/a") == "https://example.com"
        and browser_tabs._origin("http://example.com:8080/a") == "http://example.com:8080"
    )


def _lease_ttl_bounded() -> bool:
    value = browser_tabs._lease_ttl_s()
    return 30 <= value <= 3600


def _stale_handle_rejected() -> bool:
    old_registry = dict(browser_tabs._REGISTRY)
    try:
        browser_tabs._REGISTRY.clear()
        fake = [{
            "browser": "Google Chrome", "window_index": 1, "tab_index": 1,
            "active": True, "native_id": "100", "title": "Fixture", "url": "https://example.test/",
        }]
        with patch("mcp_server.browser_tabs._scan", return_value=fake):
            handle = browser_tabs.list_tabs("Google Chrome")[0]["tab_handle"]
        with patch("mcp_server.browser_tabs._scan", return_value=[]):
            try:
                browser_tabs.resolve_tab("Google Chrome", handle)
            except KeyError:
                return True
        return False
    finally:
        browser_tabs._REGISTRY.clear()
        browser_tabs._REGISTRY.update(old_registry)


def _render_reason_codes_present() -> bool:
    js = _render_readiness_js("full_page", None)
    return "ZERO_CONTENT_BOUNDS" in js and "RENDER_NOT_READY" in js and "raw_width" in js


def _element_readiness_contract_present() -> bool:
    js = _element_readiness_js("e_fixture", "click", 120)
    required = ["pointer_events", "reason_code", "ELEMENT_DETACHED", "ready"]
    return all(item in js for item in required)


def _missing_element_fails_closed() -> bool:
    output = _wait_for_element_readiness(None, "Safari", {"type": "click"}, 1, None, None)
    return output.get("ready") is False and output.get("reason_code") == "ELEMENT_NOT_READY" and output.get("_js_calls") == 0


def _effect_change_detection() -> bool:
    base = {"url": "https://example.test/a", "title": "A", "dom_revision": 1, "connected": True, "value": "", "aria_checked": "false", "modal_fingerprint": "", "activation_network_count": 0}
    same = dict(base)
    unrelated_dom = dict(base, dom_revision=2)
    control_changed = dict(base, aria_checked="true")
    modal_changed = dict(base, modal_fingerprint="div|dialog|open|Details|400|500")
    network_changed = dict(base, activation_network_count=1)
    navigated = dict(base, url="https://example.test/b")
    return (
        (not _effect_changed(base, same))
        and (not _effect_changed(base, unrelated_dom))
        and _effect_changed(base, control_changed)
        and _effect_changed(base, modal_changed)
        and _effect_changed(base, network_changed)
        and _effect_changed(base, navigated)
    )


def _trusted_input_contract_present() -> bool:
    source = inspect.getsource(__import__("mcp_server.tools_browser_agent", fromlist=["_verified_dom_action"])._verified_dom_action)
    required = (
        'input_mode == "trusted"',
        'browser != "Google Chrome"',
        '"TRUSTED_INPUT_UNAVAILABLE"',
        'request_dispatch_mouse',
        '"foreground_fallback": False',
        '"automatic_retry": False',
    )
    return all(item in source for item in required)


def _action_no_effect_contract() -> bool:
    source = inspect.getsource(__import__("mcp_server.tools_browser_agent", fromlist=["_verified_dom_action"])._verified_dom_action)
    return "ACTION_NO_EFFECT" in source and '"automatic_retry": False' in source and "read-only and bounded" in source


def _keyboard_focus_safe_default() -> bool:
    return _parameter_default(browser_press_key, "allow_foreground") is False


def _coordinate_focus_safe_default() -> bool:
    return _parameter_default(browser_coordinate_click, "allow_foreground") is False


def _browser_act_focus_safe_default() -> bool:
    return _parameter_default(browser_act, "allow_foreground") is False


def _background_open_default() -> bool:
    return _parameter_default(browser_open_url, "background") is True


def _foreground_self_escalation_blocked() -> bool:
    from fastapi import HTTPException
    try:
        browser_activate_tab(None, browser="Safari", window_index=1, tab_index=1, allow_foreground=True)
    except HTTPException as exc:
        return (
            exc.status_code == 403
            and isinstance(exc.detail, dict)
            and exc.detail.get("reason_code") == "FOREGROUND_NOT_AUTHORIZED"
        )
    return False


def _activate_tab_always_user_visible() -> bool:
    from fastapi import HTTPException
    try:
        browser_activate_tab(None, browser="Safari", window_index=1, tab_index=1, allow_foreground=False)
    except HTTPException as exc:
        return isinstance(exc.detail, dict) and exc.detail.get("reason_code") == "FOREGROUND_NOT_AUTHORIZED"
    return False


def _batch_action_limit_present() -> bool:
    source = inspect.getsource(browser_act)
    return "_MAX_ACTIONS" in source and "non-empty list" in source


def _closed_loop_plan_contract_present() -> bool:
    source = inspect.getsource(computer_plan)
    required = (
        "RECOVERY_AMBIGUOUS_TARGET",
        "_safe_recovery_candidate",
        "ACTION_NO_EFFECT",
        "outcome_unknown",
        "max_recoveries",
        "request_resource_lease",
        "wait_until",
    )
    return all(item in source for item in required)


def _native_semantic_identity_contract_present() -> bool:
    script = _observation_script("System Settings", 1, 1, 2)
    return (
        'value of attribute "AXIdentifier" of nodeRef' in script
        and "identifierText" in script
    )


def _native_background_semantic_input_contract_present() -> bool:
    replace_policy = _native_action_focus_policy({
        "type": "type", "element_id": "w1/1", "text": "x", "clear": True,
    })
    insert_policy = _native_action_focus_policy({
        "type": "paste", "element_id": "w1/1", "text": "x",
    })
    source = inspect.getsource(_semantic_text_write)
    return (
        replace_policy == "background_semantic"
        and insert_policy == "background_semantic"
        and 'attribute = "AXValue" if replace else "AXSelectedText"' in source
        and "activate=False" in source
    )


def _native_foreground_self_escalation_blocked() -> bool:
    policy = _native_action_focus_policy({
        "type": "type",
        "element_id": "w1/1",
        "text": "x",
        "input_mode": "foreground",
    })
    source = inspect.getsource(act_ui)
    return (
        policy == "foreground_required"
        and "current_foreground_authorization()" in source
        and '"FOREGROUND_REQUIRED"' in source
        and "preserve_focus" in source
    )


def _workspace_human_priority_contract_present() -> bool:
    tab_signature = inspect.signature(browser_tabs.tab_lease)
    # tab_lease resolves through _lease_fresh_row, which owns the takeover check.
    tab_source = inspect.getsource(browser_tabs.tab_lease) + inspect.getsource(browser_tabs._lease_fresh_row)
    native_source = inspect.getsource(_delegated_native_human_guard)
    act_source = inspect.getsource(act_ui)
    return (
        tab_signature.parameters["mutation"].default is False
        and "browser_human_takeover" in tab_source
        and "HUMAN_ACTIVE_RESOURCE" in native_source
        and "delegated_agent_identity" in native_source
        and "_delegated_native_human_guard" in act_source
        and "human_takeover_during_action" in act_source
    )


def _workspace_resource_lease_contract_present() -> bool:
    write = {"kind": "native_window", "id": "app:win", "mode": "write"}
    read = {"kind": "native_window", "id": "app:win", "mode": "read"}
    admission_source = inspect.getsource(agent_admission.request_resource_lease)
    heartbeat_source = inspect.getsource(agent_admission.heartbeat)
    return (
        bool(agent_admission.resources_conflict([write], [read]))
        and not agent_admission.resources_conflict([read], [read])
        and '"generation"' in admission_source
        and '"last_activity_at"' in admission_source
        and '"resource_modes"' in admission_source
        and '"last_activity_at"' in heartbeat_source
    )


def _event_delta_pipeline_contract_present() -> bool:
    browser_source = _event_wait_js(
        {"for": "selector", "selector": "#ready", "timeout_s": 1.0},
        "https://example.test/",
        1.0,
    )
    native_signature = inspect.signature(observe_ui)
    return (
        "MutationObserver" in browser_source
        and "bounded_fallback" in browser_source
        and "history.pushState" in browser_source
        and native_signature.parameters["include_screenshot"].default is False
        and "previous_observation_id" in native_signature.parameters
    )


def _perception_ladder_contract_present() -> bool:
    browser_signature = inspect.signature(browser_observe)
    plan_source = inspect.getsource(computer_plan)
    return (
        tuple(PERCEPTION_LADDER)
        == ("snapshot", "semantic", "conditional", "targeted_visual", "ocr_full_visual")
        and "previous_observation_id" in browser_signature.parameters
        and browser_signature.parameters["visual"].default == "none"
        and "previous_observation_id" in inspect.signature(observe_ui).parameters
        and 'tool_name in {"mac_observe", "browser_observe"}' in plan_source
    )


def _bounded_perception_telemetry_contract_present() -> bool:
    payload = {"ok": True, "telemetry": {}}
    finalize_perception_telemetry(
        payload,
        stage="semantic",
        state_mode="full",
        node_count=1,
        duration_ms=1,
        context_budget_bytes=4096,
    )
    telemetry = payload.get("telemetry") or {}
    context = telemetry.get("context_budget") or {}
    native_source = inspect.getsource(observe_ui)
    return (
        int(telemetry.get("payload_bytes") or 0) > 0
        and int(telemetry.get("payload_tokens_estimate") or 0) > 0
        and telemetry.get("node_count") == 1
        and context.get("limit_bytes") == 4096
        and "ocr" in inspect.signature(observe_ui).parameters
        and "previous_observation_id" in native_source
    )


def deterministic_checks() -> list[CheckResult]:
    specs = [
        ("browser.background_open_default", "Browser URL opens default to background/non-focus-stealing mode.", _background_open_default, True),
        ("browser.foreground_capability", "Model-visible foreground flags cannot self-authorize browser focus changes.", _foreground_self_escalation_blocked, True),
        ("browser.activate_tab_user_gate", "Selecting the browser current/active tab is treated as explicit user-visible foreground behavior.", _activate_tab_always_user_visible, True),
        ("browser.action_focus_default", "Semantic browser actions default to no foreground focus stealing.", _browser_act_focus_safe_default, True),
        ("browser.keyboard_focus_default", "Native browser key fallback requires explicit foreground permission.", _keyboard_focus_safe_default, True),
        ("browser.coordinate_focus_default", "Native coordinate click fallback requires explicit foreground permission.", _coordinate_focus_safe_default, True),
        ("browser.chrome_handle_stability", "Chrome stable handles are derived from native tab identity, not tab index.", _stable_chrome_handle, None),
        ("browser.origin_normalization", "Origin comparison normalizes default ports and path/query data.", _origin_normalization, None),
        ("browser.lease_ttl_bounds", "Logical tab lease TTL stays within bounded safety limits.", _lease_ttl_bounded, None),
        ("browser.stale_handle", "Closed/stale browser handles fail closed instead of rebinding silently.", _stale_handle_rejected, None),
        ("browser.render_readiness", "Render readiness exposes zero-bounds/not-ready reason codes.", _render_reason_codes_present, None),
        ("browser.element_readiness", "Element actionability has explicit readiness/reason-code signals.", _element_readiness_contract_present, None),
        ("browser.missing_element", "Missing element IDs fail before any browser execution call.", _missing_element_fails_closed, None),
        ("browser.effect_detection", "Post-action verification accepts semantic/control/modal/network/navigation progress but rejects unrelated DOM churn.", _effect_change_detection, None),
        ("browser.trusted_input_boundary", "Trusted background pointer input is explicit Chrome-only capability and never auto-escalates Safari/foreground behavior.", _trusted_input_contract_present, True),
        ("browser.no_effect", "Clicks with no observed effect are failures and are never auto-replayed.", _action_no_effect_contract, None),
        ("browser.batch_bounds", "Browser action batches have an explicit bounded action limit.", _batch_action_limit_present, None),
        ("computer_plan.closed_loop_recovery", "Composite computer plans expose bounded fail-closed recovery, semantic rebind, wait and resource-preflight contracts.", _closed_loop_plan_contract_present, None),
        ("native.semantic_identity", "Native observations include AXIdentifier-backed semantic identity for stale-target recovery.", _native_semantic_identity_contract_present, None),
        ("native.background_semantic_input", "Native text input prefers AXValue/AXSelectedText background writes without foreground activation.", _native_background_semantic_input_contract_present, True),
        ("native.foreground_capability", "Model-visible native input options cannot self-authorize foreground keyboard/pointer control.", _native_foreground_self_escalation_blocked, True),
        ("workspace.human_priority", "Delegated browser/native mutations yield when the user owns the visible target, including mid-action takeover races.", _workspace_human_priority_contract_present, True),
        ("workspace.resource_lease_contract", "Interactive resource leases expose generation/activity/mode semantics and preserve read-read sharing with write exclusion.", _workspace_resource_lease_contract_present, None),
        ("observe.event_delta_pipeline", "Browser waits prefer event wakeups with bounded fallback and native observe exposes opt-in conditional delta/not-modified reads.", _event_delta_pipeline_contract_present, True),
        ("observe.perception_ladder", "Computer perception is semantic-first, reuses conditional observations, and escalates to targeted visual/OCR only as needed.", _perception_ladder_contract_present, True),
        ("observe.bounded_context_telemetry", "Perception results expose bounded payload/token/node metadata without raw-content telemetry.", _bounded_perception_telemetry_contract_present, None),
    ]
    return [_contract(check_id, summary, probe, focus_safe=focus_safe) for check_id, summary, probe, focus_safe in specs]


def live_checks() -> list[CheckResult]:
    # Live mode deliberately reuses doctor-style read-only checks rather than
    # clicking/typing in the user's real apps. Real destructive/live workflows can
    # be added later as isolated fixtures without making default CI flaky.
    from .diagnostics import doctor_checks

    wanted = {
        "permissions.accessibility",
        "server.health",
        "companion.safari",
        "companion.chrome_files",
        "companion.runtime",
    }
    rows = [row for row in doctor_checks() if row.check_id in wanted]
    converted: list[CheckResult] = []
    for row in rows:
        status = row.status
        # An intentionally closed Chrome browser is not a deterministic product
        # regression. Keep it visible in live mode without failing the conformance run.
        if row.check_id == "companion.runtime" and status == WARN:
            status = WARN
        converted.append(CheckResult(
            check_id=f"live.{row.check_id}", category="computer_use_live", status=status,
            reason_code=row.reason_code, summary=row.summary, duration_ms=row.duration_ms,
            remediation=row.remediation, details=row.details,
        ))
    return converted


def run_conformance(*, live: bool = False) -> dict:
    started = time.perf_counter()
    checks = deterministic_checks()
    deterministic_count = len(checks)
    if live:
        checks.extend(live_checks())
    report = build_report(
        checks,
        kind="computer-use-conformance",
        extra={
            "baseline_version": CONFORMANCE_BASELINE_VERSION,
            "deterministic_check_count": deterministic_count,
            "live_enabled": bool(live),
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "metrics": {
                "focus_safe_contracts": sum(1 for row in checks if (row.details or {}).get("focus_safe") is True and row.status == PASS),
                "focus_safety_regressions": sum(1 for row in checks if (row.details or {}).get("focus_safe") is True and row.status == FAIL),
                "deterministic_pass_rate": round(
                    (sum(1 for row in checks[:deterministic_count] if row.status == PASS) / deterministic_count) * 100, 2
                ) if deterministic_count else 0.0,
            },
        },
    )
    return report
