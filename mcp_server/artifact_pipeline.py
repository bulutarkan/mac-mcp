from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_ARTIFACT_TTL_S = 2 * 60 * 60
_MAX_ARTIFACTS = 256
_LOCK = threading.RLock()
_ARTIFACTS: Dict[str, Dict[str, Any]] = {}


class ArtifactError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_file(path: str | os.PathLike[str]) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ArtifactError("ARTIFACT_SYMLINK_REFUSED", "Artifact paths must not be symlinks.")
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ArtifactError("ARTIFACT_NOT_FOUND", f"Artifact file does not exist: {raw}") from exc
    if not resolved.is_file():
        raise ArtifactError("ARTIFACT_NOT_FILE", f"Artifact path is not a regular file: {resolved}")
    return resolved


def _identity(path: Path) -> Dict[str, Any]:
    before = path.stat()
    digest = _sha256(path)
    after = path.stat()
    before_signature = (int(before.st_size), int(before.st_mtime_ns), int(before.st_dev), int(before.st_ino))
    after_signature = (int(after.st_size), int(after.st_mtime_ns), int(after.st_dev), int(after.st_ino))
    if before_signature != after_signature:
        raise ArtifactError(
            "ARTIFACT_CHANGED_DURING_HASH",
            "Artifact changed while its identity hash was being computed; wait for completion and register it again.",
            path=str(path),
        )
    return {
        "path": str(path),
        "filename": path.name,
        "size": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "sha256": digest,
    }


def _prune(now: Optional[float] = None) -> None:
    moment = time.time() if now is None else float(now)
    expired = [key for key, row in _ARTIFACTS.items() if moment - float(row.get("registered_at", 0)) > _ARTIFACT_TTL_S]
    for key in expired:
        _ARTIFACTS.pop(key, None)
    if len(_ARTIFACTS) > _MAX_ARTIFACTS:
        oldest = sorted(_ARTIFACTS.items(), key=lambda item: float(item[1].get("registered_at", 0)))
        for key, _ in oldest[: len(_ARTIFACTS) - _MAX_ARTIFACTS]:
            _ARTIFACTS.pop(key, None)


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "artifact_id", "path", "filename", "size", "mtime_ns", "sha256", "source", "registered_at"
        )
    }


def register_artifact(path: str | os.PathLike[str], *, source: str = "local") -> Dict[str, Any]:
    target = _canonical_file(path)
    ident = _identity(target)
    artifact_id = f"artifact_{uuid.uuid4().hex[:16]}"
    row = {
        **ident,
        "artifact_id": artifact_id,
        "source": str(source or "local")[:80],
        "registered_at": time.time(),
    }
    with _LOCK:
        _prune(row["registered_at"])
        _ARTIFACTS[artifact_id] = row
    return _public(row)


def resolve_artifact(
    artifact_id: str,
    *,
    expected_path: Optional[str | os.PathLike[str]] = None,
    verify_hash: bool = True,
) -> Dict[str, Any]:
    key = str(artifact_id or "").strip()
    if not re.fullmatch(r"artifact_[0-9a-f]{16}", key):
        raise ArtifactError("ARTIFACT_ID_INVALID", "artifact_id is invalid or malformed.")
    with _LOCK:
        _prune()
        stored = dict(_ARTIFACTS.get(key) or {})
    if not stored:
        raise ArtifactError("ARTIFACT_UNKNOWN", "artifact_id is unknown or expired; register the file again.")
    path = _canonical_file(str(stored["path"]))
    if expected_path is not None:
        expected = Path(expected_path).expanduser().resolve(strict=False)
        if expected != path:
            raise ArtifactError(
                "ARTIFACT_PATH_MISMATCH",
                "The supplied path does not match the registered artifact identity.",
                registered_path=str(path),
            )
    current_stat = path.stat()
    stat_fields = {
        "size": int(current_stat.st_size),
        "mtime_ns": int(current_stat.st_mtime_ns),
        "device": int(current_stat.st_dev),
        "inode": int(current_stat.st_ino),
    }
    stale = any(int(stored.get(key_name, -1)) != value for key_name, value in stat_fields.items())
    current_hash: Optional[str] = None
    if verify_hash and not stale:
        current_hash = _sha256(path)
        stale = current_hash != stored.get("sha256")
    if stale:
        raise ArtifactError(
            "ARTIFACT_STALE",
            "The artifact file changed after registration; register the intended file again before using it.",
            path=str(path),
        )
    return _public(stored)


