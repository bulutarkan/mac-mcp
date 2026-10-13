"""Auditable runs of computer_plan and recipes (#108).

Every computer_plan and recipe run gets a run_id (observability.current_run_id) that is
stamped on the run's own telemetry row and on every nested step, so a run can be
replayed as a timeline from existing, sanitized telemetry. This module keeps a small
per-run record (what ran, in what order, with what outcome) and, only when the owner
turned on run evidence, before/after screenshots for the steps that asked for them
(step evidence: before|after|both). Nothing here records full screen recordings or
transcripts.

Screenshots are never kept while a password or one-time-code field is on screen: that
checkpoint is stored as withheld instead. Records and images live under
~/.mac-mcp/runs with owner-only permissions and expire together after the chosen
retention period (1, 7 or 30 days).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from . import privacy_policy

from .data_guard import redact_sensitive_text
from .private_storage import ensure_private_dir
from .runtime_settings import run_evidence_privacy
from . import paths

_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{12}$")
_FILE_RE = re.compile(r"^[0-9]{2}-[A-Za-z0-9_.-]{1,64}-(before|after)\.jpg$")
_STEP_KEYS = ("index", "id", "type", "tool", "ok", "status", "duration_ms", "reason_code", "recovered", "branch")
_MAX_EVIDENCE_PER_RUN = 40
_MAX_IMAGE_BYTES = 1_500_000
_SENSITIVE_INPUT_TYPES = {"password"}


def runs_root() -> Path:
    return ensure_private_dir(paths.runs_dir())


def valid_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and bool(_RUN_ID_RE.fullmatch(run_id))


def _write_private(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _clean(value: Any, limit: int = 300) -> Optional[str]:
    if value is None:
        return None
    return redact_sensitive_text(" ".join(str(value).split()))[:limit]


# --- evidence capture -------------------------------------------------------

def _content_parts(result: Any) -> tuple[List[str], List[bytes]]:
    """Text and image parts of an MCP tool result (content list, or (content, structured))."""
    if isinstance(result, tuple) and result:
        result = result[0]
    texts: List[str] = []
    images: List[bytes] = []
    for item in result if isinstance(result, list) else [result]:
        kind = getattr(item, "type", None)
        if kind == "image" and getattr(item, "data", None):
            try:
                images.append(base64.b64decode(item.data))
            except (ValueError, TypeError):
                continue
        elif kind == "text" and isinstance(getattr(item, "text", None), str):
            texts.append(item.text)
        elif isinstance(item, dict):
            texts.append(json.dumps(item, default=str))
    return texts, images


def sensitive_on_screen(texts: List[str]) -> bool:
    """A password or one-time-code field in the observation that came with the screenshot."""
    for text in texts:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
        elements = []
        if isinstance(data, dict):
            elements = data.get("elements") or (data.get("result") or {}).get("elements") or []
        for element in elements if isinstance(elements, list) else []:
            if not isinstance(element, dict):
                continue
            hints = " ".join(str(element.get(key) or "") for key in ("autocomplete", "name", "aria_label", "placeholder"))
            if str(element.get("input_type") or "").lower() in _SENSITIVE_INPUT_TYPES or "one-time-code" in hints:
                return True
        if "AXSecureTextField" in text:
            return True
    return False


def _target_call(step: Mapping[str, Any]) -> Optional[tuple[str, Dict[str, Any]]]:
    tool = str(step.get("tool") or "")
    args = step.get("arguments") if isinstance(step.get("arguments"), Mapping) else {}
    note = {"description": "Run evidence checkpoint"}
    if tool.startswith("browser_") and args.get("tab_handle") and args.get("browser"):
        return "browser_observe", {"browser": args["browser"], "tab_handle": args["tab_handle"], "scope": "interactive",
                                   "max_elements": 80, "visual": "viewport", **note}
    if tool in {"mac_act", "mac_app", "mac_observe", "open_app"}:
        target = {key: args[key] for key in ("app_handle", "window_handle", "app") if args.get(key)}
        if target:
            return "mac_observe", {**target, "include_screenshot": True, "max_depth": 3, "max_children": 20, **note}
    return None


def make_capture(run_id: str, call_tool: Callable[[str, Dict[str, Any]], Awaitable[Any]]):
    """The computer_plan evidence hook for one run. Called only for steps that opted in."""
    counter = [0]

    async def capture(step: Mapping[str, Any], when: str) -> Optional[Dict[str, Any]]:
        step_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(step.get("id") or "step"))[:64]
        base = {"when": when, "step_id": step_id}
        if counter[0] >= _MAX_EVIDENCE_PER_RUN:
            return {**base, "captured": False, "reason": "evidence_limit"}
        target = _target_call(step)
        if target is None:
            return {**base, "captured": False, "reason": "no_window_or_tab_target"}
        texts, images = _content_parts(await call_tool(*target))
        if sensitive_on_screen(texts):
            return {**base, "captured": False, "reason": "withheld_sensitive_field_on_screen"}
        if not images:
            return {**base, "captured": False, "reason": "no_image_returned"}
        image = images[0]
        if len(image) > _MAX_IMAGE_BYTES:
            return {**base, "captured": False, "reason": "image_too_large"}
        counter[0] += 1
        name = f"{counter[0]:02d}-{step_id}-{when}.jpg"
        folder = ensure_private_dir(runs_root() / run_id)
        _write_private(folder / name, image)
        return {**base, "captured": True, "file": name, "bytes": len(image), "target": target[0]}

    return capture


def capture_for_run(run_id: Optional[str], call_tool: Callable[[str, Dict[str, Any]], Awaitable[Any]]):
    """The hook to pass to execute_computer_plan, or None when evidence is off or there is no run."""
    if not valid_run_id(run_id) or not run_evidence_privacy()["capture"] or not privacy_policy.keeps_records():
        return None
    return make_capture(str(run_id), call_tool)


# --- run records ------------------------------------------------------------

def _record_path(run_id: str) -> Path:
    return runs_root() / f"{run_id}.json"


def record_run(
    run_id: Optional[str], *, kind: str, name: Optional[str], started_at: float, result: Mapping[str, Any],
    recipe_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Keep what a run did: its steps and outcomes, sanitized, plus references to kept evidence."""
    if not valid_run_id(run_id) or not privacy_policy.keeps_records():
        return None
    steps = []
    evidence_count = 0
    for record in result.get("steps") or []:
        if not isinstance(record, Mapping):
            continue
        step = {key: record.get(key) for key in _STEP_KEYS if record.get(key) is not None}
        if record.get("error"):
            step["error"] = _clean(record.get("error"))
        evidence = [dict(item) for item in record.get("evidence") or [] if isinstance(item, Mapping)]
        if evidence:
            step["evidence"] = evidence
            evidence_count += sum(1 for item in evidence if item.get("captured"))
        steps.append(step)
    now = time.time()
    data = {
        "run_id": run_id, "kind": kind, "name": _clean(name, 120), "recipe_id": recipe_id,
        "started_at": started_at, "ended_at": now, "duration_ms": int((now - started_at) * 1000),
        "ok": bool(result.get("ok")), "reason_code": result.get("reason_code"), "error": _clean(result.get("error")),
        "failed_step": result.get("step_id"), "steps": steps, "evidence_count": evidence_count,
    }
    _write_private(_record_path(str(run_id)), (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    prune()
    return data


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    if not valid_run_id(run_id):
        return None
    path = _record_path(run_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    prune()
    rows = []
    for path in runs_root().glob("run_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({key: data.get(key) for key in (
            "run_id", "kind", "name", "recipe_id", "started_at", "duration_ms", "ok", "reason_code", "evidence_count")}
            | {"step_count": len(data.get("steps") or [])})
    rows.sort(key=lambda row: float(row.get("started_at") or 0), reverse=True)
    return rows[: max(1, min(int(limit), 200))]


def evidence_file(run_id: str, name: str) -> Optional[Path]:
    if not valid_run_id(run_id) or not _FILE_RE.fullmatch(str(name or "")):
        return None
    path = runs_root() / run_id / name
    return path if path.is_file() else None


def prune(*, now: Optional[float] = None, retention_days: Optional[int] = None) -> int:
    """Remove runs older than the retention period: the record and its screenshots together."""
    days = retention_days if retention_days is not None else run_evidence_privacy()["retention_days"]
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    root = runs_root()
    for path in root.glob("run_*.json"):
        try:
            started = float(json.loads(path.read_text(encoding="utf-8")).get("started_at") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            started = path.stat().st_mtime
        if started < cutoff:
            path.unlink(missing_ok=True)
            shutil.rmtree(root / path.stem, ignore_errors=True)
            removed += 1
    for folder in root.glob("run_*"):
        # Screenshots whose run record never got written (a crash mid-run) expire too.
        if folder.is_dir() and not (root / f"{folder.name}.json").exists() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed
