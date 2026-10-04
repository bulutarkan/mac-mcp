#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.perception import safe_perception_metrics
from mcp_server.security import load_settings
from mcp_server.tools_browser_agent import browser_observe
from mcp_server.tools_ui import observe_ui


def _decode_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    text: Optional[str] = None
    if isinstance(result, str):
        text = result
    elif isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, str):
            text = first
        elif hasattr(first, "text"):
            text = str(first.text)
    if not text:
        return {"ok": False, "reason_code": "UNDECODABLE_RESULT"}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "reason_code": "INVALID_JSON_RESULT"}
    return payload if isinstance(payload, dict) else {"ok": False, "reason_code": "INVALID_RESULT_SHAPE"}


def _safe_sample(payload: Mapping[str, Any], wall_ms: float) -> Dict[str, Any]:
    metrics = safe_perception_metrics(payload)
    metrics["wall_ms"] = round(max(0.0, float(wall_ms)), 1)
    metrics["ok"] = bool(payload.get("ok"))
    metrics["not_modified"] = bool(payload.get("not_modified"))
    return metrics


def aggregate_samples(samples: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = [dict(row) for row in samples]
    if not rows:
        return {"sample_count": 0}
    numeric = (
        "wall_ms",
        "duration_ms",
        "payload_bytes",
        "payload_tokens_estimate",
        "visual_bytes",
        "node_count",
        "capture_duration_ms",
        "remote_js_calls",
        "ax_traversals",
    )
    out: Dict[str, Any] = {
        "sample_count": len(rows),
        "tool_calls": len(rows),
        "ok_count": sum(1 for row in rows if row.get("ok")),
        "not_modified_count": sum(1 for row in rows if row.get("not_modified")),
        "budget_exceeded_count": sum(1 for row in rows if row.get("context_budget_exceeded")),
        "state_modes": {},
        "perception_stages": {},
    }
    for key in numeric:
        values = [float(row.get(key) or 0) for row in rows]
        ordered = sorted(values)
        p95_index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95 + 0.999)))
        out[f"avg_{key}"] = round(statistics.fmean(values), 1)
        out[f"p95_{key}"] = round(ordered[p95_index], 1)
    for row in rows:
        mode = str(row.get("state_mode") or "unknown")
        stage = str(row.get("perception_stage") or "unknown")
        out["state_modes"][mode] = out["state_modes"].get(mode, 0) + 1
        out["perception_stages"][stage] = out["perception_stages"].get(stage, 0) + 1
    return out


