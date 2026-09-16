from __future__ import annotations

import inspect
import time
from typing import Callable
from unittest.mock import patch

from . import browser_tabs
from .diagnostics import FAIL, PASS, WARN, CheckResult, build_report, result
from .tools_browser import browser_coordinate_click, browser_open_url, browser_press_key
from .tools_browser_agent import (
    _effect_changed,
    _element_readiness_js,
    _render_readiness_js,
    _wait_for_element_readiness,
    browser_act,
)

CONFORMANCE_BASELINE_VERSION = 1


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
    base = {"url": "https://example.test/a", "title": "A", "dom_revision": 1, "connected": True, "value": ""}
    same = dict(base)
    changed = dict(base, dom_revision=2)
    navigated = dict(base, url="https://example.test/b")
    return (not _effect_changed(base, same)) and _effect_changed(base, changed) and _effect_changed(base, navigated)


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


def _batch_action_limit_present() -> bool:
    source = inspect.getsource(browser_act)
    return "_MAX_ACTIONS" in source and "non-empty list" in source


def deterministic_checks() -> list[CheckResult]:
    specs = [
        ("browser.background_open_default", "Browser URL opens default to background/non-focus-stealing mode.", _background_open_default, True),
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
        ("browser.effect_detection", "Post-action verification detects DOM or navigation progress.", _effect_change_detection, None),
        ("browser.no_effect", "Clicks with no observed effect are failures and are never auto-replayed.", _action_no_effect_contract, None),
        ("browser.batch_bounds", "Browser action batches have an explicit bounded action limit.", _batch_action_limit_present, None),
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