def _run_osascript(script: str, *, timeout_s: float = 10.0) -> Tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True,
            timeout=max(0.5, min(float(timeout_s), 30.0)),
        )
    except subprocess.TimeoutExpired:
        return False, "", "AppleScript timed out"
    except Exception as exc:
        return False, "", str(exc)
    return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _panel_script(pid: int, *, mode: str, command: str) -> str:
    panel_description = "open" if mode == "open" else "save"
    return f'''
tell application "System Events"
    set p to first application process whose unix id is {int(pid)}
    set panelRef to missing value
    repeat with w in windows of p
        repeat with sh in sheets of w
            try
                if (description of sh as text) is "{panel_description}" then
                    set panelRef to sh
                    exit repeat
                end if
            end try
        end repeat
        if panelRef is not missing value then exit repeat
    end repeat
    if panelRef is missing value then error "MAC_MCP_FILE_DIALOG_NOT_FOUND"
    {command}
end tell
'''


def _panel_exists(pid: int, mode: str) -> bool:
    ok, stdout, _ = _run_osascript(_panel_script(pid, mode=mode, command='return "FOUND"'), timeout_s=2)
    return bool(ok and stdout == "FOUND")


def wait_for_file_dialog(pid: int, mode: str, *, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + max(0.2, min(float(timeout_s), 20.0))
    while time.monotonic() < deadline:
        if _panel_exists(pid, mode):
            return True
        time.sleep(0.08)
    return False


def _wait_panel_closed(pid: int, mode: str, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.2, min(float(timeout_s), 15.0))
    while time.monotonic() < deadline:
        if not _panel_exists(pid, mode):
            return True
        time.sleep(0.08)
    return False


def _apple_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _set_go_to_path(pid: int, mode: str, target: Path, *, timeout_s: float) -> None:
    path_value = _apple_string(str(target))
    command = f'''
set frontmost of p to true
keystroke "g" using {{command down, shift down}}
delay 0.15
set fieldRef to value of attribute "AXFocusedUIElement" of p
if role of fieldRef is not "AXTextField" then error "MAC_MCP_GO_TO_FIELD_NOT_FOUND"
set value of fieldRef to {path_value}
key code 36
delay 0.25
'''
    ok, _, error = _run_osascript(_panel_script(pid, mode=mode, command=command), timeout_s=timeout_s)
    if not ok:
        raise ArtifactError("FILE_DIALOG_NAVIGATION_FAILED", error or "Could not navigate the file dialog.")


def _click_panel_button(pid: int, mode: str, title: str, *, timeout_s: float) -> None:
    expected = {"open": "Open", "save": "Save"}[mode]
    if title == "Cancel":
        command = "key code 53"
    elif title == expected:
        command = "key code 36"
    else:
        raise ArtifactError("FILE_DIALOG_BUTTON_INVALID", f"Unexpected {mode} dialog button: {title}")
    ok, _, error = _run_osascript(_panel_script(pid, mode=mode, command=command), timeout_s=timeout_s)
    if not ok:
        raise ArtifactError("FILE_DIALOG_BUTTON_FAILED", error or f"Could not activate {title} in file dialog.")


def _set_save_name(pid: int, filename: str, *, timeout_s: float) -> None:
    value = _apple_string(filename)
    command = f'''
set fieldRef to value of attribute "AXFocusedUIElement" of p
if role of fieldRef is not "AXTextField" then error "MAC_MCP_SAVE_NAME_FIELD_NOT_FOUND"
if description of fieldRef is not "text field" then error "MAC_MCP_SAVE_NAME_FIELD_NOT_FOUND"
set value of fieldRef to {value}
'''
    ok, _, error = _run_osascript(_panel_script(pid, mode="save", command=command), timeout_s=timeout_s)
    if not ok:
        raise ArtifactError("FILE_DIALOG_SAVE_NAME_FAILED", error or "Could not set the Save As filename.")


def _click_replace_if_present(pid: int, *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + max(0.2, min(float(timeout_s), 5.0))
    script = f'''
tell application "System Events"
    set p to first application process whose unix id is {int(pid)}
    repeat with w in windows of p
        repeat with sh in sheets of w
            try
                if exists button "Replace" of sh then
                    click button "Replace" of sh
                    return "REPLACED"
                end if
            end try
            try
                set elems to entire contents of sh
                repeat with e in elems
                    if role of e is "AXButton" and title of e is "Replace" then
                        click e
                        return "REPLACED"
                    end if
                end repeat
            end try
        end repeat
    end repeat
    return "NONE"
end tell
'''
    while time.monotonic() < deadline:
        ok, stdout, _ = _run_osascript(script, timeout_s=1.5)
        if ok and stdout == "REPLACED":
            return True
        time.sleep(0.08)
    return False


def _wait_file_stable(
    path: Path, *, timeout_s: float = 8.0, stable_ms: int = 300,
    require_changed_from: Optional[Tuple[int, int]] = None,
) -> bool:
    deadline = time.monotonic() + max(0.2, min(float(timeout_s), 30.0))
    stable_since: Optional[float] = None
    previous: Optional[Tuple[int, int]] = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            if not path.is_file():
                raise FileNotFoundError
            signature = (int(stat.st_size), int(stat.st_mtime_ns))
        except (FileNotFoundError, OSError):
            signature = None
        now = time.monotonic()
        if signature is not None and signature == previous:
            if require_changed_from is not None and signature == require_changed_from:
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = now
                if (now - stable_since) * 1000 >= max(50, int(stable_ms)):
                    return True
        else:
            previous = signature
            stable_since = now if signature is not None else None
        time.sleep(0.08)
    return False


def drive_native_file_dialog(
    *,
    pid: int,
    mode: str,
    artifact_id: Optional[str] = None,
    path: Optional[str] = None,
    destination: Optional[str] = None,
    overwrite: bool = False,
    cancel: bool = False,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"open", "save"}:
        raise ArtifactError("FILE_DIALOG_MODE_INVALID", "file dialog mode must be open or save.")
    bounded = max(1.0, min(float(timeout_s), 30.0))
    if not wait_for_file_dialog(int(pid), normalized, timeout_s=min(bounded, 8.0)):
        raise ArtifactError("FILE_DIALOG_NOT_FOUND", f"No native {normalized} file dialog is open for the target application.")
    if cancel:
        _click_panel_button(int(pid), normalized, "Cancel", timeout_s=bounded)
        closed = _wait_panel_closed(int(pid), normalized, timeout_s=min(5.0, bounded))
        return {"ok": bool(closed), "cancelled": True, "mode": normalized, "panel_closed": closed}

    if normalized == "open":
        if not artifact_id or not path:
            raise ArtifactError("ARTIFACT_REQUIRED", "Open dialogs require artifact_id and matching path.")
        artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
        target = Path(str(artifact["path"]))
        _set_go_to_path(int(pid), "open", target, timeout_s=bounded)
        _click_panel_button(int(pid), "open", "Open", timeout_s=bounded)
        closed = _wait_panel_closed(int(pid), "open", timeout_s=min(5.0, bounded))
        if not closed:
            raise ArtifactError("FILE_DIALOG_DID_NOT_CLOSE", "The Open dialog remained visible after selecting the artifact.")
        # Re-verify after selection so a concurrent replacement cannot silently pass.
        artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
        return {"ok": True, "mode": "open", "selected": True, "panel_closed": True, "artifact": artifact}

    if not destination:
        raise ArtifactError("DESTINATION_REQUIRED", "Save dialogs require destination.")
    dest = Path(destination).expanduser().resolve(strict=False)
    preexisting = dest.exists()
    before_signature: Optional[Tuple[int, int]] = None
    if preexisting:
        if not dest.is_file():
            raise ArtifactError("DESTINATION_NOT_FILE", "Existing destination is not a regular file.", destination=str(dest))
        before_stat = dest.stat()
        before_signature = (int(before_stat.st_size), int(before_stat.st_mtime_ns))
        if not overwrite:
            raise ArtifactError("DESTINATION_EXISTS", "Destination already exists and overwrite=false.", destination=str(dest))
    if not dest.parent.exists() or not dest.parent.is_dir():
        raise ArtifactError("DESTINATION_PARENT_MISSING", f"Destination directory does not exist: {dest.parent}")
    _set_go_to_path(int(pid), "save", dest.parent, timeout_s=bounded)
    _set_save_name(int(pid), dest.name, timeout_s=bounded)
    _click_panel_button(int(pid), "save", "Save", timeout_s=bounded)
    replaced = False
    if preexisting:
        replaced = _click_replace_if_present(int(pid), timeout_s=min(2.5, bounded))
        if not replaced:
            raise ArtifactError(
                "OVERWRITE_CONFIRMATION_NOT_VERIFIED",
                "The destination existed but the macOS Replace confirmation was not observed and confirmed.",
                destination=str(dest),
            )
    if not _wait_file_stable(dest, timeout_s=bounded, stable_ms=300, require_changed_from=before_signature):
        raise ArtifactError("SAVE_COMPLETION_TIMEOUT", "Saved file did not reach a new stable completed state.", destination=str(dest))
    artifact = register_artifact(dest, source="native_save")
    return {
        "ok": True, "mode": "save", "saved": True, "destination": str(dest),
        "overwrite": bool(overwrite), "replace_confirmed": replaced, "artifact": artifact,
    }


def _preview_document_paths() -> list[str]:
    script = r'''tell application "Preview"
set out to ""
repeat with d in documents
    set p to ""
    try
        set p to POSIX path of (path of d)
    on error
        try
            set p to path of d as text
        end try
    end try
    if p is not "" then
        if out is not "" then set out to out & (ASCII character 30)
        set out to out & p
    end if
end repeat
return out
end tell'''
    ok, stdout, _ = _run_osascript(script, timeout_s=3)
    return [part for part in stdout.split(chr(30)) if part] if ok and stdout else []


def _preview_front_document_path() -> Optional[str]:
    script = r'''tell application "Preview"
if (count of documents) is 0 then return ""
try
    return POSIX path of (path of front document)
on error
    try
        return path of front document as text
    end try
end try
return ""
end tell'''
    ok, stdout, _ = _run_osascript(script, timeout_s=3)
    return stdout if ok and stdout else None


def open_in_preview(artifact_id: str, path: str, *, foreground: bool = False, timeout_s: float = 10.0) -> Dict[str, Any]:
    artifact = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
    args = ["open"]
    if not foreground:
        args.append("-g")
    args.extend(["-a", "Preview", str(artifact["path"])])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=max(1.0, min(float(timeout_s), 20.0)))
    if proc.returncode != 0:
        raise ArtifactError("PREVIEW_OPEN_FAILED", (proc.stderr or "Preview open failed").strip())
    deadline = time.monotonic() + max(1.0, min(float(timeout_s), 20.0))
    expected = Path(str(artifact["path"])).resolve(strict=True)
    while time.monotonic() < deadline:
        current_paths = [_preview_front_document_path()] if foreground else _preview_document_paths()
        for current in current_paths:
            if current:
                try:
                    if Path(current).expanduser().resolve(strict=False) == expected:
                        return {"ok": True, "opened": True, "app": "Preview", "foreground": bool(foreground), "artifact": artifact}
                except Exception:
                    pass
        time.sleep(0.1)
    raise ArtifactError("PREVIEW_DOCUMENT_NOT_VERIFIED", "Preview did not expose the exact registered artifact as its front document.")


def preview_save_as(
    artifact_id: str,
    path: str,
    destination: str,
    *,
    overwrite: bool = False,
    preserve_focus: bool = True,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    source = resolve_artifact(artifact_id, expected_path=path, verify_hash=True)
    dest = Path(destination).expanduser().resolve(strict=False)
    if dest.exists() and not overwrite:
        raise ArtifactError("DESTINATION_EXISTS", "Destination already exists and overwrite=false.", destination=str(dest))
    focus_context = None
    if preserve_focus:
        from .tools_ui import _capture_focus_context
        focus_context, focus_error = _capture_focus_context()
        if focus_context is None:
            raise ArtifactError("FOCUS_SNAPSHOT_FAILED", focus_error or "Could not capture current focus before Preview Save As.")
    try:
        open_in_preview(artifact_id, path, foreground=True, timeout_s=min(timeout_s, 8.0))
        current = _preview_front_document_path()
        if not current or Path(current).expanduser().resolve(strict=False) != Path(str(source["path"])).resolve(strict=True):
            raise ArtifactError("PREVIEW_SOURCE_MISMATCH", "Preview front document does not match the registered source artifact.")
        ok, stdout, error = _run_osascript('''tell application "System Events"
tell application process "Preview"
    set frontmost to true
    click menu item "Save As…" of menu "File" of menu bar 1
    return unix id
end tell
end tell''', timeout_s=5)
        if not ok:
            raise ArtifactError("PREVIEW_SAVE_AS_FAILED", error or stdout or "Could not open Preview Save As panel.")
        try:
            pid = int(str(stdout).strip())
        except ValueError as exc:
            raise ArtifactError("PREVIEW_PID_UNAVAILABLE", "Could not resolve Preview process identity.") from exc
        result = drive_native_file_dialog(
            pid=pid, mode="save", destination=str(dest), overwrite=overwrite, timeout_s=timeout_s,
        )
        result["source_artifact"] = source
        result["app"] = "Preview"
        return result
    finally:
        if preserve_focus and focus_context is not None:
            from .tools_ui import _restore_focus_context
            _restore_focus_context(focus_context)


def artifact_pipeline(
    *,
    action: str,
    path: Optional[str] = None,
    artifact_id: Optional[str] = None,
    destination: Optional[str] = None,
    overwrite: bool = False,
    preserve_focus: bool = True,
    timeout_s: float = 15.0,
) -> Dict[str, Any]:
    mode = str(action or "").strip().lower().replace("-", "_")
    try:
        if mode == "register":
            if not path:
                raise ArtifactError("PATH_REQUIRED", "register requires path.")
            return {"ok": True, "action": mode, "artifact": register_artifact(path, source="registered")}
        if mode == "inspect":
            if not artifact_id:
                raise ArtifactError("ARTIFACT_REQUIRED", "inspect requires artifact_id.")
            return {"ok": True, "action": mode, "artifact": resolve_artifact(artifact_id, expected_path=path, verify_hash=True)}
        if mode == "open_preview":
            if not artifact_id or not path:
                raise ArtifactError("ARTIFACT_REQUIRED", "open_preview requires artifact_id and matching path.")
            return {"action": mode, **open_in_preview(artifact_id, path, foreground=False, timeout_s=timeout_s)}
        if mode == "preview_save_as":
            if not artifact_id or not path or not destination:
                raise ArtifactError("ARTIFACT_REQUIRED", "preview_save_as requires artifact_id, matching path, and destination.")
            return {"action": mode, **preview_save_as(
                artifact_id, path, destination, overwrite=overwrite,
                preserve_focus=preserve_focus, timeout_s=timeout_s,
            )}
        raise ArtifactError("ARTIFACT_ACTION_INVALID", "action must be register, inspect, open_preview, or preview_save_as.")
    except ArtifactError as exc:
        return {"ok": False, "action": mode, "error": str(exc), "reason_code": exc.code, **exc.extra}