def _measure(call: Callable[[], Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    started = time.perf_counter()
    payload = _decode_result(call())
    wall_ms = (time.perf_counter() - started) * 1000.0
    return payload, _safe_sample(payload, wall_ms)


def _native_scenario(
    app: str,
    repeats: int,
    *,
    max_depth: int,
    max_children: int,
) -> Dict[str, Any]:
    settings = load_settings()
    optimized: list[Dict[str, Any]] = []
    baseline: list[Dict[str, Any]] = []

    first, first_sample = _measure(
        lambda: observe_ui(
            settings,
            app=app,
            window_index=1,
            max_depth=max_depth,
            max_children=max_children,
            include_screenshot=False,
            ocr=False,
        )
    )
    if not first.get("ok"):
        return {"scenario": f"native:{app}", "skipped": True, "reason_code": str(first.get("reason_code") or "OBSERVE_FAILED")}

    optimized.append(first_sample)
    previous = first.get("observation_id")
    for _ in range(max(0, repeats - 1)):
        payload, sample = _measure(
            lambda previous_id=previous: observe_ui(
                settings,
                app=app,
                window_index=1,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=False,
                ocr=False,
                previous_observation_id=str(previous_id) if previous_id else None,
            )
        )
        optimized.append(sample)
        if payload.get("observation_id"):
            previous = payload.get("observation_id")

    for _ in range(repeats):
        _payload, sample = _measure(
            lambda: observe_ui(
                settings,
                app=app,
                window_index=1,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=True,
                ocr=False,
            )
        )
        baseline.append(sample)

    return {
        "scenario": f"native:{app}",
        "optimized": aggregate_samples(optimized),
        "visual_heavy_baseline": aggregate_samples(baseline),
    }


def _browser_scenario(browser: str, tab_handle: str, repeats: int) -> Dict[str, Any]:
    settings = load_settings()
    optimized: list[Dict[str, Any]] = []
    baseline: list[Dict[str, Any]] = []

    first, first_sample = _measure(
        lambda: browser_observe(
            settings,
            browser=browser,
            tab_handle=tab_handle,
            scope="interactive",
            max_elements=40,
            visual="none",
        )
    )
    if not first.get("ok"):
        return {"scenario": f"browser:{browser}", "skipped": True, "reason_code": str(first.get("reason_code") or "OBSERVE_FAILED")}

    optimized.append(first_sample)
    previous = first.get("observation_id")
    for _ in range(max(0, repeats - 1)):
        payload, sample = _measure(
            lambda previous_id=previous: browser_observe(
                settings,
                browser=browser,
                tab_handle=tab_handle,
                scope="interactive",
                max_elements=40,
                visual="none",
                previous_observation_id=str(previous_id) if previous_id else None,
            )
        )
        optimized.append(sample)
        if payload.get("observation_id"):
            previous = payload.get("observation_id")

    for _ in range(repeats):
        _payload, sample = _measure(
            lambda: browser_observe(
                settings,
                browser=browser,
                tab_handle=tab_handle,
                scope="interactive",
                max_elements=40,
                visual="viewport",
            )
        )
        baseline.append(sample)

    return {
        "scenario": f"browser:{browser}",
        "optimized": aggregate_samples(optimized),
        "visual_heavy_baseline": aggregate_samples(baseline),
    }


def _mixed_scenario(
    app: str,
    browser: str,
    tab_handle: str,
    repeats: int,
    *,
    max_depth: int,
    max_children: int,
) -> Dict[str, Any]:
    settings = load_settings()
    optimized: list[Dict[str, Any]] = []
    baseline: list[Dict[str, Any]] = []
    native_previous: Optional[str] = None
    browser_previous: Optional[str] = None

    for _ in range(repeats):
        native_payload, native_sample = _measure(
            lambda previous_id=native_previous: observe_ui(
                settings,
                app=app,
                window_index=1,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=False,
                ocr=False,
                previous_observation_id=str(previous_id) if previous_id else None,
            )
        )
        if not native_payload.get("ok"):
            return {
                "scenario": f"mixed:{app}+{browser}",
                "skipped": True,
                "reason_code": str(native_payload.get("reason_code") or "NATIVE_OBSERVE_FAILED"),
            }
        optimized.append(native_sample)
        native_previous = native_payload.get("observation_id") or native_previous

        browser_payload, browser_sample = _measure(
            lambda previous_id=browser_previous: browser_observe(
                settings,
                browser=browser,
                tab_handle=tab_handle,
                scope="interactive",
                max_elements=40,
                visual="none",
                previous_observation_id=str(previous_id) if previous_id else None,
            )
        )
        if not browser_payload.get("ok"):
            return {
                "scenario": f"mixed:{app}+{browser}",
                "skipped": True,
                "reason_code": str(browser_payload.get("reason_code") or "BROWSER_OBSERVE_FAILED"),
            }
        optimized.append(browser_sample)
        browser_previous = browser_payload.get("observation_id") or browser_previous

    for _ in range(repeats):
        _native_payload, native_sample = _measure(
            lambda: observe_ui(
                settings,
                app=app,
                window_index=1,
                max_depth=max_depth,
                max_children=max_children,
                include_screenshot=True,
                ocr=False,
            )
        )
        baseline.append(native_sample)
        _browser_payload, browser_sample = _measure(
            lambda: browser_observe(
                settings,
                browser=browser,
                tab_handle=tab_handle,
                scope="interactive",
                max_elements=40,
                visual="viewport",
            )
        )
        baseline.append(browser_sample)

    return {
        "scenario": f"mixed:{app}+{browser}",
        "optimized": aggregate_samples(optimized),
        "visual_heavy_baseline": aggregate_samples(baseline),
    }


def _macos_version() -> str:
    try:
        return subprocess.check_output(["/usr/bin/sw_vers", "-productVersion"], text=True, timeout=2).strip()
    except Exception:
        return platform.mac_ver()[0]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Mac MCP perception benchmark. Output contains only numeric/mode telemetry; "
            "observed UI text, URLs and element contents are never printed."
        )
    )
    parser.add_argument("--apps", nargs="*", default=["Finder", "System Settings", "Notes"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-children", type=int, default=20)
    parser.add_argument("--browser", choices=["Safari", "Chrome", "Google Chrome"])
    parser.add_argument("--tab-handle")
    args = parser.parse_args(argv)

    repeats = max(1, min(int(args.repeats), 20))
    max_depth = max(0, min(int(args.max_depth), 8))
    max_children = max(1, min(int(args.max_children), 100))
    scenarios = [
        _native_scenario(
            app,
            repeats,
            max_depth=max_depth,
            max_children=max_children,
        )
        for app in args.apps
    ]
    if args.browser and args.tab_handle:
        scenarios.append(_browser_scenario(args.browser, args.tab_handle, repeats))
        mixed_app = args.apps[0] if args.apps else "Finder"
        scenarios.append(
            _mixed_scenario(
                mixed_app,
                args.browser,
                args.tab_handle,
                repeats,
                max_depth=max_depth,
                max_children=max_children,
            )
        )

    report = {
        "kind": "mac-mcp-perception-benchmark",
        "read_only": True,
        "hardware": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "macos": _macos_version(),
        },
        "repeats": repeats,
        "native_bounds": {
            "max_depth": max_depth,
            "max_children": max_children,
        },
        "scenarios": scenarios,
        "notes": [
            "payload_tokens_estimate is an approximate bytes/4 planning metric, not provider tokenizer usage",
            "no percentage improvement claim is computed; compare raw measurements on the same hardware/macOS",
            "visual-heavy baseline uses targeted window/viewport capture, not focus-stealing desktop automation",
            "mixed scenario alternates native and browser reads and reports the raw combined tool-call count",
            "native_bounds are benchmark parameters and are reported explicitly; they are not claimed as product defaults",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
