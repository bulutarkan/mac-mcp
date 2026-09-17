from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from . import browser_tabs
from .artifact_pipeline import register_artifact, resolve_artifact
from .data_guard import redact_sensitive_text
from .native_targets import lookup_window
from .security import Settings

_HANDOFF_TTL_S = 30 * 60
_MAX_HANDOFFS = 256
_MAX_SELECTION_CHARS = 32_000
_MAX_PREVIEW_CHARS = 500
_LOCK = threading.RLock()
_HANDOFFS: Dict[str, Dict[str, Any]] = {}


class HandoffError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _origin(url: str) -> Optional[str]:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _trust_class(url: str) -> str:
    origin = _origin(url)
    if not origin:
        return "unknown"
    host = urlsplit(origin).hostname or ""
    return "local_trusted" if host == "localhost" or host.endswith(".localhost") or host in {"127.0.0.1", "::1"} else "untrusted_web"


def _prune(now: Optional[float] = None) -> None:
    moment = time.time() if now is None else float(now)
    expired = [key for key, row in _HANDOFFS.items() if moment - float(row.get("created_at") or 0) > _HANDOFF_TTL_S]
    for key in expired:
        _HANDOFFS.pop(key, None)
    if len(_HANDOFFS) > _MAX_HANDOFFS:
        ordered = sorted(_HANDOFFS.items(), key=lambda item: float(item[1].get("created_at") or 0))
        for key, _ in ordered[: len(_HANDOFFS) - _MAX_HANDOFFS]:
            _HANDOFFS.pop(key, None)


def _sealed_material(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": row.get("version"),
        "kind": row.get("kind"),
        "source": row.get("source"),
        "target": row.get("target"),
        "payload": row.get("payload"),
        "created_at": row.get("created_at"),
    }


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row.get("payload") or {})
    public_payload: Dict[str, Any]
    if row.get("kind") == "text_url":
        rendered = str(payload.get("rendered_text") or "")
        selected = str(payload.get("selected_text") or "")
        preview = redact_sensitive_text(rendered[:_MAX_PREVIEW_CHARS])
        public_payload = {
            "text_length": len(rendered),
            "selected_text_length": len(selected),
            "text_sha256": hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest(),
            "preview": preview,
            "preview_truncated": len(rendered) > _MAX_PREVIEW_CHARS,
            "include_url": bool(payload.get("include_url")),
        }
    else:
        artifact = dict(payload.get("artifact") or {})
        public_payload = {
            "artifact_id": artifact.get("artifact_id"),
            "path": artifact.get("path"),
            "filename": artifact.get("filename"),
            "size": artifact.get("size"),
            "sha256": artifact.get("sha256"),
        }
    return {
        "ok": True,
        "handoff_id": row.get("handoff_id"),
        "version": row.get("version"),
        "kind": row.get("kind"),
        "source": dict(row.get("source") or {}),
        "target": dict(row.get("target") or {}),
        "payload": public_payload,
        "integrity": {
            "algorithm": "sha256",
            "envelope_sha256": row.get("envelope_sha256"),
        },
        "created_at": row.get("created_at"),
        "expires_at": float(row.get("created_at") or 0) + _HANDOFF_TTL_S,
        "single_use": True,
        "consumed": bool(row.get("consumed_at")),
        "consumed_at": row.get("consumed_at"),
        "consumed_by": row.get("consumed_by"),
    }


def _store(kind: str, source: Dict[str, Any], target: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    created_at = time.time()
    row: Dict[str, Any] = {
        "handoff_id": f"handoff_{uuid.uuid4().hex[:20]}",
        "version": 1,
        "kind": kind,
        "source": source,
        "target": target,
        "payload": payload,
        "created_at": created_at,
        "consumed_at": None,
        "consumed_by": None,
    }
    row["envelope_sha256"] = _digest(_sealed_material(row))
    with _LOCK:
        _prune(created_at)
        _HANDOFFS[row["handoff_id"]] = row
    return _public(row)


def _resolve(handoff_id: str, *, allow_consumed: bool = False) -> Dict[str, Any]:
    key = str(handoff_id or "").strip()
    if not key.startswith("handoff_") or len(key) != 28:
        raise HandoffError("HANDOFF_ID_INVALID", "handoff_id is invalid or malformed.")
    with _LOCK:
        _prune()
        stored = dict(_HANDOFFS.get(key) or {})
    if not stored:
        raise HandoffError("HANDOFF_UNKNOWN", "handoff_id is unknown or expired; create the handoff again.")
    expected = str(stored.get("envelope_sha256") or "")
    if not expected or _digest(_sealed_material(stored)) != expected:
        raise HandoffError("HANDOFF_INTEGRITY_FAILED", "The handoff envelope failed its integrity check.")
    if stored.get("consumed_at") and not allow_consumed:
        raise HandoffError("HANDOFF_CONSUMED", "The handoff was already consumed; create a new handoff before replaying the transfer.")
    if stored.get("kind") == "artifact":
        artifact = dict((stored.get("payload") or {}).get("artifact") or {})
        try:
            resolved = resolve_artifact(
                str(artifact.get("artifact_id") or ""),
                expected_path=str(artifact.get("path") or ""),
                verify_hash=True,
            )
        except Exception as exc:
            code = getattr(exc, "code", "HANDOFF_ARTIFACT_INVALID")
            raise HandoffError(str(code), str(exc)) from exc
        if resolved.get("sha256") != artifact.get("sha256"):
            raise HandoffError("HANDOFF_ARTIFACT_IDENTITY_MISMATCH", "The artifact no longer matches the identity sealed into the handoff.")
    return stored


def mark_handoff_consumed(handoff_id: str, *, consumer: str) -> Dict[str, Any]:
    key = str(handoff_id or "").strip()
    with _LOCK:
        row = _HANDOFFS.get(key)
        if row is None:
            raise HandoffError("HANDOFF_UNKNOWN", "handoff_id is unknown or expired.")
        if row.get("consumed_at"):
            raise HandoffError("HANDOFF_CONSUMED", "The handoff was already consumed.")
        row["consumed_at"] = time.time()
        row["consumed_by"] = str(consumer or "unknown")[:80]
        return _public(dict(row))


def _validate_native_target(target: Dict[str, Any]) -> Dict[str, Any]:
    app = str(target.get("app") or "").strip()
    app_handle = str(target.get("app_handle") or "").strip()
    window_handle = str(target.get("window_handle") or "").strip()
    if not app or not app_handle or not window_handle:
        raise HandoffError("HANDOFF_NATIVE_TARGET_REQUIRED", "Native handoffs require target_app, target_app_handle, and target_window_handle from mac_observe.")
    record = lookup_window(window_handle)
    if record is None:
        raise HandoffError("HANDOFF_TARGET_WINDOW_UNKNOWN", "target_window_handle is unknown or expired; observe the target window again.")
    if record.get("app_handle") != app_handle or str(record.get("app_name") or "").lower() != app.lower():
        raise HandoffError("HANDOFF_TARGET_MISMATCH", "The supplied native target handles do not identify the requested application/window.")
    return record


def _native_text_target(
    *, target_app: str, target_app_handle: str, target_window_handle: str,
    target_observation_id: str, target_element_id: str, clear: bool,
) -> Dict[str, Any]:
    target = {
        "kind": "native_text",
        "app": str(target_app or "").strip(),
        "app_handle": str(target_app_handle or "").strip(),
        "window_handle": str(target_window_handle or "").strip(),
        "observation_id": str(target_observation_id or "").strip(),
        "element_id": str(target_element_id or "").strip(),
        "clear": bool(clear),
    }
    if not target["observation_id"] or not target["element_id"]:
        raise HandoffError("HANDOFF_TEXT_TARGET_REQUIRED", "Text handoffs require target_observation_id and target_element_id from mac_observe.")
    record = _validate_native_target(target)
    target["window_title"] = str(record.get("title") or "")
    target["window_identity_kind"] = str(record.get("identity_kind") or "")
    target["window_identity_status"] = str(record.get("identity_status") or "")
    return target


def _native_file_target(*, target_app: str, target_app_handle: str, target_window_handle: str) -> Dict[str, Any]:
    target = {
        "kind": "native_file_dialog",
        "app": str(target_app or "").strip(),
        "app_handle": str(target_app_handle or "").strip(),
        "window_handle": str(target_window_handle or "").strip(),
    }
    record = _validate_native_target(target)
    target["window_title"] = str(record.get("title") or "")
    target["window_identity_kind"] = str(record.get("identity_kind") or "")
    target["window_identity_status"] = str(record.get("identity_status") or "")
    return target


def _mail_draft_attachment_target(*, target_app: str, target_app_handle: str, target_window_handle: str) -> Dict[str, Any]:
    target = {
        "kind": "mail_draft_attachment",
        "app": str(target_app or "").strip(),
        "app_handle": str(target_app_handle or "").strip(),
        "window_handle": str(target_window_handle or "").strip(),
    }
    record = _validate_native_target(target)
    if target["app"].lower() != "mail":
        raise HandoffError("HANDOFF_MAIL_TARGET_REQUIRED", "mail_draft_attachment requires a Mail compose window target.")
    title = str(record.get("title") or "").strip()
    if not title or str(record.get("identity_status") or "") != "stable":
        raise HandoffError(
            "HANDOFF_MAIL_DRAFT_IDENTITY_REQUIRED",
            "Mail draft handoff requires a uniquely titled compose window; set a unique subject and observe it again.",
        )
    target["draft_subject"] = title
    target["window_title"] = title
    target["window_identity_kind"] = str(record.get("identity_kind") or "")
    target["window_identity_status"] = "stable"
    return target


def _browser_upload_target(*, target_browser: str, target_tab_handle: str, target_css_selector: str) -> Dict[str, Any]:
    browser = str(target_browser or "").strip()
    handle = str(target_tab_handle or "").strip()
    selector = str(target_css_selector or "").strip()
    if not browser or not handle or not selector:
        raise HandoffError("HANDOFF_BROWSER_TARGET_REQUIRED", "Browser artifact handoffs require target_browser, target_tab_handle, and target_css_selector.")
    try:
        _, _, row = browser_tabs.resolve_tab(browser, handle)
    except KeyError as exc:
        raise HandoffError("HANDOFF_TARGET_TAB_UNKNOWN", str(exc)) from exc
    return {
        "kind": "browser_upload",
        "browser": str(row.get("browser") or browser),
        "tab_handle": handle,
        "url": str(row.get("url") or ""),
        "title": str(row.get("title") or ""),
        "css_selector": selector,
    }


def _browser_source(browser: str, tab_handle: str) -> Dict[str, Any]:
    try:
        _, _, row = browser_tabs.resolve_tab(browser, tab_handle)
    except KeyError as exc:
        raise HandoffError("HANDOFF_SOURCE_TAB_UNKNOWN", str(exc)) from exc
    url = str(row.get("url") or "")
    return {
        "kind": "browser",
        "browser": str(row.get("browser") or browser),
        "tab_handle": str(row.get("tab_handle") or tab_handle),
        "url": url,
        "origin": _origin(url),
        "title": str(row.get("title") or ""),
        "provenance_class": _trust_class(url),
    }


def create_browser_text_handoff(
    settings: Settings, *, browser: str, tab_handle: str,
    target_app: str, target_app_handle: str, target_window_handle: str,
    target_observation_id: str, target_element_id: str,
    include_url: bool = True, clear: bool = False,
) -> Dict[str, Any]:
    target = _native_text_target(
        target_app=target_app, target_app_handle=target_app_handle,
        target_window_handle=target_window_handle, target_observation_id=target_observation_id,
        target_element_id=target_element_id, clear=clear,
    )
    source = _browser_source(browser, tab_handle)
    from .tools_browser import browser_execute_js
    result = browser_execute_js(
        settings, browser=browser, tab_handle=tab_handle,
        js=(
            "JSON.stringify({selection:String(window.getSelection?window.getSelection().toString():''),"
            "url:String(location.href),title:String(document.title)})"
        ),
    )
    try:
        captured = json.loads(str(result.get("result") or "{}"))
    except json.JSONDecodeError as exc:
        raise HandoffError("HANDOFF_BROWSER_CAPTURE_INVALID", "Browser selection capture returned invalid JSON.") from exc
    selected = str(captured.get("selection") or "")
    if len(selected) > _MAX_SELECTION_CHARS:
        raise HandoffError("HANDOFF_TEXT_TOO_LARGE", f"Selected browser text exceeds the {_MAX_SELECTION_CHARS}-character handoff limit.")
    captured_url = str(captured.get("url") or source.get("url") or "")
    if captured_url != source.get("url"):
        raise HandoffError("HANDOFF_SOURCE_NAVIGATED", "The source tab navigated while the handoff was being captured; create it again from the current page.")
    source["title"] = str(captured.get("title") or source.get("title") or "")
    if include_url:
        rendered = selected.rstrip() + (("\n\nSource: " + captured_url) if selected else captured_url)
    else:
        rendered = selected
    if not rendered:
        raise HandoffError("HANDOFF_EMPTY_TEXT", "The browser selection is empty and include_url=false; there is nothing to transfer.")
    payload = {
        "selected_text": selected,
        "url": captured_url,
        "include_url": bool(include_url),
        "rendered_text": rendered,
    }
    return _store("text_url", source, target, payload)


def create_artifact_handoff(
    settings: Settings, *, source_type: str, path: str,
    target_kind: str, artifact_id: Optional[str] = None,
    source_browser: Optional[str] = None, source_tab_handle: Optional[str] = None,
    target_app: Optional[str] = None, target_app_handle: Optional[str] = None,
    target_window_handle: Optional[str] = None,
    target_browser: Optional[str] = None, target_tab_handle: Optional[str] = None,
    target_css_selector: Optional[str] = None,
) -> Dict[str, Any]:
    source_kind = str(source_type or "").strip().lower().replace("-", "_")
    if source_kind == "finder":
        from .tools_snapshot import _read_selected_context
        selected = _read_selected_context(settings, selected_file_limit=30)
        canonical = str(Path(path).expanduser().resolve(strict=True))
        selected_paths = {str(Path(item).expanduser().resolve(strict=False)) for item in selected.get("selected_paths") or []}
        if canonical not in selected_paths:
            raise HandoffError("HANDOFF_FINDER_SELECTION_MISMATCH", "The requested file is not currently selected in Finder.")
        artifact = resolve_artifact(artifact_id, expected_path=canonical, verify_hash=True) if artifact_id else register_artifact(canonical, source="finder_handoff")
        source = {"kind": "finder", "path": canonical, "provenance_class": "local"}
    elif source_kind == "browser_artifact":
        if not artifact_id:
            raise HandoffError("HANDOFF_ARTIFACT_REQUIRED", "browser_artifact source requires artifact_id from browser_wait_for_download.")
        artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
        if str(artifact.get("source") or "") != "browser_download":
            raise HandoffError("HANDOFF_BROWSER_ARTIFACT_SOURCE_MISMATCH", "The artifact was not registered by browser_wait_for_download.")
        if not source_browser or not source_tab_handle:
            raise HandoffError("HANDOFF_BROWSER_SOURCE_REQUIRED", "browser_artifact source requires source_browser and source_tab_handle.")
        source = _browser_source(source_browser, source_tab_handle)
        source["artifact_source"] = "browser_download"
    elif source_kind == "artifact":
        if not artifact_id:
            raise HandoffError("HANDOFF_ARTIFACT_REQUIRED", "artifact source requires artifact_id.")
        artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
        if str(artifact.get("source") or "") == "browser_download":
            raise HandoffError(
                "HANDOFF_BROWSER_PROVENANCE_REQUIRED",
                "Browser-downloaded artifacts must use source_type=browser_artifact with the source browser/tab so provenance is preserved.",
            )
        source = {"kind": "artifact", "artifact_source": artifact.get("source"), "provenance_class": "local"}
    else:
        raise HandoffError("HANDOFF_SOURCE_TYPE_INVALID", "source_type must be finder, browser_artifact, or artifact.")

    kind = str(target_kind or "").strip().lower().replace("-", "_")
    if kind == "native_file_dialog":
        target = _native_file_target(
            target_app=str(target_app or ""), target_app_handle=str(target_app_handle or ""),
            target_window_handle=str(target_window_handle or ""),
        )
    elif kind == "mail_draft_attachment":
        target = _mail_draft_attachment_target(
            target_app=str(target_app or ""), target_app_handle=str(target_app_handle or ""),
            target_window_handle=str(target_window_handle or ""),
        )
    elif kind == "browser_upload":
        target = _browser_upload_target(
            target_browser=str(target_browser or ""), target_tab_handle=str(target_tab_handle or ""),
            target_css_selector=str(target_css_selector or ""),
        )
    else:
        raise HandoffError("HANDOFF_TARGET_KIND_INVALID", "target_kind must be native_file_dialog, mail_draft_attachment, or browser_upload.")
    return _store("artifact", source, target, {"artifact": artifact})


def inspect_handoff(handoff_id: str) -> Dict[str, Any]:
    return _public(_resolve(handoff_id, allow_consumed=True))


def resolve_native_text_handoff(
    handoff_id: str, *, app: str, app_handle: str, window_handle: str,
    observation_id: str, element_id: str,
) -> Dict[str, Any]:
    row = _resolve(handoff_id)
    if row.get("kind") != "text_url":
        raise HandoffError("HANDOFF_KIND_MISMATCH", "This handoff does not contain text/URL content.")
    target = dict(row.get("target") or {})
    expected = {
        "app": str(app or "").strip(), "app_handle": str(app_handle or "").strip(),
        "window_handle": str(window_handle or "").strip(), "observation_id": str(observation_id or "").strip(),
        "element_id": str(element_id or "").strip(),
    }
    for key, value in expected.items():
        if str(target.get(key) or "") != value:
            raise HandoffError("HANDOFF_TARGET_MISMATCH", f"The native {key} does not match the target sealed into the handoff.")
    return {
        "handoff_id": row["handoff_id"],
        "text": str((row.get("payload") or {}).get("rendered_text") or ""),
        "clear": bool(target.get("clear")),
        "source": dict(row.get("source") or {}),
    }


def resolve_mail_text_handoff(
    handoff_id: str, *, app: str, app_handle: str, window_handle: str,
    observation_id: str, element_id: str,
) -> Dict[str, Any]:
    resolved = resolve_native_text_handoff(
        handoff_id, app=app, app_handle=app_handle, window_handle=window_handle,
        observation_id=observation_id, element_id=element_id,
    )
    row = _resolve(handoff_id)
    target = dict(row.get("target") or {})
    if str(target.get("app") or "").lower() != "mail":
        raise HandoffError("HANDOFF_MAIL_TARGET_REQUIRED", "This text handoff is not bound to a Mail compose window.")
    subject = str(target.get("window_title") or "").strip()
    if not subject or target.get("window_identity_status") != "stable":
        raise HandoffError(
            "HANDOFF_MAIL_DRAFT_IDENTITY_REQUIRED",
            "Mail draft text handoff requires a uniquely titled compose window; set a unique subject and create the handoff again.",
        )
    resolved["draft_subject"] = subject
    return resolved


def resolve_mail_attachment_handoff(
    handoff_id: str, *, app: str, app_handle: str, window_handle: str,
) -> Dict[str, Any]:
    row = _resolve(handoff_id)
    target = dict(row.get("target") or {})
    if row.get("kind") != "artifact" or target.get("kind") != "mail_draft_attachment":
        raise HandoffError("HANDOFF_KIND_MISMATCH", "This handoff is not bound to a Mail draft attachment target.")
    for key, value in {"app": app, "app_handle": app_handle, "window_handle": window_handle}.items():
        if str(target.get(key) or "") != str(value or ""):
            raise HandoffError("HANDOFF_TARGET_MISMATCH", f"The native {key} does not match the Mail draft sealed into the handoff.")
    if str(target.get("app") or "").lower() != "mail":
        raise HandoffError("HANDOFF_MAIL_TARGET_REQUIRED", "This attachment handoff is not bound to Mail.")
    subject = str(target.get("draft_subject") or "").strip()
    if not subject:
        raise HandoffError("HANDOFF_MAIL_DRAFT_IDENTITY_REQUIRED", "The handoff does not contain a stable Mail draft subject.")
    artifact = dict((row.get("payload") or {}).get("artifact") or {})
    return {
        "handoff_id": row["handoff_id"], "artifact": artifact,
        "draft_subject": subject, "source": dict(row.get("source") or {}),
    }


def resolve_native_file_handoff(
    handoff_id: str, *, app: str, app_handle: str, window_handle: str,
    artifact_id: Optional[str] = None, path: Optional[str] = None,
) -> Dict[str, Any]:
    row = _resolve(handoff_id)
    if row.get("kind") != "artifact" or (row.get("target") or {}).get("kind") != "native_file_dialog":
        raise HandoffError("HANDOFF_KIND_MISMATCH", "This handoff is not bound to a native file dialog.")
    target = dict(row.get("target") or {})
    for key, value in {"app": app, "app_handle": app_handle, "window_handle": window_handle}.items():
        if str(target.get(key) or "") != str(value or ""):
            raise HandoffError("HANDOFF_TARGET_MISMATCH", f"The native {key} does not match the target sealed into the handoff.")
    artifact = dict((row.get("payload") or {}).get("artifact") or {})
    if artifact_id and str(artifact.get("artifact_id")) != str(artifact_id):
        raise HandoffError("HANDOFF_ARTIFACT_MISMATCH", "artifact_id does not match the artifact sealed into the handoff.")
    if path and Path(str(artifact.get("path"))).resolve(strict=False) != Path(path).expanduser().resolve(strict=False):
        raise HandoffError("HANDOFF_ARTIFACT_MISMATCH", "path does not match the artifact sealed into the handoff.")
    return {"handoff_id": row["handoff_id"], "artifact": artifact, "source": dict(row.get("source") or {})}


def resolve_browser_upload_handoff(
    handoff_id: str, *, browser: str, tab_handle: str, current_url: str,
    css_selector: str, artifact_id: str, path: str,
) -> Dict[str, Any]:
    row = _resolve(handoff_id)
    if row.get("kind") != "artifact" or (row.get("target") or {}).get("kind") != "browser_upload":
        raise HandoffError("HANDOFF_KIND_MISMATCH", "This handoff is not bound to a browser upload target.")
    target = dict(row.get("target") or {})
    if str(target.get("browser") or "").lower() != str(browser or "").lower():
        raise HandoffError("HANDOFF_TARGET_MISMATCH", "browser does not match the target sealed into the handoff.")
    if str(target.get("tab_handle") or "") != str(tab_handle or ""):
        raise HandoffError("HANDOFF_TARGET_MISMATCH", "tab_handle does not match the target sealed into the handoff.")
    if str(target.get("css_selector") or "") != str(css_selector or ""):
        raise HandoffError("HANDOFF_TARGET_MISMATCH", "css_selector does not match the target sealed into the handoff.")
    if str(target.get("url") or "") != str(current_url or ""):
        raise HandoffError("HANDOFF_TARGET_NAVIGATED", "The target tab navigated after the handoff was created; create a new handoff for the current page.")
    artifact = dict((row.get("payload") or {}).get("artifact") or {})
    if str(artifact.get("artifact_id") or "") != str(artifact_id or ""):
        raise HandoffError("HANDOFF_ARTIFACT_MISMATCH", "artifact_id does not match the artifact sealed into the handoff.")
    if Path(str(artifact.get("path"))).resolve(strict=False) != Path(path).expanduser().resolve(strict=False):
        raise HandoffError("HANDOFF_ARTIFACT_MISMATCH", "path does not match the artifact sealed into the handoff.")
    return {"handoff_id": row["handoff_id"], "artifact": artifact, "source": dict(row.get("source") or {})}


def context_handoff(
    settings: Settings, *, action: str, handoff_id: Optional[str] = None,
    browser: Optional[str] = None, tab_handle: Optional[str] = None,
    source_type: Optional[str] = None, source_browser: Optional[str] = None,
    source_tab_handle: Optional[str] = None, path: Optional[str] = None,
    artifact_id: Optional[str] = None, target_kind: Optional[str] = None,
    target_app: Optional[str] = None, target_app_handle: Optional[str] = None,
    target_window_handle: Optional[str] = None, target_observation_id: Optional[str] = None,
    target_element_id: Optional[str] = None, target_browser: Optional[str] = None,
    target_tab_handle: Optional[str] = None, target_css_selector: Optional[str] = None,
    include_url: bool = True, clear: bool = False,
) -> Dict[str, Any]:
    try:
        operation = str(action or "").strip().lower().replace("-", "_")
        if operation == "create_browser_text":
            return create_browser_text_handoff(
                settings, browser=str(browser or ""), tab_handle=str(tab_handle or ""),
                target_app=str(target_app or ""), target_app_handle=str(target_app_handle or ""),
                target_window_handle=str(target_window_handle or ""),
                target_observation_id=str(target_observation_id or ""), target_element_id=str(target_element_id or ""),
                include_url=include_url, clear=clear,
            )
        if operation == "create_artifact":
            if not path:
                raise HandoffError("HANDOFF_PATH_REQUIRED", "create_artifact requires path.")
            return create_artifact_handoff(
                settings, source_type=str(source_type or ""), path=path, artifact_id=artifact_id,
                source_browser=source_browser, source_tab_handle=source_tab_handle,
                target_kind=str(target_kind or ""), target_app=target_app,
                target_app_handle=target_app_handle, target_window_handle=target_window_handle,
                target_browser=target_browser, target_tab_handle=target_tab_handle,
                target_css_selector=target_css_selector,
            )
        if operation == "inspect":
            if not handoff_id:
                raise HandoffError("HANDOFF_ID_REQUIRED", "inspect requires handoff_id.")
            return inspect_handoff(handoff_id)
        raise HandoffError("HANDOFF_ACTION_INVALID", "action must be create_browser_text, create_artifact, or inspect.")
    except HandoffError as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc), **exc.extra}


def reset_handoffs_for_tests() -> None:
    with _LOCK:
        _HANDOFFS.clear()
