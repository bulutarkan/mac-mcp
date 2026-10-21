"""The Photos library through Mac MCP Photos, a small PhotoKit helper (#45). It never changes the library.

macOS asks the owner once whether "Mac MCP Photos" may see their photos. The helper ships inside
Mac MCP.app and runs from a copy in ~/.mac-mcp/helpers, as Mac MCP Messages Reader does: inside the
bundle the grant would count as Mac MCP.app's, so the copy keeps it to this helper alone. The Photos
app is never opened.

It lists albums, finds items by their metadata (date, album, photo or video, favourite, file name) and
copies chosen originals into a folder; an original kept in iCloud is downloaded for that. Items say only
whether they have a location, never where, and nothing indexes the library in bulk.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import paths
from .messages_reader import _install
from .private_storage import ensure_private_dir

HELPER_NAME = "Mac MCP Photos.app"
MAX_EXPORT = 50


class PhotosError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def bundled_helper() -> Optional[Path]:
    for app in (Path.home() / "Applications" / "Mac MCP.app", Path("/Applications/Mac MCP.app")):
        path = app / "Contents" / "Helpers" / HELPER_NAME
        if path.exists():
            return path
    return None


def installed_helper() -> Path:
    return paths.state_dir() / "helpers" / HELPER_NAME


def _fingerprint(app: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        for part in ("Contents/Info.plist", "Contents/MacOS/MacMCPPhotos", "Contents/_CodeSignature/CodeResources"):
            digest.update((app / part).read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


def helper_app() -> Optional[Path]:
    """The helper copy in ~/.mac-mcp/helpers, refreshed from Mac MCP.app when that one changed."""
    source, target = bundled_helper(), installed_helper()
    if source is not None and _fingerprint(source) != _fingerprint(target):
        try:
            _install(source, target)
        except (OSError, subprocess.SubprocessError):
            pass  # a copy that is already there keeps working
    return target if target.exists() else None


def _run_helper(request: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    app = helper_app()
    if app is None:
        raise PhotosError("PHOTOS_HELPER_MISSING", "Mac MCP Photos is not installed; reinstall the Mac MCP app "
                          "(mac-mcp repair).")
    folder = ensure_private_dir(paths.state_dir() / "tmp")
    with tempfile.TemporaryDirectory(dir=folder) as work:
        request_path, result_path = Path(work) / "request.json", Path(work) / "result.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        # Through Launch Services so the helper answers for itself to the Photos permission.
        try:
            subprocess.run(["/usr/bin/open", "-W", "-n", "-g", str(app), "--args", "--request", str(request_path),
                            "--result", str(result_path)], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timed_out", "message": "Mac MCP Photos did not answer in time."}
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "error": "no_result", "message": "Mac MCP Photos did not answer."}


# Tests swap this for a fake library.
_RUNNER: Callable[[Dict[str, Any], float], Dict[str, Any]] = _run_helper


def _ask(request: Dict[str, Any], deadline_s: float = 90.0) -> Dict[str, Any]:
    # The helper ends itself at the deadline with an answer; waiting a little longer gets that answer.
    answer = _RUNNER({**request, "deadline_s": deadline_s}, deadline_s + 15.0)
    if answer.get("ok"):
        return answer
    if answer.get("error") == "photos_access":
        raise PhotosError(
            "PHOTOS_ACCESS_REQUIRED",
            "Mac MCP Photos may not see the Photos library. The owner allows it in System Settings › Privacy & "
            "Security › Photos (Mac MCP Photos). Do not work around it.", retryable=False,
        )
    code = {"album_not_found": "PHOTOS_ALBUM_NOT_FOUND", "album_not_unique": "PHOTOS_ALBUM_NOT_UNIQUE",
            "bad_request": "APP_ADAPTER_ARGUMENT_INVALID", "library_unavailable": "PHOTOS_LIBRARY_UNAVAILABLE",
            "timed_out": "PHOTOS_TIMED_OUT"}.get(str(answer.get("error")), "PHOTOS_READ_FAILED")
    raise PhotosError(code, str(answer.get("message") or answer.get("error") or "Photos could not be read."))


def access() -> Dict[str, Any]:
    """granted, limited, not_asked, denied, restricted or helper_missing, without asking the owner anything."""
    if helper_app() is None:
        return {"state": "helper_missing"}
    try:
        return {"state": _ask({"action": "access", "prompt": False}, 20.0).get("access", "unknown")}
    except PhotosError as exc:
        return {"state": "error", "message": str(exc)}


def list_albums(*, limit: int) -> Dict[str, Any]:
    answer = _ask({"action": "list_albums", "limit": limit})
    return {"ok": True, "count": answer.get("count", 0), "albums": answer.get("albums", []),
            "truncated": bool(answer.get("truncated")), "access": answer.get("access"), "read_only": True}


def _epoch(value: Optional[str], *, end: bool) -> Optional[float]:
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            moment = datetime.strptime(text, "%Y-%m-%d")
            if end:
                moment = moment.replace(hour=23, minute=59, second=59)
        else:
            moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", f"{value!r} is not a date (YYYY-MM-DD or ISO time).") from exc
    return moment.timestamp()


def find_photos(*, date_from: Optional[str], date_to: Optional[str], album: Optional[str], media_type: Optional[str],
                favorite: Optional[bool], name: Optional[str], limit: int) -> Dict[str, Any]:
    kind = str(media_type or "").strip().lower() or None
    if kind not in {None, "photo", "video"}:
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", "media_type is photo or video.")
    request: Dict[str, Any] = {"action": "find_photos", "limit": limit}
    for key, value in (("from", _epoch(date_from, end=False)), ("to", _epoch(date_to, end=True)),
                       ("album", str(album or "").strip() or None), ("media_type", kind),
                       ("favorite", favorite), ("name", str(name or "").strip() or None)):
        if value is not None:
            request[key] = value
    if "from" in request and "to" in request and request["to"] < request["from"]:
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", "date_to must be after date_from.")
    answer = _ask(request)
    out = {"ok": True, "count": answer.get("count", 0), "total": answer.get("total", 0), "items": answer.get("items", []),
           "truncated": bool(answer.get("truncated")), "order": "newest_first", "access": answer.get("access"),
           "read_only": True, "location": "Items only say has_location; where they were taken is not shared."}
    if answer.get("scan_limited"):
        out["scan_limited"] = True
        out["message"] = "The file name was looked for in the newest 20000 items only; narrow it with dates or an album."
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def default_export_folder() -> Path:
    return Path.home() / "Downloads" / "Mac MCP Photos" / datetime.now().strftime("%Y-%m-%d %H.%M.%S")


def export_photos(ids: List[str], folder: Optional[str]) -> Dict[str, Any]:
    """Copies the originals of these items into folder (default ~/Downloads/Mac MCP Photos/<time>)."""
    wanted = list(dict.fromkeys(item.strip() for item in ids if item and item.strip()))
    if not wanted:
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", "export_photos needs item_id: ids from find_photos, "
                          "separated by commas.")
    if len(wanted) > MAX_EXPORT:
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", f"Export at most {MAX_EXPORT} items at once.")
    target = Path(folder).expanduser() if folder else default_export_folder()
    if target.exists() and not target.is_dir():
        raise PhotosError("APP_ADAPTER_ARGUMENT_INVALID", "path must be a folder.")
    answer = _ask({"action": "export", "ids": wanted, "folder": str(target)}, deadline_s=60.0 + 60.0 * len(wanted))
    rows = []
    for row in answer.get("items", []):
        if row.get("ok"):
            path = Path(str(row.get("path") or ""))
            # Read back: the file is there, not empty, and its digest names exactly what was written.
            if path.is_file() and path.stat().st_size > 0:
                rows.append({"id": row["id"], "ok": True, "path": str(path), "bytes": path.stat().st_size,
                             "sha256": _sha256(path)})
                continue
            rows.append({"id": row["id"], "ok": False, "error": "not_written"})
        else:
            rows.append({"id": row.get("id"), "ok": False, "error": row.get("error"), "message": row.get("message")})
    done = [row for row in rows if row["ok"]]
    return {"ok": bool(done), "exported": len(done), "failed": len(rows) - len(done), "folder": str(target),
            "items": rows, "verified": len(done) == len(wanted) and bool(done), "verification": "file_read_back",
            "library_changed": False}
